"""endpoint_only external MCP servers are skipped by the tool resolver.

An endpoint-only external row (e.g. a partner's mock) supplies the MCP endpoint URL
to core.agent_profile.external_mcp_endpoint_for_kind, but must contribute NO
tools under MCP_TOOL_RESOLUTION — the agent's curated bundle provides the real
tools. See runtime/tools/_mcp_resolver.resolve_mcp_tools_for_profile.
"""
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from core.agent_profile import Profile


class _NoArgs(BaseModel):
    pass


def _pool_tool(name: str) -> StructuredTool:
    return StructuredTool.from_function(
        func=lambda: "ok", name=name, description=name, args_schema=_NoArgs)


def _profile(attachments) -> Profile:
    return Profile(profile_id="p-partner", agent_kind="sample_partner", slug="sample_partner_default",
                   environment="staging", mcp_attachments=attachments)


def _bundle(members):
    return {
        "mcp_server": {"mcp_server_id": "b1", "name": "__builtin__:sample_partner",
                       "source": "bundle", "context_fields": {}},
        "tool_subset": None,
        "position": 0,
        "member_tools": members,
    }


def _member(python_name: str):
    return {
        "tool": {"tool_id": f"id-{python_name}", "python_name": python_name,
                 "kind": "python",
                 "argument_schema": {"type": "object", "properties": {}}},
        "context_fields": None,
        "position": 0,
    }


def _endpoint_only_external():
    return {
        "mcp_server": {"mcp_server_id": "e1", "name": "partner-mock",
                       "source": "external", "endpoint": "http://mock-mcp.test/mcp",
                       "endpoint_only": True, "context_fields": {}},
        "tool_subset": None,
        "position": 1,
        "member_tools": [],
    }


def test_resolve_skips_endpoint_only_server(monkeypatch):
    from runtime.tools import _mcp_resolver

    calls = []

    def _never_call(*a, **k):
        calls.append((a, k))
        raise AssertionError(
            "build_mcp_tools must not be called for an endpoint_only server")

    monkeypatch.setattr(_mcp_resolver, "build_mcp_tools", _never_call)

    pool = [_pool_tool("get_transactions"), _pool_tool("render_chart")]
    profile = _profile([
        _bundle([_member("get_transactions")]),
        _endpoint_only_external(),
    ])

    tools = _mcp_resolver.resolve_mcp_tools_for_profile(profile, {}, pool)

    # Only the bundle's tool resolves; the endpoint-only external is skipped
    # entirely (build_mcp_tools never touched).
    assert [t.name for t in tools] == ["get_transactions"]
    assert calls == []
