import pytest

from anki_ipa.core.clean import Headword, clean_headword, strip_markup
from anki_ipa.core.compose import (
    Token,
    compose,
    join_alternatives,
    tokenize,
    with_punct,
    words,
)
from anki_ipa.core.normalize import (
    NormalizeConfig,
    is_fragment,
    normalize_ipa,
    strip_delimiters,
)

# --- clean -----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, text",
    [
        ("rank&nbsp;", "rank"),
        ("rank&amp;nbsp;", "rank"),
        ("<div>insurmountable</div>", "insurmountable"),
        ("nuisance&amp;nbsp;", "nuisance"),
        ("chute ", "chute"),
        ("clock<br>in", "clock in"),
        ("<div>cease and</div><div>desist</div>", "cease and desist"),
        ("inter<b>est</b>", "interest"),
        ("record[sound:rec_123.mp3]", "record"),
        ("  clock    in ", "clock in"),
    ],
)
def test_clean_junk(raw: str, text: str) -> None:
    hw = clean_headword(raw)
    assert hw.text == text
    assert hw.trailing_punct == ""


def test_strip_markup_quotes_and_ellipsis() -> None:
    assert strip_markup("You’ve “got” it…") == 'You\'ve "got" it...'


def test_single_word() -> None:
    hw = clean_headword("chute ")
    assert hw == Headword(raw="chute ", text="chute", alternatives=("chute",), full_text="chute")
    assert not hw.is_phrase and not hw.is_sentence and not hw.has_alternatives


@pytest.mark.parametrize(
    "raw, text, annotations",
    [
        ("apparel (formal)", "apparel", ("formal",)),
        ("elaborate (verb)", "elaborate", ("verb",)),
        ("record (noun) (formal)", "record", ("noun", "formal")),
    ],
)
def test_annotations(raw: str, text: str, annotations: tuple[str, ...]) -> None:
    hw = clean_headword(raw)
    assert (hw.text, hw.annotations, hw.optional) == (text, annotations, ())
    assert hw.alternatives == (text,)


def test_optional_words_dropped_for_lookup() -> None:
    hw = clean_headword("to give a run for (your) money")
    assert hw.text == "to give a run for money"
    assert hw.full_text == "to give a run for your money"
    assert hw.optional == ("your",)
    assert hw.annotations == ()
    assert hw.is_phrase


def test_only_parenthetical_falls_back_to_contents() -> None:
    assert clean_headword("(formal)").text == "formal"


@pytest.mark.parametrize(
    "raw, text, punct",
    [
        ("Don't interfere!", "Don't interfere", "!"),
        ("He was detained by a snow storm.", "He was detained by a snow storm", "."),
        ("You’ve scarcely changed.", "You've scarcely changed", "."),
        ("Are you sure?!", "Are you sure", "?!"),
        ("I was wondering…", "I was wondering", "..."),
        ("Stop it! (informal)", "Stop it", "!"),
        ("<div>Don&#x27;t interfere!&nbsp;</div>", "Don't interfere", "!"),
    ],
)
def test_sentences(raw: str, text: str, punct: str) -> None:
    hw = clean_headword(raw)
    assert (hw.text, hw.trailing_punct) == (text, punct)
    assert hw.is_sentence and hw.alternatives == (text,)


def test_alternatives() -> None:
    hw = clean_headword("adjustor, adjuster")
    assert hw.alternatives == ("adjustor", "adjuster")
    assert hw.has_alternatives and not hw.is_phrase
    assert clean_headword("grey , gray, grey").alternatives == ("grey", "gray")


@pytest.mark.parametrize(
    "raw", ["show, don't tell", "Yes, sir.", "well-off, well-to-do people"]
)
def test_phrase_commas_are_not_alternatives(raw: str) -> None:
    hw = clean_headword(raw)
    assert not hw.has_alternatives
    assert hw.alternatives == (hw.text,)


def test_multiword_phrases() -> None:
    for raw in ("cease and desist", "clock in"):
        hw = clean_headword(raw)
        assert hw.text == raw and hw.is_phrase and not hw.is_sentence


def test_trailing_comma_junk_and_empty() -> None:
    assert clean_headword("rank,").text == "rank"
    empty = clean_headword("<div>&nbsp;</div>")
    assert empty.is_empty and empty.alternatives == ()


# --- normalize -------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, out",
    [
        ("/ˈɹɛk.ɔːd/", "ˈrekɔːd"),
        ("[ˈæpl̩]", "ˈæpl"),  # syllabic mark stripped, still 2 nuclei
        ("/ˈɡʊd/", "gʊd"),  # monosyllable: stress dropped
        ("/t͡ʃiːz/", "tʃiːz"),
        ("  /aɪs   kɹiːm/ ", "aɪs kriːm"),
        ("ˈæp.əl", "ˈæpəl"),
    ],
)
def test_normalize(raw: str, out: str) -> None:
    assert normalize_ipa(raw) == out


def test_normalize_keeps_final_punct_and_config() -> None:
    assert normalize_ipa("tʃeɪndʒd.") == "tʃeɪndʒd."
    cfg = NormalizeConfig.from_config(
        {"normalize_map": {}, "strip_syllable_dots": False, "british_vowels": False}
    )
    assert normalize_ipa("/ˈɹɛk.ɔːd/", cfg) == "ˈɹɛk.ɔːd"
    full = NormalizeConfig.from_config({"normalize_map": {"ɹ": "r", "ɡ": "g"}})
    assert full.strip_syllable_dots and dict(full.char_map) == {"ɹ": "r", "ɡ": "g"}


def test_strip_delimiters_only_outer_pair() -> None:
    assert strip_delimiters("ðə [word] ɪz") == "ðə [word] ɪz"
    assert strip_delimiters("[a] [b]") == "[a] [b]"
    assert strip_delimiters("/a/, /b/") == "/a/, /b/"


@pytest.mark.parametrize("ipa", ["/ɹə-/", "-ɪŋ", "/-ɪŋ/", "", "//", "–ɪŋ"])
def test_fragments(ipa: str) -> None:
    assert is_fragment(ipa)


def test_not_fragment() -> None:
    assert not is_fragment("/ˈwel-ˈnəʊn/")
    assert not is_fragment("ɹiːd")


# --- tokenize / compose ----------------------------------------------------


def test_tokenize() -> None:
    assert tokenize("show, don't tell!") == [
        Token("show", True),
        Token(",", False),
        Token("don't", True),
        Token("tell", True),
        Token("!", False),
    ]
    assert words('You\'ve a well-known "o\'clock" (x)') == [
        "You've",
        "a",
        "well-known",
        "o'clock",
        "x",
    ]


IPA = {
    "you've": "juːv",
    "scarcely": "ˈskeəsli",
    "changed": "tʃeɪndʒd",
    "show": "ʃəʊ",
    "don't": "dəʊnt",
    "tell": "tel",
    "cease": "siːs",
    "desist": "dɪˈzɪst",
}


def lookup(word: str) -> str | None:
    return IPA.get(word.lower())


def test_compose_sentence_complete() -> None:
    hw = clean_headword("You’ve scarcely changed.")
    res = compose(hw.text, lookup, hw.trailing_punct)
    assert res.ipa == "juːv ˈskeəsli tʃeɪndʒd."
    assert res.complete and res.missing == ()


def test_compose_partial_with_placeholders() -> None:
    res = compose("cease and desist", lookup)
    assert res.ipa == "siːs [and] dɪˈzɪst"
    assert not res.complete and res.missing == ("and",)


def test_compose_keeps_inner_punct_and_dedupes_lookups() -> None:
    calls: list[str] = []

    def counting(word: str) -> str | None:
        calls.append(word)
        return lookup(word)

    res = compose("show, don't tell, tell", counting, "!")
    assert res.ipa == "ʃəʊ, dəʊnt tel, tel!"
    assert calls == ["show", "don't", "tell"]


def test_compose_placeholder_keeps_case_and_empty() -> None:
    assert compose("Xyzzy", lookup, ".").ipa == "[Xyzzy]."
    empty = compose("", lookup, ".")
    assert empty.ipa == "" and empty.complete


def test_helpers() -> None:
    assert with_punct("ˈrænk", "!") == "ˈrænk!"
    assert with_punct("", "!") == ""
    assert join_alternatives(["əˈdʒʌstə", "", "əˈdʒʌstə", "əˈdʒʌstər"]) == (
        "əˈdʒʌstə, əˈdʒʌstər"
    )
