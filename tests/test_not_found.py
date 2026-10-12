"""Phase 10a: definitive misses (`Result.definitive`) and the not-found tag."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from fakes import ENGLISH_WORDS, client

from anki_ipa.core.batch import (
    DEFAULT_TAG_NOT_FOUND,
    SKIP_FILLED,
    SKIP_NOT_FOUND,
    SKIP_NOTE_TYPE,
    Fields,
    Planned,
    build_plan,
    has_tag,
    needs_fill,
    not_found_skip_text,
    skip_reason,
    summarize,
    summary_text,
    tag_matches,
    tags_for,
    without_tags,
)
from anki_ipa.core.cache import Cache
from anki_ipa.core.http import HttpError, Interrupted, RateLimited
from anki_ipa.core.normalize import NormalizeConfig
from anki_ipa.core.resolver import NO_KEY_REASON, Resolver, Result, build_resolver
from anki_ipa.core.sources.base import Pronunciations, SourceDisabled

F = ENGLISH_WORDS


class Src:
    """Fake source: `data` hits, `errors[text]` raised for that text only."""

    def __init__(
        self,
        name: str,
        data: dict[str, Pronunciations] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.name = name
        self.data = data or {}
        self.errors = errors or {}
        self.calls: list[str] = []

    def lookup(self, text: str) -> Pronunciations | None:
        self.calls.append(text)
        if text in self.errors:
            raise self.errors[text]
        return self.data.get(text)


CAT = {"cat": Pronunciations(uk=["/kæt/"])}


def res(
    *sources: Src, fallback: Src | None = None, cache: Cache | None = None, max_errors: int = 3
) -> Resolver:
    return Resolver(
        list(sources), cache or Cache(None), NormalizeConfig(), fallback=fallback, max_errors=max_errors
    )


# -- Result.definitive ----------------------------------------------------------------
def test_clean_miss_through_every_source_is_definitive() -> None:
    a, b = Src("a"), Src("b")
    r = res(a, b).resolve("qwzxv")
    assert r == Result(None, None, False, ["qwzxv"], definitive=True)
    assert a.calls == b.calls == ["qwzxv"]


def test_hits_and_partials_are_never_definitive() -> None:
    r = res(Src("a", CAT))
    assert r.resolve("cat").definitive is False
    partial = r.resolve("cat qwzxv")
    assert partial.ipa == "kæt [qwzxv]" and not partial.complete and not partial.definitive


def test_legacy_cached_miss_is_ignored_and_the_source_asked(tmp_path: Path) -> None:
    """v0.1.0 cached misses as `null`; they are dropped on load."""
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"version": 1, "entries": {"a\tcat": [0, None]}}))
    src = Src("a", CAT)
    assert res(src, cache=Cache(path)).resolve("cat").ipa == "kæt"
    assert src.calls == ["cat"]


def test_miss_is_asked_once_per_run_and_never_cached() -> None:
    cache = Cache(None)
    src = Src("a")
    r = res(src, cache=cache)
    assert r.resolve("qwzxv").definitive
    assert r.resolve("qwzxv").definitive  # memo: same answer, no second request
    assert src.calls == ["qwzxv"] and len(cache) == 0
    # the next run (a new resolver on the same shared cache) asks afresh
    src.data["qwzxv"] = Pronunciations(uk=["/kwɪz/"])
    assert res(src, cache=cache).resolve("qwzxv").ipa == "kwɪz"
    assert src.calls == ["qwzxv", "qwzxv"]


def test_memoised_miss_of_a_source_disabled_later_still_counts() -> None:
    a, b = Src("a"), Src("b", errors={"foo": RateLimited("u", 429)})
    r = res(a, b)
    assert r.resolve("qwzxv").definitive  # both answered "not found"
    assert not r.resolve("foo").definitive and "b" in r.disabled
    # b's answer for qwzxv is remembered from this run: still definitive
    assert r.resolve("qwzxv").definitive
    # a never-asked query of the disabled source is uncertain
    assert not r.resolve("bar").definitive


def test_fragment_only_result_is_a_miss_and_not_cached() -> None:
    cache = Cache(None)
    src = Src("a", {"ing": Pronunciations(uk=["/-ɪŋ/"], other=["-ɪŋ"])})
    r = res(src, cache=cache).resolve("ing")
    assert r.ipa is None and r.definitive and len(cache) == 0 and src.calls == ["ing"]


def test_transient_error_is_not_definitive_and_flag_resets_per_headword() -> None:
    a = Src("a", errors={"foo": HttpError("u", 0, "offline")})
    b = Src("b")
    r = res(a, b, max_errors=5)
    assert r.resolve("foo") == Result(None, None, False, ["foo"])  # b said no, a failed
    assert r.resolve("bar").definitive  # next headword starts clean
    assert r.cache.get("a", "foo") is None  # still uncached: retried next time


def test_unexpected_exception_is_not_definitive() -> None:
    r = res(Src("a", errors={"foo": KeyError("x")}))
    assert not r.resolve("foo").definitive
    assert r.unexpected_errors


def test_rate_limited_source_is_not_definitive_now_or_later() -> None:
    a = Src("a", errors={"foo": RateLimited("u", 429)})
    r = res(a, Src("b"))
    assert not r.resolve("foo").definitive
    assert "a" in r.disabled
    # later headwords skip the disabled source: not asked, so not definitive
    assert not r.resolve("bar").definitive
    assert a.calls == ["foo"]


def test_max_errors_disabled_source_is_not_definitive() -> None:
    a = Src("a", errors={w: HttpError("u", 0, "x") for w in ("w1", "w2")})
    r = res(a, max_errors=2)
    r.resolve("w1")
    r.resolve("w2")
    assert "a" in r.disabled
    assert not r.resolve("w3").definitive


def test_word_by_word_all_missing() -> None:
    assert res(Src("a")).resolve("foo bar") == Result(
        None, None, False, ["foo", "bar"], definitive=True
    )
    # one token failed transiently: the whole miss is not definitive
    flaky = Src("a", errors={"bar": HttpError("u", 0, "x")})
    r = res(flaky, max_errors=5).resolve("foo bar")
    assert r.ipa is None and not r.definitive


def test_empty_headword_is_not_definitive() -> None:
    assert res(Src("a")).resolve("<div>&nbsp;</div>") == Result(None, None, False, [])


def test_claude_clean_miss_is_definitive_and_asked() -> None:
    fb = Src("claude")
    r = res(Src("a"), fallback=fb).resolve("qwzxv")
    assert r.definitive and fb.calls == ["qwzxv"]


def test_claude_hit_after_free_miss() -> None:
    fb = Src("claude", {"qwzxv": Pronunciations(uk=["kwɪz"])})
    r = res(Src("a"), fallback=fb).resolve("qwzxv")
    assert r == Result("kwɪz", "claude", True, [])


@pytest.mark.parametrize(
    "error", [SourceDisabled("invalid API key (HTTP 401)"), RateLimited("u", 429), HttpError("u", 500)]
)
def test_claude_failure_is_not_definitive(error: Exception) -> None:
    fb = Src("claude", errors={"qwzxv": error})
    r = res(Src("a"), fallback=fb, max_errors=5)
    assert not r.resolve("qwzxv").definitive


def test_claude_disabled_earlier_is_not_definitive() -> None:
    fb = Src("claude")
    r = res(Src("a"), fallback=fb)
    r.disabled["claude"] = "invalid API key"
    assert not r.resolve("qwzxv").definitive and fb.calls == []


def test_claude_enabled_without_key_is_not_definitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    http, _, _ = client(lambda m, u: pytest.fail(f"unexpected request {u}"))
    cfg: dict[str, object] = {"sources": [], "claude": {"enabled": True, "api_key": ""}}
    r = build_resolver(cfg, http=http)
    assert r.disabled == {"claude": NO_KEY_REASON}
    assert not r.resolve("qwzxv").definitive
    # Claude off: the free chain alone decides
    off = build_resolver({"sources": [], "claude": {"enabled": False}}, http=http)
    assert off.resolve("qwzxv").definitive


def test_cancelled_headword_has_no_result() -> None:
    stop = [False]

    class Stopper(Src):
        def lookup(self, text: str) -> Pronunciations | None:
            stop[0] = True
            return None

    r = Resolver([Stopper("a"), Src("b")], Cache(None), cancel=lambda: stop[0])
    assert r.resolve_many(["qwzxv", "cat"]) == []


def test_interrupted_wait_has_no_result() -> None:
    r = res(Src("a", errors={"qwzxv": Interrupted("u", 0, "cancelled")}))
    assert r.resolve_many(["qwzxv"]) == []


# -- tags -------------------------------------------------------------------------------
def test_tag_matching_like_anki() -> None:
    assert tag_matches("ipa::notfound", "ipa::notfound")
    assert tag_matches("IPA::NotFound", "ipa::notfound")
    assert tag_matches("ipa::notfound::old", "ipa::notfound")
    assert not tag_matches("ipa::notfoundx", "ipa::notfound")
    assert not tag_matches("ipa", "ipa::notfound")
    assert not tag_matches("ipa::notfound", "")
    assert has_tag(["Nemo", "IPA::notfound"], "ipa::notfound")
    assert not has_tag([], "ipa::notfound")
    assert without_tags(["Nemo", "IPA::NotFound", "ipa::notfound::x", "ipa::auto"], ["ipa::notfound"]) == [
        "Nemo", "ipa::auto",
    ]
    assert without_tags(["a"], [""]) == ["a"]


def test_fields_tag_not_found_config() -> None:
    assert Fields.from_config({}).tag_not_found == DEFAULT_TAG_NOT_FOUND == "ipa::notfound"
    assert Fields.from_config({"tag_not_found": " x::nf "}).tag_not_found == "x::nf"
    assert Fields.from_config({"tag_not_found": ""}).tag_not_found == ""


def fields(word: str = "cat", ipa: str = "") -> dict[str, str]:
    return {"English": word, "IPA-english": ipa}


def test_skip_reason_with_tags() -> None:
    w = "English words"
    assert skip_reason(w, fields(), F) is None  # no tags passed: old behaviour
    assert skip_reason(w, fields(), F, tags=["Nemo"]) is None
    for tag in ("ipa::notfound", "IPA::NotFound", "ipa::notfound::2026"):
        assert skip_reason(w, fields(), F, tags=["Nemo", tag]) == SKIP_NOT_FOUND
    assert not needs_fill(w, fields(), F, tags=["ipa::notfound"])
    assert skip_reason(w, fields(), F, tags=["ipa::notfoundish"]) is None
    # the other reasons come first
    assert skip_reason(w, fields(ipa="kæt"), F, tags=["ipa::notfound"]) == SKIP_FILLED
    assert skip_reason("Basic", fields(), F, tags=["ipa::notfound"]) == SKIP_NOTE_TYPE
    # overwrite mode still respects the tag
    assert skip_reason(w, fields(ipa="kæt"), replace(F, overwrite=True), tags=["ipa::notfound"]) == SKIP_NOT_FOUND
    # tagging off: the tag means nothing
    assert skip_reason(w, fields(), replace(F, tag_not_found=""), tags=["ipa::notfound"]) is None


def test_tags_for_misses() -> None:
    assert tags_for(Result(None, None, False, ["x"], definitive=True), F) == ("ipa::notfound",)
    assert tags_for(Result(None, None, False, ["x"]), F) == ()
    assert tags_for(Result(None, None, False, ["x"], definitive=True), replace(F, tag_not_found="")) == ()
    assert tags_for(Result("kæt [x]", "a", False, ["x"]), F) == ("ipa::auto", "ipa::partial")


ITEMS = [(1, "cat"), (2, "qwzxv"), (3, "flaky"), (4, "dog [x]"), (5, "qwzxv"), (6, "late")]
RESULTS = {
    "cat": Result("kæt", "freedictionary", True),
    "qwzxv": Result(None, None, False, ["qwzxv"], definitive=True),
    "flaky": Result(None, None, False, ["flaky"]),  # error on the way
    "dog [x]": Result("dɒg [x]", "wiktionary", False, ["x"]),
}


def test_build_plan_with_not_found_items() -> None:
    plan = build_plan(ITEMS, RESULTS, F)
    assert sorted(plan) == [1, 2, 4, 5]
    assert plan[1] == Planned("kæt", ("ipa::auto",), "freedictionary", False, ("ipa::notfound",))
    assert plan[2] == plan[5] == Planned("", ("ipa::notfound",), "", False, (), True)
    assert plan[4].tags == ("ipa::auto", "ipa::partial") and not plan[4].not_found
    assert plan[4].remove_tags == ("ipa::notfound",)
    off = build_plan(ITEMS, RESULTS, replace(F, tag_not_found=""))
    assert sorted(off) == [1, 4] and off[1].remove_tags == ()


def test_summary_counts_and_text() -> None:
    plan = build_plan(ITEMS, RESULTS, F)
    # note 5 got its IPA by hand meanwhile: its not-found tag was not written
    s = summarize(ITEMS, RESULTS, plan, {1, 2, 4}, words_total=5, cancelled=True, skipped_not_found=7)
    assert s.filled == 2 and s.partial == 1
    assert s.missed_notes == 2 and s.tagged_not_found == 1  # notes 2 (tagged) and 3 (error)
    assert s.skipped_changed == 1 and s.not_processed == 1 and s.skipped_not_found == 7
    assert s.missed_uncertain == 1  # note 3: source error, so no tag
    assert s.missed == ["qwzxv", "flaky"]
    text = summary_text(s, F)
    assert (
        "Missed: 2\n    of which tagged ipa::notfound (no IPA found, skipped in later runs; "
        "remove the tag to retry): 1"
    ) in text
    assert "Skipped 7: tagged ipa::notfound (remove the tag to retry)" in text
    assert not_found_skip_text(7, F) in text
    assert (
        "    of which not tagged (a source failed or was turned off; tried again next run): 1"
    ) in text


def test_summary_text_without_tagging_has_no_not_found_lines() -> None:
    fcfg = replace(F, tag_not_found="")
    plan = build_plan(ITEMS, RESULTS, fcfg)
    s = summarize(ITEMS, RESULTS, plan, set(plan), words_total=5, cancelled=False)
    assert s.tagged_not_found == 0 and s.missed_notes == 3
    assert "tagged ipa::notfound" not in summary_text(s, fcfg)
    assert "not tagged" not in summary_text(s, fcfg)
