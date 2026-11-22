"""Direct coverage for admin.auth.require_superuser — the gate every admin
route depends on. The rest of the suite overrides it via
dependency_overrides, so its actual 401/403/500 branches were never exercised.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import admin.auth as auth
from core.auth import TokenValidationError


def _request():
    return SimpleNamespace(state=SimpleNamespace())


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def schema(self, *_a, **_k):
        return self

    def table(self, *_a, **_k):
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        return SimpleNamespace(data=self._rows)


def _client_returning(rows, monkeypatch):
    # The superuser check reads from the MAIN project (public.user).
    monkeypatch.setattr(auth, "get_main_client", lambda: _Query(rows))


# --- token extraction ---------------------------------------------------------

def test_missing_token_is_401():
    with pytest.raises(HTTPException) as ei:
        auth.require_superuser(_request(), authorization=None, waj_admin_token=None)
    assert ei.value.status_code == 401


def test_invalid_token_is_401(monkeypatch):
    def _bad(_t):
        raise TokenValidationError("bad sig")

    monkeypatch.setattr(auth, "validate_token_verified", _bad)
    with pytest.raises(HTTPException) as ei:
        auth.require_superuser(_request(), authorization="Bearer x.y.z", waj_admin_token=None)
    assert ei.value.status_code == 401


def test_token_missing_sub_is_401(monkeypatch):
    monkeypatch.setattr(auth, "validate_token_verified", lambda _t: {})
    with pytest.raises(HTTPException) as ei:
        auth.require_superuser(_request(), authorization=None, waj_admin_token="cookie-tok")
    assert ei.value.status_code == 401


# --- authorization ------------------------------------------------------------

def test_no_user_row_is_403(monkeypatch):
    monkeypatch.setattr(auth, "validate_token_verified", lambda _t: {"sub": "auth-1"})
    _client_returning([], monkeypatch)
    with pytest.raises(HTTPException) as ei:
        auth.require_superuser(_request(), authorization=None, waj_admin_token="cookie-tok")
    assert ei.value.status_code == 403


def test_non_superuser_is_403(monkeypatch):
    monkeypatch.setattr(auth, "validate_token_verified", lambda _t: {"sub": "auth-1"})
    _client_returning(
        [{"id": "u1", "email": "x@y.z", "permission": {"is_superuser": False}}], monkeypatch
    )
    with pytest.raises(HTTPException) as ei:
        auth.require_superuser(_request(), authorization=None, waj_admin_token="cookie-tok")
    assert ei.value.status_code == 403


def test_user_lookup_failure_is_500(monkeypatch):
    monkeypatch.setattr(auth, "validate_token_verified", lambda _t: {"sub": "auth-1"})

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(auth, "get_main_client", _boom)
    with pytest.raises(HTTPException) as ei:
        auth.require_superuser(_request(), authorization=None, waj_admin_token="cookie-tok")
    assert ei.value.status_code == 500


def test_superuser_returns_context_and_stashes_state(monkeypatch):
    monkeypatch.setattr(auth, "validate_token_verified", lambda _t: {"sub": "auth-1"})
    _client_returning(
        [{"id": "u1", "email": "boss@company.example", "permission": {"is_superuser": True}}], monkeypatch
    )
    req = _request()
    ctx = auth.require_superuser(req, authorization="Bearer good.tok.here", waj_admin_token=None)
    assert ctx == {"user_token": "good.tok.here", "user_id": "u1", "email": "boss@company.example"}
    assert req.state.admin_user_id == "u1"
    assert req.state.admin_email == "boss@company.example"


def test_header_preferred_over_cookie(monkeypatch):
    seen = {}

    def _capture(tok):
        seen["tok"] = tok
        return {"sub": "auth-1"}

    monkeypatch.setattr(auth, "validate_token_verified", _capture)
    _client_returning(
        [{"id": "u1", "email": "b@w.ai", "permission": {"is_superuser": True}}], monkeypatch
    )
    auth.require_superuser(
        _request(), authorization="Bearer header.tok", waj_admin_token="cookie.tok"
    )
    assert seen["tok"] == "header.tok"
