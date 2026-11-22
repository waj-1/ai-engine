"""RuntimeAgent honors a request-scoped agent_profile_versions pin.

Task 1.4: when `core.profile_version_resolver.get_profile_version()` returns a
version id (set by the eval/bake-off X-WAJ-Profile-Version middleware),
`RuntimeAgent.__init__` must load that immutable snapshot via
`AgentProfileLoader.load_version(...)` instead of the live/active profile.
With no pin, behaviour is unchanged (`AgentProfileLoader.load_active(...)`).
"""
from unittest.mock import patch

from core.agent_profile import Profile


def _profile(kind="sales"):
    return Profile(profile_id="p1", agent_kind=kind, slug="s", environment="staging",
                   model_main={"model_string": "m"}, model_light=None,
                   prompt_version_ids={}, tool_python_names=[], data_source_ids=[],
                   mcp_server_ids=[], mcp_attachments=[], escalation_policy={},
                   voice_config={}, memory_config={}, is_fallback=False)


def test_version_pin_uses_load_version():
    from runtime.agent import RuntimeAgent

    # RuntimeAgent is a plain (non-ABC) class — no __abstractmethods__ to
    # neutralize. `_build_llm` is stubbed so `__init__` doesn't try to build a
    # real LangChain chat model from the stub Profile's `model_main`; that
    # isolates the assertion to the load-path decision this task implements.
    with patch("core.profile_version_resolver.get_profile_version", return_value="v9"), \
         patch.object(RuntimeAgent, "_build_llm", return_value=None), \
         patch("core.agent_profile.AgentProfileLoader.load_version",
               return_value=_profile()) as lv, \
         patch("core.agent_profile.AgentProfileLoader.load_active") as la:
        RuntimeAgent(agent_kind="sales", environment="staging")
    lv.assert_called_once_with("v9", "staging")
    la.assert_not_called()


def test_no_pin_uses_load_active():
    from runtime.agent import RuntimeAgent

    with patch("core.profile_version_resolver.get_profile_version", return_value=None), \
         patch.object(RuntimeAgent, "_build_llm", return_value=None), \
         patch("core.agent_profile.AgentProfileLoader.load_version") as lv, \
         patch("core.agent_profile.AgentProfileLoader.load_active",
               return_value=_profile()) as la:
        RuntimeAgent(agent_kind="sales", environment="staging")
    la.assert_called_once()
    lv.assert_not_called()
