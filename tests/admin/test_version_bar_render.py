"""Render test for the top-of-screen profile-version bar (Task 1.4).

Patches `admin.screens.agents.load_agents_context` wholesale so the route
renders `agents.html` with a fully-controlled context (bypassing the real
DB reads) — the same seam Task 1.2/1.3's context already exposes:
`active_profile`, `profile_versions`, `served_version_id`. The rest of the
keys are the minimal shape `load_agents_context` normally returns, filled
with empty defaults so the template's other (unrelated) sections render
without needing real data.
"""
from __future__ import annotations

from unittest.mock import patch

from tests.admin.conftest import client  # noqa: F401 — session-scoped superuser TestClient


def _minimal_context(active_profile, profile_versions, served_version_id):
    """Everything else `agents.html` dereferences at top level, filled with
    the emptiest values that still render — no DB round-trip involved."""
    return {
        "profiles": [active_profile],
        "top_level_profiles": [active_profile],
        "sub_profiles_by_parent": {},
        "active_profile": active_profile,
        "profile_versions": profile_versions,
        "served_version_id": served_version_id,
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


def test_agents_page_shows_version_bar(client):  # noqa: F811
    active_profile = {
        "profile_id": "p1", "agent_kind": "sales", "slug": "default",
        "display_name": "Sales", "status": "active",
    }
    profile_versions = [
        {"version_id": "v2", "version_number": 2, "promoted_at": "2026-07-20T10:00:00",
         "notes": None, "is_served": True},
    ]
    with patch(
        "admin.screens.agents.load_agents_context",
        return_value=_minimal_context(active_profile, profile_versions, "v2"),
    ):
        r = client.get("/admin/agents?profile_id=p1&kind=sales&tab=versions")

    assert r.status_code == 200
    assert "Save as new version" in r.text
    assert "Serving" in r.text  # served indicator
    assert "Serving: v2" in r.text
    assert 'action="/admin/agents/p1/pin-version"' in r.text
    assert 'action="/admin/agents/p1/versions"' in r.text
    assert "/admin/agents/p1/versions/v2/diff" in r.text


def test_agents_page_version_bar_empty_versions_shows_no_dropdown(client):  # noqa: F811
    active_profile = {
        "profile_id": "p1", "agent_kind": "sales", "slug": "default",
        "display_name": "Sales", "status": "active",
    }
    with patch(
        "admin.screens.agents.load_agents_context",
        return_value=_minimal_context(active_profile, [], None),
    ):
        r = client.get("/admin/agents?profile_id=p1&kind=sales&tab=versions")

    assert r.status_code == 200
    assert "Save as new version" in r.text
    assert "Serving: latest (default)" in r.text
    assert "No versions yet" in r.text
    # No pin <select> when there's nothing to pin.
    assert 'name="version_id"' not in r.text
