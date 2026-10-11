"""Tiny HTTP client on `urllib.request` (stdlib only).

- Descriptive User-Agent, per-request timeout.
- 404 -> None (the "not found" convention every source relies on).
- 429/503/529 -> back off (honouring `Retry-After`, capped), retry; after
  `max_retries` raise `RateLimited` so the resolver can skip that source.
- Per-host minimum interval between requests (polite, one at a time).
- The network call itself is an injectable `Transport`, so tests never hit
  the network.
"""

from __future__ import annotations

import email.utils
import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

USER_AGENT = "anki-ipa-filler/0.1 (Anki add-on; contact: https://github.com/)"
DEFAULT_TIMEOUT = 15.0
DEFAULT_MIN_INTERVAL = 1.0  # seconds between two requests to the same host
RETRY_STATUSES = frozenset({429, 503, 529})  # 529 = Anthropic "overloaded"


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)  # lower-cased keys
    body: bytes = b""

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


# (method, url, headers, body, timeout) -> Response. Must NOT raise for HTTP
# error statuses (return them); may raise `OSError` for network failures.
Transport = Callable[[str, str, Mapping[str, str], bytes | None, float], Response]


class HttpError(Exception):
    """Non-retryable HTTP failure, or a network error (status 0)."""

    def __init__(self, url: str, status: int, message: str = "") -> None:
        super().__init__(f"HTTP {status} for {url}" + (f": {message}" if message else ""))
        self.url = url
        self.status = status


class RateLimited(HttpError):
    """The host kept answering 429/503/529 after all retries."""


def urllib_transport(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
) -> Response:
    req = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return Response(resp.status, hdrs, resp.read())
    except urllib.error.HTTPError as e:
        hdrs = {k.lower(): v for k, v in (e.headers or {}).items()}
        try:
            data = e.read()
        except OSError:
            data = b""
        return Response(e.code, hdrs, data)


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """`Retry-After` as seconds: delta-seconds or an HTTP date. None if absent/bad."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, dt.timestamp() - (time.time() if now is None else now))


class HttpClient:
    def __init__(
        self,
        transport: Transport | None = None,
        *,
        user_agent: str = USER_AGENT,
        timeout: float = DEFAULT_TIMEOUT,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        host_intervals: Mapping[str, float] | None = None,
        max_retries: int = 3,
        backoff: float = 2.0,
        max_retry_after: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.transport: Transport = transport or urllib_transport
        self.user_agent = user_agent
        self.timeout = timeout
        self.min_interval = min_interval
        self.host_intervals = dict(host_intervals or {})
        self.max_retries = max_retries
        self.backoff = backoff
        self.max_retry_after = max_retry_after
        self._sleep = sleep
        self._clock = clock
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self.request_count = 0

    # -- throttle ---------------------------------------------------------
    def _throttle(self, url: str) -> None:
        host = urllib.parse.urlsplit(url).netloc.lower()
        interval = self.host_intervals.get(host, self.min_interval)
        with self._lock:
            last = self._last.get(host)
            now = self._clock()
            wait = 0.0 if last is None else last + interval - now
            # Reserve the slot before sleeping so concurrent callers queue up.
            self._last[host] = now + max(0.0, wait)
        if wait > 0:
            self._sleep(wait)

    # -- core -------------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> Response | None:
        """Response for 2xx, None for 404; raises `RateLimited` / `HttpError`."""
        hdrs = {"User-Agent": self.user_agent, **(headers or {})}
        attempt = 0
        while True:
            self._throttle(url)
            self.request_count += 1
            try:
                resp = self.transport(method, url, hdrs, body, self.timeout)
            except (OSError, http.client.HTTPException) as e:
                # URLError, timeouts, connection resets, IncompleteRead
                raise HttpError(url, 0, f"{type(e).__name__}: {e}") from e
            if resp.status == 404:
                return None
            if 200 <= resp.status < 300:
                return resp
            if resp.status in RETRY_STATUSES:
                if attempt >= self.max_retries:
                    raise RateLimited(url, resp.status, "giving up after retries")
                delay = parse_retry_after(resp.headers.get("retry-after"))
                if delay is None:
                    delay = self.backoff * (2**attempt)
                if delay > self.max_retry_after:
                    # The server wants us gone for longer than we are willing
                    # to wait: let the caller fall through to another source.
                    raise RateLimited(url, resp.status, f"Retry-After {delay:.0f}s")
                self._sleep(delay)
                attempt += 1
                continue
            raise HttpError(url, resp.status, resp.text()[:200])

    def get_text(self, url: str, headers: Mapping[str, str] | None = None) -> str | None:
        resp = self.request("GET", url, headers=headers)
        return None if resp is None else resp.text()

    def get_json(self, url: str, headers: Mapping[str, str] | None = None) -> Any:
        """Decoded JSON, or None for 404."""
        resp = self.request("GET", url, headers={"Accept": "application/json", **(headers or {})})
        return None if resp is None else _decode_json(url, resp)

    def post_json(
        self, url: str, payload: Any, headers: Mapping[str, str] | None = None
    ) -> Any:
        body = json.dumps(payload).encode("utf-8")
        hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
        resp = self.request("POST", url, headers={**hdrs, **(headers or {})}, body=body)
        return None if resp is None else _decode_json(url, resp)


def _decode_json(url: str, resp: Response) -> Any:
    try:
        return json.loads(resp.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise HttpError(url, resp.status, f"invalid JSON: {e}") from e


_default: HttpClient | None = None


def default_client() -> HttpClient:
    global _default
    if _default is None:
        _default = HttpClient()
    return _default


def get_text(url: str, headers: Mapping[str, str] | None = None) -> str | None:
    return default_client().get_text(url, headers)


def get_json(url: str, headers: Mapping[str, str] | None = None) -> Any:
    return default_client().get_json(url, headers)


def post_json(url: str, payload: Any, headers: Mapping[str, str] | None = None) -> Any:
    return default_client().post_json(url, payload, headers)
