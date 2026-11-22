"""The /admin/tools tool catalog — the tool table that didn't exist.

The tool editor (/admin/tools/{tool_id}) was orphaned: no screen listed tools,
so it was reachable only by pasting a uuid. The MCP & Tools screen now carries
a flat catalog table over agent_tools, each row linking to its detail page,
with an honest empty-state nudge on usage_guidance (the only config-driven
steer the LLM gets) — today empty on every row.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from tests.admin.conftest import client  # noqa: F401 — session-scoped superuser TestClient


def _ctx(all_tools):
    return {
        "cards": [],
        "mcp_builtin": [],
        "mcp_bundles": [],
        "mcp_external": [],
        "mcp_internal": [],
        "all_tools": all_tools,
    }


_TOOLS = [
    {"tool_id": "t1", "python_name": "get_sales_summary", "display_name": "Sales summary",
     "agent_kind": "orchard", "additional_agent_kinds": [], "scope": "read",
     "kind": "python", "usage_guidance": None, "is_deprecated": False},
    {"tool_id": "t2", "python_name": "update_iban", "display_name": "Update IBAN",
     "agent_kind": "orchard", "additional_agent_kinds": [], "scope": "write",
     "kind": "python", "usage_guidance": None, "is_deprecated": True},
]


def test_all_tools_table_rows_link_to_detail(client):  # noqa: F811
    with patch("admin.router.build_integrations_context", return_value=_ctx(_TOOLS)):
        r = client.get("/admin/tools")
    assert r.status_code == 200, r.text[:600]
    assert "Tool catalog" in r.text
    assert 'href="/admin/tools/t1"' in r.text
    assert 'href="/admin/tools/t2"' in r.text
    assert "get_sales_summary" in r.text      # code name, as muted technical text
    assert "Retired" in r.text                # t2's flag is visible
    assert "Changes data" in r.text           # write scope, in plain words


def test_guidance_progress_counts_guided_tools(client):  # noqa: F811
    with patch("admin.router.build_integrations_context", return_value=_ctx(_TOOLS)):
        r = client.get("/admin/tools")
    assert r.status_code == 200
    assert "0 of 2 tools have usage guidance" in r.text
    assert 'id="tool-noguide"' in r.text  # "Missing guidance" filter


def test_guidance_progress_once_a_row_is_guided(client):  # noqa: F811
    guided = [dict(_TOOLS[0], usage_guidance="Active pipeline = stage NOT IN (won, lost).")]
    with patch("admin.router.build_integrations_context", return_value=_ctx(guided)):
        r = client.get("/admin/tools")
    assert r.status_code == 200
    assert "1 of 1 tools have usage guidance" in r.text


def test_tools_are_grouped_per_server_and_collapsed(client):  # noqa: F811
    server = {"mcp_server_id": "s1", "name": "__builtin__:orchard", "display_name": "Tools for Orchard"}
    ctx = dict(_ctx(_TOOLS), tool_groups=[{"server": server, "tools": _TOOLS[:1]},
                                          {"server": None, "tools": _TOOLS[1:]}])
    with patch("admin.router.build_integrations_context", return_value=ctx):
        r = client.get("/admin/tools")
    text = r.text
    assert text.count("<details data-group>") == 2      # collapsed (no `open`)
    assert "Tools for Orchard" in text
    assert "Not in any server" in text


def test_tool_groups_follow_bundle_membership():
    import admin.router as r

    members = [{"mcp_server_id": "s1", "tool_id": "t2", "position": 0}]
    builder = MagicMock()
    builder.select.return_value = builder
    builder.execute.return_value = MagicMock(data=members)
    fake = MagicMock()
    fake.table.return_value = builder
    with patch.object(r, "client_for", return_value=fake):
        groups = r._tool_groups("staging", [{"mcp_server_id": "s1", "name": "b"}], _TOOLS)
    assert [g["server"]["mcp_server_id"] if g["server"] else None for g in groups] == ["s1", None]
    assert [t["tool_id"] for t in groups[0]["tools"]] == ["t2"]
    assert [t["tool_id"] for t in groups[1]["tools"]] == ["t1"]


def test_tool_rows_label_known_writers_as_changing_data(monkeypatch):
    """A tool known to change data (sync_tools.WRITE_TOOL_NAMES) never reads
    as READ, even before the next boot sync rewrites its stored scope."""
    import admin.router as r
    from admin import sync_tools

    monkeypatch.setattr(sync_tools, "WRITE_TOOL_NAMES", {"send_sample_link"})
    rows = [{"tool_id": "x", "python_name": "send_sample_link", "scope": "read"},
            {"tool_id": "y", "python_name": "get_sample_summary", "scope": "read"}]
    builder = MagicMock()
    builder.select.return_value = builder
    builder.order.return_value = builder
    builder.execute.return_value = MagicMock(data=rows)
    ok_client = MagicMock()
    ok_client.table.return_value = builder
    with patch.object(r, "get_service_client", return_value=ok_client):
        out = {t["python_name"]: t["scope"] for t in r._all_tool_rows(None)}
    assert out == {"send_sample_link": "write", "get_sample_summary": "read"}


def test_all_tool_rows_helper_includes_deprecated_and_degrades():
    """_all_tool_rows keeps deprecated rows (flagged, not hidden) and returns
    [] on any client failure instead of 500-ing the screen."""
    import admin.router as r

    builder = MagicMock()
    builder.select.return_value = builder
    builder.order.return_value = builder
    builder.execute.return_value = MagicMock(data=list(_TOOLS))
    ok_client = MagicMock()
    ok_client.table.return_value = builder

    with patch.object(r, "get_service_client", return_value=ok_client):
        rows = r._all_tool_rows(None)
    assert [t["python_name"] for t in rows] == ["get_sales_summary", "update_iban"]

    boom = MagicMock()
    boom.table.side_effect = RuntimeError("db down")
    with patch.object(r, "get_service_client", return_value=boom):
        assert r._all_tool_rows(None) == []
