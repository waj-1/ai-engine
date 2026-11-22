"""Render guard for the MCP-attachment subset editor on the Agents screen.

`agent_profile_mcp_servers.tool_subset` is NULLABLE and NULL is the normal,
documented value for "all tools" — `admin/mcp_membership.py` writes
`subset or None`, the placeholder on the input literally reads "blank = all
tools", and the read-only table above the editor renders NULL as an
"all tools" chip.

The editable input rendered that same value with
`(current_att.tool_subset if current_att else [])|join(', ')`, which only
guards the attachment being ABSENT — not its `tool_subset` being None. Jinja's
`join` does `list(value)`, so an attached MCP with no pinned subset raised
`TypeError: 'NoneType' object is not iterable` and 500'd the whole Agents
screen (seen on the orchard demo: two `agent_profile_mcp_servers` rows with
NULL `tool_subset` made /admin/agents unreachable).
"""
from __future__ import annotations

import re
from unittest.mock import patch

from tests.admin.conftest import client  # noqa: F401 — session-scoped superuser TestClient

_SERVER = {
    "mcp_server_id": "m1",
    "name": "Orchard Ops",
    "source": "external",
    "is_builtin": False,
}


def _context(tool_subset):
    """Minimal `load_agents_context` shape with one attached MCP whose
    `tool_subset` is under test."""
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
        "versions_by_prompt": {},
        "tools": [],
        "data_sources": [],
        "mcp_servers": [],
        "mcp_attachments": [
            {"mcp_server": _SERVER, "tool_subset": tool_subset,
             "position": 0, "member_tools": []},
        ],
        "attachable_mcps": [
            {**_SERVER, "tool_names": ["get_sales_summary", "export_csv"]},
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


def _subset_input_value(html: str) -> str:
    """The rendered `value="..."` of the tool_subset input for server m1."""
    match = re.search(
        r'name="tool_subset__m1"\s+value="([^"]*)"', html,
    )
    assert match, "tool_subset input for m1 not found in rendered page"
    return match.group(1)


def test_null_tool_subset_renders_empty_input_not_500(client):  # noqa: F811
    """NULL subset ("all tools") must render an empty input, not crash."""
    r = _render(client, None)

    assert r.status_code == 200, r.text[:600]
    # Blank value + the "all tools" placeholder is the intended empty state.
    assert _subset_input_value(r.text) == ""


def test_populated_tool_subset_still_renders_joined(client):  # noqa: F811
    """The non-NULL path is unchanged: names join into the input value."""
    r = _render(client, ["get_sales_summary", "export_csv"])

    assert r.status_code == 200
    assert _subset_input_value(r.text) == "get_sales_summary, export_csv"
