"""Resolver behaviour with fake sources (no network)."""

from __future__ import annotations

from pathlib import Path

from fakes import client

from anki_ipa.core.cache import Cache
from anki_ipa.core.http import HttpError, RateLimited, Response
from anki_ipa.core.normalize import NormalizeConfig
from anki_ipa.core.resolver import Resolver, Result, build_resolver
from anki_ipa.core.sources.base import Pronunciations
from anki_ipa.core.sources.freedictionary import FreeDictionarySource
from anki_ipa.core.sources.wiktionary import WiktionarySource


class FakeSource:
    def __init__(
        self,
        name: str,
        data: dict[str, Pronunciations],
        error: Exception | None = None,
    ) -> None:
        self.name = name
        self.data = data
        self.error = error
        self.calls: list[str] = []

    def lookup(self, text: str) -> Pronunciations | None:
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        return self.data.get(text)


def P(uk: list[str] | None = None, us: list[str] | None = None, other: list[str] | None = None) -> Pronunciations:
    return Pronunciations(uk=uk or [], us=us or [], other=other or [])


WORDS = {
    "clock": P(uk=["/klɒk/"], us=["/klɑk/"]),
    "in": P(uk=["/ɪn/"]),
    "he": P(uk=["/hiː/"]),
    "was": P(uk=["/wɒz/", "/wəz/"]),
    "snow": P(uk=["/snəʊ/"]),
    "storm": P(uk=["/stɔːm/"]),
    "show": P(uk=["/ʃəʊ/"]),
    "tell": P(uk=["/tɛl/"]),
}


def resolver(*sources: FakeSource, word_by_word: bool = True, max_errors: int = 3) -> Resolver:
    return Resolver(
        list(sources), Cache(None), NormalizeConfig(), word_by_word=word_by_word, max_errors=max_errors
    )


def test_accent_preference_uk_first_and_two_variants() -> None:
    src = FakeSource("a", {"record": P(uk=["/ˈɹɛk.ɔːd/", "/ɹɪˈkɔːd/", "/third/"], us=["/ˈɹɛk.ɚd/"])})
    r = resolver(src).resolve("record")
    assert r == Result("ˈrekɔːd, rɪˈkɔːd", "a", True, [])


def test_us_fallback_then_other_and_preference_override() -> None:
    src = FakeSource("a", {"x": P(us=["/ɛks/"], other=["/oth/"]), "y": P(other=["/waɪ/"])})
    assert resolver(src).resolve("x").ipa == "eks"
    assert resolver(src).resolve("y").ipa == "waɪ"
    assert Resolver([src], None, None, accent_preference=["us", "uk"]).resolve("x").ipa == "eks"


def test_phonemic_beats_phonetic_across_buckets() -> None:
    src = FakeSource("a", {"to": P(uk=["[tu̟ː]"], other=["/tuː/", "/tə/"]), "x": P(uk=["[ks]"])})
    assert resolver(src).resolve("to").ipa == "tuː, tə"
    assert resolver(src).resolve("x").ipa == "ks"  # phonetic only: still used


def test_four_uk_narrow_beats_disagreeing_untagged_phonemic() -> None:
    # Regression (Phase 3 e2e): FD's cached "four" has UK only as narrow
    # [fɔː]/[foː] and an untagged, US-like /foɹ/ that used to win (-> "for").
    src = FakeSource("a", {"four": P(uk=["[fɔː]", "[foː]"], other=["/foɹ/"])})
    assert resolver(src).resolve("four") == Result("fɔː", "a", True, [])
    # "to" still takes the untagged phonemic forms: they agree with UK [tu̟ː].
    to = FakeSource("a", {"to": P(uk=["[tu̟ː]", "[tʰʊw]"], us=["[ɾə]"], other=["/tuː/", "/tə/", "/tʊ/"])})
    assert resolver(to).resolve("to").ipa == "tuː, tə"


def test_accent_order_phonemic_uk_us_untagged_then_narrow() -> None:
    src = FakeSource(
        "a",
        {
            "us": P(uk=["[ʌs]"], us=["/ʌs/"], other=["/əs/"]),  # US phonemic beats both
            "un": P(other=["/ʌn/"], us=["[ʌn]"]),  # agrees with US narrow
            "nar": P(other=["[nɑː]", "[nɑ]"]),  # untagged narrow: first only
            "dis": P(uk=["[dɪs]"], other=["/dɪz/"]),  # disagree, UK narrow wins
            "none": P(other=["/nʌn/", "/nɒn/"]),  # no narrow tagged form: untagged ok
        },
    )
    r = resolver(src)
    assert [r.resolve(w).ipa for w in ("us", "un", "nar", "dis", "none")] == [
        "ʌs", "ʌn", "nɑː", "dɪs", "nʌn, nɒn",
    ]


def test_fragments_skipped() -> None:
    src = FakeSource("a", {"re": P(uk=["/ɹə-/", "/ɹiː/"])})
    assert resolver(src).resolve("re").ipa == "riː"


def test_fallback_to_second_source() -> None:
    first = FakeSource("fd", {})
    second = FakeSource("wk", {"scope": P(uk=["/skəʊp/"])})
    r = resolver(first, second).resolve("scope")
    assert r.ipa == "skəʊp" and r.source == "wk" and r.complete
    assert first.calls == ["scope"]


def test_word_by_word_with_placeholders() -> None:
    src = FakeSource("a", WORDS)
    r = resolver(src).resolve("clock in qwzxv")
    assert r.ipa == "klɒk ɪn [qwzxv]"
    assert r.complete is False and r.missing == ["qwzxv"] and r.source == "a"
    # whole phrase first, then each distinct token once; "in" comes from the
    # built-in function-word table, not the source
    assert src.calls == ["clock in qwzxv", "clock", "qwzxv"]


def test_word_by_word_disabled() -> None:
    r = resolver(FakeSource("a", WORDS), word_by_word=False).resolve("clock in")
    # the user turned word-by-word off: a whole-phrase miss is definitive
    assert r == Result(None, None, False, ["clock", "in"], definitive=True)


def test_all_words_missing_gives_none() -> None:
    r = resolver(FakeSource("a", {})).resolve("foo bar")
    assert r.ipa is None and r.missing == ["foo", "bar"]


def test_sentence_lowercase_fallback_and_punctuation() -> None:
    src = FakeSource("a", WORDS)
    r = resolver(src).resolve("He was detained by a snow storm.")
    # was/by/a from the weak-form table
    assert r.ipa == "hiː wəz [detained] baɪ ə snəʊ stɔːm."
    assert "He" in src.calls and "he" in src.calls  # exact, then lower-case
    assert r.missing == ["detained"]


def test_whole_phrase_hit_keeps_punctuation() -> None:
    src = FakeSource("a", {"Don't interfere": P(uk=["/dəʊnt ˌɪntəˈfɪə/"])})
    r = resolver(src).resolve("Don't interfere!")
    assert r.ipa == "dəʊnt ˌɪntəˈfɪə!" and r.complete


def test_phrase_with_comma_and_mixed_sources() -> None:
    a = FakeSource("a", {"show": WORDS["show"]})
    b = FakeSource("b", {"tell": WORDS["tell"], "don't": P(uk=["/dəʊnt/"])})
    r = resolver(a, b).resolve("show, don't tell")
    assert r.ipa == "ʃəʊ, dəʊnt tel"
    assert r.source == "mixed" and r.complete


def test_alternatives_joined() -> None:
    src = FakeSource("a", {"adjuster": P(uk=["/əˈdʒʌstə/"]), "adjustor": P(uk=["/əˈdʒʌstə/"]), "grey": P(uk=["/ɡɹeɪ/"]), "gray": P(us=["/ɡɹeɪ/"])})
    assert resolver(src).resolve("adjustor, adjuster").ipa == "əˈdʒʌstə"
    assert resolver(src).resolve("grey, gray").ipa == "greɪ"


def test_alternative_partially_missing() -> None:
    src = FakeSource("a", {"adjuster": P(uk=["/əˈdʒʌstə/"])})
    r = resolver(src).resolve("adjustor, adjuster")
    assert r.ipa == "əˈdʒʌstə" and not r.complete and r.missing == ["adjustor"]


def test_headword_cleaning_applied() -> None:
    src = FakeSource("a", {"nuisance": P(uk=["/ˈnjuː.səns/"])})
    assert resolver(src).resolve("nuisance&amp;nbsp;").ipa == "ˈnjuːsəns"
    assert resolver(src).resolve("<div>&nbsp;</div>") == Result(None, None, False, [])


def test_cache_hit_avoids_second_call(tmp_path: Path) -> None:
    src = FakeSource("a", {"scope": P(uk=["/skəʊp/"])})
    cache = Cache(tmp_path / "c.json")
    r1 = Resolver([src], cache, NormalizeConfig())
    r1.resolve("scope")
    r1.resolve("scope")
    r1.resolve("qwzxv")
    r1.resolve("qwzxv")
    assert src.calls == ["scope", "qwzxv"]
    cache.save()
    # a fresh run reads the file
    src2 = FakeSource("a", {})
    r2 = Resolver([src2], Cache(tmp_path / "c.json"), NormalizeConfig())
    assert r2.resolve("scope").ipa == "skəʊp"
    assert src2.calls == []


def test_miss_asked_once_per_resolver_and_again_by_the_next() -> None:
    shared = Cache(None)
    src = FakeSource("a", {})
    r = Resolver([src], shared, NormalizeConfig())
    r.resolve("qwzxv")
    r.resolve("qwzxv")
    assert src.calls == ["qwzxv"]  # per-run memo
    assert len(shared) == 0  # misses are not cached
    Resolver([src], shared, NormalizeConfig()).resolve("qwzxv")
    assert src.calls == ["qwzxv", "qwzxv"]  # a new run asks again


def test_rate_limited_falls_through_and_disables() -> None:
    limited = FakeSource("fd", {}, error=RateLimited("u", 429))
    wk = FakeSource("wk", {"scope": P(uk=["/skəʊp/"]), "show": P(uk=["/ʃəʊ/"])})
    r = resolver(limited, wk)
    assert r.resolve("scope").source == "wk"
    assert r.resolve("show").ipa == "ʃəʊ"
    assert limited.calls == ["scope"]  # disabled after the first RateLimited
    assert "fd" in r.disabled


def test_http_errors_not_cached_and_disable_after_max() -> None:
    flaky = FakeSource("fd", {}, error=HttpError("u", 0, "offline"))
    r = resolver(flaky, max_errors=2)
    r.resolve("a")
    r.resolve("a")  # not cached: asked again
    r.resolve("b")  # disabled now
    assert flaky.calls == ["a", "a"]
    assert "fd" in r.disabled


def test_cancel_and_progress() -> None:
    src = FakeSource("a", WORDS)
    events: list[tuple[int, int, str]] = []
    stop = [False]

    def progress(done: int, total: int, label: str) -> None:
        events.append((done, total, label))
        if done == 1:
            stop[0] = True

    r = Resolver([src], None, None, cancel=lambda: stop[0], progress=progress)
    results = r.resolve_many(["show", "tell", "clock"])
    assert [x.ipa for x in results] == ["ʃəʊ"]
    assert events == [(0, 3, "show"), (1, 3, "tell")]

    full = Resolver([src], None, None).resolve_many(["show", "tell"])
    assert [x.ipa for x in full] == ["ʃəʊ", "tel"]


def test_build_resolver_from_config(tmp_path: Path) -> None:
    http, _, _ = client(lambda m, u: Response(404))
    config: dict[str, object] = {
        "sources": ["wiktionary", "claude", "freedictionary"],
        "accent_preference": ["us"],
        "word_by_word": False,
        "normalize_map": {"ɹ": "r"},
    }
    r = build_resolver(config, str(tmp_path / "ipa_cache.json"), http=http)
    assert [type(s) for s in r.sources] == [WiktionarySource, FreeDictionarySource]
    assert r.accents == ["us", "other"]
    assert r.word_by_word is False
    assert r.cache.path == tmp_path / "ipa_cache.json"
    assert r.resolve("qwzxv").ipa is None
