"""Where the STAGING / PRODUCTION switch shows, and that production is never
picked silently (admin/nav.py env_chrome, admin/templates/_base.html).

* Settings pages whose data is the same in both environments show no switch.
* Settings pages that hold per-environment data keep it and name the
  environment on the page.
* Any config screen working on production says so in a banner.
* Picking production is remembered only for the browser session.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.admin.test_voice_budgets_page import FakeSupabase, _tables
from tests.provider_keys_fake import FakeDB

SWITCH = 'class="env-switcher'


@pytest.fixture
def fake_db(monkeypatch):
    voice = FakeSupabase(_tables())
    keys = FakeDB()
    monkeypatch.setenv("SECRET_KEY", "unit-test-secret-key-0123456789")
    monkeypatch.setattr("core.auth.get_service_client", lambda: keys)
    monkeypatch.setattr("core.auth.get_service_client_for_env", lambda env: keys)
    monkeypatch.delenv("STAGING_AI_SUPABASE_URL", raising=False)
    monkeypatch.delenv("PROD_AI_SUPABASE_URL", raising=False)
    with patch("admin.router.client_for", return_value=voice):
        yield


def test_settings_pages_without_per_environment_data_show_no_switch(client, fake_db):
    for page in ("/admin/settings", "/admin/voice-budgets", "/admin/env", "/admin/jobs"):
        r = client.get(page + "?env=prod")
        assert r.status_code == 200, page
        assert SWITCH not in r.text, page
        assert 'data-env-banner' not in r.text, page


def test_model_keys_keep_the_switch_and_name_the_environment(client, fake_db):
    r = client.get("/admin/model-keys?env=prod")
    assert r.status_code == 200
    assert SWITCH in r.text
    assert 'data-env-marker="prod"' in r.text and 'data-env-banner="prod"' in r.text
    r = client.get("/admin/model-keys?env=staging")
    assert 'data-env-marker="staging"' in r.text and "data-env-banner" not in r.text


def test_voice_budgets_keep_the_switch_when_each_environment_has_its_own_database(
        client, fake_db, monkeypatch):
    monkeypatch.setenv("PROD_AI_SUPABASE_URL", "http://prod.invalid")
    r = client.get("/admin/voice-budgets?env=prod")
    assert SWITCH in r.text and 'data-env-marker="prod"' in r.text


def test_a_config_screen_on_production_says_so():
    from admin.nav import env_chrome

    class _Req:
        url = type("U", (), {"path": "/admin/agents"})()

    assert env_chrome(_Req(), "agents") == {"switch": True, "prod_banner": True, "page_marker": False}
    # Operate screens only read: no banner there.
    assert env_chrome(_Req(), "observability")["prod_banner"] is False

    class _Settings:
        url = type("U", (), {"path": "/admin/users"})()

    assert env_chrome(_Settings(), "users")["switch"] is False


def test_production_is_remembered_only_for_the_browser_session(client, fake_db):
    r = client.get("/admin/settings?env=prod")
    cookie = r.headers.get("set-cookie", "")
    assert "waj_admin_env=prod" in cookie and "max-age" not in cookie.lower()
    r = client.get("/admin/settings?env=staging")
    cookie = r.headers.get("set-cookie", "")
    assert "waj_admin_env=staging" in cookie and "max-age=2592000" in cookie.lower()


def test_an_agents_screen_on_production_shows_the_banner(client, fake_db):
    r = client.get("/admin/agents?env=prod")
    assert r.status_code == 200
    assert SWITCH in r.text and 'data-env-banner="prod"' in r.text
    r = client.get("/admin/agents?env=staging")
    assert SWITCH in r.text and "data-env-banner" not in r.text
