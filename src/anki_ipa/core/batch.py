"""Pure batch logic for "Fill missing IPA" (no aqt / anki imports).

The Qt flow in `ui.py` and the collection ops in `ops.py` stay thin; the
decisions live here so they are unit-testable:

- `Fields`: note type / field / tag names from config (empty until the
  user picks them in Settings; the Browser search itself is built in
  `ops.build_search`, which needs the collection for escaping).
- `field_is_empty(value)` / `needs_fill(...)`: per-note candidate check
  (used for selected notes and re-checked right before writing).
- `unique_words(items)`: distinct headwords to resolve (one lookup each).
- `build_plan(items, results_by_word, config)`: `{nid: Planned}`: IPA to
  write plus tags to add / remove, or a "tag as not found" item.
- `has_tag` / `without_tags`: Anki-style tag matching (case-insensitive,
  `tag` or a child `tag::x`), for the not-found tag.
- `Summary` + `summary_text(...)`: the end-of-run report.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection as AbstractCollection
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .clean import strip_markup
from .resolver import BUILTIN_SOURCE, Result

# No default note type / fields: the user picks them in the Settings dialog.
DEFAULT_NOTE_TYPE = ""
DEFAULT_WORD_FIELD = ""
DEFAULT_IPA_FIELD = ""
DEFAULT_TAG_AUTO = "ipa::auto"
DEFAULT_TAG_LLM = "ipa::llm"
DEFAULT_TAG_PARTIAL = "ipa::partial"
# Definitive misses get this tag and are skipped in later runs ("" = off).
DEFAULT_TAG_NOT_FOUND = "ipa::notfound"
# How to retry a note tagged as not found: misses are never cached, so the
# next run looks a note up afresh as soon as the tag is gone.
RETRY_HINT = "remove the tag to retry"

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
    """The add-on's note type / field / tag names from config, with defaults.

    `note_type`, `word` and `ipa` are "" until configured (`is_configured`).
    """

    note_type: str = DEFAULT_NOTE_TYPE
    word: str = DEFAULT_WORD_FIELD
    ipa: str = DEFAULT_IPA_FIELD
    tag_auto: str = DEFAULT_TAG_AUTO
    tag_llm: str = DEFAULT_TAG_LLM
    tag_partial: str = DEFAULT_TAG_PARTIAL
    overwrite: bool = False
    tag_not_found: str = DEFAULT_TAG_NOT_FOUND  # "" = no tagging, no skipping

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
            tag_not_found=s("tag_not_found", DEFAULT_TAG_NOT_FOUND),
        )

    @property
    def is_configured(self) -> bool:
        """Note type and both fields are set (whether they exist in the
        collection is checked by `core.settings.setup_problem`)."""
        return bool(self.note_type and self.word and self.ipa)


def field_is_empty(value: str) -> bool:
    """True for "", whitespace, `&nbsp;`, `<br>`, `<div></div>` and similar."""
    return not strip_markup(value).strip()


def tag_matches(tag: str, wanted: str) -> bool:
    """Anki's `tag:wanted` rule: same tag ignoring case, or a child of it
    (`wanted::x`). An empty `wanted` matches nothing."""
    if not wanted:
        return False
    t, w = tag.lower(), wanted.lower()
    return t == w or t.startswith(w + "::")


def has_tag(tags: Iterable[str], wanted: str) -> bool:
    return any(tag_matches(t, wanted) for t in tags)


def without_tags(tags: Iterable[str], remove: Iterable[str]) -> list[str]:
    """`tags` minus every tag matching one of `remove` (children included),
    order kept. Use it to drop the not-found tag when IPA gets filled."""
    remove = [r for r in remove if r]
    return [t for t in tags if not any(tag_matches(t, r) for r in remove)]


# Why a note is not a candidate (counted for the "nothing to do" tooltip).
SKIP_NOTE_TYPE = "other note type"
SKIP_FILLED = "IPA already filled"
SKIP_NO_WORD = "empty headword"
SKIP_NOT_FOUND = "tagged as not found"


def skip_reason(
    note_type_name: str,
    fields: Mapping[str, str],
    fcfg: Fields,
    *,
    tags: Iterable[str] = (),
) -> str | None:
    """None if the note should be filled, else the reason to skip it.

    Nothing is a candidate while the note type / fields are not configured.
    `tags` are the note's tags: a note carrying `fcfg.tag_not_found` (or a
    child of it, any case) is skipped with `SKIP_NOT_FOUND`, checked after
    the other reasons. Callers that don't pass tags never get it
    (`ops.apply_plan`'s re-check relies on that).
    """
    if not fcfg.is_configured:
        return SKIP_NOTE_TYPE
    if note_type_name != fcfg.note_type or fcfg.word not in fields or fcfg.ipa not in fields:
        return SKIP_NOTE_TYPE
    if field_is_empty(fields[fcfg.word]):
        return SKIP_NO_WORD
    if not fcfg.overwrite and not field_is_empty(fields[fcfg.ipa]):
        return SKIP_FILLED
    if has_tag(tags, fcfg.tag_not_found):
        return SKIP_NOT_FOUND
    return None


def needs_fill(
    note_type_name: str, fields: Mapping[str, str], fcfg: Fields, *, tags: Iterable[str] = ()
) -> bool:
    return skip_reason(note_type_name, fields, fcfg, tags=tags) is None


def unique_words(items: Sequence[Item]) -> list[str]:
    """Distinct raw headwords in first-seen order (each is resolved once)."""
    return list(dict.fromkeys(word for _, word in items))


@dataclass(frozen=True)
class Planned:
    """What to write into one note.

    Fill item (`not_found=False`): set the IPA field to `ipa`, remove
    `remove_tags` (the not-found tag, children too: `without_tags`), then add
    `tags`. Not-found item (`not_found=True`, `ipa == ""`, `source == ""`):
    leave the IPA field alone and only add `tags` (= the not-found tag).
    """

    ipa: str
    tags: tuple[str, ...]  # to add
    source: str
    partial: bool
    remove_tags: tuple[str, ...] = ()
    not_found: bool = False


def tags_for(result: Result, fcfg: Fields) -> tuple[str, ...]:
    """Tags to add for `result`: the fill tags when it has IPA; the not-found
    tag for a definitive miss (if enabled); nothing for another miss."""
    if not result.ipa:
        if result.definitive and fcfg.tag_not_found:
            return (fcfg.tag_not_found,)
        return ()
    tags = [fcfg.tag_auto]
    if result.source in LLM_SOURCES:
        tags.append(fcfg.tag_llm)
    if not result.complete:
        tags.append(fcfg.tag_partial)
    return tuple(t for t in dict.fromkeys(tags) if t)


def build_plan(
    items: Sequence[Item], results: Mapping[str, Result], fcfg: Fields
) -> dict[int, Planned]:
    """`{nid: Planned}` for every item whose headword resolved to some IPA,
    plus a not-found item for every definitive miss (`Result.definitive`)
    while `fcfg.tag_not_found` is set.

    Partial results (`[word]` placeholders, `complete=False`) are written too
    and get `tag_partial` (never the not-found tag). Every fill item removes
    the not-found tag. Items missing from `results` (cancelled before their
    turn) and non-definitive misses (errors, disabled sources) are left out.
    """
    plan: dict[int, Planned] = {}
    remove = (fcfg.tag_not_found,) if fcfg.tag_not_found else ()
    for nid, word in items:
        res = results.get(word)
        if res is None:
            continue
        if not res.ipa:
            tags = tags_for(res, fcfg)
            if tags:
                plan[nid] = Planned(ipa="", tags=tags, source="", partial=False, not_found=True)
            continue
        plan[nid] = Planned(
            ipa=res.ipa,
            tags=tags_for(res, fcfg),
            source=res.source or "unknown",
            partial=not res.complete,
            remove_tags=remove,
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
    cache_error: str | None = None  # lookup cache could not be saved
    tagged_not_found: int = 0  # missed notes that got the not-found tag
    skipped_not_found: int = 0  # not processed: already tagged as not found
    missed_uncertain: int = 0  # missed, but not definitively (source error/off): no tag

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
    cache_error: str | None = None,
    skipped_not_found: int = 0,
) -> Summary:
    """Counts per note. `written` = nids actually updated by the write op.

    Every item lands in exactly one of: filled (`filled_by_source`), missed
    (`missed_notes`; `tagged_not_found` of them got the not-found tag,
    `missed_uncertain` were not definitive misses: a source failed or was
    off, so no tag and they are retried next run),
    `skipped_changed` (a planned fill or tag not written because the note
    changed meanwhile) or `not_processed` (cancelled). `skipped_not_found`
    is the collect step's `SKIP_NOT_FOUND` count (those notes are not items).
    """
    s = Summary(
        total=len(items),
        disabled=dict(disabled or {}),
        errors=list(errors),
        cancelled=cancelled,
        words_total=words_total,
        words_done=len(results),
        llm_status=llm_status,
        cache_error=cache_error,
        skipped_not_found=skipped_not_found,
    )
    missed: list[str] = []
    for nid, word in items:
        if word not in results:
            s.not_processed += 1
            continue
        planned = plan.get(nid)
        if planned is not None and planned.not_found:
            if nid not in written:
                s.skipped_changed += 1
                continue
            s.tagged_not_found += 1
            planned = None
        if planned is None:
            s.missed_notes += 1
            r = results[word]
            if r.ipa is None and not r.definitive:
                s.missed_uncertain += 1
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
    if s.tagged_not_found:
        lines.append(
            f"    of which tagged {fcfg.tag_not_found} (no IPA found, skipped in later runs; "
            f"{RETRY_HINT}): {s.tagged_not_found}"
        )
    if s.missed_uncertain and fcfg.tag_not_found:
        lines.append(
            f"    of which not tagged (a source failed or was turned off; tried again next run): "
            f"{s.missed_uncertain}"
        )
    if s.not_processed:
        lines.append(f"Not processed (cancelled): {s.not_processed}")
    if s.skipped_not_found:
        lines.append(not_found_skip_text(s.skipped_not_found, fcfg))
    if s.skipped_changed:
        lines.append(f"Skipped (IPA filled or note changed meanwhile): {s.skipped_changed}")
    if s.filled:
        lines.append(f"Filled notes are tagged {fcfg.tag_auto}. Edit > Undo reverts the whole batch.")
    if s.llm_status is not None:
        lines.append(f"Claude fallback: {s.llm_status}")
    if s.cache_error:
        lines.append(
            f"Lookup cache not saved ({s.cache_error}); the notes above were written, "
            "the cache is retried next time."
        )
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


def not_found_skip_text(n: int, fcfg: Fields) -> str:
    """Line / tooltip part for notes skipped because of the not-found tag."""
    return f"Skipped {n}: tagged {fcfg.tag_not_found} ({RETRY_HINT})"
