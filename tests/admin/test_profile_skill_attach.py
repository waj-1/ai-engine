"""Profile↔skill attachment reconcile — guards, ordering, invalidation."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.admin.test_skill_handlers import _Store


@pytest.fixture()
def store():
    s = _Store()
    s.tables["agent_profiles"] = [
        {"profile_id": "p1", "agent_kind": "support_bot", "environment": "staging"},
    ]
    s.tables["agent_skills"] = [
        {"skill_id": "s1", "slug": "refund-note", "display_name": "Refund",
         "description": "d", "agent_kind": "support_bot", "business_id": None,
         "always_on": False, "is_active": True, "status": "active",
         "linked_data_source_ids": []},
        {"skill_id": "s2", "slug": "tenant-only", "display_name": "T",
         "description": "d", "agent_kind": "support_bot", "business_id": "biz1",
         "always_on": False, "is_active": True, "status": "active",
         "linked_data_source_ids": []},
    ]
    return s


@pytest.fixture()
def app_client(store):
    from admin import profile_skill_attach_handlers  # noqa: F401
    from admin.auth import require_superuser
    from admin.router import router

    app = FastAPI()
    app.include_router(router)  # router already carries prefix="/admin"
    app.dependency_overrides[require_superuser] = lambda: {"user_id": "admin-1"}
    with patch("admin.profile_skill_attach_handlers.admin_service_client",
               return_value=store), \
         patch("admin.profile_skill_attach_handlers.record_change"):
        yield TestClient(app, follow_redirects=False)


def test_attach_platform_skill(app_client, store):
    with patch("admin.profile_skill_attach_handlers.invalidate_skills_cache") as inv:
        resp = app_client.post("/admin/profiles/p1/skill-attachments",
                               data={"attach_skill_ids": ["s1"]})
    assert resp.status_code == 302 and "error=" not in resp.headers["location"]
    rows = store.tables["agent_profile_skills"]
    assert [(r["skill_id"], r["position"]) for r in rows] == [("s1", 0)]
    inv.assert_called_once()


def test_attach_refuses_tenant_skill(app_client, store):
    resp = app_client.post("/admin/profiles/p1/skill-attachments",
                           data={"attach_skill_ids": ["s2"]})
    assert "error=" in resp.headers["location"]
    assert store.tables["agent_profile_skills"] == []


def test_redirect_percent_encodes_profile_id_and_uses_row_env(app_client, store):
    """A path segment can legally contain `&` — the redirect must keep the
    whole profile_id as ONE percent-encoded query value (no smuggled params),
    and the env param must come from the profile ROW, not a cookie."""
    store.tables["agent_profiles"].append(
        {"profile_id": "p&x=1", "agent_kind": "support_bot", "environment": "prod"},
    )
    with patch("admin.profile_skill_attach_handlers.invalidate_skills_cache"):
        resp = app_client.post("/admin/profiles/p&x=1/skill-attachments", data={})
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert "profile_id=p%26x%3D1" in loc, loc
    assert "&x=1" not in loc, loc
    assert "env=prod" in loc, loc


def test_error_redirect_percent_encodes_profile_id(app_client, store):
    store.tables["agent_profiles"].append(
        {"profile_id": "p&x=1", "agent_kind": "support_bot", "environment": "staging"},
    )
    resp = app_client.post("/admin/profiles/p&x=1/skill-attachments",
                           data={"attach_skill_ids": ["s2"]})  # tenant skill -> refused
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert "error=" in loc
    assert "profile_id=p%26x%3D1" in loc, loc
    assert "&x=1" not in loc, loc


def test_reconcile_detaches_removed(app_client, store):
    store.tables["agent_profile_skills"] = [
        {"profile_id": "p1", "skill_id": "s1", "position": 0},
    ]
    resp = app_client.post("/admin/profiles/p1/skill-attachments", data={})
    assert resp.status_code == 302
    assert store.tables["agent_profile_skills"] == []
