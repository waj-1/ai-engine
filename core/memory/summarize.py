# ai_agent/core/memory/summarize.py
"""Rolling conversation summary refresh (short-term memory)."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import HumanMessage

from core.memory.extract import content_to_text
from core.memory.prompts import SUMMARY_PROMPT

logger = logging.getLogger(__name__)


def needs_refresh(total: int, prior_count: int, cfg: Dict[str, Any]) -> bool:
    """True once enough new messages have accrued since the last summary.

    `total` and `prior_count` both count the session's WHOLE transcript.
    """
    trigger_turns = int(cfg.get("trigger_every_n_turns", 6))
    verbatim_msgs = int(cfg.get("keep_recent_verbatim", 4)) * 2
    # A refresh leaves the newest `verbatim_msgs` out of the summary on
    # purpose, so the transcript had prior_count + verbatim_msgs messages when
    # it ran. Only what came after that is new.
    refreshed_at = prior_count + verbatim_msgs if prior_count else 0
    return (total - refreshed_at) >= trigger_turns * 2 and total > verbatim_msgs


async def maybe_update_summary(
    light_llm,
    *,
    prior_summary: str,
    prior_count: int,
    messages: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    offset: int = 0,
    prompt: str = SUMMARY_PROMPT,
) -> Optional[Tuple[str, int]]:
    """Refresh the rolling summary if enough new turns have accrued.

    `messages` is the session's WHOLE ordered history (list of {role, content}),
    not an agent's capped history window: `prior_count` indexes into it. Only
    the messages the prior summary doesn't cover yet are folded in. `offset`
    is the transcript position of `messages[0]`, for a caller that passes only
    the transcript from `prior_count` on.
    Returns (new_summary, new_covered_count) when a
    refresh happened, else None. Never raises.
    """
    keep_recent = int(cfg.get("keep_recent_verbatim", 4))
    total = offset + len(messages)

    # Messages that should stay verbatim (most recent keep_recent turns).
    verbatim_msgs = keep_recent * 2
    if not needs_refresh(total, prior_count, cfg):
        return None

    # Only what the prior summary doesn't cover yet: the prior summary is
    # passed in, so re-sending older messages would only grow the prompt.
    start = max(min(prior_count, total - verbatim_msgs) - offset, 0)
    older = messages[start: total - verbatim_msgs - offset]
    older_text = "\n".join(
        f"{m.get('role', 'user')}: {(m.get('content') or '')[:500]}" for m in older
    )
    prompt = prompt.format(
        prior_summary=prior_summary or "(none)",
        older_messages=older_text,
    )
    try:
        resp = await light_llm.ainvoke([HumanMessage(content=prompt)])
    except Exception as exc:
        logger.warning("memory.summarize: llm failed (%s)", exc)
        return None

    summary = content_to_text(resp.content)
    summary = (summary or "").strip()
    if not summary:
        return None
    return summary, total - verbatim_msgs
