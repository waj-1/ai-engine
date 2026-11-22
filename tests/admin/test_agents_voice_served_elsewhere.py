"""The Agents screen must say where a voice specialist's config actually lives.

Reported against a client demo as "we don't see the orchard voice agent, can't
configure it, can't access the persona". The profile was there the whole time —
`↳ Orchard Voice Call Agent`, `status='specialist'`, rendered in the rail. What
was missing was its configuration: a served specialist stores no
`voice_config` of its own, because the PARENT holds it and names the
specialist by slug via `agent_profile_slug`. So clicking the row named after the
voice agent produced a Voice card with every field blank and both Enabled boxes
unchecked — truthful, and indistinguishable from "this thing is broken".

These pin the notice that points one row up, and pin that it stays OFF for an
ordinary profile so the common case keeps its quiet card.
"""
from __future__ import annotations

from unittest.mock import patch

from tests.admin.conftest import client  # noqa: F401 — session-scoped superuser TestClient

_SPECIALIST = {
    "profile_id": "voice-1", "agent_kind": "orchard", "slug": "orchard_voice",
    "display_name": "Layla — Orchard Voice Call Agent", "status": "specialist",
    "parent_profile_id": "parent-1", "voice_config": {},
}
_PARENT = {
    "profile_id": "parent-1", "agent_kind": "orchard", "slug": "orchard_default",
    "display_name": "Orchard Merchant Data Assistant", "status": "active",
    # FLAT since the 2026-08-17 unification -- these keys ARE the block.
    "voice_config": {
        "enabled": True,
        "agent_profile_slug": "orchard_voice",
        "persona_prompt_slug": "orchard_voice_persona",
        "voice_id": "marin",
        "relay_mode": True,
    },
}


def _context(active_profile, profiles, voice_config_owner):
    return {
        "profiles": profiles,
        "voice_config_owner": voice_config_owner,
        "top_level_profiles": [p for p in profiles if not p.get("parent_profile_id")],
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
        "mapped": {
            "tool_ids": set(), "source_ids": set(),
            "mcp_ids": set(), "downstream_ids": set(),
        },
        "all_models": [],
        "input_schema": None,
        "input_validators": {},
    }


def _render(client, ctx, tab="overview"):  # noqa: F811
    with patch("admin.screens.agents.load_agents_context", return_value=ctx):
        return client.get(f"/admin/agents?profile_id=x&kind=orchard&tab={tab}")


def test_specialist_page_points_at_the_profile_that_owns_its_voice_config(client):  # noqa: F811
    """The blank-specialist trap, post-fieldset: the Agents screen no longer
    renders voice fields at all, so what it owes the operator is the pointer —
    the link card must name the owner, link to it, and offer the COCKPIT at
    the owner's id rather than the specialist's (the cockpit resolves the pair
    either way, but the link must not teach that the specialist row is where
    voice lives). The settings themselves render on the cockpit now."""
    owner = {
        "profile_id": "parent-1",
        "display_name": "Orchard Merchant Data Assistant",
        # The context key is still called `live` -- it is the template's name
        # for "the owner's voice settings", not a nested DB path.
        "live": _PARENT["voice_config"],
    }
    r = _render(client, _context(_SPECIALIST, [_PARENT, _SPECIALIST], owner))

    assert r.status_code == 200, r.text[:600]
    # Names the owner AND links to it — without the link the operator has to
    # guess which of the sibling rows to open.
    assert "Orchard Merchant Data Assistant" in r.text
    assert "profile_id=parent-1" in r.text
    assert "Calls to this profile are configured on" in r.text
    # The card's cockpit link resolves to the OWNER's screen. (The nav chip at
    # the top still opens the cockpit from the viewed profile's id — the
    # cockpit resolves the pair either way — so only the card is asserted.)
    assert "/admin/agents/parent-1/voice?env=" in r.text


def test_ordinary_profile_gets_no_notice(client):  # noqa: F811
    """No other profile routes voice here → the plain link card, no owner
    pointer."""
    plain = {
        "profile_id": "p1", "agent_kind": "sales", "slug": "default",
        "display_name": "Sales", "status": "active", "voice_config": {},
    }
    r = _render(client, _context(plain, [plain], None))

    assert r.status_code == 200
    assert "Calls to this profile are configured on" not in r.text
    # No voice configured anywhere → no Voice tab; Settings offers the way in.
    settings = _render(client, _context(plain, [plain], None), tab="settings")
    assert "/admin/agents/p1/voice?env=" in settings.text


_TITLE_SUBAGENT = {
    "profile_id": "title-1", "agent_kind": "orchard", "slug": "orchard_title",
    "display_name": "Orchard Conversation Title", "status": "active",
    "parent_profile_id": "parent-1", "voice_config": {},
}
_ALL = [_PARENT, _SPECIALIST, _TITLE_SUBAGENT]


def test_owner_is_resolved_from_the_slug_pointer():
    from admin.screens.agents import resolve_voice_config_owner

    owner = resolve_voice_config_owner(_ALL, _SPECIALIST)

    assert owner is not None
    assert owner["profile_id"] == "parent-1"
    assert owner["display_name"] == "Orchard Merchant Data Assistant"
    assert owner["live"]["persona_prompt_slug"] == "orchard_voice_persona"


def test_sibling_sharing_the_parent_resolves_no_owner():
    """The title sub-agent has the SAME parent_profile_id as the voice
    specialist but no voice pointer aimed at it. Matching on parentage instead
    of the slug would hang a false "configured elsewhere" notice here."""
    from admin.screens.agents import resolve_voice_config_owner

    assert resolve_voice_config_owner(_ALL, _TITLE_SUBAGENT) is None


def test_parent_does_not_resolve_itself_as_its_own_owner():
    """The parent's own pointer names the specialist, not itself — but it also
    must not match itself if an operator ever points a profile at its own slug.
    """
    from admin.screens.agents import resolve_voice_config_owner

    assert resolve_voice_config_owner(_ALL, _PARENT) is None

    self_pointing = {**_PARENT, "slug": "orchard_default",
                     "voice_config": {"agent_profile_slug": "orchard_default"}}
    assert resolve_voice_config_owner([self_pointing], self_pointing) is None


def test_no_active_profile_or_slug_is_safe():
    from admin.screens.agents import resolve_voice_config_owner

    assert resolve_voice_config_owner(_ALL, None) is None
    assert resolve_voice_config_owner(_ALL, {"profile_id": "x"}) is None


def test_malformed_sibling_voice_config_is_skipped_not_fatal():
    """A4 hardening (resolve_live_voice_config) guarded only the ACTIVE
    profile's malformed voice_config. A SIBLING row anywhere else in the
    list with a non-dict voice_config -- e.g. a bare string -- used to reach
    promote_legacy_voice_config's `dict(profile_voice or {})` unguarded and
    raise ValueError, 500ing the whole /admin/agents page for every OTHER
    profile too, not just the broken row.
    """
    from admin.screens.agents import resolve_voice_config_owner

    malformed = {
        "profile_id": "broken-1", "agent_kind": "orchard",
        "slug": "orchard_broken", "display_name": "Broken",
        "status": "active", "voice_config": "not-a-dict",
    }
    profiles = [malformed, *_ALL]

    # The malformed row is skipped, not fatal -- the real owner is still found.
    owner = resolve_voice_config_owner(profiles, _SPECIALIST)
    assert owner is not None
    assert owner["profile_id"] == "parent-1"

    # And a malformed row that doesn't match anything resolves to no owner,
    # not an exception.
    assert resolve_voice_config_owner(profiles, _TITLE_SUBAGENT) is None


# --- the persona prompt is NOT editable from the specialist ------------------
#
# The persona editor posts the profile being VIEWED as `profile_id`, so a save
# made while looking at the specialist repoints only that row's
# `prompt_version_ids[slug]`. Since 20260817121000 backfilled the pin onto the
# PARENT, `resolve_persona_prompt`'s parent tier returns unconditionally — the
# specialist's pin is outranked, the misplaced-pin warning never fires, and the
# save is a silent no-op. Pre-PR that flow worked, so presenting the card as
# editable here is now a way to lose a live persona tune (this is a path used on
# the client demo VM).

_PERSONA_PROMPT = {
    "prompt_id": "prompt-persona-1", "slug": "orchard_voice_persona",
    "content": "أنت ليلى.", "agent_kind": "orchard",
}
_TITLE_PROMPT = {
    "prompt_id": "prompt-title-1", "slug": "title",
    "content": "Summarise the conversation.", "agent_kind": "orchard",
}


def _ctx_with_prompts(active_profile, profiles, owner):
    ctx = _context(active_profile, profiles, owner)
    ctx["prompts"] = [_PERSONA_PROMPT, _TITLE_PROMPT]
    ctx["live_voice_config"] = (active_profile.get("voice_config") or {})
    return ctx


def test_persona_editor_is_read_only_on_the_specialist(client):  # noqa: F811
    owner = {
        "profile_id": "parent-1",
        "display_name": "Orchard Merchant Data Assistant",
        "live": _PARENT["voice_config"],
    }
    r = _render(client, _ctx_with_prompts(_SPECIALIST, [_PARENT, _SPECIALIST], owner),
                tab="instructions")

    assert r.status_code == 200, r.text[:600]
    # No save form for the persona row...
    assert 'action="/admin/prompts/prompt-persona-1/versions"' not in r.text
    # ...but the other non-system prompt keeps its editor.
    assert 'action="/admin/prompts/prompt-title-1/versions"' in r.text
    # The content is still readable, and the operator is sent to the owner.
    assert "أنت ليلى." in r.text
    assert "Edit this persona on" in r.text


def test_persona_editor_stays_editable_on_the_parent(client):  # noqa: F811
    """The parent IS where the served pin lives — its editor must be untouched."""
    r = _render(client, _ctx_with_prompts(_PARENT, [_PARENT, _SPECIALIST], None),
                tab="instructions")

    assert r.status_code == 200
    assert 'action="/admin/prompts/prompt-persona-1/versions"' in r.text
    assert "Edit this persona on" not in r.text


def test_the_notice_no_longer_claims_the_persona_is_editable_here(client):  # noqa: F811
    """The old copy said "The persona prompt itself is editable on this page",
    which asserts the opposite of how resolution works. (The fieldset notice
    that used to repeat this is gone with the fieldset; the read-only persona
    block's own copy is what remains and it must name the pin's owner.)"""
    owner = {
        "profile_id": "parent-1",
        "display_name": "Orchard Merchant Data Assistant",
        "live": _PARENT["voice_config"],
    }
    r = _render(client, _ctx_with_prompts(_SPECIALIST, [_PARENT, _SPECIALIST], owner),
                tab="instructions")

    assert "editable on this page" not in r.text
    assert "holds the persona version" in r.text


# --- D10: the relationship must not depend on the rail's filter --------------
#
# `resolve_voice_config_owner` used to be handed the profile list AFTER the
# scoped-build `visible_agent_kinds()` gate had narrowed it. An owner filtered out
# of the rail therefore resolved to None: the notice disappeared, the persona
# banner disappeared, and the specialist's blank-but-editable voice card was
# left looking authoritative -- the exact reading that got reported against the
# client demo in the first place. The relationship is a property of the tenant's
# data, so it is now resolved against every profile in the environment, and only
# the LINK it produces is subject to the rail's filtering.


class _EnvQuery:
    """`select().eq().order().limit().execute()` over in-memory rows."""

    def __init__(self, rows):
        self._rows = list(rows)

    def select(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, n):
        return _EnvQuery(self._rows[:n])

    def eq(self, column, value):
        return _EnvQuery([r for r in self._rows if r.get(column) == value])

    def in_(self, *_a, **_k):
        return _EnvQuery([])

    def execute(self):
        rows = self._rows

        class _R:
            data = rows
        return _R()


class _EnvClient:
    def __init__(self, tables):
        self._tables = tables

    def table(self, name):
        return _EnvQuery(self._tables.get(name, []))


def test_owner_resolves_even_when_the_rail_filter_hides_the_parent():
    from admin.screens import agents as agents_screen

    parent = {**_PARENT, "agent_kind": "orchard", "environment": "staging"}
    specialist = {**_SPECIALIST, "agent_kind": "orchard_voice_kind",
                  "environment": "staging"}
    tables = {"agent_profiles": [parent, specialist]}

    with patch("admin.router.client_for", return_value=_EnvClient(tables)), \
            patch("core.deployment.scoped_build", return_value=True), \
            patch("admin.seed_agent_profiles.visible_agent_kinds",
                  return_value=["orchard_voice_kind"]):
        ctx = agents_screen.load_agents_context(
            "staging", None, profile_id=specialist["profile_id"],
        )

    # The parent is filtered OUT of the rail...
    assert [p["profile_id"] for p in ctx["profiles"]] == [specialist["profile_id"]]
    # ...and the relationship survives anyway.
    assert ctx["voice_config_owner"] is not None
    assert ctx["voice_config_owner"]["profile_id"] == "parent-1"
