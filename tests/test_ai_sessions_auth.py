"""`/ai/sessions` reads/mutates chat transcripts through the RLS-bypassing
service client, scoped only by the JWT's `sub`. So the token signature MUST be
verified — otherwise a forged `{"sub": "<victim>"}` token is a cross-user IDOR
(read, rename, soft-delete any user's transcripts). This locks in that the
endpoints use `authenticate_verified` (signature-checked), not the unverified
`authenticate`.
"""
from __future__ import annotations

import time

import jwt
import pytest
from fastapi import HTTPException

from core.auth import authenticate_verified

_SECRET = "test-jwt-secret-value-at-least-32-bytes-long!!"


def _token(secret=_SECRET, **claims):
    payload = {
        "sub": "11111111-1111-1111-1111-111111111111",
        "aud": "authenticated",
        "role": "authenticated",
        "exp": int(time.time()) + 3600,
        **claims,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def test_valid_signed_token_returns_sub(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", _SECRET)
    raw = _token()
    token, user_id = authenticate_verified(f"Bearer {raw}")
    assert token == raw
    assert user_id == "11111111-1111-1111-1111-111111111111"


def test_forged_signature_is_rejected(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", _SECRET)
    # Attacker signs a victim's sub with the wrong secret.
    forged = _token(secret="attacker-controlled-secret-not-the-real-one")
    with pytest.raises(HTTPException) as exc:
        authenticate_verified(f"Bearer {forged}")
    assert exc.value.status_code == 401


def test_unsigned_alg_none_token_is_rejected(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", _SECRET)
    unsigned = jwt.encode(
        {"sub": "victim", "aud": "authenticated", "exp": int(time.time()) + 3600},
        key="",
        algorithm="none",
    )
    with pytest.raises(HTTPException) as exc:
        authenticate_verified(f"Bearer {unsigned}")
    assert exc.value.status_code == 401


def test_missing_secret_fails_closed(monkeypatch):
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
    with pytest.raises(HTTPException) as exc:
        authenticate_verified(f"Bearer {_token()}")
    assert exc.value.status_code == 401


def test_missing_bearer_is_401(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", _SECRET)
    with pytest.raises(HTTPException) as exc:
        authenticate_verified(None)
    assert exc.value.status_code == 401


def test_endpoints_use_verified_auth():
    """Guard against a regression back to the unverified `authenticate`."""
    import endpoints.ai_sessions as mod
    src = __import__("inspect").getsource(mod)
    assert "asyncio.to_thread(authenticate_verified, authorization)" in src
    assert "= authenticate(authorization)" not in src
