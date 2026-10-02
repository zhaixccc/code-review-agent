"""tree-sitter navigation: definitions, header ranges and reference classification."""

from code_review_agent.code_index import (
    available_languages,
    enclosing_definition,
    extract_definitions,
    find_references,
    language_for,
)

PY = b'''import os
from billing import apply_discount


class Cart:
    def __init__(self, items):
        self.items = items

    def total(self, rate,
              currency):
        return apply_discount(sum(self.items), rate)


def helper(x, key=apply_discount):
    return Cart.total(x)
'''

TS = b'''import { formatPrice } from "./fmt";
export function formatPrice(amount: number, locale: string): string { return String(amount); }
export const render = (a: number) => { return formatPrice(a); };
class Foo { bar(x: number) { return formatPrice(x); } }
'''

JAVA = b'''import com.x.Util;
public class A {
    public A(int a) {}
    public int calc(int a,
                    int b) { return Util.help(a); }
    void run() { new A(1).calc(1, 2); }
}
'''


def test_languages_are_available_and_detected_by_suffix():
    assert {"python", "typescript", "tsx", "javascript", "java"} <= available_languages()
    assert language_for("src/app.py") == "python"
    assert language_for("a/B.TSX") == "tsx"
    assert language_for("README.md") is None


def test_python_definitions_include_header_range_and_container():
    defs = {(d.name, d.kind): d for d in extract_definitions(PY, "python")}
    cart = defs[("Cart", "class")]
    total = defs[("total", "method")]
    assert (cart.start_line, cart.end_line) == (5, 11)
    assert total.container == "Cart" and (total.start_line, total.signature_end_line, total.end_line) == (9, 10, 11)
    assert defs[("helper", "function")].container == ""


def test_python_references_are_classified():
    refs = find_references(PY, "python", {"apply_discount", "total", "Cart"})
    kinds = {(r.name, r.line): r.kind for r in refs}
    assert kinds[("apply_discount", 2)] == "import"
    assert kinds[("apply_discount", 11)] == "call"
    assert kinds[("apply_discount", 14)] == "ref"  # passed as a default value, not called
    assert kinds[("total", 15)] == "call"  # Cart.total(...)
    assert not any(r.line == 5 and r.name == "Cart" for r in refs)  # the class's own name is a definition, not a use


def test_parameter_and_keyword_names_are_not_references():
    source = b"def f(total):\n    return g(total=total)\n"
    assert [r.line for r in find_references(source, "python", {"total"})] == [2]  # only the value, not the parameter or keyword


def test_typescript_definitions_and_references():
    defs = {(d.name, d.kind) for d in extract_definitions(TS, "typescript")}
    assert {("formatPrice", "function"), ("render", "function"), ("Foo", "class"), ("bar", "method")} <= defs
    kinds = {(r.line, r.kind) for r in find_references(TS, "typescript", {"formatPrice"})}
    assert (1, "import") in kinds and (3, "call") in kinds and (4, "call") in kinds
    assert not any(line == 2 for line, _ in kinds)  # the declaration itself


def test_java_definitions_and_references():
    defs = extract_definitions(JAVA, "java")
    calc = next(d for d in defs if d.name == "calc")
    assert (calc.start_line, calc.signature_end_line, calc.container) == (4, 5, "A")
    refs = {(r.name, r.kind) for r in find_references(JAVA, "java", {"calc", "A", "help"})}
    assert {("calc", "call"), ("A", "call"), ("help", "call")} <= refs


def test_enclosing_definition_prefers_the_innermost():
    defs = extract_definitions(PY, "python")
    assert enclosing_definition(defs, 11).name == "total"
    assert enclosing_definition(defs, 6).name == "__init__"
    assert enclosing_definition(defs, 1) is None


def test_broken_source_degrades_instead_of_raising():
    assert extract_definitions(b"def broken(:\n  ???", "python") is not None
    assert find_references(b"", "python", {"x"}) == []
    assert find_references(b"x", "python", set()) == []


# ------------------------------------------------------------------- query-based structure ---
import re
from pathlib import Path

import pytest

from code_review_agent.code_index import ParsedFile, clean_names, _definition_parts, _classify, _text  # noqa: E402

_SOURCE_DIR = Path(__file__).resolve().parents[1] / "src" / "code_review_agent"


def test_name_filtered_definitions_match_the_full_extraction():
    parsed = ParsedFile.open(PY, "python")
    full = {(d.name, d.start_line, d.container) for d in parsed.definitions()}
    picked = {(d.name, d.start_line, d.container) for d in parsed.definitions({"total", "helper", "missing"})}
    assert picked == {item for item in full if item[0] in {"total", "helper"}}
    assert parsed.definitions(set()) == []  # nothing wanted, nothing returned (and no query is built)


def test_enclosing_comes_from_the_tree_not_from_line_ranges():
    parsed = ParsedFile.open(PY, "python")
    refs = {(r.name, r.line): r for r in parsed.references({"apply_discount"}, with_enclosing=True)}
    assert refs[("apply_discount", 11)].enclosing.name == "total"  # inside the method
    assert refs[("apply_discount", 11)].enclosing.container == "Cart"
    assert refs[("apply_discount", 14)].enclosing.name == "helper"  # a default value belongs to its function
    assert refs[("apply_discount", 2)].enclosing is None  # module level


def test_two_functions_on_one_line_are_told_apart():
    source = b"const a = () => foo(); const b = () => foo();\n"
    refs = ParsedFile.open(source, "typescript").references({"foo"}, with_enclosing=True)
    assert [r.enclosing.name for r in refs] == ["a", "b"]  # a line-range lookup cannot distinguish them


def test_references_are_returned_in_a_stable_total_order():
    parsed = ParsedFile.open(b"a.name.name = name\n", "python")
    assert [(r.line, r.kind) for r in parsed.references({"name"})] == sorted((r.line, r.kind) for r in parsed.references({"name"}))
    assert parsed.references({"name"}) == parsed.references({"name"})


def test_names_that_are_not_identifiers_never_reach_a_query():
    assert clean_names({"ok", "_x", "$y", 'bad"name', "a b", "1st", "x)(", ""}) == {"ok", "_x", "$y"}
    parsed = ParsedFile.open(PY, "python")
    assert parsed.references({'x") (#match? @id ".*'}) == []  # an injection attempt matches nothing, raises nothing


def test_grammar_specific_definition_kinds_are_only_queried_where_they_exist():
    # `interface` is not a JavaScript node type: building the JS query must not fail because of it
    js = ParsedFile.open(b"export class A { m() {} }\nconst f = () => 1;\n", "javascript")
    assert {(d.name, d.kind) for d in js.definitions()} == {("A", "class"), ("m", "method"), ("f", "function")}
    ts = ParsedFile.open(b"interface I { x: number }\ntype T = string;\nenum E { A }\nabstract class B {}\n", "typescript")
    assert {(d.name, d.kind) for d in ts.definitions()} == {("I", "interface"), ("T", "type"), ("E", "enum"), ("B", "class")}


def _walk_definitions(parsed: ParsedFile):
    """Reference implementation: visit every node and ask the decoder whether it defines something."""
    stack = [(parsed.tree.root_node, "")]
    found = []
    while stack:
        node, container = stack.pop()
        parts = _definition_parts(node, parsed.language, container)
        next_container = container
        if parts is not None:
            found.append((parts[0], parts[1], parsed.lines.start(node), parsed.lines.end(node), container))
            if parts[1] in {"class", "interface", "enum"}:
                next_container = parts[0]
        for child in reversed(node.children):
            stack.append((child, next_container))
    return sorted(found, key=lambda item: (item[2], item[3], item[0]))


def _walk_references(parsed: ParsedFile, names: set[str]):
    from code_review_agent.code_index import _IDENTIFIER_TYPES, family

    types = set(_IDENTIFIER_TYPES[family(parsed.language)])
    stack = [parsed.tree.root_node]
    found = []
    while stack:
        node = stack.pop()
        if node.type in types and _text(node) in names:
            kind = _classify(node, parsed.language)
            if kind is not None:
                found.append((parsed.lines.start(node), _text(node), kind))
        stack.extend(reversed(node.children))
    return sorted(found)


@pytest.mark.parametrize("path", sorted(_SOURCE_DIR.glob("*.py")), ids=lambda p: p.name)
def test_queries_find_exactly_what_a_full_walk_finds_on_real_code(path):
    parsed = ParsedFile.open(path.read_bytes(), "python")
    walked = _walk_definitions(parsed)
    queried = sorted(
        ((d.name, d.kind, d.start_line, d.end_line, d.container) for d in parsed.definitions()), key=lambda item: (item[2], item[3], item[0])
    )
    assert queried == walked
    names = {d[0] for d in walked[:20]} | {"logger", "self", "Settings", "Finding", "settings", "re", "os"}
    assert [(r.line, r.name, r.kind) for r in parsed.references(names)] == _walk_references(parsed, names)


def test_one_parsed_file_serves_definitions_references_and_imports():
    parsed = ParsedFile.open(PY, "python")
    tree = parsed.tree
    parsed.definitions(), parsed.references({"Cart"}), parsed.imports()
    assert parsed.tree is tree and parsed.lines is parsed.lines  # parsed once, one shared line table


def test_dollar_prefixed_javascript_names_are_searchable():
    source = b"class $ZodError {}\nfunction $el(a) { return new $ZodError(a); }\n$el(1);\n"
    parsed = ParsedFile.open(source, "typescript")
    assert {d.name for d in parsed.definitions({"$ZodError", "$el"})} == {"$ZodError", "$el"}
    assert {(r.name, r.kind) for r in parsed.references({"$ZodError", "$el"})} == {("$ZodError", "call"), ("$el", "call")}
