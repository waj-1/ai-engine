"""usage_guidance: resolver metadata threading + catalog rendering."""
from __future__ import annotations

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from runtime.prompts._catalog import render_tool_catalog
from runtime.tools._mcp_resolver import resolve_mcp_tools_for_profile


class _NoArgs(BaseModel):
    pass


def _pool_tool(name, description="d"):
    return StructuredTool.from_function(
        func=lambda: "ok", name=name, description=description, args_schema=_NoArgs)


class _Prof:
    is_fallback = False
    profile_id = "p1"
    agent_kind = "business"

    def __init__(self, attachments):
        self.mcp_attachments = attachments


def _bundle_attachment(members, server_guidance=None, subset=None):
    return {
        "mcp_server": {"mcp_server_id": "m1", "name": "biz", "source": "bundle",
                       "usage_guidance": server_guidance},
        "tool_subset": subset,
        "member_tools": members,
    }


def test_bundle_python_tool_gets_row_guidance():
    pool = [_pool_tool("get_leads")]
    att = _bundle_attachment([{"tool": {"python_name": "get_leads", "kind": "python",
                                        "usage_guidance": "Active pipeline = stage NOT IN (won, lost)."}}])
    tools = resolve_mcp_tools_for_profile(_Prof([att]), {}, pool)
    assert (tools[0].metadata or {}).get("usage_guidance") == \
        "Active pipeline = stage NOT IN (won, lost)."


def test_tool_without_guidance_has_no_metadata_key():
    pool = [_pool_tool("get_revenue")]
    att = _bundle_attachment([{"tool": {"python_name": "get_revenue", "kind": "python",
                                        "usage_guidance": None}}])
    tools = resolve_mcp_tools_for_profile(_Prof([att]), {}, pool)
    assert "usage_guidance" not in (tools[0].metadata or {})


def test_catalog_renders_guidance_indented_and_collapsed():
    t = _pool_tool("get_leads", "list leads by stage")
    t.metadata = {"usage_guidance": "Active pipeline = stage NOT IN\n(won,  lost)."}
    out = render_tool_catalog([t, _pool_tool("get_revenue", "revenue totals")])
    assert "- `get_leads` — list leads by stage" in out
    assert "\n  ↳ Active pipeline = stage NOT IN (won, lost)." in out
    assert "↳" not in out.split("get_revenue")[1]  # no guidance line for the other


def test_catalog_guidance_never_format_mapped():
    t = _pool_tool("t1", "desc")
    t.metadata = {"usage_guidance": "use {curly} braces literally"}
    out = render_tool_catalog([t])
    assert "{curly}" in out


def test_external_server_guidance_applies_and_never_overwrites():
    from unittest.mock import patch

    ext_att = {
        "mcp_server": {"mcp_server_id": "x1", "name": "ext", "source": "external",
                       "usage_guidance": "server-level guidance"},
        "tool_subset": None,
    }
    with_own = _pool_tool("remote_a")
    with_own.metadata = {"usage_guidance": "per-tool wins"}
    bare = _pool_tool("remote_b")
    with patch("runtime.tools._mcp_resolver.build_mcp_tools",
               return_value=[with_own, bare]):
        tools = resolve_mcp_tools_for_profile(_Prof([ext_att]), {}, [])
    by_name = {t.name: t for t in tools}
    assert by_name["remote_a"].metadata["usage_guidance"] == "per-tool wins"
    assert by_name["remote_b"].metadata["usage_guidance"] == "server-level guidance"
