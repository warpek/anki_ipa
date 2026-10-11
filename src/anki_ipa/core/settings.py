"""Pure logic behind the Settings dialog and the "not set up" prompt.

No aqt / anki imports: the dialog (`settings_dialog.py`) and the collection
helpers (`ops.py`) pass plain names in, so this is unit-testable.

- `Settings` / `settings_from_config` / `merge_settings`: the handful of keys
  the dialog edits, read from and merged back over the full config dict
  (unknown keys, and the rest of the `claude` dict, are preserved).
- `guess_note_type`, `pick_fields`: what the dialog preselects.
- `validate`: why OK is disabled (None = valid).
- `setup_problem`: why the Browser action / editor button cannot run yet.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .batch import Fields

ACCENTS = ("uk", "us")
ACCENT_ORDERS = {"uk": ["uk", "us"], "us": ["us", "uk"]}

# Field names that look like an IPA / pronunciation field (case-insensitive).
_IPA_NAME = re.compile(r"ipa|pronunciation|pronunc|phonetic|transcription", re.IGNORECASE)


@dataclass(frozen=True)
class Settings:
    """The config keys the Settings dialog edits."""

    note_type: str = ""
    word_field: str = ""
    ipa_field: str = ""
    accent_first: str = "uk"  # "uk" | "us"
    claude_enabled: bool = False
    claude_api_key: str = ""


def _s(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def settings_from_config(config: Mapping[str, Any]) -> Settings:
    prefs = config.get("accent_preference")
    first = "uk"
    if isinstance(prefs, list) and prefs and str(prefs[0]).lower() in ACCENTS:
        first = str(prefs[0]).lower()
    claude = config.get("claude")
    claude = claude if isinstance(claude, Mapping) else {}
    return Settings(
        note_type=_s(config.get("note_type")),
        word_field=_s(config.get("word_field")),
        ipa_field=_s(config.get("ipa_field")),
        accent_first=first,
        claude_enabled=bool(claude.get("enabled", False)),
        claude_api_key=_s(claude.get("api_key")),
    )


def merge_settings(config: Mapping[str, Any], s: Settings) -> dict[str, Any]:
    """A copy of `config` with the dialog's values written over it.

    Every other key survives untouched, including the other entries of the
    `claude` dict (`model`, `for_partial`, ...). The accent list is only
    replaced when its first entry changes, so a hand-edited list such as
    `["uk"]` survives a save that did not touch the accent.
    """
    out: dict[str, Any] = copy.deepcopy(dict(config))
    out["note_type"] = s.note_type
    out["word_field"] = s.word_field
    out["ipa_field"] = s.ipa_field
    old = settings_from_config(config)
    prefs = config.get("accent_preference")
    if not isinstance(prefs, list) or not prefs or old.accent_first != s.accent_first:
        out["accent_preference"] = list(ACCENT_ORDERS[s.accent_first])
    claude = out.get("claude")
    claude = dict(claude) if isinstance(claude, Mapping) else {}
    claude["enabled"] = s.claude_enabled
    claude["api_key"] = s.claude_api_key
    out["claude"] = claude
    return out


def is_ipa_like(name: str) -> bool:
    return bool(_IPA_NAME.search(name))


def guess_ipa_field(field_names: Sequence[str], exclude: str = "") -> str:
    """First IPA-looking field name other than `exclude`, else ""."""
    return next((f for f in field_names if f != exclude and is_ipa_like(f)), "")


def guess_word_field(field_names: Sequence[str], exclude: str = "") -> str:
    """The first field that does not look like IPA (Anki's sort field is
    usually the headword), else the first field other than `exclude`."""
    for f in field_names:
        if f != exclude and not is_ipa_like(f):
            return f
    return next((f for f in field_names if f != exclude), "")


def guess_note_type(note_types: Mapping[str, Sequence[str]]) -> str:
    """The first note type (in the given order) with an IPA-looking field and
    at least one other field, else ""."""
    for name, fields in note_types.items():
        ipa = guess_ipa_field(fields)
        if ipa and guess_word_field(fields, exclude=ipa):
            return name
    return ""


def pick_fields(
    field_names: Sequence[str], configured: Settings, note_type: str
) -> tuple[str, str]:
    """(word field, IPA field) to preselect for `note_type`.

    The configured fields win when `note_type` is the configured note type
    and they still exist; anything else is guessed (IPA field by name, word
    field = first other field). "" means "nothing to preselect".
    """
    word = ipa = ""
    if note_type == configured.note_type:
        if configured.word_field in field_names:
            word = configured.word_field
        if configured.ipa_field in field_names and configured.ipa_field != word:
            ipa = configured.ipa_field
    if not ipa:
        ipa = guess_ipa_field(field_names, exclude=word)
    if not word:
        word = guess_word_field(field_names, exclude=ipa)
    return word, ipa


def validate(s: Settings, field_names: Sequence[str] | None) -> str | None:
    """Why the dialog's values cannot be saved, or None.

    `field_names` are the fields of `s.note_type` (None: no such note type).
    """
    if not s.note_type:
        return "Choose a note type."
    if field_names is None:
        return f'Note type "{s.note_type}" does not exist.'
    if not s.word_field or not s.ipa_field:
        return "Choose the word field and the IPA field."
    for f in (s.word_field, s.ipa_field):
        if f not in field_names:
            return f'The note type has no field "{f}".'
    if s.word_field == s.ipa_field:
        return "The word field and the IPA field must be different."
    return None


NOT_SET_UP = "IPA Filler isn't set up yet: choose the note type and its word and IPA fields."


def setup_problem(fcfg: Fields, field_names: Sequence[str] | None) -> str | None:
    """Why the configured note type / fields cannot be used, or None.

    `field_names`: the fields of `fcfg.note_type` in the collection (None when
    there is no note type of that exact name).
    """
    if not fcfg.is_configured:
        return NOT_SET_UP
    if field_names is None:
        return f'IPA Filler: note type "{fcfg.note_type}" was not found in this collection.'
    missing = [f for f in (fcfg.word, fcfg.ipa) if f not in field_names]
    if missing:
        names = ", ".join(f'"{m}"' for m in missing)
        return f'IPA Filler: note type "{fcfg.note_type}" has no field {names}.'
    if fcfg.word == fcfg.ipa:
        return f'IPA Filler: the word field and the IPA field are both "{fcfg.word}".'
    return None


def setup_prompt(problem: str) -> str:
    return f"{problem}\n\nOpen the IPA Filler settings now?"
