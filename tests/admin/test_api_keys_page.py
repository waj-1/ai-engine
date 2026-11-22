"""Settings > API keys (`/admin/api-keys`): the list, the create form that
shows a key exactly once, revoke, and where the screen sits in the nav.
"""
from __future__ import annotations

import re

import pytest

from admin.services import api_keys as service
from core import api_keys as keys
from tests.api_keys_fake import install

PAGE = "/admin/api-keys"
ME = "00000000-0000-0000-0000-000000000001"  # the conftest's signed-in admin
KEY_IN_HTML = re.compile(r"wae_[A-Za-z0-9_-]{43}")


@pytest.fixture
def store(monkeypatch):
    return install(monkeypatch)


def _flat(html: str) -> str:
    return " ".join(html.split())


def test_empty_state_and_form(client, store):
    r = client.get(PAGE)
    assert r.status_code == 200
    assert "No API keys yet" in r.text
    assert 'action="/admin/api-keys"' in r.text and 'name="access"' in r.text
    assert r.headers.get("cache-control") == "no-store"


def test_create_shows_the_key_once_with_a_copy_button_and_a_warning(client, store):
    r = client.post(PAGE, data={"name": "Website widget", "access": "chat", "expires": ""})
    assert r.status_code == 200
    (key,) = set(KEY_IN_HTML.findall(r.text))
    text = _flat(r.text)
    assert "Copy this key now. You will not see it again." in text
    assert 'id="new-api-key-copy"' in r.text and "navigator.clipboard" in r.text
    # The response that carries the key must never be stored by the browser.
    assert r.headers.get("cache-control") == "no-store"
    # It is the working key, stored as a digest only.
    assert keys.verify_key(key)["name"] == "Website widget"
    assert store.rows[0]["scopes"] == ["chat"] and store.rows[0]["created_by"] == ME
    assert all(key not in str(v) for v in store.rows[0].values())

    # Any later view of the page has the prefix and nothing more.
    again = client.get(PAGE)
    assert KEY_IN_HTML.findall(again.text) == []
    assert key[:12] in again.text and "Website widget" in again.text
    assert "You will not see it again" not in again.text
    assert keys.hash_key(key) not in r.text + again.text


def test_create_answers_with_a_page_not_a_redirect_that_would_carry_the_key(client, store):
    r = client.post(PAGE, data={"name": "Website widget", "access": "chat"}, follow_redirects=False)
    assert r.status_code == 200 and "location" not in r.headers
    assert "set-cookie" not in r.headers


def test_full_access_stores_both_scopes_and_an_expiry(client, store):
    r = client.post(PAGE, data={"name": "Ops", "access": "admin", "expires": "30"})
    assert r.status_code == 200
    row = store.rows[0]
    assert row["scopes"] == ["chat", "admin"] and row["expires_at"]
    assert "Admin" in r.text and "until " in r.text


@pytest.mark.parametrize("data,message", [
    ({"name": "  ", "access": "chat"}, "Give the key a name"),
    ({"name": "x", "access": "root"}, "Choose one of the listed options"),
    ({"name": "x", "access": "chat", "expires": "9999"}, "Choose one of the listed options"),
])
def test_create_refuses_bad_input_and_makes_no_key(client, store, data, message):
    r = client.post(PAGE, data=data)
    assert r.status_code == 400 and message in r.text
    assert store.rows == [] and KEY_IN_HTML.findall(r.text) == []


def test_the_list_shows_name_prefix_access_use_and_status(client, store):
    live, live_key = service.create_key("Website widget", ["chat"], created_by=ME)
    gone, _ = service.create_key("Old script", ["chat", "admin"], created_by=ME)
    service.revoke_key(gone["id"], revoked_by=ME)
    store.row(live["id"])["last_used_at"] = "2026-01-01T00:00:00+00:00"
    text = _flat(client.get(PAGE).text)
    for needle in ("Website widget", "Old script", live_key[:12], "Active", "Revoked", "Never"):
        assert needle in text
    # A revoked key has no Revoke button; a live one does.
    assert f'action="/admin/api-keys/{live["id"]}/revoke"' in text
    assert f'action="/admin/api-keys/{gone["id"]}/revoke"' not in text
    assert "key_hash" not in text and keys.hash_key(live_key) not in text


def test_revoke_stops_the_key_and_is_audited_to_the_signed_in_admin(client, store):
    row, key = service.create_key("Website widget", ["chat"], created_by=ME)
    r = client.post(f"{PAGE}/{row['id']}/revoke", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/admin/api-keys?revoked=1"
    with pytest.raises(Exception):
        keys.verify_key(key)
    assert [(a["action"], a["actor_user_id"]) for a in store.audit] == [("create", ME), ("revoke", ME)]
    assert "The key was revoked" in client.get(r.headers["location"]).text


def test_portal_create_is_audited_without_the_key(client, store):
    r = client.post(PAGE, data={"name": "Website widget", "access": "chat"})
    (key,) = set(KEY_IN_HTML.findall(r.text))
    (entry,) = store.audit
    assert (entry["entity_type"], entry["action"], entry["actor_user_id"]) == ("api_keys", "create", ME)
    assert key not in repr(entry) and keys.hash_key(key) not in repr(entry)


def test_revoking_an_unknown_key_reports_it(client, store):
    r = client.post(f"{PAGE}/11111111-1111-1111-1111-111111111111/revoke", follow_redirects=False)
    assert r.status_code == 302 and "error=" in r.headers["location"]
    assert r.headers["location"].startswith("/admin/api-keys?")


def test_a_load_failure_is_an_error_state_not_a_500(client, store):
    store.fail = RuntimeError("database is down")
    r = client.get(PAGE)
    assert r.status_code == 200 and "Couldn't load the API keys" in _flat(r.text).replace("&#39;", "'")


def test_the_screen_sits_under_settings(client, store):
    from admin import nav

    assert nav.section_for(PAGE) == "settings"
    tabs = nav.section_tabs_for(PAGE)
    assert tabs["current"] == "api_keys" and any(t.href == PAGE for t in tabs["tabs"])
    hub = client.get("/admin/settings")
    assert "API keys" in hub.text and f'href="{PAGE}"' in hub.text


def test_a_partner_build_does_not_list_the_screen(client, store, monkeypatch):
    """The chat API a key opens is not served on a scoped build, so Settings
    does not offer keys there (same rule as the other staff-only entries)."""
    from admin import nav
    from tests.scoped_build import use_agents_demo

    use_agents_demo(monkeypatch)
    assert all(t.key != "api_keys" for t in nav.visible_tabs("settings"))
    assert all(e.key != "api_keys" for e in nav.settings_entries())


def test_writes_need_a_signed_in_admin(store):
    """No dependency override here: the real gate must refuse an anonymous POST."""
    from fastapi.testclient import TestClient

    from admin import auth
    from main import app

    saved = app.dependency_overrides.pop(auth.require_superuser, None)
    try:
        anon = TestClient(app)
        assert anon.get(PAGE).status_code == 401
        assert anon.post(PAGE, data={"name": "x", "access": "admin"}).status_code == 401
        assert anon.post(f"{PAGE}/11111111-1111-1111-1111-111111111111/revoke").status_code == 401
        # An API key is not a portal session, whatever its scope.
        _, key = service.create_key("Ops", ["admin"], created_by=ME)
        r = anon.get(PAGE, headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 401
    finally:
        if saved is not None:
            app.dependency_overrides[auth.require_superuser] = saved
    assert [r["name"] for r in store.rows] == ["Ops"]


def test_activity_names_a_key_that_made_a_change():
    from admin.screens import govern
    from tests.api_keys_fake import FakeKeyStore

    db = FakeKeyStore()
    db.tables["api_keys"].append({"id": "22222222-2222-2222-2222-222222222222", "name": "CI runner",
                                  "key_prefix": "wae_abcd1234", "key_hash": "x" * 64})
    people = govern.resolve_people(None, ["22222222-2222-2222-2222-222222222222",
                                          "33333333-3333-3333-3333-333333333333"])
    govern.label_api_key_actors(people, db)
    assert people["22222222-2222-2222-2222-222222222222"]["label"] == "API key: CI runner"
    assert people["33333333-3333-3333-3333-333333333333"]["label"].startswith("Unknown user")
    assert "key_hash" not in db.queries[-1]["columns"]
