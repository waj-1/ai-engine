"""SSRF regression tests for the HTTP-tool dry-run endpoint.

The dry-run handler used to only *log* a warning when the operator-supplied
endpoint resolved to a loopback / link-local / cloud-metadata address and
then PROCEED to fetch it (advisory-only). The runtime adapter
(``runtime.tools._http_adapter._call``) already BLOCKS those hosts via
``_classify_endpoint``. These tests pin the dry-run to the same behaviour:
a blocked host must be refused *before* any HTTP request is issued, while a
public host must pass the guard (it may still fail later for other reasons).

We patch ``runtime.tools._http_adapter.httpx.Client`` with a factory that
raises if it's ever instantiated, so any real outbound call is detectable.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


def _make_client():
    """Boot the app with require_superuser bypassed. Mirrors the helper in
    test_tool_dry_run.py — kept inline so this file is self-contained."""
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


def _exploding_httpx_factory():
    """An httpx.Client replacement that fails the test if it's ever called —
    i.e. if a blocked dry-run actually reached the network."""
    def _boom(*_a, **_k):
        raise AssertionError("httpx.Client was instantiated — a blocked dry-run "
                             "issued a real HTTP request")
    return MagicMock(side_effect=_boom)


def _stub_httpx_factory():
    """An httpx.Client replacement that returns a benign 200 JSON response,
    used to prove a public endpoint gets *past* the SSRF guard."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"ok": True}
    resp.text = ""
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=cm)
    cm.__exit__ = MagicMock(return_value=False)
    cm.request = MagicMock(return_value=resp)
    return MagicMock(return_value=cm)


# ---------------------------------------------------------------------------
# Cloud-metadata address (169.254.169.254) → blocked, no HTTP issued.
# ---------------------------------------------------------------------------


def test_dry_run_blocks_cloud_metadata_without_fetching():
    tc, teardown = _make_client()
    factory = _exploding_httpx_factory()
    try:
        with patch("runtime.tools._http_adapter.httpx.Client", factory):
            body = _valid_body(endpoint_url="http://169.254.169.254/latest/meta-data/")
            r = tc.post("/admin/tools/dry-run", json=body)
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False, data
        assert data["error_class"] == "ConfigError", data
        assert "block" in data["error"].lower()
        # No outbound request must have been made.
        factory.assert_not_called()
    finally:
        teardown()


# ---------------------------------------------------------------------------
# Loopback address (127.0.0.1) → blocked, no HTTP issued.
# ---------------------------------------------------------------------------


def test_dry_run_blocks_loopback_without_fetching():
    tc, teardown = _make_client()
    factory = _exploding_httpx_factory()
    try:
        with patch("runtime.tools._http_adapter.httpx.Client", factory):
            body = _valid_body(endpoint_url="http://127.0.0.1:8123/")
            r = tc.post("/admin/tools/dry-run", json=body)
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False, data
        assert data["error_class"] == "ConfigError", data
        assert "block" in data["error"].lower()
        factory.assert_not_called()
    finally:
        teardown()


# ---------------------------------------------------------------------------
# Public endpoint → NOT blocked by this guard; reaches the (stubbed) client.
# ---------------------------------------------------------------------------


def test_dry_run_public_endpoint_not_blocked_by_ssrf_guard():
    tc, teardown = _make_client()
    factory = _stub_httpx_factory()
    try:
        with patch("runtime.tools._http_adapter.httpx.Client", factory):
            r = tc.post("/admin/tools/dry-run", json=_valid_body())
        assert r.status_code == 200
        data = r.json()
        # The guard must not have fired — either it succeeded, or it failed
        # for some non-SSRF reason. Crucially it is NOT a ConfigError block.
        assert data.get("error_class") != "ConfigError", data
        # And the request actually made it to the (stubbed) HTTP client.
        factory.assert_called()
    finally:
        teardown()


# ---------------------------------------------------------------------------
# Malformed port text (e.g. an operator fat-fingering the dry-run URL) →
# _classify_endpoint's urlparse(...).port can raise ValueError. This handler
# is documented to "never raise for input-shaped problems" (always returns
# {"ok": bool, ...}), and _classify_endpoint is called here with NO
# surrounding try/except — a regression here is an unhandled 500, not a
# clean ok=False.
# ---------------------------------------------------------------------------


def test_dry_run_malformed_port_returns_error_without_raising():
    tc, teardown = _make_client()
    factory = _exploding_httpx_factory()
    try:
        with patch("runtime.tools._http_adapter.httpx.Client", factory):
            body = _valid_body(endpoint_url="http://127.0.0.1:8080.evil.com/")
            r = tc.post("/admin/tools/dry-run", json=body)
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False, data
        assert data["error_class"] == "ConfigError", data
        assert "block" in data["error"].lower()
        # No outbound request must have been made.
        factory.assert_not_called()
    finally:
        teardown()
