"""Syntax-tree code navigation (tree-sitter): definitions and name-based references.

This is the deterministic half of impact analysis: it finds *where* a changed symbol is defined and *who* mentions it.
Matching is by name (like Aider's repo map or tree-sitter "tags"), not by resolved types, so callers can include
unrelated symbols that share the name. Callers must treat results as evidence to inspect, not as proof.

Source text is only parsed, never executed. A missing grammar package or a parse failure degrades to "no results".
"""

from __future__ import annotations

import bisect
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_LANGUAGE_BY_SUFFIX = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "tsx",
    ".java": "java",
}
SOURCE_SUFFIXES = frozenset(_LANGUAGE_BY_SUFFIX)

_CONTAINER_KINDS = {"class", "interface", "enum"}


def language_for(path: str) -> str | None:
    return _LANGUAGE_BY_SUFFIX.get(PurePosixPath(path.replace("\\", "/")).suffix.lower())


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
    return {name for name in ("python", "javascript", "typescript", "tsx", "java") if _language(name) is not None}


def parse(source: bytes, language: str) -> Any:
    """Parse source bytes. Returns a tree, or None if the grammar is missing or parsing fails."""
    grammar = _language(language)
    if grammar is None:
        return None
    try:
        from tree_sitter import Parser

        return Parser(grammar).parse(source)  # a Parser is not thread-safe, so build one per call
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


def _walk(root: Any) -> Iterator[Any]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


# --------------------------------------------------------------------------- definitions ---
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


def extract_definitions(source: bytes, language: str, tree: Any = None) -> list[Definition]:
    tree = tree or parse(source, language)
    if tree is None:
        return []
    lines = _Lines(source)
    definitions: list[Definition] = []
    stack: list[tuple[Any, str]] = [(tree.root_node, "")]
    while stack:
        node, container = stack.pop()
        parts = _definition_parts(node, language, container)
        next_container = container
        if parts is not None:
            name, kind, body = parts
            definitions.append(
                Definition(
                    name=name,
                    kind=kind,
                    start_line=lines.start(node),
                    end_line=lines.end(node),
                    signature_end_line=_signature_end(node, body, language, lines),
                    container=container,
                )
            )
            if kind in _CONTAINER_KINDS:
                next_container = name
        for child in reversed(node.children):
            stack.append((child, next_container))
    return sorted(definitions, key=lambda item: (item.start_line, item.end_line))


def enclosing_definition(definitions: list[Definition], line: int) -> Definition | None:
    """Smallest definition whose line range contains the line."""
    best: Definition | None = None
    for definition in definitions:
        if definition.start_line <= line <= definition.end_line:
            if best is None or (definition.end_line - definition.start_line) <= (best.end_line - best.start_line):
                best = definition
    return best


# --------------------------------------------------------------------------- references ---
_IDENTIFIER_TYPES = {
    "python": {"identifier"},
    "java": {"identifier", "type_identifier"},
    "javascript": {"identifier", "property_identifier", "type_identifier", "shorthand_property_identifier"},
    "typescript": {"identifier", "property_identifier", "type_identifier", "shorthand_property_identifier"},
    "tsx": {"identifier", "property_identifier", "type_identifier", "shorthand_property_identifier"},
}
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


def find_references(source: bytes, language: str, names: set[str], tree: Any = None) -> list[Reference]:
    """All identifiers in the file whose text is one of ``names`` and that are uses (not definitions/parameters)."""
    if not names:
        return []
    tree = tree or parse(source, language)
    if tree is None:
        return []
    identifier_types = _IDENTIFIER_TYPES.get(language, set())
    lines = _Lines(source)
    references: list[Reference] = []
    for node in _walk(tree.root_node):
        if node.type not in identifier_types:
            continue
        name = _text(node)
        if name not in names:
            continue
        kind = _classify(node, language)
        if kind is not None:
            references.append(Reference(name=name, line=lines.start(node), kind=kind))
    return references
