"""Shared test doubles (no network)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from anki_ipa.core.batch import Fields
from anki_ipa.core.http import HttpClient, Response

FIXTURES = Path(__file__).parent / "fixtures"

# The developer's deck (the config defaults are empty since Phase 7).
ENGLISH_WORDS = Fields(note_type="English words", word="English", ipa="IPA-english")

Handler = Callable[[str, str], Response]


class FakeTransport:
    """Records calls; answers via `handler(method, url)`."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> Response:
        self.calls.append((method, url, dict(headers)))
        return self.handler(method, url)


def client(
    handler: Handler,
    *,
    min_interval: float = 0.0,
    max_retries: int = 3,
    max_retry_after: float = 60.0,
) -> tuple[HttpClient, FakeTransport, list[float]]:
    """HttpClient over a fake transport with a fake clock/sleep (no waiting)."""
    t = FakeTransport(handler)
    sleeps: list[float] = []
    now = [0.0]

    def sleep(s: float) -> None:
        sleeps.append(s)
        now[0] += s

    c = HttpClient(
        t,
        sleep=sleep,
        clock=lambda: now[0],
        min_interval=min_interval,
        max_retries=max_retries,
        max_retry_after=max_retry_after,
    )
    return c, t, sleeps
