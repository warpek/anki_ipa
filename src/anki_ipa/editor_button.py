"""Editor button "IPA": fill the IPA field of the note open in the editor.

Registered through `gui_hooks.editor_did_init_buttons`, which Anki 26.08
fires for both editor classes (legacy `Editor` in `setupWeb`, Svelte
`NewEditor` in `_set_ready`); both have the same `addButton`, whose wrapper
saves pending edits before calling us. Before 26.08 (min. supported 25.07)
`aqt.editor.Editor` is the only editor and `NewEditor` does not exist (None).

Flow (same rules, plan, tags and write op as the Browser batch):
0. `ui.ensure_setup`: offer the Settings dialog when not set up;
1. read the note: legacy `editor.note`, or `ops.read_note` by id;
   `check_editor_note` (note type, word field) -> tooltip on a problem;
   a non-empty IPA field asks the user before overwriting;
2. `QueryOp.without_collection()`: resolve the headword (cache + sources +
   optional Claude), off the main thread;
3. write, after saving the editor's pending edits again:
   - existing note (legacy or Svelte): `ops.apply_plan` in a `CollectionOp`,
     one undo step "Fill IPA"; the legacy editor is reloaded here (we are the
     initiator), the Svelte editor reloads itself on the op notification;
   - unsaved note in the legacy Add window: `ops.fill_note` on `editor.note`
     and redraw only (the note is saved when the user adds it);
   - unsaved note in the Svelte Add window: not supported (no Python-side
     note; the webview owns the fields), a tooltip says so.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, Union

from anki.notes import Note
from aqt import gui_hooks
from aqt.editor import Editor  # 26.08+: re-exports aqt.editor_legacy.Editor
from aqt.operations import CollectionOp, QueryOp
from aqt.utils import askUser, tooltip

from . import ui
from .core.batch import (
    EditorCheck,
    Fields,
    Planned,
    check_editor_note,
    confirm_overwrite_text,
    editor_message,
    plan_single,
)
from .core.resolver import Result
from .ops import NoteSnapshot, WriteOutcome, apply_plan, fill_note, read_note

if TYPE_CHECKING:
    from aqt.editor import NewEditor
else:
    try:  # Svelte editor, Anki 26.08+ only
        from aqt.editor import NewEditor
    except ImportError:
        NewEditor = None

# A string, so the union is never evaluated at runtime (NewEditor may be None).
# Every NewEditor-only path is reached through `isinstance(editor, Editor)`
# being False, which on Anki < 26.08 never happens (only Editor exists).
AnyEditor = Union[Editor, "NewEditor"]

BUTTON_CMD = "anki_ipa_fill"
BUTTON_LABEL = "IPA"
BUTTON_TIP = "Fill the IPA field from online dictionaries (IPA Filler)"
SVELTE_ADD_UNSUPPORTED = (
    "IPA: not available in the experimental editor's Add window. "
    "Add the note first, then use the IPA button in the Browser."
)
CHANGED_MEANWHILE = "IPA: nothing written, the note changed meanwhile."


def register() -> None:
    gui_hooks.editor_did_init_buttons.append(_on_init_buttons)


def _on_init_buttons(buttons: list[str], editor: AnyEditor) -> None:
    buttons.append(
        editor.addButton(None, BUTTON_CMD, _on_button, tip=BUTTON_TIP, label=BUTTON_LABEL)
    )


def _say(editor: AnyEditor, msg: str) -> None:
    tooltip(msg, parent=editor.parentWindow)


def _on_failure(editor: AnyEditor, e: Exception) -> None:
    ui._on_failure(editor.parentWindow, e)


# -- step 1: read + check -----------------------------------------------------
def _on_button(editor: AnyEditor) -> None:
    """Called by `addButton`'s wrapper after the editor saved its edits."""
    config = ui.get_config()  # main thread, passed down
    fcfg = Fields.from_config(config)
    # Not set up / configured note type or fields missing: offer Settings.
    # (A configured setup and a note of another type: tooltip, see below.)
    if not ui.ensure_setup(editor.parentWindow, fcfg):
        return
    if isinstance(editor, Editor):
        note = editor.note
        if note is None:
            return
        if editor.addMode:  # unsaved note: only the editor's copy exists
            nt = note.note_type()
            type_name = str(nt["name"]) if nt else ""
            check = check_editor_note(type_name, dict(note.items()), fcfg)
            _after_check(editor, config, fcfg, check, nid=None, add_note=note)
            return
        nid = int(note.id)
    else:
        if not editor.nid:  # Svelte Add window: no note in Python
            _say(editor, SVELTE_ADD_UNSUPPORTED)
            return
        nid = int(editor.nid)

    def on_read(snap: NoteSnapshot) -> None:
        check = check_editor_note(snap.type_name, snap.fields, fcfg)
        _after_check(editor, config, fcfg, check, nid=nid, add_note=None)

    QueryOp(
        parent=editor.parentWindow, op=lambda col: read_note(col, nid), success=on_read
    ).failure(lambda e: _on_failure(editor, e)).run_in_background()


def _after_check(
    editor: AnyEditor,
    config: dict[str, Any],
    fcfg: Fields,
    check: EditorCheck,
    *,
    nid: int | None,
    add_note: Note | None,
) -> None:
    if check.problem:
        _say(editor, check.problem)
        return
    overwrite = False
    if check.needs_confirm:
        if not askUser(
            confirm_overwrite_text(check, fcfg),
            parent=editor.parentWindow,
            defaultno=True,
            title=ui.ACTION_LABEL,
        ):
            return
        overwrite = True
    _resolve(editor, config, replace(fcfg, overwrite=overwrite), check.word, nid, add_note)


# -- step 2: resolve (network, off the main thread) ---------------------------
def _resolve(
    editor: AnyEditor,
    config: dict[str, Any],
    fcfg: Fields,
    word: str,
    nid: int | None,
    add_note: Note | None,
) -> None:
    # Shared cache + client with the Browser batch (created on the main thread).
    resolver = ui.session_resolver(config)

    def op(_col: object) -> Result:
        try:
            return resolver.resolve(word)
        finally:
            resolver.cache.save()  # never raises OSError

    (
        QueryOp(
            parent=editor.parentWindow,
            op=op,
            success=lambda r: _write(editor, fcfg, word, r, nid, add_note),
        )
        .failure(lambda e: _on_failure(editor, e))
        .without_collection()
        .with_progress("Looking up IPA...")
        .run_in_background()
    )


# -- step 3: write ---------------------------------------------------------------
def _write(
    editor: AnyEditor,
    fcfg: Fields,
    word: str,
    result: Result,
    nid: int | None,
    add_note: Note | None,
) -> None:
    planned = plan_single(nid or 0, word, result, fcfg)
    msg = editor_message(word, planned, fcfg)
    if planned is None:
        _say(editor, msg)
        return
    if add_note is not None:
        assert isinstance(editor, Editor)
        editor.call_after_note_saved(lambda: _fill_add_note(editor, add_note, planned, fcfg, msg))
        return
    assert nid is not None
    target: int = nid
    # Save edits typed during the lookup first, then write as one undo step.
    editor.call_after_note_saved(
        lambda: _write_existing(editor, target, planned, fcfg, msg), keepFocus=True
    )


def _fill_add_note(
    editor: Editor, note: Note, planned: Planned, fcfg: Fields, msg: str
) -> None:
    if editor.note is not note:  # Add window moved on (note added / type changed)
        _say(editor, CHANGED_MEANWHILE)
        return
    fill_note(note, planned, fcfg)
    editor.loadNoteKeepingFocus()
    _say(editor, msg)


def _write_existing(
    editor: AnyEditor, nid: int, planned: Planned, fcfg: Fields, msg: str
) -> None:
    def on_done(out: WriteOutcome) -> None:
        if isinstance(editor, Editor) and editor.note is not None and editor.note.id == nid:
            # We are the initiator, so the Browser / Edit window does not
            # reload the legacy editor itself; keep the cursor where it is.
            editor.note.load()
            editor.loadNoteKeepingFocus()
        _say(editor, msg if out.written else CHANGED_MEANWHILE)

    CollectionOp(
        parent=editor.parentWindow, op=lambda col: apply_plan(col, {nid: planned}, fcfg)
    ).success(on_done).failure(lambda e: _on_failure(editor, e)).run_in_background(
        initiator=editor
    )

