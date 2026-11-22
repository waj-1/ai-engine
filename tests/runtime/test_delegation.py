"""ask_<slug> delegation tools — binding, depth guard, bounded loop."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage

from core.agent_profile import Profile
from runtime.agent import RuntimeAgent

_DESC = {"profile_id": "sub1", "slug": "business_reporting", "display_name": "Reporting",
         "description": "attribution, retention, multi-period comparisons",
         "agent_kind": "business"}


def _parent(delegates):
    return Profile(profile_id="p1", agent_kind="business", slug="business_default",
                   environment="staging", downstream_delegates=delegates)


def _agent(profile):
    a = RuntimeAgent.__new__(RuntimeAgent)
    a.agent_kind = profile.agent_kind
    a.profile = profile
    a.binding_fn = None
    a.llm = None
    return a


def _prepare(agent, ctx=None, pool=()):
    with patch("runtime.agent.resolve_mcp_tools_for_profile",
               side_effect=lambda p, c, pl: list(pl)):
        return agent.prepare(ctx or {"fallback_system_prompt": "sys"}, list(pool))


def test_prepare_binds_ask_tool_with_config_description():
    ready = _prepare(_agent(_parent([_DESC])))
    assert "ask_business_reporting" in ready["tools_by_name"]
    tool = ready["tools_by_name"]["ask_business_reporting"]
    assert "attribution" in tool.description


def test_depth_guard_no_nested_delegation():
    ready = _prepare(_agent(_parent([_DESC])), ctx={"fallback_system_prompt": "sys",
                                                    "_delegation_depth": 1})
    assert "ask_business_reporting" not in ready["tools_by_name"]


def test_no_delegates_no_tools():
    ready = _prepare(_agent(_parent([])))
    assert not [n for n in ready["tools_by_name"] if n.startswith("ask_")]


class _FakeLLM:
    """Scripted: first call returns a final AIMessage."""

    def bind_tools(self, _tools):
        return self

    def invoke(self, _msgs):
        return AIMessage(content="Report: WoW revenue +12%")


def test_delegate_run_returns_subagent_text():
    sub_profile = Profile(profile_id="sub1", agent_kind="business",
                          slug="business_reporting", environment="staging",
                          resolved_prompts={"system": "You are the reporting specialist."})
    ready = _prepare(_agent(_parent([_DESC])))
    tool = ready["tools_by_name"]["ask_business_reporting"]
    with patch("core.agent_profile.AgentProfileLoader.load_delegate", return_value=sub_profile), \
         patch("runtime.delegation.build_chat_model", return_value=_FakeLLM()), \
         patch("runtime.agent.resolve_mcp_tools_for_profile",
               side_effect=lambda p, c, pl: []):
        out = tool.func("compare this week vs last week")
    assert "WoW revenue +12%" in out


def test_delegate_run_missing_model_is_recoverable():
    sub_profile = Profile(profile_id="sub1", agent_kind="business",
                          slug="business_reporting", environment="staging")
    ready = _prepare(_agent(_parent([_DESC])))
    tool = ready["tools_by_name"]["ask_business_reporting"]
    with patch("core.agent_profile.AgentProfileLoader.load_delegate", return_value=sub_profile), \
         patch("runtime.delegation.build_chat_model", return_value=None), \
         patch("runtime.agent.resolve_mcp_tools_for_profile",
               side_effect=lambda p, c, pl: []):
        out = tool.func("anything")
    assert "unavailable" in out.lower()


class _ExplodingLLM:
    """Scripted: bind_tools succeeds, invoke always raises."""

    def bind_tools(self, _tools):
        return self

    def invoke(self, _msgs):
        raise RuntimeError("provider 503")


def test_delegate_run_model_error_is_recoverable():
    """A delegate LLM error (network/rate-limit/provider) must surface as a
    friendly string, not propagate out of the ask_ tool (Fix 2)."""
    sub_profile = Profile(profile_id="sub1", agent_kind="business",
                          slug="business_reporting", environment="staging",
                          resolved_prompts={"system": "You are the reporting specialist."})
    ready = _prepare(_agent(_parent([_DESC])))
    tool = ready["tools_by_name"]["ask_business_reporting"]
    with patch("core.agent_profile.AgentProfileLoader.load_delegate", return_value=sub_profile), \
         patch("runtime.delegation.build_chat_model", return_value=_ExplodingLLM()), \
         patch("runtime.agent.resolve_mcp_tools_for_profile",
               side_effect=lambda p, c, pl: []):
        out = tool.func("compare this week vs last week")
    assert isinstance(out, str)
    assert "error" in out.lower()
    assert "business_reporting" in out


def test_delegate_uses_its_own_agent_kind_binding():
    """Fix 1: the delegate resolves ITS agent_kind's registered binding
    (not None), so a delegate of a kind with a binding runs like a
    first-class agent of that kind."""
    from runtime.bindings import register_binding, BindingResult

    calls = []

    def _bound_kind_binding(profile, ctx):
        calls.append((profile.profile_id, ctx.get("_delegation_depth")))
        return BindingResult(system_addendum="DELEGATE BINDING ADDENDUM")

    register_binding("delegation_test_kind", _bound_kind_binding)

    desc = {"profile_id": "sub-bound", "slug": "bound_specialist",
            "display_name": "Bound Specialist", "description": "a delegate with a binding",
            "agent_kind": "delegation_test_kind"}
    sub_profile = Profile(profile_id="sub-bound", agent_kind="delegation_test_kind",
                          slug="bound_specialist", environment="staging",
                          resolved_prompts={"system": "Base prompt."})
    ready = _prepare(_agent(_parent([desc])))
    tool = ready["tools_by_name"]["ask_bound_specialist"]

    captured_prompt = {}

    class _CapturingLLM:
        def bind_tools(self, _tools):
            return self

        def invoke(self, msgs):
            captured_prompt["system"] = msgs[0].content
            return AIMessage(content="done")

    with patch("core.agent_profile.AgentProfileLoader.load_delegate", return_value=sub_profile), \
         patch("runtime.delegation.build_chat_model", return_value=_CapturingLLM()), \
         patch("runtime.agent.resolve_mcp_tools_for_profile",
               side_effect=lambda p, c, pl: []):
        out = tool.func("anything")

    assert out == "done"
    assert calls, "the delegate's registered binding was never invoked"
    assert "DELEGATE BINDING ADDENDUM" in captured_prompt["system"]


def test_prepare_delegation_collision_keeps_existing_pool_tool():
    """prepare()'s delegation-binding block (runtime/agent.py ~186-191):
    if a resolved/pool tool already claims the name ask_<slug>, the
    delegate tool must NOT overwrite it."""
    existing = SimpleNamespace(name="ask_foo", description="pool tool, not a delegate")
    desc = {"profile_id": "sub-foo", "slug": "foo", "display_name": "Foo",
            "description": "a delegate whose name collides with a pool tool",
            "agent_kind": "business"}
    ready = _prepare(_agent(_parent([desc])), pool=[existing])
    bound = ready["tools_by_name"]["ask_foo"]
    assert bound is existing
    assert bound.description == "pool tool, not a delegate"


def test_prepare_delegation_collision_only_first_duplicate_slug_binds():
    """Two delegates whose slugs sanitize to the same ask_ name: only the
    first one binds; the second is skipped (logged, not overwritten)."""
    desc_a = {"profile_id": "sub-a", "slug": "dup name", "display_name": "A",
              "description": "first delegate", "agent_kind": "business"}
    desc_b = {"profile_id": "sub-b", "slug": "dup!name", "display_name": "B",
              "description": "second delegate", "agent_kind": "business"}
    ready = _prepare(_agent(_parent([desc_a, desc_b])))
    ask_tools = [n for n in ready["tools_by_name"] if n == "ask_dup_name"]
    assert ask_tools == ["ask_dup_name"]
    assert ready["tools_by_name"]["ask_dup_name"].description.startswith(
        "first delegate")
