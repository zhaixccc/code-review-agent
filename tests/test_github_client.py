"""GitHub client behaviour against a mock HTTP transport (no network)."""

import base64
import json

import httpx
import pytest

from code_review_agent.github_client import GitHubClient, GitHubError, validate_repo_path

MARKER = "<!-- code-review-agent -->"


def make_client(handler):
    transport = httpx.MockTransport(handler)
    http = httpx.Client(base_url="https://api.github.test", transport=transport, headers={"Authorization": "Bearer t"})
    return GitHubClient(token="t", client=http)


def test_get_commit_paginates_files_and_reads_the_message_once():
    pages = []

    def handler(request):
        page = int(request.url.params["page"])
        pages.append((page, request.url.params["per_page"]))
        count = {1: 100, 2: 100, 3: 7}.get(page, 0)
        return httpx.Response(200, json={
            "commit": {"message": "big change"},
            "files": [{"filename": f"f{page}_{i}.py", "patch": "@@"} for i in range(count)],
        })

    message, files = make_client(handler).get_commit("o/r", "a" * 40, max_pages=5)
    assert message == "big change" and len(files) == 207
    assert pages == [(1, "100"), (2, "100"), (3, "100")]


def test_get_commit_respects_the_page_cap():
    def handler(request):
        return httpx.Response(200, json={"commit": {"message": "m"}, "files": [{"filename": "x.py"}] * 100})

    _, files = make_client(handler).get_commit("o/r", "a" * 40, max_pages=2)
    assert len(files) == 200


def test_upsert_commit_comment_updates_only_our_own_marked_comment():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "me"})
        if request.method == "GET":
            return httpx.Response(200, json=[
                {"id": 1, "body": f"{MARKER} spoof", "user": {"login": "someone-else"}},
                {"id": 2, "body": f"{MARKER} mine", "user": {"login": "Me"}},
            ])
        return httpx.Response(200, json={})

    result = make_client(handler).upsert_commit_comment("o/r", "a" * 40, "new body", MARKER)
    assert result == "updated"
    assert ("PATCH", "/repos/o/r/comments/2") in calls  # never the spoofed comment 1


def test_upsert_creates_when_no_own_comment_exists():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "me"})
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": 1, "body": "unrelated", "user": {"login": "me"}}])
        return httpx.Response(201, json={})

    assert make_client(handler).upsert_issue_comment("o/r", 7, "body", MARKER) == "created"
    assert ("POST", "/repos/o/r/issues/7/comments") in calls


def test_inline_review_rejection_returns_false_instead_of_raising():
    def handler(request):
        return httpx.Response(422, json={"message": "Unprocessable"})

    assert make_client(handler).post_pull_request_review("o/r", 1, "a" * 40, "b", [{"path": "x", "line": 1, "side": "RIGHT", "body": "c"}]) is False


def test_other_http_errors_do_not_leak_response_bodies():
    def handler(request):
        return httpx.Response(500, json={"message": "secret internal detail"})

    with pytest.raises(GitHubError) as error:
        make_client(handler).get_pull_request_title("o/r", 1)
    assert "secret internal detail" not in str(error.value) and error.value.status_code == 500


def test_get_repository_file_decodes_content_and_handles_missing_and_large_files():
    def handler(request):
        if request.url.path.endswith("AGENTS.md"):
            return httpx.Response(200, json={"type": "file", "encoding": "base64", "size": 10, "content": base64.b64encode("规则".encode()).decode()})
        if request.url.path.endswith("HUGE.md"):
            return httpx.Response(200, json={"type": "file", "encoding": "base64", "size": 10_000_000, "content": ""})
        return httpx.Response(404, json={})

    client = make_client(handler)
    assert client.get_repository_file("o/r", "AGENTS.md") == "规则"
    assert client.get_repository_file("o/r", "NOPE.md") is None
    assert client.get_repository_file("o/r", "HUGE.md") is None


@pytest.mark.parametrize("path", ["../etc/passwd", "/abs", "a/../b", "bad path", "x" * 300])
def test_repository_paths_are_validated(path):
    with pytest.raises(ValueError):
        validate_repo_path(path)


def test_requests_carry_the_pagination_parameters_as_json_safe_types():
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=[])

    make_client(handler).list_commit_comments("o/r")
    assert seen == {"per_page": "100", "page": "1"}
    assert json.dumps(seen)
