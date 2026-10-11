"""Collection read/write steps (imports `anki`, never `aqt`).

The functions take a `Collection` and run inside `QueryOp` / `CollectionOp`
in the GUI, or directly in tests and scripts. The batch and the editor
button share them: `read_note` + `apply_plan` for an existing note,
`fill_note` alone for an unsaved note in the Add window.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from anki.collection import Collection, OpChanges
from anki.errors import NotFoundError
from anki.notes import Note, NoteId

from .core.batch import Fields, Item, Planned, skip_reason

UNDO_NAME = "Fill IPA"


@dataclass
class Collected:
    items: list[Item] = field(default_factory=list)  # (nid, raw word field)
    skipped: dict[str, int] = field(default_factory=dict)  # reason -> count


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
    if selected:
        nids: Sequence[int] = selected
    else:
        nids = col.find_notes(fcfg.search())
    out = Collected()
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
