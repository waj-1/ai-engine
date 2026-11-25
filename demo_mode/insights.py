"""Conversation insights on the public demo, within a budget.

"What happened in conversations" on Overview is filled by a background sweep
(core/conversation_insights.py) that reads each finished conversation and
makes ONE short model call for its topic, outcome and language. Left to
itself it would read every chat any visitor has, on a model key the demo does
not have. On the demo it runs under these rules instead:

* **Only the demo's own sample conversations** (demo_mode/sample_traffic.py
  marks each one's session with ``metadata.sample``). A visitor's chat, a test
  run's case and the builder assistant's chats are never sent to the model;
  they are filed as not counted, so the panel and its totals are about the
  sample conversations only, and the panel says so.
* **On the agents' own small model** (``DEMO_AGENT_DEPLOYMENT``), at most 400
  output tokens a call (its reasoning included).
* **A daily cap**, ``DEMO_INSIGHTS_DAILY_CALLS`` (default 100; 0 switches the
  sweep off), and every call also takes one turn of the demo's daily cap
  (``DEMO_DAILY_TURNS``). Past either, a conversation gets the no-model
  reading: its language by script, its outcome by the text rules, no topic.

What a call costs: the transcript (capped at 4,000 characters) plus about 250
tokens of instructions in, one small JSON object out. Measured on the sample
conversations (2026-11-24): 329 input and 73 output tokens on average.

A conversation is read once it has been quiet for
``DEMO_INSIGHTS_IDLE_MINUTES`` (default 30, the engine's own default; the
engine's floor of 5 is lowered to 1 here so a rehearsal does not have to wait).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from demo_mode import settings

logger = logging.getLogger(__name__)

CALLS_BUCKET = "insight_calls"
#: The key the sample-traffic generator writes on its sessions.
SAMPLE_KEY = "sample"

METHOD_NOTE = ("On this public demo the summary covers the demo's own sample conversations "
               "(the channel \"Sample traffic\"); visitors' chats and test runs are not analysed.")


def daily_calls() -> int:
    """``DEMO_INSIGHTS_DAILY_CALLS``: model calls a day the sweep may make."""
    return settings._int("DEMO_INSIGHTS_DAILY_CALLS", 100)


def idle_minutes() -> int:
    """``DEMO_INSIGHTS_IDLE_MINUTES``: how long a conversation must be quiet."""
    return settings._int("DEMO_INSIGHTS_IDLE_MINUTES", 30, 1)


def interval_sec() -> int:
    """``DEMO_INSIGHTS_INTERVAL_SEC``: seconds between sweeps."""
    return settings._int("DEMO_INSIGHTS_INTERVAL_SEC", 300, 20)


def enabled() -> bool:
    return settings.enabled() and daily_calls() > 0


def sweep_settings() -> Dict[str, Any]:
    return {"model": settings.agent_deployment(), "idle_minutes": idle_minutes(),
            "interval_sec": interval_sec(), "first_delay_sec": min(90, interval_sec())}


def model_row() -> Optional[Dict[str, Any]]:
    """The model the sweep calls: the demo's agents model with a short answer.
    None when the sweep is off."""
    if not enabled():
        return None
    from demo_mode import seed

    row = seed._model_row("agent", settings.agent_deployment(), "")
    return {"provider": row["provider"], "model_string": row["model_string"],
            "temperature": 0.0, "max_output_tokens": 400,
            "extra_config": dict(row["extra_config"])}


def covers(candidate: Dict[str, Any]) -> bool:
    """True for one of the demo's own sample conversations."""
    return bool((candidate.get("metadata") or {}).get(SAMPLE_KEY))


def take_call() -> bool:
    """One model call from the sweep's own daily budget AND from the demo's
    daily turn cap. False (nothing spent on the model) when either is used up."""
    from demo_mode import limits

    if not limits.take(CALLS_BUCKET, daily_calls()):
        return False
    return limits.take_turn()
