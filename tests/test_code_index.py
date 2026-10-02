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
