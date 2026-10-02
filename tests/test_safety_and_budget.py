import pytest

from code_review_agent.models import ChangedFile
from code_review_agent.prioritize import risk_score, select_files
from code_review_agent.secrets_guard import (
    is_sensitive_path,
    redact,
    scan_added_lines,
)
from code_review_agent.state import StateStore


@pytest.mark.parametrize(
    "name,expected",
    [
        (".env", True),
        ("config/.env.production", True),
        (".env.example", False),
        ("deploy/server.pem", True),
        ("keys/id_rsa", True),
        ("home/.npmrc", True),
        ("src/app.py", False),
        ("docs/keys.md", False),
        ("terraform.tfstate", True),
        ("credentials.json.sample", False),
    ],
)
def test_is_sensitive_path(name, expected):
    assert is_sensitive_path(name) is expected


def test_redact_removes_known_secret_formats_but_keeps_ordinary_code():
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    text = f"x = 1\nGITHUB = '{token}'\nkey = 'AKIAABCDEFGHIJKLMNOP'\n-----BEGIN RSA PRIVATE KEY-----\nprint('ok')"
    safe, count = redact(text)
    assert count == 3 and token not in safe and "AKIAABCDEFGHIJKLMNOP" not in safe
    assert "BEGIN RSA PRIVATE KEY" not in safe and "x = 1" in safe and "print('ok')" in safe


def test_assignment_rule_ignores_placeholders_and_env_lookups_but_catches_real_values():
    safe_lines = [
        "password = 'changeme-please'",
        "api_key = os.environ['API_KEY_VALUE_X']",
        "token = '${TOKEN}'",
        "secret = 'your_secret_here_1234'",
        "password = 'short'",
    ]
    for line in safe_lines:
        assert redact(line)[1] == 0, line
    assert redact("password = 'Zq8!xK2#mPv9Lw'")[1] == 1


def test_scan_added_lines_reports_line_numbers_and_kind_only():
    annotated = "@@ -1 +1,3 @@\n    1   keep\n    2 + token = 'ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8'\n    3 + print('x')\n      - removed"
    hits = scan_added_lines(annotated)
    assert hits == [(2, "GitHub Token")]


def test_risk_score_prefers_security_sensitive_code_over_docs_and_tests():
    auth = ChangedFile(filename="src/auth/login.py", additions=10, patch="x")
    util = ChangedFile(filename="src/util/strings.py", additions=10, patch="x")
    test = ChangedFile(filename="tests/test_login.py", additions=10, patch="x")
    doc = ChangedFile(filename="docs/guide.md", additions=10, patch="x")
    workflow = ChangedFile(filename=".github/workflows/release.yml", additions=10, patch="x")
    assert risk_score(auth) > risk_score(util) > risk_score(test) > risk_score(doc)
    assert risk_score(workflow) > risk_score(doc)


def test_select_files_respects_file_and_size_budget_and_is_deterministic():
    files = [ChangedFile(filename=f"src/mod{i}.py", additions=5, patch="x" * 1000) for i in range(5)]
    files.append(ChangedFile(filename="src/auth/token.py", additions=5, patch="x" * 1000))
    selected, skipped = select_files(files, max_files=10, max_total_chars=2500, max_file_chars=1000)
    assert selected[0].filename == "src/auth/token.py" and len(selected) == 2
    assert len(skipped) == 4 and all("预算" in reason for reason in skipped)
    again, _ = select_files(list(reversed(files)), max_files=10, max_total_chars=2500, max_file_chars=1000)
    assert [f.filename for f in again] == [f.filename for f in selected]


def test_select_files_always_keeps_one_file_even_if_it_exceeds_the_budget():
    big = ChangedFile(filename="src/big.py", additions=500, patch="x" * 50_000)
    selected, skipped = select_files([big], max_files=5, max_total_chars=1000, max_file_chars=24_000)
    assert [f.filename for f in selected] == ["src/big.py"] and skipped == []


def test_state_store_persists_claims_and_namespaces(tmp_path):
    store = StateStore(tmp_path)
    assert store.claim_review("a") is True and store.claim_review("a") is False
    store.put("conventions", "r:AGENTS.md", "hash1")
    reopened = StateStore(tmp_path)
    assert reopened.claim_review("a") is False and reopened.get("conventions", "r:AGENTS.md") == "hash1"
    reopened.release_review("a")
    assert StateStore(tmp_path).claim_review("a") is True


def test_state_store_survives_a_corrupt_file(tmp_path):
    (tmp_path / "state.json").write_text("{not json", encoding="utf-8")
    assert StateStore(tmp_path).claim_review("x") is True
