"""MCP-resolution correctness: name, order, and schema of the bound tool list.

resolve_mcp_tools_for_profile binds a profile's tools from its MCP attachments
over the agent's factory pool. These tests lock the invariants: bound order
follows the factory-pool order (via member `position` from ordered_membership,
not the tool_subset allow-set), per-tool schema is preserved, and HTTP-authored
members are appended after the Python tools in deterministic name-sorted order.
"""
from typing import List

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from admin.builtin_mcp_membership import ordered_membership
from core.agent_profile import Profile
from runtime.tools._mcp_resolver import resolve_mcp_tools_for_profile


class _PerfArgs(BaseModel):
    days: int = Field(7, description="lookback days")


class _ClientsArgs(BaseModel):
    query: str = Field(..., description="search")


def _pool() -> List[StructuredTool]:
    # Pool order [get_clients, get_business_performance] is the REVERSE of
    # alphabetical ([get_business_performance, get_clients]) so an accidental
    # name-sort in ordered_membership would break the pool-order guarantee here
    # (the MCP path must follow factory-pool order, not alphabetical order).
    return [
        StructuredTool.from_function(func=lambda query="": "c", name="get_clients",
                                     description="clients", args_schema=_ClientsArgs),
        StructuredTool.from_function(func=lambda days=7: "p", name="get_business_performance",
                                     description="perf", args_schema=_PerfArgs),
    ]


def _mcp_tools(pool, tool_python_names):
    # Build built-in-bundle membership from the pool via the shared helper,
    # then attach with tool_subset == tool_python_names (what the seeder pins).
    tool_rows = [{"python_name": t.name, "tool_id": f"id-{t.name}", "kind": "python"}
                 for t in pool]
    members = ordered_membership([t.name for t in pool], tool_rows)
    attachment = {
        "mcp_server": {"mcp_server_id": "m", "name": "__builtin__:business",
                       "source": "bundle", "context_fields": {}},
        "tool_subset": tool_python_names,
        "position": 0,
        "member_tools": [{"tool": {"tool_id": m["tool_id"], "python_name": m["python_name"],
                                   "kind": m["kind"]},
                          "context_fields": None, "position": m["position"]}
                         for m in members],
    }
    prof = Profile(profile_id="p", agent_kind="business", slug="business",
                   environment="dev", mcp_attachments=[attachment])
    return resolve_mcp_tools_for_profile(prof, {}, pool)


@pytest.mark.parametrize("subset", [
    ["get_clients", "get_business_performance"],   # full set (pool order)
    ["get_clients"],                               # partial whitelist
    ["get_business_performance", "get_clients"],   # subset order (alphabetical) differs from pool order
])
def test_mcp_resolution_names_order_and_schema(subset):
    pool = _pool()
    mcp = _mcp_tools(pool, subset)

    # 1. Bound order = pool order filtered by the subset allow-set. tool_subset
    #    is an allow-set, not an order source, so the alphabetical third case
    #    still resolves in pool order.
    expected = [t.name for t in pool if t.name in set(subset)]
    assert [t.name for t in mcp] == expected, f"order/name mismatch for subset={subset}"

    # 2. Per-name schema is preserved from the factory pool tool.
    pool_schema = {t.name: t.args_schema.model_json_schema() for t in pool}
    mcp_schema = {t.name: t.args_schema.model_json_schema() for t in mcp}
    assert mcp_schema == {n: pool_schema[n] for n in expected}


def test_non_fallback_profile_with_no_attachments_binds_zero_tools():
    # A configured (non-fallback) profile with no MCP attachments binds no
    # tools — the seeder pins an explicit bundle per profile so this never
    # occurs in seeded data, but the resolver's contract is zero, not "all".
    pool = _pool()
    prof = Profile(profile_id="p", agent_kind="business", slug="business",
                   environment="dev", mcp_attachments=[])
    assert resolve_mcp_tools_for_profile(prof, {}, pool) == []


def test_http_members_appended_name_sorted_after_python():
    """HTTP-authored tools (not in the factory pool) are appended AFTER python
    tools in DETERMINISTIC name-sorted order.
    """
    pool = _pool()  # [get_clients, get_business_performance] — python factory pool

    def _http_row(name):
        return {"tool_id": f"id-{name}", "python_name": name, "kind": "http",
                "endpoint_url": "https://example.test/x", "http_method": "POST",
                "argument_schema": {"type": "object", "properties": {}}}

    members = ordered_membership(
        [t.name for t in pool],
        [{"python_name": "get_clients", "tool_id": "id-get_clients", "kind": "python"},
         _http_row("zzz_report"), _http_row("aaa_report")],
    )
    # membership: the python pool tool first, then http tools name-sorted (aaa, zzz)
    assert [m["python_name"] for m in members] == ["get_clients", "aaa_report", "zzz_report"]

    attachment = {
        "mcp_server": {"mcp_server_id": "m", "name": "__builtin__:business",
                       "source": "bundle", "context_fields": {}},
        "tool_subset": None,  # expose all members
        "position": 0,
        "member_tools": [
            {"tool": {"tool_id": m["tool_id"], "python_name": m["python_name"],
                      "kind": m["kind"], "endpoint_url": "https://example.test/x",
                      "http_method": "POST",
                      "argument_schema": {"type": "object", "properties": {}}},
             "context_fields": None, "position": m["position"]}
            for m in members
        ],
    }
    prof = Profile(profile_id="p", agent_kind="business", slug="business",
                   environment="dev", mcp_attachments=[attachment])
    tools = resolve_mcp_tools_for_profile(prof, {}, pool)
    assert [t.name for t in tools] == ["get_clients", "aaa_report", "zzz_report"]
