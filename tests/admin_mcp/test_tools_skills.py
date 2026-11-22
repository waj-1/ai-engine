"""Admin MCP skills tools — action dispatch, validation, audit."""
from __future__ import annotations

import pytest

from tests.admin.test_skill_handlers import _Store


@pytest.fixture()
def store():
    s = _Store()
    s.tables["agent_profiles"] = [
        {"profile_id": "p1", "agent_kind": "support_bot", "environment": "staging"},
    ]
    return s


@pytest.fixture()
def tc(store, monkeypatch):
    import admin_mcp.tools_configure as tc

    monkeypatch.setattr(tc, "client_for_env", lambda env: store)
    monkeypatch.setattr(tc, "actor_audit", lambda *a, **k: None)
    return tc


def test_skills_create_and_list(tc, store):
    out = tc.skills(env="staging", action="create", slug="refund-note",
                    display_name="Refund", description="format a note",
                    agent_kind="support_bot", content="body")
    assert out.get("skill", {}).get("slug") == "refund-note"
    assert store.tables["agent_skill_versions"][0]["version_number"] == 1
    listed = tc.skills(env="staging", action="list")
    assert any(s["slug"] == "refund-note" for s in listed["skills"])


def test_skills_create_validation_error(tc, store):
    out = tc.skills(env="staging", action="create", slug="Bad Slug",
                    display_name="x", description="d",
                    agent_kind="support_bot", content="body")
    assert "error" in out
    assert store.tables["agent_skills"] == []


def test_skills_create_version_increments(tc, store):
    store.tables["agent_skills"].append(
        {"skill_id": "s1", "slug": "refund-note", "display_name": "Refund",
         "description": "d", "agent_kind": "support_bot", "business_id": None,
         "always_on": False, "is_active": True, "status": "active",
         "linked_data_source_ids": []})
    store.tables["agent_skill_versions"].append(
        {"skill_version_id": "v1", "skill_id": "s1", "version_number": 1,
         "content": "old"})
    out = tc.skills(env="staging", action="create_version",
                    skill_id="s1", content="new")
    assert out["version_number"] == 2


def test_profile_skill_attachments_set_and_list(tc, store):
    store.tables["agent_skills"].append(
        {"skill_id": "s1", "slug": "refund-note", "display_name": "Refund",
         "description": "d", "agent_kind": "support_bot", "business_id": None,
         "always_on": False, "is_active": True, "status": "active",
         "linked_data_source_ids": []})
    out = tc.profile_skill_attachments(
        env="staging", profile_id="p1", action="set", skill_ids=["s1"])
    assert out["attachments"] == ["s1"]
    listed = tc.profile_skill_attachments(
        env="staging", profile_id="p1", action="list")
    assert listed["attachments"][0]["skill_id"] == "s1"


def test_profile_skill_attachments_rejects_tenant_skill(tc, store):
    store.tables["agent_skills"].append(
        {"skill_id": "s2", "slug": "tenant", "display_name": "T",
         "description": "d", "agent_kind": "support_bot", "business_id": "biz1",
         "always_on": False, "is_active": True, "status": "active",
         "linked_data_source_ids": []})
    out = tc.profile_skill_attachments(
        env="staging", profile_id="p1", action="set", skill_ids=["s2"])
    assert "error" in out


def _seed_skill(store, **overrides):
    row = {"skill_id": "s1", "slug": "refund-note", "display_name": "Refund",
           "description": "d", "agent_kind": "support_bot", "business_id": None,
           "always_on": False, "is_active": True, "status": "active",
           "linked_data_source_ids": []}
    row.update(overrides)
    store.tables["agent_skills"].append(row)
    return row


def test_skills_update_archive_matches_portal_fields(tc, store):
    """MCP archive must write the exact same two columns as the portal's
    POST /admin/skills/{id}/archive (admin/skill_handlers.py::skill_archive):
    status="archived" AND is_active=False together."""
    _seed_skill(store)
    out = tc.skills(env="staging", action="update", skill_id="s1",
                    status="archived")
    assert "error" not in out
    row = store.tables["agent_skills"][0]
    assert row["status"] == "archived"
    assert row["is_active"] is False


def test_skills_update_archive_forces_is_active_false_even_if_true_passed(tc, store):
    """The portal route never lets status="archived" ship with is_active
    still True — the MCP path must couple them the same way even if a
    caller explicitly (and incorrectly) passes is_active=True."""
    _seed_skill(store)
    out = tc.skills(env="staging", action="update", skill_id="s1",
                    status="archived", is_active=True)
    assert "error" not in out
    row = store.tables["agent_skills"][0]
    assert row["status"] == "archived"
    assert row["is_active"] is False


def test_skills_update_unarchive(tc, store):
    _seed_skill(store, status="archived", is_active=False)
    out = tc.skills(env="staging", action="update", skill_id="s1",
                    status="active", is_active=True)
    assert "error" not in out
    row = store.tables["agent_skills"][0]
    assert row["status"] == "active"
    assert row["is_active"] is True


def test_skills_update_invalid_status_rejected(tc, store):
    _seed_skill(store)
    out = tc.skills(env="staging", action="update", skill_id="s1",
                    status="deleted")
    assert "error" in out
    row = store.tables["agent_skills"][0]
    assert row["status"] == "active"
    assert row["is_active"] is True


def test_skills_update_without_status_leaves_status_and_is_active_untouched(tc, store):
    """Back-compat: an update that doesn't mention status/is_active must not
    perturb them — a plain metadata edit must not accidentally archive or
    resurrect a skill."""
    _seed_skill(store, status="archived", is_active=False)
    out = tc.skills(env="staging", action="update", skill_id="s1",
                    display_name="Refund note v2")
    assert "error" not in out
    row = store.tables["agent_skills"][0]
    assert row["display_name"] == "Refund note v2"
    assert row["status"] == "archived"
    assert row["is_active"] is False


def test_skills_update_archive_audit_and_cache_invalidation(tc, store, monkeypatch):
    """Archive over MCP must record the same actor_audit entity/action the
    portal route does (agent_skills / "archive") and invalidate both caches
    the portal's skill_archive invalidates: invalidate_skills_cache() AND
    AgentProfileLoader.invalidate() — a silent divergence here is exactly
    the bug class this tool exists to prevent."""
    _seed_skill(store)
    audit_calls = []
    monkeypatch.setattr(tc, "actor_audit",
                        lambda *a, **k: audit_calls.append((a, k)))
    skills_cache_calls = []
    monkeypatch.setattr("core.skills.invalidate_skills_cache",
                        lambda: skills_cache_calls.append(True))
    from core.agent_profile import AgentProfileLoader
    loader_calls = []
    monkeypatch.setattr(AgentProfileLoader, "invalidate",
                        classmethod(lambda cls, *a, **k: loader_calls.append((a, k))))

    out = tc.skills(env="staging", action="update", skill_id="s1",
                    status="archived")

    assert "error" not in out
    assert audit_calls, "actor_audit was not called"
    args, kwargs = audit_calls[0]
    assert args[0] == "agent_skills"
    assert args[1] == "s1"
    assert args[2] == "archive"
    assert skills_cache_calls, "invalidate_skills_cache was not called"
    assert loader_calls, "AgentProfileLoader.invalidate was not called"


def test_skills_update_unarchive_audit_action(tc, store, monkeypatch):
    _seed_skill(store, status="archived", is_active=False)
    audit_calls = []
    monkeypatch.setattr(tc, "actor_audit",
                        lambda *a, **k: audit_calls.append((a, k)))

    out = tc.skills(env="staging", action="update", skill_id="s1",
                    status="active")

    assert "error" not in out
    args, kwargs = audit_calls[0]
    assert args[2] == "unarchive"


def test_skills_update_plain_metadata_edit_audit_action_is_update(tc, store, monkeypatch):
    """A status-less update must keep using the "update" audit action, not
    "archive"/"unarchive" — the action name is a signal of what actually
    changed."""
    _seed_skill(store)
    audit_calls = []
    monkeypatch.setattr(tc, "actor_audit",
                        lambda *a, **k: audit_calls.append((a, k)))

    out = tc.skills(env="staging", action="update", skill_id="s1",
                    display_name="Refund note v2")

    assert "error" not in out
    args, kwargs = audit_calls[0]
    assert args[2] == "update"
