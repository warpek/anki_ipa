"""Pure logic behind the editor button (core.batch) and the word overrides."""

from __future__ import annotations

from anki_ipa.core.batch import (
    EditorCheck,
    Fields,
    Planned,
    check_editor_note,
    confirm_overwrite_text,
    editor_message,
    plan_single,
)
from anki_ipa.core.cache import Cache
from anki_ipa.core.resolver import WORD_OVERRIDES, Resolver, Result
from anki_ipa.core.sources.base import Pronunciations

F = Fields()


def fields(word: str = "cat", ipa: str = "") -> dict[str, str]:
    return {"English": word, "Polish": "kot", "IPA-english": ipa}


# -- check_editor_note -----------------------------------------------------------
def test_check_ok_returns_raw_word() -> None:
    c = check_editor_note("English words", fields("<div>dog&nbsp;</div>"), F)
    assert c == EditorCheck(None, "<div>dog&nbsp;</div>", "")
    assert not c.needs_confirm


def test_check_other_note_type() -> None:
    c = check_editor_note("Basic", {"Front": "cat", "Back": ""}, F)
    assert c.problem is not None and '"English words"' in c.problem and '"Basic"' in c.problem


def test_check_missing_field_on_right_type() -> None:
    c = check_editor_note("English words", {"English": "cat"}, F)
    assert c.problem is not None and "IPA-english" in c.problem


def test_check_other_type_name_is_escaped() -> None:
    c = check_editor_note("<b>x</b>", {}, F)
    assert c.problem is not None and "<b>" not in c.problem


def test_check_empty_word() -> None:
    c = check_editor_note("English words", fields(" &nbsp;<br>"), F)
    assert c.problem == "IPA: the English field is empty."


def test_check_filled_ipa_needs_confirm_even_without_overwrite() -> None:
    c = check_editor_note("English words", fields("record", "<b>ˈrekɔːd</b>"), F)
    assert c.problem is None and c.needs_confirm and c.current_ipa == "ˈrekɔːd"
    text = confirm_overwrite_text(c, F)
    assert "IPA-english" in text and "ˈrekɔːd" in text


def test_check_markup_only_ipa_is_empty() -> None:
    c = check_editor_note("English words", fields("cat", "&nbsp;<br>"), F)
    assert c.problem is None and not c.needs_confirm


def test_check_uses_configured_names() -> None:
    f = Fields(note_type="Vocab", word="Word", ipa="IPA")
    assert check_editor_note("Vocab", {"Word": "cat", "IPA": ""}, f).problem is None
    assert check_editor_note("English words", fields(), f).problem is not None


# -- plan_single / editor_message ----------------------------------------------
def test_plan_single_tags_like_batch() -> None:
    p = plan_single(5, "w", Result("kæt", "freedictionary", True), F)
    assert p == Planned("kæt", ("ipa::auto",), "freedictionary", False)
    p = plan_single(0, "w", Result("hiː [x]", "mixed", False, ["x"]), F)
    assert p is not None and p.tags == ("ipa::auto", "ipa::partial") and p.partial
    p = plan_single(0, "w", Result("ɪnsəˈmaʊntəbl", "claude", True), F)
    assert p is not None and p.tags == ("ipa::auto", "ipa::llm")
    assert plan_single(1, "w", Result(None, None, False, ["w"]), F) is None


def test_editor_message() -> None:
    assert editor_message("<i>qwzxv</i>", None, F) == "IPA: nothing found for “qwzxv”."
    ok = editor_message("cat", Planned("kæt", ("ipa::auto",), "freedictionary", False), F)
    assert ok == "IPA: kæt (FreeDictionaryAPI.com)"
    part = editor_message("x", Planned("a [b]", ("ipa::auto", "ipa::partial"), "mixed", True), F)
    assert "ipa::partial" in part
    llm = editor_message("x", Planned("ɪn", ("ipa::auto", "ipa::llm"), "claude", False), F)
    assert "(Claude)" in llm and "ipa::llm" in llm


# -- WORD_OVERRIDES (until) ------------------------------------------------------
UNTIL = Pronunciations(uk=["/ʌnˈtɪl/", "/ənˈtɪl/", "/ʊnˈtɪl/"], us=["/ʌnˈtɪl/"], other=[])


class Src:
    name = "freedictionary"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def lookup(self, text: str) -> Pronunciations | None:
        self.calls.append(text)
        return {"until": UNTIL, "wait": Pronunciations(uk=["/weɪt/"])}.get(text)


def test_until_override_single_word_without_lookup() -> None:
    assert WORD_OVERRIDES["until"] == "ənˈtɪl"
    src = Src()
    res = Resolver([src], Cache(None)).resolve("Until")
    assert res == Result("ənˈtɪl", "builtin", True, [])
    assert src.calls == []


def test_until_override_inside_phrase() -> None:
    src = Src()
    res = Resolver([src], Cache(None)).resolve("wait until")
    assert res.ipa == "weɪt ənˈtɪl" and res.complete and res.source == "mixed"
    assert "until" not in src.calls


def test_overrides_can_be_disabled() -> None:
    res = Resolver([Src()], Cache(None), overrides={}).resolve("until")
    assert res.ipa == "ʌnˈtɪl, ənˈtɪl" and res.source == "freedictionary"
