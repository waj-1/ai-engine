"""`resolve_live_voice_config` mirrors the RUNTIME's shim, in Python.

agents.html used to compute the block itself — `{% set live_vc =
(vc.get('live') or vc) %}` — a third implementation of the `{"live": {...}}`
compat shim, on Jinja truthiness rather than `isinstance(nested, dict)`. It
diverged from `voice/live/config.py::_voice_block` on exactly two shapes:

* `live: {}` — the runtime resolves the empty block (duplex disabled); the `or`
  fell through to the TOP level and rendered the retired turn-based keys as
  duplex values.
* `live: "somestring"` — `.get` on a string raised `UndefinedError`, and
  `agents_view` has no try/except, so /admin/agents 500'd permanently. The unify
  migration's `jsonb_typeof = 'object'` gate deliberately skips that shape, so
  nothing repairs the row.

The Duplex voice fieldset itself is gone (2026-08-26 — the voice cockpit is
the single portal writer now), but the resolver survives with one consumer on
this screen: `_load_persona_versions` reads `agent_profile_slug` through it,
and it must keep resolving exactly as the runtime does, malformed shapes
included. The render tests pin the fieldset's replacement: no duplex inputs,
no 500 on a malformed column, and no Jinja re-implementation of the shim.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from admin.screens.agents import resolve_live_voice_config
from tests.admin.conftest import client  # noqa: F401 — session-scoped superuser TestClient

TEMPLATE = Path(__file__).resolve().parents[2] / "admin" / "templates" / "agents.html"


def _context(active_profile):
    return {
        "profiles": [active_profile],
        "voice_config_owner": None,
        "live_voice_config": resolve_live_voice_config(active_profile),
        "top_level_profiles": [active_profile],
        "sub_profiles_by_parent": {},
        "active_profile": active_profile,
        "profile_versions": [],
        "served_version_id": None,
        "prompts": [],
        "versions_by_prompt": {},
        "tools": [],
        "data_sources": [],
        "mcp_servers": [],
        "mcp_attachments": [],
        "attachable_mcps": [],
        "skill_attachments": [],
        "attachable_skills": [],
        "downstream_candidates": [],
        "e2e_runs": [],
        "mapped": {"tool_ids": set(), "source_ids": set(),
                   "mcp_ids": set(), "downstream_ids": set()},
        "all_models": [],
        "realtime_models": [],
        "input_schema": None,
        "input_validators": {},
    }


def _render(client, active_profile, tab="overview"):  # noqa: F811
    with patch("admin.screens.agents.load_agents_context",
               return_value=_context(active_profile)):
        return client.get(f"/admin/agents?profile_id=x&kind=orchard&tab={tab}")


def _profile(voice_config):
    return {
        "profile_id": "p1", "agent_kind": "orchard", "slug": "orchard_default",
        "display_name": "Orchard", "status": "active", "voice_config": voice_config,
    }


# --- the resolver ------------------------------------------------------------

def test_empty_live_block_resolves_to_the_empty_block_not_the_top_level():
    """`_voice_block` returns the nested block whenever it is a dict — `{}`
    included. Jinja truthiness sent this to the top level instead."""
    resolved = resolve_live_voice_config(_profile({
        "live": {},
        "enabled": True,                    # retired turn-based
        "voice_id": "ar-SA-HamedNeural",    # retired turn-based
        "stt_provider": "azure",
    }))

    assert resolved.get("enabled") is None, "the retired `enabled: true` must not surface"
    assert resolved.get("voice_id") is None
    assert resolved["_retired_turn_based"]["voice_id"] == "ar-SA-HamedNeural"


def test_a_nested_block_resolves_from_the_nested_keys():
    resolved = resolve_live_voice_config(_profile({
        "voice_id": "ar-SA-HamedNeural",                 # retired turn-based
        "live": {"enabled": True, "model_id": "m-1"},    # duplex
    }))

    assert resolved["model_id"] == "m-1"
    assert resolved["enabled"] is True


def test_a_flat_row_resolves_as_itself():
    flat = {"enabled": True, "model_id": "m-1", "voice_id": "marin"}
    assert resolve_live_voice_config(_profile(flat)) == flat


def test_a_non_dict_live_value_resolves_to_a_dict():
    """The shape the migration's `jsonb_typeof = 'object'` gate never repairs."""
    resolved = resolve_live_voice_config(_profile({"live": "somestring",
                                                   "model_id": "m-1"}))
    assert isinstance(resolved, dict)
    assert resolved["model_id"] == "m-1"


def test_a_missing_or_malformed_column_is_an_empty_block():
    assert resolve_live_voice_config({"profile_id": "p"}) == {}
    assert resolve_live_voice_config(None) == {}
    assert resolve_live_voice_config(_profile("not-a-dict")) == {}


# --- the screen --------------------------------------------------------------

def test_the_agents_screen_renders_no_duplex_inputs_at_all(client):  # noqa: F811
    """The fieldset is gone; a stored voice_config — retired keys included —
    must not surface as editable controls anywhere on this page. The link card
    replaces it."""
    r = _render(client, _profile({
        "live": {},
        "enabled": True,
        "voice_id": "ar-SA-HamedNeural",
        "stt_provider": "azure",
    }))

    assert r.status_code == 200, r.text[:600]
    assert 'value="ar-SA-HamedNeural"' not in r.text
    assert 'name="live_' not in r.text
    # The resolved block is empty (`live: {}`), so there is no Voice tab; the
    # Settings tab offers the way in to the voice settings screen instead.
    settings = _render(client, _profile({
        "live": {}, "enabled": True, "voice_id": "ar-SA-HamedNeural",
    }), tab="settings")
    assert 'value="ar-SA-HamedNeural"' not in settings.text
    assert "set up voice for this agent" in settings.text
    assert "/voice?env=" in settings.text


def test_a_non_dict_live_value_does_not_500_the_agents_screen(client):  # noqa: F811
    r = _render(client, _profile({"live": "somestring", "model_id": "m-1"}))

    assert r.status_code == 200, r.text[:600]
    # A configured voice block → the Voice tab (the voice settings screen).
    assert "/voice?env=" in r.text


def test_the_template_no_longer_reimplements_the_shim():
    """A Jinja copy of the shim is invisible to a grep for
    `promote_legacy_voice_config` when the rollback window closes and the shim
    is deleted. Any future reader of the block must come from the context,
    resolved in Python — today the template reads none of it."""
    text = TEMPLATE.read_text()

    assert "{% set vc = (active_profile.voice_config" not in text
    assert ".get('live')" not in text
