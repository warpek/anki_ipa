"""Shared types for pronunciation sources."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

ACCENTS = ("uk", "us", "other")

# Accent labels (lower-cased) as they appear in FreeDictionaryAPI tags and
# Wiktionary `a=` / `{{a|...}}` labels. Anything else (AU, NZ, CA, Scotland,
# India, ...) is a non-target accent and dropped.
_UK_LABELS = frozenset(
    {
        "uk", "rp", "received pronunciation", "british", "britain", "british english",
        "england", "southern england", "standard southern british", "ssb",
        "modern rp", "traditional rp", "conservative rp", "non-rhotic",
    }
)
_US_LABELS = frozenset(
    {
        "us", "ga", "genam", "general american", "american", "usa",
        "united states", "american english", "us english",
    }
)
# Labels/qualifiers that make a transcription unsuitable (historic or marginal).
_HISTORIC = frozenset(
    {
        "early", "early modern", "early modern english", "middle english", "obsolete",
        "archaic", "dated", "nonstandard", "non-standard", "proscribed", "rare",
        "old-fashioned", "uncommon", "historical", "eme",
    }
)
_LABEL_SPLIT = re.compile(r"\s*[,;]\s*|\s+and\s+")


def split_labels(labels: Iterable[str]) -> list[str]:
    """Normalise labels: split `RP,GA`, strip `<<...>>` / wiki markup, lower-case."""
    out: list[str] = []
    for label in labels:
        label = re.sub(r"<<|>>|\[\[|\]\]|''", "", label)
        for part in _LABEL_SPLIT.split(label):
            part = part.strip().lower()
            if part:
                out.append(part)
    return out


def classify(labels: Iterable[str]) -> set[str] | None:
    """Accent buckets for a transcription with these labels.

    - no labels -> {"other"} (untagged)
    - historic/marginal label -> None (drop it)
    - otherwise the matching {"uk", "us"} subset (empty set = non-target accent)
    """
    parts = split_labels(labels)
    if not parts:
        return {"other"}
    if any(p in _HISTORIC for p in parts):
        return None
    buckets: set[str] = set()
    for p in parts:
        if p in _UK_LABELS:
            buckets.add("uk")
        elif p in _US_LABELS:
            buckets.add("us")
    return buckets


@dataclass
class Pronunciations:
    """Raw (un-normalised) IPA strings per accent bucket, in source order."""

    uk: list[str] = field(default_factory=list)
    us: list[str] = field(default_factory=list)
    other: list[str] = field(default_factory=list)  # untagged

    def bucket(self, accent: str) -> list[str]:
        if accent not in ACCENTS:
            return []
        lst: list[str] = getattr(self, accent)
        return lst

    def add(self, accent: str, ipa: str) -> None:
        lst = self.bucket(accent)
        if ipa and ipa not in lst:
            lst.append(ipa)

    def is_empty(self) -> bool:
        return not (self.uk or self.us or self.other)

    def to_dict(self) -> dict[str, list[str]]:
        return {"uk": list(self.uk), "us": list(self.us), "other": list(self.other)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Pronunciations:
        def lst(key: str) -> list[str]:
            v = data.get(key, [])
            return [str(x) for x in v] if isinstance(v, list) else []

        return cls(uk=lst("uk"), us=lst("us"), other=lst("other"))


class SourceDisabled(Exception):
    """Raised by a source that cannot work for the rest of the run.

    E.g. the Claude API rejected the key (401). The resolver disables the
    source with `str(exc)` as the reason and does not cache anything.
    """


@runtime_checkable
class Source(Protocol):
    name: str

    def lookup(self, text: str) -> Pronunciations | None:
        """Pronunciations for `text`, or None if the source has none.

        May raise `http.RateLimited` (resolver disables the source for the run)
        or `http.HttpError` (treated as a transient miss, not cached).
        """
        ...


def prefer_phonemic(pron: Pronunciations) -> Pronunciations:
    """Drop narrow `[...]` transcriptions from buckets that also have `/.../` ones."""
    for accent in ACCENTS:
        items = pron.bucket(accent)
        phonemic = [i for i in items if not i.strip().startswith("[")]
        if phonemic and len(phonemic) != len(items):
            items[:] = phonemic
    return pron
