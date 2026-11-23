"""Duplex voice for the generic `custom` agent.

A custom agent is configuration only, so its voice is too: the realtime
model, voice, languages and barge-in come from the profile's flat
`voice_config`, and the brain that answers `ask_agent` is the same agent,
unless `voice_config.agent_profile_slug` names another custom agent.

Many custom agents share the kind, so a call always names its agent by slug
(`WS /v1/agents/{slug}/voice`, endpoints/agents_v1.py) and nothing here ever
falls back to "the kind's active profile".

Two things a product kind has seeded for it are built in here, so that
turning voice on is enough to take a call:

  * the persona the realtime model runs on, used when `voice_config` names no
    `persona_prompt_slug` (an operator who wants their own writes a prompt and
    names it);
  * a greeting, used when `greeting_trigger` is blank. A realtime session is
    silent until spoken to, which on a call reads as a dead line.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# `voice_config` values a custom agent uses where its row sets none.
CONFIG_DEFAULTS: Dict[str, Any] = {
    "greeting_trigger": "Greet the caller in one short sentence and ask how you can help.",
}

# The imperative wording of rule 1 is deliberate: a realtime model told that a
# request "goes to" a tool has spoken the tool call as text instead of making
# it. It must be told to CALL the tool.
_PERSONA = """You are the voice of an assistant on a live voice call. Speak {language}; if the caller clearly speaks another language, follow them.

Rules:
1. You do not know the answers yourself. For every question, request or task from the caller you must call the `ask_agent` tool, passing the caller's request in their own words, and then say what it returns. Never answer from your own knowledge, and never invent a fact, a name or a number.
2. A greeting, a thank-you, a goodbye, or asking the caller to repeat needs no tool call.
3. Say the tool's answer faithfully and briefly. Add no offer, recap or detail it did not contain. Read a long reference number in groups of four digits.
4. If the tool reports that something failed, say so plainly and offer to try again.
5. Speak naturally, in short sentences. Never read out markup, symbols or web addresses."""


def default_persona(profile: Any = None, *, language: Optional[str] = None) -> str:
    """The built-in realtime persona for a custom agent that names none."""
    return _PERSONA.format(language=language or "the caller's language")


def build_custom_voice_agent(params: Dict[str, Any]) -> Any:
    """The agent that answers a custom agent's call.

    `agent_slug` is the agent the call was addressed to. `profile_slug`
    (`voice_config.agent_profile_slug`) names a different custom agent as the
    brain. Either way the profile resolves by `(custom, slug, environment)`
    and FAILS CLOSED: an unknown slug raises `MissingProfileError`.

    `profile_pin` is the profile an operator asked to test (a draft). It only
    applies when the addressed agent is its own brain: a pin swaps which copy
    of an agent answers, never which agent.
    """
    from core.profile_version_resolver import _PROFILE_ID
    from runtime.agents.custom import create_custom_agent

    slug = str(params.get("agent_slug") or "").strip()
    if not slug:
        raise LookupError(
            "a custom agent is called by its address: WS /v1/agents/{slug}/voice")
    brain = str(params.get("profile_slug") or "").strip() or slug
    pin = params.get("profile_pin") if brain == slug else None
    token = _PROFILE_ID.set(pin or None)
    try:
        return create_custom_agent(brain, locale=params.get("locale"))
    finally:
        _PROFILE_ID.reset(token)
