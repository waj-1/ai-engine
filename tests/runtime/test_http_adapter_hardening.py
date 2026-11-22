"""Defense-in-depth regression tests for the HTTP tool adapter.

Covers two hardenings layered on top of the SSRF guard:
  * the underlying ``httpx.Client`` is built with ``follow_redirects=False``
    so an allowed public endpoint can't 30x-redirect into an internal host
    that never went through ``_classify_endpoint``; and
  * a large 2xx body returned to the LLM is capped at ``_MAX_RESPONSE_CHARS``
    (plus a short truncation marker) rather than buffered/forwarded verbatim.

Both tests monkeypatch ``httpx.Client`` so no real network call happens.
``https://example.test/`` is an unresolvable host, which ``_classify_endpoint``
treats as 'public', so the request path (not the SSRF block) is exercised.
"""
from __future__ import annotations

import httpx
import pytest

from runtime.tools._http_adapter import (
    _MAX_RESPONSE_CHARS,
    _classify_endpoint,
    build_http_tool,
)


class _FakeResponse:
    def __init__(self, status_code: int = 200, json_body=None, text: str = ""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self):
        if self._json_body is None:
            raise ValueError("no json")
        return self._json_body


class _FakeClient:
    """Stand-in for httpx.Client that records constructor kwargs."""

    instances: list = []

    def __init__(self, *args, **kwargs):
        self.init_kwargs = kwargs
        self.last = {}
        _FakeClient.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def request(self, method, url, **kwargs):
        self.last = {"method": method, "url": url, **kwargs}
        return _FakeResponse(
            status_code=getattr(_FakeClient, "next_status", 200),
            json_body=getattr(_FakeClient, "next_json", None),
            text=getattr(_FakeClient, "next_text", ""),
        )


def _row(**overrides):
    row = {
        "python_name": "ping_endpoint",
        "description": "ping",
        "endpoint_url": "https://example.test/",
        "http_method": "POST",
        "argument_schema": {
            "type": "object",
            "properties": {"msg": {"type": "string"}},
        },
    }
    row.update(overrides)
    return row


@pytest.fixture(autouse=True)
def _reset_fake_client():
    _FakeClient.instances = []
    for attr in ("next_status", "next_json", "next_text"):
        if hasattr(_FakeClient, attr):
            delattr(_FakeClient, attr)
    yield


def test_endpoint_classified_public():
    # Sanity: the unresolvable host classifies as 'public', so build_http_tool's
    # _call takes the request path rather than the SSRF 'blocked' early return.
    assert _classify_endpoint("https://example.test/") == "public"


def test_client_built_with_follow_redirects_false(monkeypatch):
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    _FakeClient.next_json = {"ok": True}
    tool = build_http_tool(_row())

    tool.func(msg="hi")

    client = _FakeClient.instances[-1]
    assert client.init_kwargs.get("follow_redirects") is False


def test_success_body_capped_at_max_chars(monkeypatch):
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    # A huge text body (no JSON) on a 2xx response.
    big = "x" * (_MAX_RESPONSE_CHARS + 5_000)
    _FakeClient.next_status = 200
    _FakeClient.next_text = big
    tool = build_http_tool(_row())

    out = tool.func(msg="hi")

    # Returned string is capped to the limit plus the truncation marker.
    assert out.startswith("x" * _MAX_RESPONSE_CHARS)
    assert "...[truncated 5000 bytes]" in out
    assert len(out) < len(big)
    # The body itself was never returned verbatim past the cap.
    assert len(out) == _MAX_RESPONSE_CHARS + len("...[truncated 5000 bytes]")


def test_small_success_body_not_truncated(monkeypatch):
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    _FakeClient.next_json = {"ok": True}
    tool = build_http_tool(_row())

    out = tool.func(msg="hi")

    assert "truncated" not in out
    assert out == '{"ok": true}'
