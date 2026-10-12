"""JSON file cache of source lookups, keyed by (source, query).

Only *found* results are stored, and they never expire. Misses ("not
found") are never cached: whether a note is looked up again is decided by
the note's `ipa::notfound` tag alone, so removing the tag always gives a
fresh lookup (the resolver remembers misses only for one run). `put(...,
None)` is ignored, and `null` entries in files written by v0.1.0 (which
cached misses) are dropped on load; the file loses them on the next save.
Writes are atomic (temp file + `os.replace`). The caller resolves the path
(normally `<addon>/user_files/ipa_cache.json`, which survives upgrades).

`save()` never raises `OSError` (disk full, permissions, a Windows sharing
violation on `os.replace` while an antivirus or backup tool holds the file):
it logs the problem, records it in `last_error`, removes its own temp file
and keeps the data dirty, so a later save retries. A lookup batch is never
lost because the cache could not be written. Temp files left behind by a
crash (`<name>.*.tmp` next to the cache, older than `STALE_TMP_AGE`) are
removed on load.

One `Cache` instance should be shared by everything that writes the same
file (the add-on keeps one per session): two instances would each write
their own snapshot and the last `save()` would drop the other's new entries.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

STALE_TMP_AGE = 3600.0  # seconds; younger temp files may belong to a live writer
_VERSION = 1
_SEP = "\t"


@dataclass(frozen=True)
class CacheEntry:
    value: Any  # JSON value (never None)
    timestamp: float


class Cache:
    def __init__(
        self,
        path: str | os.PathLike[str] | None,
        *,
        autosave_every: int = 25,
        clock: Callable[[], float] = time.time,
        replace: Callable[[str, Path], None] = os.replace,
    ) -> None:
        """`path=None` gives an in-memory cache (never written).

        `replace` is the atomic rename (a seam for tests).
        """
        self.path = Path(path) if path is not None else None
        self.autosave_every = autosave_every
        self._clock = clock
        self._lock = threading.RLock()
        self._data: dict[str, list[Any]] = {}  # key -> [timestamp, value]
        self._dirty = 0
        self._retry_at = 0  # after a failed autosave: wait for more changes
        self._replace = replace
        self.last_error: str | None = None  # last failed save, None after a good one
        self._load()

    @staticmethod
    def _key(source: str, query: str) -> str:
        return f"{source}{_SEP}{query}"

    def _tmp_prefix(self) -> str:
        assert self.path is not None
        return self.path.name + "."

    def _remove_stale_tmp(self) -> None:
        """Delete `<name>.*.tmp` files a crashed save left next to the cache."""
        assert self.path is not None
        prefix = self._tmp_prefix()
        try:
            names = os.listdir(self.path.parent)
        except OSError:
            return
        now = time.time()  # file mtimes are wall-clock, whatever `clock` is
        for name in names:
            if not (name.startswith(prefix) and name.endswith(".tmp")):
                continue
            tmp = self.path.parent / name
            try:
                if now - tmp.stat().st_mtime > STALE_TMP_AGE:
                    tmp.unlink()
            except OSError:
                pass  # vanished meanwhile or not ours to delete: leave it

    def _load(self) -> None:
        if self.path is None:
            return
        self._remove_stale_tmp()
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            entries = raw.get("entries", {}) if isinstance(raw, dict) else {}
            self._data = {
                k: v
                for k, v in entries.items()
                if isinstance(v, list) and len(v) == 2 and v[1] is not None
            }
        except (OSError, ValueError):
            self._data = {}  # unreadable cache: start fresh, overwrite on save

    def get(self, source: str, query: str) -> CacheEntry | None:
        """The stored (found) result, or None if absent."""
        with self._lock:
            item = self._data.get(self._key(source, query))
        if item is None:
            return None
        return CacheEntry(item[1], float(item[0]))

    def put(self, source: str, query: str, value: Any) -> None:
        """Store a found result; `None` (a miss) is ignored, never cached."""
        if value is None:
            return
        with self._lock:
            self._data[self._key(source, query)] = [self._clock(), value]
            self._dirty += 1
            if self.autosave_every and self._dirty >= max(self.autosave_every, self._retry_at):
                self.save()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def save(self) -> bool:
        """Atomically write the cache (no-op for in-memory or unchanged caches).

        Returns False when the write failed. Never raises `OSError`: the error
        is logged and kept in `last_error`, the temp file is removed and the
        data stays dirty, so the next `save()` (or autosave after another
        `autosave_every` changes) tries again.
        """
        with self._lock:
            if self.path is None or not self._dirty:
                return True
            payload = json.dumps(
                {"version": _VERSION, "entries": self._data},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            tmp: str | None = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(
                    prefix=self._tmp_prefix(), suffix=".tmp", dir=self.path.parent
                )
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                self._replace(tmp, self.path)
            except OSError as e:
                if tmp is not None:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                self.last_error = f"{type(e).__name__}: {e}"
                self._retry_at = self._dirty + self.autosave_every
                log.warning("IPA cache not saved to %s: %s", self.path, self.last_error)
                return False
            except BaseException:
                if tmp is not None:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                raise
            self._dirty = 0
            self._retry_at = 0
            self.last_error = None
            return True

    flush = save
