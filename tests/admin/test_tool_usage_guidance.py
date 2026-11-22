"""Tools screen persists usage_guidance on create/edit."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.admin.test_skill_handlers import _Store


@pytest.fixture()
def store():
    s = _Store()
    s.tables["agent_tools"] = [{
        "tool_id": "t1", "python_name": "get_leads", "display_name": "Leads",
        "description": "d", "kind": "http", "scope": "read", "agent_kind": "business",
        "endpoint_url": "https://x", "http_method": "POST", "argument_schema": {},
        "is_deprecated": False, "category": "http", "additional_agent_kinds": [],
        "timeout_seconds": 30, "usage_guidance": None,
    }]
    return s


@pytest.fixture()
def app_client(store):
    from admin import tool_handlers  # noqa: F401
    from admin.auth import require_superuser
    from admin.router import router

    app = FastAPI()
    app.include_router(router)  # router already carries prefix="/admin"
    app.dependency_overrides[require_superuser] = lambda: {"user_id": "admin-1"}
    with patch("admin.tool_handlers.admin_service_client", return_value=store), \
         patch("admin.tool_handlers.record_change"):
        yield TestClient(app, follow_redirects=False)


def test_edit_persists_usage_guidance(app_client, store):
    # NOTE: the brief guessed POST /admin/tools/{id}/edit, but that route
    # (tool_http_edit) only accepts HTTP-endpoint fields and redirects 303.
    # The Metadata form on tool_detail.html (which sits next to the
    # `description` textarea, where this field lives) posts to the
    # catch-all POST /admin/tools/{id} (tool_update), which redirects 302 —
    # that's the real edit path for this field.
    resp = app_client.post("/admin/tools/t1", data={
        "python_name": "get_leads", "display_name": "Leads", "description": "d",
        "endpoint_url": "https://x", "http_method": "POST", "scope": "read",
        "agent_kind": "business", "argument_schema": "{}",
        "usage_guidance": "Active pipeline = stage NOT IN (won, lost).",
    })
    assert resp.status_code == 302
    assert store.tables["agent_tools"][0]["usage_guidance"] == \
        "Active pipeline = stage NOT IN (won, lost)."
