"""Filesystem HTTP cache with TTL.

Used to avoid hammering RSS servers (default 6h TTL per the brief).
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path


class FileCache:
    """Simple key -> bytes cache rooted at ``base_dir``.

    Keys are hashed (SHA1) so any URL is safe as a filename.
    """

    def __init__(self, base_dir: str | Path, ttl_seconds: int = 6 * 3600) -> None:
        self.base_dir = Path(base_dir)
        self.ttl_seconds = ttl_seconds
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return self.base_dir / digest

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        if not path.exists():
            return None
        age = time.time() - path.stat().st_mtime
        if age > self.ttl_seconds:
            return None
        return path.read_bytes()

    def set(self, key: str, value: bytes) -> None:
        self._path(key).write_bytes(value)
