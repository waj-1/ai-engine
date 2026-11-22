"""Tests for the agent_channels admin CRUD and the core.channels helper.

Covers:
  GET  /admin/channels                       — list
  GET  /admin/channels/new                   — blank form
  POST /admin/channels                       — create (happy + validation)
  GET  /admin/channels/{id}                  — detail form
  POST /admin/channels/{id}                  — update
  POST /admin/channels/{id}/delete           — soft-delete (is_active=false)
  core.channels.get_active_channels()        — shape + caching + invalidate

Uses the same _FakeClient pattern as test_tool_routes.py so we control
what PostgREST "returns" per-table and can inspect last_insert /
last_update payloads.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


# ---------------------------------------------------------------------------
# Scriptable supabase fake — same surface as test_tool_routes.py
# ---------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self, rows: List[Dict[str, Any]], inserted_id: str = "new-ch-id"):
        self._rows = rows
        self._inserted_id = inserted_id
        self.last_select: Optional[str] = None
        self.last_filter: Dict[str, Any] = {}
        self.last_update: Optional[Dict[str, Any]] = None
        self.last_insert: Optional[Dict[str, Any]] = None

    def select(self, cols):
        self.last_select = cols
        return self

    def eq(self, col, val):
        self.last_filter[col] = val
        return self

    def in_(self, col, vals):
        self.last_filter[f"in_{col}"] = list(vals)
        return self

    def limit(self, n):
        return self

    def order(self, *a, **kw):
        return self

    def range(self, *a, **kw):
        return self

    def gte(self, *a, **kw):
        return self

    def insert(self, payload):
        self.last_insert = payload
        return _InsertResult(payload, self._inserted_id)

    def update(self, payload):
        self.last_update = payload
        return self

    def delete(self):
        return self

    def execute(self):
        result = MagicMock()
        result.data = list(self._rows)
        return result


class _InsertResult:
    def __init__(self, payload, inserted_id):
        self._payload = payload
        self._inserted_id = inserted_id

    def execute(self):
        result = MagicMock()
        result.data = [{**self._payload, "channel_id": self._inserted_id}]
        return result


class _FakeClient:
    def __init__(self, rows_by_table=None, inserted_id="new-ch-id"):
        self._rows_by_table = rows_by_table or {}
        self._inserted_id = inserted_id
        self.queries: Dict[str, _FakeQuery] = {}

    def table(self, name):
        if name not in self.queries:
            self.queries[name] = _FakeQuery(
                self._rows_by_table.get(name, []), self._inserted_id
            )
        return self.queries[name]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def waj_app_channel_row():
    return {
        "channel_id": "ch-waj-app",
        "slug": "waj_app",
        "name": "WAJ App",
        "kind": "embed",
        "is_active": True,
        "allowed_origins": ["https://app.company.example"],
        "allowed_agent_kinds": ["sample_agent", "sample_other", "support_bot"],
        "default_profile_id": None,
        "business_id": None,
        "auth_required": True,
        "rate_limit_rpm": 60,
        "config": {},
        "description": "operator dashboard",
    }


def _valid_form() -> Dict[str, Any]:
    """Minimum-valid POST body for /admin/channels create+update."""
    return {
        "slug": "waj_app",
        "name": "WAJ App",
        "kind": "embed",
        "is_active": "on",
        "allowed_origins": "https://app.company.example",
        "allowed_agent_kinds": ["sample_agent", "sample_other"],
        "default_profile_id": "",
        "business_id": "",
        "auth_required": "on",
        "rate_limit_rpm": "60",
        "config": "",
        "description": "",
    }


def _make_app_with_fake_db(fake_client):
    """Boot the FastAPI app with require_superuser bypassed and the
    supabase service client patched in channel_handlers (plus the
    business picker module so its independent fetch goes through the
    same fake client)."""
    from fastapi.testclient import TestClient
    from admin import auth, channel_handlers, _business_picker
    from main import app

    def _fake_superuser():
        return {
            "user_token": "fake-jwt",
            "user_id": "u-1",
            "email": "test@waj.local",
        }

    # Set the auth override, BUT remember its prior value so teardown
    # restores it. The session-scoped smoke fixture in conftest.py also
    # registers an override; popping it unconditionally during teardown
    # breaks every subsequent smoke test that ran after this one
    # (assert 401 == 200). Capture-and-restore is the polite pattern.
    prior_auth_override = app.dependency_overrides.get(auth.require_superuser)
    app.dependency_overrides[auth.require_superuser] = _fake_superuser
    patches = [
        patch.object(channel_handlers, "admin_service_client", return_value=fake_client),
        patch.object(channel_handlers, "client_for", return_value=fake_client),
        patch.object(channel_handlers, "record_change"),
        patch.object(channel_handlers, "_invalidate_channels_cache"),
        # The detail/edit views fetch businesses for the picker — point
        # that helper at the same fake client so it stays scripted.
        patch.object(_business_picker, "fetch_businesses_for_picker",
                     return_value=[{"id": "biz-1", "name": "Orchard", "subdomain": "orchard"}]),
    ]
    for p in patches:
        p.start()

    client = TestClient(app)

    def _teardown():
        for p in patches:
            p.stop()
        if prior_auth_override is not None:
            app.dependency_overrides[auth.require_superuser] = prior_auth_override
        else:
            app.dependency_overrides.pop(auth.require_superuser, None)

    return client, _teardown


# ---------------------------------------------------------------------------
# GET /admin/channels — list view renders
# ---------------------------------------------------------------------------


def test_list_renders_rows(waj_app_channel_row):
    fake = _FakeClient(rows_by_table={"agent_channels": [waj_app_channel_row]})
    client, teardown = _make_app_with_fake_db(fake)
    try:
        r = client.get("/admin/channels")
        assert r.status_code == 200
        assert "waj_app" in r.text
        assert "WAJ App" in r.text
    finally:
        teardown()


def test_list_with_kind_filter(waj_app_channel_row):
    fake = _FakeClient(rows_by_table={"agent_channels": [waj_app_channel_row]})
    client, teardown = _make_app_with_fake_db(fake)
    try:
        r = client.get("/admin/channels?kind=embed")
        assert r.status_code == 200
        # eq() should have been called for the kind filter
        q = fake.queries["agent_channels"]
        assert q.last_filter.get("kind") == "embed"
    finally:
        teardown()


# ---------------------------------------------------------------------------
# GET /admin/channels/new — blank create form
# ---------------------------------------------------------------------------


def test_new_form_renders():
    fake = _FakeClient()
    client, teardown = _make_app_with_fake_db(fake)
    try:
        r = client.get("/admin/channels/new")
        assert r.status_code == 200
        assert 'name="slug"' in r.text
        assert 'name="allowed_origins"' in r.text
        assert 'name="rate_limit_rpm"' in r.text
    finally:
        teardown()


# ---------------------------------------------------------------------------
# POST /admin/channels — happy path inserts + redirects
# ---------------------------------------------------------------------------


def test_create_valid_inserts_and_redirects(product_build):
    fake = _FakeClient(inserted_id="abc-123")
    client, teardown = _make_app_with_fake_db(fake)
    try:
        r = client.post("/admin/channels", data=_valid_form(), follow_redirects=False)
        assert r.status_code == 303, r.text[:400]
        assert r.headers["location"] == "/admin/channels/abc-123"

        payload = fake.queries["agent_channels"].last_insert
        assert payload is not None
        assert payload["slug"] == "waj_app"
        assert payload["kind"] == "embed"
        assert payload["allowed_origins"] == ["https://app.company.example"]
        assert set(payload["allowed_agent_kinds"]) == {"sample_agent", "sample_other"}
        assert payload["rate_limit_rpm"] == 60
        assert payload["is_active"] is True
        assert payload["auth_required"] is True
        assert payload["config"] == {}
        # business_id must be coerced to None for a non-tenant kind.
        assert payload["business_id"] is None
        assert payload["created_by"] == "u-1"
    finally:
        teardown()


# ---------------------------------------------------------------------------
# POST /admin/channels — validation failures
# ---------------------------------------------------------------------------


def test_create_bad_slug_returns_400():
    fake = _FakeClient()
    client, teardown = _make_app_with_fake_db(fake)
    try:
        form = _valid_form()
        form["slug"] = "Bad Slug With Spaces"
        r = client.post("/admin/channels", data=form, follow_redirects=False)
        assert r.status_code == 400
        assert "slug" in r.text.lower()
        # Nothing should have been inserted (table may not have been touched at all).
        q = fake.queries.get("agent_channels")
        assert q is None or q.last_insert is None
    finally:
        teardown()


def test_create_bad_slug_with_trailing_hyphen_is_fine():
    """Sanity-check the slug regex actually allows the second-char-onward
    hyphen set, so future devs don't tighten it accidentally."""
    fake = _FakeClient(inserted_id="abc-x")
    client, teardown = _make_app_with_fake_db(fake)
    try:
        form = _valid_form()
        form["slug"] = "tenant-site-1"
        r = client.post("/admin/channels", data=form, follow_redirects=False)
        assert r.status_code == 303, r.text[:200]
    finally:
        teardown()


def test_create_invalid_origin_returns_400():
    fake = _FakeClient()
    client, teardown = _make_app_with_fake_db(fake)
    try:
        form = _valid_form()
        form["allowed_origins"] = "https://app.company.example/some/path"
        r = client.post("/admin/channels", data=form, follow_redirects=False)
        assert r.status_code == 400
        assert "origin" in r.text.lower()
        q = fake.queries.get("agent_channels")
        assert q is None or q.last_insert is None
    finally:
        teardown()


def test_create_origin_without_scheme_returns_400():
    fake = _FakeClient()
    client, teardown = _make_app_with_fake_db(fake)
    try:
        form = _valid_form()
        form["allowed_origins"] = "app.company.example"
        r = client.post("/admin/channels", data=form, follow_redirects=False)
        assert r.status_code == 400
        q = fake.queries.get("agent_channels")
        assert q is None or q.last_insert is None
    finally:
        teardown()


@pytest.fixture
def tenant_kind(monkeypatch):
    """A channel kind whose channel belongs to one business (a plugin's
    `tenant_channel_kinds`, core/channel_kinds.py)."""
    from admin import channel_handlers

    monkeypatch.setattr(channel_handlers, "_CHANNEL_KINDS",
                        channel_handlers._CHANNEL_KINDS + ("tenant_site",))
    monkeypatch.setattr(channel_handlers, "_TENANT_KINDS", ("tenant_site",))
    monkeypatch.setitem(channel_handlers.CHANNEL_KIND_LABELS, "tenant_site", "Tenant site")
    return "tenant_site"


def test_create_tenant_channel_without_business_returns_400(tenant_kind):
    fake = _FakeClient()
    client, teardown = _make_app_with_fake_db(fake)
    try:
        form = _valid_form()
        form["slug"] = "site_orchard"
        form["kind"] = tenant_kind
        form["business_id"] = ""
        r = client.post("/admin/channels", data=form, follow_redirects=False)
        assert r.status_code == 400
        assert "business_id" in r.text
        q = fake.queries.get("agent_channels")
        assert q is None or q.last_insert is None
    finally:
        teardown()


def test_create_tenant_channel_with_business_succeeds(tenant_kind):
    fake = _FakeClient(inserted_id="site-1")
    client, teardown = _make_app_with_fake_db(fake)
    try:
        form = _valid_form()
        form["slug"] = "site_orchard"
        form["kind"] = tenant_kind
        form["business_id"] = "biz-uuid-1"
        r = client.post("/admin/channels", data=form, follow_redirects=False)
        assert r.status_code == 303
        payload = fake.queries["agent_channels"].last_insert
        assert payload["kind"] == tenant_kind
        assert payload["business_id"] == "biz-uuid-1"
    finally:
        teardown()


def test_business_id_dropped_on_a_non_tenant_kind(tenant_kind):
    fake = _FakeClient(inserted_id="abc-1")
    client, teardown = _make_app_with_fake_db(fake)
    try:
        form = _valid_form()
        form["business_id"] = "biz-uuid-1"
        r = client.post("/admin/channels", data=form, follow_redirects=False)
        assert r.status_code == 303
        assert fake.queries["agent_channels"].last_insert["business_id"] is None
    finally:
        teardown()


def test_create_bad_rate_limit_returns_400():
    fake = _FakeClient()
    client, teardown = _make_app_with_fake_db(fake)
    try:
        form = _valid_form()
        form["rate_limit_rpm"] = "999999"
        r = client.post("/admin/channels", data=form, follow_redirects=False)
        assert r.status_code == 400
        assert "rate_limit" in r.text.lower()
    finally:
        teardown()


def test_create_invalid_config_json_returns_400():
    fake = _FakeClient()
    client, teardown = _make_app_with_fake_db(fake)
    try:
        form = _valid_form()
        form["config"] = "{not valid"
        r = client.post("/admin/channels", data=form, follow_redirects=False)
        assert r.status_code == 400
        assert "json" in r.text.lower()
    finally:
        teardown()


# ---------------------------------------------------------------------------
# GET /admin/channels/{id} — detail
# ---------------------------------------------------------------------------


def test_detail_renders_existing(waj_app_channel_row):
    fake = _FakeClient(rows_by_table={"agent_channels": [waj_app_channel_row]})
    client, teardown = _make_app_with_fake_db(fake)
    try:
        r = client.get(f"/admin/channels/{waj_app_channel_row['channel_id']}")
        assert r.status_code == 200
        assert "waj_app" in r.text
        # The textarea is pre-populated with the origin
        assert "https://app.company.example" in r.text
    finally:
        teardown()


def test_detail_404_when_missing():
    fake = _FakeClient(rows_by_table={"agent_channels": []})
    client, teardown = _make_app_with_fake_db(fake)
    try:
        r = client.get("/admin/channels/does-not-exist")
        assert r.status_code == 404
    finally:
        teardown()


# ---------------------------------------------------------------------------
# POST /admin/channels/{id} — update
# ---------------------------------------------------------------------------


def test_update_modifies_row(waj_app_channel_row):
    fake = _FakeClient(rows_by_table={"agent_channels": [waj_app_channel_row]})
    client, teardown = _make_app_with_fake_db(fake)
    try:
        form = _valid_form()
        form["name"] = "WAJ App — updated"
        form["rate_limit_rpm"] = "120"
        r = client.post(
            f"/admin/channels/{waj_app_channel_row['channel_id']}",
            data=form,
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text[:400]
        update = fake.queries["agent_channels"].last_update
        assert update is not None
        assert update["name"] == "WAJ App — updated"
        assert update["rate_limit_rpm"] == 120
    finally:
        teardown()


def test_update_404_when_missing():
    fake = _FakeClient(rows_by_table={"agent_channels": []})
    client, teardown = _make_app_with_fake_db(fake)
    try:
        r = client.post(
            "/admin/channels/missing-id", data=_valid_form(), follow_redirects=False,
        )
        assert r.status_code == 404
    finally:
        teardown()


# ---------------------------------------------------------------------------
# POST /admin/channels/{id}/delete — soft-delete
# ---------------------------------------------------------------------------


def test_delete_marks_inactive(waj_app_channel_row):
    fake = _FakeClient(rows_by_table={"agent_channels": [waj_app_channel_row]})
    client, teardown = _make_app_with_fake_db(fake)
    try:
        r = client.post(
            f"/admin/channels/{waj_app_channel_row['channel_id']}/delete",
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text[:400]
        update = fake.queries["agent_channels"].last_update
        assert update == {"is_active": False}
        assert r.headers["location"] == "/admin/channels"
    finally:
        teardown()


def test_delete_404_when_missing():
    fake = _FakeClient(rows_by_table={"agent_channels": []})
    client, teardown = _make_app_with_fake_db(fake)
    try:
        r = client.post("/admin/channels/missing/delete", follow_redirects=False)
        assert r.status_code == 404
    finally:
        teardown()


# ---------------------------------------------------------------------------
# core.channels — runtime helper shape + caching
# ---------------------------------------------------------------------------


def test_get_active_channels_returns_slug_keyed_map(waj_app_channel_row):
    from core import channels as channels_mod

    fake = _FakeClient(rows_by_table={"agent_channels": [waj_app_channel_row]})
    channels_mod.invalidate()
    with patch.object(channels_mod, "get_service_client", return_value=fake):
        snapshot = channels_mod.get_active_channels()
    try:
        assert "waj_app" in snapshot
        row = snapshot["waj_app"]
        assert row["kind"] == "embed"
        assert row["allowed_origins"] == ["https://app.company.example"]
        assert row["allowed_agent_kinds"] == ["sample_agent", "sample_other", "support_bot"]
        # Defaults when DB column came back None
        assert isinstance(row["config"], dict)
    finally:
        channels_mod.invalidate()


def test_get_active_channels_caches_until_invalidate(waj_app_channel_row):
    from core import channels as channels_mod

    channels_mod.invalidate()
    fake_a = _FakeClient(rows_by_table={"agent_channels": [waj_app_channel_row]})
    fake_b = _FakeClient(rows_by_table={"agent_channels": []})

    with patch.object(channels_mod, "get_service_client", return_value=fake_a):
        first = channels_mod.get_active_channels()
    # Second call should NOT re-query — even though we point at an
    # empty-DB fake, the cache should still serve the original row.
    with patch.object(channels_mod, "get_service_client", return_value=fake_b):
        second = channels_mod.get_active_channels()
    try:
        assert "waj_app" in first
        assert "waj_app" in second  # cache hit
        # After invalidate the empty fake takes over
        channels_mod.invalidate()
        with patch.object(channels_mod, "get_service_client", return_value=fake_b):
            third = channels_mod.get_active_channels()
        assert third == {}
    finally:
        channels_mod.invalidate()


def test_get_channel_returns_copy(waj_app_channel_row):
    from core import channels as channels_mod

    channels_mod.invalidate()
    fake = _FakeClient(rows_by_table={"agent_channels": [waj_app_channel_row]})
    with patch.object(channels_mod, "get_service_client", return_value=fake):
        row = channels_mod.get_channel("waj_app")
        missing = channels_mod.get_channel("does-not-exist")
    try:
        assert row is not None and row["slug"] == "waj_app"
        assert missing is None
        # Mutating the returned dict must not poison the cache.
        row["slug"] = "mutated"
        with patch.object(channels_mod, "get_service_client", return_value=fake):
            again = channels_mod.get_channel("waj_app")
        assert again["slug"] == "waj_app"
    finally:
        channels_mod.invalidate()
