import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from code_review_agent.config import Settings
from code_review_agent.server import create_app, parse_event, verify_signature
from code_review_agent.state import StateStore

SECRET = "x" * 32
SETTINGS = Settings(
    deepseek_api_key="k", github_token="t", github_webhook_secret=SECRET,
    allowed_repos=("Owner/Repo",), review_events=("push",), max_commits_per_push=2,
)


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def push_payload(**overrides):
    payload = {
        "ref": "refs/heads/main",
        "after": "c" * 40,
        "deleted": False,
        "repository": {"full_name": "owner/repo"},
        "sender": {"type": "User"},
        "commits": [
            {"id": "1" * 40, "message": "one", "distinct": True},
            {"id": "2" * 40, "message": "Merge branch 'x'", "distinct": True},
            {"id": "3" * 40, "message": "three", "distinct": True},
            {"id": "4" * 40, "message": "four", "distinct": False},
            {"id": "5" * 40, "message": "five", "distinct": True},
        ],
    }
    payload.update(overrides)
    return payload


def test_verify_signature():
    body = b'{"a":1}'
    assert verify_signature(SECRET, body, sign(body))
    assert not verify_signature(SECRET, body, sign(body, "other" * 8))
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature(SECRET, body, "sha1=abc")
    assert not verify_signature("", body, sign(body, ""))


def test_parse_push_skips_merge_and_non_distinct_and_caps_latest_commits():
    targets = parse_event("push", push_payload(), SETTINGS)
    assert [t.sha[0] for t in targets] == ["3", "5"]


def test_parse_push_ignores_bots_deleted_tags_other_repos_and_other_events():
    assert parse_event("push", push_payload(sender={"type": "Bot"}), SETTINGS) == []
    assert parse_event("push", push_payload(deleted=True), SETTINGS) == []
    assert parse_event("push", push_payload(ref="refs/tags/v1"), SETTINGS) == []
    assert parse_event("push", push_payload(repository={"full_name": "evil/repo"}), SETTINGS) == []
    assert parse_event("pull_request", {}, SETTINGS) == []  # not enabled in REVIEW_EVENTS


def test_parse_pull_request():
    settings = Settings(review_events=("pull_request",))
    payload = {
        "action": "synchronize",
        "repository": {"full_name": "o/r"},
        "sender": {"type": "User"},
        "pull_request": {"number": 9, "draft": False, "head": {"sha": "d" * 40}},
    }
    (target,) = parse_event("pull_request", payload, settings)
    assert (target.repo, target.sha, target.pr_number) == ("o/r", "d" * 40, 9)
    payload["pull_request"]["draft"] = True
    assert parse_event("pull_request", payload, settings) == []
    payload["pull_request"]["draft"] = False
    payload["action"] = "closed"
    assert parse_event("pull_request", payload, settings) == []


class FakeGraph:
    def __init__(self):
        self.targets = []

    def invoke(self, state, config=None):
        self.targets.append(state["target"])
        return {"verdict": "approve", "posted": True, "errors": []}


def post(client, payload, event="push", secret=SECRET):
    body = json.dumps(payload).encode()
    return client.post(
        "/webhook/github", content=body,
        headers={"X-Hub-Signature-256": sign(body, secret), "X-GitHub-Event": event, "Content-Type": "application/json"},
    )


def test_webhook_flow(tmp_path):
    graph = FakeGraph()
    client = TestClient(create_app(SETTINGS, graph=graph, state=StateStore(tmp_path)))

    assert client.get("/healthz").json() == {"status": "ok"}
    assert post(client, {}, event="ping").json() == {"status": "pong"}

    bad = post(client, push_payload(), secret="y" * 32)
    assert bad.status_code == 401 and graph.targets == []

    ok = post(client, push_payload())
    assert ok.status_code == 202 and ok.json() == {"status": "queued", "reviews": 2}
    assert [t["sha"][0] for t in graph.targets] == ["3", "5"]

    again = post(client, push_payload())  # redelivery of the same commits
    assert again.json()["status"] == "ignored" and len(graph.targets) == 2


def test_deduplication_survives_a_restart(tmp_path):
    first = FakeGraph()
    post(TestClient(create_app(SETTINGS, graph=first, state=StateStore(tmp_path))), push_payload())
    assert len(first.targets) == 2

    second = FakeGraph()  # a brand-new app and state object, same state directory
    response = post(TestClient(create_app(SETTINGS, graph=second, state=StateStore(tmp_path))), push_payload())
    assert response.json()["status"] == "ignored" and second.targets == []


def test_webhook_rejects_malformed_payload_and_ignores_irrelevant_events(tmp_path):
    client = TestClient(create_app(SETTINGS, graph=FakeGraph(), state=StateStore(tmp_path)))
    body = b"not json"
    response = client.post("/webhook/github", content=body, headers={"X-Hub-Signature-256": sign(body), "X-GitHub-Event": "push"})
    assert response.status_code == 400
    assert post(client, {"zen": "x"}, event="issues").json()["status"] == "ignored"


def test_app_requires_webhook_secret(tmp_path):
    with pytest.raises(RuntimeError):
        create_app(Settings(deepseek_api_key="k", github_token="t", github_webhook_secret="short"), graph=FakeGraph(), state=StateStore(tmp_path))
