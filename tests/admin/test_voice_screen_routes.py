"""`/admin/agents/{id}/voice` — the rendered screen and its save path.

Two things are pinned here that the field-registry test cannot reach, because
both are properties of the PAGE rather than of the registry:

* **D10 — the parent/specialist relationship is structure, not a notice.**
  `admin/screens/agents.py::resolve_voice_config_owner` derives its notice from
  the Agents rail's already-filtered profile list, so a scoped-build or env filter
  that hides the parent also hides the explanation, and the specialist's blank
  voice card is left looking authoritative. This screen resolves the pair from
  the database by slug and always posts to the OWNER's id — so there is no
  filter state in which the relationship, or the correct save target, can
  vanish.
* **The effective-value notes reach the HTML.** A note computed in Python and
  never rendered is the same defect as no note at all.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.admin.conftest import client  # noqa: F401 — session-scoped superuser TestClient

_PARENT = {
    "profile_id": "parent-1", "agent_kind": "orchard", "slug": "orchard_default",
    "display_name": "Orchard Merchant Data Assistant", "status": "active",
    "environment": "staging", "parent_profile_id": None,
    "prompt_version_ids": {}, "served_version_id": None,
    "voice_config": {
        "enabled": True,
        "model_id": "model-rt-1",
        "agent_profile_slug": "orchard_voice",
        "persona_prompt_slug": "orchard_voice_persona",
        "voice_id": "marin",
        "language": "ar-SA",
        # Both incident levers disengaged, and the streaming checkbox left on
        # against a provider that refuses to stream: the three notes the screen
        # exists to surface, in one fixture.
        "barge_in_enabled": False,
        "answer_watchdog_sec": 0,
        "stream_sentences": True,
    },
}
_SPECIALIST = {
    "profile_id": "voice-1", "agent_kind": "orchard", "slug": "orchard_voice",
    "display_name": "Layla — Orchard Voice Call Agent", "status": "specialist",
    "environment": "staging", "parent_profile_id": "parent-1",
    "prompt_version_ids": {}, "served_version_id": None,
    "voice_config": {},
}
_MODEL = {
    "model_id": "model-rt-1", "name": "Azure Voice Live (native)",
    "provider": "azure_voice_live", "model_string": "gpt-realtime",
    "is_active": True, "environment": "staging",
    "extra_config": {"realtime": True, "voice_type": "native"},
}

_TABLES = {
    "agent_profiles": [_PARENT, _SPECIALIST],
    "agent_models": [_MODEL],
    "agent_profile_versions": [],
    "agent_prompts": [],
    "agent_prompt_versions": [],
}


class _Query:
    """A minimal PostgREST-shaped query over in-memory rows.

    Only `.select().eq().limit().order().execute()` — the exact surface
    `load_voice_context` uses. Deliberately not a MagicMock: the point of these
    tests is that the screen asks the right questions of the right table, and a
    mock that answers everything with `[]` would pass while rendering nothing.
    """

    def __init__(self, rows):
        self._rows = list(rows)

    def select(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def eq(self, column, value):
        return _Query([r for r in self._rows if r.get(column) == value])

    def limit(self, n):
        return _Query(self._rows[:n])

    def execute(self):
        class _R:
            data = self._rows
        return _R()


class _Client:
    def __init__(self, tables):
        self._tables = tables

    def table(self, name):
        return _Query(self._tables.get(name, []))


@pytest.fixture
def voice_db():
    with patch("admin.router.client_for", return_value=_Client(_TABLES)):
        yield


def test_voice_screen_renders_every_registered_field(client, voice_db):  # noqa: F811
    from admin.screens.agent_voice import VOICE_FIELDS

    r = client.get("/admin/agents/parent-1/voice?env=staging")
    assert r.status_code == 200, r.text[:800]
    missing = [f.field_name for f in VOICE_FIELDS
               if f'name="{f.field_name}"' not in r.text]
    assert not missing, missing


def test_screen_surfaces_the_disengaged_incident_levers(client, voice_db):  # noqa: F811
    """The whole point. An operator landing here mid-incident must be able to
    SEE that barge-in was pulled and the watchdog zeroed — the two conditions
    that, with no field to show them, survived incident #38 for days."""
    r = client.get("/admin/agents/parent-1/voice?env=staging")
    assert "cannot interrupt at all" in r.text
    assert "rescues an answer which never starts speaking" in r.text


def test_screen_says_stream_sentences_is_off_on_this_provider(client, voice_db):  # noqa: F811
    """`azure_voice_live` declares supports_streamed_tool_results = False, so
    the endpoint refuses to stream however the checkbox reads."""
    r = client.get("/admin/agents/parent-1/voice?env=staging")
    assert "supports_streamed_tool_results" in r.text


def test_screen_says_the_empty_topic_terms_disable_both_stall_tiers(client, voice_db):  # noqa: F811
    r = client.get("/admin/agents/parent-1/voice?env=staging")
    assert "residual stall tiers" in r.text
    assert "DISABLED" in r.text


def test_both_halves_of_the_pair_are_named_from_the_parent(client, voice_db):  # noqa: F811
    r = client.get("/admin/agents/parent-1/voice?env=staging")
    assert "Orchard Merchant Data Assistant" in r.text
    assert "Layla" in r.text
    assert 'action="/admin/agents/parent-1/voice' in r.text


def test_opening_the_specialist_shows_the_same_pair_and_saves_to_the_owner(
    client, voice_db,  # noqa: F811
):
    """D10. Viewing the specialist must show the SAME answer as viewing the
    parent — the divergence is a property of the tenant, not of the page — and
    the form must post to the row that actually stores the config, or the edit
    lands where nothing reads it."""
    r = client.get("/admin/agents/voice-1/voice?env=staging")
    assert r.status_code == 200, r.text[:800]
    assert "Orchard Merchant Data Assistant" in r.text
    assert 'action="/admin/agents/parent-1/voice' in r.text
    assert "You opened the voice specialist" in r.text


def test_the_pair_survives_a_rail_filter_that_hides_the_parent(voice_db):
    """The failure mode D10 names, at the resolver rather than the template:
    the pair comes from a slug query against the environment, so nothing about
    which rows a filtered rail happens to be showing can change the answer."""
    from admin.router import client_for
    from admin.screens.agent_voice import resolve_voice_pair

    pair = resolve_voice_pair(client_for("staging"), "staging", _SPECIALIST)
    assert pair["viewing"] == "specialist"
    assert pair["owner"]["profile_id"] == "parent-1"
    assert pair["specialist"]["profile_id"] == "voice-1"


def test_a_profile_with_no_specialist_still_renders_both_panels(client, voice_db):  # noqa: F811
    """"No specialist" is a fact an operator needs, so the panel says it rather
    than disappearing — a panel that vanishes teaches nothing."""
    solo = {**_PARENT, "profile_id": "solo-1", "slug": "solo",
            "voice_config": {"enabled": True, "model_id": "model-rt-1"}}
    with patch("admin.router.client_for",
               return_value=_Client({**_TABLES, "agent_profiles": [solo]})):
        r = client.get("/admin/agents/solo-1/voice?env=staging")
    assert r.status_code == 200, r.text[:800]
    assert "No separate voice specialist" in r.text


def test_unknown_profile_is_a_404_not_a_blank_form(client, voice_db):  # noqa: F811
    r = client.get("/admin/agents/nope/voice?env=staging")
    assert r.status_code == 404


def test_save_writes_the_whole_block_and_redirects_to_the_viewed_profile(client):  # noqa: F811
    """The POST target is the OWNER, the redirect is back to whatever the
    operator was looking at — on a specialist those are different rows."""
    captured = {}

    def _update(profile_id, payload, actor_user_id):
        captured["profile_id"] = profile_id
        captured["payload"] = payload
        return {}

    with patch("admin.router.client_for", return_value=_Client(_TABLES)), \
            patch("admin.router.admin_service_client",
                  return_value=_Client(_TABLES)), \
            patch("admin.services.profiles.update_profile_row", _update):
        r = client.post(
            "/admin/agents/parent-1/voice?env=staging",
            data={"_voice_form": "1", "return_profile_id": "voice-1",
                  "vc_enabled": "on", "vc_barge_in_enabled": "on",
                  "vc_answer_watchdog_sec": "4"},
            follow_redirects=False,
        )
    assert r.status_code == 303, r.text[:400]
    assert r.headers["location"].startswith("/admin/agents/voice-1/voice")
    assert captured["profile_id"] == "parent-1"
    assert captured["payload"]["voice_config"]["barge_in_enabled"] is True
    assert captured["payload"]["voice_config"]["answer_watchdog_sec"] == 4.0


def test_save_without_the_form_marker_is_refused(client):  # noqa: F811
    """A POST that cannot prove it came from this screen owns nothing here.
    Writing its absent checkboxes would turn a stray request into a config
    change — the same failure Part B input #6 records against the mapping
    form's gate, which is why this route refuses rather than guesses."""
    with patch("admin.router.client_for", return_value=_Client(_TABLES)), \
            patch("admin.router.admin_service_client",
                  return_value=_Client(_TABLES)):
        r = client.post("/admin/agents/parent-1/voice?env=staging",
                        data={"vc_enabled": "on"}, follow_redirects=False)
    assert r.status_code == 400


def test_agents_screen_links_to_the_voice_screen(client):  # noqa: F811
    """The voice settings screen IS the agent's Voice tab (B1): the detail
    view's tab strip links there, and the screen names the incident levers
    (rendered from VOICE_FIELDS, asserted on the rendered page above)."""
    from admin.screens.agents_views import build_detail_view

    view = build_detail_view({
        "active_profile": {"profile_id": "p1", "agent_kind": "orchard",
                           "voice_config": {"agent_profile_slug": "orchard_voice"}},
        "profiles": [],
    }, "staging")
    assert ("voice", "Voice", "/admin/agents/p1/voice?env=staging") in view["detail_tabs"]

def test_the_return_target_cannot_be_used_as_an_open_redirect(client):  # noqa: F811
    """`return_profile_id` is form input that lands in a Location header, so it
    is character-checked before it gets there. Superuser gating is not a reason
    to interpolate untrusted text into a redirect."""
    with patch("admin.router.client_for", return_value=_Client(_TABLES)), \
            patch("admin.router.admin_service_client",
                  return_value=_Client(_TABLES)), \
            patch("admin.services.profiles.update_profile_row",
                  lambda **_k: {}):
        r = client.post(
            "/admin/agents/parent-1/voice?env=staging",
            data={"_voice_form": "1",
                  "return_profile_id": "../../evil.example/x"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/agents/parent-1/voice?env=staging"
