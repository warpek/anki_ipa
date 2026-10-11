"""Source parsers on recorded fixtures (tests/fixtures/*, recorded 2026-10-11)."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fakes import FIXTURES, Handler, client

from anki_ipa.core.http import Response
from anki_ipa.core.sources import freedictionary as fd
from anki_ipa.core.sources import wiktionary as wk
from anki_ipa.core.sources.base import Pronunciations, Source, classify

WORDS = ["record", "read", "ice_cream", "cease_and_desist", "scope", "qwzxv"]


def fd_fixture(name: str) -> object:
    return json.loads((FIXTURES / "freedictionary" / f"{name}.json").read_text("utf-8"))


def wk_fixture(name: str) -> str:
    return (FIXTURES / "wiktionary" / f"{name}.wikitext").read_text("utf-8")


# -- base ----------------------------------------------------------------------


def test_classify() -> None:
    assert classify([]) == {"other"}
    assert classify(["Received Pronunciation"]) == {"uk"}
    assert classify(["RP,GA,CA"]) == {"uk", "us"}
    assert classify(["General Australian", "New Zealand", "UK"]) == {"uk"}
    assert classify(["General Australian"]) == set()
    assert classify(["Early", "Modern"]) is None
    assert classify(["[[Received Pronunciation]]"]) == {"uk"}


def test_pronunciations_dict_roundtrip() -> None:
    p = Pronunciations(uk=["/a/"], us=["/b/"], other=[])
    assert Pronunciations.from_dict(p.to_dict()) == p
    assert Pronunciations.from_dict({"uk": "bad"}) == Pronunciations()


def test_sources_satisfy_protocol() -> None:
    c, _, _ = client(lambda m, u: Response(404))
    assert isinstance(fd.FreeDictionarySource(c), Source)
    assert isinstance(wk.WiktionarySource(c), Source)


# -- FreeDictionaryAPI ---------------------------------------------------------


def test_fd_record() -> None:
    p = fd.parse(fd_fixture("record"))
    assert p is not None
    assert p.uk == ["/ˈɹɛk.ɔːd/", "/ɹɪˈkɔːd/"]  # noun + verb; fragment /ɹə-/ dropped
    assert p.us[0] == "/ˈɹɛk.ɚd/"


def test_fd_read_drops_early_modern_and_au() -> None:
    p = fd.parse(fd_fixture("read"))
    assert p is not None
    assert p.uk == ["/ɹiːd/", "/ɹɛd/"]
    assert "/rɛːd/" not in p.uk + p.us + p.other
    assert "/ɹed/" not in p.uk + p.us + p.other  # AU/NZ only


def test_fd_ice_cream_untagged_goes_to_other() -> None:
    p = fd.parse(fd_fixture("ice_cream"))
    assert p is not None
    assert p.uk == ["/ˌaɪsˈkɹiːm/", "/ˈaɪsˌkɹiːm/"]
    assert p.other == ["/ˈaɪs.kɹiːm/", "/aɪsˈkɹiːm/"]


def test_fd_scope_us_tag() -> None:
    p = fd.parse(fd_fixture("scope"))
    assert p is not None and p.uk == ["/ˈskəʊp/"] and p.us == ["/ˈskoʊp/"]


@pytest.mark.parametrize("name", ["cease_and_desist", "qwzxv"])
def test_fd_no_pronunciation(name: str) -> None:
    assert fd.parse(fd_fixture(name)) is None


def test_fd_lookup_url_quoting() -> None:
    c, t, _ = client(lambda m, u: Response(200, {}, json.dumps(fd_fixture("ice_cream")).encode()))
    p = fd.FreeDictionarySource(c).lookup("ice cream")
    assert p is not None
    assert t.calls[0][1] == "https://freedictionaryapi.com/api/v1/entries/en/ice%20cream"


def test_fd_lookup_404() -> None:
    c, _, _ = client(lambda m, u: Response(404))
    assert fd.FreeDictionarySource(c).lookup("x") is None


# -- Wiktionary ----------------------------------------------------------------


def test_wk_record_two_etymologies_enpr_accent() -> None:
    p = wk.parse(wk_fixture("record"))
    assert p is not None
    assert p.uk == ["/ˈɹɛk.ɔːd/", "/ɹɪˈkɔːd/"]
    assert p.us == ["/ˈɹɛk.ɚd/", "/ˈɹɛk.ɔɹd/", "/ɹɪˈkɔɹd/"]
    assert not p.other  # Dutch /rəˈkoːr/ etc. not picked up


def test_wk_read() -> None:
    p = wk.parse(wk_fixture("read"))
    assert p is not None
    assert p.uk == ["/ɹiːd/", "/ɹɛd/"]
    assert p.us == ["/ɹid/", "/ɹɛd/"]


def test_wk_ice_cream_and_scope() -> None:
    ice = wk.parse(wk_fixture("ice_cream"))
    assert ice is not None and ice.uk == ["/ˌaɪsˈkɹiːm/", "/ˈaɪsˌkɹiːm/"]
    assert ice.other == ["/ˈaɪs.kɹiːm/", "/aɪsˈkɹiːm/"]
    scope = wk.parse(wk_fixture("scope"))
    assert scope is not None and scope.uk == ["/skəʊp/"] and scope.us == ["/skoʊp/"]


def test_wk_no_pronunciation() -> None:
    assert wk.parse(wk_fixture("cease_and_desist")) is None
    assert wk.parse(wk_fixture("insurmountable")) is None  # audio only
    assert wk.parse(wk_fixture("adjuster")) is None  # no Pronunciation section
    assert wk.parse("") is None


SYNTHETIC = """\
==English==
===Pronunciation 1===
* {{a|UK}} {{IPA|en|/ʃəʊ/}}, {{a|en|US}} {{IPA|en|/ʃoʊ/}}
* {{IPA|en|/x1/<a:RP>|/x2/<a:GA>|/x3/<q:obsolete>}}
* {{IPA|en|/y1/|/y2/|a1=RP|a2=Scotland|q1=some speakers}}
* {{IPA|en|/z1/|/z2/|q2=rare}}
* {{a|RP}}
** {{IPA|en|/n1/}}<ref>{{R:foo|{{IPA|en|/bad/}}}}</ref>
* {{IPA|fr|/fr/}} <!-- {{IPA|en|/hidden/}} -->
* {{enPR|ôr}}
====Noun====
{{IPA|en|/not-pron-section/}}
==French==
===Pronunciation===
* {{IPA|en|/french/}}
"""


def test_wk_label_forms() -> None:
    p = wk.parse(SYNTHETIC)
    assert p is not None
    assert p.uk == ["/ʃəʊ/", "/x1/", "/y1/", "/n1/"]
    assert p.us == ["/ʃoʊ/", "/x2/"]
    assert p.other == ["/z1/"]  # /x3/ obsolete, /z2/ rare, /y2/ Scotland dropped


def test_wk_iter_templates_nested() -> None:
    tpls = list(wk.iter_templates("{{IPA|en|/a/|ref={{R|x|y}}|a=RP}} {{a|US}}"))
    assert [t.name for t in tpls] == ["ipa", "a"]
    assert tpls[0].positional == ["en", "/a/"]
    assert tpls[0].named == {"ref": "{{R|x|y}}", "a": "RP"}


def _wk_handler(pages: dict[str, str]) -> tuple[list[str], Handler]:
    seen: list[str] = []

    def handler(m: str, url: str) -> Response:
        title = unquote(parse_qs(urlsplit(url).query)["title"][0])
        seen.append(title)
        return Response(200, {}, pages[title].encode()) if title in pages else Response(404)

    return seen, handler


def test_wk_redirect_followed_once() -> None:
    seen, handler = _wk_handler(
        {"Record": "#REDIRECT [[record]]", "record": wk_fixture("record")}
    )
    c, _, _ = client(handler)
    p = wk.WiktionarySource(c).lookup("Record")
    assert p is not None and p.uk[0] == "/ˈɹɛk.ɔːd/"
    assert seen == ["Record", "record"]


def test_wk_double_redirect_gives_up() -> None:
    seen, handler = _wk_handler({"a": "#REDIRECT [[b]]", "b": "#redirect [[c]]"})
    c, _, _ = client(handler)
    assert wk.WiktionarySource(c).lookup("a") is None
    assert "c" not in seen


def test_wk_exact_then_lowercase_and_memo() -> None:
    seen, handler = _wk_handler({"scope": wk_fixture("scope")})
    c, _, _ = client(handler)
    src = wk.WiktionarySource(c)
    assert src.lookup("Scope") is not None
    assert seen == ["Scope", "scope"]
    src.lookup("scope")  # memoised page, no new request
    assert seen == ["Scope", "scope"]


def test_wk_url_spaces() -> None:
    c, t, _ = client(lambda m, u: Response(404))
    wk.WiktionarySource(c).lookup("ice cream")
    assert t.calls[0][1] == "https://en.wiktionary.org/w/index.php?title=ice_cream&action=raw"
