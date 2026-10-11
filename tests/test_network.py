"""Live smoke tests against the real sources (opt-in: `pytest -m network`).

Kept to a handful of requests: FreeDictionaryAPI allows ~1000 req/h and
Wiktionary asks for polite, serial access.
"""

from __future__ import annotations

import pytest

from anki_ipa.core.http import HttpClient
from anki_ipa.core.sources.freedictionary import FreeDictionarySource
from anki_ipa.core.sources.wiktionary import WiktionarySource

pytestmark = pytest.mark.network

HTTP = HttpClient()


def test_freedictionary_live_record() -> None:
    p = FreeDictionarySource(HTTP).lookup("record")
    assert p is not None
    assert "/ˈɹɛk.ɔːd/" in p.uk


def test_freedictionary_live_unknown() -> None:
    assert FreeDictionarySource(HTTP).lookup("qwzxv") is None


def test_wiktionary_live_scope() -> None:
    p = WiktionarySource(HTTP).lookup("scope")
    assert p is not None
    assert "/skəʊp/" in p.uk
