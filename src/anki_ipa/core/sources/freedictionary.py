"""FreeDictionaryAPI.com (Wiktionary-derived JSON, CC BY-SA 4.0).

Response shape (recorded 2026-10-11, see tests/fixtures/freedictionary/):

    {"word": "record",
     "entries": [{"language": {"code": "en", "name": "English"},
                  "partOfSpeech": "noun",
                  "pronunciations": [{"type": "ipa", "text": "/ˈɹɛk.ɔːd/",
                                      "tags": ["Received Pronunciation"]}, ...],
                  "senses": [...], ...}, ...],
     "source": {"url": "https://en.wiktionary.org/wiki/record", ...}}

An unknown word is HTTP 200 with `"entries": []`. Tags are free-form accent
names ("Received Pronunciation", "UK", "General American", "US", "General
Australian", ...); "Early Modern" arrives split as ["Early", "Modern"].
Phrases without their own pronunciation section have `"pronunciations": []`.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from ..http import HttpClient, default_client
from ..normalize import is_fragment
from .base import Pronunciations, classify, prefer_phonemic

BASE_URL = "https://freedictionaryapi.com/api/v1/entries/en/"


def parse(data: Any) -> Pronunciations | None:
    """Pronunciations from a decoded API response (None if there are none)."""
    if not isinstance(data, dict):
        return None
    pron = Pronunciations()
    for entry in data.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        lang = entry.get("language")
        if isinstance(lang, dict) and lang.get("code") not in (None, "en"):
            continue
        for p in entry.get("pronunciations") or []:
            if not isinstance(p, dict) or p.get("type", "ipa") != "ipa":
                continue
            text = str(p.get("text") or "").strip()
            if not text or is_fragment(text):
                continue
            tags = p.get("tags") or []
            buckets = classify(str(t) for t in tags if isinstance(t, str))
            for accent in sorted(buckets or ()):
                pron.add(accent, text)
    prefer_phonemic(pron)
    return None if pron.is_empty() else pron


class FreeDictionarySource:
    name = "freedictionary"

    def __init__(self, http: HttpClient | None = None, base_url: str = BASE_URL) -> None:
        self.http = http or default_client()
        self.base_url = base_url

    def url(self, text: str) -> str:
        return self.base_url + urllib.parse.quote(text.strip(), safe="")

    def lookup(self, text: str) -> Pronunciations | None:
        if not text.strip():
            return None
        return parse(self.http.get_json(self.url(text)))
