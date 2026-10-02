"""GitHub webhook receiver (FastAPI). Verifies the HMAC signature, then reviews in the background."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from .config import Settings
from .github_client import GitHubClient
from .graph import build_graph, run_review
from .llm import make_llm
from .memory import build_memory
from .models import ReviewTarget
from .state import StateStore

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


def review_key(target: ReviewTarget) -> str:
    return f"{target.repo.lower()}@{target.sha.lower()}#{target.pr_number or 0}"


def create_app(
    settings: Settings, graph: Any | None = None, max_parallel_reviews: int = 2, state: StateStore | None = None
) -> FastAPI:
    settings.require_webhook()
    app = FastAPI(title="code-review-agent", docs_url=None, redoc_url=None, openapi_url=None)
    store = state or StateStore(settings.state_dir)
    if graph is None:
        github = GitHubClient(settings.github_token, settings.github_api_url)
        graph = build_graph(settings, github, make_llm(settings), memory=build_memory(settings, store))
    compiled = graph
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
                    store.release_review(review_key(target))  # allow a redelivery to retry after a failure
            except Exception:
                logger.exception("Review failed for %s@%s", target.repo, target.sha[:7])
                store.release_review(review_key(target))

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
        queued = [target for target in targets if store.claim_review(review_key(target))]
        for target in queued:
            background.add_task(review, target)
        return {"status": "queued" if queued else "ignored", "reviews": len(queued)}

    app.state.store = store
    return app


def app_factory() -> FastAPI:
    """uvicorn entry point: ``uvicorn code_review_agent.server:app_factory --factory``."""
    from .config import load_settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "httpcore", "langchain_openai", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return create_app(load_settings())
