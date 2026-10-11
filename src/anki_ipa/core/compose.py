"""Tokenize a cleaned phrase and compose per-word IPA into the phrase IPA.

Lookups are injected (`Lookup = Callable[[str], str | None]`) and should return
already-normalized IPA (see `normalize.normalize_ipa`).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

Lookup = Callable[[str], str | None]

# Function words inside multi-word text: (weak form, strong form). The weak
# form is used mid-phrase, the strong one in final position (phrasal-verb
# particle or stranded preposition: `clock in`, `what are you looking at`) or
# when weak forms are switched off. Overrides the dictionary entirely (the
# sources list odd first variants: `in` -> `ən`, `a` -> `ʌ`). Articles have no
# stranded use, so they keep the weak form everywhere. Never used for
# single-word headwords. `that`, `you`, `your` are deliberately absent.
FUNCTION_WORDS: Mapping[str, tuple[str, str]] = {
    "a": ("ə", "ə"),
    "an": ("ən", "ən"),
    "the": ("ðə", "ðə"),
    "to": ("tə", "tuː"),
    "and": ("ənd", "ænd"),
    "of": ("əv", "ɒv"),
    "for": ("fə", "fɔː"),
    "at": ("ət", "æt"),
    "from": ("frəm", "frɒm"),
    "was": ("wəz", "wɒz"),
    "were": ("wə", "wɜː"),
    "can": ("kən", "kæn"),
    "some": ("səm", "sʌm"),
    "but": ("bət", "bʌt"),
    "than": ("ðən", "ðæn"),
    "as": ("əz", "æz"),
    # strong only
    "in": ("ɪn", "ɪn"),
    "on": ("ɒn", "ɒn"),
    "up": ("ʌp", "ʌp"),
    "off": ("ɒf", "ɒf"),
    "out": ("aʊt", "aʊt"),
    "by": ("baɪ", "baɪ"),
}

# Words keep internal apostrophes/hyphens: don't, you've, well-known, o'clock.
_TOKEN = re.compile(r"(?P<word>[^\W_]+(?:['’-][^\W_]+)*)|(?P<punct>\.\.\.|[^\w\s])")
# Punctuation kept in the IPA, glued to the preceding word; anything else
# (quotes, brackets, dashes, &) is dropped.
_KEPT_PUNCT = {",", ";", ":", "!", "?", ".", "..."}


@dataclass(frozen=True)
class Token:
    text: str
    is_word: bool


@dataclass(frozen=True)
class Composed:
    ipa: str
    missing: tuple[str, ...] = ()  # unresolved words, in order, de-duplicated
    from_table: int = 0  # words taken from `function_words`

    @property
    def complete(self) -> bool:
        return not self.missing


def tokenize(text: str) -> list[Token]:
    return [
        Token(m.group(), m.lastgroup == "word")
        for m in _TOKEN.finditer(text)
    ]


def words(text: str) -> list[str]:
    return [t.text for t in tokenize(text) if t.is_word]


def placeholder(word: str) -> str:
    return f"[{word}]"


def with_punct(ipa: str, trailing_punct: str) -> str:
    """Re-append the headword's sentence punctuation (after normalization!)."""
    return f"{ipa}{trailing_punct}" if ipa else ipa


def join_alternatives(ipas: Iterable[str]) -> str:
    """`ipa1, ipa2` in the user's style; empties and duplicates dropped."""
    return ", ".join(dict.fromkeys(i for i in ipas if i))


def compose(
    text: str,
    lookup: Lookup,
    trailing_punct: str = "",
    *,
    function_words: Mapping[str, tuple[str, str]] | None = None,
    weak_forms: bool = True,
) -> Composed:
    """Look up each word token; unresolved ones become `[word]`.

    Tokens are passed to `lookup` exactly as written (case preserved), each
    distinct token at most once per call. With `function_words` (e.g.
    `FUNCTION_WORDS`) and at least two words, listed words (case-insensitive)
    skip `lookup`: weak form mid-phrase, strong form when final (last word or
    followed by `, ; : . ! ? ...`) or when `weak_forms` is False.
    """
    tokens = tokenize(text)
    use_table = function_words is not None and sum(t.is_word for t in tokens) > 1
    cache: dict[str, str | None] = {}
    parts: list[str] = []
    missing: list[str] = []
    from_table = 0
    for i, tok in enumerate(tokens):
        if not tok.is_word:
            if tok.text in _KEPT_PUNCT and parts:
                parts[-1] += tok.text
            continue
        forms = function_words.get(tok.text.lower()) if use_table and function_words else None
        if forms is not None:
            # next word or kept punctuation (dropped quotes/brackets skipped)
            nxt = next((t for t in tokens[i + 1 :] if t.is_word or t.text in _KEPT_PUNCT), None)
            final = nxt is None or not nxt.is_word
            parts.append(forms[0] if weak_forms and not final else forms[1])
            from_table += 1
            continue
        if tok.text not in cache:
            cache[tok.text] = lookup(tok.text)
        ipa = cache[tok.text]
        if ipa:
            parts.append(ipa)
        else:
            parts.append(placeholder(tok.text))
            if tok.text not in missing:
                missing.append(tok.text)
    return Composed(with_punct(" ".join(parts), trailing_punct), tuple(missing), from_table)
