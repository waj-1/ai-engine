"""GET/PATCH/DELETE /ai/sessions with a portal-issued API key.

A key acts as itself: the chats it started through /v1/agents are the ones it
can list and read. Another key, a revoked key and a malformed key see nothing.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from admin.services import api_keys as service
from tests.api_keys_fake import install


@pytest.fixture(autouse=True)
def _isolate_global_ip_limiter():
    import main

    main.rate_limit_store.clear()
    yield
    main.rate_limit_store.clear()


@pytest.fixture
def wired(monkeypatch):
    import main

    store = install(monkeypatch)
    return TestClient(main.app), store


def _get(client, key, path="/ai/sessions"):
    return client.get(path, headers={"Authorization": f"Bearer {key}"})


def test_a_chat_key_lists_its_own_sessions_and_nobody_elses(wired):
    client, _store = wired
    mine, key = service.create_key("mine", ["chat"])
    other, other_key = service.create_key("other", ["chat"])
    with patch("core.chat_persistence.list_sessions", new=AsyncMock(return_value=[{"id": "s1"}])) as listed:
        assert _get(client, key).json() == {"sessions": [{"id": "s1"}]}
        assert listed.await_args.args[0] == str(mine["id"])
        _get(client, other_key)
        assert listed.await_args.args[0] == str(other["id"])
        assert str(mine["id"]) != str(other["id"])


def test_a_key_reads_one_session_scoped_to_itself(wired):
    client, _store = wired
    row, key = service.create_key("mine", ["chat"])
    with patch("core.chat_persistence.get_session", new=AsyncMock(return_value={"id": "s1"})) as got:
        assert _get(client, key, "/ai/sessions/s1").status_code == 200
        assert str(row["id"]) in [str(a) for a in got.await_args.args] + [str(v) for v in got.await_args.kwargs.values()]


def test_a_revoked_or_unknown_key_is_refused_before_any_session_is_read(wired):
    client, _store = wired
    row, key = service.create_key("gone", ["chat"])
    service.revoke_key(row["id"], None)
    with patch("core.chat_persistence.list_sessions", new=AsyncMock(return_value=[])) as listed:
        assert _get(client, key).status_code == 401
        assert _get(client, "wae_" + "x" * 43).status_code == 401
        listed.assert_not_awaited()
