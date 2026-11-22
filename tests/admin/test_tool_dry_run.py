"""Tests for the HTTP-tool dry-run endpoint.

The handler builds a `StructuredTool` via `runtime.tools._http_adapter.build_http_tool`,
validates the supplied kwargs against the generated Pydantic model, then
invokes the tool. We monkeypatch `httpx.Client` inside the adapter module
so tests don't touch the network. The endpoint always returns HTTP 200 —
"the dry-run plumbing completed" — and the body's `ok` flag distinguishes
endpoint-call success from validation / transport failures.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


def _make_client():
    """Boot the app with require_superuser bypassed. Mirrors the helper in
    test_tool_routes.py — kept inline so this test file is self-contained
    and can be run with `-x -q --no-cov` in isolation."""
    from fastapi.testclient import TestClient
    from admin import auth
    from main import app

    def _fake_superuser():
        return {"user_token": "fake-jwt", "user_id": "u-1", "email": "test@waj.local"}

    app.dependency_overrides[auth.require_superuser] = _fake_superuser
    tc = TestClient(app)

    def _teardown():
        app.dependency_overrides.pop(auth.require_superuser, None)

    return tc, _teardown


# A minimum-valid body. Individual tests override fields.
def _valid_body(**over) -> Dict[str, Any]:
    body = {
        "endpoint_url": "https://api.example.com/x",
        "http_method": "POST",
        "auth_env_var": None,
        "timeout_seconds": 30,
        "argument_schema": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
        "args": {"q": "hello"},
    }
    body.update(over)
    return body


def _mock_httpx_client(response_status: int = 200, response_json=None, response_text=None):
    """Return a contextmanager-shaped MagicMock that stands in for
    `httpx.Client(timeout=...)` inside `_http_adapter._call`. Each call
    returns a response with the given status + payload."""
    resp = MagicMock()
    resp.status_code = response_status
    if response_json is not None:
        resp.json.return_value = response_json
        resp.text = ""
    else:
        resp.json.side_effect = ValueError("not json")
        resp.text = response_text or ""

    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=cm)
    cm.__exit__ = MagicMock(return_value=False)
    cm.request = MagicMock(return_value=resp)

    factory = MagicMock(return_value=cm)
    return factory, cm


# ---------------------------------------------------------------------------
# Validation: missing required arg → ok=False, ValidationError
# ---------------------------------------------------------------------------


def test_dry_run_missing_required_arg_returns_validation_error():
    tc, teardown = _make_client()
    try:
        body = _valid_body(args={})  # `q` is required, missing
        r = tc.post("/admin/tools/dry-run", json=body)
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False
        assert data["error_class"] == "ValidationError"
        assert "q" in data["error"]
    finally:
        teardown()


# ---------------------------------------------------------------------------
# Bad schema → ValidationError, surfaces parse failure
# ---------------------------------------------------------------------------


def test_dry_run_bad_schema_returns_validation_error():
    tc, teardown = _make_client()
    try:
        # Schema root must be type=object; pass a scalar to trigger the
        # adapter's ValueError.
        body = _valid_body(argument_schema={"type": "string"})
        r = tc.post("/admin/tools/dry-run", json=body)
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False
        assert data["error_class"] == "ValidationError"
        assert "object" in data["error"].lower()
    finally:
        teardown()


# ---------------------------------------------------------------------------
# Happy POST: 200 JSON body comes back ok=True with parsed response.
# ---------------------------------------------------------------------------


def test_dry_run_happy_post_returns_ok():
    tc, teardown = _make_client()
    factory, _cm = _mock_httpx_client(
        response_status=200, response_json={"hits": 3, "items": ["a", "b"]},
    )
    try:
        with patch("runtime.tools._http_adapter.httpx.Client", factory):
            r = tc.post("/admin/tools/dry-run", json=_valid_body())
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True, data
        assert data["http_status"] == 200
        assert data["latency_ms"] >= 1
        assert data["response"]["content_type"] == "application/json"
        assert "hits" in data["response"]["body"]
    finally:
        teardown()


# ---------------------------------------------------------------------------
# Auth header redaction: env var supplied → echoed header is redacted with
# the env-var name, NEVER the raw token.
# ---------------------------------------------------------------------------


def test_dry_run_auth_header_attached_and_redacted():
    tc, teardown = _make_client()
    factory, _cm = _mock_httpx_client(response_status=200, response_json={"ok": True})
    os.environ["MY_TOOL_TOKEN"] = "super-secret-bearer-value"
    try:
        with patch("runtime.tools._http_adapter.httpx.Client", factory):
            body = _valid_body(auth_env_var="MY_TOOL_TOKEN")
            r = tc.post("/admin/tools/dry-run", json=body)
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True, data
        auth_header = data["request"]["headers"].get("Authorization", "")
        # The redacted marker must NOT contain the raw token.
        assert "super-secret-bearer-value" not in auth_header
        assert "redacted" in auth_header
        assert "MY_TOOL_TOKEN" in auth_header
    finally:
        os.environ.pop("MY_TOOL_TOKEN", None)
        teardown()


# ---------------------------------------------------------------------------
# Non-2xx endpoint: the dry-run "worked" — the endpoint just returned an
# error. ok=True so the operator can see what came back.
# ---------------------------------------------------------------------------


def test_dry_run_non_2xx_endpoint_is_visible():
    tc, teardown = _make_client()
    factory, _cm = _mock_httpx_client(
        response_status=404, response_json={"error": "no such record"},
    )
    try:
        with patch("runtime.tools._http_adapter.httpx.Client", factory):
            r = tc.post("/admin/tools/dry-run", json=_valid_body())
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True, data
        # The adapter encodes non-2xx as "HTTP 404 error: ..." — the
        # handler must recover the status code from that string.
        assert data["http_status"] == 404
        assert "no such record" in data["response"]["body"]
    finally:
        teardown()


# ---------------------------------------------------------------------------
# Transport exception: adapter returns "HTTP request failed: ..." → the
# handler surfaces TransportError with the message intact.
# ---------------------------------------------------------------------------


def test_dry_run_transport_error_returns_transport_error_class():
    import httpx

    tc, teardown = _make_client()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=cm)
    cm.__exit__ = MagicMock(return_value=False)
    cm.request = MagicMock(side_effect=httpx.ConnectError("nope"))
    factory = MagicMock(return_value=cm)
    try:
        with patch("runtime.tools._http_adapter.httpx.Client", factory):
            r = tc.post("/admin/tools/dry-run", json=_valid_body())
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False
        assert data["error_class"] == "TransportError"
        assert data["error"]  # non-empty
    finally:
        teardown()
