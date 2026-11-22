"""The Voice tab for a generic (portal-built) agent, and the test runners
that call one.

A generic agent is called at its own address and is configuration only, so
the page has to hold three things a product kind's page never needed: the
address and a Call button, a plain statement of why a call cannot start on
THIS server (no realtime model, a missing provider key), and the built-in
persona and greeting shown as defaults rather than as misconfigurations.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.admin.conftest import client  # noqa: F401 — session-scoped superuser TestClient
from tests.admin.test_voice_screen_routes import _Client

_MODEL = {
    "model_id": "model-rt-1", "name": "Live speech model",
    "provider": "google_genai", "model_string": "live-preview",
    "is_active": True, "environment": "staging", "extra_config": {"realtime": True},
}


def _agent(slug, profile_id, **voice):
    return {
        "profile_id": profile_id, "agent_kind": "custom", "slug": slug,
        "display_name": slug.title(), "status": "active", "is_active": True,
        "environment": "staging", "parent_profile_id": None,
        "prompt_version_ids": {}, "served_version_id": None, "voice_config": voice,
    }


_HELPDESK = _agent("helpdesk", "hd-1", enabled=True, model_id="model-rt-1",
                   agent_profile_slug="helpdesk-voice")
_BRAIN = _agent("helpdesk-voice", "hd-2")


@pytest.fixture
def voice_db(monkeypatch):
    monkeypatch.setenv("ENABLE_GENERIC_AGENTS", "1")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google")
    tables = {
        "agent_profiles": [_HELPDESK, _BRAIN], "agent_models": [_MODEL],
        "agent_profile_versions": [], "agent_prompts": [], "agent_prompt_versions": [],
    }
    with patch("admin.router.client_for", return_value=_Client(tables)):
        yield tables


def test_the_page_names_the_address_and_offers_a_call(client, voice_db):  # noqa: F811
    r = client.get("/admin/agents/hd-1/voice?env=staging")
    assert r.status_code == 200, r.text[:800]
    assert "WS /v1/agents/helpdesk/voice" in r.text
    assert "data-voice-call-start" in r.text
    assert "/static/shared/voice-live.js" in r.text and "/admin/static/voice-call.js" in r.text
    assert "A call cannot start yet" not in r.text


def test_a_missing_realtime_key_is_named_on_the_page(client, voice_db, monkeypatch):  # noqa: F811
    """The fresh-stack case: no provider key on the server. The page says
    which key to set, and offers no Call button that could only fail."""
    monkeypatch.delenv("GOOGLE_API_KEY")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    r = client.get("/admin/agents/hd-1/voice?env=staging")
    assert r.status_code == 200, r.text[:800]
    assert "A call cannot start yet" in r.text
    assert "Realtime model key missing: set GOOGLE_API_KEY on the server." in r.text
    assert "data-voice-call-start" not in r.text


def test_no_realtime_model_is_named_on_the_page(client, voice_db):  # noqa: F811
    voice_db["agent_profiles"][0] = _agent("helpdesk", "hd-1", enabled=True)
    r = client.get("/admin/agents/hd-1/voice?env=staging")
    assert r.status_code == 200, r.text[:800]
    assert "No realtime voice model is set for this agent." in r.text
    assert "data-voice-call-start" not in r.text


def test_the_api_flag_off_is_named_on_the_page(client, voice_db, monkeypatch):  # noqa: F811
    monkeypatch.delenv("ENABLE_GENERIC_AGENTS")
    r = client.get("/admin/agents/hd-1/voice?env=staging")
    assert "ENABLE_GENERIC_AGENTS" in r.text and "data-voice-call-start" not in r.text


def test_a_blank_persona_and_greeting_read_as_built_in_defaults(client, voice_db):  # noqa: F811
    r = client.get("/admin/agents/hd-1/voice?env=staging")
    assert "built-in voice persona" in r.text
    assert "the built-in greeting is used" in r.text
    # The product-kind warning for the same blank field must not show.
    assert "the connect has no persona" not in r.text


def test_an_agent_borrowed_as_a_brain_still_owns_its_own_voice_settings(client, voice_db):  # noqa: F811
    """`helpdesk` names `helpdesk-voice` as its brain. On a product kind that
    makes the second row a specialist whose page saves to the first. A generic
    agent has its own address, so its page must save to ITSELF."""
    from admin.router import client_for
    from admin.screens.agent_voice import resolve_voice_pair

    pair = resolve_voice_pair(client_for("staging"), "staging", _BRAIN)
    assert pair["viewing"] == "owner" and pair["owner"]["profile_id"] == "hd-2"
    r = client.get("/admin/agents/hd-2/voice?env=staging")
    assert 'action="/admin/agents/hd-2/voice' in r.text
    assert "WS /v1/agents/helpdesk-voice/voice" in r.text
    # And the owner's page still shows who answers.
    owner = resolve_voice_pair(client_for("staging"), "staging", _HELPDESK)
    assert owner["specialist"]["profile_id"] == "hd-2"


def test_the_agents_screen_does_not_send_a_borrowed_brain_to_another_agents_settings():
    """The Agents screen tells a product kind's voice specialist that its
    calls are configured on the agent that names it. A generic agent has its
    own address and settings, so it must get no such redirect."""
    from admin.screens.agents import resolve_voice_config_owner

    assert resolve_voice_config_owner([_HELPDESK, _BRAIN], _BRAIN) is None
    product = [
        {"profile_id": "a", "agent_kind": "orchard", "slug": "main",
         "voice_config": {"agent_profile_slug": "voice"}},
        {"profile_id": "b", "agent_kind": "orchard", "slug": "voice", "voice_config": {}},
    ]
    assert resolve_voice_config_owner(product, product[1])["profile_id"] == "a"


def test_a_product_kinds_page_is_unchanged(client):  # noqa: F811
    from tests.admin.test_voice_screen_routes import _TABLES

    with patch("admin.router.client_for", return_value=_Client(_TABLES)):
        r = client.get("/admin/agents/parent-1/voice?env=staging")
    assert r.status_code == 200
    assert "Call this agent" not in r.text and "voice-call.js" not in r.text


# ------------------------------------------------------------------ the runners
def test_a_voice_eval_targets_the_agents_own_address(monkeypatch):
    from admin.services.test_chat import voice_target

    monkeypatch.setenv("ENABLE_GENERIC_AGENTS", "1")
    target = voice_target({"agent_kind": "custom", "slug": "helpdesk", "profile_id": "hd-1"})
    assert target == {"ws_path": "/v1/agents/helpdesk/voice", "auth": {}, "profile_id": "hd-1"}
    monkeypatch.delenv("ENABLE_GENERIC_AGENTS")
    assert voice_target({"agent_kind": "custom", "slug": "helpdesk"}) is None


def test_a_scenario_calling_a_generic_agent_carries_the_operators_token(monkeypatch):
    from admin.voice_scenario_runner import scenario_headers

    monkeypatch.delenv("VOICE_SCENARIO_TARGET", raising=False)
    generic = {"ws_path": "/v1/agents/helpdesk/voice"}
    assert scenario_headers(generic, target=None, headers=None, user_token="tok") == {
        "Authorization": "Bearer tok"}
    # Never to another server, never over a header the caller chose, and
    # never for a kind-addressed scenario.
    assert scenario_headers(generic, target="wss://elsewhere.example", headers=None,
                            user_token="tok") is None
    monkeypatch.setenv("VOICE_SCENARIO_TARGET", "wss://elsewhere.example")
    assert scenario_headers(generic, target=None, headers=None, user_token="tok") is None
    monkeypatch.delenv("VOICE_SCENARIO_TARGET")
    mine = {"authorization": "Bearer mine"}
    assert scenario_headers(generic, target=None, headers=mine, user_token="tok") == mine
    assert scenario_headers({"ws_path": "/voice/live"}, target=None, headers=None,
                            user_token="tok") is None
    assert scenario_headers({"ws_path": "/v1/agents/helpdesk/voice/../x"}, target=None,
                            headers=None, user_token="tok") is None
