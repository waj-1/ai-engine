"""An external MCP server's tools are counted from its own list.

Connections read "Tool catalog (0) - No tools registered yet" and an agent
with an attached external server read "0 available right now", although
Test connection listed four tools and the agent called them at runtime: the
counts read only catalog rows (`agent_tools`), and an external server's tools
are not catalog rows. They are now read from the list the server returned
(the last one this process saw; a short live read otherwise).
"""
from __future__ import annotations

import pytest

from admin import external_mcp_tools as emt
from runtime.tools import _mcp_adapter

ENDPOINT = "https://tools.example.test/mcp"
SERVER = {"mcp_server_id": "srv-ext", "name": "parcel_tools", "source": "external",
          "is_builtin": False, "is_active": True, "endpoint": ENDPOINT,
          "endpoint_only": False, "tool_subset": None, "auth_env_var": None}
TOOLS = [{"name": n, "description": f"{n} desc"} for n in
         ("lookup_order", "refund_order", "track_parcel", "open_ticket")]


@pytest.fixture
def listed(monkeypatch):
    """What Test connection leaves behind: the server's tools/list cached."""
    monkeypatch.setattr("runtime.tools._http_adapter._classify_endpoint", lambda url: "public")
    monkeypatch.setitem(_mcp_adapter._TOOLS_CACHE, (ENDPOINT, None), (0.0, TOOLS))
    yield


def test_listed_tools_reads_the_last_listing(listed):
    out = emt.listed_tools([SERVER])
    assert [t["name"] for t in out["srv-ext"]] == [t["name"] for t in TOOLS]
    pinned = emt.listed_tools([{**SERVER, "tool_subset": ["refund_order"]}])
    assert [t["name"] for t in pinned["srv-ext"]] == ["refund_order"]


def test_never_listed_server_is_unknown_not_zero(monkeypatch):
    monkeypatch.setattr("runtime.tools._http_adapter._classify_endpoint", lambda url: "public")
    assert emt.listed_tools([SERVER]) == {"srv-ext": None}


def test_connections_catalog_counts_the_external_servers_tools(listed, monkeypatch):
    from admin import router

    monkeypatch.setattr(router, "_mcp_server_cards",
                        lambda env=None: [{**SERVER, "display_name": "Parcel tools",
                                           "listed_tools": emt.listed_tools([SERVER])["srv-ext"]}])
    monkeypatch.setattr(router, "_all_tool_rows", lambda env=None: [])
    monkeypatch.setattr(router, "_tool_groups", lambda env, servers, tools: [])
    ctx = router.build_integrations_context("staging")
    assert ctx["catalog_count"] == 4
    group = ctx["tool_groups"][0]
    assert group["server"]["mcp_server_id"] == "srv-ext"
    assert [t["python_name"] for t in group["tools"]][0] == "lookup_order"


def test_connections_page_renders_the_external_tools(client, listed, monkeypatch):
    from admin import router

    monkeypatch.setattr(router, "_mcp_server_cards",
                        lambda env=None: [{**SERVER, "display_name": "Parcel tools", "tool_count": 4,
                                           "type_label": "External", "used_by": [],
                                           "listed_tools": emt.listed_tools([SERVER])["srv-ext"]}])
    monkeypatch.setattr(router, "_all_tool_rows", lambda env=None: [])
    page = client.get("/admin/tools")
    assert page.status_code == 200
    assert "Tool catalog <span class=\"caption num\">(4)</span>" in page.text
    assert "No tools registered yet" not in page.text
    assert "track_parcel" in page.text


def test_agent_bound_count_includes_the_external_servers_tools(listed):
    from admin.screens.agents_views import _bound_count

    att = [{"mcp_server": SERVER, "tool_subset": None, "member_tools": []}]
    assert _bound_count(att)["count"] == 0                       # unknown without the list
    counted = _bound_count(att, emt.listed_tools([SERVER]))
    assert counted["count"] == 4 and counted["external"] is False
