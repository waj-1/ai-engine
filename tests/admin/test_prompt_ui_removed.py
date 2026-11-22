"""Render test for Task 2.3 (profile-version-lifecycle, phase P2): the
per-prompt version selector/serve/history UI is gone from agents.html — the
profile-version bar (Task 1.4) is the one operator-facing version concept
now. The prompt card is a plain editor of the draft's `system` prompt.

Patches `admin.screens.agents.load_agents_context` wholesale (same seam as
`tests/admin/test_version_bar_render.py`) so the route renders `agents.html`
with a fully-controlled context, bypassing the real DB reads."""
from __future__ import annotations

from unittest.mock import patch

from tests.admin.conftest import client  # noqa: F401 — session-scoped superuser TestClient


def _minimal_context(active_profile, prompts):
    """Everything `agents.html` dereferences at top level, filled with the
    emptiest values that still render — mirrors
    `test_version_bar_render._minimal_context` plus a `prompts` list (this
    test's whole point is the prompt card, which that helper leaves empty)."""
    return {
        "profiles": [active_profile],
        "top_level_profiles": [active_profile],
        "sub_profiles_by_parent": {},
        "active_profile": active_profile,
        "profile_versions": [],
        "served_version_id": None,
        "prompts": prompts,
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


def test_agents_page_has_no_prompt_version_selector(client):  # noqa: F811
    active_profile = {
        "profile_id": "p1", "agent_kind": "sales", "slug": "default",
        "display_name": "Sales", "status": "active",
    }
    prompts = [{
        "prompt_id": "prompt-1", "agent_kind": "sales", "slug": "system",
        "content": "You are the WAJ sales assistant.",
    }]
    with patch(
        "admin.screens.agents.load_agents_context",
        return_value=_minimal_context(active_profile, prompts),
    ):
        r = client.get("/admin/agents?profile_id=p1&kind=sales&tab=instructions")

    assert r.status_code == 200
    # Prompt version selector/serve UI gone.
    assert "data-prompt-version-select" not in r.text
    assert "Serve this version" not in r.text
    assert "Serve version" not in r.text
    assert "data-prompt-version-summary" not in r.text
    assert "versions in history" not in r.text
    # The plain editor stays: the draft's system prompt text still renders,
    # and the save form (still posting to /admin/prompts/{id}/versions —
    # storage is unchanged) is present.
    assert "You are the WAJ sales assistant." in r.text
    assert 'action="/admin/prompts/prompt-1/versions"' in r.text
    assert "Save instructions" in r.text  # the tab's one Save


def test_agents_page_prompt_card_handles_missing_system_prompt(client):  # noqa: F811
    """No system prompt seeded → the empty-state message, no crash on the
    removed `versions_by_prompt`/`served_v`/`selected_v` context keys."""
    active_profile = {
        "profile_id": "p1", "agent_kind": "sales", "slug": "default",
        "display_name": "Sales", "status": "active",
    }
    with patch(
        "admin.screens.agents.load_agents_context",
        return_value=_minimal_context(active_profile, []),
    ):
        r = client.get("/admin/agents?profile_id=p1&kind=sales&tab=instructions")

    assert r.status_code == 200
    assert "No instructions have been set up" in r.text
    assert "data-prompt-version-select" not in r.text
    assert "Serve this version" not in r.text
