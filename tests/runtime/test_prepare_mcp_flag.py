"""prepare() resolves tools via MCP attachments unconditionally (flag retired)."""
from typing import List

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from core.agent_profile import Profile


class _NoArgs(BaseModel):
    pass


def _tool(name: str) -> StructuredTool:
    return StructuredTool.from_function(
        func=lambda: "ok", name=name, description=name, args_schema=_NoArgs)


@pytest.fixture()
def agent(monkeypatch):
    import runtime.agent as agent_mod

    profile = Profile(
        profile_id="p1", agent_kind="business", slug="business", environment="dev",
        model_main={"provider": "fake", "model_name": "fake"},
        # tool_python_names is legacy and no longer consulted; the profile's
        # MCP attachment (binding tool_b) is what decides the bound set.
        tool_python_names=["tool_a"],
        mcp_attachments=[{
            "mcp_server": {"mcp_server_id": "m1", "name": "B", "source": "bundle",
                           "context_fields": {}},
            "tool_subset": None, "position": 0,
            "member_tools": [{"tool": {"tool_id": "t-b", "python_name": "tool_b",
                                       "kind": "python"},
                              "context_fields": None, "position": 0}],
        }],
    )
    monkeypatch.setattr(agent_mod.AgentProfileLoader, "load_active",
                        classmethod(lambda cls, kind, env=None: profile))
    monkeypatch.setattr(agent_mod, "get_binding", lambda kind: None)
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda model: object())
    return agent_mod.RuntimeAgent("business")


def _prepare_tools(agent) -> List[str]:
    ready = agent.prepare({"fallback_system_prompt": ""}, [_tool("tool_a"), _tool("tool_b")])
    return sorted(ready["tools_by_name"].keys())


def test_prepare_binds_mcp_attachment_tools(agent, monkeypatch):
    # MCP resolution is now the only path: the bound set comes from the
    # profile's MCP attachment (tool_b), never the legacy whitelist (tool_a).
    monkeypatch.delenv("MCP_TOOL_RESOLUTION", raising=False)
    assert _prepare_tools(agent) == ["tool_b"]


def test_prepare_mcp_resolution_is_flag_independent(agent, monkeypatch):
    # The retired flag has no effect: setting it changes nothing.
    monkeypatch.setenv("MCP_TOOL_RESOLUTION", "1")
    assert _prepare_tools(agent) == ["tool_b"]


def test_prepare_logs_error_when_prompt_mandates_unbound_tool(agent, caplog):
    import logging
    # tool_a is in the pool but NOT bound (only tool_b binds) — a prompt that
    # tells the model to call it is the #1032 mismatch; prepare() must log it.
    caplog.set_level(logging.ERROR)
    ready = agent.prepare(
        {"fallback_system_prompt": "For HOW-TO you MUST call `tool_a` first."},
        [_tool("tool_a"), _tool("tool_b")],
    )
    assert sorted(ready["tools_by_name"].keys()) == ["tool_b"]
    assert any("tool_a" in r.getMessage()
               for r in caplog.records if r.levelno >= logging.ERROR)


def test_prepare_no_error_when_mandated_tool_is_bound(agent, caplog):
    import logging
    caplog.set_level(logging.ERROR)
    agent.prepare({"fallback_system_prompt": "Use `tool_b` to answer."},
                  [_tool("tool_a"), _tool("tool_b")])
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
