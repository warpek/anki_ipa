"""Pure batch logic for "Fill missing IPA" (no aqt / anki imports).

The Qt flow in `ui.py` and the collection ops in `ops.py` stay thin; the
decisions live here so they are unit-testable:

- `build_search(config)`: Browser search for notes that need IPA.
- `field_is_empty(value)` / `needs_fill(...)`: per-note candidate check
  (used for selected notes and re-checked right before writing).
- `unique_words(items)`: distinct headwords to resolve (one lookup each).
- `build_plan(items, results_by_word, config)`: `{nid: Planned(ipa, tags)}`.
- `Summary` + `summary_text(...)`: the end-of-run report.
- `check_editor_note`, `plan_single`, `editor_message`: the editor button
  (one note, same candidate rules, plan and tags as the batch).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection as AbstractCollection
import html
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .clean import strip_markup
from .resolver import BUILTIN_SOURCE, Result

DEFAULT_NOTE_TYPE = "English words"
DEFAULT_WORD_FIELD = "English"
DEFAULT_IPA_FIELD = "IPA-english"
DEFAULT_TAG_AUTO = "ipa::auto"
DEFAULT_TAG_LLM = "ipa::llm"
DEFAULT_TAG_PARTIAL = "ipa::partial"

# `Result.source` values that mean a language model produced the IPA (the
# Claude fallback); such notes get `tag_llm` on top of `tag_auto`.
LLM_SOURCES = frozenset({"claude"})

SOURCE_LABELS = {
    "freedictionary": "FreeDictionaryAPI.com",
    "wiktionary": "Wiktionary",
    "claude": "Claude",
    "mixed": "mixed (word by word, several sources)",
    BUILTIN_SOURCE: "built-in table (function words, overrides)",
}

Item = tuple[int, str]  # (note id, raw word-field value)


@dataclass(frozen=True)
class Fields:
    """The add-on's field / tag names from config, with defaults."""

    note_type: str = DEFAULT_NOTE_TYPE
    word: str = DEFAULT_WORD_FIELD
    ipa: str = DEFAULT_IPA_FIELD
    tag_auto: str = DEFAULT_TAG_AUTO
    tag_llm: str = DEFAULT_TAG_LLM
    tag_partial: str = DEFAULT_TAG_PARTIAL
    overwrite: bool = False

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> Fields:
        def s(key: str, default: str) -> str:
            v = config.get(key, default)
            return str(v).strip() if v is not None else default

        return cls(
            note_type=s("note_type", DEFAULT_NOTE_TYPE),
            word=s("word_field", DEFAULT_WORD_FIELD),
            ipa=s("ipa_field", DEFAULT_IPA_FIELD),
            tag_auto=s("tag_auto", DEFAULT_TAG_AUTO),
            tag_llm=s("tag_llm", DEFAULT_TAG_LLM),
            tag_partial=s("tag_partial", DEFAULT_TAG_PARTIAL),
            overwrite=bool(config.get("overwrite_existing", False)),
        )

    def search(self) -> str:
        """`"note:<type>" "<word>:_*" "<ipa>:"` (no empty-IPA term when overwriting)."""
        parts = [f'"note:{self.note_type}"', f'"{self.word}:_*"']
        if not self.overwrite:
            parts.append(f'"{self.ipa}:"')
        return " ".join(parts)


def build_search(config: Mapping[str, Any]) -> str:
    """Browser search for notes that need IPA (see `Fields.search`)."""
    return Fields.from_config(config).search()


def field_is_empty(value: str) -> bool:
    """True for "", whitespace, `&nbsp;`, `<br>`, `<div></div>` and similar."""
    return not strip_markup(value).strip()


# Why a note is not a candidate (counted for the "nothing to do" tooltip).
SKIP_NOTE_TYPE = "other note type"
SKIP_FILLED = "IPA already filled"
SKIP_NO_WORD = "empty headword"


def skip_reason(
    note_type_name: str, fields: Mapping[str, str], fcfg: Fields
) -> str | None:
    """None if the note should be filled, else the reason to skip it."""
    if note_type_name != fcfg.note_type or fcfg.word not in fields or fcfg.ipa not in fields:
        return SKIP_NOTE_TYPE
    if field_is_empty(fields[fcfg.word]):
        return SKIP_NO_WORD
    if not fcfg.overwrite and not field_is_empty(fields[fcfg.ipa]):
        return SKIP_FILLED
    return None


def needs_fill(note_type_name: str, fields: Mapping[str, str], fcfg: Fields) -> bool:
    return skip_reason(note_type_name, fields, fcfg) is None


def unique_words(items: Sequence[Item]) -> list[str]:
    """Distinct raw headwords in first-seen order (each is resolved once)."""
    return list(dict.fromkeys(word for _, word in items))


@dataclass(frozen=True)
class Planned:
    ipa: str
    tags: tuple[str, ...]
    source: str
    partial: bool


def tags_for(result: Result, fcfg: Fields) -> tuple[str, ...]:
    tags = [fcfg.tag_auto]
    if result.source in LLM_SOURCES:
        tags.append(fcfg.tag_llm)
    if not result.complete:
        tags.append(fcfg.tag_partial)
    return tuple(t for t in dict.fromkeys(tags) if t)


def build_plan(
    items: Sequence[Item], results: Mapping[str, Result], fcfg: Fields
) -> dict[int, Planned]:
    """`{nid: Planned}` for every item whose headword resolved to some IPA.

    Partial results (`[word]` placeholders, `complete=False`) are written too
    and get `tag_partial`. Items missing from `results` (cancelled before
    their turn) or resolved to None are left out.
    """
    plan: dict[int, Planned] = {}
    for nid, word in items:
        res = results.get(word)
        if res is None or not res.ipa:
            continue
        plan[nid] = Planned(
            ipa=res.ipa,
            tags=tags_for(res, fcfg),
            source=res.source or "unknown",
            partial=not res.complete,
        )
    return plan


@dataclass
class Summary:
    total: int  # candidate notes
    filled_by_source: Counter[str] = field(default_factory=Counter)
    partial: int = 0
    missed: list[str] = field(default_factory=list)  # cleaned headwords (distinct)
    missed_notes: int = 0
    not_processed: int = 0  # notes whose headword was not reached (cancel)
    skipped_changed: int = 0  # filled/changed between collect and write
    disabled: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False
    words_total: int = 0
    words_done: int = 0
    llm_status: str | None = None  # None: no LLM fallback configured
    llm_filled: list[tuple[str, str]] = field(default_factory=list)  # (headword, ipa)

    @property
    def filled(self) -> int:
        return sum(self.filled_by_source.values())


def summarize(
    items: Sequence[Item],
    results: Mapping[str, Result],
    plan: Mapping[int, Planned],
    written: AbstractCollection[int],
    *,
    words_total: int,
    cancelled: bool,
    disabled: Mapping[str, str] | None = None,
    errors: Sequence[str] = (),
    llm_status: str | None = None,
) -> Summary:
    """Counts per note. `written` = nids actually updated by the write op."""
    s = Summary(
        total=len(items),
        disabled=dict(disabled or {}),
        errors=list(errors),
        cancelled=cancelled,
        words_total=words_total,
        words_done=len(results),
        llm_status=llm_status,
    )
    missed: list[str] = []
    for nid, word in items:
        if word not in results:
            s.not_processed += 1
            continue
        planned = plan.get(nid)
        if planned is None:
            s.missed_notes += 1
            shown = strip_markup(word).strip()
            if shown not in missed:
                missed.append(shown)
            continue
        if nid not in written:
            s.skipped_changed += 1
            continue
        s.filled_by_source[planned.source] += 1
        if planned.partial:
            s.partial += 1
        if planned.source in LLM_SOURCES:
            entry = (strip_markup(word).strip(), planned.ipa)
            if entry not in s.llm_filled:
                s.llm_filled.append(entry)
    s.missed = missed
    return s


def summary_text(s: Summary, fcfg: Fields | None = None) -> str:
    fcfg = fcfg or Fields()
    lines: list[str] = []
    head = f"Notes processed: {s.total - s.not_processed} of {s.total}"
    if s.cancelled:
        head += f" (CANCELLED after {s.words_done} of {s.words_total} headwords)"
    lines.append(head)
    lines.append(f"Filled: {s.filled}")
    for src, n in sorted(s.filled_by_source.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"    {SOURCE_LABELS.get(src, src)}: {n}")
    lines.append(f"    of which partial ([word] placeholders, tagged {fcfg.tag_partial}): {s.partial}")
    lines.append(f"Missed: {s.missed_notes}")
    if s.not_processed:
        lines.append(f"Not processed (cancelled): {s.not_processed}")
    if s.skipped_changed:
        lines.append(f"Skipped (IPA filled or note changed meanwhile): {s.skipped_changed}")
    if s.filled:
        lines.append(f"Filled notes are tagged {fcfg.tag_auto}. Edit > Undo reverts the whole batch.")
    if s.llm_status is not None:
        lines.append(f"Claude fallback: {s.llm_status}")
    # The LLM fallback's own state is on the line above.
    disabled = {
        k: v for k, v in s.disabled.items() if s.llm_status is None or k not in LLM_SOURCES
    }
    if disabled:
        lines.append("")
        lines.append("Sources disabled during the run:")
        for name, reason in disabled.items():
            lines.append(f"    {SOURCE_LABELS.get(name, name)}: {reason}")
    if s.errors:
        lines.append("")
        lines.append(f"Unexpected source errors ({len(s.errors)}), treated as misses:")
        lines += [f"    {e}" for e in s.errors[:10]]
        if len(s.errors) > 10:
            lines.append(f"    ... and {len(s.errors) - 10} more")
    if s.llm_filled:
        lines.append("")
        lines.append(
            f"Filled by Claude ({len(s.llm_filled)}), please review "
            f"(Browser search: tag:{fcfg.tag_llm}):"
        )
        lines += [f"    {w}  →  {ipa}" for w, ipa in s.llm_filled]
    if s.missed:
        lines.append("")
        lines.append(f"Missed headwords ({len(s.missed)}):")
        lines += [f"    {w}" for w in s.missed]
    return "\n".join(lines)


# -- editor button (one note) --------------------------------------------------
@dataclass(frozen=True)
class EditorCheck:
    """Outcome of `check_editor_note` for the note open in the editor."""

    problem: str | None = None  # tooltip text when the button cannot act
    word: str = ""  # raw word-field value to resolve
    current_ipa: str = ""  # cleaned non-empty IPA already there: ask first

    @property
    def needs_confirm(self) -> bool:
        return bool(self.current_ipa)


def check_editor_note(
    note_type_name: str, fields: Mapping[str, str], fcfg: Fields
) -> EditorCheck:
    """The batch's `skip_reason`, except that a filled IPA field is not a
    skip but a question for the user (`needs_confirm`)."""
    reason = skip_reason(note_type_name, fields, replace(fcfg, overwrite=True))
    if reason == SKIP_NOTE_TYPE:
        if note_type_name != fcfg.note_type:
            return EditorCheck(
                f"IPA: only for notes of type \"{html.escape(fcfg.note_type)}\" "
                f"(this one is \"{html.escape(note_type_name)}\"; see note_type in the add-on config)."
            )
        missing = [f for f in (fcfg.word, fcfg.ipa) if f not in fields]
        return EditorCheck(
            f"IPA: the note type has no field {', '.join(html.escape(m) for m in missing)} "
            "(see word_field / ipa_field in the add-on config)."
        )
    if reason == SKIP_NO_WORD:
        return EditorCheck(f"IPA: the {html.escape(fcfg.word)} field is empty.")
    ipa = fields[fcfg.ipa]
    current = "" if field_is_empty(ipa) else strip_markup(ipa).strip()
    return EditorCheck(None, fields[fcfg.word], current)


def confirm_overwrite_text(check: EditorCheck, fcfg: Fields) -> str:
    return (
        f"The {fcfg.ipa} field already contains:\n\n    {check.current_ipa}\n\n"
        "Replace it with the looked-up IPA?"
    )


def plan_single(nid: int, word: str, result: Result, fcfg: Fields) -> Planned | None:
    """`build_plan` for one note: None when nothing usable was found."""
    return build_plan([(nid, word)], {word: result}, fcfg).get(nid)


def editor_message(word: str, planned: Planned | None, fcfg: Fields) -> str:
    """Tooltip after the editor button ran (HTML-escaped)."""
    shown = html.escape(strip_markup(word).strip())
    if planned is None:
        return f"IPA: nothing found for \u201c{shown}\u201d."
    label = SOURCE_LABELS.get(planned.source, planned.source)
    msg = f"IPA: {html.escape(planned.ipa)} ({html.escape(label)})"
    if planned.partial:
        msg += f"<br>Partial: [word] = not found; tagged {html.escape(fcfg.tag_partial)}."
    if planned.source in LLM_SOURCES:
        msg += f"<br>Written by Claude; tagged {html.escape(fcfg.tag_llm)}, please check."
    return msg
