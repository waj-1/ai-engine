"""Tests for the AI chat-session read/manage endpoints (endpoints/ai_sessions.py).

The Flutter app reads its chat history through these endpoints (the chat
tables live in the standalone AI project the app cannot reach directly).
We mount only this router so the suite doesn't need the full `main` app,
and patch the persistence layer — query behaviour is covered separately in
tests/test_ai_sessions_persistence.py.
"""

import time

import jwt
import pytest
from unittest.mock import AsyncMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

# These endpoints scope RLS-bypassing service-client reads by the token's sub,
# so they authenticate with `authenticate_verified` (signature-checked). The
# test token must therefore be a real HS256 token (aud=authenticated) signed
# with the same SUPABASE_JWT_SECRET the endpoint verifies against — an unsigned
# fixture token is correctly rejected (see tests/test_ai_sessions_auth.py).
_JWT_SECRET = "test-ai-sessions-jwt-secret"


@pytest.fixture(autouse=True)
def _jwt_secret(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", _JWT_SECRET)


@pytest.fixture
def client():
    from endpoints.ai_sessions import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def auth(test_user_id):
    token = jwt.encode(
        {
            "sub": test_user_id,
            "aud": "authenticated",
            "role": "authenticated",
            "exp": int(time.time()) + 3600,
        },
        _JWT_SECRET,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def test_list_sessions_requires_auth(client):
    r = client.get("/ai/sessions", params={"business_id": "b1"})
    assert r.status_code == 401


def test_list_sessions_without_a_business_lists_the_callers_own_unowned_sessions(client, auth):
    """A generic agent's sessions carry no business. Omitting business_id
    lists those, still scoped to the token's user and nobody else's."""
    with patch("endpoints.ai_sessions.chat_persistence.list_sessions",
               new=AsyncMock(return_value=[{"id": "s9", "user_id": "test-user-123"}])) as m:
        r = client.get("/ai/sessions", headers=auth)
    assert r.status_code == 200
    assert r.json() == {"sessions": [{"id": "s9", "user_id": "test-user-123"}]}
    m.assert_awaited_once_with("test-user-123", None, source=None)


def test_list_sessions_without_a_business_still_requires_auth(client):
    assert client.get("/ai/sessions").status_code == 401


def test_list_sessions_returns_sessions_scoped_to_token_user(client, auth):
    with patch("endpoints.ai_sessions.chat_persistence.list_sessions",
               new=AsyncMock(return_value=[{"id": "s1", "user_id": "test-user-123"}])) as m:
        r = client.get("/ai/sessions", params={"business_id": "b1"}, headers=auth)
    assert r.status_code == 200
    assert r.json() == {"sessions": [{"id": "s1", "user_id": "test-user-123"}]}
    m.assert_awaited_once_with("test-user-123", "b1", source=None)


def test_get_session_404_when_missing(client, auth):
    with patch("endpoints.ai_sessions.chat_persistence.get_session",
               new=AsyncMock(return_value=None)):
        r = client.get("/ai/sessions/s1", headers=auth)
    assert r.status_code == 404


def test_get_session_returns_session(client, auth):
    with patch("endpoints.ai_sessions.chat_persistence.get_session",
               new=AsyncMock(return_value={"id": "s1", "title": "T"})):
        r = client.get("/ai/sessions/s1", headers=auth)
    assert r.status_code == 200
    assert r.json()["id"] == "s1"


def test_get_messages_404_when_session_not_owned(client, auth):
    with patch("endpoints.ai_sessions.chat_persistence.list_session_messages",
               new=AsyncMock(return_value=None)):
        r = client.get("/ai/sessions/s1/messages", headers=auth)
    assert r.status_code == 404


def test_get_messages_returns_messages(client, auth):
    with patch("endpoints.ai_sessions.chat_persistence.list_session_messages",
               new=AsyncMock(return_value=[{"id": "m1", "role": "user"}])):
        r = client.get("/ai/sessions/s1/messages", headers=auth)
    assert r.status_code == 200
    assert r.json() == {"messages": [{"id": "m1", "role": "user"}]}


def test_rename_session_returns_updated_session(client, auth):
    with patch("endpoints.ai_sessions.chat_persistence.rename_session",
               new=AsyncMock(return_value={"id": "s1", "title": "New"})) as m:
        r = client.patch("/ai/sessions/s1", json={"title": "New"}, headers=auth)
    assert r.status_code == 200
    assert r.json()["title"] == "New"
    m.assert_awaited_once_with("s1", "test-user-123", "New")


def test_rename_session_404_when_missing(client, auth):
    with patch("endpoints.ai_sessions.chat_persistence.rename_session",
               new=AsyncMock(return_value=None)):
        r = client.patch("/ai/sessions/s1", json={"title": "New"}, headers=auth)
    assert r.status_code == 404


def test_delete_session_success(client, auth):
    with patch("endpoints.ai_sessions.chat_persistence.soft_delete_session",
               new=AsyncMock(return_value=True)) as m:
        r = client.delete("/ai/sessions/s1", headers=auth)
    assert r.status_code == 200
    assert r.json() == {"success": True}
    m.assert_awaited_once_with("s1", "test-user-123")


def test_delete_session_404_when_missing(client, auth):
    with patch("endpoints.ai_sessions.chat_persistence.soft_delete_session",
               new=AsyncMock(return_value=False)):
        r = client.delete("/ai/sessions/s1", headers=auth)
    assert r.status_code == 404


def test_delete_session_requires_auth(client):
    r = client.delete("/ai/sessions/s1")
    assert r.status_code == 401
