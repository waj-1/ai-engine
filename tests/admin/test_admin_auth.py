"""Unit tests for admin.auth.require_superuser — the gate on every /admin route.

Called as a plain function (FastAPI deps are just callables) with a stub
Request, so we exercise the real token-extraction + superuser-flag logic
without booting the app or hitting Supabase.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from fastapi import HTTPException

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

from admin import auth as admin_auth
from core.auth import TokenValidationError


@pytest.fixture(autouse=True)
def _clear_superuser_cache():
    """resolve_superuser now memoizes the DB superuser lookup by `sub` for a
    TTL. Tests here reuse sub="auth-1" with different DB outcomes, so clear
    the cache around each test to keep them independent."""
    admin_auth.reset_superuser_cache()
    yield
    admin_auth.reset_superuser_cache()


def _request():
    return types.SimpleNamespace(state=types.SimpleNamespace())


class _FakeUserClient:
    """Mimics client.schema('public').table('user').select().eq().limit().execute().data"""

    def __init__(self, rows):
        self._rows = rows

    def schema(self, _name):
        return self

    def table(self, _name):
        return self

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        return types.SimpleNamespace(data=self._rows)


# --- _extract_token --------------------------------------------------------


def test_extract_token_prefers_authorization_header(monkeypatch):
    monkeypatch.setattr(admin_auth, "require_bearer", lambda h: "from-header")
    assert admin_auth._extract_token("Bearer xyz", "cookie-tok") == "from-header"


def test_extract_token_falls_back_to_cookie():
    assert admin_auth._extract_token(None, "cookie-tok") == "cookie-tok"


def test_extract_token_401_when_missing_both():
    with pytest.raises(HTTPException) as exc:
        admin_auth._extract_token(None, None)
    assert exc.value.status_code == 401


# --- require_superuser ------------------------------------------------------


def test_require_superuser_happy_path(monkeypatch):
    monkeypatch.setattr(admin_auth, "validate_token_verified", lambda t: {"sub": "auth-1"})
    monkeypatch.setattr(
        admin_auth,
        "get_main_client",
        lambda: _FakeUserClient(
            [{"id": "user-1", "email": "boss@company.example", "permission": {"is_superuser": True}}]
        ),
    )
    req = _request()
    ctx = admin_auth.require_superuser(req, authorization=None, waj_admin_token="tok")
    assert ctx["user_id"] == "user-1"
    assert ctx["email"] == "boss@company.example"
    assert ctx["user_token"] == "tok"
    # State is stashed for the audit logger.
    assert req.state.admin_user_id == "user-1"
    assert req.state.admin_email == "boss@company.example"


def test_require_superuser_invalid_token_401(monkeypatch):
    def boom(_t):
        raise TokenValidationError("bad token")

    monkeypatch.setattr(admin_auth, "validate_token_verified", boom)
    with pytest.raises(HTTPException) as exc:
        admin_auth.require_superuser(_request(), authorization=None, waj_admin_token="tok")
    assert exc.value.status_code == 401


def test_require_superuser_missing_sub_401(monkeypatch):
    monkeypatch.setattr(admin_auth, "validate_token_verified", lambda t: {"no_sub": True})
    with pytest.raises(HTTPException) as exc:
        admin_auth.require_superuser(_request(), authorization=None, waj_admin_token="tok")
    assert exc.value.status_code == 401


def test_require_superuser_no_user_row_403(monkeypatch):
    monkeypatch.setattr(admin_auth, "validate_token_verified", lambda t: {"sub": "auth-1"})
    monkeypatch.setattr(admin_auth, "get_main_client", lambda: _FakeUserClient([]))
    with pytest.raises(HTTPException) as exc:
        admin_auth.require_superuser(_request(), authorization=None, waj_admin_token="tok")
    assert exc.value.status_code == 403


def test_require_superuser_non_superuser_403(monkeypatch):
    monkeypatch.setattr(admin_auth, "validate_token_verified", lambda t: {"sub": "auth-1"})
    monkeypatch.setattr(
        admin_auth,
        "get_main_client",
        lambda: _FakeUserClient(
            [{"id": "user-2", "email": "user@company.example", "permission": {"is_superuser": False}}]
        ),
    )
    with pytest.raises(HTTPException) as exc:
        admin_auth.require_superuser(_request(), authorization=None, waj_admin_token="tok")
    assert exc.value.status_code == 403


def test_require_superuser_missing_permission_object_403(monkeypatch):
    """A user row with permission=None must NOT be treated as superuser."""
    monkeypatch.setattr(admin_auth, "validate_token_verified", lambda t: {"sub": "auth-1"})
    monkeypatch.setattr(
        admin_auth,
        "get_main_client",
        lambda: _FakeUserClient([{"id": "user-3", "email": "x@company.example", "permission": None}]),
    )
    with pytest.raises(HTTPException) as exc:
        admin_auth.require_superuser(_request(), authorization=None, waj_admin_token="tok")
    assert exc.value.status_code == 403


# --- resolve_superuser -------------------------------------------------------


def test_resolve_superuser_happy_path(monkeypatch):
    monkeypatch.setattr(admin_auth, "validate_token_verified", lambda t: {"sub": "auth-1"})
    monkeypatch.setattr(
        admin_auth,
        "get_main_client",
        lambda: _FakeUserClient(
            [{"id": "u1", "email": "a@company.example", "permission": {"is_superuser": True}}]
        ),
    )
    ctx = admin_auth.resolve_superuser("tok")
    assert ctx == {"user_token": "tok", "user_id": "u1", "email": "a@company.example"}


def test_resolve_superuser_verification_error_is_401(monkeypatch):
    def boom(_):
        raise TokenValidationError("Invalid token: Signature verification failed")

    monkeypatch.setattr(admin_auth, "validate_token_verified", boom)
    with pytest.raises(HTTPException) as exc:
        admin_auth.resolve_superuser("forged")
    assert exc.value.status_code == 401


# --- resolve_superuser cache -------------------------------------------------


class _CountingUserClient(_FakeUserClient):
    """_FakeUserClient that counts how many times `.execute()` is called —
    i.e. how many `user`-table round-trips resolve_superuser makes."""

    def __init__(self, rows):
        super().__init__(rows)
        self.calls = 0

    def execute(self):
        self.calls += 1
        return super().execute()


_SUPERUSER_ROW = {"id": "u1", "email": "a@company.example", "permission": {"is_superuser": True}}


def test_resolve_superuser_caches_db_lookup(monkeypatch):
    """A second call with the same `sub` inside the TTL reuses the cached
    superuser fact and does NOT re-hit the DB."""
    monkeypatch.setenv("ADMIN_AUTH_CACHE_TTL_SECONDS", "60")
    monkeypatch.setattr(admin_auth, "validate_token_verified", lambda t: {"sub": "auth-1"})
    client = _CountingUserClient([dict(_SUPERUSER_ROW)])
    monkeypatch.setattr(admin_auth, "get_main_client", lambda: client)

    first = admin_auth.resolve_superuser("tok-a")
    second = admin_auth.resolve_superuser("tok-b")  # same sub, different token

    assert client.calls == 1  # DB hit once, second served from cache
    assert first == {"user_token": "tok-a", "user_id": "u1", "email": "a@company.example"}
    # user_token always reflects the CURRENT request's token, never the cached one.
    assert second == {"user_token": "tok-b", "user_id": "u1", "email": "a@company.example"}


def test_resolve_superuser_reverifies_token_on_cache_hit(monkeypatch):
    """Even with a warm cache, a forged/expired token is rejected — the JWT
    is re-verified every call; only the DB lookup is memoized."""
    monkeypatch.setenv("ADMIN_AUTH_CACHE_TTL_SECONDS", "60")
    monkeypatch.setattr(admin_auth, "validate_token_verified", lambda t: {"sub": "auth-1"})
    client = _CountingUserClient([dict(_SUPERUSER_ROW)])
    monkeypatch.setattr(admin_auth, "get_main_client", lambda: client)
    admin_auth.resolve_superuser("good")  # warms the cache for auth-1

    def boom(_):
        raise TokenValidationError("Invalid token: Signature verification failed")

    monkeypatch.setattr(admin_auth, "validate_token_verified", boom)
    with pytest.raises(HTTPException) as exc:
        admin_auth.resolve_superuser("forged")
    assert exc.value.status_code == 401


def test_resolve_superuser_cache_disabled_when_ttl_zero(monkeypatch):
    """TTL=0 disables the cache — every call re-hits the DB."""
    monkeypatch.setenv("ADMIN_AUTH_CACHE_TTL_SECONDS", "0")
    monkeypatch.setattr(admin_auth, "validate_token_verified", lambda t: {"sub": "auth-1"})
    client = _CountingUserClient([dict(_SUPERUSER_ROW)])
    monkeypatch.setattr(admin_auth, "get_main_client", lambda: client)

    admin_auth.resolve_superuser("tok")
    admin_auth.resolve_superuser("tok")
    assert client.calls == 2


def test_resolve_superuser_does_not_cache_non_superuser(monkeypatch):
    """A 403 (non-superuser) is never cached — a later grant is seen at once."""
    monkeypatch.setenv("ADMIN_AUTH_CACHE_TTL_SECONDS", "60")
    monkeypatch.setattr(admin_auth, "validate_token_verified", lambda t: {"sub": "auth-1"})
    non_su = _CountingUserClient([{"id": "u1", "email": "a@company.example",
                                   "permission": {"is_superuser": False}}])
    monkeypatch.setattr(admin_auth, "get_main_client", lambda: non_su)
    with pytest.raises(HTTPException) as exc:
        admin_auth.resolve_superuser("tok")
    assert exc.value.status_code == 403

    # Now the user is granted superuser — resolve must reflect it immediately,
    # proving the earlier 403 wasn't cached.
    granted = _CountingUserClient([dict(_SUPERUSER_ROW)])
    monkeypatch.setattr(admin_auth, "get_main_client", lambda: granted)
    ctx = admin_auth.resolve_superuser("tok")
    assert ctx["user_id"] == "u1"
