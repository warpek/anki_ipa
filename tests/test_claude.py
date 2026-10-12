"""Claude fallback (Phase 4) with a fake transport: no real API requests."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

import pytest
from fakes import client

from anki_ipa.core.batch import Fields, build_plan, summarize, summary_text
from anki_ipa.core.cache import Cache
from anki_ipa.core.http import HttpClient, RateLimited, Response
from anki_ipa.core.normalize import NormalizeConfig
from anki_ipa.core.resolver import NO_KEY_REASON, Resolver, Result, build_resolver
from anki_ipa.core.sources.base import Pronunciations, SourceDisabled
from anki_ipa.core.sources.claude import (
    ANTHROPIC_VERSION,
    API_URL,
    DEFAULT_MODEL,
    EXAMPLES,
    SYSTEM_PROMPT,
    ClaudeSource,
    validate_reply,
)

KEY = "sk-ant-test-key"


def reply(text: str, stop: str | None = "end_turn", **extra: Any) -> Response:
    body = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": DEFAULT_MODEL,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop,
        "usage": {"input_tokens": 500, "output_tokens": 12},
        **extra,
    }
    return Response(200, {"content-type": "application/json"}, json.dumps(body).encode())


class FakeApi:
    """Transport for api.anthropic.com: records request bodies, answers via `answer(text)`."""

    def __init__(self, answer: Callable[[str], Response]) -> None:
        self.answer = answer
        self.requests: list[tuple[str, str, dict[str, str], dict[str, Any]]] = []

    @property
    def texts(self) -> list[str]:
        return [body["messages"][0]["content"] for _, _, _, body in self.requests]

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> Response:
        assert body is not None
        payload = json.loads(body)
        self.requests.append((method, url, dict(headers), payload))
        text = payload["messages"][0]["content"].removeprefix("<text>").removesuffix("</text>")
        return self.answer(text)


def claude(
    answer: Callable[[str], Response] | Mapping[str, str], *, max_retries: int = 3
) -> tuple[ClaudeSource, FakeApi, list[float]]:
    if isinstance(answer, Mapping):
        table = answer
        answer = lambda t: reply(table[t]) if t in table else reply("")  # noqa: E731
    api = FakeApi(answer)
    sleeps: list[float] = []
    http = HttpClient(api, min_interval=0.0, sleep=sleeps.append, max_retries=max_retries)
    return ClaudeSource(http, KEY), api, sleeps


class FakeSource:
    def __init__(self, name: str, data: dict[str, Pronunciations]) -> None:
        self.name = name
        self.data = data
        self.calls: list[str] = []

    def lookup(self, text: str) -> Pronunciations | None:
        self.calls.append(text)
        return self.data.get(text)


FREE = FakeSource(
    "free",
    {
        "nick": Pronunciations(uk=["/nɪk/"]),
        "keel": Pronunciations(uk=["/kiːl/"]),
        "over": Pronunciations(uk=["/ˈəʊvə/"]),
    },
)


def resolver(fb: ClaudeSource | None, *, for_partial: bool = True, cache: Cache | None = None) -> Resolver:
    free = FakeSource(FREE.name, FREE.data)
    return Resolver(
        [free], cache if cache is not None else Cache(None), NormalizeConfig(), fallback=fb, fallback_for_partial=for_partial
    )


# --- request shape / parsing -------------------------------------------------------


def test_request_shape() -> None:
    src, api, _ = claude({"improvize": "ˈɪmprəvaɪz"})
    assert src.lookup("improvize") == Pronunciations(uk=["ˈɪmprəvaɪz"])
    [(method, url, headers, body)] = api.requests
    assert (method, url) == ("POST", API_URL)
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == KEY
    assert headers["anthropic-version"] == ANTHROPIC_VERSION == "2023-06-01"
    assert headers["content-type"] == "application/json"
    assert headers["User-Agent"].startswith("anki-ipa-filler/")
    assert body == {
        "model": "claude-haiku-5-5",
        "max_tokens": 200,
        "thinking": {"type": "disabled"},
        "output_config": {"effort": "low"},
        "system": [
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
        ],
        "messages": [{"role": "user", "content": "<text>improvize</text>"}],
    }
    # Haiku 5.5 rejects non-default sampling parameters (400): never sent.
    assert not {"temperature", "top_p", "top_k"} & body.keys()


def test_system_prompt_is_stable_and_has_the_examples() -> None:
    for text, ipa in EXAMPLES:
        assert f"{text} → {ipa}" in SYSTEM_PROMPT
    assert "ı" not in SYSTEM_PROMPT  # dotless i normalised to ɪ
    assert "Received Pronunciation" in SYSTEM_PROMPT
    src, api, _ = claude({"a": "ə", "b": "biː"})
    src.lookup("a")
    src.lookup("b")
    assert api.requests[0][3]["system"] == api.requests[1][3]["system"]


def test_custom_model() -> None:
    api = FakeApi(lambda t: reply("nɪk"))
    src = ClaudeSource(HttpClient(api, min_interval=0.0), KEY, "claude-sonnet-5-5")
    src.lookup("nick")
    assert api.requests[0][3]["model"] == "claude-sonnet-5-5"


def test_parses_text_blocks_after_thinking_and_counts_usage() -> None:
    def answer(t: str) -> Response:
        body = {
            "content": [
                {"type": "thinking", "thinking": "", "signature": "x"},
                {"type": "text", "text": "ˈəʊvə maɪ "},
                {"type": "text", "text": "ded ˈbɒdi"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 520, "output_tokens": 20, "cache_read_input_tokens": 400},
        }
        return Response(200, {}, json.dumps(body).encode())

    src, _, _ = claude(answer)
    assert src.transcribe("over my dead body") == "ˈəʊvə maɪ ded ˈbɒdi"
    assert (src.calls, src.input_tokens, src.output_tokens, src.cache_read_tokens) == (1, 520, 20, 400)
    assert src.usage_text() == "1 API call(s), 520 input (+400 cached) + 20 output tokens"


@pytest.mark.parametrize("stop", ["max_tokens", "refusal", None])
def test_bad_stop_reason_is_a_miss(stop: str | None) -> None:
    src, _, _ = claude(lambda t: reply("nɪk", stop=stop))
    assert src.lookup("nick") is None
    assert "stop_reason" in src.rejected[0]


# --- validation ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "text", "expected"),
    [
        ("nɪk", "nick", "nɪk"),
        ("/nɪk/", "nick", "nɪk"),
        ('"/nɪk/"', "nick", "nɪk"),
        ("`ˈskræpbʊk`", "scrapbook", "ˈskræpbʊk"),
        ("[ˈskræpbʊk]", "scrapbook", "ˈskræpbʊk"),
        ("  juːv ˈskeəsli tʃeɪndʒd.  ", "You've scarcely changed.", "juːv ˈskeəsli tʃeɪndʒd."),
        ("bed", "bed", "bed"),  # pure ASCII is fine for a single word
        ("ðə kæt", "the cat", "ðə kæt"),
        ("kwɪk", "quick", "kwɪk"),
        ("ˌnaɪnˈtiːn ˈeɪti ˈnaɪn", "1989", "ˌnaɪnˈtiːn ˈeɪti ˈnaɪn"),
    ],
)
def test_validate_accepts(raw: str, text: str, expected: str) -> None:
    assert validate_reply(raw, text) == expected


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        ("", "nick"),
        ("The IPA is nɪk", "nick"),  # explanation (upper case)
        ("nɪk\nThis is British RP.", "nick"),  # second line
        ("Sure! nɪk", "nick"),
        ("nɪk (British)", "nick"),
        ("nɪk 2", "nick"),
        ("kiːl ˈəʊvə, meaning to collapse", "keel over"),  # 'c'
        ("I'd transcribe it as kiːl ˈəʊvə", "keel over"),
        ("keel over", "keel over"),  # phrase echoed back in plain ASCII
        ("[word] ˈəʊvə", "keel over"),  # placeholder inside
        ("kiːl <b>ˈəʊvə</b>", "keel over"),
        ("yes", "yes"),  # 'y' is not English IPA (jes)
        ("ə " * 40, "nick"),  # far too long for the input
    ],
)
def test_validate_rejects(raw: str, text: str) -> None:
    assert validate_reply(raw, text) is None


def test_rejected_reply_is_a_definitive_miss_asked_once_per_run() -> None:
    src, api, _ = claude({"insurmountable": "Here is the transcription: ˌɪnsəˈmaʊntəbəl"})
    r = resolver(src)
    assert r.resolve("insurmountable") == Result(None, None, False, ["insurmountable"], definitive=True)
    assert r.resolve("insurmountable").ipa is None
    assert len(api.requests) == 1  # remembered for the run
    assert r.cache.get("claude", "insurmountable") is None  # but not cached
    assert "rejected reply" in src.rejected[0]
    # the next run (new resolver, same cache) sends it again
    again = resolver(src, cache=r.cache)
    assert again.resolve("insurmountable").definitive and len(api.requests) == 2


# --- errors ----------------------------------------------------------------------------


def test_401_disables_claude_for_the_run() -> None:
    err = json.dumps({"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
    src, api, _ = claude(lambda t: Response(401, {}, err.encode()))
    with pytest.raises(SourceDisabled, match="invalid API key"):
        src.lookup("x")
    r = resolver(src)
    assert r.resolve("insurmountable").ipa is None
    assert r.resolve("consecutively").ipa is None
    assert len(api.requests) == 2  # one direct + one via the resolver, then off
    assert r.disabled["claude"] == "invalid API key (HTTP 401)"
    assert r.cache.get("claude", "insurmountable") is None  # not cached
    assert r.fallback_status() == "disabled: invalid API key (HTTP 401) (0 API call(s), 0 input + 0 output tokens)"
    assert KEY not in r.disabled["claude"]


def test_404_unknown_model_disables() -> None:
    src, _, _ = claude(lambda t: Response(404, {}, b'{"type":"error"}'))
    r = resolver(src)
    r.resolve("insurmountable")
    assert r.disabled["claude"] == "model 'claude-haiku-5-5' not found (HTTP 404)"


@pytest.mark.parametrize("status", [429, 529])
def test_rate_limited_and_overloaded_back_off_then_disable(status: int) -> None:
    src, api, sleeps = claude(lambda t: Response(status, {"retry-after": "1"}), max_retries=2)
    with pytest.raises(RateLimited):
        src.lookup("x")
    assert len(api.requests) == 3 and sleeps == [1.0, 1.0]
    r = resolver(src)
    assert r.resolve("insurmountable").ipa is None
    assert r.disabled["claude"].startswith("rate limited")
    n = len(api.requests)
    r.resolve("consecutively")
    assert len(api.requests) == n


def test_overloaded_then_success() -> None:
    answers = iter([Response(529, {}), reply("ɪnsəˈmaʊntəbəl")])
    src, api, sleeps = claude(lambda t: next(answers))
    assert src.transcribe("insurmountable") == "ɪnsəˈmaʊntəbəl"
    assert len(api.requests) == 2 and sleeps == [2.0]


def test_server_error_is_transient_and_not_cached() -> None:
    answers = iter([Response(500, {}, b"oops"), reply("ˌɪnsəˈmaʊntəbəl")])
    src, _, _ = claude(lambda t: next(answers))
    r = resolver(src)
    assert r.resolve("insurmountable").ipa is None
    assert "claude" not in r.disabled
    assert r.resolve("insurmountable") == Result("ˌɪnsəˈmaʊntəbəl", "claude", True, [])


# --- resolver integration ------------------------------------------------------------


def test_not_called_when_free_chain_is_complete() -> None:
    src, api, _ = claude({})
    r = resolver(src)
    assert r.resolve("nick") == Result("nɪk", "free", True, [])
    assert r.resolve("keel over") == Result("kiːl ˈəʊvə", "free", True, [])
    assert api.requests == []


def test_called_for_missing_with_cache() -> None:
    src, api, _ = claude({"improvize": "ˈɪmprəvaɪz"})
    r = resolver(src)
    assert r.resolve("improvize") == Result("ˈɪmprəvaɪz", "claude", True, [])
    assert r.resolve("improvize").source == "claude"
    assert len(api.requests) == 1
    entry = r.cache.get("claude", "improvize")
    assert entry is not None and entry.value == {"uk": ["ˈɪmprəvaɪz"], "us": [], "other": []}
    assert r.fallback_status() == "on (1 API call(s), 500 input + 12 output tokens)"


def test_cache_survives_a_new_run(tmp_path: Any) -> None:
    path = tmp_path / "c.json"
    src, api, _ = claude({"improvize": "ˈɪmprəvaɪz"})
    r = resolver(src, cache=Cache(path))
    r.resolve_many(["improvize"])
    src2, api2, _ = claude({})
    assert resolver(src2, cache=Cache(path)).resolve("improvize").ipa == "ˈɪmprəvaɪz"
    assert api2.requests == []


def test_partial_sentence_replaced() -> None:
    src, api, _ = claude({"Nick keel qwzxv.": "nɪk kiːl ˈkwɪzɪv."})
    r = resolver(src)
    # free chain: "nɪk kiːl [qwzxv]." (partial) -> Claude gets the whole sentence
    res = r.resolve("Nick keel qwzxv.")
    assert api.texts == ["<text>Nick keel qwzxv.</text>"]
    assert res == Result("nɪk kiːl ˈkwɪzɪv.", "claude", True, [])


def test_partial_kept_when_for_partial_off() -> None:
    src, api, _ = claude({"Nick keel qwzxv.": "nɪk kiːl ˈkwɪzɪv."})
    r = resolver(src, for_partial=False)
    res = r.resolve("Nick keel qwzxv.")
    assert res == Result("nɪk kiːl [qwzxv].", "free", False, ["qwzxv"])
    assert api.requests == []
    assert r.resolve("improvize").ipa is None and len(api.requests) == 1  # misses still go


def test_partial_kept_when_claude_misses() -> None:
    src, _, _ = claude({"Nick keel qwzxv.": "I cannot transcribe qwzxv."})
    res = resolver(src).resolve("Nick keel qwzxv.")
    assert res == Result("nɪk kiːl [qwzxv].", "free", False, ["qwzxv"])


def test_never_called_without_fallback() -> None:
    r = resolver(None)
    assert r.resolve("improvize").ipa is None
    assert r.fallback_status() is None


def test_normalisation_and_punctuation() -> None:
    src, _, _ = claude(
        {
            "Don't interfere!": "/dəʊnt ˌɪntəˈfɪə(r)/",  # no "!", optional r, slashes
            "hello": "həˈlɛʊ",
            "yes": "ˈjɛs.",
            "Is it far?": "ɪz ɪt fɑː ?",
        }
    )
    r = resolver(src)
    assert r.resolve("Don't interfere!").ipa == "dəʊnt ˌɪntəˈfɪə!"
    assert r.resolve("hello").ipa == "həˈleʊ"  # british_vowels ɛ -> e
    assert r.resolve("yes").ipa == "jes"  # monosyllable stress + stray period dropped
    assert r.resolve("Is it far?").ipa == "ɪz ɪt fɑː?"


def test_alternatives_one_request_and_deduplicated() -> None:
    src, api, _ = claude({"adjustor, adjuster": "əˈdʒʌstə, əˈdʒʌstə"})
    res = resolver(src).resolve("adjustor, adjuster")
    assert api.texts == ["<text>adjustor, adjuster</text>"]
    assert res == Result("əˈdʒʌstə", "claude", True, [])


def test_claude_result_tagged_llm_in_plan_and_summary() -> None:
    src, _, _ = claude({"improvize": "ˈɪmprəvaɪz"})
    r = resolver(src)
    items = [(1, "improvize"), (2, "nick"), (3, "improvize&nbsp;")]
    results = {w: r.resolve(w) for _, w in items}
    fcfg = Fields()
    plan = build_plan(items, results, fcfg)
    assert plan[1].tags == ("ipa::auto", "ipa::llm")
    assert plan[2].tags == ("ipa::auto",)
    s = summarize(
        items, results, plan, {1, 2, 3}, words_total=3, cancelled=False,
        disabled=r.disabled, llm_status=r.fallback_status(),
    )
    assert s.llm_filled == [("improvize", "ˈɪmprəvaɪz")]
    text = summary_text(s, fcfg)
    assert "    Claude: 2" in text
    assert "Claude fallback: on (1 API call(s)" in text
    assert "Filled by Claude (1), please review (Browser search: tag:ipa::llm):\n    improvize  →  ˈɪmprəvaɪz" in text


def test_summary_shows_claude_disabled_once() -> None:
    s = summarize(
        [(1, "x")], {"x": Result(None, None, False, ["x"])}, {}, set(), words_total=1,
        cancelled=False, disabled={"claude": NO_KEY_REASON, "wiktionary": "rate limited"},
        llm_status=f"disabled: {NO_KEY_REASON}",
    )
    text = summary_text(s)
    assert f"Claude fallback: disabled: {NO_KEY_REASON}" in text
    assert text.count(NO_KEY_REASON) == 1
    assert "    Wiktionary: rate limited" in text
    # no fallback configured: no Claude line at all
    assert "Claude" not in summary_text(summarize([], {}, {}, set(), words_total=0, cancelled=False))


# --- build_resolver --------------------------------------------------------------------


def _http() -> HttpClient:
    http, _, _ = client(lambda m, u: pytest.fail(f"unexpected request {u}"))
    return http


def test_build_resolver_claude_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    r = build_resolver({"sources": [], "claude": {"enabled": False, "api_key": "k"}}, http=_http())
    assert r.fallback is None and r.fallback_status() is None
    r = build_resolver({"sources": ["claude"]}, http=_http())  # never a per-word source
    assert r.sources == [] and r.fallback is None


def test_build_resolver_config_key_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    cfg = {"claude": {"enabled": True, "api_key": " sk-cfg ", "model": "claude-x", "for_partial": False}}
    r = build_resolver(cfg, http=_http())
    assert isinstance(r.fallback, ClaudeSource)
    assert (r.fallback.api_key, r.fallback.model) == ("sk-cfg", "claude-x")
    assert r.fallback_for_partial is False
    assert r.fallback_status() == "on (0 API call(s), 0 input + 0 output tokens)"


def test_build_resolver_env_key_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    r = build_resolver({"claude": {"enabled": True, "api_key": ""}}, http=_http())
    assert isinstance(r.fallback, ClaudeSource)
    assert (r.fallback.api_key, r.fallback.model) == ("sk-env", DEFAULT_MODEL)
    assert r.fallback_for_partial is True


def test_build_resolver_no_key_skips_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = build_resolver({"sources": [], "claude": {"enabled": True, "api_key": ""}}, http=_http())
    assert r.fallback is None
    assert r.disabled == {"claude": NO_KEY_REASON}
    assert r.fallback_status() == f"disabled: {NO_KEY_REASON}"
    assert r.resolve("improvize").ipa is None  # no request (the fake http would fail)
