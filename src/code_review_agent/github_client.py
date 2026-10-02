"""Minimal GitHub REST client (read commits / PR files, write review comments)."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from .models import ChangedFile

logger = logging.getLogger(__name__)

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
MAX_COMMENT_CHARS = 60_000


class GitHubError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def validate_repo(repo: str) -> str:
    if not _REPO_RE.match(repo):
        raise ValueError("repo must look like 'owner/name'.")
    return repo


def validate_sha(sha: str) -> str:
    if not _SHA_RE.match(sha):
        raise ValueError("sha must be a hexadecimal commit id.")
    return sha


class GitHubClient:
    def __init__(self, token: str = "", api_url: str = "https://api.github.com", client: httpx.Client | None = None) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "code-review-agent",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = client or httpx.Client(base_url=api_url, headers=headers, timeout=30.0)
        self._has_token = bool(token)

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            raise GitHubError(f"GitHub request failed: {type(error).__name__}") from error
        if response.status_code >= 400:
            # Never echo response bodies verbatim into logs/comments; they can contain request details.
            raise GitHubError(f"GitHub API returned HTTP {response.status_code} for {method} {path.split('?')[0]}", response.status_code)
        return response.json() if response.content else None

    def get_commit(self, repo: str, sha: str) -> tuple[str, list[ChangedFile]]:
        """Return (commit message, changed files) for a commit."""
        data = self._request("GET", f"/repos/{validate_repo(repo)}/commits/{validate_sha(sha)}")
        message = str((data.get("commit") or {}).get("message") or "")
        return message, [self._file(item) for item in data.get("files") or []]

    def get_pull_request_files(self, repo: str, number: int, max_pages: int = 3) -> list[ChangedFile]:
        files: list[ChangedFile] = []
        for page in range(1, max_pages + 1):
            batch = self._request("GET", f"/repos/{validate_repo(repo)}/pulls/{int(number)}/files", params={"per_page": 100, "page": page})
            files.extend(self._file(item) for item in batch or [])
            if not batch or len(batch) < 100:
                break
        return files

    def get_pull_request_title(self, repo: str, number: int) -> str:
        data = self._request("GET", f"/repos/{validate_repo(repo)}/pulls/{int(number)}")
        return f"{data.get('title', '')}\n\n{data.get('body') or ''}".strip()

    def post_commit_comment(self, repo: str, sha: str, body: str) -> None:
        self._require_token()
        self._request("POST", f"/repos/{validate_repo(repo)}/commits/{validate_sha(sha)}/comments", json={"body": body[:MAX_COMMENT_CHARS]})

    def post_pull_request_review(
        self,
        repo: str,
        number: int,
        sha: str,
        body: str,
        inline: list[dict[str, Any]] | None = None,
        fallback_body: str | None = None,
    ) -> None:
        """Post a COMMENT review. If inline anchors are rejected (HTTP 422), retry with fallback_body only."""
        self._require_token()
        path = f"/repos/{validate_repo(repo)}/pulls/{int(number)}/reviews"
        payload: dict[str, Any] = {"commit_id": validate_sha(sha), "body": body[:MAX_COMMENT_CHARS], "event": "COMMENT"}
        if inline:
            payload["comments"] = inline[:50]
        try:
            self._request("POST", path, json=payload)
        except GitHubError as error:
            if inline and error.status_code == 422:
                logger.warning("Inline review comments were rejected; posting the summary only.")
                payload.pop("comments", None)
                payload["body"] = (fallback_body or body)[:MAX_COMMENT_CHARS]
                self._request("POST", path, json=payload)
            else:
                raise

    def _require_token(self) -> None:
        if not self._has_token:
            raise GitHubError("A GitHub token is required to post comments.")

    @staticmethod
    def _file(item: dict[str, Any]) -> ChangedFile:
        return ChangedFile(
            filename=str(item.get("filename", "")),
            status=str(item.get("status", "modified")),
            additions=int(item.get("additions", 0) or 0),
            deletions=int(item.get("deletions", 0) or 0),
            patch=item.get("patch"),
        )
