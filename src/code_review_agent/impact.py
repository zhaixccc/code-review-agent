"""Impact analysis: which code depends on the symbols this change touches?

Reading a diff alone cannot answer "what else breaks". This module takes the repository snapshot at the reviewed
commit, finds the definitions the diff touches (tree-sitter), then finds their callers/importers/tests across the
repository and renders short code excerpts as *evidence* for the reviewer model.

Honesty rules:
  * Matching is by name. Same-name symbols elsewhere produce false matches; the evidence says how many other
    definitions share the name so the model (and a human) can discount it.
  * Everything here is read-only parsing. Nothing from the repository is executed.
  * Excerpts are redacted for secrets before they can reach the model.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .code_index import (
    SOURCE_SUFFIXES,
    Definition,
    ImportRef,
    ParsedFile,
    extract_definitions,
    family,
    language_for,
    parse,
)
from .config import Settings
from .diff_utils import changed_new_lines
from .models import ChangedFile
from .module_links import imports_link, same_java_package
from .prioritize import is_test_path
from .secrets_guard import is_sensitive_path, redact

logger = logging.getLogger(__name__)

_GENERIC_METHODS = {
    "get", "set", "run", "main", "init", "name", "id", "test", "setup", "teardown", "render", "update", "create", "delete",
    "add", "remove", "handle", "call", "apply", "process", "start", "stop", "close", "open", "read", "write", "load", "save",
    "build", "parse", "index", "list", "map", "filter", "reduce", "then", "catch", "to_string", "tostring", "equals",
    "hashcode", "length", "size", "next", "values", "keys", "items", "execute", "validate", "toString", "hashCode",
}
_KIND_WEIGHT = {"call": 3, "type": 2, "import": 1, "attr": 1, "ref": 0}
_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "function", "else", "do", "try", "with", "new", "throw", "await", "typeof", "super", "this"}
_MAX_REMOVED_PER_FILE = 4
_MAX_NAMES = 40

_DEFINITION_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "python": [
        re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\("),
        re.compile(r"^\s*class\s+([A-Za-z_]\w*)"),
    ],
    "js": [
        re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\*?\s+([A-Za-z_$][\w$]*)\s*[(<]"),
        re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)"),
        re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=>"),
        re.compile(r"^\s*(?:(?:public|private|protected|static|async|readonly|override)\s+)*([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::\s*[^{=]+)?\{\s*$"),
    ],
    "java": [
        re.compile(r"^\s*(?:(?:public|protected|private|static|final|abstract|synchronized|native|default)\s+)+(?:<[^>]+>\s+)?[\w.<>\[\]?,]+(?:\s*<[^>]*>)?\s+([A-Za-z_]\w*)\s*\("),
        re.compile(r"\b(?:class|interface|enum|record)\s+([A-Za-z_]\w*)"),
    ],
}


def _family(language: str) -> str:
    return "js" if language in {"javascript", "typescript", "tsx"} else language


def _defined_names(lines: list[str], language: str) -> set[str]:
    names: set[str] = set()
    for line in lines:
        for pattern in _DEFINITION_PATTERNS.get(_family(language), []):
            match = pattern.search(line)
            if match and match.group(1) not in _KEYWORDS:
                names.add(match.group(1))
    return names


@dataclass
class ChangedSymbol:
    name: str
    kind: str
    path: str
    start_line: int
    end_line: int
    change: str  # signature | body | removed
    search_names: tuple[str, ...]
    container: str = ""


@dataclass
class Caller:
    path: str
    line: int
    kind: str
    enclosing: Definition | None
    in_changed_file: bool
    is_test: bool
    sites: int = 1
    # imports  : this file imports the changed file (the strongest sign that the use is the changed symbol)
    # package  : Java, same package (no import needed)
    # shadowed : this file defines its own symbol with the same name and does not import the changed file,
    #            so the use most likely refers to that other symbol
    # elsewhere: this file imports (or, in Java, shares a package with) a DIFFERENT file that defines a symbol of this
    #            name, and does not import the changed file
    # unknown  : no evidence either way
    link: str = "unknown"

    @property
    def unrelated(self) -> bool:
        return self.link in {"shadowed", "elsewhere"}


@dataclass
class SymbolImpact:
    symbol: ChangedSymbol
    callers: list[Caller] = field(default_factory=list)
    total_callers: int = 0
    unmodified_callers: int = 0
    likely_unrelated: int = 0
    test_files: list[str] = field(default_factory=list)
    other_definitions: int = 0


@dataclass
class ImpactReport:
    by_file: dict[str, str] = field(default_factory=dict)
    summary: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Hit:
    path: str
    line: int
    kind: str
    enclosing: Definition | None


@dataclass
class _FileFacts:
    """What the scan learned about one file that mentions a searched name."""

    language: str
    imports: list[ImportRef]
    defined: set[str]  # searched names this file itself defines


def _safe_read(root: Path, relative: str) -> bytes | None:
    try:
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            return None
        return path.read_bytes()
    except OSError:
        return None


def _search_names(definition: Definition, language: str) -> tuple[str, ...]:
    constructor = definition.kind == "method" and definition.container and (
        definition.name in {"__init__", "constructor"} or (language == "java" and definition.name == definition.container)
    )
    return (definition.container,) if constructor else (definition.name,)


def _skippable(definition: Definition) -> bool:
    name = definition.name
    if definition.kind == "method":
        if name in {"__init__", "constructor"}:
            return False
        if name.startswith("__") and name.endswith("__"):
            return True
        if name.lower() in {item.lower() for item in _GENERIC_METHODS}:
            return True
    return len(name) < 3 and definition.kind != "method"


def _file_symbols(root: Path, file: ChangedFile) -> tuple[list[ChangedSymbol], set[str], set[str], list[str]]:
    """Return (changed symbols, removed names, added names, notes) for one file."""
    language = language_for(file.filename)
    patch = file.patch or ""
    removed_lines = [line[1:] for line in patch.splitlines() if line.startswith("-") and not line.startswith("---")]
    added_lines = [line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")]
    if language is None:
        return [], set(), set(), []
    added, gaps = changed_new_lines(patch)
    touched = added | {n for gap in gaps for n in gap}
    source = _safe_read(root, file.filename)
    definitions: list[Definition] = []
    if source is not None:
        tree = parse(source, language)
        if tree is not None:
            definitions = extract_definitions(source, language, tree)
    removed_names = _defined_names(removed_lines, language)
    symbols: list[ChangedSymbol] = []
    for definition in definitions:
        if _skippable(definition):
            continue
        if file.status == "added":
            continue  # nothing existed before this change, so nothing can depend on the old behaviour
        if definition.name not in removed_names and all(n in added for n in range(definition.start_line, definition.end_line + 1)):
            continue  # a brand-new symbol: every line is new and no old definition of it was removed
        header = any(definition.start_line <= n <= definition.signature_end_line for n in added) or any(
            definition.start_line <= a and b <= definition.signature_end_line for a, b in gaps
        )
        if definition.kind in {"class", "interface", "enum"}:
            if not header:
                continue  # only the declaration of a type matters here; its members are separate definitions
        elif not any(definition.start_line <= n <= definition.end_line for n in touched):
            continue
        symbols.append(
            ChangedSymbol(
                name=definition.name,
                kind=definition.kind,
                path=file.filename,
                start_line=definition.start_line,
                end_line=definition.end_line,
                change="signature" if header else "body",
                search_names=_search_names(definition, language),
                container=definition.container,
            )
        )
    present = {definition.name for definition in definitions}
    removed = {name for name in _defined_names(removed_lines, language) if name not in present}
    return symbols, removed, _defined_names(added_lines, language), []


def _iter_source_files(root: Path, limit: int) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if len(files) >= limit:
            break
        if path.suffix.lower() in SOURCE_SUFFIXES and path.is_file():
            files.append(path)
    return sorted(files)


def _word_pattern(names: set[str]) -> re.Pattern[bytes]:
    """Whole-word matcher over raw bytes. ``render`` must not select a file that only has ``render_to_string``.

    Identifiers never contain ASCII non-word bytes other than ``$``, so the look-arounds cannot hide a real use.
    """
    alternation = b"|".join(re.escape(name.encode("utf-8")) for name in sorted(names, key=len, reverse=True))
    return re.compile(rb"(?<![\w$])(?:" + alternation + rb")(?![\w$])")


def _scan(
    root: Path, names: set[str], *, max_scan: int, max_parse: int, deadline: float
) -> tuple[dict[str, list[_Hit]], dict[str, list[tuple[str, Definition]]], dict[str, _FileFacts], bool, int]:
    needles = [name.encode("utf-8") for name in names]
    word = _word_pattern(names)
    hits: dict[str, list[_Hit]] = defaultdict(list)
    definitions_found: dict[str, list[tuple[str, Definition]]] = defaultdict(list)
    facts: dict[str, _FileFacts] = {}
    parsed = 0
    incomplete = False
    for path in _iter_source_files(root, max_scan):
        if time.monotonic() > deadline or parsed >= max_parse:
            incomplete = True
            break
        try:
            data = path.read_bytes()
        except OSError:
            continue
        # Two stages: a plain substring test is nearly free; the whole-word regex only runs on the survivors.
        if not any(needle in data for needle in needles):
            continue
        present = {match.decode("utf-8", "replace") for match in word.findall(data)}
        if not present:
            continue
        language = language_for(path.name)
        file = ParsedFile.open(data, language) if language else None
        if file is None:
            continue
        parsed += 1
        relative = path.relative_to(root).as_posix()
        defined: set[str] = set()
        for definition in file.definitions(present):  # a name-filtered query: cheap, no full extraction
            definitions_found[definition.name].append((relative, definition))
            defined.add(definition.name)
        found = file.references(present, with_enclosing=True)
        for reference in found:
            hits[reference.name].append(_Hit(relative, reference.line, reference.kind, reference.enclosing))
        if found:
            facts[relative] = _FileFacts(language, file.imports(), defined)
    return hits, definitions_found, facts, incomplete, parsed


def _link(symbol: ChangedSymbol, path: str, facts: _FileFacts | None, other_definers: set[str]) -> str:
    """How strongly does this file's use of the name point at the changed symbol? (see ``Caller.link``)"""
    if facts is None or path == symbol.path:
        return "unknown"
    if imports_link(path, facts.imports, symbol.path, facts.language):
        return "imports"
    java = family(facts.language) == "java"
    if java and same_java_package(path, symbol.path):
        return "package"
    if facts.defined & set(symbol.search_names):
        return "shadowed"
    for other in other_definers:
        if imports_link(path, facts.imports, other, facts.language) or (java and same_java_package(path, other)):
            return "elsewhere"
    return "unknown"


_LINK_ORDER = {"imports": 0, "package": 0, "unknown": 1, "shadowed": 2, "elsewhere": 2}


def _callers(
    symbol: ChangedSymbol,
    hits: dict[str, list[_Hit]],
    changed_paths: set[str],
    facts: dict[str, _FileFacts],
    definers: dict[str, list[tuple[str, Definition]]],
) -> list[Caller]:
    other_definers = {
        path for name in symbol.search_names for path, _ in definers.get(name, []) if path != symbol.path
    }
    groups: dict[tuple[str, int], Caller] = {}
    for name in symbol.search_names:
        for hit in hits.get(name, []):
            if hit.path == symbol.path and symbol.start_line <= hit.line <= symbol.end_line:
                continue  # the symbol's own body (recursion) is already in the diff
            key = (hit.path, hit.enclosing.start_line if hit.enclosing else hit.line)
            existing = groups.get(key)
            if existing is None:
                groups[key] = Caller(
                    hit.path, hit.line, hit.kind, hit.enclosing, hit.path in changed_paths, is_test_path(hit.path),
                    link=_link(symbol, hit.path, facts.get(hit.path), other_definers),
                )
            else:
                existing.sites += 1
                if _KIND_WEIGHT.get(hit.kind, 0) > _KIND_WEIGHT.get(existing.kind, 0):
                    existing.kind, existing.line = hit.kind, hit.line
    callers = list(groups.values())
    with_usage = {c.path for c in callers if c.kind != "import"}
    return [c for c in callers if not (c.kind == "import" and c.path in with_usage)]  # an import adds nothing next to a real use


def _rank(symbol: ChangedSymbol, caller: Caller) -> tuple[Any, ...]:
    same_dir = Path(caller.path).parent == Path(symbol.path).parent
    return (_LINK_ORDER.get(caller.link, 1), caller.in_changed_file, -_KIND_WEIGHT.get(caller.kind, 0), not same_dir, caller.path, caller.line)


def _snippet(root: Path, cache: dict[str, list[str]], caller: Caller) -> str:
    if caller.path not in cache:
        data = _safe_read(root, caller.path)
        cache[caller.path] = data.decode("utf-8", "replace").splitlines() if data is not None else []
    lines = cache[caller.path]
    if not lines:
        return ""
    low_limit = caller.enclosing.start_line if caller.enclosing else caller.line
    high_limit = caller.enclosing.end_line if caller.enclosing else caller.line
    first = max(low_limit, caller.line - 5)
    last = min(high_limit, caller.line + 5, len(lines))
    rows: list[str] = []
    if first > low_limit and low_limit <= len(lines):
        rows.append(f"{low_limit:>5} | {lines[low_limit - 1][:160]}")
        if first > low_limit + 1:
            rows.append("      | ...")
    for number in range(first, last + 1):
        marker = ">" if number == caller.line else " "
        rows.append(f"{number:>5}{marker}| {lines[number - 1][:160]}")
    text, _ = redact("\n".join(rows))
    return text


def _render_symbol(root: Path, cache: dict[str, list[str]], index: int, impact: SymbolImpact, max_callers: int) -> str:
    symbol = impact.symbol
    label = {
        "signature": "SIGNATURE/HEADER CHANGED",
        "body": "body changed",
        "removed": "REMOVED OR RENAMED in this file (no definition left here and none added elsewhere in this change)",
    }[symbol.change]
    where = f"{symbol.path}:{symbol.start_line}-{symbol.end_line}" if symbol.start_line else symbol.path
    names = "" if symbol.search_names == (symbol.name,) else f" (referenced as {', '.join(symbol.search_names)})"
    out = [f"[{index}] {symbol.name} ({symbol.kind}) at {where}{names}: {label}"]
    if impact.other_definitions:
        out.append(
            f"    note: {impact.other_definitions} other definition(s) with the same name exist in the repository. "
            "Each caller below is tagged: 'imports the changed file' is strong evidence; 'defines its own symbol with the "
            "same name' most likely calls something else."
        )
    shown = [c for c in impact.callers if not c.unrelated][:max_callers]  # unrelated ones are counted, never shown
    unrelated = f"; {impact.likely_unrelated} more call a different same-named symbol (not shown)" if impact.likely_unrelated else ""
    out.append(
        f"    callers/users found: {impact.total_callers} (showing {len(shown)}); "
        f"{impact.unmodified_callers} in files NOT modified by this change{unrelated}"
    )
    for caller in shown:
        where_called = f"in {caller.enclosing.name}()" if caller.enclosing else "at module level"
        state = "file also modified in this change" if caller.in_changed_file else "file NOT modified in this change"
        sites = f", {caller.sites} sites" if caller.sites > 1 else ""
        tag = _LINK_TEXT.get(caller.link, "")
        out.append(f"    - {caller.path}:{caller.line} {where_called} [{caller.kind}{sites}; {state}{tag}]")
        snippet = _snippet(root, cache, caller)
        if snippet:
            out.extend("        " + row for row in snippet.splitlines())
    out.append("    tests referencing it: " + (", ".join(impact.test_files) if impact.test_files else "none found"))
    return "\n".join(out)


_LINK_TEXT = {
    "imports": "; imports the changed file",
    "package": "; same package as the changed file",
    "shadowed": "; defines its own symbol with the same name and does not import the changed file: likely unrelated",
    "elsewhere": "; imports a DIFFERENT file that defines a symbol with this name: likely unrelated",
}


def _summary_line(impact: SymbolImpact) -> str | None:
    symbol = impact.symbol
    if impact.unmodified_callers == 0:
        return None  # every user is in a file this change also touches: the reviewer sees them in the diff
    if symbol.change == "removed":
        return f"`{symbol.name}` 在 {symbol.path} 中被移除或改名，仍有 {impact.total_callers} 处引用（{impact.unmodified_callers} 处所在文件未随本次改动更新）"
    if symbol.change == "signature":
        return (
            f"`{symbol.name}`（{symbol.path}）的签名/声明有变更，涉及 {impact.total_callers} 处调用方，"
            f"其中 {impact.unmodified_callers} 处所在文件未随本次改动更新"
        )
    return None


def analyze_impact(root: Path, files: list[ChangedFile], changed_paths: set[str], settings: Settings) -> ImpactReport:
    """Build per-file evidence text about the callers of the symbols touched by ``files``."""
    deadline = time.monotonic() + settings.impact_time_budget_seconds
    per_file: dict[str, list[ChangedSymbol]] = {}
    removed_candidates: dict[str, set[str]] = {}
    added_names: set[str] = set()
    for file in files:
        if is_sensitive_path(file.filename) or not file.patch:
            continue
        symbols, removed, added, _ = _file_symbols(root, file)
        per_file[file.filename] = symbols
        removed_candidates[file.filename] = removed
        added_names |= added
    for filename, removed in removed_candidates.items():
        for name in sorted(removed - added_names)[:_MAX_REMOVED_PER_FILE]:
            if len(name) >= 3:
                per_file[filename].append(
                    ChangedSymbol(name=name, kind="function", path=filename, start_line=0, end_line=0, change="removed", search_names=(name,))
                )
    order = {"signature": 0, "removed": 1, "body": 2}
    names: set[str] = set()
    for filename, symbols in per_file.items():
        symbols.sort(key=lambda s: (order[s.change], s.start_line))
        del symbols[settings.impact_max_symbols_per_file :]
        for symbol in symbols:
            names.update(symbol.search_names)
    names = set(sorted(names)[:_MAX_NAMES])
    report = ImpactReport(stats={"symbols": sum(len(s) for s in per_file.values()), "names": len(names)})
    if not names:
        return report
    hits, definitions_found, facts, incomplete, parsed = _scan(
        root, names, max_scan=settings.impact_max_scan_files, max_parse=settings.impact_max_parse_files, deadline=deadline
    )
    report.stats.update({"files_parsed": parsed, "incomplete": incomplete})
    cache: dict[str, list[str]] = {}
    for filename, symbols in per_file.items():
        sections: list[str] = []
        budget = settings.impact_evidence_chars
        for symbol in symbols:
            callers = _callers(symbol, hits, changed_paths, facts, definitions_found)
            non_tests = sorted((c for c in callers if not c.is_test), key=lambda c: _rank(symbol, c))
            test_files = sorted({c.path for c in callers if c.is_test})[:3]
            others = sum(
                1
                for name in symbol.search_names
                for path, definition in definitions_found.get(name, [])
                if not (path == symbol.path and definition.start_line == symbol.start_line)
            )
            related = [c for c in non_tests if not c.unrelated]
            impact = SymbolImpact(
                symbol=symbol,
                callers=non_tests,
                total_callers=len(related),
                unmodified_callers=sum(1 for c in related if not c.in_changed_file),
                likely_unrelated=len(non_tests) - len(related),
                test_files=test_files,
                other_definitions=others,
            )
            if not related:
                continue  # no caller points at this symbol (all are unrelated or absent): nothing worth showing a model
            if symbol.change == "body" and not impact.unmodified_callers:
                continue  # a body-only edit with no outside users is already fully visible in the diff
            max_callers = settings.impact_max_callers if symbol.change != "body" else 2
            text = _render_symbol(root, cache, len(sections) + 1, impact, max_callers)
            if sections and len(text) > budget:
                break
            budget -= len(text)
            sections.append(text)
            line = _summary_line(impact)
            if line:
                report.summary.append(line)
        if sections:
            note = "\n(note: the repository search stopped early because of time/size limits; callers may be missing)" if incomplete else ""
            report.by_file[filename] = "\n\n".join(sections) + note
    report.summary = report.summary[:8]
    return report
