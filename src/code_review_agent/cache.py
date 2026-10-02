"""Cache of per-file model results, so re-reviewing unchanged code (PR updates, webhook redeliveries) costs nothing.

The key covers everything that influences the model output: model, prompt version, language, file, diff text,
commit message, recalled memory and impact evidence. Only successful, complete results are cached.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .state import StateStore

_NAMESPACE = "file_reviews"
_MAX_ENTRIES = 1500


class ReviewCache:
    def __init__(self, directory: Path, max_entries: int = _MAX_ENTRIES) -> None:
        self._store = StateStore(Path(directory) / "review_cache")
        self._max = max_entries
        self._puts = 0

    @staticmethod
    def key(*parts: str) -> str:
        digest = hashlib.sha256()
        for part in parts:
            digest.update(part.encode("utf-8", "replace"))
            digest.update(b"\x1f")
        return digest.hexdigest()

    def get(self, key: str) -> list[dict[str, Any]] | None:
        entry = self._store.get(_NAMESPACE, key)
        findings = entry.get("f") if isinstance(entry, dict) else None
        return findings if isinstance(findings, list) else None

    def put(self, key: str, findings: list[dict[str, Any]]) -> None:
        self._store.put(_NAMESPACE, key, {"t": time.time(), "f": findings})
        self._puts += 1
        if self._puts % 25 == 0:
            self._store.trim(_NAMESPACE, self._max)


def build_cache(settings: Settings) -> ReviewCache | None:
    return ReviewCache(settings.state_dir) if settings.review_cache else None
