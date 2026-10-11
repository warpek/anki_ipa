"""Phase 2b: British style normalisation (vowels, stress, optional sounds,
diacritics) and weak forms of function words in phrases."""

from __future__ import annotations

import pytest

from anki_ipa.core.cache import Cache
from anki_ipa.core.compose import FUNCTION_WORDS, compose
from anki_ipa.core.normalize import (
    NormalizeConfig,
    british_vowels,
    count_nuclei,
    normalize_ipa,
    strip_monosyllable_stress,
    strip_optional_sounds,
)
from anki_ipa.core.resolver import Resolver, build_resolver
from anki_ipa.core.sources.base import Pronunciations

# --- vowels ------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, out",
    [
        ("ˈsasi", "ˈsæsi"),
        ("tɛl", "tel"),
        ("ˈbɛlt", "ˈbelt"),
        ("skɛː", "skeə"),  # SQUARE, long form
        ("skɛə", "skeə"),  # SQUARE, diphthong form
        ("baɪ", "baɪ"),  # PRICE untouched
        ("aʊt", "aʊt"),  # MOUTH untouched
        ("ˈaɪsˌkriːm", "ˈaɪsˌkriːm"),
        ("baːθ", "bɑːθ"),  # BATH written aː
        ("faːðə", "fɑːðə"),
        ("ˈhapi", "ˈhæpi"),
        ("ˈaʊtbreɪk", "ˈaʊtbreɪk"),
        ("ˈɡɑːtə", "ˈɡɑːtə"),
    ],
)
def test_british_vowels(raw: str, out: str) -> None:
    assert british_vowels(raw) == out


def test_vowel_rule_sees_through_diacritics() -> None:
    # non-syllabic mark on the second element of a diphthong
    assert normalize_ipa("/naʊ̯/") == "naʊ"
    assert normalize_ipa("/maɪ̯/") == "maɪ"


# --- optional sounds and diacritics -------------------------------------------


@pytest.mark.parametrize(
    "raw, out",
    [
        ("ˌɪntəˈfɪə(r)", "ˌɪntəˈfɪə"),
        ("ˌɪntəˈfɪə(ɹ)", "ˌɪntəˈfɪə"),
        ("ˈles(ə)n", "ˈlesən"),
        ("n(j)uː", "njuː"),
        ("(h)wɪtʃ", "wɪtʃ"),
        ("tʃɑːn(t)s", "tʃɑːns"),
    ],
)
def test_strip_optional_sounds(raw: str, out: str) -> None:
    assert strip_optional_sounds(raw) == out


@pytest.mark.parametrize(
    "raw, out",
    [
        ("/snəʊ̯/", "snəʊ"),  # non-syllabic
        ("[tu̟ː]", "tuː"),  # advanced; length mark kept
        ("/ˈkwæsɒ̃/", "ˈkwæsɒ"),  # nasal
        ("/ˈbʌtn̩/", "ˈbʌtn"),  # syllabic: still 2 nuclei, stress kept
        ("[kl̥ɒk]", "klɒk"),  # voiceless
        ("/t͡ʃiːz/", "tʃiːz"),  # tie bar
        ("/ˈsiːˑt/", "siːt"),  # half-long
        ("/ˌɪn.təˈfɪə(ɹ)/", "ˌɪntəˈfɪə"),
    ],
)
def test_diacritics_and_full_pipeline(raw: str, out: str) -> None:
    assert normalize_ipa(raw) == out


# --- monosyllable stress --------------------------------------------------------


@pytest.mark.parametrize(
    "word, n",
    [
        ("ˈhiː", 1),
        ("ˈbelt", 1),
        ("skəʊp", 1),
        ("ˈskeəs", 1),
        ("ˈbɒdi", 2),
        ("ˈbiːɪŋ", 2),
        ("aɪˈdɪə", 2),
        ("ˈbʌtn̩", 2),
        ("ˈsʌmbˌɒdi", 3),
        ("hm", 0),
    ],
)
def test_count_nuclei(word: str, n: int) -> None:
    assert count_nuclei(word) == n


def test_strip_monosyllable_stress_per_word() -> None:
    assert strip_monosyllable_stress("ˈhiː") == "hiː"
    assert strip_monosyllable_stress("ˈbɒdi") == "ˈbɒdi"
    assert strip_monosyllable_stress("ˈgɑːtə ˈbelt") == "ˈgɑːtə belt"
    assert strip_monosyllable_stress("ˈhiː wəz dɪˈteɪnd") == "hiː wəz dɪˈteɪnd"
    assert strip_monosyllable_stress("ˌaʊt ˈɡəʊ") == "aʊt ɡəʊ"
    assert normalize_ipa("/ˈskəʊp/") == "skəʊp"
    assert normalize_ipa("/ˈhiː/") == "hiː"


def test_whole_phrase_from_source() -> None:
    assert normalize_ipa("/ʃəʊ dəʊnt tɛl/") == "ʃəʊ dəʊnt tel"
    assert normalize_ipa("/ˈhiː wəz dɪˈteɪnd baɪ ə snəʊ̯ stɔːm./") == (
        "hiː wəz dɪˈteɪnd baɪ ə snəʊ stɔːm."
    )


def test_each_rule_toggleable() -> None:
    off = NormalizeConfig.from_config(
        {
            "british_vowels": False,
            "strip_optional_sounds": False,
            "strip_diacritics": False,
            "strip_monosyllable_stress": False,
        }
    )
    assert normalize_ipa("/ˈsasi/", off) == "ˈsasi"
    assert normalize_ipa("/ˈbɛlt/", off) == "ˈbɛlt"
    assert normalize_ipa("/fɪə(ɹ)/", off) == "fɪə(r)"
    assert normalize_ipa("/snəʊ̯/", off) == "snəʊ̯"
    on = NormalizeConfig.from_config({})
    assert on.british_vowels and on.strip_optional_sounds
    assert on.strip_diacritics and on.strip_monosyllable_stress
    # char map still applies before the vowel rules
    assert normalize_ipa("/ˈɡɑːtə ˈbɛlt/") == "ˈgɑːtə belt"


# --- weak forms -----------------------------------------------------------------

DICT = {"clock": "klɒk", "cook": "kʊk", "look": "lʊk", "looking": "ˈlʊkɪŋ", "what": "wɒt",
        "are": "ɑː", "you": "juː", "cease": "siːs", "desist": "dɪˈzɪst", "half": "hɑːf",
        "in": "ən", "a": "ʌ", "and": "ænd"}  # "in"/"a"/"and": odd first variants


def lookup(word: str) -> str | None:
    return DICT.get(word.lower())


def wf(text: str, punct: str = "", weak: bool = True) -> str:
    return compose(text, lookup, punct, function_words=FUNCTION_WORDS, weak_forms=weak).ipa


def test_weak_forms_mid_phrase_strong_when_final() -> None:
    assert wf("clock in") == "klɒk ɪn"
    assert wf("to cook up") == "tə kʊk ʌp"
    assert wf("cease and desist") == "siːs ənd dɪˈzɪst"
    assert wf("what are you looking at", "?") == "wɒt ɑː juː ˈlʊkɪŋ æt?"
    assert wf("looking at, you") == "ˈlʊkɪŋ æt, juː"  # clause-final before a comma
    assert wf("half a") == "hɑːf ə"  # articles never strong


def test_weak_forms_off_uses_strong_table_forms() -> None:
    assert wf("to cook up", weak=False) == "tuː kʊk ʌp"
    assert wf("clock in", weak=False) == "klɒk ɪn"


def test_table_skips_lookup_and_counts() -> None:
    calls: list[str] = []

    def counting(word: str) -> str | None:
        calls.append(word)
        return lookup(word)

    res = compose("A clock In the", counting, function_words=FUNCTION_WORDS)
    assert res.ipa == "ə klɒk ɪn ðə" and res.from_table == 3
    assert calls == ["clock"]


def test_single_word_never_uses_table() -> None:
    assert compose("in", lookup, function_words=FUNCTION_WORDS).ipa == "ən"
    assert compose("and", lookup).ipa == "ænd"


def test_no_table_by_default() -> None:
    assert compose("clock in", lookup).ipa == "klɒk ən"


# --- resolver end to end (raw data as cached from the live sources) ------------------

P = Pronunciations
RAW = {
    "sassy": P(uk=["/ˈsasi/"], us=["/ˈsæsi/"]),
    "clock": P(uk=["/klɒk/"], us=["/klɑk/"], other=["[kl̥ɒχ]"]),
    "in": P(other=["/ən/", "/ɪn/", "/ɘn/"]),
    "garter": P(uk=["/ˈɡɑːtə/"], us=["/ˈɡɑːɹtɚ/"]),
    "belt": P(other=["/ˈbɛlt/"]),
    "to": P(uk=["[tu̟ː]"], other=["/tuː/", "/tə/"]),
    "cook": P(other=["/kʊk/", "/kuːk/"]),
    "up": P(uk=["/ʌp/"], us=["/ʌp/"]),
    "Don't": P(uk=["/dəʊnt/"], us=["/doʊnt/"]),
    "interfere": P(uk=["/ˌɪn.təˈfɪə(ɹ)/"], us=["/ˌɪn.tɚˈfɪɚ/"]),
    "He": P(other=["/ˈhiː/", "/ˈhʌ/"]),
    "was": P(other=["/wəz/", "/wɒz/"]),
    "detained": P(other=["/dɪˈteɪnd/"]),
    "by": P(other=["/baɪ/"]),
    "a": P(uk=["/ʌ/", "/ɑ/", "/ə/"], us=["/ə/"]),
    "snow": P(uk=["/snəʊ̯/"], us=["/snoʊ̯/"]),
    "storm": P(uk=["/stɔːm/"], us=["/stɔɹm/"]),
    "show, don't tell": P(other=["/ʃəʊ dəʊnt tɛl/"]),
}


class RawSource:
    name = "fd"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def lookup(self, text: str) -> Pronunciations | None:
        self.calls.append(text)
        return RAW.get(text)


@pytest.mark.parametrize(
    "headword, ipa",
    [
        ("sassy", "ˈsæsi"),
        ("clock in", "klɒk ɪn"),
        ("garter belt", "ˈgɑːtə belt"),
        ("to cook up", "tə kʊk ʌp"),
        ("Don't interfere!", "dəʊnt ˌɪntəˈfɪə!"),
        ("He was detained by a snow storm.", "hiː wəz dɪˈteɪnd baɪ ə snəʊ stɔːm."),
        ("show, don't tell", "ʃəʊ dəʊnt tel"),
        ("a", "ʌ, ɑ"),  # single-word headword: dictionary, never the table
    ],
)
def test_resolver_style(headword: str, ipa: str) -> None:
    r = Resolver([RawSource()], Cache(None), NormalizeConfig())
    res = r.resolve(headword)
    assert res.ipa == ipa and res.complete


def test_resolver_skips_lookups_for_table_words() -> None:
    src = RawSource()
    Resolver([src], Cache(None)).resolve("He was detained by a snow storm.")
    assert not {"was", "by", "a"} & set(src.calls)


def test_resolver_table_only_phrase_is_builtin() -> None:
    res = Resolver([RawSource()], Cache(None)).resolve("out of")
    assert res.ipa == "aʊt ɒv" and res.source == "builtin" and res.complete


def test_build_resolver_weak_forms_flag() -> None:
    assert build_resolver({}).weak_forms is True
    r = build_resolver({"weak_forms": False, "strip_monosyllable_stress": False})
    assert r.weak_forms is False and not r.norm_cfg.strip_monosyllable_stress
