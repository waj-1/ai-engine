"""Skills portal handlers — CRUD, versioning, validation, cache invalidation."""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def app_client():
    from admin import skill_handlers  # noqa: F401 — registers routes
    from admin.auth import require_superuser
    from admin.router import router

    app = FastAPI()
    app.include_router(router)  # router already carries prefix="/admin"
    app.dependency_overrides[require_superuser] = lambda: {"user_id": "admin-1"}
    return TestClient(app, follow_redirects=False)


class _Store:
    """Minimal in-memory PostgREST double shared across tables."""

    def __init__(self):
        self.tables: Dict[str, List[dict]] = {
            "agent_skills": [], "agent_skill_versions": [],
            "agent_skill_resources": [], "agent_profile_skills": [],
            "agent_data_sources": [],
        }

    def table(self, name):
        store = self

        class _Q:
            def __init__(self):
                self._f = []
                self._insert = None
                self._update = None
                self._delete = False

            def select(self, *_a, **_k):
                return self

            def order(self, *_a, **_k):
                return self

            def limit(self, *_a, **_k):
                return self

            def eq(self, c, v):
                self._f.append((c, v))
                return self

            def in_(self, c, vs):
                self._f.append((c, list(vs)))
                return self

            def insert(self, payload):
                self._insert = payload
                return self

            def update(self, payload):
                self._update = payload
                return self

            def delete(self):
                self._delete = True
                return self

            def execute(self):
                rows = store.tables[name]

                def _match(r):
                    for c, v in self._f:
                        if isinstance(v, list):
                            if r.get(c) not in v:
                                return False
                        elif r.get(c) != v:
                            return False
                    return True

                class _R:
                    data: Any = None

                if self._insert is not None:
                    payload = dict(self._insert)
                    payload.setdefault(
                        {"agent_skills": "skill_id",
                         "agent_skill_versions": "skill_version_id",
                         "agent_skill_resources": "resource_id"}.get(name, "id"),
                        f"{name}-{len(rows) + 1}",
                    )
                    rows.append(payload)
                    _R.data = [payload]
                elif self._update is not None:
                    _R.data = []
                    for r in rows:
                        if _match(r):
                            r.update(self._update)
                            _R.data.append(r)
                elif self._delete:
                    keep = [r for r in rows if not _match(r)]
                    _R.data = [r for r in rows if _match(r)]
                    store.tables[name] = keep
                else:
                    _R.data = [r for r in rows if _match(r)]
                return _R()

        return _Q()


@pytest.fixture()
def store():
    return _Store()


@pytest.fixture(autouse=True)
def _patch_clients(store):
    with patch("admin.skill_handlers.admin_service_client", return_value=store), \
         patch("admin.skill_handlers.record_change"):
        yield


def test_create_skill_and_first_version(app_client, store):
    resp = app_client.post("/admin/skills/create", data={
        "slug": "refund-note", "display_name": "Refund note",
        "description": "format a visit summary note", "agent_kind": "support_bot",
        "content": "The body.",
    })
    assert resp.status_code == 302
    assert len(store.tables["agent_skills"]) == 1
    assert store.tables["agent_skill_versions"][0]["version_number"] == 1


def test_create_rejects_braces_in_description(app_client, store):
    resp = app_client.post("/admin/skills/create", data={
        "slug": "bad", "display_name": "Bad", "description": "has {brace}",
        "agent_kind": "support_bot", "content": "x",
    })
    assert resp.status_code == 302 and "error=" in resp.headers["location"]
    assert store.tables["agent_skills"] == []


def test_create_rejects_oversize_body(app_client, store):
    from core.skills import MAX_BODY_BYTES

    resp = app_client.post("/admin/skills/create", data={
        "slug": "big", "display_name": "Big", "description": "ok",
        "agent_kind": "support_bot", "content": "x" * (MAX_BODY_BYTES + 1),
    })
    assert resp.status_code == 302 and "error=" in resp.headers["location"]
    assert store.tables["agent_skills"] == []


def test_new_version_increments_and_invalidates_cache(app_client, store):
    store.tables["agent_skills"].append(
        {"skill_id": "s1", "slug": "refund-note", "display_name": "Refund",
         "description": "d", "agent_kind": "support_bot", "business_id": None,
         "always_on": False, "is_active": True, "status": "active",
         "linked_data_source_ids": []})
    store.tables["agent_skill_versions"].append(
        {"skill_version_id": "v1", "skill_id": "s1", "version_number": 3,
         "content": "old"})
    with patch("admin.skill_handlers.invalidate_skills_cache") as inv:
        resp = app_client.post("/admin/skills/s1/versions/create",
                               data={"content": "new body", "notes": "tweak"})
    assert resp.status_code == 302
    nums = [v["version_number"] for v in store.tables["agent_skill_versions"]]
    assert 4 in nums
    inv.assert_called_once()


def test_archive_soft_deletes(app_client, store):
    store.tables["agent_skills"].append(
        {"skill_id": "s1", "slug": "x", "display_name": "X", "description": "d",
         "agent_kind": "support_bot", "business_id": None, "always_on": False,
         "is_active": True, "status": "active", "linked_data_source_ids": []})
    resp = app_client.post("/admin/skills/s1/archive")
    assert resp.status_code == 302
    row = store.tables["agent_skills"][0]
    assert row["status"] == "archived" and row["is_active"] is False
