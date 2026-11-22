"""resolve_mcp_tools_for_profile — bundle members, subsets, external, dedupe."""
from typing import Any, Dict

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from core.agent_profile import Profile


class _NoArgs(BaseModel):
    pass


def _pool_tool(name: str) -> StructuredTool:
    return StructuredTool.from_function(
        func=lambda: "ok", name=name, description=name, args_schema=_NoArgs)


def _profile(attachments) -> Profile:
    return Profile(profile_id="p1", agent_kind="business", slug="business",
                   environment="dev", mcp_attachments=attachments)


def _bundle(members, tool_subset=None, server_cf=None):
    return {
        "mcp_server": {"mcp_server_id": "m1", "name": "Bundle", "source": "bundle",
                       "context_fields": server_cf or {}},
        "tool_subset": tool_subset,
        "position": 0,
        "member_tools": members,
    }


def _member(python_name, kind="python", context_fields=None, **row_extra):
    row = {"tool_id": f"id-{python_name}", "python_name": python_name, "kind": kind,
           "argument_schema": {"type": "object", "properties": {}}, **row_extra}
    return {"tool": row, "context_fields": context_fields, "position": 0}


def test_fallback_profile_returns_full_pool():
    from core.agent_profile import FALLBACK_PROFILE
    from runtime.tools._mcp_resolver import resolve_mcp_tools_for_profile

    pool = [_pool_tool("a"), _pool_tool("b")]
    assert resolve_mcp_tools_for_profile(FALLBACK_PROFILE, {}, pool) == pool


def test_bundle_python_members_come_from_pool_with_subset():
    from runtime.tools._mcp_resolver import resolve_mcp_tools_for_profile

    pool = [_pool_tool("get_clients"), _pool_tool("get_invoices"), _pool_tool("unrelated")]
    profile = _profile([_bundle(
        [_member("get_clients"), _member("get_invoices")],
        tool_subset=["get_clients"],
    )])
    tools = resolve_mcp_tools_for_profile(profile, {}, pool)
    assert [t.name for t in tools] == ["get_clients"]


def test_bundle_http_member_built_with_effective_context(monkeypatch):
    from runtime.tools import _mcp_resolver

    captured: Dict[str, Any] = {}

    def fake_build_http_tool(row, *, context_fields=None, ctx=None):
        captured["row"], captured["cf"], captured["ctx"] = row, context_fields, ctx
        return _pool_tool(row["python_name"])

    monkeypatch.setattr(_mcp_resolver, "build_http_tool", fake_build_http_tool)
    profile = _profile([_bundle(
        [_member("crm_lookup", kind="http",
                 context_fields={"businessId": "$ctx.member_biz"})],
        server_cf={"token": "$ctx.user_token", "businessId": "$ctx.server_biz"},
    )])
    ctx = {"user_token": "t"}
    tools = _mcp_resolver.resolve_mcp_tools_for_profile(profile, ctx, [])
    assert [t.name for t in tools] == ["crm_lookup"]
    # precedence: member overrides server
    assert captured["cf"] == {"token": "$ctx.user_token", "businessId": "$ctx.member_biz"}
    assert captured["ctx"] is ctx


def test_external_attachment_delegates_to_build_mcp_tools(monkeypatch):
    from runtime.tools import _mcp_resolver

    ext_server = {"mcp_server_id": "m2", "name": "ext", "source": "external",
                  "endpoint": "http://mock-mcp.test/mcp", "context_fields": {}}

    def fake_build_mcp_tools(server_row, ctx, tool_subset=None, **_):
        assert server_row is ext_server
        assert tool_subset == ["get-sales-summary"]
        return [_pool_tool("get-sales-summary")]

    monkeypatch.setattr(_mcp_resolver, "build_mcp_tools", fake_build_mcp_tools)
    profile = _profile([{"mcp_server": ext_server, "tool_subset": ["get-sales-summary"],
                         "position": 0, "member_tools": []}])
    tools = _mcp_resolver.resolve_mcp_tools_for_profile(profile, {}, [])
    assert [t.name for t in tools] == ["get-sales-summary"]


def test_duplicate_names_dedupe_first_wins_and_unknown_kind_skipped():
    from runtime.tools._mcp_resolver import resolve_mcp_tools_for_profile

    pool = [_pool_tool("shared")]
    profile = _profile([
        _bundle([_member("shared")]),
        _bundle([_member("shared"), _member("mystery_tool", kind="mystery")]),
    ])
    tools = resolve_mcp_tools_for_profile(profile, {}, pool)
    assert [t.name for t in tools] == ["shared"]  # one copy; unknown kind skipped
