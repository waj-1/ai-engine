"""Tests for the cross-environment service client + email/password sign-in
seams in ``core.auth``.

Covers two previously-untested functions:
  * ``get_service_client_for_env`` — the dev -> (staging|prod) write path
    used by the promote flow, including the ``CrossEnvNotConfiguredError``
    guard and the ``*_SERVICE_ROLE_KEY`` alias.
  * ``sign_in_with_password`` — the GoTrue email/password exchange used by
    the admin portal, including its no-detail-leak error contract.

All network/DB is mocked at the ``core.auth.create_client`` seam — these
tests never reach a real Supabase project. Env is mutated with
monkeypatch.setenv/delenv so the process environment is restored per test.
"""

from unittest.mock import Mock

import pytest

from core import auth
from core.auth import (
    CrossEnvNotConfiguredError,
    SignInError,
    get_service_client_for_env,
    sign_in_with_password,
)


# ---------------------------------------------------------------------------
# get_service_client_for_env
# ---------------------------------------------------------------------------


class TestGetServiceClientForEnv:
    def test_raises_when_cross_env_unconfigured(self, monkeypatch):
        """ENVIRONMENT=dev, requesting staging with no STAGING_* vars set
        must raise CrossEnvNotConfiguredError naming the missing vars."""
        monkeypatch.setenv("ENVIRONMENT", "dev")
        monkeypatch.delenv("STAGING_AI_SUPABASE_URL", raising=False)
        monkeypatch.delenv("STAGING_AI_SUPABASE_SERVICE_KEY", raising=False)
        monkeypatch.delenv("STAGING_AI_SUPABASE_SERVICE_ROLE_KEY", raising=False)

        with pytest.raises(CrossEnvNotConfiguredError) as excinfo:
            get_service_client_for_env("staging")

        msg = str(excinfo.value)
        assert "STAGING_AI_SUPABASE_URL" in msg
        assert "STAGING_AI_SUPABASE_SERVICE_KEY" in msg

    def test_raises_when_url_set_but_key_missing(self, monkeypatch):
        """A partial config (URL only) still trips the guard — both halves
        are required."""
        monkeypatch.setenv("ENVIRONMENT", "dev")
        monkeypatch.setenv("STAGING_AI_SUPABASE_URL", "http://staging.invalid")
        monkeypatch.delenv("STAGING_AI_SUPABASE_SERVICE_KEY", raising=False)
        monkeypatch.delenv("STAGING_AI_SUPABASE_SERVICE_ROLE_KEY", raising=False)

        with pytest.raises(CrossEnvNotConfiguredError):
            get_service_client_for_env("staging")

    def test_same_env_falls_through_to_get_service_client(self, monkeypatch):
        """A same-env request (dev -> dev) must NOT raise and must delegate
        to get_service_client(). Patch it to a sentinel and assert identity."""
        monkeypatch.setenv("ENVIRONMENT", "dev")
        sentinel = object()
        called = {"n": 0}

        def _fake_get_service_client():
            called["n"] += 1
            return sentinel

        monkeypatch.setattr(auth, "get_service_client", _fake_get_service_client)

        # "development" must normalize to "dev" and match current env.
        result = get_service_client_for_env("development")

        assert result is sentinel
        assert called["n"] == 1

    def test_production_normalizes_to_prod_same_env_passthrough(self, monkeypatch):
        """ENVIRONMENT=production and target=prod both normalize to 'prod',
        so it's a same-env passthrough to get_service_client()."""
        monkeypatch.setenv("ENVIRONMENT", "production")
        sentinel = object()
        monkeypatch.setattr(auth, "get_service_client", lambda: sentinel)

        assert get_service_client_for_env("prod") is sentinel

    def test_service_role_key_alias_builds_client(self, monkeypatch):
        """When only the *_SERVICE_ROLE_KEY alias is set (not the plain
        *_SERVICE_KEY), the client must still be built using the alias key."""
        monkeypatch.setenv("ENVIRONMENT", "dev")
        monkeypatch.setenv("STAGING_AI_SUPABASE_URL", "http://staging.invalid")
        monkeypatch.delenv("STAGING_AI_SUPABASE_SERVICE_KEY", raising=False)
        monkeypatch.setenv("STAGING_AI_SUPABASE_SERVICE_ROLE_KEY", "alias-role-key-xyz")

        built = Mock(name="staging_client")
        captured = {}

        def _fake_create_client(url, key, options=None):
            captured["url"] = url
            captured["key"] = key
            captured["options"] = options
            return built

        monkeypatch.setattr(auth, "create_client", _fake_create_client)

        result = get_service_client_for_env("staging")

        assert result is built
        assert captured["url"] == "http://staging.invalid"
        # The alias key must be the one threaded into create_client.
        assert captured["key"] == "alias-role-key-xyz"


# ---------------------------------------------------------------------------
# sign_in_with_password
# ---------------------------------------------------------------------------


def _patch_create_client(monkeypatch, *, auth_obj):
    """Install a fake create_client whose returned client exposes
    ``client.auth`` == ``auth_obj``. Returns the captured-call dict."""
    fake_client = Mock()
    fake_client.auth = auth_obj
    monkeypatch.setattr(auth, "create_client", lambda *a, **k: fake_client)
    return fake_client


class TestSignInWithPassword:
    def test_missing_config_raises_signin_error(self, monkeypatch):
        """No SUPABASE_URL / SUPABASE_ANON_KEY -> SignInError before any
        network call."""
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)

        # create_client should never be reached; make it explode if it is.
        def _boom(*a, **k):  # pragma: no cover - asserts we never get here
            raise AssertionError("create_client should not be called")

        monkeypatch.setattr(auth, "create_client", _boom)

        with pytest.raises(SignInError) as excinfo:
            sign_in_with_password("a@b.co", "pw")
        assert "not configured" in str(excinfo.value)

    def test_gotrue_exception_does_not_leak_detail(self, monkeypatch):
        """A raw GoTrue/auth exception must be swallowed into a generic
        SignInError whose message does NOT contain the raw exception text."""
        monkeypatch.setenv("SUPABASE_URL", "http://test.supabase.co")
        monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")

        secret_detail = "INTERNAL_GOTRUE_STACK_SECRET_42"
        auth_obj = Mock()
        auth_obj.sign_in_with_password = Mock(side_effect=RuntimeError(secret_detail))
        _patch_create_client(monkeypatch, auth_obj=auth_obj)

        with pytest.raises(SignInError) as excinfo:
            sign_in_with_password("a@b.co", "wrong")

        message = str(excinfo.value)
        assert secret_detail not in message
        assert message == "Invalid email or password."
        # The original exception is still chained for server-side logs.
        assert isinstance(excinfo.value.__cause__, RuntimeError)

    def test_none_session_raises_signin_error(self, monkeypatch):
        """A result with session=None -> SignInError (no token to return)."""
        monkeypatch.setenv("SUPABASE_URL", "http://test.supabase.co")
        monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")

        result = Mock()
        result.session = None
        auth_obj = Mock()
        auth_obj.sign_in_with_password = Mock(return_value=result)
        _patch_create_client(monkeypatch, auth_obj=auth_obj)

        with pytest.raises(SignInError) as excinfo:
            sign_in_with_password("a@b.co", "pw")
        assert "no session" in str(excinfo.value)

    def test_none_access_token_raises_signin_error(self, monkeypatch):
        """A session present but access_token=None -> SignInError."""
        monkeypatch.setenv("SUPABASE_URL", "http://test.supabase.co")
        monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")

        session = Mock()
        session.access_token = None
        session.refresh_token = "ignored"
        result = Mock()
        result.session = session
        auth_obj = Mock()
        auth_obj.sign_in_with_password = Mock(return_value=result)
        _patch_create_client(monkeypatch, auth_obj=auth_obj)

        with pytest.raises(SignInError):
            sign_in_with_password("a@b.co", "pw")

    def test_success_returns_access_and_refresh(self, monkeypatch):
        """Happy path: returns dict with access_token + refresh_token."""
        monkeypatch.setenv("SUPABASE_URL", "http://test.supabase.co")
        monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")

        session = Mock()
        session.access_token = "jwt-access-token"
        session.refresh_token = "jwt-refresh-token"
        result = Mock()
        result.session = session
        auth_obj = Mock()
        auth_obj.sign_in_with_password = Mock(return_value=result)
        _patch_create_client(monkeypatch, auth_obj=auth_obj)

        out = sign_in_with_password("a@b.co", "pw")

        assert out == {
            "access_token": "jwt-access-token",
            "refresh_token": "jwt-refresh-token",
        }

    def test_success_refresh_defaults_to_empty_string(self, monkeypatch):
        """When the session has no refresh_token, the returned dict carries
        an empty-string default rather than None."""
        monkeypatch.setenv("SUPABASE_URL", "http://test.supabase.co")
        monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")

        session = Mock()
        session.access_token = "jwt-access-token"
        session.refresh_token = None
        result = Mock()
        result.session = session
        auth_obj = Mock()
        auth_obj.sign_in_with_password = Mock(return_value=result)
        _patch_create_client(monkeypatch, auth_obj=auth_obj)

        out = sign_in_with_password("a@b.co", "pw")

        assert out["access_token"] == "jwt-access-token"
        assert out["refresh_token"] == ""
