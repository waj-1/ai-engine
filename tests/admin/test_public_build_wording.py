"""Screens of a build with no product plugin: neutral wording, nothing that
only exists in one deployment (2026-11-23, found on a fresh public install).

- Team & access: no "WAJ app(s)" wording; the admin who signed up on the
  first-run page is a person, not a hidden service account.
- Connections › Channels › New channel: neutral example name / code name,
  and a type preselected from the kinds this build offers.
- Ctrl K search: no business source (the engine has no business table), so
  nothing to fail and no "Businesses couldn't be searched" note.

A product build's own wording comes from its plugin hooks; the plugin's
tests pin that it is unchanged (tests/admin/test_waj_portal_wording.py).
"""
from __future__ import annotations

import html as _html
from unittest.mock import MagicMock

import pytest

ME = "00000000-0000-0000-0000-000000000001"  # the conftest's signed-in admin


@pytest.fixture
def no_plugin(monkeypatch):
    monkeypatch.setattr("engine_plugins.PLUGINS", ())


def _users_db(monkeypatch, rows):
    result = MagicMock()
    result.data = rows
    builder = MagicMock()
    builder.execute.return_value = result
    for method in ("select", "eq", "order", "limit", "in_"):
        getattr(builder, method).return_value = builder
    fake = MagicMock()
    fake.table.return_value = builder
    fake.auth.admin.get_user_by_id.side_effect = RuntimeError("no auth service in tests")
    monkeypatch.setattr("admin.router.get_main_client", lambda: fake)


def _admin_row(user_id=ME, email="admin@example.com"):
    # What core/admin_account.py writes for the first-run admin.
    return {"id": user_id, "auth_id": "a-1", "email": email, "phone_number": None,
            "country_code": None, "role": "admin", "permission_level": None,
            "permission": {"is_superuser": True}, "business_id": None,
            "created_at": "2026-11-23T08:00:00"}


# --------------------------------------------------------------------------
# Team & access
# --------------------------------------------------------------------------


def test_the_first_run_admin_is_listed_as_a_person(client, monkeypatch, no_plugin):
    """admin@example.com on the first-run page: shown with the default
    filters, not behind "Show service accounts (1)"."""
    _users_db(monkeypatch, [_admin_row()])
    html = client.get("/admin/users").text
    assert "No people found." not in html
    assert "admin@example.com" in html and "(you)" in html
    assert "Show service accounts" not in html
    assert "service account" not in html


def test_another_example_com_admin_is_a_person_too(client, monkeypatch, no_plugin):
    _users_db(monkeypatch, [_admin_row(), _admin_row("11111111-2222-3333-4444-555555555555",
                                                     "second@example.org")])
    html = client.get("/admin/users").text
    assert "second@example.org" in html and "Show service accounts" not in html


def test_test_accounts_are_still_hidden(client, monkeypatch, no_plugin):
    rows = [_admin_row(),
            {**_admin_row("22222222-2222-2222-2222-222222222222", "bot@example.com"),
             "permission": {"is_superuser": False}, "role": "owner"},
            {**_admin_row("33333333-3333-3333-3333-333333333333", "slot@e2e.test")}]
    _users_db(monkeypatch, rows)
    html = client.get("/admin/users").text
    assert "bot@example.com" not in html and "slot@e2e.test" not in html
    assert "Show service accounts (2)" in html


def test_team_wording_names_no_product_app(client, monkeypatch, no_plugin):
    _users_db(monkeypatch, [_admin_row()])
    html = _html.unescape(client.get("/admin/users").text)
    assert "WAJ app" not in html
    assert "admin in the" not in html
    assert "Everyone else listed here can't open the portal." in html


# --------------------------------------------------------------------------
# Channels › New channel
# --------------------------------------------------------------------------


def test_the_engine_s_example_channel_names_no_product(no_plugin):
    from core.channel_kinds import channel_name_example

    assert channel_name_example() == ("Website chat", "website_chat")


def test_the_new_channel_form_shows_the_build_s_example(client, monkeypatch):
    from admin.router import templates

    monkeypatch.setitem(templates.env.globals, "channel_name_example", ("Website chat", "website_chat"))
    html = client.get("/admin/channels/new").text
    assert 'placeholder="Website chat"' in html and 'placeholder="website_chat"' in html
    assert 'placeholder="WAJ App"' not in html and 'placeholder="waj_app"' not in html


def test_the_new_channel_form_preselects_a_kind_this_build_offers(client, monkeypatch):
    from admin import channel_handlers

    monkeypatch.setattr(channel_handlers, "_channel_kinds_for_build", lambda: ["public_api", "embed"])
    html = client.get("/admin/channels/new").text
    assert 'value="public_api" checked' in html


# --------------------------------------------------------------------------
# Ctrl K search
# --------------------------------------------------------------------------


def test_the_engine_searches_no_business_table():
    """The business source is the product plugin's (hook ``search``); the
    engine's own people sources are conversations and telemetry only, so a
    build without the plugin neither runs nor mentions it."""
    from admin import search, search_sources

    assert not hasattr(search_sources, "businesses")
    engine_keys = [key for key, _label, fn in search.PEOPLE_SOURCES
                   if fn.__module__.startswith("admin.")]
    assert engine_keys == ["conversations", "telemetry"]
