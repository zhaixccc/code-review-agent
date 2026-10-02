"""Syntax-tree code navigation (tree-sitter): definitions, name-based references and imports.

This is the deterministic half of impact analysis: it finds *where* a changed symbol is defined and *who* mentions it.
Matching is by name (like Aider's repo map or tree-sitter "tags"), not by resolved types, so callers can include
unrelated symbols that share the name. Callers must treat results as evidence to inspect, not as proof.

Source text is only parsed, never executed. A missing grammar package or a parse failure degrades to "no results".

Structure (why it is fast): the Python-level API of tree-sitter creates one object per node it touches, so walking a
whole tree costs more than parsing it. Instead, native *queries* find the few candidate nodes (definition nodes, or
identifiers whose text is one of the wanted names), and only those are decoded. A :class:`ParsedFile` parses once and
shares the line table between definitions, references and imports.
"""

from __future__ import annotations

import bisect
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_LANGUAGE_BY_SUFFIX = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "tsx",
    ".java": "java",
}
SOURCE_SUFFIXES = frozenset(_LANGUAGE_BY_SUFFIX)
LANGUAGES = ("python", "javascript", "typescript", "tsx", "java")

_CONTAINER_KINDS = {"class", "interface", "enum"}
_NAME_RE = re.compile(r"^[^\W\d][\w$]*$|^\$[\w$]*$")  # identifiers only (JS allows a leading "$"); nothing else reaches a query


def language_for(path: str) -> str | None:
    return _LANGUAGE_BY_SUFFIX.get(PurePosixPath(path.replace("\\", "/")).suffix.lower())


def family(language: str) -> str:
    return "js" if language in {"javascript", "typescript", "tsx"} else language


@lru_cache(maxsize=None)
def _language(name: str) -> Any:
    """Load a grammar lazily; None when the package is not installed."""
    try:
        from tree_sitter import Language

        if name == "python":
            import tree_sitter_python as module

            return Language(module.language())
        if name == "javascript":
            import tree_sitter_javascript as module

            return Language(module.language())
        if name in {"typescript", "tsx"}:
            import tree_sitter_typescript as module

            return Language(module.language_tsx() if name == "tsx" else module.language_typescript())
        if name == "java":
            import tree_sitter_java as module

            return Language(module.language())
    except Exception as error:  # missing optional package or ABI mismatch
        logger.warning("tree-sitter grammar %s unavailable (%s).", name, type(error).__name__)
    return None


def available_languages() -> set[str]:
    return {name for name in LANGUAGES if _language(name) is not None}


def parse(source: bytes, language: str) -> Any:
    """Parse source bytes. Returns a tree, or None if the grammar is missing or parsing fails."""
    grammar = _language(language)
    if grammar is None:
        return None
    try:
        from tree_sitter import Parser

        # A Parser is not thread-safe; measured: building one per call costs nothing next to parsing.
        return Parser(grammar).parse(source)
    except Exception as error:
        logger.warning("tree-sitter parse failed (%s).", type(error).__name__)
        return None


@dataclass(frozen=True)
class Definition:
    name: str
    kind: str  # function | method | class | interface | type | enum
    start_line: int  # 1-based, inclusive
    end_line: int
    signature_end_line: int  # last line of the header (everything before the body)
    container: str = ""  # enclosing class/interface name


@dataclass(frozen=True)
class Reference:
    name: str
    line: int
    kind: str  # call | import | type | attr | ref
    enclosing: Definition | None = field(default=None, compare=False)  # innermost definition containing the use


@dataclass(frozen=True)
class ImportRef:
    """One import / re-export / require, reduced to what is needed to tell whether a file depends on a module."""

    spec: str  # python: dotted module ("" for "from . import x"); js/ts: module specifier; java: qualified name
    names: tuple[str, ...] = ()  # python: imported names ("*" for a wildcard)
    level: int = 0  # python: number of leading dots of a relative import
    wildcard: bool = False  # java: "import a.b.*"
    static: bool = False  # java: "import static a.b.C.m"
    line: int = 0


def _text(node: Any) -> str:
    return node.text.decode("utf-8", "replace") if node is not None and node.text is not None else ""


class _Lines:
    """Byte offset -> 1-based line number.

    Node.start_point / end_point are deliberately never used: with tree-sitter 0.26 on Windows, reading them on real
    files (for example this project's own graph.py) corrupted the interpreter and ended the process with an access
    violation. Byte offsets are plain integers and have no such problem.
    """

    def __init__(self, source: bytes) -> None:
        self._starts = [0] + [match.end() for match in re.finditer(b"\n", source)]

    def start(self, node: Any) -> int:
        return bisect.bisect_right(self._starts, node.start_byte)

    def end(self, node: Any) -> int:
        return bisect.bisect_right(self._starts, max(node.start_byte, node.end_byte - 1))


# --------------------------------------------------------------------------- query construction ---
# Node types that can define a symbol, per language family. A value is an extra constraint inside the pattern.
_DEF_PATTERNS: dict[str, tuple[tuple[str, str], ...]] = {
    "python": (("function_definition", ""), ("class_definition", "")),
    "java": tuple(
        (kind, "")
        for kind in (
            "method_declaration", "constructor_declaration", "class_declaration", "interface_declaration",
            "enum_declaration", "record_declaration", "annotation_type_declaration",
        )
    ),
    "js": (
        ("function_declaration", ""), ("generator_function_declaration", ""), ("method_definition", ""),
        ("class_declaration", ""), ("abstract_class_declaration", ""), ("interface_declaration", ""),
        ("type_alias_declaration", ""), ("enum_declaration", ""),
        # `const f = () => ...` and class fields holding functions are definitions too, other declarators are not
        ("variable_declarator", "value: [(arrow_function) (function_expression)]"),
        ("public_field_definition", "value: [(arrow_function) (function_expression)]"),
    ),
}
_CONTAINER_TYPES = {
    "python": {"class_definition"},
    "java": {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration", "annotation_type_declaration"},
    "js": {"class_declaration", "abstract_class_declaration", "interface_declaration", "enum_declaration"},
}
_IDENTIFIER_TYPES = {
    "python": ("identifier",),
    "java": ("identifier", "type_identifier"),
    "js": ("identifier", "property_identifier", "type_identifier", "shorthand_property_identifier"),
}
_IMPORT_PATTERNS = {
    "python": "[(import_statement) (import_from_statement)] @imp",
    "java": "(import_declaration) @imp",
    "js": (
        "[(import_statement) (export_statement source: (_))] @imp\n"
        '(call_expression function: (identifier) @fn arguments: (arguments . (string) @arg) (#eq? @fn "require"))'
    ),
}
# Fields that name the symbol for node types where the name is not an arbitrary node
_NAME_NODE = {"variable_declarator": "(identifier)", "public_field_definition": "(property_identifier)"}


def _has_kind(grammar: Any, kind: str) -> bool:
    return grammar.id_for_node_kind(kind, True) is not None


def _quote(names: Iterable[str]) -> str:
    return " ".join(f'"{name}"' for name in sorted(names))


def clean_names(names: Iterable[str]) -> frozenset[str]:
    """Only plain identifiers may be embedded in a query."""
    return frozenset(name for name in names if isinstance(name, str) and _NAME_RE.match(name))


@lru_cache(maxsize=256)
def _definition_query(language: str, names: frozenset[str] | None) -> Any:
    grammar = _language(language)
    if grammar is None or (names is not None and not names):
        return None
    from tree_sitter import Query

    predicate = "" if names is None else f"(#any-of? @name {_quote(names)})"
    patterns = []
    for kind, constraint in _DEF_PATTERNS[family(language)]:
        if _has_kind(grammar, kind):
            name_node = _NAME_NODE.get(kind, "(_)")
            patterns.append(f"({kind} name: {name_node} @name {constraint} {predicate}) @def")
    return Query(grammar, "\n".join(patterns)) if patterns else None


@lru_cache(maxsize=256)
def _reference_query(language: str, names: frozenset[str]) -> Any:
    grammar = _language(language)
    if grammar is None or not names:
        return None
    from tree_sitter import Query

    kinds = " ".join(f"({kind})" for kind in _IDENTIFIER_TYPES[family(language)] if _has_kind(grammar, kind))
    return Query(grammar, f"([{kinds}] @id (#any-of? @id {_quote(names)}))")


@lru_cache(maxsize=16)
def _import_query(language: str) -> Any:
    grammar = _language(language)
    if grammar is None:
        return None
    from tree_sitter import Query

    return Query(grammar, _IMPORT_PATTERNS[family(language)])


def _captures(query: Any, node: Any) -> dict[str, list[Any]]:
    from tree_sitter import QueryCursor

    return QueryCursor(query).captures(node)


# --------------------------------------------------------------------------- decoding ---
def _definition_parts(node: Any, language: str, container: str) -> tuple[str, str, Any] | None:
    """Return (name, kind, body_node) when this node defines a named symbol."""
    kind_by_type: dict[str, str]
    if language == "python":
        if node.type == "function_definition":
            name = _text(node.child_by_field_name("name"))
            return (name, "method" if container else "function", node.child_by_field_name("body")) if name else None
        if node.type == "class_definition":
            name = _text(node.child_by_field_name("name"))
            return (name, "class", node.child_by_field_name("body")) if name else None
        return None
    if language == "java":
        kind_by_type = {
            "method_declaration": "method", "constructor_declaration": "method", "class_declaration": "class",
            "interface_declaration": "interface", "enum_declaration": "enum", "record_declaration": "class",
            "annotation_type_declaration": "interface",
        }
        kind = kind_by_type.get(node.type)
        if kind is None:
            return None
        name = _text(node.child_by_field_name("name"))
        return (name, kind, node.child_by_field_name("body")) if name else None
    # javascript / typescript / tsx
    kind_by_type = {
        "function_declaration": "function", "generator_function_declaration": "function", "method_definition": "method",
        "class_declaration": "class", "abstract_class_declaration": "class", "interface_declaration": "interface",
        "type_alias_declaration": "type", "enum_declaration": "enum",
    }
    kind = kind_by_type.get(node.type)
    if kind is not None:
        name = _text(node.child_by_field_name("name"))
        return (name, kind, node.child_by_field_name("body")) if name else None
    if node.type in {"variable_declarator", "public_field_definition"}:
        value = node.child_by_field_name("value")
        if value is not None and value.type in {"arrow_function", "function_expression", "function"}:
            name_node = node.child_by_field_name("name")
            name = _text(name_node) if name_node is not None and name_node.type in {"identifier", "property_identifier"} else ""
            if name:
                return name, "method" if node.type == "public_field_definition" else "function", value.child_by_field_name("body")
    return None


def _signature_end(node: Any, body: Any, language: str, lines: _Lines) -> int:
    """Last 1-based line of the declaration header (everything before the body)."""
    start = lines.start(node)
    if body is None:
        return lines.end(node)
    body_line = lines.start(body)
    return max(start, body_line - 1) if language == "python" else max(start, body_line)


_JS_DECLARATIONS = {
    "function_declaration", "generator_function_declaration", "class_declaration", "abstract_class_declaration",
    "method_definition", "variable_declarator", "public_field_definition", "property_signature", "method_signature",
    "interface_declaration", "type_alias_declaration", "enum_declaration", "function_signature", "abstract_method_signature",
}
_JAVA_DECLARATIONS = {
    "method_declaration", "constructor_declaration", "class_declaration", "interface_declaration", "enum_declaration",
    "record_declaration", "formal_parameter", "variable_declarator", "annotation_type_declaration", "spread_parameter",
}


def _same(a: Any, b: Any) -> bool:
    return a is not None and b is not None and a == b


def _ancestor_type(node: Any, types: set[str], levels: int = 4) -> bool:
    current = node.parent
    for _ in range(levels):
        if current is None:
            return False
        if current.type in types:
            return True
        current = current.parent
    return False


def _classify(node: Any, language: str) -> str | None:
    """Return the kind of reference this identifier is, or None when it is a definition/parameter name (not a use)."""
    parent = node.parent
    if parent is None:
        return "ref"
    kind = parent.type
    if language == "python":
        if kind in {"function_definition", "class_definition"} and _same(parent.child_by_field_name("name"), node):
            return None
        if _ancestor_type(node, {"import_from_statement", "import_statement"}):
            return "import"
        if kind == "keyword_argument" and _same(parent.child_by_field_name("name"), node):
            return None
        if kind in {"parameters", "lambda_parameters"}:
            return None
        if kind in {"default_parameter", "typed_default_parameter"} and _same(parent.child_by_field_name("name"), node):
            return None
        if kind == "typed_parameter" and not _same(parent.child_by_field_name("type"), node):
            return None
        if kind == "call" and _same(parent.child_by_field_name("function"), node):
            return "call"
        if kind == "attribute":
            if _same(parent.child_by_field_name("attribute"), node):
                grandparent = parent.parent
                if grandparent is not None and grandparent.type == "call" and _same(grandparent.child_by_field_name("function"), parent):
                    return "call"
                return "attr"
            return "ref"
        return "ref"
    if language == "java":
        if kind in _JAVA_DECLARATIONS and _same(parent.child_by_field_name("name"), node):
            return None
        if _ancestor_type(node, {"import_declaration"}):
            return "import"
        if kind == "method_invocation" and _same(parent.child_by_field_name("name"), node):
            return "call"
        if kind == "object_creation_expression" and _same(parent.child_by_field_name("type"), node):
            return "call"
        return "type" if node.type == "type_identifier" else "ref"
    # javascript / typescript / tsx
    if kind in _JS_DECLARATIONS and _same(parent.child_by_field_name("name"), node):
        return None
    if kind in {"required_parameter", "optional_parameter"} and _same(parent.child_by_field_name("pattern"), node):
        return None
    if _ancestor_type(node, {"import_specifier", "import_clause", "namespace_import", "named_imports", "import_statement"}):
        return "import"
    if kind == "call_expression" and _same(parent.child_by_field_name("function"), node):
        return "call"
    if kind == "new_expression" and _same(parent.child_by_field_name("constructor"), node):
        return "call"
    if kind in {"jsx_opening_element", "jsx_self_closing_element"} and _same(parent.child_by_field_name("name"), node):
        return "call"
    if kind == "member_expression":
        if _same(parent.child_by_field_name("property"), node):
            grandparent = parent.parent
            if grandparent is not None and grandparent.type == "call_expression" and _same(grandparent.child_by_field_name("function"), parent):
                return "call"
            return "attr"
        return "ref"
    return "type" if node.type == "type_identifier" else "ref"


def _unquote(text: str) -> str:
    text = text.strip()
    return text[1:-1] if len(text) >= 2 and text[0] in "'\"`" and text[-1] == text[0] else text


_JAVA_IMPORT = re.compile(r"^\s*import\s+(static\s+)?([\w$.]+?)(\.\*)?\s*;", re.DOTALL)


# --------------------------------------------------------------------------- one parsed file ---
class ParsedFile:
    """A source file parsed once. Definitions, references and imports are extracted on demand from the same tree."""

    __slots__ = ("source", "language", "tree", "_lines")

    def __init__(self, source: bytes, language: str, tree: Any) -> None:
        self.source, self.language, self.tree = source, language, tree
        self._lines: _Lines | None = None

    @classmethod
    def open(cls, source: bytes, language: str, tree: Any = None) -> "ParsedFile | None":
        tree = tree or parse(source, language)
        return None if tree is None else cls(source, language, tree)

    @property
    def lines(self) -> _Lines:
        if self._lines is None:
            self._lines = _Lines(self.source)
        return self._lines

    # ---- definitions ----
    def _container_name(self, node: Any) -> str:
        types = _CONTAINER_TYPES[family(self.language)]
        parent = node.parent
        while parent is not None:
            if parent.type in types:
                name = _text(parent.child_by_field_name("name"))
                if name:
                    return name
            parent = parent.parent
        return ""

    def _make_definition(self, node: Any, container: str) -> Definition | None:
        parts = _definition_parts(node, self.language, container)
        if parts is None:
            return None
        name, kind, body = parts
        lines = self.lines
        return Definition(
            name=name,
            kind=kind,
            start_line=lines.start(node),
            end_line=lines.end(node),
            signature_end_line=_signature_end(node, body, self.language, lines),
            container=container,
        )

    def definitions(self, names: Iterable[str] | None = None) -> list[Definition]:
        """Every definition in the file, or only those whose name is in ``names`` (a much cheaper query)."""
        wanted = None if names is None else clean_names(names)
        query = _definition_query(self.language, wanted)
        if query is None:
            return []
        seen: dict[tuple[int, int], Any] = {}
        for node in _captures(query, self.tree.root_node).get("def", []):
            seen.setdefault((node.start_byte, node.end_byte), node)
        ordered = sorted(seen.values(), key=lambda node: (node.start_byte, -node.end_byte))
        found: list[Definition] = []
        if wanted is not None:
            # Only a few nodes were selected, so the enclosing container is looked up per node.
            for node in ordered:
                definition = self._make_definition(node, self._container_name(node))
                if definition is not None:
                    found.append(definition)
            return sorted(found, key=lambda item: (item.start_line, item.end_line))
        # All definitions are present: containers are tracked with a stack over the containment order.
        stack: list[tuple[int, str]] = []  # (end_byte, container name)
        for node in ordered:
            while stack and stack[-1][0] <= node.start_byte:
                stack.pop()
            definition = self._make_definition(node, stack[-1][1] if stack else "")
            if definition is None:
                continue
            found.append(definition)
            if definition.kind in _CONTAINER_KINDS:
                stack.append((node.end_byte, definition.name))
        return sorted(found, key=lambda item: (item.start_line, item.end_line))

    def enclosing(self, node: Any) -> Definition | None:
        """The innermost definition that contains ``node`` (found by climbing the tree, no full extraction)."""
        types = {kind for kind, _ in _DEF_PATTERNS[family(self.language)]}
        parent = node.parent
        while parent is not None:
            if parent.type in types:
                definition = self._make_definition(parent, self._container_name(parent))
                if definition is not None:
                    return definition
            parent = parent.parent
        return None

    # ---- references ----
    def references(self, names: Iterable[str], with_enclosing: bool = False) -> list[Reference]:
        """Uses of the given names (definitions, parameters and keyword names are not uses)."""
        query = _reference_query(self.language, clean_names(names))
        if query is None:
            return []
        lines = self.lines
        found: list[Reference] = []
        for node in _captures(query, self.tree.root_node).get("id", []):
            kind = _classify(node, self.language)
            if kind is not None:
                found.append(
                    Reference(name=_text(node), line=lines.start(node), kind=kind, enclosing=self.enclosing(node) if with_enclosing else None)
                )
        found.sort(key=lambda ref: (ref.line, ref.name, ref.kind))  # a total order: results must be reproducible
        return found

    # ---- imports ----
    def imports(self) -> list[ImportRef]:
        query = _import_query(self.language)
        if query is None:
            return []
        captures = _captures(query, self.tree.root_node)
        fam = family(self.language)
        lines = self.lines
        found: list[ImportRef] = []
        if fam == "python":
            for node in captures.get("imp", []):
                found.extend(self._python_import(node, lines.start(node)))
        elif fam == "java":
            for node in captures.get("imp", []):
                match = _JAVA_IMPORT.match(_text(node))
                if match:
                    found.append(ImportRef(spec=match.group(2), wildcard=bool(match.group(3)), static=bool(match.group(1)), line=lines.start(node)))
        else:
            for node in captures.get("imp", []):
                source = node.child_by_field_name("source")
                if source is not None:
                    found.append(ImportRef(spec=_unquote(_text(source)), line=lines.start(node)))
            for node in captures.get("arg", []):
                found.append(ImportRef(spec=_unquote(_text(node)), line=lines.start(node)))
        return found

    @staticmethod
    def _python_import(node: Any, line: int) -> list[ImportRef]:
        def module_of(child: Any) -> str:
            target = child.child_by_field_name("name") if child.type == "aliased_import" else child
            return _text(target)

        if node.type == "import_statement":
            return [ImportRef(spec=module_of(child), line=line) for child in node.children_by_field_name("name")]
        module = node.child_by_field_name("module_name")
        text = _text(module)
        level = len(text) - len(text.lstrip("."))
        names = tuple(module_of(child) for child in node.children_by_field_name("name"))
        if any(child.type == "wildcard_import" for child in node.children):
            names += ("*",)
        return [ImportRef(spec=text.lstrip("."), names=names, level=level, line=line)]


# --------------------------------------------------------------------------- convenience wrappers ---
def extract_definitions(source: bytes, language: str, tree: Any = None) -> list[Definition]:
    parsed = ParsedFile.open(source, language, tree)
    return parsed.definitions() if parsed else []


def find_references(source: bytes, language: str, names: set[str], tree: Any = None) -> list[Reference]:
    """All identifiers in the file whose text is one of ``names`` and that are uses (not definitions/parameters)."""
    parsed = ParsedFile.open(source, language, tree)
    return parsed.references(names) if parsed else []


def enclosing_definition(definitions: list[Definition], line: int) -> Definition | None:
    """Smallest definition whose line range contains the line."""
    best: Definition | None = None
    for definition in definitions:
        if definition.start_line <= line <= definition.end_line:
            if best is None or (definition.end_line - definition.start_line) <= (best.end_line - best.start_line):
                best = definition
    return best
