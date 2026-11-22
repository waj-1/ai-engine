"""Memory config defaults + deep-merge resolver.

`agent_profiles.memory_config` holds operator overrides (possibly `{}`).
`resolve_memory_config` merges them over `DEFAULT_MEMORY_CONFIG` so callers
always get a complete, well-typed config. The whole feature is OFF unless
`enabled` is explicitly true.
"""
from __future__ import annotations

import copy
from typing import Any, Dict

DEFAULT_MEMORY_CONFIG: Dict[str, Any] = {
    "enabled": False,
    # `window` is the short-term conversation window every agent reads, and is
    # independent of `enabled` (which only gates the long-term memory feature
    # below). An agent always has a window; it may not have long-term memory.
    # max_messages/max_message_chars replace the AGENT_HISTORY_MSG_* env
    # defaults so operators can retune a window from the portal instead of a
    # redeploy; suggestion_context_messages sizes the history slice handed to
    # the follow-up-suggestion sub-agent.
    "window": {"max_messages": 10, "max_message_chars": 4000,
               "suggestion_context_messages": 6},
    "extraction": {"model_slot": "light", "importance_threshold": 0.5, "max_per_turn": 5},
    "dedup": {"similarity_threshold": 0.9},
    "retrieval": {"top_k": 6, "min_similarity": 0.7, "always_include_pinned": True},
    "summary": {"trigger_every_n_turns": 6, "keep_recent_verbatim": 4, "model_slot": "light"},
    "retention_days": None,
}


def resolve_memory_config(raw: Dict[str, Any] | None) -> Dict[str, Any]:
    """Return a complete config = DEFAULT_MEMORY_CONFIG deep-merged with `raw`."""
    merged = copy.deepcopy(DEFAULT_MEMORY_CONFIG)
    if not raw:
        return merged
    for key, value in raw.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def resolve_window_config(raw: Dict[str, Any] | None) -> Dict[str, Any]:
    """Return just the short-term conversation `window` block, defaults-merged.

    Convenience for callers that only need the window (history sizing) and not
    the whole long-term-memory config.
    """
    return resolve_memory_config(raw)["window"]
