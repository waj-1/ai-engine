"""Unit tests for core.observability (the Laminar SDK bootstrap).

The `dashboard_trace_url` / `dashboard_dataset_url` deep-link helpers this file
also used to cover are **gone**: they resolved a project id through the Laminar
web UI's NextAuth API (404 on the deployed build), no template ever rendered
them, and the admin portal renders traces itself from ClickHouse. The guard
against them creeping back is `test_dashboard_url_helpers_are_gone` below.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

from core import observability


@pytest.fixture(autouse=True)
def _reset_initialized(monkeypatch):
    """Each test starts from the not-yet-initialized state."""
    monkeypatch.setattr(observability, "_initialized", False)
    monkeypatch.delenv("LMNR_DISABLED", raising=False)
    monkeypatch.delenv("LMNR_PROJECT_API_KEY", raising=False)
    monkeypatch.delenv("LMNR_BASE_URL", raising=False)
    monkeypatch.delenv("LMNR_DASHBOARD_URL", raising=False)
    yield


def test_truthy_variants():
    for v in ("1", "true", "TRUE", " yes ", "on"):
        assert observability._truthy(v) is True
    for v in ("0", "false", "", None, "nope"):
        assert observability._truthy(v) is False


def test_initialize_skipped_when_disabled(monkeypatch):
    monkeypatch.setenv("LMNR_DISABLED", "true")
    monkeypatch.setenv("LMNR_PROJECT_API_KEY", "should-be-ignored")
    assert observability.initialize_observability() is False
    assert observability._initialized is False


def test_initialize_skipped_without_api_key():
    assert observability.initialize_observability() is False
    assert observability._initialized is False


def test_initialize_is_idempotent(monkeypatch):
    monkeypatch.setattr(observability, "_initialized", True)
    # Even with no key, an already-initialized bootstrap short-circuits to True.
    assert observability.initialize_observability() is True


def test_initialize_success_passes_config(monkeypatch):
    captured = {}

    def fake_init(**kwargs):
        captured.update(kwargs)

    fake_lmnr = types.SimpleNamespace(
        Laminar=types.SimpleNamespace(initialize=fake_init, shutdown=lambda: None)
    )
    monkeypatch.setitem(sys.modules, "lmnr", fake_lmnr)
    monkeypatch.setenv("LMNR_PROJECT_API_KEY", "key-123")
    monkeypatch.setenv("LMNR_BASE_URL", "http://localhost")
    monkeypatch.setenv("LMNR_HTTP_PORT", "8001")
    monkeypatch.setenv("LMNR_GRPC_PORT", "8002")

    assert observability.initialize_observability() is True
    assert observability._initialized is True
    assert captured["project_api_key"] == "key-123"
    assert captured["base_url"] == "http://localhost"
    assert captured["http_port"] == 8001
    assert captured["grpc_port"] == 8002


def test_initialize_swallows_sdk_failure(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("sdk down")

    fake_lmnr = types.SimpleNamespace(Laminar=types.SimpleNamespace(initialize=boom))
    monkeypatch.setitem(sys.modules, "lmnr", fake_lmnr)
    monkeypatch.setenv("LMNR_PROJECT_API_KEY", "key-123")
    assert observability.initialize_observability() is False
    assert observability._initialized is False


def test_dashboard_url_helpers_are_gone():
    """The web-UI deep-link helpers must not come back.

    They can only be implemented by asking the Laminar frontend for a project
    id, and that frontend serves no auth routes in the deployed build — so a
    reintroduced helper would be dead code that looks alive. Traces are rendered
    by `/admin/observability` from ClickHouse instead.
    """
    assert not hasattr(observability, "dashboard_trace_url")
    assert not hasattr(observability, "dashboard_dataset_url")


def test_laminar_frontend_client_module_is_gone():
    """`core/lmnr_client.py` signed into the Laminar frontend with NextAuth
    (`GET /api/auth/csrf` → `POST /api/auth/callback/email`). The deployed build
    404s those routes, so every screen reading through it was broken in
    production. Importing it must fail — an accidental restore is a regression."""
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("core.lmnr_client")


def test_no_jinja_deep_link_globals(monkeypatch):
    """The `lmnr_trace_url` / `lmnr_dataset_url` / `lmnr_dashboard_url` Jinja
    globals went with the helpers (zero templates referenced them)."""
    from admin.router import templates

    for name in ("lmnr_trace_url", "lmnr_dataset_url", "lmnr_dashboard_url"):
        assert name not in templates.env.globals
