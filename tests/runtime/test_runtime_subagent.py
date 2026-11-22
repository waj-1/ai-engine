"""Tests for RuntimeAgent sub-agent resolution fallback paths.

Covers the two seams left untested by tests/runtime/test_runtime.py:

- `resolve_subagent_prompt` returning the caller-supplied fallback when the
  sub-profile lookup raises.
- `get_subagent_llm` returning None when the active sub-profile is a fallback
  profile, and None when the sub-profile lookup raises.

Constructed the same way as test_runtime.py: patch
`runtime.agent.AgentProfileLoader.load_active` and `runtime.agent.build_chat_model`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


@pytest.fixture(autouse=True)
def _stub_google_key():
    prev = os.environ.get("GOOGLE_API_KEY")
    os.environ["GOOGLE_API_KEY"] = "test-google"
    yield
    if prev is None:
        os.environ.pop("GOOGLE_API_KEY", None)
    else:
        os.environ["GOOGLE_API_KEY"] = prev


def test_resolve_subagent_prompt_falls_back_when_lookup_raises():
    """When the sub-profile lookup raises, resolve_subagent_prompt must
    swallow the error and return the caller's fallback string (the
    file-system prompt) instead of crashing the parent."""
    from runtime.agent import RuntimeAgent
    from core.agent_profile import FALLBACK_PROFILE

    # Construct the parent agent with a fallback profile (no model → no
    # build_chat_model call needed during __init__).
    with patch(
        "runtime.agent.AgentProfileLoader.load_active",
        return_value=FALLBACK_PROFILE,
    ):
        agent = RuntimeAgent("parent_kind")

    # Now make the SUB-profile lookup raise. The except branch in
    # resolve_subagent_prompt sets prompt_version_id=None, so the resolver
    # returns the fallback without touching the DB.
    with patch(
        "runtime.agent.AgentProfileLoader.load_active",
        side_effect=RuntimeError("simulated profile lookup failure"),
    ):
        out = agent.resolve_subagent_prompt(
            sub_kind="sales",
            fallback="FILE SYSTEM FALLBACK PROMPT",
        )

    assert out == "FILE SYSTEM FALLBACK PROMPT"


def test_resolve_subagent_prompt_fallback_applies_variables():
    """On the fallback path, the supplied variables are still substituted
    via PromptResolver.resolve's str.format path."""
    from runtime.agent import RuntimeAgent
    from core.agent_profile import FALLBACK_PROFILE

    with patch(
        "runtime.agent.AgentProfileLoader.load_active",
        return_value=FALLBACK_PROFILE,
    ):
        agent = RuntimeAgent("parent_kind")

    with patch(
        "runtime.agent.AgentProfileLoader.load_active",
        side_effect=RuntimeError("simulated profile lookup failure"),
    ):
        out = agent.resolve_subagent_prompt(
            sub_kind="sales",
            fallback="Hello {locale}",
            variables={"locale": "ar"},
        )

    assert out == "Hello ar"


def test_get_subagent_llm_returns_none_for_fallback_profile():
    """get_subagent_llm returns None when the resolved sub-profile is a
    fallback profile — the caller should use its own self.llm."""
    from runtime.agent import RuntimeAgent
    from core.agent_profile import FALLBACK_PROFILE

    with patch(
        "runtime.agent.AgentProfileLoader.load_active",
        return_value=FALLBACK_PROFILE,
    ):
        agent = RuntimeAgent("parent_kind")

    # Sub-profile lookup returns a fallback profile (is_fallback=True).
    # build_chat_model must NOT be called in this branch.
    with patch(
        "runtime.agent.AgentProfileLoader.load_active",
        return_value=FALLBACK_PROFILE,
    ), patch("runtime.agent.build_chat_model") as build_mock:
        out = agent.get_subagent_llm("sales")

    assert out is None
    build_mock.assert_not_called()


def test_get_subagent_llm_returns_none_on_exception():
    """get_subagent_llm swallows any lookup/build error and returns None."""
    from runtime.agent import RuntimeAgent
    from core.agent_profile import FALLBACK_PROFILE

    with patch(
        "runtime.agent.AgentProfileLoader.load_active",
        return_value=FALLBACK_PROFILE,
    ):
        agent = RuntimeAgent("parent_kind")

    with patch(
        "runtime.agent.AgentProfileLoader.load_active",
        side_effect=RuntimeError("simulated profile lookup failure"),
    ):
        out = agent.get_subagent_llm("sales")

    assert out is None


def test_get_subagent_llm_builds_model_for_non_fallback_profile():
    """Positive control: a non-fallback sub-profile with a model_main is
    built via build_chat_model and that instance is returned."""
    from runtime.agent import RuntimeAgent
    from core.agent_profile import FALLBACK_PROFILE, Profile

    with patch(
        "runtime.agent.AgentProfileLoader.load_active",
        return_value=FALLBACK_PROFILE,
    ):
        agent = RuntimeAgent("parent_kind")

    sub_profile = Profile(
        profile_id="00000000-0000-0000-0000-000000000002",
        agent_kind="sales",
        slug="default",
        environment="dev",
        model_main={"provider": "google_genai", "model_string": "gemini-2.5-pro"},
        is_fallback=False,
    )
    built = MagicMock(name="built_llm")

    with patch(
        "runtime.agent.AgentProfileLoader.load_active",
        return_value=sub_profile,
    ), patch("runtime.agent.build_chat_model", return_value=built) as build_mock:
        out = agent.get_subagent_llm("sales")

    assert out is built
    build_mock.assert_called_once_with(sub_profile.model_main)
