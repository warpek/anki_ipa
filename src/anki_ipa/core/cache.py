"""JSON file cache of source lookups, keyed by (source, query).

Positive results never expire; negative results (value None) expire after
`negative_ttl` seconds so words added to a dictionary later get retried.
Writes are atomic (temp file + `os.replace`). The caller resolves the path
(normally `<addon>/user_files/ipa_cache.json`, which survives upgrades).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

NEGATIVE_TTL = 30 * 24 * 3600.0
_VERSION = 1
_SEP = "\t"


@dataclass(frozen=True)
class CacheEntry:
    value: Any  # JSON value; None = negative result
    timestamp: float

    @property
    def is_negative(self) -> bool:
        return self.value is None


class Cache:
    def __init__(
        self,
        path: str | os.PathLike[str] | None,
        *,
        negative_ttl: float = NEGATIVE_TTL,
        autosave_every: int = 25,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """`path=None` gives an in-memory cache (never written)."""
        self.path = Path(path) if path is not None else None
        self.negative_ttl = negative_ttl
        self.autosave_every = autosave_every
        self._clock = clock
        self._lock = threading.RLock()
        self._data: dict[str, list[Any]] = {}  # key -> [timestamp, value]
        self._dirty = 0
        self._load()

    @staticmethod
    def _key(source: str, query: str) -> str:
        return f"{source}{_SEP}{query}"

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            entries = raw.get("entries", {}) if isinstance(raw, dict) else {}
            self._data = {
                k: v for k, v in entries.items() if isinstance(v, list) and len(v) == 2
            }
        except (OSError, ValueError):
            self._data = {}  # unreadable cache: start fresh, overwrite on save

    def get(self, source: str, query: str) -> CacheEntry | None:
        """The entry, or None if absent or an expired negative result."""
        with self._lock:
            item = self._data.get(self._key(source, query))
        if item is None:
            return None
        ts, value = float(item[0]), item[1]
        if value is None and self._clock() - ts > self.negative_ttl:
            return None
        return CacheEntry(value, ts)

    def put(self, source: str, query: str, value: Any) -> None:
        with self._lock:
            self._data[self._key(source, query)] = [self._clock(), value]
            self._dirty += 1
            if self.autosave_every and self._dirty >= self.autosave_every:
                self.save()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def save(self) -> None:
        """Atomically write the cache (no-op for in-memory or unchanged caches)."""
        with self._lock:
            if self.path is None or not self._dirty:
                return
            payload = json.dumps(
                {"version": _VERSION, "entries": self._data},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            self._dirty = 0

    flush = save
