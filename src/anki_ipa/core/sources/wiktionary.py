"""English Wiktionary raw wikitext (`action=raw`), CC BY-SA 4.0.

Only the `==English==` section is read, and inside it every
`Pronunciation` / `Pronunciation N` subsection (one per etymology, at
any heading depth). Typical lines (recorded 2026-10-11):

    * {{enPR|rĕkʹôrd|a=RP}}, {{IPA|en|/ˈɹɛk.ɔːd/}}         <- accent on enPR
    * {{IPA|en|/ɹɪˈkɔːd/|/ɹə-/|a=RP}}                      <- fragment dropped
    * {{IPA|en|/skəʉp/|[skɐʉp]|a=AU|q2=some speakers}}
    * {{a|UK}} {{IPA|en|/.../}}                            <- label template
    * {{IPA|en|/x/<a:RP>|/y/<a:GA>}}                       <- inline modifiers

Accent labels for one IPA item, most specific first: inline `<a:...>` or
`aN=`; the template's `a=`; the last `{{a|...}}`/`{{accent|...}}` on the
same line (or an `a=` on a preceding `{{enPR}}`); a label-only parent bullet.
`{{enPR}}` transcriptions themselves are ignored.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field

from ..http import HttpClient, default_client
from ..normalize import is_fragment
from .base import Pronunciations, classify, prefer_phonemic, split_labels

BASE_URL = "https://en.wiktionary.org/w/index.php"

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_REF = re.compile(r"<ref\b[^>/]*/>|<ref\b[^>]*>.*?</ref>", re.S | re.I)
_ENGLISH = re.compile(r"^==\s*English\s*==\s*$", re.M)
_L2 = re.compile(r"^==[^=].*?==\s*$", re.M)
_HEADING = re.compile(r"^(=+)\s*(.*?)\s*\1\s*$")
_PRON_HEADING = re.compile(r"^pronunciation(?:\s+\d+)?$", re.I)
_REDIRECT = re.compile(r"^\s*#\s*redirect\s*:?\s*\[\[([^\]|#]+)", re.I)
_INLINE_MOD = re.compile(r"<(\w+):([^<>]*)>")
_NAMED = re.compile(r"^\s*([A-Za-z_]+)(\d*)\s*=(.*)$", re.S)

_LABEL_TEMPLATES = {"a", "accent", "accents"}
_QUALIFIER_TEMPLATES = {"q", "qual", "qualifier", "i", "qf", "qq"}


@dataclass
class Template:
    name: str
    positional: list[str] = field(default_factory=list)
    named: dict[str, str] = field(default_factory=dict)


def _split_top(s: str, sep: str = "|") -> list[str]:
    """Split on `sep` outside nested `{{...}}` / `[[...]]`."""
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    i = 0
    while i < len(s):
        two = s[i : i + 2]
        if two in ("{{", "[["):
            depth += 1
            buf.append(two)
            i += 2
            continue
        if two in ("}}", "]]") and depth:
            depth -= 1
            buf.append(two)
            i += 2
            continue
        if s[i] == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(s[i])
        i += 1
    parts.append("".join(buf))
    return parts


def iter_templates(line: str) -> Iterator[Template]:
    """Top-level `{{...}}` templates on a line, in order (nested ones stay inside)."""
    i = 0
    while True:
        start = line.find("{{", i)
        if start < 0:
            return
        depth = 0
        j = start
        end = -1
        while j < len(line) - 1:
            if line.startswith("{{", j):
                depth += 1
                j += 2
            elif line.startswith("}}", j):
                depth -= 1
                j += 2
                if depth == 0:
                    end = j
                    break
            else:
                j += 1
        if end < 0:
            return  # unbalanced: give up on the rest of the line
        parts = _split_top(line[start + 2 : end - 2])
        tpl = Template(parts[0].strip().lower())
        for p in parts[1:]:
            m = _NAMED.match(p)
            if m:
                tpl.named[m.group(1).strip() + m.group(2)] = m.group(3).strip()
            else:
                tpl.positional.append(p.strip())
        yield tpl
        i = end


def english_section(wikitext: str) -> str:
    m = _ENGLISH.search(wikitext)
    if not m:
        return ""
    rest = wikitext[m.end() :]
    nxt = _L2.search(rest)
    return rest[: nxt.start()] if nxt else rest


def pronunciation_blocks(section: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] | None = None
    for line in section.splitlines():
        h = _HEADING.match(line.strip())
        if h:
            if current is not None:
                blocks.append("\n".join(current))
            current = [] if _PRON_HEADING.match(h.group(2)) else None
        elif current is not None:
            current.append(line)
    if current is not None:
        blocks.append("\n".join(current))
    return blocks


def _labels(value: str | None) -> list[str] | None:
    if value is None:
        return None
    labels = split_labels([value])
    return labels or None


def _add(pron: Pronunciations, ipa: str, labels: list[str], qualifiers: list[str]) -> None:
    ipa = ipa.strip()
    if not ipa or is_fragment(ipa) or not (ipa[0] in "/[" and ipa[-1] in "/]"):
        return
    if classify(qualifiers or []) is None:  # obsolete/rare/... qualifier
        return
    buckets = classify(labels)
    for accent in sorted(buckets or ()):
        pron.add(accent, ipa)


def parse_block(block: str, pron: Pronunciations) -> None:
    inherited: dict[int, list[str]] = {}  # bullet depth -> labels from a label-only line
    for raw_line in block.splitlines():
        stripped = raw_line.lstrip()
        depth = len(stripped) - len(stripped.lstrip("*#:"))
        if depth == 0:
            continue
        for d in [d for d in inherited if d >= depth]:
            del inherited[d]
        parent = [inherited[d] for d in sorted(inherited) if d < depth]
        line_labels: list[str] = parent[-1] if parent else []
        line_quals: list[str] = []
        had_ipa = had_label = False
        for tpl in iter_templates(stripped):
            if tpl.name in _LABEL_TEMPLATES:
                args = tpl.positional
                if args and args[0] == "en":
                    args = args[1:]
                line_labels = split_labels(args)
                had_label = True
            elif tpl.name in _QUALIFIER_TEMPLATES:
                line_quals += split_labels(tpl.positional)
            elif tpl.name == "enpr":
                enpr_labels = _labels(tpl.named.get("a"))
                if enpr_labels:
                    line_labels = enpr_labels
                    had_label = True
            elif tpl.name == "ipa":
                if not tpl.positional or tpl.positional[0] != "en":
                    continue
                had_ipa = True
                tpl_labels = _labels(tpl.named.get("a"))
                tpl_quals = split_labels([tpl.named.get("q", ""), tpl.named.get("qq", "")])
                for n, item in enumerate(tpl.positional[1:], start=1):
                    mods = {k.lower(): v for k, v in _INLINE_MOD.findall(item)}
                    ipa = _INLINE_MOD.sub("", item)
                    labels = (
                        _labels(mods.get("a"))
                        or _labels(tpl.named.get(f"a{n}"))
                        or tpl_labels
                        or line_labels
                    )
                    quals = (
                        line_quals
                        + tpl_quals
                        + split_labels(
                            [
                                mods.get("q", ""),
                                mods.get("qq", ""),
                                tpl.named.get(f"q{n}", ""),
                                tpl.named.get(f"qq{n}", ""),
                            ]
                        )
                    )
                    _add(pron, ipa, labels or [], quals)
        if had_label and not had_ipa:
            inherited[depth] = line_labels


def parse(wikitext: str) -> Pronunciations | None:
    """Pronunciations from a page's raw wikitext (English section only)."""
    text = _REF.sub("", _COMMENT.sub("", wikitext))
    pron = Pronunciations()
    for block in pronunciation_blocks(english_section(text)):
        parse_block(block, pron)
    prefer_phonemic(pron)
    return None if pron.is_empty() else pron


def redirect_target(wikitext: str) -> str | None:
    m = _REDIRECT.match(wikitext)
    return m.group(1).strip() if m else None


class WiktionarySource:
    name = "wiktionary"

    def __init__(
        self, http: HttpClient | None = None, base_url: str = BASE_URL, page_cache: int = 512
    ) -> None:
        self.http = http or default_client()
        self.base_url = base_url
        self._pages: dict[str, str | None] = {}  # per-run memo of fetched pages
        self._page_cache = page_cache

    def url(self, title: str) -> str:
        t = urllib.parse.quote(title.strip().replace(" ", "_"), safe="")
        return f"{self.base_url}?title={t}&action=raw"

    def fetch(self, title: str) -> str | None:
        """Raw wikitext (None if missing), following one redirect."""
        if title in self._pages:
            return self._pages[title]
        text = self.http.get_text(self.url(title))
        if text is not None:
            target = redirect_target(text)
            if target and target != title:
                text = self.http.get_text(self.url(target))
                if text is not None and redirect_target(text):
                    text = None  # double redirect: give up
        if len(self._pages) >= self._page_cache:
            self._pages.clear()
        self._pages[title] = text
        return text

    def lookup(self, text: str) -> Pronunciations | None:
        text = " ".join(text.split())
        if not text:
            return None
        for title in dict.fromkeys([text, text.lower()]):
            page = self.fetch(title)
            if page is not None:
                result = parse(page)
                if result is not None:
                    return result
        return None
