"""The Agents screen's "Bound at runtime" panel tells the truth about binding.

The runtime binds tools via `resolve_mcp_tools_for_profile` over the profile's
`agent_profile_mcp_servers` attachments narrowed by each attachment's
`tool_subset` (runtime/tools/_mcp_resolver.py). `agent_profiles.tool_ids` is
read by NOTHING at runtime — yet the Tools card used to render tool_ids as its
primary checkbox table under a KPI hint claiming "Unmapped tools never reach
the LLM". Live incident this caused: the orchard voice profile showed 16 tools
while serving 13, hiding a missing get_transactions.

These tests pin the discriminating case: 16 legacy tool_ids, an attachment
subset of 13 → the panel must show 13 bound chips and "Not bound: 3". A panel
computed from tool_ids would show 16 and 0 — i.e. these fail against the old
rendering, not just pass against the new one.
"""
from __future__ import annotations

import re
from unittest.mock import patch

from tests.admin.conftest import client  # noqa: F401 — session-scoped superuser TestClient

from admin.screens.agents import build_bound_tools_panel

# ---------------------------------------------------------------------------
# Fixture data: 16 tools, one bundle attachment whose subset pins 13 of them.
# ---------------------------------------------------------------------------

_ALL_NAMES = [f"tool_{i:02d}" for i in range(1, 17)]          # tool_01..tool_16
_BOUND_NAMES = _ALL_NAMES[:13]                                 # 13 bind
_UNBOUND_NAMES = _ALL_NAMES[13:]                               # 3 do not

_ALL_TOOLS = [
    {"tool_id": f"t{i:02d}", "python_name": name, "display_name": name.title()}
    for i, name in enumerate(_ALL_NAMES, start=1)
]
_TOOL_IDS = [t["tool_id"] for t in _ALL_TOOLS]                 # legacy list: all 16

_ATTACHMENT = {
    "mcp_server": {
        "mcp_server_id": "m1", "name": "__builtin__:orchard",
        "source": "bundle", "is_builtin": True,
    },
    "tool_subset": list(_BOUND_NAMES),
    "position": 0,
    "member_tools": [
        {"tool": dict(t), "context_fields": None, "position": i}
        for i, t in enumerate(_ALL_TOOLS)
    ],
}


# ---------------------------------------------------------------------------
# Unit: the panel builder mirrors the resolver's subset semantics.
# ---------------------------------------------------------------------------


def test_panel_reflects_subset_not_tool_ids():
    """16 tool_ids + subset of 13 → 13 bound, 3 legacy-not-bound.

    THE discriminator: a panel derived from tool_ids would report 16/0.
    """
    panel = build_bound_tools_panel([_ATTACHMENT], _TOOL_IDS, _ALL_TOOLS)

    assert panel["bound_count"] == 13
    assert len(panel["groups"]) == 1
    chip_names = [t["python_name"] for t in panel["groups"][0]["tools"]]
    assert chip_names == _BOUND_NAMES
    # Every chip carries the tool_id needed for its /admin/tools/{id} link.
    assert all(t["tool_id"] for t in panel["groups"][0]["tools"])
    assert panel["legacy_not_bound"] == _UNBOUND_NAMES


def test_panel_null_subset_binds_every_member():
    att = {**_ATTACHMENT, "tool_subset": None}
    panel = build_bound_tools_panel([att], _TOOL_IDS, _ALL_TOOLS)
    assert panel["bound_count"] == 16
    assert panel["legacy_not_bound"] == []


def test_panel_external_and_endpoint_only_attachments_reported_not_enumerated():
    """Externals can't be enumerated statically (the resolver's helper omits
    them); the panel must surface them as such, not silently drop them."""
    atts = [
        {"mcp_server": {"mcp_server_id": "e1", "name": "orchard-mock",
                        "source": "external"},
         "tool_subset": ["login"], "position": 0, "member_tools": []},
        {"mcp_server": {"mcp_server_id": "e2", "name": "orchard-endpoint",
                        "source": "external", "endpoint_only": True},
         "tool_subset": None, "position": 1, "member_tools": []},
    ]
    panel = build_bound_tools_panel(atts, [], [])
    assert panel["groups"] == []
    assert panel["bound_count"] == 0
    names = {x["name"]: x for x in panel["external"]}
    assert set(names) == {"orchard-mock", "orchard-endpoint"}
    assert names["orchard-mock"]["subset"] == ["login"]
    assert names["orchard-endpoint"]["endpoint_only"] is True


def test_panel_no_attachments_means_zero_bound():
    panel = build_bound_tools_panel([], _TOOL_IDS, _ALL_TOOLS)
    assert panel["bound_count"] == 0
    assert panel["groups"] == [] and panel["external"] == []
    # Every legacy tool is then not-bound — the pre-panel UI showed 16 "mapped".
    assert len(panel["legacy_not_bound"]) == 16


# ---------------------------------------------------------------------------
# Render: the page shows the bound chips, the not-bound count, and no longer
# claims tool_ids gates the LLM.
# ---------------------------------------------------------------------------


def _context():
    active_profile = {
        "profile_id": "p1", "agent_kind": "orchard", "slug": "orchard_voice",
        "display_name": "Orchard Voice", "status": "active",
        "tool_ids": list(_TOOL_IDS),
    }
    return {
        "profiles": [active_profile],
        "top_level_profiles": [active_profile],
        "sub_profiles_by_parent": {},
        "active_profile": active_profile,
        "profile_versions": [],
        "served_version_id": None,
        "prompts": [],
        "tools": [dict(t) for t in _ALL_TOOLS],
        "data_sources": [],
        "mcp_servers": [],
        "mcp_attachments": [_ATTACHMENT],
        "attachable_mcps": [],
        "bound_tools": build_bound_tools_panel([_ATTACHMENT], _TOOL_IDS, _ALL_TOOLS),
        "skill_attachments": [],
        "attachable_skills": [],
        "downstream_candidates": [],
        "e2e_runs": [],
        "mapped": {
            "tool_ids": set(_TOOL_IDS), "source_ids": set(),
            "mcp_ids": set(), "downstream_ids": set(),
        },
        "all_models": [],
        "input_schema": None,
        "input_validators": {},
    }


def _render(client, tab="tools"):  # noqa: F811
    with patch(
        "admin.screens.agents.load_agents_context", return_value=_context()
    ):
        return client.get(f"/admin/agents?profile_id=p1&kind=orchard&tab={tab}")


def test_bound_panel_renders_13_chips_and_not_bound_3(client):  # noqa: F811
    r = _render(client)
    assert r.status_code == 200, r.text[:600]

    # The attached toolset's chips are exactly what binds (title = the
    # python name; the visible label is the plain display name).
    chip_names = re.findall(r'class="chip chip-neutral" title="([a-z0-9_]+)"', r.text)
    assert sorted(set(chip_names)) == sorted(_BOUND_NAMES)
    assert "3 listed tools never reach the agent" in r.text
    for name in _UNBOUND_NAMES:
        assert name in r.text  # named, not just counted


def test_false_kpi_hint_is_gone_and_truth_is_stated(client):  # noqa: F811
    r = _render(client)
    assert r.status_code == 200
    assert "Unmapped tools never reach the LLM" not in r.text
    # The Tools tab states the bound count from the attachments, not tool_ids.
    assert "<strong data-bound-count>13</strong> available right now" in r.text
    overview = _render(client, "overview")
    assert "<strong>13</strong> actions" in overview.text


def test_legacy_surfaces_are_labelled_legacy(client):  # noqa: F811
    r = _render(client)
    assert r.status_code == 200
    # tool_ids / mcp_server_ids: folded under Advanced, honestly labelled,
    # still editable.
    assert "legacy lists the runtime no longer reads" in r.text
    assert "Legacy tool list" in r.text
    assert 'name="tool_ids"' in r.text
    assert "Legacy server list" in r.text
    assert "does not give the agent a tool" in r.text
    # Toolsets (the attachments) claim the binding.
    assert "Only attached toolsets give it actions" in r.text
