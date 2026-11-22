"""update_profile accepts delegatable and status (with guard rails: status
value whitelist + dual-active-row refusal — see admin/services/profiles.py)."""
from __future__ import annotations

import admin_mcp.tools_configure as tc
from tests.admin_mcp.conftest import FakeClient

ROW = {"profile_id": "p1", "agent_kind": "business", "environment": "staging",
       "slug": "business_reporting", "status": "specialist", "delegatable": False}


def test_update_profile_sets_delegatable(monkeypatch):
    client = FakeClient({"agent_profiles": [dict(ROW)]})
    # update_profile() delegates the actual write to
    # admin.services.profiles.update_profile_row(), which resolves its
    # client via core.auth.admin_service_client() (not admin_mcp's
    # client_for_env) — see test_update_profile_template_returns_err in
    # test_tools_configure.py for the same pattern.
    monkeypatch.setattr("core.auth.admin_service_client", lambda: client)
    out = tc.update_profile(profile_id="p1", delegatable=True)
    assert "error" not in out
    assert client.table("agent_profiles")._data[0]["delegatable"] is True


def test_update_profile_sets_status_specialist(monkeypatch):
    row = {**ROW, "status": "draft"}
    client = FakeClient({"agent_profiles": [row]})
    monkeypatch.setattr("core.auth.admin_service_client", lambda: client)
    out = tc.update_profile(profile_id="p1", status="specialist")
    assert "error" not in out
    assert client.table("agent_profiles")._data[0]["status"] == "specialist"


def test_update_profile_active_refused_when_another_active_exists(monkeypatch):
    # p1 is a specialist trying to promote itself to 'active'; p2 already
    # serves (business, staging) as the active row — the dual-active guard
    # must refuse p1's transition and leave it untouched.
    target = {**ROW, "profile_id": "p1", "status": "specialist"}
    other_active = {**ROW, "profile_id": "p2", "slug": "business_main",
                    "status": "active", "is_active": True}
    client = FakeClient({"agent_profiles": [target, other_active]})
    monkeypatch.setattr("core.auth.admin_service_client", lambda: client)

    out = tc.update_profile(profile_id="p1", status="active")

    assert "error" in out
    assert "business_main" in out["error"]
    by_id = {r["profile_id"]: r for r in client.table("agent_profiles")._data}
    assert by_id["p1"]["status"] == "specialist"  # unchanged


def test_update_profile_status_template_refused(monkeypatch):
    row = {**ROW, "status": "draft"}
    client = FakeClient({"agent_profiles": [row]})
    monkeypatch.setattr("core.auth.admin_service_client", lambda: client)

    out = tc.update_profile(profile_id="p1", status="template")

    assert "error" in out
    assert client.table("agent_profiles")._data[0]["status"] == "draft"  # unchanged


def test_update_profile_status_bogus_refused(monkeypatch):
    row = {**ROW, "status": "draft"}
    client = FakeClient({"agent_profiles": [row]})
    monkeypatch.setattr("core.auth.admin_service_client", lambda: client)

    out = tc.update_profile(profile_id="p1", status="bogus")

    assert "error" in out
    assert client.table("agent_profiles")._data[0]["status"] == "draft"  # unchanged
