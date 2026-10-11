"""Phase 8: cache writes that fail, stale temp files, a cache and HTTP client
shared by several resolvers, and cancellable waits."""

from __future__ import annotations

import errno
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fakes import client

from anki_ipa.core import cache as cache_mod
from anki_ipa.core.batch import summarize, summary_text
from anki_ipa.core.cache import STALE_TMP_AGE, Cache
from anki_ipa.core.http import SLEEP_CHUNK, Interrupted, Response, cancel_scope
from anki_ipa.core.normalize import NormalizeConfig
from anki_ipa.core.resolver import Resolver
from anki_ipa.core.sources.base import Pronunciations
from anki_ipa.core.sources.freedictionary import FreeDictionarySource


class Src:
    def __init__(self, name: str, data: dict[str, str]) -> None:
        self.name = name
        self.data = data
        self.calls: list[str] = []

    def lookup(self, text: str) -> Pronunciations | None:
        self.calls.append(text)
        ipa = self.data.get(text)
        return None if ipa is None else Pronunciations(uk=[ipa], us=[], other=[])


def _failing_replace(err: int = errno.EACCES) -> Any:
    calls: list[str] = []

    def replace(src: str, dst: Path) -> None:
        calls.append(src)
        raise PermissionError(err, "The process cannot access the file", str(dst))

    return replace, calls


def _tmp_files(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir() if p.name.endswith(".tmp"))


# -- Cache.save never raises -------------------------------------------------------


def test_failed_replace_is_logged_kept_dirty_and_retried(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "ipa_cache.json"
    replace, calls = _failing_replace()
    c = Cache(path, autosave_every=0, replace=replace)
    c.put("fd", "record", {"uk": ["/x/"]})
    with caplog.at_level("WARNING", logger=cache_mod.__name__):
        assert c.save() is False
    assert calls and c.last_error is not None and "PermissionError" in c.last_error
    assert "not saved" in caplog.text
    assert not path.exists()
    assert _tmp_files(tmp_path) == []  # own temp file removed
    # still dirty: a later save with a working rename writes everything
    c._replace = os.replace
    assert c.save() is True
    assert c.last_error is None
    assert "fd\trecord" in json.loads(path.read_text(encoding="utf-8"))["entries"]


def test_failed_write_removes_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_fdopen = os.fdopen

    class Full:
        def __init__(self, f: Any) -> None:
            self.f = f

        def __enter__(self) -> Full:
            return self

        def __exit__(self, *a: object) -> None:
            self.f.close()

        def write(self, _s: str) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fdopen", lambda fd, *a, **k: Full(real_fdopen(fd, *a, **k)))
    c = Cache(tmp_path / "ipa_cache.json", autosave_every=0)
    c.put("fd", "a", None)
    assert c.save() is False
    assert c.last_error is not None and "No space left" in c.last_error
    assert _tmp_files(tmp_path) == []


def test_unwritable_directory_does_not_raise(tmp_path: Path) -> None:
    blocker = tmp_path / "user_files"
    blocker.write_text("a file where the folder should be")
    c = Cache(blocker / "ipa_cache.json", autosave_every=0)
    c.put("fd", "a", None)
    assert c.save() is False  # mkdir fails with FileExistsError / NotADirectoryError
    assert c.last_error is not None


def test_autosave_failure_never_raises_from_put_and_backs_off(tmp_path: Path) -> None:
    replace, calls = _failing_replace()
    c = Cache(tmp_path / "ipa_cache.json", autosave_every=2, replace=replace)
    for i in range(7):
        c.put("fd", f"w{i}", None)  # must not raise
    # autosave at 2 changes fails -> next try only after 2 more (4), then 6
    assert len(calls) == 3
    assert len(c) == 7


def test_stale_own_temp_files_are_removed_on_load(tmp_path: Path) -> None:
    old = time.time() - STALE_TMP_AGE - 60
    stale = tmp_path / "ipa_cache.json.k2j3_x.tmp"
    fresh = tmp_path / "ipa_cache.json.abcdef.tmp"
    foreign = tmp_path / "other.json.zzz.tmp"
    unrelated = tmp_path / "ipa_cache.json.bak"
    for p in (stale, fresh, foreign, unrelated):
        p.write_text("{}")
    for p in (stale, foreign, unrelated):
        os.utime(p, (old, old))
    (tmp_path / "ipa_cache.json").write_text(
        json.dumps({"version": 1, "entries": {"fd\ta": [1.0, None]}}), encoding="utf-8"
    )
    c = Cache(tmp_path / "ipa_cache.json")
    assert len(c) == 1
    assert not stale.exists()
    assert fresh.exists() and foreign.exists() and unrelated.exists()


def test_stale_cleanup_without_cache_file_or_folder(tmp_path: Path) -> None:
    old = time.time() - STALE_TMP_AGE - 60
    stale = tmp_path / "ipa_cache.json.x.tmp"
    stale.write_text("")
    os.utime(stale, (old, old))
    Cache(tmp_path / "ipa_cache.json")
    assert not stale.exists()
    Cache(tmp_path / "missing" / "ipa_cache.json")  # no folder: no error


def test_resolve_many_survives_a_failing_cache(tmp_path: Path) -> None:
    replace, _ = _failing_replace()
    cache = Cache(tmp_path / "ipa_cache.json", autosave_every=1, replace=replace)
    r = Resolver([Src("a", {"scope": "/skəʊp/", "nick": "/nɪk/"})], cache, NormalizeConfig())
    results = r.resolve_many(["scope", "nick"])
    assert [x.ipa for x in results] == ["skəʊp", "nɪk"]
    assert cache.last_error is not None


def test_summary_mentions_cache_error() -> None:
    s = summarize([], {}, {}, set(), words_total=0, cancelled=False, cache_error="OSError: x")
    assert "Lookup cache not saved (OSError: x)" in summary_text(s)
    s2 = summarize([], {}, {}, set(), words_total=0, cancelled=False)
    assert "cache" not in summary_text(s2)


# -- one cache / client shared by several resolvers ---------------------------------


def test_two_resolvers_sharing_one_cache_keep_both_entries(tmp_path: Path) -> None:
    """Batch and editor button: each saves, neither drops the other's entries."""
    path = tmp_path / "ipa_cache.json"
    shared = Cache(path, autosave_every=1)
    data = {f"w{i}": f"/w{i}/" for i in range(40)}
    batch = Resolver([Src("a", data)], shared, NormalizeConfig())
    editor = Resolver([Src("a", data)], shared, NormalizeConfig())
    words_a = [f"w{i}" for i in range(0, 40, 2)]
    words_b = [f"w{i}" for i in range(1, 40, 2)]

    def run_editor() -> None:
        for w in words_b:
            editor.resolve(w)
            editor.cache.save()

    t = threading.Thread(target=run_editor)
    t.start()
    batch.resolve_many(words_a)
    t.join()
    shared.save()
    on_disk = json.loads(path.read_text(encoding="utf-8"))["entries"]
    assert len(on_disk) == 40
    assert len(Cache(path)) == 40


def test_separate_caches_would_lose_entries(tmp_path: Path) -> None:
    """Why the add-on shares one instance: the last snapshot wins."""
    path = tmp_path / "ipa_cache.json"
    c1, c2 = Cache(path, autosave_every=0), Cache(path, autosave_every=0)
    c1.put("a", "x", None)
    c2.put("a", "y", None)
    c1.save()
    c2.save()
    assert len(Cache(path)) == 1


def test_shared_client_throttles_across_resolvers() -> None:
    http, t, sleeps = client(
        lambda m, u: Response(200, {}, b'{"word":"x","entries":[]}'), min_interval=1.0
    )
    r1 = Resolver([FreeDictionarySource(http)], Cache(None), NormalizeConfig())
    r2 = Resolver([FreeDictionarySource(http)], Cache(None), NormalizeConfig())
    r1.resolve("alpha")
    r2.resolve("beta")
    assert len(t.calls) == 2 and http.request_count == 2
    assert sleeps == [1.0]  # the second resolver waited for the first's slot


# -- cancellable waits ---------------------------------------------------------------


def test_retry_after_wait_is_interrupted_by_cancel_scope() -> None:
    http, t, sleeps = client(lambda m, u: Response(429, {"retry-after": "30"}, b""))
    flag = threading.Event()

    def cancel() -> bool:
        if len(sleeps) >= 3:
            flag.set()
        return flag.is_set()

    with cancel_scope(cancel), pytest.raises(Interrupted):
        http.get_json("https://x.test/a")
    assert len(t.calls) == 1
    assert sleeps == [SLEEP_CHUNK] * 3


def test_waits_outside_a_scope_are_one_sleep() -> None:
    http, _, sleeps = client(
        lambda m, u: Response(429, {"retry-after": "30"}, b""), max_retries=1
    )
    with pytest.raises(Exception):
        http.get_json("https://x.test/a")
    assert sleeps == [30.0]


def test_resolver_cancel_during_backoff_stops_batch_without_caching() -> None:
    http, t, sleeps = client(lambda m, u: Response(429, {"retry-after": "30"}, b""))
    cancelled = threading.Event()

    def progress(done: int, total: int, word: str) -> None:
        pass

    def cancel() -> bool:
        if sleeps:  # cancel as soon as the back-off started
            cancelled.set()
        return cancelled.is_set()

    cache = Cache(None)
    r = Resolver(
        [FreeDictionarySource(http)], cache, NormalizeConfig(), cancel=cancel, progress=progress
    )
    out = r.resolve_many(["record", "scope"])
    assert out == []
    assert len(t.calls) == 1
    assert sum(sleeps) <= SLEEP_CHUNK
    assert len(cache) == 0 and r.disabled == {}
