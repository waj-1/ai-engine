"""The admin MCP `provider_keys` tool: set, remove, list and test stored model
keys. No action may return a key or its ciphertext; a write with no
SECRET_KEY is refused; writes carry the calling operator into the audit log.
"""
from __future__ import annotations

import json

import pytest

import admin_mcp.tools_configure as tc
from admin.services import provider_keys as svc
from core import provider_keys as pk
from tests.admin_mcp.conftest import ACTOR, rpc
from tests.provider_keys_fake import FakeDB

SECRET = "unit-test-secret-key-0123456789"
KEY = "sk-sample-ZYXWVUTSRQPONMLK-7788"
KEY2 = "sk-sample-ABCDEFGHIJKLMNOP-9911"


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setenv("SECRET_KEY", SECRET)
    monkeypatch.setattr(tc, "client_for_env", lambda env: fake)
    monkeypatch.setattr("core.auth.get_service_client", lambda: fake)
    monkeypatch.setattr(
        "admin.audit.record_change",
        lambda **kw: fake.tables.setdefault("agent_audit_log", []).append(kw))
    pk.invalidate()
    yield fake
    pk.invalidate()


async def test_set_list_remove_never_return_the_key(db, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "env-gemini-key")
    stored = await tc.provider_keys(action="set", provider="openai", value=KEY)
    assert stored["stored"] is True and stored["key"]["hint"] == "7788"
    named = await tc.provider_keys(action="set", provider="openai", value=KEY2, name="catalog")
    listed = await tc.provider_keys(action="list")
    by_provider = {p["provider"]: p for p in listed["providers"]}
    assert by_provider["openai"]["source"] == "portal"
    assert by_provider["openai"]["credentials"][0]["name"] == "catalog"
    assert by_provider["google_genai"]["source"] == "environment"
    assert pk.stored_key("openai") == KEY  # the runtime uses it at once

    dry = await tc.provider_keys(action="remove", provider="openai")
    assert dry["confirmed"] is False and len(db.tables["provider_keys"]) == 2
    removed = await tc.provider_keys(action="remove", provider="openai", confirm=True)
    assert removed == {"removed": True, "provider": "openai", "name": ""}
    again = await tc.provider_keys(action="remove", provider="openai", confirm=True)
    assert "no stored key" in again["error"]

    ciphertexts = [r["ciphertext"] for r in db.tables["provider_keys"]]
    everything = json.dumps([stored, named, listed, dry, removed, again,
                             db.tables["agent_audit_log"]], default=str)
    for secret in (KEY, KEY2, *ciphertexts):
        assert secret not in everything
    assert "ciphertext" not in everything
    actions = [(r["action"], r["actor_user_id"]) for r in db.tables["agent_audit_log"]]
    assert actions == [("set", ACTOR["user_id"]), ("set", ACTOR["user_id"]),
                       ("delete", ACTOR["user_id"])]


async def test_set_is_refused_without_secret_key(db, monkeypatch):
    monkeypatch.delenv("SECRET_KEY")
    out = await tc.provider_keys(action="set", provider="openai", value=KEY)
    assert "SECRET_KEY is not set" in out["error"] and KEY not in json.dumps(out)
    assert db.tables.get("provider_keys", []) == []
    listed = await tc.provider_keys(action="list")
    assert listed["can_store"] is False and "SECRET_KEY" in listed["problem"]


async def test_bad_requests_are_errors_not_exceptions(db):
    assert "provider is required" in (await tc.provider_keys(action="set", value=KEY))["error"]
    out = await tc.provider_keys(action="set", provider="nonesuch", value=KEY)
    assert "unknown provider" in out["error"]
    out = await tc.provider_keys(action="set", provider="openai", value="")
    assert "empty" in out["error"]


async def test_test_action_reports_the_provider_error_without_the_key(db, monkeypatch):
    await tc.provider_keys(action="set", provider="anthropic", value=KEY)

    class Rejected:
        status_code = 401
        text = ""

        def json(self):
            return {"error": {"message": f"invalid x-api-key {KEY}"}}

    monkeypatch.setattr(svc, "_http_get", lambda url, headers: Rejected())
    out = await tc.provider_keys(action="test", provider="anthropic")
    assert out["ok"] is False and out["source"] == "portal"
    assert "401" in out["detail"] and "invalid x-api-key" in out["detail"]
    assert KEY not in out["detail"]


def test_tool_is_served_over_mcp_and_its_schema_asks_for_no_reveal(mcp_client, db):
    payload = rpc(mcp_client, "tools/list").json()
    tool = next(t for t in payload["result"]["tools"] if t["name"] == "provider_keys")
    props = tool["inputSchema"]["properties"]
    assert set(props) == {"action", "provider", "value", "name", "env", "confirm"}
    assert props["action"]["enum"] == ["list", "set", "remove", "test"]
    assert all(p.get("description") for p in props.values())
