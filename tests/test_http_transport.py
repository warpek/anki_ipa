"""Transports (requests / urllib), default selection, UA helpers; offline."""

from __future__ import annotations

import json
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from anki_ipa.core import http
from anki_ipa.core.http import (
    CERT_HINT,
    USER_AGENT,
    CertificateVerifyFailed,
    HttpClient,
    HttpError,
    addon_version,
    default_transport,
    make_user_agent,
    requests_transport,
    urllib_transport,
)

requests = pytest.importorskip("requests")
from requests.adapters import HTTPAdapter  # noqa: E402
from requests.structures import CaseInsensitiveDict  # noqa: E402

URL = "https://x.test/a"


def _patch_send(monkeypatch: pytest.MonkeyPatch, fn: Any) -> list[Any]:
    """Replace the network layer of real `requests` (no sockets opened)."""
    seen: list[Any] = []

    def send(self: HTTPAdapter, request: Any, **kwargs: Any) -> Any:
        seen.append((request, kwargs))
        return fn(request)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    return seen


def _resp(request: Any, status: int, body: bytes, headers: dict[str, str]) -> Any:
    r = requests.Response()
    r.status_code = status
    r._content = body
    r.headers = CaseInsensitiveDict(headers)
    r.url = request.url
    r.request = request
    return r


# -- requests_transport -------------------------------------------------------------
@pytest.mark.parametrize("status", [200, 404, 429, 500])
def test_requests_status_passthrough_and_lowercase_headers(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    seen = _patch_send(
        monkeypatch, lambda req: _resp(req, status, b"body", {"Retry-After": "3", "X-Mixed": "v"})
    )
    r = requests_transport("GET", URL, {"User-Agent": "ua/1"}, None, 7.0)
    assert r.status == status and r.body == b"body"
    assert dict(r.headers) == {"retry-after": "3", "x-mixed": "v"}
    req, kwargs = seen[0]
    assert req.headers["User-Agent"] == "ua/1"
    assert kwargs["timeout"] == 7.0 and kwargs["verify"] is True


def test_requests_post_body(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _patch_send(monkeypatch, lambda req: _resp(req, 200, b"{}", {}))
    requests_transport("POST", URL, {"Content-Type": "application/json"}, b'{"a":1}', 5.0)
    assert seen[0][0].method == "POST" and seen[0][0].body == b'{"a":1}'


def test_requests_exception_maps_to_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(req: Any) -> Any:
        raise requests.ConnectionError("no route")

    _patch_send(monkeypatch, boom)
    with pytest.raises(OSError) as ei:
        requests_transport("GET", URL, {}, None, 1.0)
    assert not isinstance(ei.value, CertificateVerifyFailed)
    assert "ConnectionError" in str(ei.value)
    # ... and HttpClient reports it as a network error (status 0)
    c = HttpClient(requests_transport, min_interval=0, sleep=lambda s: None)
    with pytest.raises(HttpError) as he:
        c.get_text(URL)
    assert he.value.status == 0


def test_requests_timeout_maps_to_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(req: Any) -> Any:
        raise requests.Timeout("slow")

    _patch_send(monkeypatch, boom)
    with pytest.raises(OSError):
        requests_transport("GET", URL, {}, None, 1.0)


def test_requests_cert_failure_is_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(req: Any) -> Any:
        inner = ssl.SSLCertVerificationError(1, "unable to get local issuer certificate")
        raise requests.exceptions.SSLError(inner)

    _patch_send(monkeypatch, boom)
    with pytest.raises(CertificateVerifyFailed):
        requests_transport("GET", URL, {}, None, 1.0)
    c = HttpClient(requests_transport, min_interval=0, sleep=lambda s: None)
    with pytest.raises(HttpError) as he:
        c.get_text(URL)
    assert he.value.status == 0 and CERT_HINT in str(he.value)


def test_requests_other_ssl_error_is_plain_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(req: Any) -> Any:
        raise requests.exceptions.SSLError("handshake failure")

    _patch_send(monkeypatch, boom)
    with pytest.raises(OSError) as ei:
        requests_transport("GET", URL, {}, None, 1.0)
    assert not isinstance(ei.value, CertificateVerifyFailed)


# -- urllib_transport ----------------------------------------------------------------
def test_urllib_cert_failure_is_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    def urlopen(req: Any, timeout: float) -> Any:
        raise urllib.error.URLError(ssl.SSLCertVerificationError(1, "self-signed certificate"))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(CertificateVerifyFailed) as ei:
        urllib_transport("GET", URL, {}, None, 1.0)
    assert CERT_HINT in str(ei.value) and "self-signed" in str(ei.value)


def test_urllib_other_url_error_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    def urlopen(req: Any, timeout: float) -> Any:
        raise urllib.error.URLError("Name or service not known")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(urllib.error.URLError):
        urllib_transport("GET", URL, {}, None, 1.0)


# -- default selection ------------------------------------------------------------
def test_default_prefers_requests() -> None:
    assert default_transport() is requests_transport
    assert HttpClient().transport is requests_transport


def test_default_falls_back_to_urllib(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "requests", None)  # import -> ImportError
    assert default_transport() is urllib_transport
    assert HttpClient().transport is urllib_transport


def test_explicit_transport_wins() -> None:
    assert HttpClient(urllib_transport).transport is urllib_transport


# -- User-Agent ---------------------------------------------------------------------
def test_user_agent_format() -> None:
    assert make_user_agent("1.2.3") == (
        "anki-ipa-filler/1.2.3 (Anki add-on; +https://github.com/warpek/anki_ipa)"
    )
    assert USER_AGENT == make_user_agent("dev")


def test_addon_version_from_manifest(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(json.dumps({"human_version": "0.2.0"}))
    (tmp_path / "meta.json").write_text(json.dumps({"human_version": "9.9"}))
    assert addon_version(str(tmp_path)) == "0.2.0"


def test_addon_version_falls_back_to_meta_then_dev(tmp_path: Path) -> None:
    assert addon_version(str(tmp_path)) == "dev"  # nothing there
    (tmp_path / "manifest.json").write_text("{broken")
    assert addon_version(str(tmp_path)) == "dev"
    (tmp_path / "meta.json").write_text(json.dumps({"human_version": "0.3"}))
    assert addon_version(str(tmp_path)) == "0.3"
    (tmp_path / "meta.json").write_text(json.dumps(["not", "a", "dict"]))
    assert addon_version(str(tmp_path)) == "dev"


def test_real_manifest_version() -> None:
    pkg = Path(http.__file__).resolve().parent.parent
    assert addon_version(str(pkg)) == json.loads((pkg / "manifest.json").read_text())[
        "human_version"
    ]


def test_build_resolver_passes_user_agent() -> None:
    from anki_ipa.core.resolver import build_resolver
    from anki_ipa.core.sources.freedictionary import FreeDictionarySource

    r = build_resolver({"sources": ["freedictionary"]}, user_agent="ua-test/1")
    src = r.sources[0]
    assert isinstance(src, FreeDictionarySource)
    assert src.http.user_agent == "ua-test/1"
