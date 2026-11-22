"""Tests for the email/password admin sign-in flow.

The portal exchanges email+password for a Supabase session via GoTrue
(`core.auth.sign_in_with_password`) and stores the returned access_token
in the admin cookie. `require_superuser` still validates that token on
every protected endpoint, so these tests focus on the exchange + cookie
plumbing, not the superuser gate (covered elsewhere)."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


def _client():
    from fastapi.testclient import TestClient
    from main import app
    return TestClient(app)


# --- core.auth.sign_in_with_password --------------------------------------


def test_sign_in_returns_tokens_on_success(monkeypatch):
    from core import auth

    monkeypatch.setenv("SUPABASE_URL", "http://localhost:54321")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")

    class _Session:
        access_token = "jwt-abc"
        refresh_token = "refresh-xyz"

    class _Result:
        session = _Session()

    fake_client = type("C", (), {"auth": type("A", (), {
        "sign_in_with_password": staticmethod(lambda creds: _Result())
    })()})()

    with patch.object(auth, "create_client", return_value=fake_client):
        out = auth.sign_in_with_password("a@company.example", "pw")
    assert out == {"access_token": "jwt-abc", "refresh_token": "refresh-xyz"}


def test_sign_in_raises_on_bad_credentials(monkeypatch):
    from core import auth

    monkeypatch.setenv("SUPABASE_URL", "http://localhost:54321")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")

    def _boom(creds):
        raise Exception("Invalid login credentials")

    fake_client = type("C", (), {"auth": type("A", (), {
        "sign_in_with_password": staticmethod(_boom)
    })()})()

    with patch.object(auth, "create_client", return_value=fake_client):
        try:
            auth.sign_in_with_password("a@company.example", "wrong")
            assert False, "expected SignInError"
        except auth.SignInError as exc:
            assert "Invalid email or password" in str(exc)


def test_sign_in_raises_when_no_session(monkeypatch):
    from core import auth

    monkeypatch.setenv("SUPABASE_URL", "http://localhost:54321")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")

    class _Result:
        session = None

    fake_client = type("C", (), {"auth": type("A", (), {
        "sign_in_with_password": staticmethod(lambda creds: _Result())
    })()})()

    with patch.object(auth, "create_client", return_value=fake_client):
        try:
            auth.sign_in_with_password("a@company.example", "pw")
            assert False, "expected SignInError"
        except auth.SignInError as exc:
            assert "no session" in str(exc).lower()


def test_sign_in_raises_when_unconfigured(monkeypatch):
    from core import auth

    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    try:
        auth.sign_in_with_password("a@company.example", "pw")
        assert False, "expected SignInError"
    except auth.SignInError as exc:
        assert "not configured" in str(exc).lower()


# --- POST /admin/login -----------------------------------------------------


def test_login_email_password_sets_cookie_and_redirects():
    from admin import router

    with patch.object(router, "_set_admin_cookie") as set_cookie, \
         patch("core.auth.sign_in_with_password", return_value={"access_token": "jwt-1", "refresh_token": "r"}):
        r = _client().post(
            "/admin/login",
            data={"email": "admin@company.example", "password": "secret"},
            follow_redirects=False,
        )
    assert r.status_code == 302
    assert r.headers["location"] == "/admin/"
    set_cookie.assert_called_once()
    # the access_token from the session is what gets stored
    assert set_cookie.call_args.args[1] == "jwt-1"


def test_login_bad_credentials_rerenders_401():
    from core.auth import SignInError

    with patch("core.auth.sign_in_with_password", side_effect=SignInError("Invalid email or password.")):
        r = _client().post(
            "/admin/login",
            data={"email": "admin@company.example", "password": "wrong"},
            follow_redirects=False,
        )
    assert r.status_code == 401
    assert "Invalid email or password" in r.text
    # email is preserved so the operator doesn't retype it
    assert "admin@company.example" in r.text


def test_login_missing_fields_rerenders_400():
    r = _client().post("/admin/login", data={}, follow_redirects=False)
    assert r.status_code == 400
    assert "Enter your email and password" in r.text


def test_login_raw_token_fallback_still_works():
    from admin import router

    with patch.object(router, "_set_admin_cookie") as set_cookie:
        r = _client().post(
            "/admin/login",
            data={"token": "raw-jwt-paste"},
            follow_redirects=False,
        )
    assert r.status_code == 302
    set_cookie.assert_called_once()
    assert set_cookie.call_args.args[1] == "raw-jwt-paste"
