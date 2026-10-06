"""Minimal GitHub REST client (read commits / PR files, write review comments, read maintainer feedback)."""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from .models import ChangedFile

logger = logging.getLogger(__name__)

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_.@/-]{1,200}$")
MAX_COMMENT_CHARS = 60_000
MAX_INLINE_REVIEW_COMMENTS = 50
MAX_FILE_BYTES = 200_000


class GitHubError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def validate_repo(repo: str) -> str:
    # "../x" satisfies the character class but would escape the /repos/ URL prefix: dot-only segments are never valid.
    if not _REPO_RE.match(repo) or any(set(part) == {"."} for part in repo.split("/")):
        raise ValueError("repo must look like 'owner/name'.")
    return repo


def validate_sha(sha: str) -> str:
    if not _SHA_RE.match(sha):
        raise ValueError("sha must be a hexadecimal commit id.")
    return sha


def validate_repo_path(path: str) -> str:
    if not _SAFE_PATH_RE.match(path) or ".." in path.split("/") or path.startswith("/"):
        raise ValueError("invalid repository path")
    return path


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
        self._login: str | None = None

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

    # --- identity ---------------------------------------------------------------
    def authenticated_login(self) -> str:
        if self._login is None:
            self._login = str(self._request("GET", "/user").get("login", ""))
        return self._login

    # --- reading changes --------------------------------------------------------
    def get_commit(self, repo: str, sha: str, max_pages: int = 5) -> tuple[str, list[ChangedFile]]:
        """Return (commit message, changed files); the files list is paginated by GitHub for large commits."""
        path = f"/repos/{validate_repo(repo)}/commits/{validate_sha(sha)}"
        message = ""
        files: list[ChangedFile] = []
        for page in range(1, max(1, max_pages) + 1):
            data = self._request("GET", path, params={"per_page": 100, "page": page})
            if page == 1:
                message = str((data.get("commit") or {}).get("message") or "")
            batch = data.get("files") or []
            files.extend(self._file(item) for item in batch)
            if len(batch) < 100:
                break
        return message, files

    def get_pull_request_files(self, repo: str, number: int, max_pages: int = 5) -> list[ChangedFile]:
        files: list[ChangedFile] = []
        for page in range(1, max(1, max_pages) + 1):
            batch = self._request("GET", f"/repos/{validate_repo(repo)}/pulls/{int(number)}/files", params={"per_page": 100, "page": page})
            files.extend(self._file(item) for item in batch or [])
            if not batch or len(batch) < 100:
                break
        return files

    def get_pull_request_title(self, repo: str, number: int) -> str:
        data = self._request("GET", f"/repos/{validate_repo(repo)}/pulls/{int(number)}")
        return f"{data.get('title', '')}\n\n{data.get('body') or ''}".strip()

    def get_repository_file(self, repo: str, path: str) -> str | None:
        """Read a text file from the repository's DEFAULT branch (never from a PR head)."""
        try:
            data = self._request("GET", f"/repos/{validate_repo(repo)}/contents/{validate_repo_path(path)}")
        except GitHubError as error:
            if error.status_code == 404:
                return None
            raise
        if not isinstance(data, dict) or data.get("type") != "file" or data.get("encoding") != "base64":
            return None
        if int(data.get("size", 0) or 0) > MAX_FILE_BYTES:
            return None
        return base64.b64decode(data.get("content", "")).decode("utf-8", "replace")

    # --- writing comments -------------------------------------------------------
    def download_archive(self, repo: str, sha: str, destination: Path, max_bytes: int) -> int:
        """Stream the repository tarball at a commit to ``destination`` (aborts when it exceeds ``max_bytes``)."""
        path = f"/repos/{validate_repo(repo)}/tarball/{validate_sha(sha)}"
        size = 0
        try:
            # The API redirects to codeload; httpx drops the Authorization header on cross-origin redirects.
            with self._http.stream("GET", path, follow_redirects=True) as response:
                if response.status_code >= 400:
                    raise GitHubError(f"GitHub API returned HTTP {response.status_code} for GET {path}", response.status_code)
                with open(destination, "wb") as stream:
                    for chunk in response.iter_bytes(65_536):
                        size += len(chunk)
                        if size > max_bytes:
                            raise GitHubError("repository archive is larger than the configured limit")
                        stream.write(chunk)
        except httpx.HTTPError as error:
            raise GitHubError(f"GitHub archive download failed: {type(error).__name__}") from error
        return size

    # --- writing comments -------------------------------------------------------
    def upsert_commit_comment(self, repo: str, sha: str, body: str, marker: str) -> str:
        """Update the agent's existing comment on this commit, or create one. Returns 'updated' or 'created'."""
        self._require_token()
        base = f"/repos/{validate_repo(repo)}/commits/{validate_sha(sha)}/comments"
        existing = self._own_comment(self._list(base, max_pages=3), marker)
        if existing is not None:
            self._request("PATCH", f"/repos/{validate_repo(repo)}/comments/{int(existing['id'])}", json={"body": body[:MAX_COMMENT_CHARS]})
            return "updated"
        self._request("POST", base, json={"body": body[:MAX_COMMENT_CHARS]})
        return "created"

    def upsert_issue_comment(self, repo: str, number: int, body: str, marker: str) -> str:
        self._require_token()
        base = f"/repos/{validate_repo(repo)}/issues/{int(number)}/comments"
        existing = self._own_comment(self._list(base, max_pages=5), marker)
        if existing is not None:
            self._request("PATCH", f"/repos/{validate_repo(repo)}/issues/comments/{int(existing['id'])}", json={"body": body[:MAX_COMMENT_CHARS]})
            return "updated"
        self._request("POST", base, json={"body": body[:MAX_COMMENT_CHARS]})
        return "created"

    def list_pull_request_review_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self._list(f"/repos/{validate_repo(repo)}/pulls/{int(number)}/comments", max_pages=5)

    def post_pull_request_review(self, repo: str, number: int, sha: str, body: str, inline: list[dict[str, Any]]) -> bool:
        """Post a COMMENT review with inline comments. Returns False if GitHub rejects the anchors (HTTP 422)."""
        self._require_token()
        if len(inline) > MAX_INLINE_REVIEW_COMMENTS:
            logger.warning(
                "Inline review limit reached: sending %d of %d comment(s); caller must retain the rest in the summary.",
                MAX_INLINE_REVIEW_COMMENTS,
                len(inline),
            )
        payload: dict[str, Any] = {
            "commit_id": validate_sha(sha),
            "body": body[:MAX_COMMENT_CHARS],
            "event": "COMMENT",
            "comments": inline[:MAX_INLINE_REVIEW_COMMENTS],
        }
        try:
            self._request("POST", f"/repos/{validate_repo(repo)}/pulls/{int(number)}/reviews", json=payload)
        except GitHubError as error:
            if error.status_code == 422:
                logger.warning("Inline review comments were rejected by GitHub.")
                return False
            raise
        return True

    # --- reading maintainer feedback -------------------------------------------
    def list_commit_comments(self, repo: str, max_pages: int = 3) -> list[dict[str, Any]]:
        return self._list(f"/repos/{validate_repo(repo)}/comments", max_pages=max_pages)

    def list_repo_review_comments(self, repo: str, max_pages: int = 3) -> list[dict[str, Any]]:
        return self._list(f"/repos/{validate_repo(repo)}/pulls/comments", params={"sort": "created", "direction": "desc"}, max_pages=max_pages)

    def list_reactions(self, repo: str, kind: str, comment_id: int) -> list[dict[str, Any]]:
        prefix = {"commit": "comments", "review": "pulls/comments", "issue": "issues/comments"}[kind]
        return self._list(f"/repos/{validate_repo(repo)}/{prefix}/{int(comment_id)}/reactions", max_pages=2)

    def repository_owner(self, repo: str) -> str:
        return str((self._request("GET", f"/repos/{validate_repo(repo)}").get("owner") or {}).get("login", ""))

    # --- helpers ---------------------------------------------------------------
    def _list(self, path: str, params: dict[str, Any] | None = None, max_pages: int = 3) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            batch = self._request("GET", path, params={**(params or {}), "per_page": 100, "page": page})
            items.extend(batch or [])
            if not batch or len(batch) < 100:
                break
        return items

    def _own_comment(self, comments: list[dict[str, Any]], marker: str) -> dict[str, Any] | None:
        me = self.authenticated_login().lower()
        for comment in comments:
            author = str((comment.get("user") or {}).get("login", "")).lower()
            if marker in str(comment.get("body", "")) and author == me:
                return comment
        return None

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
