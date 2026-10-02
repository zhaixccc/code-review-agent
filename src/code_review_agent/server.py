"""GitHub webhook receiver (FastAPI). Verifies the HMAC signature, then reviews in the background."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from collections import OrderedDict
from typing import Any, Callable

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from .config import Settings
from .github_client import GitHubClient
from .graph import build_graph, run_review
from .llm import make_llm
from .models import ReviewTarget

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 5 * 1024 * 1024
PR_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}
_ZERO_SHA = "0" * 40


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def parse_event(event: str, payload: dict[str, Any], settings: Settings) -> list[ReviewTarget]:
    """Turn a webhook payload into review targets; return [] for anything that should be ignored."""
    if event not in settings.review_events:
        return []
    repo = str((payload.get("repository") or {}).get("full_name") or "")
    if not repo or not settings.is_repo_allowed(repo):
        return []
    if str((payload.get("sender") or {}).get("type")) == "Bot":
        return []  # avoid review loops and noise from bots (dependabot, etc.)

    if event == "push":
        ref = str(payload.get("ref") or "")
        if payload.get("deleted") or not ref.startswith("refs/heads/") or str(payload.get("after")) == _ZERO_SHA:
            return []
        commits = [
            commit for commit in payload.get("commits") or []
            if commit.get("distinct", True) and not str(commit.get("message", "")).startswith("Merge ")
        ]
        return [ReviewTarget(repo=repo, sha=str(commit["id"])) for commit in commits[-settings.max_commits_per_push:]]

    if event == "pull_request":
        pull = payload.get("pull_request") or {}
        if payload.get("action") not in PR_ACTIONS or pull.get("draft"):
            return []
        return [ReviewTarget(repo=repo, sha=str((pull.get("head") or {})["sha"]), pr_number=int(pull["number"]))]
    return []


class _Deduplicator:
    """GitHub retries deliveries; remember recent targets so the same commit is reviewed once."""

    def __init__(self, capacity: int = 2000) -> None:
        self._seen: OrderedDict[tuple[str, str, int | None], None] = OrderedDict()
        self._capacity = capacity
        self._lock = threading.Lock()

    def first_time(self, target: ReviewTarget) -> bool:
        key = (target.repo.lower(), target.sha.lower(), target.pr_number)
        with self._lock:
            if key in self._seen:
                return False
            self._seen[key] = None
            while len(self._seen) > self._capacity:
                self._seen.popitem(last=False)
            return True

    def forget(self, target: ReviewTarget) -> None:
        with self._lock:
            self._seen.pop((target.repo.lower(), target.sha.lower(), target.pr_number), None)


def create_app(settings: Settings, graph: Any | None = None, max_parallel_reviews: int = 2) -> FastAPI:
    settings.require_webhook()
    app = FastAPI(title="code-review-agent", docs_url=None, redoc_url=None, openapi_url=None)
    github = GitHubClient(settings.github_token, settings.github_api_url)
    compiled = graph or build_graph(settings, github, make_llm(settings))
    dedupe = _Deduplicator()
    slots = threading.BoundedSemaphore(max_parallel_reviews)

    def review(target: ReviewTarget) -> None:
        with slots:
            try:
                result = run_review(compiled, target, settings)
                logger.info(
                    "Reviewed %s@%s verdict=%s posted=%s errors=%d",
                    target.repo, target.sha[:7], result.get("verdict"), result.get("posted"), len(result.get("errors", [])),
                )
                if not result.get("posted") and result.get("errors"):
                    dedupe.forget(target)  # allow a redelivery to retry after a failure
            except Exception:
                logger.exception("Review failed for %s@%s", target.repo, target.sha[:7])
                dedupe.forget(target)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhook/github", status_code=202)
    async def webhook(
        request: Request,
        background: BackgroundTasks,
        x_hub_signature_256: str | None = Header(default=None),
        x_github_event: str | None = Header(default=None),
    ) -> dict[str, Any]:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Payload too large")
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Payload too large")
        if not verify_signature(settings.github_webhook_secret, body, x_hub_signature_256):
            raise HTTPException(status_code=401, detail="Invalid signature")
        if x_github_event == "ping":
            return {"status": "pong"}
        try:
            payload = json.loads(body)
            targets = parse_event(x_github_event or "", payload, settings)
        except (ValueError, KeyError, TypeError):
            raise HTTPException(status_code=400, detail="Malformed payload") from None
        queued = [target for target in targets if dedupe.first_time(target)]
        for target in queued:
            background.add_task(review, target)
        return {"status": "queued" if queued else "ignored", "reviews": len(queued)}

    app.state.dedupe = dedupe
    return app


def app_factory() -> FastAPI:
    """uvicorn entry point: ``uvicorn code_review_agent.server:app_factory --factory``."""
    from .config import load_settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "httpcore", "langchain_openai", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return create_app(load_settings())
