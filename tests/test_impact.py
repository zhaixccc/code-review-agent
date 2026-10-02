"""Impact analysis on small on-disk repositories (no network, no model)."""

from pathlib import Path

from code_review_agent.config import Settings
from code_review_agent.impact import analyze_impact
from code_review_agent.models import ChangedFile

SETTINGS = Settings(deepseek_api_key="k")


def write(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def changed(name: str, patch: str) -> ChangedFile:
    return ChangedFile(filename=name, patch=patch)


PRICING_HEAD = 'def apply_discount(price, rate, currency):\n    return price * (1 - rate)\n'
PRICING_PATCH = "@@ -1,2 +1,2 @@\n-def apply_discount(price, rate):\n+def apply_discount(price, rate, currency):\n     return price * (1 - rate)\n"


def test_signature_change_lists_callers_in_unmodified_files_and_tests(tmp_path):
    root = write(
        tmp_path,
        {
            "billing/pricing.py": PRICING_HEAD,
            "orders/checkout.py": "from billing.pricing import apply_discount\n\n\ndef checkout(total, rate):\n    return apply_discount(total, rate)\n",
            "tests/test_pricing.py": "from billing.pricing import apply_discount\n\n\ndef test_it():\n    assert apply_discount(1, 0, 'USD') == 1\n",
            "legacy/other.py": "def apply_discount(a):\n    return a\n",  # unrelated symbol with the same name
        },
    )
    report = analyze_impact(root, [changed("billing/pricing.py", PRICING_PATCH)], {"billing/pricing.py"}, SETTINGS)
    text = report.by_file["billing/pricing.py"]
    assert "SIGNATURE/HEADER CHANGED" in text
    assert "orders/checkout.py:5 in checkout()" in text and "file NOT modified in this change" in text
    assert "return apply_discount(total, rate)" in text  # real code excerpt as evidence
    assert "tests referencing it: tests/test_pricing.py" in text
    assert "1 other definition(s) with the same name" in text  # honest about name-based ambiguity
    assert len(report.summary) == 1 and "apply_discount" in report.summary[0]


def test_removed_or_renamed_symbol_is_reported_with_its_remaining_users(tmp_path):
    root = write(
        tmp_path,
        {
            "utils/text.py": "def normalise_email(v):\n    return v\n",
            "a/signup.py": "from utils.text import normalize_email\n\n\ndef signup(f):\n    return normalize_email(f)\n",
        },
    )
    patch = "@@ -1,2 +1,2 @@\n-def normalize_email(v):\n+def normalise_email(v):\n     return v\n"
    report = analyze_impact(root, [changed("utils/text.py", patch)], {"utils/text.py"}, SETTINGS)
    text = report.by_file["utils/text.py"]
    assert "normalize_email" in text and "REMOVED OR RENAMED" in text and "a/signup.py:5" in text
    assert any("normalize_email" in line and "未随本次改动更新" in line for line in report.summary)


def test_a_symbol_moved_to_another_changed_file_is_not_reported_as_removed(tmp_path):
    root = write(
        tmp_path,
        {
            "old.py": "x = 1\n",
            "new.py": "def helper(v):\n    return v\n",
            "use.py": "from new import helper\n\n\ndef go():\n    return helper(1)\n",
        },
    )
    old_patch = "@@ -1,3 +1 @@\n-def helper(v):\n-    return v\n x = 1\n"
    new_patch = "@@ -0,0 +1,2 @@\n+def helper(v):\n+    return v\n"
    report = analyze_impact(root, [changed("old.py", old_patch), changed("new.py", new_patch)], {"old.py", "new.py"}, SETTINGS)
    assert not any("REMOVED OR RENAMED" in text for text in report.by_file.values())


def test_removing_a_parameter_line_inside_a_signature_counts_as_a_signature_change(tmp_path):
    root = write(
        tmp_path,
        {"lib.py": "def compute(a,\n      c):\n    return a\n", "use.py": "from lib import compute\n\n\ndef go():\n    return compute(1, 2, 3)\n"},
    )
    patch = "@@ -1,4 +1,3 @@\n def compute(a,\n-      b,\n       c):\n     return a\n"
    report = analyze_impact(root, [changed("lib.py", patch)], {"lib.py"}, SETTINGS)
    assert "SIGNATURE/HEADER CHANGED" in report.by_file["lib.py"]


def test_removed_docstring_is_a_body_change_not_a_signature_change(tmp_path):
    root = write(
        tmp_path,
        {"lib.py": "def compute(a):\n    return a\n", "use.py": "from lib import compute\n\n\ndef go():\n    return compute(1)\n"},
    )
    patch = '@@ -1,3 +1,2 @@\n def compute(a):\n-    """doc"""\n     return a\n'
    report = analyze_impact(root, [changed("lib.py", patch)], {"lib.py"}, SETTINGS)
    assert "body changed" in report.by_file["lib.py"] and "SIGNATURE" not in report.by_file["lib.py"]
    assert report.summary == []  # body edits never produce a headline


def test_body_change_with_users_only_in_modified_files_produces_no_evidence(tmp_path):
    root = write(tmp_path, {"app.py": "def _p(n):\n    return PREFIX + n\n\n\ndef greet(n):\n    return _p(n)\n"})
    patch = "@@ -1,2 +1,2 @@\n def _p(n):\n-    return 'Hello, ' + n\n+    return PREFIX + n\n"
    report = analyze_impact(root, [changed("app.py", patch)], {"app.py"}, SETTINGS)
    assert report.by_file == {} and report.summary == []


def test_no_callers_means_no_evidence(tmp_path):
    root = write(tmp_path, {"lib.py": "def lonely(a, b):\n    return a\n"})
    patch = "@@ -1,2 +1,2 @@\n-def lonely(a):\n+def lonely(a, b):\n     return a\n"
    assert analyze_impact(root, [changed("lib.py", patch)], {"lib.py"}, SETTINGS).by_file == {}


def test_constructor_changes_search_for_the_class_name(tmp_path):
    root = write(
        tmp_path,
        {
            "models.py": "class Account:\n    def __init__(self, owner, currency):\n        self.owner = owner\n",
            "svc.py": "from models import Account\n\n\ndef open_account(o):\n    return Account(o)\n",
        },
    )
    patch = "@@ -1,3 +1,3 @@\n class Account:\n-    def __init__(self, owner):\n+    def __init__(self, owner, currency):\n         self.owner = owner\n"
    text = analyze_impact(root, [changed("models.py", patch)], {"models.py"}, SETTINGS).by_file["models.py"]
    assert "referenced as Account" in text and "svc.py:5" in text


def test_generic_method_names_are_not_searched(tmp_path):
    root = write(
        tmp_path,
        {"w.py": "class W:\n    def get(self, key, default):\n        return default\n", "x.py": "d = {}\nd.get('a')\n"},
    )
    patch = "@@ -1,3 +1,3 @@\n class W:\n-    def get(self, key):\n+    def get(self, key, default):\n         return default\n"
    assert analyze_impact(root, [changed("w.py", patch)], {"w.py"}, SETTINGS).by_file == {}


def test_excerpts_are_redacted_before_they_can_reach_the_model(tmp_path):
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    root = write(
        tmp_path,
        {
            "lib.py": "def compute(a, b):\n    return a\n",
            "use.py": f"def go():\n    key = '{token}'\n    return compute(1)\n",
        },
    )
    patch = "@@ -1,2 +1,2 @@\n-def compute(a):\n+def compute(a, b):\n     return a\n"
    text = analyze_impact(root, [changed("lib.py", patch)], {"lib.py"}, SETTINGS).by_file["lib.py"]
    assert token not in text and "已脱敏" in text


def test_evidence_respects_the_size_budget(tmp_path):
    users = {f"u{i}.py": f"from lib import compute\n\n\ndef go{i}():\n    return compute(1)\n" for i in range(6)}
    root = write(tmp_path, {"lib.py": "def compute(a, b):\n    return a\n", **users})
    patch = "@@ -1,2 +1,2 @@\n-def compute(a):\n+def compute(a, b):\n     return a\n"
    small = Settings(deepseek_api_key="k", impact_evidence_chars=1000, impact_max_callers=2)
    text = analyze_impact(root, [changed("lib.py", patch)], {"lib.py"}, small).by_file["lib.py"]
    assert text.count("file NOT modified") == 2  # capped by impact_max_callers
    assert "callers/users found: 6 (showing 2)" in text


def test_time_budget_marks_the_result_incomplete(tmp_path):
    users = {f"u{i}.py": "from lib import compute\n\n\ndef go():\n    return compute(1)\n" for i in range(5)}
    root = write(tmp_path, {"lib.py": "def compute(a, b):\n    return a\n", **users})
    patch = "@@ -1,2 +1,2 @@\n-def compute(a):\n+def compute(a, b):\n     return a\n"
    capped = Settings(deepseek_api_key="k", impact_max_parse_files=2)
    report = analyze_impact(root, [changed("lib.py", patch)], {"lib.py"}, capped)
    assert report.stats["incomplete"] is True and "callers may be missing" in report.by_file["lib.py"]


def test_sensitive_and_unsupported_files_are_ignored(tmp_path):
    root = write(tmp_path, {".env": "A=1\n", "notes.md": "x\n"})
    report = analyze_impact(root, [changed(".env", "@@ -0,0 +1 @@\n+A=1\n"), changed("notes.md", "@@ -0,0 +1 @@\n+x\n")], set(), SETTINGS)
    assert report.by_file == {} and report.stats["symbols"] == 0


def test_typescript_signature_change(tmp_path):
    root = write(
        tmp_path,
        {
            "src/format.ts": "export function formatPrice(amount: number, locale: string): string {\n  return String(amount);\n}\n",
            "src/cart.ts": "import { formatPrice } from './format';\n\nexport function renderTotal(t: number): string {\n  return formatPrice(t);\n}\n",
        },
    )
    patch = "@@ -1,3 +1,3 @@\n-export function formatPrice(amount: number): string {\n+export function formatPrice(amount: number, locale: string): string {\n   return String(amount);\n }\n"
    text = analyze_impact(root, [changed("src/format.ts", patch)], {"src/format.ts"}, SETTINGS).by_file["src/format.ts"]
    assert "src/cart.ts:4 in renderTotal()" in text


def test_new_files_and_brand_new_functions_have_no_old_dependents(tmp_path):
    root = write(
        tmp_path,
        {
            "fresh.py": "def brand_new(a):\n    return a\n\n\ndef caller():\n    return brand_new(1)\n",
            "mod.py": "def existing(a):\n    return a\n\n\ndef added_later(b):\n    return b\n\n\ndef user():\n    return added_later(2)\n",
        },
    )
    fresh = ChangedFile(filename="fresh.py", status="added", patch="@@ -0,0 +1,6 @@\n+def brand_new(a):\n+    return a\n+\n+\n+def caller():\n+    return brand_new(1)\n")
    modified = changed("mod.py", "@@ -2,0 +3,5 @@\n+\n+\n+def added_later(b):\n+    return b\n")
    report = analyze_impact(root, [fresh, modified], {"fresh.py", "mod.py"}, SETTINGS)
    assert report.by_file == {}  # added_later is new in a modified file: no pre-existing caller can be broken by it


def test_a_fully_rewritten_function_that_keeps_its_name_is_still_analysed(tmp_path):
    root = write(
        tmp_path,
        {"lib.py": "def compute(a, b):\n    return b\n", "use.py": "from lib import compute\n\n\ndef go():\n    return compute(1)\n"},
    )
    patch = "@@ -1,2 +1,2 @@\n-def compute(a):\n-    return a\n+def compute(a, b):\n+    return b\n"
    assert "SIGNATURE/HEADER CHANGED" in analyze_impact(root, [changed("lib.py", patch)], {"lib.py"}, SETTINGS).by_file["lib.py"]
