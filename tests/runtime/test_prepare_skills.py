"""RuntimeAgent.prepare() skill injection — catalog, always_on, read_skill binding."""
from __future__ import annotations

from unittest.mock import patch

from core.agent_profile import Profile
from runtime.agent import RuntimeAgent

_ENTRY = {"skill": {"slug": "refund-note", "description": "format a Refund note",
                    "always_on": False},
          "version": {"version_number": 1, "content": "Refund body"},
          "resources": [], "linked_data_sources": [], "position": 0,
          "scope": "platform"}
_ALWAYS_ON = {"skill": {"slug": "safety", "description": "safety rails",
                        "always_on": True},
              "version": {"version_number": 1, "content": "ALWAYS check allergies"},
              "resources": [], "linked_data_sources": [], "position": 1,
              "scope": "platform"}


def _agent_with(entries):
    """Build a RuntimeAgent without touching the DB."""
    profile = Profile(profile_id="p1", agent_kind="support_bot", slug="support_bot_default",
                      environment="staging", skill_attachments=entries)
    agent = RuntimeAgent.__new__(RuntimeAgent)
    agent.agent_kind = "support_bot"
    agent.profile = profile
    agent.binding_fn = None
    agent.llm = None
    return agent


def _prepare(agent, ctx=None):
    # Bypass MCP resolution (no attachments configured) — pool passes through
    # only for fallback profiles, so patch the resolver to identity.
    with patch("runtime.agent.resolve_mcp_tools_for_profile",
               side_effect=lambda p, c, pool: list(pool)):
        return agent.prepare(ctx or {"fallback_system_prompt": "You are a clinician."}, [])


def test_prepare_injects_catalog_and_binds_read_skill():
    agent = _agent_with([_ENTRY])
    ready = _prepare(agent)
    assert "## Skills" in ready["system_prompt"]
    assert "- `refund-note` — format a Refund note" in ready["system_prompt"]
    assert "Refund body" not in ready["system_prompt"]          # NOT inlined
    assert "read_skill" in ready["tools_by_name"]
    assert any(t.name == "read_skill" for t in ready["tools"])


def test_prepare_inlines_always_on_bodies():
    agent = _agent_with([_ENTRY, _ALWAYS_ON])
    ready = _prepare(agent)
    assert "ALWAYS check allergies" in ready["system_prompt"]
    assert "Refund body" not in ready["system_prompt"]


def test_prepare_no_skills_is_noop():
    agent = _agent_with([])
    with patch("core.skills.resolve_tenant_skills", return_value=[]):
        ready = _prepare(agent)
    assert "## Skills" not in ready["system_prompt"]
    assert "read_skill" not in ready["tools_by_name"]


def test_prepare_fills_available_skills_token():
    agent = _agent_with([_ENTRY])
    ctx = {"fallback_system_prompt": "Intro.\n{available_skills}\nOutro."}
    ready = _prepare(agent, ctx)
    assert "- `refund-note` —" in ready["system_prompt"]
    assert "{available_skills}" not in ready["system_prompt"]


def test_prepare_strips_token_when_no_skills():
    agent = _agent_with([])
    ctx = {"fallback_system_prompt": "Intro.\n{available_skills}\nOutro."}
    with patch("core.skills.resolve_tenant_skills", return_value=[]):
        ready = _prepare(agent, ctx)
    assert "{available_skills}" not in ready["system_prompt"]
