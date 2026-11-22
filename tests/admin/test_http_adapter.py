"""Unit tests for `runtime.tools._http_adapter.build_http_tool`.

We monkeypatch the `httpx.Client` class used inside the adapter so no
real network calls are made. The Client is a context manager — we fake
that with a small object exposing `__enter__` / `__exit__` and
`request(...)`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


# ---------------------------------------------------------------------------
# Fake httpx.Client helpers
# ---------------------------------------------------------------------------


class _FakeClient:
    """A context-manager stand-in for `httpx.Client`.

    `request_calls` is appended-to so individual tests can inspect what the
    adapter sent: method, url, params/json, headers.
    """

    def __init__(self, *, response=None, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc
        self.request_calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def request(self, method, url, **kwargs):
        self.request_calls.append({"method": method, "url": url, **kwargs})
        if self.raise_exc:
            raise self.raise_exc
        return self.response


def _make_response(status_code=200, json_body=None, text=None):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    if json_body is not None:
        resp.json = lambda: json_body
        resp.text = text if text is not None else json.dumps(json_body)
    else:
        resp.json = MagicMock(side_effect=ValueError("not json"))
        resp.text = text or ""
    return resp


def _install_fake_client(monkeypatch, fake):
    """Make `httpx.Client(...)` return our fake regardless of args."""
    from runtime.tools import _http_adapter

    def _factory(*args, **kwargs):
        return fake

    monkeypatch.setattr(_http_adapter.httpx, "Client", _factory)


# ---------------------------------------------------------------------------
# Happy-path POST
# ---------------------------------------------------------------------------


def test_build_http_tool_post_happy_path(monkeypatch):
    from runtime.tools._http_adapter import build_http_tool

    fake = _FakeClient(
        response=_make_response(200, json_body={"answer": "ok"})
    )
    _install_fake_client(monkeypatch, fake)

    tool = build_http_tool({
        "python_name": "web_search",
        "description": "search the web",
        "endpoint_url": "http://stub/tool",
        "http_method": "POST",
        "argument_schema": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
    })

    assert tool.name == "web_search"
    # `q` should be a required field on the args schema model
    args_model = tool.args_schema
    field = args_model.model_fields["q"]
    assert field.is_required()

    result = tool.invoke({"q": "hello"})
    assert isinstance(result, str)
    assert '"answer":"ok"' in result.replace(" ", "")
    # The POST should send the args as JSON body, not query params.
    assert fake.request_calls[0]["method"] == "POST"
    assert fake.request_calls[0]["json"] == {"q": "hello"}
    assert "params" not in fake.request_calls[0]


# ---------------------------------------------------------------------------
# GET method places args in query params
# ---------------------------------------------------------------------------


def test_build_http_tool_get_uses_params(monkeypatch):
    from runtime.tools._http_adapter import build_http_tool

    fake = _FakeClient(response=_make_response(200, json_body={"ok": True}))
    _install_fake_client(monkeypatch, fake)

    tool = build_http_tool({
        "python_name": "fetch_thing",
        "description": "GET it",
        "endpoint_url": "http://stub/q",
        "http_method": "GET",
        "argument_schema": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
    })
    tool.invoke({"q": "hi"})

    call = fake.request_calls[0]
    assert call["method"] == "GET"
    assert call["params"] == {"q": "hi"}
    assert "json" not in call


# ---------------------------------------------------------------------------
# Auth header attached when env var is set
# ---------------------------------------------------------------------------


def test_build_http_tool_auth_header_when_env_set(monkeypatch):
    from runtime.tools._http_adapter import build_http_tool

    monkeypatch.setenv("MY_TOKEN", "sekret-123")
    fake = _FakeClient(response=_make_response(200, json_body={"ok": True}))
    _install_fake_client(monkeypatch, fake)

    tool = build_http_tool({
        "python_name": "with_auth",
        "description": "auth tool",
        "endpoint_url": "http://stub/x",
        "http_method": "POST",
        "auth_env_var": "MY_TOKEN",
        "argument_schema": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
    })
    tool.invoke({"q": "x"})

    headers = fake.request_calls[0]["headers"]
    assert headers == {"Authorization": "Bearer sekret-123"}


def test_build_http_tool_no_auth_header_when_env_var_none(monkeypatch):
    from runtime.tools._http_adapter import build_http_tool

    fake = _FakeClient(response=_make_response(200, json_body={"ok": True}))
    _install_fake_client(monkeypatch, fake)

    tool = build_http_tool({
        "python_name": "no_auth",
        "description": "no auth",
        "endpoint_url": "http://stub/x",
        "http_method": "POST",
        "auth_env_var": None,
        "argument_schema": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
    })
    tool.invoke({"q": "x"})

    headers = fake.request_calls[0]["headers"]
    assert "Authorization" not in headers


def test_build_http_tool_no_auth_header_when_env_var_missing(monkeypatch):
    from runtime.tools._http_adapter import build_http_tool

    # Make sure the env var is genuinely unset
    monkeypatch.delenv("UNSET_FOR_TEST", raising=False)
    fake = _FakeClient(response=_make_response(200, json_body={"ok": True}))
    _install_fake_client(monkeypatch, fake)

    tool = build_http_tool({
        "python_name": "missing_env",
        "description": "missing env",
        "endpoint_url": "http://stub/x",
        "http_method": "POST",
        "auth_env_var": "UNSET_FOR_TEST",
        "argument_schema": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
    })
    tool.invoke({"q": "x"})

    headers = fake.request_calls[0]["headers"]
    assert "Authorization" not in headers


# ---------------------------------------------------------------------------
# Error responses don't raise
# ---------------------------------------------------------------------------


def test_build_http_tool_non_2xx_returns_error_string(monkeypatch):
    from runtime.tools._http_adapter import build_http_tool

    fake = _FakeClient(
        response=_make_response(422, json_body={"detail": "bad field"})
    )
    _install_fake_client(monkeypatch, fake)

    tool = build_http_tool({
        "python_name": "err_tool",
        "description": "err",
        "endpoint_url": "http://stub/x",
        "http_method": "POST",
        "argument_schema": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
    })
    result = tool.invoke({"q": "x"})
    assert isinstance(result, str)
    assert result.startswith("HTTP 422 error:")


def test_build_http_tool_transport_exception_returns_error_string(monkeypatch):
    from runtime.tools._http_adapter import build_http_tool

    fake = _FakeClient(raise_exc=httpx.ConnectError("dns down"))
    _install_fake_client(monkeypatch, fake)

    tool = build_http_tool({
        "python_name": "net_err",
        "description": "boom",
        "endpoint_url": "http://stub/x",
        "http_method": "POST",
        "argument_schema": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
    })
    result = tool.invoke({"q": "x"})
    assert isinstance(result, str)
    assert result.startswith("HTTP request failed:")
    assert "dns down" in result


# ---------------------------------------------------------------------------
# Bad schema raises at build-time
# ---------------------------------------------------------------------------


def test_build_http_tool_string_schema_raises():
    from runtime.tools._http_adapter import build_http_tool

    with pytest.raises(ValueError):
        build_http_tool({
            "python_name": "bad",
            "description": "bad",
            "endpoint_url": "http://stub/x",
            "argument_schema": "not a dict",
        })


def test_build_http_tool_non_object_schema_raises():
    from runtime.tools._http_adapter import build_http_tool

    with pytest.raises(ValueError):
        build_http_tool({
            "python_name": "bad2",
            "description": "bad",
            "endpoint_url": "http://stub/x",
            "argument_schema": {"type": "string"},
        })
