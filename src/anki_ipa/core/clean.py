"""Turn a raw `English` field value into a lookup-ready headword.

Pipeline (order matters): HTML unescape (repeated, so `&amp;nbsp;` works) ->
drop `[sound:...]` -> strip tags -> unicode spaces/quotes/ellipsis -> collapse
whitespace -> remove parentheticals -> split off trailing sentence punctuation
-> detect comma-separated alternatives.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

_SOUND = re.compile(r"\[sound:[^\]]*\]")
# Block-level tags separate words; inline formatting tags do not ("inter<b>est</b>").
_BLOCK_TAG = re.compile(r"</?(?:div|br|p|li|ul|ol|tr|td|table|h\d)\b[^>]*>", re.I)
_ANY_TAG = re.compile(r"<[^>]*>")
_PAREN = re.compile(r"\(([^()]*)\)")
_TRAILING_PUNCT = re.compile(r"\s*([.!?]+)$")
_TRAILING_JUNK = re.compile(r"[\s,;:]+$")
_SPACES = re.compile(r"\s+")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,;:!?.])")

_CHAR_MAP = str.maketrans(
    {
        " ": " ",  # NBSP
        " ": " ",  # thin space
        " ": " ",  # narrow NBSP
        "​": "",  # zero-width space
        "﻿": "",  # BOM
        "‘": "'",
        "’": "'",
        "‛": "'",
        "′": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "…": "...",
    }
)


@dataclass(frozen=True)
class Headword:
    """A cleaned headword.

    `text` is the lookup string: parentheticals and trailing sentence punctuation
    removed. `alternatives` is always non-empty unless the headword is empty:
    `(text,)` normally, or one entry per comma-separated alternative.
    """

    raw: str
    text: str
    alternatives: tuple[str, ...]
    trailing_punct: str = ""
    annotations: tuple[str, ...] = ()  # trailing labels: "(verb)", "(formal)"
    optional: tuple[str, ...] = ()  # leading/inner optional words: "(your)"
    full_text: str = ""  # `text` with optional words kept (parens removed)

    @property
    def is_empty(self) -> bool:
        return not self.text

    @property
    def is_sentence(self) -> bool:
        return bool(self.trailing_punct)

    @property
    def has_alternatives(self) -> bool:
        return len(self.alternatives) > 1

    @property
    def is_phrase(self) -> bool:
        """More than one word in the lookup text (alternatives are never phrases)."""
        return not self.has_alternatives and len(self.text.split()) > 1


def strip_markup(raw: str) -> str:
    """HTML/unicode clean-up only: no parenthetical or punctuation handling."""
    s = raw
    for _ in range(5):  # unescape until stable: "&amp;amp;nbsp;" -> "\xa0"
        u = html.unescape(s)
        if u == s:
            break
        s = u
    s = _SOUND.sub(" ", s)
    s = _BLOCK_TAG.sub(" ", s)
    s = _ANY_TAG.sub("", s)
    s = s.translate(_CHAR_MAP)
    return _SPACES.sub(" ", s).strip()


def _tidy(s: str) -> str:
    s = _SPACES.sub(" ", s).strip()
    return _SPACE_BEFORE_PUNCT.sub(r"\1", s)


def _remove_parentheticals(s: str) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
    """Return (text without parens, text keeping optional words, annotations, optional)."""
    annotations: list[str] = []
    optional: list[str] = []
    dropped: list[str] = []
    kept: list[str] = []
    pos = 0
    for m in _PAREN.finditer(s):
        dropped.append(s[pos : m.start()])
        kept.append(s[pos : m.start()])
        inner = m.group(1).strip()
        trailing = not _PAREN.sub("", s[m.end() :]).strip(" .!?,;:")
        if trailing:
            annotations.append(inner)
        else:
            optional.append(inner)
            kept.append(inner)
        pos = m.end()
    dropped.append(s[pos:])
    kept.append(s[pos:])
    text, full = _tidy("".join(dropped)), _tidy("".join(kept))
    if not text:  # whole value was parenthesised: "(formal)" -> use its contents
        text = full = _tidy(s.replace("(", " ").replace(")", " "))
        annotations, optional = [], []
    return text, full, tuple(annotations), tuple(optional)


def _split_alternatives(text: str) -> tuple[str, ...]:
    """Commas mean alternatives only if every part is a single word.

    `adjustor, adjuster` -> two alternatives; `show, don't tell` -> one phrase.
    Callers must not pass sentences (those keep their commas).
    """
    if "," not in text:
        return (text,)
    parts = [p.strip() for p in text.split(",")]
    if len(parts) > 1 and all(p and " " not in p for p in parts):
        return tuple(dict.fromkeys(parts))
    return (text,)


def clean_headword(raw: str) -> Headword:
    s = strip_markup(raw)
    text, full, annotations, optional = _remove_parentheticals(s)

    punct = ""
    m = _TRAILING_PUNCT.search(text)
    if m and m.start() > 0:
        punct = m.group(1)
        text = text[: m.start()]
        full = _TRAILING_PUNCT.sub("", full)
    text = _TRAILING_JUNK.sub("", text)
    full = _TRAILING_JUNK.sub("", full)

    alternatives = () if not text else (text,) if punct else _split_alternatives(text)
    return Headword(
        raw=raw,
        text=text,
        alternatives=alternatives,
        trailing_punct=punct,
        annotations=annotations,
        optional=optional,
        full_text=full,
    )
