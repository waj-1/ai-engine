"""Tests for folding the Test Fixtures vault into the Tests screen.

Covers the changes that move /admin/test-fixtures into /admin/tests as an
`id="fixtures"` section, mirroring how env-vars folded into /admin/env:

  GET  /admin/test-fixtures   — 302-redirects to /admin/tests#fixtures.
  fetch_test_fixtures(client)  — returns the masked rows (jwt/secret kinds
                                show the placeholder unless ?reveal matches),
                                with the _hidden/_revealed/_display_value
                                annotations the section template reads.
  tests.html fixtures section  — renders stubbed test_fixtures rows, masks
                                hidden values, and exposes the create form +
                                Download .env.test button.

Uses the same TestClient + auth-override + fake-supabase pattern as
test_env_consolidation.py / test_channels.py.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch, MagicMock

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


# ---------------------------------------------------------------------------
# Minimal scriptable supabase fake (subset of the test_channels.py shape)
# ---------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        result = MagicMock()
        result.data = list(self._rows)
        return result


class _FakeClient:
    def __init__(self, rows_by_table=None):
        self._rows_by_table = rows_by_table or {}

    def table(self, name):
        return _FakeQuery(self._rows_by_table.get(name, []))


def _make_client(fake_db=None):
    """Boot the app with require_superuser bypassed. Patches the supabase
    client in test_fixtures_handlers so the legacy GET/redirect path works
    without a real Supabase instance."""
    from fastapi.testclient import TestClient
    from admin import auth, router, test_fixtures_handlers
    from main import app

    fake_db = fake_db or _FakeClient()

    def _fake_superuser():
        return {"user_token": "jwt", "user_id": "u-1", "email": "t@waj.local"}

    prior = app.dependency_overrides.get(auth.require_superuser)
    app.dependency_overrides[auth.require_superuser] = _fake_superuser

    patches = [
        patch.object(router, "get_service_client", return_value=fake_db),
        patch.object(test_fixtures_handlers, "admin_service_client", return_value=fake_db),
        patch.object(test_fixtures_handlers, "client_for", return_value=fake_db),
    ]
    for p in patches:
        p.start()

    client = TestClient(app)

    def _teardown():
        for p in patches:
            p.stop()
        if prior is not None:
            app.dependency_overrides[auth.require_superuser] = prior
        else:
            app.dependency_overrides.pop(auth.require_superuser, None)

    return client, _teardown


# ---------------------------------------------------------------------------
# /admin/test-fixtures → /admin/tests#fixtures redirect
# ---------------------------------------------------------------------------


def test_test_fixtures_list_redirects_to_tests():
    client, teardown = _make_client()
    try:
        r = client.get("/admin/test-fixtures", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/admin/tests#fixtures"
    finally:
        teardown()


# ---------------------------------------------------------------------------
# fetch_test_fixtures returns masked rows
# ---------------------------------------------------------------------------


def test_fetch_test_fixtures_masks_hidden_kinds():
    from admin.test_fixtures_handlers import fetch_test_fixtures

    rows = [
        {"fixture_id": "fx-jwt", "name": "partner_jwt", "kind": "jwt",
         "value": "eyJ-super-secret-token", "environment": "dev"},
        {"fixture_id": "fx-uuid", "name": "test_business_id", "kind": "uuid",
         "value": "11111111-2222-3333", "environment": "dev"},
    ]
    fake = _FakeClient(rows_by_table={"test_fixtures": rows})

    out = fetch_test_fixtures(fake, "dev")
    by_id = {r["fixture_id"]: r for r in out}

    # jwt kind masked, not revealed by default
    assert by_id["fx-jwt"]["_hidden"] is True
    assert by_id["fx-jwt"]["_revealed"] is False
    assert by_id["fx-jwt"]["_display_value"] == "••••"

    # non-hidden kind shows its real value
    assert by_id["fx-uuid"]["_hidden"] is False
    assert by_id["fx-uuid"]["_revealed"] is True
    assert by_id["fx-uuid"]["_display_value"] == "11111111-2222-3333"


def test_fetch_test_fixtures_reveal_unmasks_matching_row():
    from admin.test_fixtures_handlers import fetch_test_fixtures

    rows = [
        {"fixture_id": "fx-jwt", "name": "partner_jwt", "kind": "jwt",
         "value": "eyJ-super-secret-token", "environment": "dev"},
    ]
    fake = _FakeClient(rows_by_table={"test_fixtures": rows})

    out = fetch_test_fixtures(fake, "dev", reveal="fx-jwt")
    assert out[0]["_revealed"] is True
    assert out[0]["_display_value"] == "eyJ-super-secret-token"


# ---------------------------------------------------------------------------
# tests.html renders the fixtures section with stubbed rows
# ---------------------------------------------------------------------------


def test_tests_screen_renders_fixtures_section_masked():
    from admin.test_fixtures_handlers import fetch_test_fixtures
    from admin.router import templates

    rows = [
        {"fixture_id": "fx-jwt", "name": "partner_jwt", "kind": "jwt",
         "value": "eyJ-super-secret-token", "environment": "dev",
         "description": "partner tenant jwt", "last_used_at": None},
        {"fixture_id": "fx-uuid", "name": "test_business_id", "kind": "uuid",
         "value": "11111111-2222-3333", "environment": "dev",
         "description": None, "last_used_at": None},
    ]
    fake = _FakeClient(rows_by_table={"test_fixtures": rows})
    test_fixtures = fetch_test_fixtures(fake, "dev")

    html = templates.get_template("tests.html").render(
        ctx={"email": "t@waj.local"},
        active="tests",
        current_env="dev",
        suites=[],
        runs=[],
        test_fixtures=test_fixtures,
        fixtures_export_url="/admin/test-fixtures/export?env=dev",
        kinds=("jwt", "uuid", "string", "json", "secret"),
        environments=("dev", "staging", "prod"),
    )

    # Section anchor + heading present
    assert 'id="fixtures"' in html
    assert "Test fixtures" in html
    # Create form + export button
    assert 'action="/admin/test-fixtures"' in html
    assert "Download .env.test" in html
    assert "/admin/test-fixtures/export?env=dev" in html
    # Visible env-var name + the non-hidden value render
    assert "WAJ_TEST_PARTNER_JWT" in html
    assert "11111111-2222-3333" in html
    # The jwt row is masked in its inline display: the placeholder shows and
    # the row is not revealed, so the hidden copy-textarea (only rendered for
    # revealed rows) is absent for it. (The Edit form still carries the raw
    # value in a name="value" textarea — same as the original screen.)
    assert "••••" in html
    assert 'id="fx-val-fx-jwt"' not in html
    # The non-hidden uuid row IS revealed, so its copy-textarea is present.
    assert 'id="fx-val-fx-uuid"' in html
    # Per-row reveal link is anchored back to the fixtures section
    assert "reveal=fx-jwt" in html
    assert "#fixtures" in html


def test_tests_screen_renders_without_fixtures_context():
    """tests.html must not error when test_fixtures is absent from context
    (the GET handler wiring is a pending TODO — see
    admin/_fixtures_into_tests.todo.md). It should fall back to empty."""
    client, teardown = _make_client(_FakeClient(rows_by_table={"agent_test_runs": []}))
    try:
        r = client.get("/admin/tests")
        assert r.status_code == 200
        assert "Test fixtures" in r.text
        assert "No fixtures yet" in r.text
    finally:
        teardown()


# ---------------------------------------------------------------------------
# Export audits the bulk reveal
# ---------------------------------------------------------------------------


async def test_export_audits_bulk_reveal(monkeypatch):
    """Downloading .env.test reveals every stored secret at once — the route
    must leave an audit trace, carrying names only, never the values."""
    from admin import test_fixtures_handlers as h

    fake_db = _FakeClient(rows_by_table={"test_fixtures": [
        {"name": "staff_jwt", "value": "eyJ.top.secret"},
        {"name": "biz_uuid", "value": "b-1"},
    ]})
    monkeypatch.setattr(h, "client_for", lambda env: fake_db)
    audits = []
    monkeypatch.setattr(h, "record_change", lambda **k: audits.append(k))

    resp = await h.test_fixtures_export(
        env="staging", ctx={"user_id": "u-1", "email": "t@waj.local"})

    assert resp.status_code == 200
    assert "WAJ_TEST_STAFF_JWT=eyJ.top.secret" in resp.body.decode()
    assert audits and audits[0]["action"] == "export"
    assert audits[0]["actor_user_id"] == "u-1"
    payload = audits[0]["after"]
    assert payload["count"] == 2
    assert payload["names"] == ["biz_uuid", "staff_jwt"]
    assert "eyJ.top.secret" not in str(payload)
