"""Opt-in Anthropic prompt caching.

Marks the (large, stable) system prompt with an Anthropic ``cache_control``
ephemeral breakpoint so the provider caches the prefix across turns. DEFAULT
OFF: only fires when the model is an Anthropic model AND its
``extra_config.prompt_cache`` is truthy. No-op for every other provider —
Gemini caches implicitly (read-only) with no marker needed, so stamping
cache_control there would be meaningless.

``ChatAnthropic`` (langchain-anthropic) reads
``message.additional_kwargs["cache_control"]`` and hoists it onto the message's
content blocks, and returns ``cache_read``/``cache_creation`` in usage_metadata.

``anthropic_vertex`` was dropped from the tuple below on 2026-08-16 with the rest
of the Vertex removal: no builder can produce that provider any more, so the
branch was unreachable.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from langchain_core.messages import SystemMessage

_ANTHROPIC_PROVIDERS = ("anthropic",)


def _cache_enabled(model: Optional[Dict[str, Any]]) -> bool:
    if not model:
        return False
    if (model.get("provider") or "") not in _ANTHROPIC_PROVIDERS:
        return False
    extra = model.get("extra_config") or {}
    val = extra.get("prompt_cache")
    return str(val).strip().lower() in ("1", "true", "yes", "on") if not isinstance(val, bool) else val


def cached_system_message(system_prompt: str, model: Optional[Dict[str, Any]]) -> SystemMessage:
    """Build the system message, marking it cacheable when the model opts in."""
    if _cache_enabled(model):
        return SystemMessage(
            content=system_prompt,
            additional_kwargs={"cache_control": {"type": "ephemeral"}},
        )
    return SystemMessage(content=system_prompt)
