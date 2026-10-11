"""Resolve a raw `English` field value to IPA through cache -> sources.

Per alternative of the cleaned headword:
1. whole text through every source in order (first source with a usable
   transcription in a preferred accent wins); up to `max_variants` distinct
   normalised variants of the best accent bucket;
2. otherwise, for multi-word text with `word_by_word`, compose per-token
   lookups (same chain, token as written, then lower-cased) with `[word]`
   placeholders; function words come from `compose.FUNCTION_WORDS` instead
   (weak forms mid-phrase when `weak_forms`, strong forms when final);
3. re-append sentence punctuation; finally join alternatives with `, `.
4. optional fallback (Claude): when the result is missing, or partial and
   `fallback_for_partial`, the WHOLE cleaned headword (alternatives re-joined
   with `, `, sentence punctuation kept) goes to the fallback source in one
   request; a usable reply replaces the result (`source="claude"`,
   `complete=True`). Its reply is normalised like any source result (weak
   forms are left to the model), cached under the source name.

Every source lookup is cached (negative results too). A source raising
`RateLimited` is disabled for the rest of the run; other `HttpError`s are a
transient miss (not cached), and `max_errors` consecutive ones disable the
source as well (e.g. offline). `SourceDisabled` (e.g. Claude 401) disables it
immediately.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .cache import Cache
from .clean import Headword, clean_headword
from .compose import FUNCTION_WORDS, compose, join_alternatives, with_punct, words
from .http import USER_AGENT, HttpClient, HttpError, Interrupted, RateLimited, cancel_scope
from .normalize import NormalizeConfig, is_fragment, normalize_ipa
from .sources.base import ACCENTS, Pronunciations, Source, SourceDisabled
from .sources.claude import ENV_KEY, ClaudeSource
from .sources.claude import DEFAULT_MODEL as CLAUDE_DEFAULT_MODEL
from .sources.freedictionary import FreeDictionarySource
from .sources.wiktionary import WiktionarySource

CancelCheck = Callable[[], bool]
Progress = Callable[[int, int, str], None]  # (done, total, current headword)

SOURCE_CLASSES: dict[str, Callable[[HttpClient], Source]] = {
    "freedictionary": FreeDictionarySource,
    "wiktionary": WiktionarySource,
}


BUILTIN_SOURCE = "builtin"  # `Result.source` when only built-in tables contributed

# Single words whose free-source transcription disagrees with British
# learner's-dictionary convention (OALD/Cambridge) and the deck's style.
# Matched case-insensitively on a whole one-word query (headword or token),
# before any source lookup; the value is final, already in deck style.
# Keep this tiny: it is a patch list, not a dictionary.
WORD_OVERRIDES: dict[str, str] = {
    "until": "ənˈtɪl",  # FD/Wiktionary list RP /ʌnˈtɪl/ first
}
CLAUDE = "claude"
NO_KEY_REASON = (
    "no API key: set claude.api_key in the add-on config or the "
    f"{ENV_KEY} environment variable"
)
_TRAILING_PUNCT = re.compile(r"[\s.!?…,;:]+$")


_SKELETON_DROP = re.compile("[̀-ͯ᷀-᷿ˈˌʰˑ()]")


def _skeleton(ipa: str) -> str:
    """IPA without diacritics, stress, aspiration and optional-sound parens."""
    return _SKELETON_DROP.sub("", ipa)


def _agree(phonemic: Sequence[str], narrow: Sequence[str]) -> bool:
    """True if some phonemic form matches some narrow form (by skeleton)."""
    keys = {_skeleton(n) for n in narrow}
    return any(_skeleton(p) in keys for p in phonemic)


class Cancelled(Exception):
    """Raised from `resolve` when the cancel hook returns True."""


@dataclass(frozen=True)
class Result:
    ipa: str | None
    source: str | None  # first contributing source name, or "mixed"
    complete: bool
    missing: list[str] = field(default_factory=list)  # unresolved words


@dataclass
class _Hit:
    variants: list[str]
    source: str


class Resolver:
    def __init__(
        self,
        sources: Sequence[Source],
        cache: Cache | None = None,
        norm_cfg: NormalizeConfig | None = None,
        accent_preference: Sequence[str] = ("uk", "us"),
        word_by_word: bool = True,
        *,
        weak_forms: bool = True,
        fallback: Source | None = None,
        fallback_for_partial: bool = True,
        max_variants: int = 2,
        max_phrase_words: int = 6,
        max_errors: int = 3,
        cancel: CancelCheck | None = None,
        progress: Progress | None = None,
        overrides: Mapping[str, str] | None = None,
    ) -> None:
        self.sources = list(sources)
        self.overrides = {
            k.lower(): v for k, v in (WORD_OVERRIDES if overrides is None else overrides).items()
        }
        self.cache = cache if cache is not None else Cache(None)
        self.norm_cfg = norm_cfg or NormalizeConfig()
        prefs = [a for a in accent_preference if a in ACCENTS]
        self.accents = list(dict.fromkeys([*prefs, "other"]))
        self.word_by_word = word_by_word
        self.weak_forms = weak_forms
        self.fallback = fallback
        self.fallback_for_partial = fallback_for_partial
        # Name of the configured fallback, even when it could not be built
        # (no API key): `fallback_status()` reports on it.
        self.fallback_requested: str | None = fallback.name if fallback is not None else None
        self.max_variants = max_variants
        self.max_phrase_words = max_phrase_words
        self.max_errors = max_errors
        self.cancel = cancel
        self.progress = progress
        self.disabled: dict[str, str] = {}  # source name -> reason
        self.unexpected_errors: list[str] = []  # non-HTTP exceptions from sources
        self._errors: dict[str, int] = {}

    # -- single source ----------------------------------------------------
    def _check_cancel(self) -> None:
        if self.cancel is not None and self.cancel():
            raise Cancelled()

    def _source_lookup(self, source: Source, query: str) -> Pronunciations | None:
        entry = self.cache.get(source.name, query)
        if entry is not None:
            return None if entry.is_negative else Pronunciations.from_dict(entry.value)
        if source.name in self.disabled:
            return None
        self._check_cancel()
        try:
            # Long Retry-After / throttle waits inside the client stop early
            # when the batch is cancelled.
            with cancel_scope(self.cancel):
                pron = source.lookup(query)
        except Interrupted:
            raise Cancelled() from None
        except RateLimited as e:
            self.disabled[source.name] = f"rate limited: {e}"
            return None
        except SourceDisabled as e:
            self.disabled[source.name] = str(e)
            return None
        except Exception as e:
            # Network errors (HttpError) and unexpected source bugs (e.g. a
            # response shape the parser chokes on) are a transient miss for
            # this word only: never cached, never abort the batch. Cancelled
            # is raised by `_check_cancel` before the lookup, or from an
            # `Interrupted` wait above.
            n = self._errors[source.name] = self._errors.get(source.name, 0) + 1
            if not isinstance(e, HttpError):
                self.unexpected_errors.append(f"{source.name} {query!r}: {type(e).__name__}: {e}")
            if n >= self.max_errors:
                self.disabled[source.name] = f"{n} consecutive errors, last: {e}"
            return None
        self._errors[source.name] = 0
        self.cache.put(source.name, query, None if pron is None else pron.to_dict())
        return pron

    def _variants(self, pron: Pronunciations) -> list[str]:
        """Up to `max_variants` normalised variants, best accent first.

        Order (2026-10-11, Phase 4 fix for "four"):
        1. phonemic `/.../` forms of the preferred tagged buckets (UK, then US);
        2. untagged phonemic forms, but only when they agree with the narrow
           `[...]` forms of the first tagged bucket that has some (or no tagged
           narrow form exists): FD's "to" has UK `[tu̟ː]` + untagged
           `/tuː/, /tə/` -> `tuː, tə`, while "four" has UK `[fɔː]` + untagged
           `/foɹ/` (US-like) -> the UK narrow form wins;
        3. the first narrow form of that tagged bucket (narrow lists are
           allophonic detail, not alternative pronunciations: `fɔː`, not
           `fɔː, foː`);
        4. untagged phonemic forms that did not agree, then the first untagged
           narrow form.
        """
        tagged = [a for a in self.accents if a != "other"]
        for accent in tagged:
            out = self._collect(pron.bucket(accent), phonemic=True)
            if out:
                return out[: self.max_variants]
        untagged = self._collect(pron.other, phonemic=True)
        narrow: list[str] = []
        for accent in tagged:
            narrow = self._collect(pron.bucket(accent), phonemic=False)
            if narrow:
                break
        if untagged and (not narrow or _agree(untagged, narrow)):
            return untagged[: self.max_variants]
        if narrow:
            return narrow[:1]
        if untagged:
            return untagged[: self.max_variants]
        return self._collect(pron.other, phonemic=False)[:1]

    def _collect(self, raws: Sequence[str], *, phonemic: bool) -> list[str]:
        """Distinct normalised, non-fragment forms of one kind, in source order."""
        out: list[str] = []
        for raw in raws:
            if is_fragment(raw) or raw.strip().startswith("[") == phonemic:
                continue
            ipa = normalize_ipa(raw, self.norm_cfg)
            if ipa and ipa not in out:
                out.append(ipa)
        return out

    def _chain(self, query: str) -> _Hit | None:
        override = self.overrides.get(query.strip().lower())
        if override:
            return _Hit([override], BUILTIN_SOURCE)
        for source in self.sources:
            pron = self._source_lookup(source, query)
            if pron is not None:
                variants = self._variants(pron)
                if variants:
                    return _Hit(variants, source.name)
        return None

    # -- alternatives -----------------------------------------------------
    def _resolve_alternative(
        self, alt: str, trailing_punct: str
    ) -> tuple[str | None, list[str], list[str]]:
        """(ipa, contributing sources, missing words) for one alternative."""
        n_words = len(words(alt))
        if n_words <= self.max_phrase_words:
            hit = self._chain(alt)
            if hit is not None:
                ipa = join_alternatives(with_punct(v, trailing_punct) for v in hit.variants)
                return ipa, [hit.source], []
        if n_words <= 1 or not self.word_by_word:
            return None, [], [alt] if n_words <= 1 else words(alt)

        used: list[str] = []

        def word_lookup(token: str) -> str | None:
            hit = self._chain(token)
            if hit is None and token.lower() != token:
                hit = self._chain(token.lower())
            if hit is None:
                return None
            used.append(hit.source)
            return hit.variants[0]

        composed = compose(
            alt,
            word_lookup,
            trailing_punct,
            function_words=FUNCTION_WORDS,
            weak_forms=self.weak_forms,
        )
        if not used and not composed.from_table:  # placeholders only are useless
            return None, [], list(composed.missing)
        if not used:
            used.append(BUILTIN_SOURCE)
        return composed.ipa, used, list(composed.missing)

    def resolve(self, raw_field_value: str) -> Result:
        hw: Headword = clean_headword(raw_field_value)
        if hw.is_empty:
            return Result(None, None, False, [])
        ipas: list[str] = []
        sources: list[str] = []
        missing: list[str] = []
        all_resolved = True
        for alt in hw.alternatives:
            ipa, used, miss = self._resolve_alternative(alt, hw.trailing_punct)
            if ipa:
                ipas.append(ipa)
            else:
                all_resolved = False
            sources += used
            missing += [m for m in miss if m not in missing]
        joined = join_alternatives(ipas) or None
        distinct = list(dict.fromkeys(sources))
        source = None if not distinct else distinct[0] if len(distinct) == 1 else "mixed"
        result = Result(joined, source, joined is not None and all_resolved and not missing, missing)
        if not result.complete and (result.ipa is None or self.fallback_for_partial):
            return self._fallback_result(hw) or result
        return result

    # -- fallback (Claude) ------------------------------------------------
    def _fallback_result(self, hw: Headword) -> Result | None:
        fb = self.fallback
        if fb is None or fb.name in self.disabled:
            return None
        query = ", ".join(hw.alternatives) + hw.trailing_punct
        pron = self._source_lookup(fb, query)
        raws = [] if pron is None else pron.uk or pron.us or pron.other
        if not raws:
            return None
        ipa = _TRAILING_PUNCT.sub("", normalize_ipa(raws[0], self.norm_cfg))
        if hw.has_alternatives:
            ipa = join_alternatives(part.strip() for part in ipa.split(","))
        if not ipa or is_fragment(ipa):
            return None
        return Result(with_punct(ipa, hw.trailing_punct), fb.name, True, [])

    def fallback_status(self) -> str | None:
        """One line for the summary; None when no fallback is configured."""
        name = self.fallback_requested
        if name is None:
            return None
        usage_text = getattr(self.fallback, "usage_text", None)
        usage = f" ({usage_text()})" if callable(usage_text) else ""
        if name in self.disabled:
            return f"disabled: {self.disabled[name]}{usage}"
        return f"on{usage}"

    def resolve_many(self, raw_values: Sequence[str]) -> list[Result]:
        """Resolve in order; stops early (shorter list) when cancelled.

        Calls `progress(done, total, headword)` before each item and flushes
        the cache at the end.
        """
        results: list[Result] = []
        total = len(raw_values)
        try:
            for i, raw in enumerate(raw_values):
                self._check_cancel()
                if self.progress is not None:
                    self.progress(i, total, raw)
                results.append(self.resolve(raw))
            if self.progress is not None:
                self.progress(total, total, "")
        except Cancelled:
            pass
        finally:
            self.cache.save()
        return results


def build_resolver(
    config: Mapping[str, Any],
    cache_path: str | None = None,
    *,
    http: HttpClient | None = None,
    cache: Cache | None = None,
    cancel: CancelCheck | None = None,
    progress: Progress | None = None,
    user_agent: str = USER_AGENT,
) -> Resolver:
    """Resolver from the add-on config dict.

    `sources` names map through `SOURCE_CLASSES` (unknown names, including
    "claude", are skipped: Claude is never part of the per-word chain). The
    Claude fallback comes from the `claude` dict: `enabled`, `api_key` (empty:
    env `ANTHROPIC_API_KEY`), `model`, `for_partial`. Enabled without any key,
    it is reported as disabled (`NO_KEY_REASON`) and no request is made. The
    caller resolves `cache_path` (e.g. `<addon dir>/user_files/ipa_cache.json`)
    and the `user_agent` (from the add-on's manifest; ignored when `http` is
    given).
    """
    client = http or HttpClient(user_agent=user_agent)
    names = config.get("sources", ["freedictionary", "wiktionary"])
    sources = [SOURCE_CLASSES[n](client) for n in names if n in SOURCE_CLASSES]
    prefs = config.get("accent_preference", ["uk", "us"])
    ccfg = config.get("claude")
    ccfg = ccfg if isinstance(ccfg, Mapping) else {}
    claude_on = bool(ccfg.get("enabled", False))
    fallback: Source | None = None
    if claude_on:
        key = str(ccfg.get("api_key") or "").strip() or os.environ.get(ENV_KEY, "").strip()
        if key:
            model = str(ccfg.get("model") or "").strip() or CLAUDE_DEFAULT_MODEL
            fallback = ClaudeSource(client, key, model)
    resolver = Resolver(
        sources,
        cache if cache is not None else Cache(cache_path),
        NormalizeConfig.from_config(config),
        accent_preference=[str(p) for p in prefs] if isinstance(prefs, list) else ("uk", "us"),
        word_by_word=bool(config.get("word_by_word", True)),
        weak_forms=bool(config.get("weak_forms", True)),
        fallback=fallback,
        fallback_for_partial=bool(ccfg.get("for_partial", True)),
        cancel=cancel,
        progress=progress,
    )
    if claude_on and fallback is None:
        resolver.fallback_requested = CLAUDE
        resolver.disabled[CLAUDE] = NO_KEY_REASON
    return resolver
