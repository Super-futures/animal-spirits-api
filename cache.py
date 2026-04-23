"""
File-backed cache.

Because this script runs as a one-shot via GitHub Actions (not a long-running
server), an in-memory cache dies with the process. Instead we persist to a
small JSON file committed alongside state.json.

This is actually better for z-score normalisation: the baseline history
survives across runs, so the first run after deployment isn't cold.
"""

import json
import time
from pathlib import Path
from typing import Any, Optional

CACHE_PATH = Path(__file__).parent / "data" / "cache.json"


class FileCache:
    def __init__(self, path: Path = CACHE_PATH):
        self.path = path
        self._store: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._store = json.loads(self.path.read_text())
            except Exception:
                self._store = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._store, indent=2))

    def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if entry is None:
            return None
        if time.time() > entry["expires_at"]:
            return None
        return entry["value"]

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        self._store[key] = {
            "value": value,
            "expires_at": time.time() + ttl_seconds,
        }

    def keys(self) -> list[str]:
        return [k for k, v in self._store.items() if time.time() <= v["expires_at"]]

    def flush(self) -> None:
        """Persist to disk. Call at end of run."""
        # Prune expired entries before saving to keep file small.
        now = time.time()
        self._store = {k: v for k, v in self._store.items() if v["expires_at"] > now}
        self._save()


cache = FileCache()
