"""API keys on the admin MCP server: which keys may open `/mcp` (the scope
boundary), who a key acts as, and the `api_keys` tool (list / create / revoke).
"""
from __future__ import annotations

import pytest

import admin_mcp.tools_govern as tg
from admin.services import api_keys as service
from core import api_keys as keys
from tests.admin_mcp.conftest import ACTOR, MCP_HEADERS
from tests.api_keys_fake import install

CREATOR = "00000000-0000-0000-0000-0000000000aa"


@pytest.fixture
def store(monkeypatch):
    return install(monkeypatch)


def _call(client, key, method="tools/list", params=None):
    return client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers={**MCP_HEADERS, "Authorization": f"Bearer {key}"})


# --------------------------------------------------------------------------
# Auth: the scope boundary
# --------------------------------------------------------------------------


def test_an_admin_key_opens_the_mcp_server(mcp_client, store):
    _, key = service.create_key("Ops", ["admin"], created_by=CREATOR)
    resp = _call(mcp_client, key)
    assert resp.status_code == 200
    assert any(t["name"] == "api_keys" for t in resp.json()["result"]["tools"])


def test_a_chat_only_key_cannot_administer_the_engine(mcp_client, store):
    """The boundary this feature exists to draw: a key handed to a chat widget
    must not be able to list, let alone change, the engine's configuration."""
    _, key = service.create_key("Website widget", ["chat"], created_by=CREATOR)
    for method, params in (("tools/list", None), ("initialize", {"protocolVersion": "2025-03-26"}),
                           ("tools/call", {"name": "api_keys", "arguments": {"action": "list"}}),
                           ("tools/call", {"name": "users", "arguments": {"action": "list"}})):
        resp = _call(mcp_client, key, method, params)
        assert resp.status_code == 403, method
        assert resp.json() == {"error": "This API key does not have the scope this request needs"}


def test_revoked_expired_and_unknown_keys_are_refused_alike(mcp_client, store):
    row, revoked = service.create_key("Ops", ["admin"], created_by=CREATOR)
    assert _call(mcp_client, revoked).status_code == 200
    service.revoke_key(row["id"], revoked_by=CREATOR)
    other, expired = service.create_key("Short", ["admin"], created_by=CREATOR)
    store.row(other["id"])["expires_at"] = "2020-01-01T00:00:00+00:00"
    answers = {(r.status_code, r.text) for r in (
        _call(mcp_client, k) for k in (revoked, expired, keys.generate_key(), "wae_nope"))}
    assert answers == {(401, '{"error": "Invalid API key"}')}


def test_a_key_is_never_handed_to_the_token_verifier(mcp_client, store, monkeypatch):
    import admin_mcp.auth as mcp_auth

    handed = []
    monkeypatch.setattr(mcp_auth, "resolve_superuser",
                        lambda tok: handed.append(tok) or dict(ACTOR))
    _, key = service.create_key("Ops", ["admin"], created_by=CREATOR)
    assert _call(mcp_client, key).status_code == 200
    assert _call(mcp_client, keys.generate_key()).status_code == 401
    assert handed == []
    # ...and a token still goes there, unchanged.
    assert _call(mcp_client, "a.jwt.token").status_code == 200
    assert handed == ["a.jwt.token"]


def test_a_change_made_with_a_key_is_attributed_to_that_key(mcp_client, store):
    """The actor of an MCP call made with a key is the key itself, so the audit
    log can say which key did it (not the person who once created the key)."""
    row, key = service.create_key("Ops", ["admin"], created_by=CREATOR)
    resp = _call(mcp_client, key, "tools/call",
                 {"name": "api_keys", "arguments": {"action": "create", "name": "Made over MCP"}})
    assert resp.status_code == 200
    made = [r for r in store.rows if r["name"] == "Made over MCP"]
    assert made and made[0]["created_by"] == row["id"]
    assert store.audit[-1]["actor_user_id"] == row["id"] and store.audit[-1]["action"] == "create"


# --------------------------------------------------------------------------
# The api_keys tool (called directly, as the autouse actor)
# --------------------------------------------------------------------------


def test_create_returns_the_key_once_and_list_never_does(store):
    out = tg.api_keys(action="create", name="CI runner", scopes=["chat", "admin"], expires_in_days=30)
    key = out["key"]
    assert out["created"] is True and keys._KEY_RE.match(key)
    assert out["api_key"]["scopes"] == ["chat", "admin"] and out["api_key"]["expires_at"]
    assert out["api_key"]["created_by"] == ACTOR["user_id"]
    assert "key_hash" not in out["api_key"]
    assert keys.verify_key(key)["id"] == out["api_key"]["id"]  # it is the working key

    listed = tg.api_keys(action="list")
    assert listed["count"] == 1 and listed["api_keys"][0]["status"] == "active"
    blob = repr(listed)
    assert key not in blob and keys.hash_key(key) not in blob and "key_hash" not in blob


def test_create_defaults_to_the_chat_scope(store):
    out = tg.api_keys(action="create", name="Widget")
    assert out["api_key"]["scopes"] == ["chat"] and out["api_key"]["expires_at"] is None


@pytest.mark.parametrize("kwargs,message", [
    ({"name": ""}, "name"),
    ({"name": "x", "scopes": ["root"]}, "unknown scope"),
    ({"name": "x", "expires_in_days": 99999}, "expires_in_days"),
])
def test_create_reports_bad_input_as_an_error_payload(store, kwargs, message):
    out = tg.api_keys(action="create", **kwargs)
    assert message in out["error"] and "key" not in out and store.rows == []


def test_revoke_is_confirm_gated_then_final(store):
    made = tg.api_keys(action="create", name="CI runner")
    key, key_id = made["key"], made["api_key"]["id"]

    dry = tg.api_keys(action="revoke", key_id=key_id)
    assert dry["confirmed"] is False and keys.verify_key(key)  # still works

    done = tg.api_keys(action="revoke", key_id=key_id, confirm=True)
    assert done["revoked"] is True and done["api_key"]["status"] == "revoked"
    with pytest.raises(Exception):
        keys.verify_key(key)
    assert [a["action"] for a in store.audit] == ["create", "revoke"]
    assert store.audit[-1]["actor_user_id"] == ACTOR["user_id"]

    assert tg.api_keys(action="list", include_revoked=False)["count"] == 0
    assert tg.api_keys(action="list")["count"] == 1


def test_revoke_needs_a_known_key(store):
    assert "key_id" in tg.api_keys(action="revoke")["error"]
    assert tg.api_keys(action="revoke", key_id="11111111-1111-1111-1111-111111111111",
                       confirm=True)["error"] == "API key not found"
