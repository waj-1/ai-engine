"""Render guard for the per-toolset action subset the Tools tab posts.

Since the B1 redesign the toolset list shows ATTACHED toolsets only, and the
chosen actions ride as hidden inputs in the SAME wire variant the old
checkbox grid emitted (the action-picker modal rewrites them): bundle MCPs
send `tool_subset_mode__{id}=list` + `tool_subset_all__{id}` OR one
`tool_subset_cb__{id}` per chosen action; external MCPs keep the
comma-separated `tool_subset__{id}` text field. The wire contract is asserted
in test_profile_mcp_attach_checkboxes.py — this file pins the pre-seeding the
form must render for an unchanged Save to round-trip exactly.
"""
from __future__ import annotations

import re
from unittest.mock import patch

from tests.admin.conftest import client  # noqa: F401 — session-scoped superuser TestClient

_BUNDLE = {
    "mcp_server_id": "m1",
    "name": "Sales Docs",
    "source": "bundle",
    "is_builtin": False,
}


def _context(tool_subset):
    active_profile = {
        "profile_id": "p1", "agent_kind": "sales", "slug": "default",
        "display_name": "Sales", "status": "active",
    }
    return {
        "profiles": [active_profile],
        "top_level_profiles": [active_profile],
        "sub_profiles_by_parent": {},
        "active_profile": active_profile,
        "profile_versions": [],
        "served_version_id": None,
        "prompts": [],
        "tools": [],
        "data_sources": [],
        "mcp_servers": [],
        "mcp_attachments": [
            {"mcp_server": _BUNDLE, "tool_subset": tool_subset,
             "position": 0, "member_tools": []},
        ],
        "attachable_mcps": [
            {**_BUNDLE, "tool_names": ["get_sales_summary", "export_csv"]},
        ],
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


def _render(client, tool_subset):  # noqa: F811
    with patch(
        "admin.screens.agents.load_agents_context",
        return_value=_context(tool_subset),
    ):
        return client.get("/admin/agents?profile_id=p1&kind=sales&tab=tools")


def _cb_checked(html: str, value: str) -> bool:
    """True when the action is pinned (posted as a tool_subset_cb__ value)."""
    return bool(re.search(
        r'<input type="hidden" name="tool_subset_cb__m1" value="%s"' % re.escape(value),
        html,
    ))


def _all_flag_checked(html: str) -> bool:
    return bool(re.search(
        r'<input type="hidden" name="tool_subset_all__m1" value="1"', html,
    ))


def test_bundle_renders_checkboxes_preseeded_from_subset(client):  # noqa: F811
    r = _render(client, ["get_sales_summary"])
    assert r.status_code == 200, r.text[:600]
    # The wire-variant marker must accompany the checkboxes, or the handler
    # falls back to the (absent) text field and reads the save as "all tools".
    assert 'name="tool_subset_mode__m1" value="list"' in r.text
    assert not _all_flag_checked(r.text)
    assert _cb_checked(r.text, "get_sales_summary")
    assert not _cb_checked(r.text, "export_csv")
    # No text field for a bundle server — the checkboxes replace it.
    assert 'name="tool_subset__m1"' not in r.text


def test_bundle_null_subset_preseeds_all_flag_and_every_box(client):  # noqa: F811
    r = _render(client, None)
    assert r.status_code == 200
    assert _all_flag_checked(r.text)
    # "All actions" posts no per-action pins (stored as NULL → follow the bundle).
    assert not _cb_checked(r.text, "get_sales_summary")
    assert not _cb_checked(r.text, "export_csv")
    # The picker still knows every action (catalog JSON for the modal).
    assert "get_sales_summary" in r.text and "export_csv" in r.text
    assert 'name="attach_mcp_ids" value="m1"' in r.text
