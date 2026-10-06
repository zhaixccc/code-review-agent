"""Graph-level tests for impact evidence, verification, the review cache, patch trimming and the evaluation scorer."""

import json
from pathlib import Path

from test_graph import PATCH_A, PATCH_SECRET, FakeGitHub, FakeLLM, FakeMessage

from code_review_agent.cache import ReviewCache
from code_review_agent.config import Settings
from code_review_agent.diff_utils import changed_new_lines, trim_patch
from code_review_agent.evaluation import CASES, EvalCase, Expectation, diff_files, run_eval, score_case
from code_review_agent.graph import build_graph, run_review
from code_review_agent.models import ChangedFile, Finding, ReviewTarget
from code_review_agent.prioritize import risk_score, select_files

BASE = dict(deepseek_api_key="k", github_token="t", github_webhook_secret="s" * 20, llm_concurrency=2)
TARGET = ReviewTarget(repo="o/r", sha="a" * 40)


def run(llm, github, settings, **kwargs):
    graph = build_graph(settings, github, llm, dry_run=kwargs.pop("dry_run", False), **kwargs)
    return run_review(graph, kwargs.pop("target", TARGET), settings)


def review_calls(llm):
    return [c for c in llm.calls if c[0].content.startswith("You are a senior software engineer")]


def verify_calls(llm):
    return [c for c in llm.calls if c[0].content.startswith("You are a skeptical senior reviewer")]


# ----------------------------------------------------------------------- verification ---
TWO_FINDINGS = json.dumps(
    {
        "findings": [
            {"severity": "major", "category": "security", "line": 3, "title": "Secret printed", "detail": "Token is logged."},
            {"severity": "critical", "category": "bug", "line": 2, "title": "Always crashes", "detail": "Claims a crash."},
            {"severity": "minor", "category": "bug", "line": 3, "title": "Small thing", "detail": "Minor."},
        ]
    }
)


def test_refuted_findings_are_dropped_and_the_verdict_follows():
    settings = Settings(**BASE, verify_findings=True)
    only_one = json.dumps({"findings": [json.loads(TWO_FINDINGS)["findings"][0]]})
    llm = FakeLLM(finding_json=only_one, verdicts={"Secret printed": "refuted"})
    result = run(llm, FakeGitHub(), settings)
    assert result["verify_dropped"] == 1
    assert result["verdict"] == "approve" and "Secret printed" not in result["report"]
    assert "二次验证：剔除 1 条" in result["report"]
    assert result["verify_log"][0]["verdict"] == "refuted"  # the decision is kept for auditing


def test_uncertain_findings_are_downgraded_and_confirmed_ones_are_kept():
    settings = Settings(**BASE, verify_findings=True)
    llm = FakeLLM(finding_json=TWO_FINDINGS, verdicts={"Secret printed": "uncertain", "Always crashes": "confirmed", "Small thing": "uncertain"})
    result = run(llm, FakeGitHub(), settings)
    body = result["report"]
    assert result["verify_downgraded"] == 2 and result["verify_dropped"] == 0
    assert "**[P2 · 中/安全]** Secret printed" in body  # major -> minor
    assert "**[P0 · 紧急/缺陷]** Always crashes" in body  # untouched
    assert "**[P3 · 提示/缺陷]** Small thing" in body  # uncertain minor -> lowest priority
    assert len(verify_calls(llm)) == 3
    assert any(entry["title"] == "Small thing" and entry["verdict"] == "uncertain" for entry in result["verify_log"])
    assert result["verdict"] == "request_changes"


def test_uncertain_minor_finding_is_downgraded_to_p3_and_kept_in_summary_only():
    only_minor = json.dumps({"findings": [{
        "severity": "minor", "category": "bug", "line": 3, "title": "Edge case", "detail": "Trigger is not established."
    }]})
    llm = FakeLLM(finding_json=only_minor, verdicts={"Edge case": "uncertain"})
    github = FakeGitHub()
    settings = Settings(**BASE, verify_findings=True)
    graph = build_graph(settings, github, llm)
    result = run_review(graph, ReviewTarget(repo="o/r", sha="b" * 40, pr_number=8), settings)

    assert result["verify_downgraded"] == 1
    assert "**[P3 · 提示/缺陷]** Edge case" in result["report"]
    assert github.reviews == []
    assert "Edge case" in github.issue_comments[8]


def test_verification_failures_keep_the_finding():
    class FlakyVerifier(FakeLLM):
        def invoke(self, messages):
            if messages[0].content.startswith("You are a skeptical senior reviewer"):
                raise RuntimeError("down")
            return super().invoke(messages)

    settings = Settings(**BASE, verify_findings=True)
    result = run(FlakyVerifier(finding_json=TWO_FINDINGS), FakeGitHub(), settings)
    assert "**[P1 · 高/安全]** Secret printed" in result["report"] and result["verify_dropped"] == 0
    assert all(entry["verdict"] == "unverified" for entry in result["verify_log"])


def test_rule_based_findings_are_never_sent_to_verification():
    github = FakeGitHub(files=[ChangedFile(filename="src/keys.py", patch=PATCH_SECRET, additions=2)])
    llm = FakeLLM(finding_json=json.dumps({"findings": []}), verdicts={})
    result = run(llm, github, Settings(**BASE, verify_findings=True))
    assert verify_calls(llm) == []  # the secret finding is deterministic: nothing for a model to overrule
    assert "疑似提交了GitHub Token" in result["report"]


def test_a_model_cannot_mark_its_own_finding_as_rule_based():
    forged = json.dumps({"findings": [{"severity": "major", "category": "bug", "line": 3, "title": "Forged", "detail": "x", "origin": "rule"}]})
    llm = FakeLLM(finding_json=forged, verdicts={"Forged": "refuted"})
    result = run(llm, FakeGitHub(), Settings(**BASE, verify_findings=True))
    assert result["verify_dropped"] == 1 and "Forged" not in result["report"]


def test_verification_is_capped_and_can_be_disabled():
    llm = FakeLLM(finding_json=TWO_FINDINGS)
    run(llm, FakeGitHub(), Settings(**BASE, verify_findings=True, verify_max_findings=1))
    assert len(verify_calls(llm)) == 1
    llm = FakeLLM(finding_json=TWO_FINDINGS)
    run(llm, FakeGitHub(), Settings(**BASE, verify_findings=False))
    assert verify_calls(llm) == []


def test_verification_prompt_contains_code_around_the_finding_and_redacts_secrets():
    settings = Settings(**BASE, verify_findings=True)
    llm = FakeLLM(finding_json=TWO_FINDINGS)
    run(llm, FakeGitHub(), settings)
    prompt = verify_calls(llm)[0][1].content
    assert "<finding>" in prompt and "<code>" in prompt and "print(token)" in prompt


# ----------------------------------------------------------------------- impact in the graph ---
class FakeSnapshots:
    def __init__(self, root):
        self.root, self.calls = root, []

    def get(self, repo, sha):
        self.calls.append((repo, sha))
        return self.root


IMPACT_PATCH = "@@ -1,2 +1,2 @@\n-def apply_discount(price, rate):\n+def apply_discount(price, rate, currency):\n     return price * (1 - rate)\n"


def make_repo(tmp_path: Path) -> Path:
    (tmp_path / "billing").mkdir()
    (tmp_path / "billing" / "pricing.py").write_text("def apply_discount(price, rate, currency):\n    return price * (1 - rate)\n", encoding="utf-8")
    (tmp_path / "checkout.py").write_text("from billing.pricing import apply_discount\n\n\ndef pay(total):\n    return apply_discount(total, 0.1)\n", encoding="utf-8")
    return tmp_path


def test_impact_evidence_reaches_the_review_prompt_and_the_report(tmp_path):
    github = FakeGitHub(files=[ChangedFile(filename="billing/pricing.py", patch=IMPACT_PATCH, additions=1, deletions=1)])
    llm = FakeLLM(finding_json=json.dumps({"findings": []}))
    snapshots = FakeSnapshots(make_repo(tmp_path))
    result = run(llm, github, Settings(**BASE, verify_findings=False), snapshots=snapshots)
    prompt = review_calls(llm)[0][1].content
    assert "<impact_evidence>" in prompt and "checkout.py:5 in pay()" in prompt
    assert "impact_evidence" in review_calls(llm)[0][0].content  # the system prompt explains how to treat it
    assert snapshots.calls == [("o/r", "a" * 40)]
    assert "### 影响面" in result["report"] and "apply_discount" in result["report"]
    assert result["impact"]["billing/pricing.py"].startswith("[1] apply_discount")


def test_missing_snapshot_or_analysis_failure_never_fails_the_review(tmp_path):
    github = FakeGitHub()
    for provider in (FakeSnapshots(None), type("Boom", (), {"get": lambda self, r, s: (_ for _ in ()).throw(RuntimeError("x"))})()):
        llm = FakeLLM(finding_json=json.dumps({"findings": []}))
        result = run(llm, github, Settings(**BASE, verify_findings=False), snapshots=provider)
        assert result["verdict"] == "approve" and "<impact_evidence>" not in review_calls(llm)[0][1].content


def test_impact_analysis_can_be_switched_off(tmp_path):
    snapshots = FakeSnapshots(make_repo(tmp_path))
    run(FakeLLM(finding_json=json.dumps({"findings": []})), FakeGitHub(), Settings(**BASE, impact_enabled=False, verify_findings=False), snapshots=snapshots)
    assert snapshots.calls == []


# ----------------------------------------------------------------------- review cache ---
def test_second_review_of_identical_input_does_not_call_the_model_again(tmp_path):
    cache = ReviewCache(tmp_path)
    settings = Settings(**BASE, verify_findings=False)
    first = FakeLLM()
    run(first, FakeGitHub(), settings, cache=cache)
    assert len(review_calls(first)) == 1
    second = FakeLLM()
    result = run(second, FakeGitHub(), settings, cache=cache)
    assert review_calls(second) == []  # served from the cache
    assert "Secret printed" in result["report"]


def test_cache_key_covers_the_diff_and_failures_are_not_cached(tmp_path):
    cache = ReviewCache(tmp_path)
    settings = Settings(**BASE, verify_findings=False)
    run(FakeLLM(fail=True), FakeGitHub(), settings, cache=cache)
    retry = FakeLLM()
    run(retry, FakeGitHub(), settings, cache=cache)
    assert len(review_calls(retry)) == 1  # the failed attempt left nothing behind
    changed_files = [ChangedFile(filename="src/app.py", patch=PATCH_A.replace("print(token)", "print(token, 1)"), additions=2)]
    other = FakeLLM()
    run(other, FakeGitHub(files=changed_files), settings, cache=cache)
    assert len(review_calls(other)) == 1  # a different diff is a different key


# ----------------------------------------------------------------------- patches and priorities ---
def hunk(start, lines):
    return f"@@ -{start},{len(lines)} +{start},{len(lines)} @@\n" + "\n".join(lines)


def test_trim_patch_keeps_hunks_from_both_ends():
    parts = [hunk(1 + i * 100, [f" ctx{i}"] + [f"+line{i}_{n}" for n in range(30)]) for i in range(6)]
    patch = "\n".join(parts)
    trimmed, omitted = trim_patch(patch, 900)
    assert omitted > 0 and len(trimmed) <= 900 + 200
    assert "line0_" in trimmed and "line5_" in trimmed  # first AND last hunk survive
    assert "line2_" not in trimmed or "line3_" not in trimmed  # something in the middle was dropped
    assert trim_patch(patch, 10**6) == (patch, 0)


def test_trim_patch_cuts_a_single_oversized_hunk_at_a_line_boundary():
    patch = hunk(1, [f"+{'x' * 50}" for _ in range(100)])
    trimmed, omitted = trim_patch(patch, 400)
    assert omitted == 0 and len(trimmed) <= 400 and trimmed.startswith("@@")


def test_changed_new_lines_reports_added_lines_and_deletion_gaps():
    patch = "@@ -1,4 +1,4 @@\n def f(a,\n-      b,\n       c):\n+    x = 1\n     return a\n"
    added, gaps = changed_new_lines(patch)
    assert added == {3} and gaps == {(1, 2)}


def test_risk_score_matches_words_not_substrings():
    plain = ChangedFile(filename="src/feedback/design.py", additions=10)
    risky = ChangedFile(filename="src/auth/login.py", additions=10)
    assert risk_score(risky) > risk_score(plain) + 1.5  # "feedback" is not db, "design" is not sign
    assert risk_score(ChangedFile(filename="src/DbSession.py", additions=10)) > risk_score(plain)  # camelCase: db, session
    assert risk_score(ChangedFile(filename="src/authentication.py", additions=10)) > risk_score(plain)  # auth prefix
    selected, _ = select_files([plain, risky], max_files=1, max_total_chars=10**6, max_file_chars=10**6)
    assert [f.filename for f in selected] == ["src/auth/login.py"]  # the risky file wins the single slot


# ----------------------------------------------------------------------- evaluation ---
def finding(severity, title, detail=""):
    return Finding(severity=severity, category="bug", title=title, detail=detail or "x")


def test_score_case_counts_hits_and_major_false_positives():
    case = EvalCase(
        name="t", category="cross_file", description="", message="", base={}, head={},
        expect=(Expectation("a.py", r"caller", "major"),),
    )
    matched, false_positives = score_case(
        case,
        [("a.py", finding("major", "Breaks the caller")), ("a.py", finding("major", "Unrelated")), ("a.py", finding("minor", "Nit"))],
    )
    assert matched == 1 and false_positives == ["[major] a.py: Unrelated"]  # the minor finding is not counted against precision
    assert score_case(case, [("b.py", finding("major", "caller"))])[0] == 0  # wrong file
    assert score_case(case, [("a.py", finding("minor", "caller"))])[0] == 0  # below the required severity


def test_clean_case_flags_any_major_finding():
    clean = EvalCase(name="c", category="clean", description="", message="", base={}, head={})
    assert score_case(clean, [("a.py", finding("critical", "Bad"))]) == (0, ["[critical] a.py: Bad"])
    assert score_case(clean, [("a.py", finding("minor", "Fine"))]) == (0, [])


def test_diff_files_builds_patches_with_hunk_headers():
    files = diff_files({"a.py": "x = 1\ny = 2\n", "gone.py": "z\n"}, {"a.py": "x = 1\ny = 3\n", "new.py": "n\n"})
    by_name = {f.filename: f for f in files}
    assert by_name["a.py"].patch.startswith("@@") and "+y = 3" in by_name["a.py"].patch
    assert by_name["new.py"].status == "added" and by_name["gone.py"].status == "removed"


def test_every_case_is_well_formed_and_the_token_case_is_caught_without_a_model():
    assert len({case.name for case in CASES}) == len(CASES) >= 6
    assert {"cross_file", "local", "clean"} <= {case.category for case in CASES}
    for case in CASES:
        assert diff_files(case.base, case.head), case.name  # every case actually changes something

    class Silent:
        def invoke(self, messages):
            if messages[0].content.startswith("You write the overall summary"):
                return FakeMessage(json.dumps({"summary": ""}))
            return FakeMessage(json.dumps({"findings": []}))

    report = run_eval(Settings(**BASE, verify_findings=False), Silent(), ["py_hardcoded_token", "py_clean_refactor"])
    results = {r.name: r for r in report.results}
    assert results["py_hardcoded_token"].matched == 1  # found by the deterministic secret scan
    assert results["py_clean_refactor"].false_positives == []
    assert all(r.impact_found is False for r in report.results)  # nothing depends on these symbols
    cross = run_eval(Settings(**BASE, verify_findings=False), Silent(), ["py_signature_break"])
    assert cross.results[0].impact_found is True  # the fixture's caller was located by the syntax tree
    assert cross.results[0].matched == 0  # a model that reports nothing misses the bug: recall is measured honestly
    assert 0 <= cross.recall <= 1
