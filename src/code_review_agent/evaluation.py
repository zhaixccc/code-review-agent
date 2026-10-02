"""Evaluation harness: seeded-defect changes with known expected findings.

A case is a ``base`` and a ``head`` snapshot of a tiny repository plus the findings a good reviewer must report
(``expect``). Clean cases expect nothing at major/critical severity. The harness runs the real review graph in
dry-run mode against fixtures (no GitHub, no posting) and scores recall and false positives, so changes such as
"enable impact analysis" or "turn verification off" can be compared on the same cases.

Matching is a regex over the finding text, so it is deliberately coarse; read the printed findings too.
"""

from __future__ import annotations

import difflib
import json
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from .config import Settings
from .graph import _final_reviews, build_graph, run_review
from .models import SEVERITY_ORDER, ChangedFile, Finding, ReviewTarget
from .report import collect


@dataclass(frozen=True)
class Expectation:
    file: str
    pattern: str  # regex (case-insensitive) searched in title + detail + suggestion
    min_severity: str = "major"


@dataclass(frozen=True)
class EvalCase:
    name: str
    category: str  # local | cross_file | clean
    description: str
    message: str
    base: dict[str, str]
    head: dict[str, str]
    expect: tuple[Expectation, ...] = ()


# A secret-looking value assembled at runtime so this file itself never contains one.
_FAKE_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"

CASES: tuple[EvalCase, ...] = (
    EvalCase(
        name="py_signature_break",
        category="cross_file",
        description="A new required parameter is added; the only caller (another file, unchanged) is not updated.",
        message="Support zero-decimal currencies in discounts",
        base={
            "billing/pricing.py": 'def apply_discount(price, rate):\n    """Return the price after a percentage discount."""\n    return round(price * (1 - rate), 2)\n',
            "orders/checkout.py": 'from billing.pricing import apply_discount\n\n\ndef checkout(cart_total, coupon_rate):\n    """Charge the customer the discounted total."""\n    final = apply_discount(cart_total, coupon_rate)\n    return {"charged": final}\n',
            "tests/test_pricing.py": "from billing.pricing import apply_discount\n\n\ndef test_apply_discount():\n    assert apply_discount(100, 0.1) == 90\n",
        },
        head={
            "billing/pricing.py": 'def apply_discount(price, rate, currency):\n    """Return the price after a percentage discount."""\n    digits = 0 if currency == "JPY" else 2\n    return round(price * (1 - rate), digits)\n',
            "orders/checkout.py": 'from billing.pricing import apply_discount\n\n\ndef checkout(cart_total, coupon_rate):\n    """Charge the customer the discounted total."""\n    final = apply_discount(cart_total, coupon_rate)\n    return {"charged": final}\n',
            "tests/test_pricing.py": "from billing.pricing import apply_discount\n\n\ndef test_apply_discount():\n    assert apply_discount(100, 0.1) == 90\n",
        },
        expect=(Expectation("billing/pricing.py", r"checkout"),),  # must name the broken caller, which the diff never shows
    ),
    EvalCase(
        name="py_removed_function",
        category="cross_file",
        description="A helper is renamed; two callers in unchanged files still use the old name.",
        message="Rename email helper to British spelling",
        base={
            "utils/text.py": 'def normalize_email(value):\n    """Lower-case and strip an e-mail address."""\n    return value.strip().lower()\n\n\ndef slugify(value):\n    return value.strip().lower().replace(" ", "-")\n',
            "accounts/signup.py": 'from utils.text import normalize_email\n\n\ndef signup(form):\n    email = normalize_email(form["email"])\n    return {"email": email}\n',
            "accounts/login.py": 'from utils.text import normalize_email\n\n\ndef login(form):\n    email = normalize_email(form["email"])\n    return {"email": email, "ok": True}\n',
        },
        head={
            "utils/text.py": 'def normalise_email(value):\n    """Lower-case and strip an e-mail address."""\n    return value.strip().lower()\n\n\ndef slugify(value):\n    return value.strip().lower().replace(" ", "-")\n',
            "accounts/signup.py": 'from utils.text import normalize_email\n\n\ndef signup(form):\n    email = normalize_email(form["email"])\n    return {"email": email}\n',
            "accounts/login.py": 'from utils.text import normalize_email\n\n\ndef login(form):\n    email = normalize_email(form["email"])\n    return {"email": email, "ok": True}\n',
        },
        expect=(Expectation("utils/text.py", r"signup|login"),),
    ),
    EvalCase(
        name="py_return_semantics",
        category="cross_file",
        description="A list-returning function now returns None when empty; the caller iterates over the result.",
        message="Return None when no item is low on stock",
        base={
            "inventory/stock.py": 'def low_stock_items(items, threshold):\n    """Return the items below the threshold (an empty list when none)."""\n    return [item for item in items if item["qty"] < threshold]\n',
            "reports/daily.py": 'from inventory.stock import low_stock_items\n\n\ndef daily_report(items):\n    lines = []\n    for item in low_stock_items(items, 5):\n        lines.append(f"low: {item[\'name\']}")\n    return lines\n',
        },
        head={
            "inventory/stock.py": 'def low_stock_items(items, threshold):\n    """Return the items below the threshold, or None when nothing is low."""\n    low = [item for item in items if item["qty"] < threshold]\n    return low or None\n',
            "reports/daily.py": 'from inventory.stock import low_stock_items\n\n\ndef daily_report(items):\n    lines = []\n    for item in low_stock_items(items, 5):\n        lines.append(f"low: {item[\'name\']}")\n    return lines\n',
        },
        expect=(Expectation("inventory/stock.py", r"daily"),),
    ),
    EvalCase(
        name="ts_signature_break",
        category="cross_file",
        description="A TypeScript function gains a required parameter; its caller in another file is not updated.",
        message="Localise price formatting",
        base={
            "src/format.ts": "export function formatPrice(amount: number): string {\n  return `$${amount.toFixed(2)}`;\n}\n",
            "src/cart.ts": "import { formatPrice } from './format';\n\nexport function renderTotal(total: number): string {\n  return `Total: ${formatPrice(total)}`;\n}\n",
        },
        head={
            "src/format.ts": "export function formatPrice(amount: number, locale: string): string {\n  return new Intl.NumberFormat(locale, { style: 'currency', currency: 'USD' }).format(amount);\n}\n",
            "src/cart.ts": "import { formatPrice } from './format';\n\nexport function renderTotal(total: number): string {\n  return `Total: ${formatPrice(total)}`;\n}\n",
        },
        expect=(Expectation("src/format.ts", r"cart|renderTotal"),),
    ),
    EvalCase(
        name="py_sql_injection",
        category="local",
        description="A new lookup builds SQL with an f-string from user input.",
        message="Add lookup by name",
        base={
            "users/repo.py": 'def get_user(db, user_id):\n    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()\n',
        },
        head={
            "users/repo.py": 'def get_user(db, user_id):\n    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()\n\n\ndef find_by_name(db, name):\n    query = f"SELECT * FROM users WHERE name = \'{name}\'"\n    return db.execute(query).fetchall()\n',
        },
        expect=(Expectation("users/repo.py", r"SQL|injection|注入|parameter|参数化", "major"),),
    ),
    EvalCase(
        name="py_hardcoded_token",
        category="local",
        description="A real-looking GitHub token is committed (must be caught deterministically, without the model).",
        message="Add GitHub sync",
        base={"sync/github.py": "import os\n\nTOKEN = os.environ.get('GITHUB_TOKEN')\n"},
        head={"sync/github.py": f"TOKEN = '{_FAKE_TOKEN}'\n"},
        expect=(Expectation("sync/github.py", r"凭据|密钥|token|Token|泄露", "critical"),),
    ),
    EvalCase(
        name="py_clean_refactor",
        category="clean",
        description="A behaviour-preserving refactor (type hints, docstring, clearer names). Nothing should be flagged.",
        message="Tidy up the average helper",
        base={"stats/avg.py": "def avg(xs):\n    t = 0\n    for x in xs:\n        t += x\n    return t / len(xs)\n"},
        head={"stats/avg.py": 'def avg(values: list[float]) -> float:\n    """Arithmetic mean of a non-empty list."""\n    total = 0.0\n    for value in values:\n        total += value\n    return total / len(values)\n'},
    ),
    EvalCase(
        name="py_signature_change_callers_updated",
        category="clean",
        description="A parameter is added AND every caller is updated in the same change. Must not be reported as breaking.",
        message="Add currency to apply_discount and update checkout",
        base={
            "billing/pricing.py": 'def apply_discount(price, rate):\n    return round(price * (1 - rate), 2)\n',
            "orders/checkout.py": 'from billing.pricing import apply_discount\n\n\ndef checkout(total, rate):\n    return apply_discount(total, rate)\n',
            "orders/refund.py": 'from billing.pricing import apply_discount\n\n\ndef refund(total, rate):\n    return apply_discount(total, rate)\n',
        },
        head={
            "billing/pricing.py": 'def apply_discount(price, rate, currency):\n    return round(price * (1 - rate), 2)\n',
            "orders/checkout.py": 'from billing.pricing import apply_discount\n\n\ndef checkout(total, rate):\n    return apply_discount(total, rate, "USD")\n',
            "orders/refund.py": 'from billing.pricing import apply_discount\n\n\ndef refund(total, rate):\n    return apply_discount(total, rate, "USD")\n',
        },
    ),
    EvalCase(
        name="py_compatible_signature_change",
        category="clean",
        description="An optional parameter with a default is added; unmodified callers keep working. Must not be reported.",
        message="Allow overriding the currency",
        base={
            "billing/pricing.py": 'def apply_discount(price, rate):\n    return round(price * (1 - rate), 2)\n',
            "orders/checkout.py": 'from billing.pricing import apply_discount\n\n\ndef checkout(total, rate):\n    return apply_discount(total, rate)\n',
        },
        head={
            "billing/pricing.py": 'def apply_discount(price, rate, currency="USD"):\n    return round(price * (1 - rate), 2)\n',
            "orders/checkout.py": 'from billing.pricing import apply_discount\n\n\ndef checkout(total, rate):\n    return apply_discount(total, rate)\n',
        },
    ),
    EvalCase(
        name="py_clean_internal_change",
        category="clean",
        description="A private helper changes internally; its only user is the same file and is consistent.",
        message="Cache the greeting prefix",
        base={"app/greet.py": 'def _prefix(name):\n    return "Hello, " + name\n\n\ndef greet(name):\n    return _prefix(name) + "!"\n'},
        head={"app/greet.py": 'PREFIX = "Hello, "\n\n\ndef _prefix(name):\n    return PREFIX + name\n\n\ndef greet(name):\n    return _prefix(name) + "!"\n'},
    ),
)


class FixtureGitHub:
    """Serves one fixture as a commit (only what the review graph needs in dry-run mode)."""

    def __init__(self, case: EvalCase) -> None:
        self._case = case

    def get_commit(self, repo: str, sha: str, max_pages: int = 5) -> tuple[str, list[ChangedFile]]:
        return self._case.message, diff_files(self._case.base, self._case.head)


class FixtureSnapshots:
    """Writes the fixture's HEAD files to a temporary directory, like a repository snapshot at the reviewed commit."""

    def __init__(self, case: EvalCase, directory: Path) -> None:
        self._root = directory
        for name, text in case.head.items():
            target = directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    def get(self, repo: str, sha: str) -> Path | None:
        return self._root


def diff_files(base: dict[str, str], head: dict[str, str]) -> list[ChangedFile]:
    files: list[ChangedFile] = []
    for path in sorted(set(base) | set(head)):
        old, new = base.get(path), head.get(path)
        if old == new:
            continue
        lines = list(difflib.unified_diff((old or "").splitlines(), (new or "").splitlines(), fromfile="", tofile="", lineterm="", n=3))[2:]
        files.append(
            ChangedFile(
                filename=path,
                status="added" if old is None else "removed" if new is None else "modified",
                additions=sum(1 for line in lines if line.startswith("+")),
                deletions=sum(1 for line in lines if line.startswith("-")),
                patch="\n".join(lines),
            )
        )
    return files


@dataclass
class CaseResult:
    name: str
    category: str
    expected: int
    matched: int
    false_positives: list[str]
    findings: list[str]
    verify_log: list[dict[str, Any]] = field(default_factory=list)
    impact_found: bool = False
    errors: list[str] = field(default_factory=list)


@dataclass
class EvalReport:
    results: list[CaseResult]
    options: dict[str, Any]

    @property
    def expected(self) -> int:
        return sum(r.expected for r in self.results)

    @property
    def matched(self) -> int:
        return sum(r.matched for r in self.results)

    @property
    def false_positives(self) -> int:
        return sum(len(r.false_positives) for r in self.results)

    @property
    def recall(self) -> float:
        return self.matched / self.expected if self.expected else 1.0

    @property
    def precision(self) -> float:
        reported = self.matched + self.false_positives
        return self.matched / reported if reported else 1.0

    def by_category(self) -> dict[str, tuple[int, int, int]]:
        table: dict[str, list[int]] = {}
        for r in self.results:
            row = table.setdefault(r.category, [0, 0, 0])
            row[0] += r.expected
            row[1] += r.matched
            row[2] += len(r.false_positives)
        return {key: (row[0], row[1], row[2]) for key, row in table.items()}

    def render(self) -> str:
        lines = [f"options: {json.dumps(self.options, ensure_ascii=False)}", ""]
        for r in self.results:
            status = "OK " if r.matched == r.expected and not r.false_positives else "BAD"
            lines.append(
                f"[{status}] {r.name:<26} {r.category:<10} expected={r.expected} matched={r.matched} "
                f"false_positives={len(r.false_positives)} impact_evidence={'yes' if r.impact_found else 'no'}"
            )
            for text in r.false_positives:
                lines.append(f"        false positive: {text}")
            for text in r.errors:
                lines.append(f"        error: {text}")
        lines.append("")
        for category, (expected, matched, false_positives) in self.by_category().items():
            lines.append(f"{category:<10} recall {matched}/{expected}   false positives {false_positives}")
        lines.append(f"TOTAL      recall {self.matched}/{self.expected} = {self.recall:.0%}   precision = {self.precision:.0%}   false positives = {self.false_positives}")
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "options": self.options,
            "recall": self.recall,
            "precision": self.precision,
            "false_positives": self.false_positives,
            "cases": [r.__dict__ for r in self.results],
        }


def score_case(case: EvalCase, found: list[tuple[str, Finding]]) -> tuple[int, list[str]]:
    """Return (number of expectations met, descriptions of major/critical findings that matched no expectation)."""
    matched = 0
    used: set[int] = set()
    for expectation in case.expect:
        limit = SEVERITY_ORDER[expectation.min_severity]
        pattern = re.compile(expectation.pattern, re.IGNORECASE)
        hit = False
        for index, (path, finding) in enumerate(found):
            text = f"{finding.title}\n{finding.detail}\n{finding.suggestion or ''}"
            if path == expectation.file and SEVERITY_ORDER[finding.severity] <= limit and pattern.search(text):
                hit = True
                used.add(index)
        matched += 1 if hit else 0
    false_positives = [
        f"[{finding.severity}] {path}: {finding.title}"
        for index, (path, finding) in enumerate(found)
        if index not in used and SEVERITY_ORDER[finding.severity] <= SEVERITY_ORDER["major"]
    ]
    return matched, false_positives


def run_eval(settings: Settings, llm: BaseChatModel, names: list[str] | None = None, runs: int = 1) -> EvalReport:
    selected = [case for case in CASES if not names or case.name in names]
    if not selected:
        raise ValueError("no evaluation case matches the given names: " + ", ".join(case.name for case in CASES))
    results: list[CaseResult] = []
    for case in selected:
        for _ in range(max(1, runs)):
            with tempfile.TemporaryDirectory(prefix="cra-eval-") as directory:
                snapshots = FixtureSnapshots(case, Path(directory)) if settings.impact_enabled else None
                graph = build_graph(settings, FixtureGitHub(case), llm, dry_run=True, snapshots=snapshots)  # type: ignore[arg-type]
                state = run_review(graph, ReviewTarget(repo="eval/fixture", sha="0" * 40), settings)
            found = collect(_final_reviews(state))
            matched, false_positives = score_case(case, found)
            results.append(
                CaseResult(
                    name=case.name,
                    category=case.category,
                    expected=len(case.expect),
                    matched=matched,
                    false_positives=false_positives,
                    findings=[f"[{f.severity}/{f.category}] {path}: {f.title}" for path, f in found],
                    verify_log=state.get("verify_log", []),
                    impact_found=bool(state.get("impact")),
                    errors=list(state.get("errors", [])),
                )
            )
    options = {"impact": settings.impact_enabled, "verify": settings.verify_findings, "model": settings.deepseek_model, "runs": runs}
    return EvalReport(results=results, options=options)


def save_report(report: EvalReport, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"eval-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path
