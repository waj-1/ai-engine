"""Tests for the standalone-AI-project client wiring in core.auth.

All network is mocked at the core.auth.create_client seam. Env is set with
patch.dict so the process environment is restored per test.
"""
from unittest.mock import Mock, patch

import pytest

from core.auth import get_main_client, get_service_client, get_service_client_for_env


@patch("core.auth.create_client")
@patch.dict("os.environ", {
    "AI_SUPABASE_URL": "http://ai.supabase.co",
    "AI_SUPABASE_SERVICE_KEY": "ai_service_key",
    "SUPABASE_URL": "http://main.supabase.co",
    "SUPABASE_SERVICE_KEY": "main_service_key",
}, clear=True)
def test_service_client_targets_ai_project(mock_create_client):
    mock_create_client.return_value = Mock()
    get_service_client()
    url, key = mock_create_client.call_args.args[0], mock_create_client.call_args.args[1]
    assert url == "http://ai.supabase.co"
    assert key == "ai_service_key"


@patch("core.auth.create_client")
@patch.dict("os.environ", {
    "AI_SUPABASE_URL": "http://ai.supabase.co",
    "AI_SUPABASE_SERVICE_KEY": "ai_service_key",
}, clear=True)
def test_service_client_uses_public_schema(mock_create_client):
    mock_create_client.return_value = Mock()
    get_service_client()
    opts = mock_create_client.call_args.kwargs["options"]
    # No wajagents schema override anymore → default public.
    assert getattr(opts, "schema", "public") in ("public", None)


def test_service_client_missing_ai_env_raises():
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ValueError, match="AI_SUPABASE_URL"):
            get_service_client()


@patch("core.auth.create_client")
@patch.dict("os.environ", {
    "SUPABASE_URL": "http://main.supabase.co",
    "SUPABASE_SERVICE_KEY": "main_service_key",
    "AI_SUPABASE_URL": "http://ai.supabase.co",
    "AI_SUPABASE_SERVICE_KEY": "ai_service_key",
}, clear=True)
def test_main_client_targets_main_project(mock_create_client):
    mock_create_client.return_value = Mock()
    get_main_client()
    url, key = mock_create_client.call_args.args[0], mock_create_client.call_args.args[1]
    assert url == "http://main.supabase.co"
    assert key == "main_service_key"


def test_main_client_missing_env_raises():
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ValueError, match="SUPABASE_URL"):
            get_main_client()


@patch("core.auth.create_client")
@patch.dict("os.environ", {"ENVIRONMENT": "dev"}, clear=True)
def test_cross_env_uses_ai_prefixed_vars(mock_create_client, monkeypatch):
    monkeypatch.setenv("STAGING_AI_SUPABASE_URL", "http://staging-ai.supabase.co")
    monkeypatch.setenv("STAGING_AI_SUPABASE_SERVICE_KEY", "staging_ai_key")
    mock_create_client.return_value = Mock()
    get_service_client_for_env("staging")
    url = mock_create_client.call_args.args[0]
    assert url == "http://staging-ai.supabase.co"


def test_admin_superuser_check_uses_main_client(monkeypatch):
    """require_superuser must look up public.user via the MAIN client, not
    the AI service client."""
    import admin.auth as admin_auth

    calls = {"main": 0, "service": 0}

    class _Resp:
        data = [{"id": "u1", "permission": {"is_superuser": True}, "email": "a@b.c"}]

    class _Q:
        def table(self, *_): return self
        def schema(self, *_): return self
        def select(self, *_): return self
        def eq(self, *_): return self
        def limit(self, *_): return self
        def execute(self): return _Resp()

    def _main(): calls["main"] += 1; return _Q()
    def _service(): calls["service"] += 1; return _Q()
    monkeypatch.setattr(admin_auth, "get_main_client", _main, raising=False)
    monkeypatch.setattr(admin_auth, "get_service_client", _service, raising=False)

    # require_superuser now goes through validate_token_verified, which
    # requires a signature over SUPABASE_JWT_SECRET plus exp/aud claims.
    import time
    import jwt

    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret")
    token = jwt.encode(
        {"sub": "auth-123", "aud": "authenticated", "exp": int(time.time()) + 3600},
        "test-secret",
        algorithm="HS256",
    )

    from fastapi import Request
    req = Request({"type": "http", "headers": []})
    ctx = admin_auth.require_superuser(req, authorization=f"Bearer {token}", waj_admin_token=None)
    assert ctx["user_id"] == "u1"
    assert calls["main"] == 1 and calls["service"] == 0
