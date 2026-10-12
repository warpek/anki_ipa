"""http.py and cache.py, offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fakes import client

from anki_ipa.core.cache import Cache
from anki_ipa.core.http import USER_AGENT, HttpError, RateLimited, Response, parse_retry_after


def test_user_agent_and_json() -> None:
    c, t, _ = client(lambda m, u: Response(200, {}, b'{"a": 1}'))
    assert c.get_json("https://x.test/a") == {"a": 1}
    assert t.calls[0][2]["User-Agent"] == USER_AGENT
    assert "@" not in USER_AGENT


def test_404_is_none() -> None:
    c, _, _ = client(lambda m, u: Response(404, {}, b"<html>"))
    assert c.get_json("https://x.test/a") is None
    assert c.get_text("https://x.test/a") is None


def test_other_errors_raise() -> None:
    c, _, _ = client(lambda m, u: Response(500, {}, b"boom"))
    with pytest.raises(HttpError) as ei:
        c.get_text("https://x.test/a")
    assert ei.value.status == 500 and not isinstance(ei.value, RateLimited)


def test_network_error_is_http_error() -> None:
    def boom(m: str, u: str) -> Response:
        raise OSError("offline")

    c, _, _ = client(boom)
    with pytest.raises(HttpError) as ei:
        c.get_text("https://x.test/a")
    assert ei.value.status == 0


def test_invalid_json_raises() -> None:
    c, _, _ = client(lambda m, u: Response(200, {}, b"not json"))
    with pytest.raises(HttpError):
        c.get_json("https://x.test/a")


def test_429_retry_after_then_success() -> None:
    answers = [Response(429, {"retry-after": "7"}), Response(200, {}, b"ok")]
    c, t, sleeps = client(lambda m, u: answers.pop(0))
    assert c.get_text("https://x.test/a") == "ok"
    assert len(t.calls) == 2
    assert sleeps == [7.0]


def test_503_backoff_without_header_then_rate_limited() -> None:
    c, t, sleeps = client(lambda m, u: Response(503), max_retries=2)
    with pytest.raises(RateLimited):
        c.get_text("https://x.test/a")
    assert len(t.calls) == 3
    assert sleeps == [2.0, 4.0]  # exponential backoff


def test_retry_after_over_cap_gives_up_immediately() -> None:
    c, t, sleeps = client(lambda m, u: Response(429, {"retry-after": "3600"}), max_retry_after=60)
    with pytest.raises(RateLimited):
        c.get_text("https://x.test/a")
    assert len(t.calls) == 1 and sleeps == []


def test_parse_retry_after() -> None:
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None
    assert parse_retry_after("Thu, 01 Jan 1970 00:01:40 GMT", now=0.0) == 100.0


def test_per_host_throttle() -> None:
    c, _, sleeps = client(lambda m, u: Response(200, {}, b"x"), min_interval=1.5)
    c.get_text("https://a.test/1")
    c.get_text("https://b.test/1")  # other host: no wait
    c.get_text("https://a.test/2")  # same host, clock did not move: wait 1.5
    assert sleeps == [1.5]


def test_post_json() -> None:
    c, t, _ = client(lambda m, u: Response(200, {}, b'{"ok": true}'))
    assert c.post_json("https://x.test/p", {"q": 1}) == {"ok": True}
    assert t.calls[0][0] == "POST"
    assert t.calls[0][2]["Content-Type"] == "application/json"


# -- cache -------------------------------------------------------------------


def test_cache_roundtrip_and_atomic_save(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "ipa_cache.json"
    c = Cache(path, autosave_every=0)
    c.put("fd", "record", {"uk": ["/x/"]})
    c.put("fd", "qwzxv", None)
    assert not path.exists()  # autosave disabled
    c.save()
    assert json.loads(path.read_text())["version"] == 1
    assert not list(path.parent.glob("*.tmp"))
    c2 = Cache(path)
    hit = c2.get("fd", "record")
    assert hit is not None and hit.value == {"uk": ["/x/"]}
    assert c2.get("fd", "qwzxv") is None  # misses are never stored
    assert len(c2) == 1
    assert c2.get("wk", "record") is None  # keyed by source too


def test_cache_ignores_misses_and_drops_legacy_null_entries(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    # v0.1.0 cached misses as null values
    legacy = {"fd\tneg": [1.0, None], "fd\tpos": [1.0, {"uk": ["/a/"]}], "bad": [1.0]}
    path.write_text(json.dumps({"version": 1, "entries": legacy}))
    c = Cache(path, autosave_every=0)
    assert c.get("fd", "neg") is None and len(c) == 1
    pos = c.get("fd", "pos")
    assert pos is not None and pos.value == {"uk": ["/a/"]}  # positives never expire
    c.put("fd", "miss", None)  # ignored: nothing to save
    assert len(c) == 1 and c.save()
    assert "fd\tneg" in json.loads(path.read_text())["entries"]  # not dirty: file unchanged
    c.put("fd", "new", {"uk": ["/b/"]})
    c.save()
    assert set(json.loads(path.read_text())["entries"]) == {"fd\tpos", "fd\tnew"}


def test_cache_autosave_and_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text("{corrupt")
    c = Cache(path, autosave_every=2)
    assert len(c) == 0
    c.put("s", "a", {"uk": ["/a/"]})
    c.put("s", "b", {"uk": ["/b/"]})
    assert len(json.loads(path.read_text())["entries"]) == 2


def test_in_memory_cache() -> None:
    c = Cache(None)
    c.put("s", "a", {"uk": []})
    c.save()
    assert c.get("s", "a") is not None
