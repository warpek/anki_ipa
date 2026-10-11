"""Collection read/write steps (imports `anki`, never `aqt`).

The functions take a `Collection` and run inside `QueryOp` / `CollectionOp`
in the GUI, or directly in tests and scripts. The batch and the editor
button share them: `read_note` + `apply_plan` for an existing note,
`fill_note` alone for an unsaved note in the Add window.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from anki.collection import Collection, OpChanges, SearchNode
from anki.errors import NotFoundError
from anki.models import NotetypeId
from anki.notes import Note, NoteId

from .core.batch import Fields, Item, Planned, skip_reason
from .core.settings import setup_problem as _setup_problem

UNDO_NAME = "Fill IPA"


@dataclass
class Collected:
    items: list[Item] = field(default_factory=list)  # (nid, raw word field)
    skipped: dict[str, int] = field(default_factory=dict)  # reason -> count
    search: str = ""  # the Browser search used (no selection only)


def build_search(col: Collection, fcfg: Fields) -> str:
    """Browser search for notes that need IPA, escaped by the backend.

    `note:<type>`, `<word>:_*` (word field non-empty) and `<ipa>:` (IPA field
    empty; left out when overwriting), so `"`, `_`, `*`, `:` and spaces in
    the names are matched literally. Same `SearchNode` fields in 25.07 and
    26.08 (`SearchNode.Field` has only `field_name`/`text` in common).
    """
    nodes = [SearchNode(note=fcfg.note_type), SearchNode(field_name=fcfg.word)]
    if not fcfg.overwrite:
        nodes.append(SearchNode(field=SearchNode.Field(field_name=fcfg.ipa, text="")))
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


@dataclass(frozen=True)
class NoteSnapshot:
    type_name: str
    fields: dict[str, str]


def read_note(col: Collection, nid: int) -> NoteSnapshot:
    """Note type name and fields of one note (raises `NotFoundError`)."""
    note = col.get_note(NoteId(nid))
    return NoteSnapshot(_type_name(col, note, {}), dict(note.items()))


def fill_note(note: Note, planned: Planned, fcfg: Fields) -> None:
    """Set the IPA field and add the plan's tags (in memory, no save)."""
    note[fcfg.ipa] = planned.ipa
    for tag in planned.tags:
        note.add_tag(tag)


def collect_words(
    col: Collection, fcfg: Fields, selected: Sequence[int] | None = None
) -> Collected:
    """Candidate notes: the selection (filtered) or every `build_search` match."""
    out = Collected()
    if selected:
        nids: Sequence[int] = selected
    else:
        out.search = build_search(col, fcfg)
        nids = col.find_notes(out.search)
    type_names: dict[int, str] = {}
    for nid in nids:
        note = col.get_note(NoteId(nid))
        reason = skip_reason(_type_name(col, note, type_names), dict(note.items()), fcfg)
        if reason is None:
            out.items.append((int(nid), note[fcfg.word]))
        else:
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

    Each note is re-fetched and re-checked (still the right note type, IPA
    still empty unless overwriting) because the network step may take long.
    No undo entry is created when nothing is left to write.
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
        if skip_reason(_type_name(col, note, type_names), dict(note.items()), fcfg) is not None:
            outcome.skipped.append(nid)
            continue
        fill_note(note, planned, fcfg)
        notes.append(note)
        outcome.written.append(nid)
    if notes:
        pos = col.add_custom_undo_entry(UNDO_NAME)
        col.update_notes(notes)
        outcome.changes = col.merge_undo_entries(pos)
    return outcome
