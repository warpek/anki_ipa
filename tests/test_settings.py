"""Pure logic of the Settings dialog and the "not set up" prompt (core.settings)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fakes import ENGLISH_WORDS

from anki_ipa.core.batch import Fields
from anki_ipa.core.settings import (
    NOT_SET_UP,
    Settings,
    guess_ipa_field,
    guess_note_type,
    guess_word_field,
    merge_settings,
    pick_fields,
    settings_from_config,
    setup_problem,
    setup_prompt,
    validate,
)

CONFIG_JSON = Path(__file__).resolve().parent.parent / "src" / "anki_ipa" / "config.json"
ENGLISH = ["English", "Polish", "Not-english", "Not-polish", "English-sound", "IPA-english"]


def defaults() -> dict[str, object]:
    data = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


# -- read / merge ------------------------------------------------------------------
def test_settings_from_default_config_are_empty() -> None:
    s = settings_from_config(defaults())
    assert s == Settings("", "", "", "uk", False, "")


def test_settings_from_config_tolerates_junk() -> None:
    s = settings_from_config(
        {"note_type": None, "accent_preference": "uk", "claude": "nope", "word_field": 3}
    )
    assert s == Settings("", "3", "", "uk", False, "")
    assert settings_from_config({"accent_preference": ["US", "uk"]}).accent_first == "us"
    assert settings_from_config({"accent_preference": ["au"]}).accent_first == "uk"


def test_merge_preserves_unknown_keys_and_claude_extras() -> None:
    cfg = defaults()
    cfg["my_own_key"] = {"x": 1}
    cfg["claude"] = {"enabled": False, "api_key": "", "model": "m-1", "for_partial": False}
    s = Settings("Vocab", "Word", "IPA", "uk", True, "sk-test")
    out = merge_settings(cfg, s)
    assert out["my_own_key"] == {"x": 1}
    assert (out["note_type"], out["word_field"], out["ipa_field"]) == ("Vocab", "Word", "IPA")
    assert out["claude"] == {
        "enabled": True, "api_key": "sk-test", "model": "m-1", "for_partial": False
    }
    assert out["sources"] == cfg["sources"] and out["weak_forms"] is True
    # the input is not modified
    assert cfg["note_type"] == "" and cfg["claude"]["enabled"] is False  # type: ignore[index]
    assert settings_from_config(out) == s


def test_merge_accent_only_rewritten_when_changed() -> None:
    cfg = {"accent_preference": ["uk"]}  # hand-edited: UK only
    assert merge_settings(cfg, Settings(accent_first="uk"))["accent_preference"] == ["uk"]
    assert merge_settings(cfg, Settings(accent_first="us"))["accent_preference"] == ["us", "uk"]
    assert merge_settings({}, Settings(accent_first="uk"))["accent_preference"] == ["uk", "us"]
    assert merge_settings({}, Settings())["claude"] == {"enabled": False, "api_key": ""}


# -- preselection ----------------------------------------------------------------
@pytest.mark.parametrize(
    ("fields", "ipa"),
    [
        (ENGLISH, "IPA-english"),
        (["Front", "Back", "Pronunciation"], "Pronunciation"),
        (["Word", "Phonetic spelling"], "Phonetic spelling"),
        (["Front", "Back"], ""),
        (["ipa"], "ipa"),
    ],
)
def test_guess_ipa_field(fields: list[str], ipa: str) -> None:
    assert guess_ipa_field(fields) == ipa


def test_guess_word_field_is_first_non_ipa_field() -> None:
    assert guess_word_field(ENGLISH) == "English"
    assert guess_word_field(["IPA", "Word"]) == "Word"
    assert guess_word_field(["IPA"], exclude="IPA") == ""
    assert guess_word_field(["IPA", "Pronunciation"], exclude="IPA") == "Pronunciation"
    assert guess_word_field([]) == ""


def test_guess_note_type() -> None:
    types = {"Basic": ["Front", "Back"], "English words": ENGLISH, "Only IPA": ["IPA"]}
    assert guess_note_type(types) == "English words"
    assert guess_note_type({"Basic": ["Front", "Back"]}) == ""
    assert guess_note_type({"Only IPA": ["IPA"]}) == ""


def test_pick_fields_prefers_configured_then_guesses() -> None:
    conf = Settings("English words", "Polish", "English-sound")
    assert pick_fields(ENGLISH, conf, "English words") == ("Polish", "English-sound")
    # another note type: guessed
    assert pick_fields(["Front", "Back", "IPA"], conf, "Vocab") == ("Front", "IPA")
    # configured fields gone: guessed again
    gone = Settings("English words", "Headword", "Transcript")
    assert pick_fields(ENGLISH, gone, "English words") == ("English", "IPA-english")
    # nothing configured, no IPA-like field
    assert pick_fields(["Front", "Back"], Settings(), "Basic") == ("Front", "")
    # configured word == configured IPA (hand-edited JSON): IPA re-guessed
    same = Settings("English words", "IPA-english", "IPA-english")
    assert pick_fields(ENGLISH, same, "English words") == ("IPA-english", "")


# -- validation --------------------------------------------------------------------
def test_validate() -> None:
    ok = Settings("English words", "English", "IPA-english")
    assert validate(ok, ENGLISH) is None
    assert validate(Settings(), None) == "Choose a note type."
    assert validate(ok, None) is not None
    assert validate(Settings("English words", "English", ""), ENGLISH) is not None
    assert "different" in (validate(Settings("English words", "English", "English"), ENGLISH) or "")
    assert "Nope" in (validate(Settings("English words", "Nope", "IPA-english"), ENGLISH) or "")


def test_setup_problem() -> None:
    assert setup_problem(Fields(), None) == NOT_SET_UP
    assert setup_problem(Fields(), ENGLISH) == NOT_SET_UP
    assert setup_problem(ENGLISH_WORDS, ENGLISH) is None
    msg = setup_problem(ENGLISH_WORDS, None)
    assert msg is not None and '"English words" was not found' in msg
    msg = setup_problem(ENGLISH_WORDS, ["English", "Polish"])
    assert msg is not None and '"IPA-english"' in msg and '"English"' not in msg.split("field")[1]
    same = Fields(note_type="English words", word="English", ipa="English")
    assert "both" in (setup_problem(same, ENGLISH) or "")
    assert setup_prompt(NOT_SET_UP).startswith(NOT_SET_UP) and "settings" in setup_prompt("x")
