"""Post-process one IPA transcription returned by a source.

Apply `normalize_ipa` to each raw source result (per word or per whole phrase)
BEFORE composing and before re-appending sentence punctuation. Never run it on
composed output: it would strip a lone `[word]` placeholder's brackets.

Pipeline (each style step toggleable via `NormalizeConfig`):
strip delimiters -> char map -> tie bars -> syllable dots -> British vowel
rules -> optional `(..)` segments -> monosyllable stress strip (per word) ->
combining diacritics -> collapse spaces. The stress strip runs before the
diacritic strip so a syllabic consonant (`n̩`) still counts as a nucleus.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

_TIE_BARS = str.maketrans("", "", "͜͡")  # t͡ʃ -> tʃ
# A syllable dot sits between two non-space, non-dot characters: ˈɹɛk.ɔːd
_SYLLABLE_DOT = re.compile(r"(?<=[^\s.])\.(?=[^\s.])")
_SPACES = re.compile(r"\s+")
_DASHES = "-‐‑‒–—"

# British (OALD/Cambridge-style) vowels from Wiktionary-style modern RP.
# Ordered: longest/most specific first. Combining marks may sit between a
# vowel and what follows it, hence `_M*` in the lookarounds.
_M = "[\u0300-\u036f\u1dc0-\u1dff]"
VOWEL_RULES: tuple[tuple[str, str], ...] = (
    ("ɛː", "eə"),  # SQUARE: ɛː -> eə
    ("ɛ", "e"),  # DRESS (and ɛə -> eə)
    (f"a(?={_M}*ː)", "ɑ"),  # BATH/START: aː -> ɑː
    (f"a(?!{_M}*[ɪʊː])", "æ"),  # TRAP: a -> æ, but not in aɪ / aʊ
)
_VOWEL_RULES = tuple((re.compile(p), r) for p, r in VOWEL_RULES)

# Optional segments: dropped for linking/intrusive r, h and epenthetic stops
# (`fɪə(r)`, `(h)wɪtʃ`, `tʃɑːn(t)s`); any other `(x)` keeps its content
# without the parens (`ˈles(ə)n` -> `ˈlesən`, `n(j)uː` -> `njuː`).
_OPTIONAL_DROP = re.compile(r"\((?:r|ɹ|ɾ|h|t|d|p|k)\)")
_OPTIONAL_UNWRAP = re.compile(r"\(([^()\s]*)\)")

# Combining diacritics (non-syllabic ̯, advanced ̟, nasal ̃, syllabic ̩,
# voiceless ̥, tie bars ...) plus half-long ˑ. Never ː or stress marks.
_DIACRITICS = re.compile(f"{_M}|ˑ")
_SYLLABIC = "\u0329\u030d"
_NON_SYLLABIC_MARKS = re.compile("[\u0300-\u030c\u030e-\u0328\u032a-\u036f\u1dc0-\u1dff]")
_VOWELS = "iyɨʉɯuɪʏʊeøɘɵɤoəɚɝɛœɜɞʌɔæɐaɶɑɒ"
DIPHTHONGS = ("eɪ", "aɪ", "ɔɪ", "əʊ", "aʊ", "ɪə", "eə", "ʊə", "oʊ", "ɛə", "ɛɪ", "ʌɪ", "ɑɪ", "ɒʊ")
# One nucleus: a diphthong, a (long) vowel, or a syllabic consonant.
_NUCLEUS = re.compile(
    "|".join(DIPHTHONGS) + f"|[{_VOWELS}]|[^\\s{_VOWELS}][{_SYLLABIC}]"
)


@dataclass(frozen=True)
class NormalizeConfig:
    char_map: Mapping[str, str] = field(default_factory=lambda: {"ɹ": "r", "ɡ": "g"})
    strip_syllable_dots: bool = True
    british_vowels: bool = True  # VOWEL_RULES
    strip_optional_sounds: bool = True  # (r) dropped, (ə) unwrapped
    strip_diacritics: bool = True  # combining marks and ˑ
    strip_monosyllable_stress: bool = True  # ˈbelt -> belt

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> NormalizeConfig:
        """Build from the add-on config (`normalize_map`, `strip_syllable_dots`)."""
        default = cls()
        raw_map = config.get("normalize_map", default.char_map)
        char_map = (
            {str(k): str(v) for k, v in raw_map.items()}
            if isinstance(raw_map, Mapping)
            else dict(default.char_map)
        )
        def flag(key: str) -> bool:
            return bool(config.get(key, getattr(default, key)))

        return cls(
            char_map=char_map,
            strip_syllable_dots=flag("strip_syllable_dots"),
            british_vowels=flag("british_vowels"),
            strip_optional_sounds=flag("strip_optional_sounds"),
            strip_diacritics=flag("strip_diacritics"),
            strip_monosyllable_stress=flag("strip_monosyllable_stress"),
        )


DEFAULT = NormalizeConfig()


def strip_delimiters(ipa: str) -> str:
    """Remove one enclosing `/.../` or `[...]` pair (only if it encloses everything)."""
    s = ipa.strip()
    if len(s) >= 2:
        if s[0] == "/" and s[-1] == "/" and "/" not in s[1:-1]:
            return s[1:-1].strip()
        if s[0] == "[" and s[-1] == "]" and not any(c in s[1:-1] for c in "[]"):
            return s[1:-1].strip()
    return s


def normalize_ipa(ipa: str, config: NormalizeConfig = DEFAULT) -> str:
    s = strip_delimiters(ipa)
    for src, dst in config.char_map.items():
        s = s.replace(src, dst)
    s = s.translate(_TIE_BARS)
    if config.strip_syllable_dots:
        s = _SYLLABLE_DOT.sub("", s)
    if config.british_vowels:
        s = british_vowels(s)
    if config.strip_optional_sounds:
        s = strip_optional_sounds(s)
    s = _SPACES.sub(" ", s).strip()
    if config.strip_monosyllable_stress:
        s = strip_monosyllable_stress(s)
    if config.strip_diacritics:
        s = _DIACRITICS.sub("", s)
    return s


def british_vowels(ipa: str) -> str:
    """Apply `VOWEL_RULES` in order (`ˈsasi` -> `ˈsæsi`, `tɛl` -> `tel`)."""
    for pattern, repl in _VOWEL_RULES:
        ipa = pattern.sub(repl, ipa)
    return ipa


def strip_optional_sounds(ipa: str) -> str:
    """`ˌɪntəˈfɪə(r)` -> `ˌɪntəˈfɪə`; `ˈles(ə)n` -> `ˈlesən`."""
    return _OPTIONAL_UNWRAP.sub(r"\1", _OPTIONAL_DROP.sub("", ipa))


def count_nuclei(word: str) -> int:
    """Vowel nuclei in one transcribed word; diphthongs and long vowels count once."""
    return len(_NUCLEUS.findall(_NON_SYLLABIC_MARKS.sub("", word)))


def strip_monosyllable_stress(ipa: str) -> str:
    """Drop a leading `ˈ`/`ˌ` from every space-separated word with one nucleus."""
    return " ".join(
        w[1:] if w[:1] in ("ˈ", "ˌ") and count_nuclei(w) == 1 else w
        for w in ipa.split(" ")
    )


def is_fragment(ipa: str) -> bool:
    """True for empty values and affix fragments like `/ɹə-/` or `-ɪŋ` (unusable)."""
    s = strip_delimiters(ipa)
    return not s or s[0] in _DASHES or s[-1] in _DASHES or s.startswith("...")
