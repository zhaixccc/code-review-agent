"""Small JSON state file (atomic writes, thread-safe) so restarts do not forget what was already done."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_REVIEWED = 5000


class StateStore:
    def __init__(self, directory: Path) -> None:
        self._path = Path(directory) / "state.json"
        self._lock = threading.RLock()
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handle, temp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(self._data, stream, ensure_ascii=False)
            os.replace(temp, self._path)
        except OSError as error:
            logger.warning("Could not persist state: %s", type(error).__name__)

    # --- review de-duplication -------------------------------------------------
    def claim_review(self, key: str) -> bool:
        """Return True the first time a review key is seen (and remember it)."""
        with self._lock:
            reviewed: dict[str, float] = self._data.setdefault("reviewed", {})
            if key in reviewed:
                return False
            reviewed[key] = time.time()
            if len(reviewed) > MAX_REVIEWED:
                for old in sorted(reviewed, key=reviewed.get)[: len(reviewed) - MAX_REVIEWED]:
                    del reviewed[old]
            self._save()
            return True

    def release_review(self, key: str) -> None:
        with self._lock:
            if self._data.setdefault("reviewed", {}).pop(key, None) is not None:
                self._save()

    # --- generic key/value namespaces -----------------------------------------
    def get(self, namespace: str, key: str) -> Any:
        with self._lock:
            return self._data.get(namespace, {}).get(key)

    def put(self, namespace: str, key: str, value: Any) -> None:
        with self._lock:
            self._data.setdefault(namespace, {})[key] = value
            self._save()
