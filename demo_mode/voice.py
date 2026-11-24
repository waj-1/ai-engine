"""Voice calls in the public demo, and the caps that make them safe to offer.

A realtime model bills by the minute and every visitor is anonymous, so a
call is only allowed when ``DEMO_VOICE`` is on (`demo_mode.policy` refuses the
socket otherwise) and `guard` then wraps every call:

Per visitor
    ``DEMO_VOICE_CALLS_PER_HOUR`` calls an hour (`demo_mode.limits`, session
    and address). An address can be made up by a caller who reaches the
    server directly, so nothing below depends on it.

The whole demo
    * ``DEMO_VOICE_MAX_CALLS`` calls open at the same time, in this process.
    * ``DEMO_VOICE_MAX_CALL_SECONDS`` for one call, whatever the agent's own
      voice settings say (a visitor can edit those).
    * ``DEMO_VOICE_DAILY_SECONDS`` seconds of calls per UTC day, every agent
      together, counted in the database next to the daily turn cap
      (`demo_usage_take`, bucket ``voice_seconds``), in memory when the
      database cannot be reached. The engine's own allowance is per agent
      and a visitor can make agents, so it does not bound the demo.

A call is charged while it runs, every `STEP_SEC` seconds, and for the rest
when it ends. When a charge does not fit in what is left of the day, or the
call reaches its length limit, the call is ended with a sentence the Call
button shows. A new call needs at least one step left, so the day can go
over by at most one step for each call open at that moment.

The seeded voice setup (`voice_model_row`, `voice_config`) is here too:
`demo_mode.seed` gives the Support desk a voice so a visitor can press Call
without setting anything up.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from demo_mode import limits, settings

logger = logging.getLogger(__name__)

VOICE_BUCKET = "voice_seconds"
#: How often a running call is charged, and the least a new call needs left.
STEP_SEC = 15
#: voice.live.protocol.CLOSE_RATE_LIMITED: the shared voice client shows the
#: reason of a close in the 4000s.
CLOSE_CODE = 4429
#: How long a call that was told to end may take to hang up its model.
_TEARDOWN_SEC = 20.0

MODEL_NAME = "Demo voice model"
#: The agent a visitor can call without setting anything up.
CALL_SLUG = "support-desk"

OFF_MESSAGE = "Voice calls are switched off in the public demo."
CAP_MESSAGE = "The public demo has used today's voice minutes. Calls open again at midnight UTC."
BUSY_MESSAGE = "All of the demo's voice lines are in use. Try again in a minute."
RATE_MESSAGE = "You have placed a lot of calls this hour. Try again later."

_open_calls = 0
# Seams for the tests' clock.
_sleep = asyncio.sleep
_now = time.monotonic


def reset_for_tests() -> None:
    global _open_calls
    _open_calls = 0


def open_calls() -> int:
    return _open_calls


def length_message() -> str:
    limit = settings.voice_max_call_seconds()
    span = f"{limit // 60} minutes" if limit >= 120 and limit % 60 == 0 else f"{limit} seconds"
    return f"Calls in the public demo end after {span}. Thanks for trying it."


def seconds_left() -> int:
    """What is left of today's voice seconds (``DEMO_VOICE_DAILY_SECONDS``)."""
    return max(0, settings.voice_daily_seconds() - limits.used(VOICE_BUCKET))


def _charge(seconds: int) -> bool:
    """Take `seconds` from today's voice seconds; False when they do not fit."""
    if seconds <= 0:
        return True
    return limits.take(VOICE_BUCKET, settings.voice_daily_seconds(), seconds)


def problem() -> Optional[str]:
    """In demo mode, why no call can start right now (shown on the agent's
    Voice tab), or None. Always None outside demo mode."""
    if not settings.enabled():
        return None
    if not settings.voice_enabled():
        return OFF_MESSAGE + " Run your own copy of the engine to try them."
    if seconds_left() < STEP_SEC:
        return CAP_MESSAGE
    return None


def note() -> Optional[str]:
    """The demo's call limits, in a sentence for the Voice tab."""
    if not settings.voice_enabled():
        return None
    limit = settings.voice_max_call_seconds()
    span = f"{limit // 60} minutes" if limit >= 120 and limit % 60 == 0 else f"{limit} seconds"
    return (f"In the public demo a call lasts up to {span}, and all visitors share "
            f"{settings.voice_daily_seconds() // 60} minutes of calls a day. "
            "Calls are not recorded; the transcript is kept until the nightly reset.")


async def _refuse(websocket: Any, reason: str) -> None:
    """Accept, then close with the reason: a refused handshake reaches a
    browser as a bare failure with no words."""
    try:
        await websocket.accept()
        await websocket.close(code=CLOSE_CODE, reason=reason[:120])
    except Exception:  # noqa: BLE001 (the caller has already gone)
        pass


async def _meter(state: Dict[str, int]) -> str:
    """Charge the running call every step. Returns why the call must end."""
    limit = settings.voice_max_call_seconds()
    while True:
        wait = max(1, min(STEP_SEC, limit - state["charged"]))
        await _sleep(wait)
        if not await asyncio.to_thread(_charge, wait):
            return CAP_MESSAGE
        state["charged"] += wait
        if state["charged"] >= limit:
            return length_message()


async def guard(websocket: Any, run: Callable[[], Awaitable[None]]) -> None:
    """Serve one call (`run`) under the demo's caps. Only called in demo mode."""
    global _open_calls
    if not settings.voice_enabled():
        # demo_mode.gate refuses this before any route; kept for a caller
        # that reaches the route another way.
        await websocket.close(code=1008)
        return
    if await asyncio.to_thread(seconds_left) < STEP_SEC:
        return await _refuse(websocket, CAP_MESSAGE)
    # No await from here to the count going up: two handshakes cannot both
    # take the last line.
    if _open_calls >= settings.voice_max_calls():
        return await _refuse(websocket, BUSY_MESSAGE)
    from demo_mode.gate import visitor

    ip, session = visitor(websocket.scope)
    if not limits.hit("voice", ip=ip, session=session):
        return await _refuse(websocket, RATE_MESSAGE)
    _open_calls += 1
    started = _now()
    state = {"charged": 0}
    call = asyncio.ensure_future(run())
    meter = asyncio.ensure_future(_meter(state))
    try:
        await asyncio.wait({call, meter}, return_when=asyncio.FIRST_COMPLETED)
        if call.done():
            call.result()  # a failure of the call is the call's to report
            return
        reason = meter.result()
        logger.info("demo voice: ending a call (%s)", reason)
        call.cancel()
        await asyncio.wait({call}, timeout=_TEARDOWN_SEC)
        try:
            await websocket.close(code=CLOSE_CODE, reason=reason[:120])
        except Exception:  # noqa: BLE001 (already closed by the call's own teardown)
            pass
    finally:
        _open_calls -= 1
        for task in (call, meter):
            if not task.done():
                task.cancel()
        # The part of the last step that was not charged yet. Without it a
        # call shorter than one step would be free, any number of times.
        rest = min(STEP_SEC, int(_now() - started) - state["charged"])
        if rest > 0:
            # Shielded: when this task is cancelled while leaving, the thread
            # still charges.
            try:
                await asyncio.shield(asyncio.to_thread(_charge_rest, rest))
            except Exception:  # noqa: BLE001 (the cap must not fail a call that has ended)
                logger.warning("demo voice: could not charge the end of a call", exc_info=True)


def _charge_rest(seconds: int) -> None:
    """Charge the end of a call; when it does not fit, take what is left."""
    if _charge(seconds):
        return
    left = seconds_left()
    if left > 0:
        _charge(left)


# --------------------------------------------------------------------------
# The seeded voice setup
# --------------------------------------------------------------------------


def ready() -> bool:
    """Is the demo's realtime model set up on this server? (`DEMO_VOICE` on,
    and the provider's endpoint and key present.)"""
    if not settings.voice_enabled():
        return False
    if settings.voice_provider() == "azure_voice_live":
        return bool(settings.voice_endpoint() and os.getenv(settings.VOICE_KEY_ENV))
    return bool(os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"))


def voice_model_row(env: str) -> Dict[str, Any]:
    """The `agent_models` row of the demo's realtime model."""
    provider = settings.voice_provider()
    extra: Dict[str, Any] = {"seeded_by": "demo_mode.seed", "demo_role": "voice",
                             "realtime": True, "pricing_mode": "manual"}
    if provider == "azure_voice_live":
        extra.update(endpoint=settings.voice_endpoint(),
                     api_version=settings.voice_api_version(),
                     auth_env_var=settings.VOICE_KEY_ENV)
    return {
        "name": MODEL_NAME,
        "provider": provider,
        "model_string": settings.voice_model(),
        "temperature": 0.3,
        "environment": env,
        "is_active": True,
        "extra_config": extra,
    }


def voice_config(model_id: str) -> Dict[str, Any]:
    """The voice settings the seeded agent starts with."""
    return {
        "enabled": True,
        "model_id": model_id,
        "voice_id": settings.voice_name(),
        "language": settings.voice_language(),
        "max_session_sec": settings.voice_max_call_seconds(),
    }
