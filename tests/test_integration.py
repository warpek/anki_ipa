"""Collect + write ops against a real temporary `anki.collection.Collection`."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from anki.collection import Collection
from anki.decks import DeckId
from anki.notes import NoteId

from anki_ipa.core.batch import Fields, build_plan, build_search, unique_words
from anki_ipa.core.resolver import Result
from anki_ipa.ops import UNDO_NAME, apply_plan, collect_words

FIELDS = ["English", "Polish", "Not-english", "Not-polish", "English-sound", "IPA-english"]
F = Fields()

# (English, IPA-english) per note; key = test handle
NOTES = {
    "cat": ("cat", ""),
    "dog_html": ("<div>dog&nbsp;</div>", ""),
    "phrase": ("cease and desist", ""),
    "sentence": ("He was detained by a snow storm.", ""),
    "missed": ("qwzxv", ""),
    "llm": ("insurmountable", ""),
    "filled": ("record", "ˈrekɔːd"),
    "no_word": ("", ""),
    "nbsp_ipa": ("scope", "&nbsp;"),  # counts as empty for selections only
}

RESULTS = {
    "cat": Result("kæt", "freedictionary", True),
    "<div>dog&nbsp;</div>": Result("dɒg", "wiktionary", True),
    "cease and desist": Result("siːs ənd dɪˈsɪst", "freedictionary", True),
    "He was detained by a snow storm.": Result(
        "hiː wəz [detained] baɪ ə snəʊ stɔːm.", "mixed", False, ["detained"]
    ),
    "qwzxv": Result(None, None, False, ["qwzxv"]),
    "insurmountable": Result("ˌɪnsəˈmaʊntəbl", "claude", True),
    "scope": Result("skəʊp", "freedictionary", True),
}


def undo_name(col: Collection) -> str:
    """Name of the next Edit > Undo step ("" when none)."""
    status: Any = col.undo_status()  # protobuf message, untyped for mypy
    return str(status.undo)


@pytest.fixture
def col(tmp_path: Path) -> Iterator[Collection]:
    c = Collection(str(tmp_path / "collection.anki2"))
    try:
        yield c
    finally:
        c.close()


def setup(col: Collection) -> dict[str, NoteId]:
    mm = col.models
    nt = mm.new("English words")
    for name in FIELDS:
        mm.add_field(nt, mm.new_field(name))
    t = mm.new_template("Card 1")
    t["qfmt"] = "{{English}}"
    t["afmt"] = "{{FrontSide}}<hr id=answer>{{Polish}} /{{IPA-english}}/"
    mm.add_template(nt, t)
    mm.add(nt)
    added = mm.by_name("English words")
    assert added is not None and [f["name"] for f in added["flds"]] == FIELDS
    nt = added
    did = DeckId(col.decks.id("English words") or 1)
    ids: dict[str, NoteId] = {}
    for key, (word, ipa) in NOTES.items():
        n = col.new_note(nt)
        n["English"] = word
        n["Polish"] = "pl"
        n["IPA-english"] = ipa
        col.add_note(n, did)
        ids[key] = n.id
    # A note of another type that must never be touched.
    basic = mm.by_name("Basic")
    assert basic is not None
    other = col.new_note(basic)
    other["Front"] = "cat"
    col.add_note(other, did)
    ids["other_type"] = other.id
    return ids


def snapshot(col: Collection, ids: dict[str, NoteId]) -> dict[str, tuple[list[str], list[str]]]:
    out = {}
    for key, nid in ids.items():
        n = col.get_note(nid)
        out[key] = (list(n.fields), sorted(n.tags))
    return out


def test_search_finds_exactly_the_empty_ones(col: Collection) -> None:
    ids = setup(col)
    found = set(col.find_notes(build_search({})))
    expected = {ids[k] for k in ("cat", "dog_html", "phrase", "sentence", "missed", "llm")}
    assert found == expected  # not: filled, no_word, &nbsp; ipa, Basic note
    c = collect_words(col, F)
    assert {nid for nid, _ in c.items} == expected and c.skipped == {}


def test_collect_selected_filters(col: Collection) -> None:
    ids = setup(col)
    sel = [ids[k] for k in ("cat", "filled", "other_type", "nbsp_ipa", "no_word")]
    c = collect_words(col, F, sel)
    assert c.items == [(ids["cat"], "cat"), (ids["nbsp_ipa"], "scope")]
    assert c.skipped == {"IPA already filled": 1, "other note type": 1, "empty headword": 1}
    c2 = collect_words(col, Fields(overwrite=True), sel)
    assert [nid for nid, _ in c2.items] == [ids["cat"], ids["filled"], ids["nbsp_ipa"]]


def test_write_plan_tags_and_single_undo_step(col: Collection) -> None:
    ids = setup(col)
    c = collect_words(col, F, [int(n) for n in ids.values()])
    assert len(unique_words(c.items)) == 7
    plan = build_plan(c.items, RESULTS, F)
    assert set(plan) == {ids[k] for k in ("cat", "dog_html", "phrase", "sentence", "llm", "nbsp_ipa")}

    before = snapshot(col, ids)
    # Simulate the user filling "cat" while the lookups were running.
    n = col.get_note(ids["cat"])
    n["IPA-english"] = "kat-by-hand"
    col.update_note(n)
    before_write = snapshot(col, ids)
    undo_before = undo_name(col)

    outcome = apply_plan(col, plan, F)
    assert outcome.skipped == [ids["cat"]]
    assert sorted(outcome.written) == sorted(
        ids[k] for k in ("dog_html", "phrase", "sentence", "llm", "nbsp_ipa")
    )
    assert outcome.changes.note_text

    after = snapshot(col, ids)
    assert after["cat"] == before_write["cat"]  # re-check protected the manual edit
    assert after["dog_html"][0][5] == "dɒg" and after["dog_html"][1] == ["ipa::auto"]
    assert after["phrase"][0][5] == "siːs ənd dɪˈsɪst"
    assert after["sentence"][0][5] == "hiː wəz [detained] baɪ ə snəʊ stɔːm."
    assert after["sentence"][1] == ["ipa::auto", "ipa::partial"]
    assert after["llm"][1] == ["ipa::auto", "ipa::llm"]
    assert after["nbsp_ipa"][0][5] == "skəʊp"
    for key in ("missed", "filled", "no_word", "other_type"):
        assert after[key] == before[key], key
    for key in ("dog_html", "phrase"):  # other fields untouched
        assert after[key][0][:5] == before[key][0][:5]

    # Exactly one undo step, named after the batch, restores everything.
    assert undo_name(col) == UNDO_NAME
    col.undo()
    assert snapshot(col, ids) == before_write
    assert undo_name(col) == undo_before  # the manual edit is next


def test_empty_plan_creates_no_undo_entry(col: Collection) -> None:
    ids = setup(col)
    undo_before = undo_name(col)
    outcome = apply_plan(col, {}, F)
    assert outcome.written == [] and undo_name(col) == undo_before
    plan = build_plan([(ids["filled"], "record")], {"record": Result("x", "wiktionary", True)}, F)
    outcome = apply_plan(col, plan, F)  # filled -> skipped by the re-check
    assert outcome.skipped == [ids["filled"]] and undo_name(col) == undo_before


def test_ops_do_not_import_qt() -> None:
    assert "aqt" not in sys.modules or sys.modules["aqt"].mw is None


def test_editor_helpers_read_fill_and_overwrite(col: Collection) -> None:
    """The editor button's write path: read_note + apply_plan / fill_note."""
    from anki_ipa.core.batch import Planned, check_editor_note
    from anki_ipa.ops import fill_note, read_note

    ids = setup(col)
    snap = read_note(col, ids["filled"])
    assert snap.type_name == "English words" and snap.fields["IPA-english"] == "ˈrekɔːd"
    assert check_editor_note(snap.type_name, snap.fields, F).needs_confirm
    assert read_note(col, ids["other_type"]).type_name == "Basic"

    planned = Planned("ˈrekɔːd, rɪˈkɔːd", ("ipa::auto",), "freedictionary", False)
    # Not confirmed (overwrite off): the filled note is left alone.
    assert apply_plan(col, {ids["filled"]: planned}, F).written == []
    # Confirmed: one "Fill IPA" undo step that undo reverts.
    out = apply_plan(col, {ids["filled"]: planned}, Fields(overwrite=True))
    assert out.written == [ids["filled"]]
    n = col.get_note(ids["filled"])
    assert n["IPA-english"] == "ˈrekɔːd, rɪˈkɔːd" and n.tags == ["ipa::auto"]
    assert undo_name(col) == UNDO_NAME
    col.undo()
    assert col.get_note(ids["filled"])["IPA-english"] == "ˈrekɔːd"

    # Unsaved note (Add window): fill_note only touches the in-memory note.
    nt = col.models.by_name("English words")
    assert nt is not None
    new = col.new_note(nt)
    new["English"] = "cat"
    new.tags = ["mine"]
    fill_note(new, Planned("kæt", ("ipa::auto", "ipa::partial"), "mixed", True), F)
    assert new["IPA-english"] == "kæt" and sorted(new.tags) == ["ipa::auto", "ipa::partial", "mine"]
    assert new.id == 0
