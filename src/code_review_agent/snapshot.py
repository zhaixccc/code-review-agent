"""Repository snapshots: download the code at the reviewed commit and extract it safely for static analysis.

Only source files are extracted; nothing from the archive is ever executed. Extraction is defensive against
path traversal, links, device files and archive bombs (file count, per-file size and total size limits).
"""

from __future__ import annotations

import logging
import os
import shutil
import tarfile
import threading
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from .code_index import SOURCE_SUFFIXES
from .config import Settings
from .github_client import GitHubError, validate_repo, validate_sha

logger = logging.getLogger(__name__)

_SKIP_DIRS = {"node_modules", "vendor", "dist", ".git", "__pycache__", "venv", ".venv", "third_party", ".next", "site-packages"}
_COMPLETE = ".snapshot-complete"


class SnapshotProvider(Protocol):
    def get(self, repo: str, sha: str) -> Path | None: ...


def extract_source_archive(
    archive: Path, destination: Path, *, max_files: int = 20_000, max_file_bytes: int = 300_000, max_total_bytes: int = 200_000_000
) -> int:
    """Extract only regular source files from a .tar.gz into ``destination``. Returns the number of files written."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    written = 0
    total = 0
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            if not member.isfile():  # directories, symlinks, hard links, devices, fifos
                continue
            parts = PurePosixPath(member.name).parts[1:]  # drop the "<owner>-<repo>-<sha>/" prefix
            if not parts or any(part in {"", ".", ".."} or ":" in part or "\\" in part for part in parts):
                continue
            relative = PurePosixPath(*parts)
            if relative.suffix.lower() not in SOURCE_SUFFIXES or any(part in _SKIP_DIRS for part in parts[:-1]):
                continue
            if member.size > max_file_bytes:
                continue
            if written >= max_files or total + member.size > max_total_bytes:
                logger.warning("Snapshot limit reached; remaining files are not extracted.")
                break
            stream = tar.extractfile(member)
            if stream is None:
                continue
            data = stream.read(max_file_bytes + 1)
            if len(data) > max_file_bytes:
                continue
            target = destination.joinpath(*parts)
            try:
                if not target.resolve().is_relative_to(root):
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            except OSError:
                continue  # e.g. Windows path length or reserved names: skip the single file
            written += 1
            total += len(data)
    return written


class GitHubSnapshotProvider:
    """Downloads ``/tarball/<sha>`` once per commit and keeps the newest few snapshots on disk."""

    def __init__(self, github: Any, cache_dir: Path, *, max_download_mb: int = 80, keep: int = 6) -> None:
        self._github = github
        self._dir = Path(cache_dir)
        self._max_bytes = max_download_mb * 1024 * 1024
        self._keep = max(1, keep)
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def get(self, repo: str, sha: str) -> Path | None:
        key = f"{validate_repo(repo).replace('/', '--')}@{validate_sha(sha).lower()}"
        target = self._dir / key
        with self._lock_for(key):
            if (target / _COMPLETE).exists():
                try:
                    os.utime(target / _COMPLETE)
                except OSError:
                    pass
                return target
            scratch = self._dir / f".tmp-{uuid.uuid4().hex[:12]}"
            try:
                scratch.mkdir(parents=True, exist_ok=True)
                archive = scratch / "archive.tar.gz"
                self._github.download_archive(repo, sha, archive, self._max_bytes)
                source = scratch / "src"
                count = extract_source_archive(archive, source)
                (source / _COMPLETE).write_text(str(count), encoding="utf-8")
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                os.replace(source, target)
            except (GitHubError, tarfile.TarError, OSError, EOFError, ValueError) as error:
                logger.warning("Could not build a snapshot of %s@%s (%s).", repo, sha[:7], type(error).__name__)
                return None
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
        self._evict()
        return target

    def _evict(self) -> None:
        try:
            snapshots = [p for p in self._dir.iterdir() if p.is_dir() and (p / _COMPLETE).exists()]
            snapshots.sort(key=lambda p: (p / _COMPLETE).stat().st_mtime, reverse=True)
            for old in snapshots[self._keep :]:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass


def build_snapshots(settings: Settings, github: Any) -> GitHubSnapshotProvider | None:
    if not settings.impact_enabled:
        return None
    return GitHubSnapshotProvider(
        github, settings.state_dir / "snapshots", max_download_mb=settings.snapshot_max_mb, keep=settings.snapshot_keep
    )


__all__ = ["GitHubSnapshotProvider", "SnapshotProvider", "build_snapshots", "extract_source_archive"]
