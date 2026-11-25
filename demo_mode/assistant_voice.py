"""Talking to the builder assistant: the mic button of the "Build with AI" panel.

``WS /admin/demo/assistant/voice`` (admin/demo_handlers.py) is a voice call with
the builder assistant (demo_mode/assistant.py), served by the engine's shared
voice handler (endpoints/voice_live.py) the way any agent's call is, through a
`VoiceRoute`:

* the realtime model is the seeded "Demo voice model" row (`demo_mode.voice`),
  the same one the Support desk's Call button uses. It hears the visitor,
  speaks, and hands every request to its `ask_agent` tool;
* `ask_agent` runs the builder assistant itself (the same agent, tools,
  allow-list and step limit as a typed message), in the panel's conversation,
  so typed and spoken turns share one history;
* the voice settings are `live_config` below, not a row a visitor can edit.

Caps. The call runs under `demo_mode.voice.guard` like every demo call: the
day's voice seconds, the call length, the calls open at once and the
visitor's calls per hour. Each spoken request is also a builder message: it
counts against the visitor's hourly messages (``DEMO_ASSISTANT_PER_HOUR``),
the demo's daily turns (``DEMO_DAILY_TURNS``) and, per tool call, the
per-message and hourly tool limits. Recording stays off (the demo's process
lock-down).

A spoken answer is short: the builder writes its usual answer for the panel
and starts it with one plain sentence, and only that sentence is read out
(`spoken_summary`). Long turns are covered by a holding line spoken as soon
as the request goes out, and by the step rows the panel shows while it runs.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

from demo_mode import settings

#: What the builder is told on a spoken turn, instead of the engine's generic
#: voice rules (which ask for a reply with no links, and the panel shows one).
VOICE_NOTE = (
    "## The visitor is talking to you\n"
    "The visitor speaks to you through the panel's microphone, so their words come from "
    "speech recognition and may be loose. Do the work exactly as for a typed message. "
    "Your reply is shown in the panel as usual, and only its first sentence is read aloud. "
    "So start with one short, plain sentence that says what you did or found, with no "
    "markdown, links or ids in it. Then give the details and links as usual."
)

#: Spoken when the call connects (an instruction to the realtime model).
GREETING = ("Say in one short sentence that you can build and test agents for Northwind, "
            "and ask what to build.")

#: Spoken as soon as a request goes out: a builder turn takes seconds, often
#: tens of seconds. Worded clear of the announce phrases the stall tripwire
#: listens for (common/announce.py).
PROGRESS = [
    "On it. You can follow the steps in the panel.",
    "Working on that now. The steps show in the panel.",
]

#: A builder turn can wait up to two minutes for a test run
#: (demo_mode.assistant.wait_for_test_run), plus its other steps.
TURN_TIMEOUT_SEC = 150

PANEL_POINTER = "The details are in the panel."
_MAX_SPOKEN = 220


def live_config(model_id: str) -> Dict[str, Any]:
    """The voice settings of a call with the builder assistant: the seeded
    realtime model, the demo's voice and language, the demo's call length."""
    return {
        "enabled": True,
        "model_id": model_id,
        "voice_id": settings.voice_name(),
        "language": settings.voice_language(),
        "max_session_sec": settings.voice_max_call_seconds(),
        "greeting_trigger": GREETING,
        "delegate_timeout_sec": TURN_TIMEOUT_SEC,
        "progress_on_dispatch": True,
        "progress_spoken": list(PROGRESS),
        # The answer is spoken whole (the summary), never sentence by sentence.
        "stream_sentences": False,
    }


def voice_model_id() -> Optional[str]:
    """The id of the demo's realtime model row in the environment the demo
    builds in, or None when the seed made none (voice not set up). Blocking."""
    from core.auth import get_service_client
    from demo_mode import seed, voice

    rows = (get_service_client().table("agent_models").select("model_id")
            .eq("name", voice.MODEL_NAME).eq("environment", seed._env())
            .eq("is_active", True).limit(1).execute().data or [])
    return str(rows[0]["model_id"]) if rows else None


_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MARKS = re.compile(r"[*_`#>|]+")
_BULLET = re.compile(r"^\s*(?:[-+]|\d+[.)])\s+", re.M)
_URL = re.compile(r"(?:https?://|/admin/)\S+")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _plain(text: str) -> str:
    text = _LINK.sub(r"\1", text or "")
    text = _URL.sub("", text)
    text = _BULLET.sub("", text)
    text = _MARKS.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def spoken_summary(answer: str) -> str:
    """What is read aloud of a builder answer: its first sentence (two when the
    first is very short), in plain words, and a pointer to the panel when the
    answer says more than that."""
    paragraphs = [p for p in re.split(r"\n\s*\n", answer or "") if p.strip()]
    if not paragraphs:
        return ""
    sentences = [s for s in _SENTENCE.split(_plain(paragraphs[0])) if s]
    if not sentences:
        return ""
    said = sentences[0]
    if len(said) < 40 and len(sentences) > 1:
        said = f"{said} {sentences[1]}"
    if len(said) > _MAX_SPOKEN:
        said = said[:_MAX_SPOKEN].rsplit(" ", 1)[0].rstrip(",;:") + "."
    rest = _plain(answer)
    if len(rest) > len(said) + 20:
        said = f"{said} {PANEL_POINTER}"
    return said
