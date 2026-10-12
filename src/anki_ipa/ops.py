"""Collection read/write steps (imports `anki`, never `aqt`).

The functions take a `Collection` and run inside `QueryOp` / `CollectionOp`
in the GUI, or directly in tests and scripts.
"""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from anki.collection import Collection, OpChanges, SearchNode
from anki.errors import NotFoundError
from anki.models import NotetypeId
from anki.notes import Note, NoteId

from .core.batch import (
    SKIP_FILLED,
    SKIP_NOT_FOUND,
    Fields,
    Item,
    Planned,
    has_tag,
    not_found_skip_text,
    skip_reason,
    without_tags,
)
from .core.settings import setup_problem as _setup_problem

UNDO_NAME = "Fill IPA"


@dataclass
class Collected:
    items: list[Item] = field(default_factory=list)  # (nid, raw word field)
    skipped: dict[str, int] = field(default_factory=dict)  # reason -> count
    search: str = ""  # the Browser search used (no selection only)

    def skip_text(self, fcfg: Fields) -> str:
        """Why nothing (or not everything) was a candidate, for the "nothing
        to do" tooltip: ` (2 IPA already filled). Skipped 3: tagged
        ipa::notfound (remove the tag to retry)` ("" when nothing skipped)."""
        other = {r: n for r, n in self.skipped.items() if r != SKIP_NOT_FOUND}
        text = ""
        if other:
            text = " (" + ", ".join(f"{n} {r}" for r, n in sorted(other.items())) + ")"
        tagged = self.skipped.get(SKIP_NOT_FOUND, 0)
        if tagged:
            text += ". " + not_found_skip_text(tagged, fcfg)
        return text

    def nothing_to_do_html(self, fcfg: Fields, label: str, had_selection: bool) -> str:
        """The "nothing to do" tooltip, HTML-escaped (Anki's `tooltip`
        renders HTML; the search string and tag names may contain `<`/`&`)."""
        where = "selected notes" if had_selection else f"notes matching {self.search}"
        text = f"{label}: nothing to do in the {where}"
        return html.escape(text + self.skip_text(fcfg))


def _base_nodes(fcfg: Fields) -> list[SearchNode]:
    # No "IPA field empty" term (`<ipa>:`): it only matches truly empty
    # fields, while `field_is_empty` also counts `&nbsp;`, `<br>`,
    # `<div></div>` as empty. `collect_words` filters with `skip_reason`.
    return [SearchNode(note=fcfg.note_type), SearchNode(field_name=fcfg.word)]


def build_search(col: Collection, fcfg: Fields) -> str:
    """Browser search for the notes that may need IPA, escaped by the backend.

    `note:<type>`, `<word>:_*` (word field non-empty) and
    `-tag:<tag_not_found>` (left out when that tag is ""; like Anki's `tag:`,
    it also excludes child tags and ignores case), so `"`, `_`, `*`, `:` and
    spaces in the names are matched literally. Whether the IPA field is empty
    is checked per note by `collect_words` (markup-only fields count as
    empty, which a search can't express). Same `SearchNode` fields in 25.07
    and 26.08 (`negated`/`tag`/`field_name` exist in both).
    """
    nodes = _base_nodes(fcfg)
    if fcfg.tag_not_found:
        nodes.append(SearchNode(negated=SearchNode(tag=fcfg.tag_not_found)))
    return col.build_search_string(*nodes)


def not_found_search(col: Collection, fcfg: Fields) -> str | None:
    """`build_search` with the not-found tag required instead of excluded:
    the notes a run without selection skips because of the tag (None when
    tagging is off)."""
    if not fcfg.tag_not_found:
        return None
    nodes = _base_nodes(fcfg) + [SearchNode(tag=fcfg.tag_not_found)]
    return col.build_search_string(*nodes)


def _names_and_ids(col: Collection) -> list[tuple[str, NotetypeId]]:
    entries: list[Any] = list(col.models.all_names_and_ids())  # untyped protobuf
    return [(str(e.name), NotetypeId(int(e.id))) for e in entries]


def note_type_fields(col: Collection, name: str) -> list[str] | None:
    """Field names of the note type called exactly `name` (None: no such type)."""
    for nt_name, ntid in _names_and_ids(col):
        if nt_name == name:
            nt = col.models.get(ntid)
            return col.models.field_names(nt) if nt else None
    return None


def note_types_with_fields(col: Collection) -> dict[str, list[str]]:
    """`{note type name: field names}` for every note type, sorted by name."""
    out: dict[str, list[str]] = {}
    for nt_name, ntid in sorted(_names_and_ids(col), key=lambda e: e[0].lower()):
        nt = col.models.get(ntid)
        if nt:
            out[nt_name] = col.models.field_names(nt)
    return out


def setup_problem(col: Collection, fcfg: Fields) -> str | None:
    """Why the configured note type / fields can't be used here (None = ok)."""
    if not fcfg.is_configured:
        return _setup_problem(fcfg, None)
    return _setup_problem(fcfg, note_type_fields(col, fcfg.note_type))


def _type_name(col: Collection, note: Note, cache: dict[int, str]) -> str:
    mid = int(note.mid)
    if mid not in cache:
        nt = col.models.get(note.mid)
        cache[mid] = str(nt["name"]) if nt else ""
    return cache[mid]


def fill_note(note: Note, planned: Planned, fcfg: Fields) -> None:
    """Apply one plan item to `note` in memory (no save).

    Fill item: set the IPA field. Not-found item: never touch the IPA field
    (its `ipa` is ""; writing it would wipe existing IPA). Then drop
    `remove_tags` (the not-found tag and its children, any case) and add
    `tags`.
    """
    if not planned.not_found:
        note[fcfg.ipa] = planned.ipa
    if planned.remove_tags:
        note.tags = without_tags(note.tags, planned.remove_tags)
    for tag in planned.tags:
        note.add_tag(tag)


def collect_words(
    col: Collection, fcfg: Fields, selected: Sequence[int] | None = None
) -> Collected:
    """Candidate notes: the selection or every `build_search` match, each
    filtered with `skip_reason` (so an IPA field holding only `&nbsp;` /
    `<br>` counts as empty).

    Notes tagged `fcfg.tag_not_found` are skipped (`SKIP_NOT_FOUND`): in a
    selection by `skip_reason`, without one by the search itself, so they are
    counted with `not_found_search` (only those that would otherwise be
    candidates, i.e. not filled by hand since). Without a selection, notes
    whose IPA is filled are the normal case and are not counted as skipped.
    """
    out = Collected()
    type_names: dict[int, str] = {}

    def reason_for(nid: int, *, tags: bool = True) -> tuple[Note, str | None]:
        note = col.get_note(NoteId(nid))
        name = _type_name(col, note, type_names)
        return note, skip_reason(name, dict(note.items()), fcfg, tags=note.tags if tags else ())

    if selected:
        nids: Sequence[int] = selected
    else:
        out.search = build_search(col, fcfg)
        nids = col.find_notes(out.search)
        tagged_search = not_found_search(col, fcfg)
        if tagged_search is not None:
            tagged = sum(
                1 for n in col.find_notes(tagged_search) if reason_for(n, tags=False)[1] is None
            )
            if tagged:
                out.skipped[SKIP_NOT_FOUND] = tagged
    for nid in nids:
        note, reason = reason_for(nid)
        if reason is None:
            out.items.append((int(nid), note[fcfg.word]))
        elif not (reason == SKIP_FILLED and not selected):
            out.skipped[reason] = out.skipped.get(reason, 0) + 1
    return out


@dataclass
class WriteOutcome:
    """`CollectionOp` result: `.changes` drives the GUI refresh."""

    changes: OpChanges
    written: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)  # changed since collect


def apply_plan(col: Collection, plan: Mapping[int, Planned], fcfg: Fields) -> WriteOutcome:
    """Write the plan as ONE undo step named `UNDO_NAME`.

    Each note is re-fetched and re-checked (still the right note type,
    headword not empty, IPA still empty unless overwriting) because the
    network step may take long. The not-found tag is NOT part of the
    re-check: a note tagged meanwhile still gets the IPA that was found (the
    fill item removes the tag), and a not-found item on a note that already
    carries the tag counts as written without a change. Fills and not-found
    tags go into the same undo step. No undo entry when nothing is left to
    write.
    """
    notes = []
    outcome = WriteOutcome(OpChanges())
    type_names: dict[int, str] = {}
    for nid, planned in plan.items():
        try:
            note = col.get_note(NoteId(nid))
        except NotFoundError:  # deleted meanwhile
            outcome.skipped.append(nid)
            continue
        # No `tags=`: the not-found tag never blocks a write here.
        if skip_reason(_type_name(col, note, type_names), dict(note.items()), fcfg) is not None:
            outcome.skipped.append(nid)
            continue
        if planned.not_found and all(has_tag(note.tags, t) for t in planned.tags):
            outcome.written.append(nid)  # already tagged: nothing to change
            continue
        fill_note(note, planned, fcfg)
        notes.append(note)
        outcome.written.append(nid)
    if notes:
        pos = col.add_custom_undo_entry(UNDO_NAME)
        col.update_notes(notes)
        outcome.changes = col.merge_undo_entries(pos)
    return outcome
