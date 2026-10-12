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

from dataclasses import replace

from fakes import ENGLISH_WORDS

from anki_ipa.core.batch import (
    SKIP_NOT_FOUND,
    Fields,
    Planned,
    build_plan,
    summarize,
    unique_words,
)
from anki_ipa.core.resolver import Result
from anki_ipa.ops import (
    UNDO_NAME,
    apply_plan,
    build_search,
    collect_words,
    note_types_with_fields,
)

FIELDS = ["English", "Polish", "Not-english", "Not-polish", "English-sound", "IPA-english"]
F = ENGLISH_WORDS

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
    "nbsp_ipa": ("scope", "&nbsp;"),  # markup only: counts as empty
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


EMPTY = ("cat", "dog_html", "phrase", "sentence", "missed", "llm", "nbsp_ipa")


def test_no_selection_finds_exactly_the_empty_ones(col: Collection) -> None:
    ids = setup(col)
    found = set(col.find_notes(build_search(col, F)))
    # The search has no "IPA empty" term: it can't see markup-only fields.
    assert found == {ids[k] for k in (*EMPTY, "filled")}  # not: no_word, Basic note
    c = collect_words(col, F)
    assert {nid for nid, _ in c.items} == {ids[k] for k in EMPTY}
    assert c.skipped == {}  # filled notes are the normal case, not "skipped"


def test_no_selection_picks_up_markup_only_ipa_fields(col: Collection) -> None:
    ids = setup(col)
    br = add_word(col, "rank", "<br>")
    div = add_word(col, "sassy", "<div>&nbsp;</div>")
    spaces = add_word(col, "goofy", " \u00a0 ")
    tagged = add_word(col, "vexing", "<br>", tags=(NF,))
    c = collect_words(col, F)
    got = {nid for nid, _ in c.items}
    assert {ids["nbsp_ipa"], br, div, spaces} <= got and tagged not in got
    assert ids["filled"] not in got
    assert c.skipped == {SKIP_NOT_FOUND: 1}
    plan = build_plan(c.items, {"rank": Result("ræŋk", "freedictionary", True)}, F)
    assert apply_plan(col, plan, F).written == [br]
    assert col.get_note(br)["IPA-english"] == "ræŋk"
    # Overwrite on: the same notes plus the filled ones.
    ow = collect_words(col, replace(F, overwrite=True))
    assert {ids["filled"], br, div} <= {nid for nid, _ in ow.items}


def test_collect_selected_filters(col: Collection) -> None:
    ids = setup(col)
    sel = [ids[k] for k in ("cat", "filled", "other_type", "nbsp_ipa", "no_word")]
    c = collect_words(col, F, sel)
    assert c.items == [(ids["cat"], "cat"), (ids["nbsp_ipa"], "scope")]
    assert c.skipped == {"IPA already filled": 1, "other note type": 1, "empty headword": 1}
    c2 = collect_words(col, replace(F, overwrite=True), sel)
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


# -- search escaping + setup check (SearchNode) ----------------------------------
ODD_TYPE = 'My "odd" type_1*: x'  # Anki strips the quotes when saving
ODD_WORD = "Word_*"
ODD_IPA = "IPA*_x"


def setup_odd(col: Collection) -> tuple[Fields, dict[str, NoteId]]:
    """A note type whose name / fields hold search syntax, plus decoys."""
    mm = col.models
    nt = mm.new(ODD_TYPE)
    for name in ("Front", ODD_WORD, ODD_IPA):
        mm.add_field(nt, mm.new_field(name))
    t = mm.new_template("Card 1")
    t["qfmt"] = "{{Front}}"
    t["afmt"] = "{{FrontSide}}"
    mm.add_template(nt, t)
    mm.add(nt)
    # Use the names as Anki stored them.
    name = next(n for n in note_types_with_fields(col) if n.startswith("My ") and "odd" in n)
    assert '"' not in name and "_1*: x" in name
    model = mm.by_name(name)
    assert model is not None
    assert mm.field_names(model) == ["Front", ODD_WORD, ODD_IPA]
    fcfg = Fields(note_type=name, word=ODD_WORD, ipa=ODD_IPA)
    did = DeckId(1)
    ids: dict[str, NoteId] = {}
    for key, word, ipa in [
        ("empty", "cat", ""),
        ("empty2", "dog house", ""),
        ("filled", "fish", "fɪʃ"),
        ("no_word", "", ""),
        ("star", "*", ""),  # a literal * headword is still a non-empty word
    ]:
        n = col.new_note(model)
        n["Front"] = key
        n[ODD_WORD] = word
        n[ODD_IPA] = ipa
        col.add_note(n, did)
        ids[key] = n.id
    # Decoy: a note type that a wildcard reading of the name would also match.
    decoy = mm.new("My odd type_12345: x")
    for f in ("Front", "Word_ab", "IPA*_x"):
        mm.add_field(decoy, mm.new_field(f))
    t = mm.new_template("Card 1")
    t["qfmt"] = "{{Front}}"
    t["afmt"] = "{{FrontSide}}"
    mm.add_template(decoy, t)
    mm.add(decoy)
    dm = mm.by_name("My odd type_12345: x")
    assert dm is not None
    n = col.new_note(dm)
    n["Front"] = "decoy"
    n["Word_ab"] = "cat"
    col.add_note(n, did)
    ids["decoy"] = n.id
    return fcfg, ids


def test_search_escapes_odd_names(col: Collection) -> None:
    from anki_ipa.ops import setup_problem

    fcfg, ids = setup_odd(col)
    s = build_search(col, fcfg)
    assert "\\_" in s and "\\*" in s  # wildcards in the names are escaped
    assert set(col.find_notes(s)) == {ids["empty"], ids["empty2"], ids["star"], ids["filled"]}
    c = collect_words(col, fcfg)
    assert c.search == s and c.skipped == {}
    assert sorted(w for _, w in c.items) == ["*", "cat", "dog house"]
    # overwrite: same search, the filled note becomes a candidate
    ow = replace(fcfg, overwrite=True)
    assert build_search(col, ow) == s
    assert sorted(w for _, w in collect_words(col, ow).items) == ["*", "cat", "dog house", "fish"]

    assert setup_problem(col, fcfg) is None
    assert note_types_with_fields(col)[fcfg.note_type] == ["Front", ODD_WORD, ODD_IPA]


def test_setup_problem_against_collection(col: Collection) -> None:
    from anki_ipa.core.settings import NOT_SET_UP
    from anki_ipa.ops import note_type_fields, setup_problem

    setup(col)
    assert setup_problem(col, F) is None
    assert setup_problem(col, Fields()) == NOT_SET_UP
    assert note_type_fields(col, "English words") == FIELDS
    assert note_type_fields(col, "english words") is None  # exact name only
    missing = setup_problem(col, replace(F, note_type="Vocabulary"))
    assert missing is not None and "not found" in missing
    nofield = setup_problem(col, replace(F, ipa="IPA"))
    assert nofield is not None and '"IPA"' in nofield


# -- not-found tag (Phase 10b) ----------------------------------------------------
NF = F.tag_not_found  # "ipa::notfound"


def add_word(col: Collection, word: str, ipa: str = "", tags: tuple[str, ...] = ()) -> NoteId:
    nt = col.models.by_name("English words")
    assert nt is not None
    n = col.new_note(nt)
    n["English"] = word
    n["IPA-english"] = ipa
    n.tags = list(tags)
    col.add_note(n, DeckId(1))
    return n.id


def lower(tags: list[str]) -> list[str]:
    return sorted(t.lower() for t in tags)


def test_search_excludes_not_found_tag(col: Collection) -> None:
    ids = setup(col)
    tagged = add_word(col, "vexing", tags=(NF, "mine"))
    upper = add_word(col, "vexed", tags=("IPA::NotFound",))  # Anki tags ignore case
    child = add_word(col, "vexer", tags=(NF + "::old",))  # child tag
    similar = add_word(col, "vex", tags=("ipa::notfoundish", "ipa::auto"))  # not a match
    tagged_filled = add_word(col, "vexation", "ˌvekˈseɪʃn", tags=(NF,))
    s = build_search(col, F)
    assert s.endswith("-tag:ipa::notfound")
    expected = {ids[k] for k in EMPTY}
    assert set(col.find_notes(s)) == expected | {similar, ids["filled"]}

    c = collect_words(col, F)
    assert {nid for nid, _ in c.items} == expected | {similar}
    # counted by not_found_search; the tagged note filled by hand is not
    assert c.skipped == {SKIP_NOT_FOUND: 3}
    assert c.skip_text(F) == ". Skipped 3: tagged ipa::notfound (remove the tag to retry)"

    # Tagging off: no exclusion, no count.
    off = replace(F, tag_not_found="")
    assert "tag:" not in build_search(col, off)
    assert set(col.find_notes(build_search(col, off))) == expected | {
        similar,
        tagged,
        upper,
        child,
        tagged_filled,
        ids["filled"],
    }
    c_off = collect_words(col, off)
    assert c_off.skipped == {} and len(c_off.items) == len(expected) + 4

    # Overwrite: the filled tagged note is excluded / counted too.
    ow = replace(F, overwrite=True)
    assert tagged_filled not in set(col.find_notes(build_search(col, ow)))
    assert collect_words(col, ow).skipped[SKIP_NOT_FOUND] == 4


def test_collect_selection_skips_tagged(col: Collection) -> None:
    ids = setup(col)
    tagged = add_word(col, "vexing", tags=(NF,))
    child = add_word(col, "vexer", tags=("IPA::notfound::x",))
    sel = [ids["cat"], tagged, ids["filled"], child]
    c = collect_words(col, F, sel)
    assert c.items == [(ids["cat"], "cat")]
    assert c.skipped == {"IPA already filled": 1, SKIP_NOT_FOUND: 2}
    assert c.skip_text(F) == (
        " (1 IPA already filled). Skipped 2: tagged ipa::notfound (remove the tag to retry)"
    )
    # Everything selected is tagged: the tooltip part still reads well.
    only = collect_words(col, F, [tagged, child])
    assert only.items == [] and only.skip_text(F).startswith(". Skipped 2: tagged")
    # Tagging off: tagged notes are ordinary candidates.
    c_off = collect_words(col, replace(F, tag_not_found=""), sel)
    assert [nid for nid, _ in c_off.items] == [ids["cat"], tagged, child]


def test_nothing_to_do_tooltip_is_html_escaped(col: Collection) -> None:
    setup(col)
    odd = replace(F, tag_not_found="ipa::<none>&x")
    add_word(col, "vexing", tags=(odd.tag_not_found,))
    c = collect_words(col, odd, [add_word(col, "vexed", tags=(odd.tag_not_found,))])
    msg = c.nothing_to_do_html(odd, "Fill missing IPA", True)
    assert msg == (
        "Fill missing IPA: nothing to do in the selected notes. "
        "Skipped 1: tagged ipa::&lt;none&gt;&amp;x (remove the tag to retry)"
    )
    full = collect_words(col, replace(odd, note_type="Nope <b>"))
    msg = full.nothing_to_do_html(replace(odd, note_type="Nope <b>"), "Fill missing IPA", False)
    assert "<" not in msg and "Nope &lt;b&gt;" in msg


def test_apply_plan_fills_tags_and_untags_in_one_undo_step(col: Collection) -> None:
    ids = setup(col)
    retry = add_word(col, "vexing", tags=("Mine", "IPA::NotFound", NF + "::old"))
    gone = add_word(col, "qwzxv2", tags=("keep",))
    meanwhile = add_word(col, "zzyzx", tags=("keep",))
    results = {
        "cat": Result("kæt", "freedictionary", True),
        "vexing": Result("ˈveksɪŋ", "wiktionary", True),  # tagged earlier, now found
        "qwzxv": Result(None, None, False, ["qwzxv"], definitive=True),
        "qwzxv2": Result(None, None, False, ["qwzxv2"], definitive=True),
        "zzyzx": Result(None, None, False, ["zzyzx"], definitive=True),
        "insurmountable": Result(None, None, False, ["insurmountable"]),  # error: no tag
    }
    items = [
        (ids["cat"], "cat"),
        (retry, "vexing"),
        (ids["missed"], "qwzxv"),
        (gone, "qwzxv2"),
        (meanwhile, "zzyzx"),
        (ids["llm"], "insurmountable"),
    ]
    plan = build_plan(items, results, F)
    assert set(plan) == {ids["cat"], retry, ids["missed"], gone, meanwhile}
    assert plan[ids["missed"]].not_found and plan[retry].remove_tags == (NF,)

    every = {**ids, "retry": retry, "gone": gone, "meanwhile": meanwhile}
    # The user fills "zzyzx" by hand while the lookups run.
    n = col.get_note(meanwhile)
    n["IPA-english"] = "ˈzaɪzɪks"
    col.update_note(n)
    before = snapshot(col, every)

    out = apply_plan(col, plan, F)
    assert out.skipped == [meanwhile]
    assert sorted(out.written) == sorted([ids["cat"], retry, ids["missed"], gone])
    after = snapshot(col, every)
    assert after["cat"][0][5] == "kæt" and after["cat"][1] == ["ipa::auto"]
    # Found after all: IPA written, not-found tag (any case, children) removed.
    assert after["retry"][0][5] == "ˈveksɪŋ" and after["retry"][1] == ["Mine", "ipa::auto"]
    # Definitive misses: tag added, IPA (and every other field) untouched. The
    # backend reuses the spelling of a tag it already knows ("IPA::NotFound"
    # from `retry`), so compare ignoring case.
    assert after["missed"][0] == before["missed"][0] and lower(after["missed"][1]) == [NF]
    assert after["gone"][0] == before["gone"][0] and lower(after["gone"][1]) == [NF, "keep"]
    # Filled meanwhile: neither IPA nor tags touched.
    assert after["meanwhile"] == before["meanwhile"]
    assert after["llm"] == before["llm"]  # non-definitive miss: no tag

    # The fresh run skips the tagged notes.
    found = set(col.find_notes(build_search(col, F)))
    assert ids["missed"] not in found and gone not in found

    s = summarize(items, results, plan, set(out.written), words_total=6, cancelled=False)
    assert (s.filled, s.missed_notes, s.tagged_not_found, s.skipped_changed) == (2, 3, 2, 1)

    assert undo_name(col) == UNDO_NAME
    col.undo()  # one step restores fields AND tags
    assert snapshot(col, every) == before


def test_not_found_item_never_wipes_ipa(col: Collection) -> None:
    """Even with overwrite on (`overwrite_existing`), a not-found item only
    adds the tag."""
    ids = setup(col)
    nf = Planned("", (NF,), "", False, (), True)
    ow = replace(F, overwrite=True)
    out = apply_plan(col, {ids["filled"]: nf}, ow)
    assert out.written == [ids["filled"]]
    n = col.get_note(ids["filled"])
    assert n["IPA-english"] == "ˈrekɔːd" and n.tags == [NF]
    undo_after_tag = undo_name(col)
    # Already tagged: counted as written, no note change, no new undo step.
    col.undo()
    n = col.get_note(ids["filled"])
    n.tags = ["ipa::NOTFOUND"]
    col.update_note(n)
    undo_before = undo_name(col)
    out = apply_plan(col, {ids["filled"]: nf}, ow)
    assert out.written == [ids["filled"]] and undo_name(col) == undo_before
    assert col.get_note(ids["filled"]).tags == ["ipa::NOTFOUND"]
    assert undo_after_tag == UNDO_NAME
    # Overwrite off: the filled note is skipped by the re-check.
    assert apply_plan(col, {ids["filled"]: nf}, F).skipped == [ids["filled"]]


def test_removing_the_tag_gives_a_fresh_lookup_next_run(col: Collection) -> None:
    """Misses are never cached: the tag alone decides whether a note is retried."""
    from anki_ipa.core.cache import Cache
    from anki_ipa.core.normalize import NormalizeConfig
    from anki_ipa.core.resolver import Resolver
    from anki_ipa.core.sources.base import Pronunciations

    class Src:
        name = "fake"

        def __init__(self) -> None:
            self.calls: list[str] = []
            self.data: dict[str, Pronunciations] = {}

        def lookup(self, text: str) -> Pronunciations | None:
            self.calls.append(text)
            return self.data.get(text)

    setup(col)
    nid = add_word(col, "vexing")
    src, shared = Src(), Cache(None)  # one Cache per session, like ui.shared_cache()

    def run() -> None:
        c = collect_words(col, F)
        words = unique_words(c.items)
        resolver = Resolver([src], shared, NormalizeConfig())  # new per run
        results = dict(zip(words, resolver.resolve_many(words)))
        apply_plan(col, build_plan(c.items, results, F), F)

    run()
    assert src.calls.count("vexing") == 1
    assert lower(col.get_note(nid).tags) == [NF]
    run()  # tagged: not a candidate, no lookup
    assert src.calls.count("vexing") == 1

    n = col.get_note(nid)
    n.tags = []
    col.update_note(n)
    src.data["vexing"] = Pronunciations(uk=["/ˈvɛksɪŋ/"])  # the dictionary got it
    run()
    assert src.calls.count("vexing") == 2  # asked again despite the earlier miss
    n = col.get_note(nid)
    assert n["IPA-english"] == "ˈveksɪŋ" and n.tags == ["ipa::auto"]
