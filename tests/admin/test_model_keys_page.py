"""Settings › Model keys (/admin/model-keys) and the service behind it.

What these pin:

1. The page shows, per provider, where its key comes from, and lets an admin
   set, replace and remove a stored key.
2. A stored key is never returned: not by the page, the status JSON, the
   service's return values, the audit rows or the test result. Only its hint.
3. With no SECRET_KEY the portal refuses to store a key, says why, and
   writes nothing.
4. Test makes one call with the key in use and reports the provider's error
   with the key removed from it.
5. Writes are audited.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from admin.services import provider_keys as svc
from core import provider_keys as pk
from tests.provider_keys_fake import FakeDB

PAGE = "/admin/model-keys"
SECRET = "unit-test-secret-key-0123456789"
KEY = "sk-sample-ZYXWVUTSRQPONMLK-7788"
KEY2 = "sk-sample-ABCDEFGHIJKLMNOP-9911"
ACTOR = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setenv("SECRET_KEY", SECRET)
    monkeypatch.setattr("core.auth.get_service_client", lambda: fake)
    monkeypatch.setattr("core.auth.get_service_client_for_env", lambda env: fake)
    monkeypatch.setattr(
        "admin.audit.record_change",
        lambda **kw: fake.tables.setdefault("agent_audit_log", []).append(kw))
    pk.invalidate()
    with patch("admin.router.client_for", return_value=fake):
        yield fake
    pk.invalidate()


def _everything_that_left(*parts) -> str:
    return " ".join(p if isinstance(p, str) else json.dumps(p, default=str) for p in parts)


# --------------------------------------------------------------------------
# 1. The page
# --------------------------------------------------------------------------


def test_page_lists_every_provider_and_its_key_source(client, db, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "env-gemini-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    svc.set_key("openai", KEY, ACTOR)
    r = client.get(PAGE)
    assert r.status_code == 200
    for label in ("Google Gemini", "OpenAI", "Anthropic", "Azure OpenAI"):
        assert label in r.text
    by_provider = {p["provider"]: p for p in svc.key_status()["providers"]}
    assert by_provider["openai"]["source"] == "portal"
    assert by_provider["google_genai"]["source"] == "environment"
    assert by_provider["anthropic"]["source"] == "none"
    assert "Stored key" in r.text and "Environment variable" in r.text and "No key" in r.text
    assert "ends in <code>7788</code>" in r.text


def test_set_replace_and_remove_through_the_forms(client, db, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai-key")
    r = client.post(PAGE, data={"provider": "openai", "value": KEY}, follow_redirects=False)
    assert r.status_code == 302 and "saved=OpenAI" in r.headers["location"]
    assert pk.stored_key("openai") == KEY
    r = client.post(PAGE, data={"provider": "openai", "value": KEY2}, follow_redirects=False)
    assert r.status_code == 302
    assert pk.stored_key("openai") == KEY2
    assert len(db.tables["provider_keys"]) == 1
    r = client.post(PAGE + "/delete", data={"provider": "openai"}, follow_redirects=False)
    assert r.status_code == 302 and "removed=OpenAI" in r.headers["location"]
    assert db.tables["provider_keys"] == []
    assert pk.resolve_key("openai", "OPENAI_API_KEY") == "env-openai-key"


def test_named_credential_sits_beside_the_default(client, db):
    client.post(PAGE, data={"provider": "openai", "value": KEY}, follow_redirects=False)
    r = client.post(PAGE, data={"provider": "openai", "name": "catalog", "value": KEY2},
                    follow_redirects=False)
    assert r.status_code == 302
    assert pk.stored_key("openai") == KEY and pk.stored_key("openai", "catalog") == KEY2
    page = client.get(PAGE).text
    assert 'data-credential="catalog"' in page and "ends in <code>9911</code>" in page


@pytest.mark.parametrize("data, needle", [
    ({"provider": "nonesuch", "value": KEY}, "unknown+provider"),
    ({"provider": "openai", "value": "   "}, "empty"),
    ({"provider": "openai", "value": "has a space in it"}, "does+not+look+like+a+key"),
    ({"provider": "openai", "name": "bad name!", "value": KEY}, "credential+name"),
])
def test_bad_input_is_refused_without_echoing_the_value(client, db, data, needle):
    r = client.post(PAGE, data=data, follow_redirects=False)
    assert r.status_code == 302 and needle in r.headers["location"]
    assert KEY not in r.headers["location"] and "space+in+it" not in r.headers["location"]
    assert db.tables.get("provider_keys", []) == []


# --------------------------------------------------------------------------
# 2. A stored key is never returned
# --------------------------------------------------------------------------


def test_nothing_returns_a_stored_key_or_its_ciphertext(client, db):
    returned = svc.set_key("openai", KEY, ACTOR)
    named = svc.set_key("openai", KEY2, ACTOR, name="catalog")
    ciphertexts = [r["ciphertext"] for r in db.tables["provider_keys"]]
    assert len(ciphertexts) == 2 and all(KEY not in c and KEY2 not in c for c in ciphertexts)
    page = client.get(PAGE).text
    status_json = client.get(PAGE + "/status.json").text
    with patch.object(svc, "_http_get", lambda url, headers: _Resp(200, {})):
        test_json = client.post(PAGE + "/test", data={"provider": "openai"}).text
    removed = svc.remove_key("openai", ACTOR, name="catalog")
    left = _everything_that_left(returned, named, svc.key_status(), svc.provider_sources(),
                                 page, status_json, test_json, removed,
                                 db.tables["agent_audit_log"])
    for secret in (KEY, KEY2, *ciphertexts):
        assert secret not in left
    assert "ciphertext" not in _everything_that_left(returned, named, svc.key_status(),
                                                    db.tables["agent_audit_log"])
    # The hint is what identifies a key.
    assert returned["hint"] == "7788" and "7788" in page


# --------------------------------------------------------------------------
# 3. No SECRET_KEY
# --------------------------------------------------------------------------


def test_without_secret_key_the_portal_refuses_and_says_why(client, db, monkeypatch):
    monkeypatch.delenv("SECRET_KEY")
    r = client.post(PAGE, data={"provider": "openai", "value": KEY}, follow_redirects=False)
    assert r.status_code == 302
    assert "SECRET_KEY+is+not+set" in r.headers["location"] and KEY not in r.headers["location"]
    # Nothing was written, in any form.
    assert db.tables.get("provider_keys", []) == []
    assert KEY not in json.dumps(db.tables, default=str)
    with pytest.raises(pk.SecretKeyMissing):
        svc.set_key("openai", KEY, ACTOR)
    page = client.get(PAGE).text
    assert "Keys cannot be stored on this server yet" in page and "SECRET_KEY is not set" in page
    assert svc.key_status()["can_store"] is False


def test_a_key_stored_under_another_secret_is_shown_as_unreadable(client, db, monkeypatch):
    svc.set_key("openai", KEY, ACTOR)
    monkeypatch.setenv("SECRET_KEY", "a-different-server-secret-000")
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai-key")
    pk.invalidate()
    openai = next(p for p in svc.key_status()["providers"] if p["provider"] == "openai")
    assert openai["default"]["readable"] is False and openai["source"] == "environment"
    assert "Cannot be read" in client.get(PAGE).text


# --------------------------------------------------------------------------
# 4. Test
# --------------------------------------------------------------------------


class _Resp:
    def __init__(self, status, body, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def test_test_button_makes_one_call_with_the_stored_key(client, db, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai-key")
    svc.set_key("openai", KEY, ACTOR)
    calls = []

    def fake_get(url, headers):
        calls.append((url, headers))
        return _Resp(200, {"data": []})

    with patch.object(svc, "_http_get", fake_get):
        r = client.post(PAGE + "/test", data={"provider": "openai"})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["source"] == "portal"
    assert len(calls) == 1
    url, headers = calls[0]
    assert url == "https://api.openai.com/v1/models" and KEY not in url
    assert headers["Authorization"] == f"Bearer {KEY}"


def test_test_reports_the_provider_error_without_the_key(db):
    svc.set_key("google_genai", KEY, ACTOR)

    def rejecting(url, headers):
        assert KEY not in url  # the key travels in a header, never the address
        return _Resp(400, {"error": {"message": f"API key not valid: {KEY}. Pass a valid key."}})

    result = svc.check_key("google_genai", http_get=rejecting)
    assert result["ok"] is False and "API key not valid" in result["detail"] and "400" in result["detail"]
    assert KEY not in result["detail"] and KEY[:16] not in result["detail"]

    def exploding(url, headers):
        raise RuntimeError(f"connection reset while sending header x-goog-api-key={KEY}")

    result = svc.check_key("google_genai", http_get=exploding)
    assert result["ok"] is False and "Could not reach" in result["detail"]
    assert KEY not in result["detail"]
    # A body that is not JSON is reported as text, scrubbed too.
    result = svc.check_key("google_genai", http_get=lambda u, h: _Resp(502, None, f"bad gateway {KEY}"))
    assert "502" in result["detail"] and KEY not in result["detail"]


def test_test_uses_the_environment_key_when_none_is_stored(db, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-anthropic-key")
    seen = {}
    result = svc.check_key("anthropic", http_get=lambda u, h: seen.update(h) or _Resp(200, {}))
    assert result == {"ok": True, "source": "environment", "detail": "Anthropic accepted the key."}
    assert seen["x-api-key"] == "env-anthropic-key"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    result = svc.check_key("anthropic", http_get=lambda u, h: pytest.fail("no key, no call"))
    assert result["ok"] is False and result["source"] == "none"


def test_named_credential_is_tested_against_its_models_endpoint(db):
    svc.set_key("openai", KEY2, ACTOR, name="catalog")
    result = svc.check_key("openai", "catalog", http_get=lambda u, h: pytest.fail("no endpoint, no call"))
    assert result["ok"] is False and "base_url" in result["detail"]
    db.tables["agent_models"] = [{"provider": "openai", "environment": "staging", "is_active": True,
                                  "extra_config": {"base_url": "https://models.example.test/v1/",
                                                   "credential": "catalog"}}]
    seen = []
    result = svc.check_key("openai", "catalog",
                           http_get=lambda u, h: seen.append((u, h)) or _Resp(200, {}))
    assert result["ok"] is True
    assert seen == [("https://models.example.test/v1/models", {"Authorization": f"Bearer {KEY2}"})]


def test_azure_test_needs_an_endpoint(db, monkeypatch):
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    svc.set_key("azure_openai", KEY, ACTOR)
    result = svc.check_key("azure_openai", http_get=lambda u, h: pytest.fail("no endpoint, no call"))
    assert result["ok"] is False and "AZURE_OPENAI_ENDPOINT" in result["detail"]
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://sample.openai.azure.com/")
    seen = []
    svc.check_key("azure_openai", http_get=lambda u, h: seen.append((u, h)) or _Resp(200, {}))
    assert seen[0][0].startswith("https://sample.openai.azure.com/openai/models?")
    assert seen[0][1] == {"api-key": KEY}


# --------------------------------------------------------------------------
# 5. Audit, the model form, the nav
# --------------------------------------------------------------------------


def test_writes_are_audited_with_the_actor_and_no_key(db):
    svc.set_key("openai", KEY, ACTOR)
    svc.set_key("openai", KEY2, ACTOR)
    svc.remove_key("openai", ACTOR)
    assert svc.remove_key("openai", ACTOR) is False  # nothing left; not audited
    log = db.tables["agent_audit_log"]
    assert [row["action"] for row in log] == ["set", "replace", "delete"]
    assert all(row["entity_type"] == "provider_keys" and row["actor_user_id"] == ACTOR for row in log)
    assert log[0]["after"]["hint"] == "7788" and log[1]["before"]["hint"] == "7788"
    assert log[1]["after"]["hint"] == "9911" and log[2]["after"] is None
    stored = db.tables.get("provider_keys", [])
    assert stored == []
    row = svc.set_key("openai", KEY, ACTOR)
    assert row["created_by"] == ACTOR and row["updated_by"] == ACTOR and row["replaced"] is False


def test_status_json_tells_the_model_form_which_providers_have_a_key(client, db, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "env-gemini-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    svc.set_key("openai", KEY, ACTOR)
    body = client.get(PAGE + "/status.json").json()
    assert body["sources"]["openai"] == "portal"
    assert body["sources"]["google_genai"] == body["sources"]["google"] == "environment"
    assert body["sources"]["anthropic"] == "none"
    assert body["manage_url"] == PAGE
    form = client.get("/admin/agents?tab=models").text
    assert "data-key-status-for" in form and "/admin/model-keys/status.json" in form


def test_model_keys_is_a_settings_screen(client, db):
    from admin import nav

    assert nav.section_for(PAGE) == "settings"
    tabs = nav.section_tabs_for(PAGE)
    assert tabs and tabs["current"] == "model_keys"
    hub = client.get("/admin/settings").text
    assert "Model keys" in hub and 'href="/admin/model-keys"' in hub
