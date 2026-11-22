"""Tests for the Task 15 govern-domain tools: users, audit, memory,
channels, test_fixtures, jobs. Same FakeClient pattern as
test_tools_configure.py / test_tools_ship.py; tools that lazily import a
handler module (users -> core.auth.get_main_client, jobs -> admin._jobs)
get their SOURCE module attribute monkeypatched rather than the
tools_govern name, since the import happens fresh inside the function body
on every call. channels/test_fixtures read AND write through
client_for_env(env)."""
from __future__ import annotations

import admin_mcp.tools_govern as tg
from tests.admin_mcp.conftest import FakeClient

USER_ROW = {"id": "u1", "email": "op@waj.test", "phone_number": None,
            "role": "owner", "permission": {"is_superuser": False},
            "business_id": "b1", "created_at": "2026-01-01T00:00:00Z",
            "is_deleted": False}

CHANNEL_ROW = {"channel_id": "c1", "slug": "waj-app", "name": "WAJ App",
               "kind": "embed", "is_active": True, "allowed_origins": [],
               "allowed_agent_kinds": [], "default_profile_id": None,
               "business_id": None, "auth_required": True,
               "rate_limit_rpm": 60, "config": {}, "description": None}

FIXTURE_JWT = {"fixture_id": "f1", "name": "staff_jwt", "environment": "staging",
               "kind": "jwt", "value": "eyJ.top.secret", "description": None}


def _fake_env_client(monkeypatch, tables):
    """Patch client_for_env (used by audit/memory/channels-read/test_fixtures-read)."""
    client = FakeClient(tables)
    monkeypatch.setattr(tg, "client_for_env", lambda env: client)
    return client


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


def test_users_list_computes_superuser_and_drops_permission(monkeypatch):
    client = FakeClient({"user": [dict(USER_ROW)]})
    import core.auth as core_auth
    monkeypatch.setattr(core_auth, "get_main_client", lambda: client)

    out = tg.users(action="list")

    assert out["users"][0]["is_superuser"] is False
    assert "permission" not in out["users"][0]


def test_users_set_superuser_without_confirm(monkeypatch):
    client = FakeClient({"user": [dict(USER_ROW)]})
    import core.auth as core_auth
    monkeypatch.setattr(core_auth, "get_main_client", lambda: client)

    out = tg.users(action="set_superuser", user_id="u1", grant=True)

    assert out["confirmed"] is False
    assert "u1" in out["would"]


def test_users_set_superuser_audits_grant(monkeypatch):
    client = FakeClient({"user": [dict(USER_ROW)]})
    import core.auth as core_auth
    monkeypatch.setattr(core_auth, "get_main_client", lambda: client)
    audits = []
    monkeypatch.setattr(tg, "actor_audit", lambda *a, **k: audits.append((a, k)))

    out = tg.users(action="set_superuser", user_id="u1", grant=True, confirm=True)

    assert out == {"updated": True, "user_id": "u1", "is_superuser": True}
    assert audits[0][0] == ("user", "u1", "GRANT_SUPERUSER")
    assert audits[0][1] == {"after": {"is_superuser": True}}


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


def test_audit_verify_returns_marker_ack(monkeypatch):
    audits = []
    monkeypatch.setattr(tg, "actor_audit", lambda *a, **k: audits.append((a, k)))

    out = tg.audit(action="verify")  # deprecated name of probe_write

    assert out["verified_marker_recorded"] is True
    assert out["probe_write"] == "ok"
    assert audits[0][0] == ("agent_audit_log",
                            "00000000-0000-0000-0000-000000000000",
                            "CALLEE_VERIFY")


def test_audit_list_filters_by_entity_type(monkeypatch):
    _fake_env_client(monkeypatch, {"agent_audit_log": [
        {"audit_id": "a1", "entity_type": "agent_tools", "action": "create"},
        {"audit_id": "a2", "entity_type": "agent_models", "action": "create"},
    ]})

    out = tg.audit(action="list", entity_type="agent_tools")

    assert out["count"] == 1
    assert out["audit_log"][0]["audit_id"] == "a1"


def test_audit_get_not_found(monkeypatch):
    _fake_env_client(monkeypatch, {"agent_audit_log": []})
    out = tg.audit(action="get", audit_id="missing")
    assert "error" in out


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


def test_memory_get_config_defaults_merged(monkeypatch, product_build):
    # Two same-kind rows in the shared DB (staging + prod). get_config defaults
    # to env="staging" and must return the staging row, not whichever sorts
    # first — the env-scoping regression guard.
    _fake_env_client(monkeypatch, {"agent_profiles": [
        {"profile_id": "p1", "agent_kind": "sample_agent", "slug": "main",
         "environment": "staging", "is_active": True, "status": "active",
         "memory_config": {"enabled": True}},
        {"profile_id": "p1-prod", "agent_kind": "sample_agent", "slug": "main",
         "environment": "prod", "is_active": True, "status": "active",
         "memory_config": {"enabled": False}},
    ]})

    out = tg.memory(action="get_config", agent_kind="sample_agent")

    assert out["profile"]["profile_id"] == "p1"
    assert out["config_form"]["enabled"] is True


def test_memory_get_config_selects_prod_profile_when_env_prod(monkeypatch, product_build):
    _fake_env_client(monkeypatch, {"agent_profiles": [
        {"profile_id": "p1", "agent_kind": "sample_agent", "slug": "main",
         "environment": "staging", "is_active": True, "status": "active",
         "memory_config": {"enabled": True}},
        {"profile_id": "p1-prod", "agent_kind": "sample_agent", "slug": "main",
         "environment": "prod", "is_active": True, "status": "active",
         "memory_config": {"enabled": False}},
    ]})

    out = tg.memory(action="get_config", agent_kind="sample_agent", env="prod")

    assert out["profile"]["profile_id"] == "p1-prod"
    assert out["config_form"]["enabled"] is False


def test_memory_pin_audits_pin_action(monkeypatch):
    client = _fake_env_client(monkeypatch, {"ai_user_memory": [
        {"id": "m1", "pinned": False},
    ]})
    audits = []
    monkeypatch.setattr(tg, "actor_audit", lambda *a, **k: audits.append((a, k)))

    out = tg.memory(action="pin", memory_id="m1", pinned=True)

    assert out == {"pinned": True, "memory_id": "m1"}
    assert audits[0][0] == ("ai_user_memory", "m1", "pin")
    assert client.tables["ai_user_memory"]._data[0]["pinned"] is True


def test_memory_delete_without_confirm_does_not_archive(monkeypatch):
    client = _fake_env_client(monkeypatch, {"ai_user_memory": [
        {"id": "m1", "status": "active"},
    ]})
    audits = []
    monkeypatch.setattr(tg, "actor_audit", lambda *a, **k: audits.append((a, k)))

    out = tg.memory(action="delete", memory_id="m1")

    assert out["confirmed"] is False
    assert "m1" in out["would"]
    assert client.tables["ai_user_memory"]._data[0]["status"] == "active"
    assert audits == []


def test_memory_delete_with_confirm_soft_archives(monkeypatch):
    client = _fake_env_client(monkeypatch, {"ai_user_memory": [
        {"id": "m1", "status": "active"},
    ]})
    monkeypatch.setattr(tg, "actor_audit", lambda *a, **k: None)

    out = tg.memory(action="delete", memory_id="m1", confirm=True)

    assert out == {"deleted": True, "memory_id": "m1"}
    assert client.tables["ai_user_memory"]._data[0]["status"] == "archived"


# ---------------------------------------------------------------------------
# channels
# ---------------------------------------------------------------------------


def test_channels_create_rejects_bad_slug():
    out = tg.channels(action="create", slug="1-bad-slug", name="Bad",
                      kind="embed")
    assert "error" in out
    assert "slug" in out["error"]


def test_channels_create_tenant_kind_requires_business_id(monkeypatch):
    from admin import channel_handlers

    monkeypatch.setattr(channel_handlers, "_CHANNEL_KINDS",
                        channel_handlers._CHANNEL_KINDS + ("tenant_site",))
    monkeypatch.setattr(channel_handlers, "_TENANT_KINDS", ("tenant_site",))
    out = tg.channels(action="create", slug="orchard", name="Orchard",
                      kind="tenant_site")
    assert "error" in out
    assert "business_id" in out["error"]


def test_channels_list_filters_by_kind(monkeypatch):
    _fake_env_client(monkeypatch, {"agent_channels": [
        dict(CHANNEL_ROW),
        {**CHANNEL_ROW, "channel_id": "c2", "kind": "public_api", "slug": "api"},
    ]})

    out = tg.channels(action="list", kind="embed")

    assert len(out["channels"]) == 1
    assert out["channels"][0]["channel_id"] == "c1"


def test_channels_delete_confirm_gated(monkeypatch):
    _fake_env_client(monkeypatch, {"agent_channels": [dict(CHANNEL_ROW)]})

    out = tg.channels(action="delete", channel_id="c1")

    assert out["confirmed"] is False


# ---------------------------------------------------------------------------
# test_fixtures
# ---------------------------------------------------------------------------


def test_test_fixtures_list_masks_jwt_kind(monkeypatch):
    _fake_env_client(monkeypatch, {"test_fixtures": [dict(FIXTURE_JWT)]})

    out = tg.test_fixtures(action="list", env="staging")

    assert out["fixtures"][0]["value"] == "••••"


def test_test_fixtures_export_reveals_real_values(monkeypatch):
    _fake_env_client(monkeypatch, {"test_fixtures": [
        {"name": "staff_jwt", "value": "eyJ.top.secret", "environment": "staging"},
    ]})

    out = tg.test_fixtures(action="export", env="staging")

    assert out["lines"] == ["WAJ_TEST_STAFF_JWT=eyJ.top.secret"]


def test_test_fixtures_export_audits_bulk_reveal(monkeypatch):
    """Export is a bulk secrets reveal — it must leave an audit trace, and
    that trace must carry names only, never the revealed values."""
    _fake_env_client(monkeypatch, {"test_fixtures": [
        {"name": "staff_jwt", "value": "eyJ.top.secret", "environment": "staging"},
        {"name": "biz_uuid", "value": "b-1", "environment": "staging"},
    ]})
    audits = []
    monkeypatch.setattr(tg, "actor_audit", lambda *a, **k: audits.append((a, k)))

    tg.test_fixtures(action="export", env="staging")

    assert audits[0][0] == ("test_fixtures", None, "export")
    payload = audits[0][1]["after"]
    assert payload["environment"] == "staging"
    assert payload["count"] == 2
    assert payload["names"] == ["biz_uuid", "staff_jwt"]
    assert "eyJ.top.secret" not in str(payload)


def test_test_fixtures_create_rejects_bad_name():
    out = tg.test_fixtures(action="create", name="Bad Name!", kind="string",
                           value="x")
    assert "error" in out


def test_test_fixtures_create_rejects_bad_env():
    out = tg.test_fixtures(action="create", name="staff_jwt", kind="string",
                           value="x", env="bogus")
    assert "error" in out


def test_test_fixtures_delete_confirm_gated(monkeypatch):
    _fake_env_client(monkeypatch, {"test_fixtures": [dict(FIXTURE_JWT)]})

    out = tg.test_fixtures(action="delete", fixture_id="f1")

    assert out["confirmed"] is False


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------


def test_jobs_cancel_passes_through(monkeypatch):
    import admin._jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "cancel_job", lambda job_id: True)
    audits = []
    monkeypatch.setattr(tg, "actor_audit", lambda *a, **k: audits.append((a, k)))

    out = tg.jobs(action="cancel", job_id="j1")

    assert out == {"cancel_applied": True, "job_id": "j1"}
    assert audits[0][0] == ("admin_jobs", "j1", "cancel")


def test_jobs_cancel_already_finished_returns_false(monkeypatch):
    import admin._jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "cancel_job", lambda job_id: False)
    monkeypatch.setattr(tg, "actor_audit", lambda *a, **k: None)

    out = tg.jobs(action="cancel", job_id="j1")

    assert out["cancel_applied"] is False


def test_jobs_list_passes_filters(monkeypatch):
    import admin._jobs as jobs_mod
    seen = {}

    def fake_list_recent_jobs(*, limit, status, kind):
        seen.update(limit=limit, status=status, kind=kind)
        return [{"job_id": "j1", "status": "running"}]

    monkeypatch.setattr(jobs_mod, "list_recent_jobs", fake_list_recent_jobs)

    out = tg.jobs(action="list", status="running", kind="eval_run", limit=10)

    assert out["count"] == 1
    assert seen == {"limit": 10, "status": "running", "kind": "eval_run"}
