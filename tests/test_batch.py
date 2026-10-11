"""Pure batch logic: candidate filter, write plan, tags, summary text."""

from __future__ import annotations

import http.client

import pytest
from fakes import client

from anki_ipa.core.batch import (
    SKIP_FILLED,
    SKIP_NO_WORD,
    SKIP_NOTE_TYPE,
    Fields,
    build_plan,
    build_search,
    field_is_empty,
    skip_reason,
    summarize,
    summary_text,
    tags_for,
    unique_words,
)
from anki_ipa.core.cache import Cache
from anki_ipa.core.http import HttpError, Response
from anki_ipa.core.normalize import NormalizeConfig
from anki_ipa.core.resolver import Resolver, Result
from anki_ipa.core.sources.base import Pronunciations

F = Fields()


def fields(word: str = "cat", ipa: str = "") -> dict[str, str]:
    return {
        "English": word,
        "Polish": "",
        "Not-english": "",
        "Not-polish": "",
        "English-sound": "",
        "IPA-english": ipa,
    }


def test_fields_from_config_and_search() -> None:
    cfg = {"note_type": "English words", "tag_partial": "x::p", "overwrite_existing": True}
    f = Fields.from_config(cfg)
    assert (f.word, f.ipa, f.tag_auto, f.tag_partial, f.overwrite) == (
        "English", "IPA-english", "ipa::auto", "x::p", True
    )
    assert build_search({}) == '"note:English words" "English:_*" "IPA-english:"'
    assert build_search(cfg) == '"note:English words" "English:_*"'


@pytest.mark.parametrize("value", ["", "  ", "&nbsp;", "<br>", "<div></div>", "<div><br></div>"])
def test_field_is_empty(value: str) -> None:
    assert field_is_empty(value)


def test_field_not_empty() -> None:
    assert not field_is_empty("kæt")
    assert not field_is_empty("<div>kæt</div>")


def test_skip_reason() -> None:
    assert skip_reason("English words", fields(), F) is None
    assert skip_reason("English words", fields(ipa="&nbsp;"), F) is None
    assert skip_reason("Basic", fields(), F) == SKIP_NOTE_TYPE
    assert skip_reason("English words", {"English": "cat"}, F) == SKIP_NOTE_TYPE
    assert skip_reason("English words", fields(ipa="kæt"), F) == SKIP_FILLED
    assert skip_reason("English words", fields(word="<br>"), F) == SKIP_NO_WORD
    assert skip_reason("English words", fields(ipa="kæt"), Fields(overwrite=True)) is None


def test_unique_words_keeps_order() -> None:
    assert unique_words([(1, "b"), (2, "a"), (3, "b")]) == ["b", "a"]


def test_tags_for() -> None:
    assert tags_for(Result("kæt", "wiktionary", True), F) == ("ipa::auto",)
    assert tags_for(Result("kæt", "claude", True), F) == ("ipa::auto", "ipa::llm")
    assert tags_for(Result("[x] kæt", "mixed", False, ["x"]), F) == ("ipa::auto", "ipa::partial")
    assert tags_for(Result("kæt", "claude", False), Fields(tag_llm="")) == (
        "ipa::auto",
        "ipa::partial",
    )


ITEMS = [(1, "cat"), (2, "dog house"), (3, "qwzxv"), (4, "cat"), (5, "late")]
RESULTS = {
    "cat": Result("kæt", "freedictionary", True),
    "dog house": Result("dɒg [house]", "freedictionary", False, ["house"]),
    "qwzxv": Result(None, None, False, ["qwzxv"]),
}


def test_build_plan() -> None:
    plan = build_plan(ITEMS, RESULTS, F)
    assert sorted(plan) == [1, 2, 4]
    assert plan[1].ipa == "kæt" and plan[1].tags == ("ipa::auto",) and not plan[1].partial
    assert plan[2].tags == ("ipa::auto", "ipa::partial") and plan[2].partial
    assert plan[2].source == "freedictionary"


def test_summarize_and_text() -> None:
    plan = build_plan(ITEMS, RESULTS, F)
    s = summarize(
        ITEMS,
        RESULTS,
        plan,
        {1, 2},  # note 4 was filled by the user meanwhile
        words_total=4,
        cancelled=True,
        disabled={"freedictionary": "rate limited: 429"},
        errors=["wiktionary 'x': KeyError: 'y'"],
    )
    assert s.filled == 2 and s.partial == 1 and s.missed_notes == 1
    assert s.missed == ["qwzxv"] and s.not_processed == 1 and s.skipped_changed == 1
    text = summary_text(s, F)
    assert "Notes processed: 4 of 5 (CANCELLED after 3 of 4 headwords)" in text
    assert "Filled: 2" in text and "FreeDictionaryAPI.com: 2" in text
    assert "partial ([word] placeholders, tagged ipa::partial): 1" in text
    assert "Missed: 1" in text and "Not processed (cancelled): 1" in text
    assert "Skipped (IPA filled or note changed meanwhile): 1" in text
    assert "Sources disabled during the run:\n    FreeDictionaryAPI.com: rate limited: 429" in text
    assert "KeyError" in text
    assert text.endswith("Missed headwords (1):\n    qwzxv")


def test_summary_missed_headwords_are_cleaned_and_distinct() -> None:
    items = [(1, "<div>foo&nbsp;</div>"), (2, "foo")]
    results = {w: Result(None, None, False, ["foo"]) for _, w in items}
    s = summarize(items, results, {}, set(), words_total=2, cancelled=False)
    assert s.missed == ["foo"] and s.missed_notes == 2 and s.filled == 0
    text = summary_text(s)
    assert "CANCELLED" not in text and "Filled: 0" in text


# -- resolver / http robustness relied on by the batch ------------------------
class BuggySource:
    name = "buggy"

    def lookup(self, text: str) -> Pronunciations | None:
        raise KeyError("pronunciations")


class GoodSource:
    name = "good"

    def lookup(self, text: str) -> Pronunciations | None:
        return Pronunciations(uk=["/kæt/"]) if text == "cat" else None


class FlakySource:
    name = "flaky"

    def lookup(self, text: str) -> Pronunciations | None:
        raise HttpError("u", 0, "timed out")


def test_unexpected_source_exception_is_a_miss_not_an_abort() -> None:
    r = Resolver([BuggySource(), GoodSource()], Cache(None), NormalizeConfig(), max_errors=5)
    out = r.resolve_many(["cat", "dog"])
    assert [x.ipa for x in out] == ["kæt", None]
    assert len(r.unexpected_errors) == 2 and "KeyError" in r.unexpected_errors[0]
    assert r.cache.get("buggy", "cat") is None  # not cached


def test_network_errors_do_not_abort_batch() -> None:
    r = Resolver([FlakySource(), GoodSource()], Cache(None), NormalizeConfig(), max_errors=2)
    out = r.resolve_many(["cat", "dog", "cat"])
    assert len(out) == 3 and out[0].ipa == "kæt"
    assert "flaky" in r.disabled and r.unexpected_errors == []


def test_incomplete_read_is_http_error() -> None:
    def boom(m: str, u: str) -> Response:
        raise http.client.IncompleteRead(b"")

    c, _, _ = client(boom)
    with pytest.raises(HttpError) as ei:
        c.get_text("https://x.test/a")
    assert ei.value.status == 0
