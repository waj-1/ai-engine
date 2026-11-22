"""One retry on another model when Azure's content filter refuses a prompt.

User voice test 2026-09-30: an Arabic call's fourth booking turn carried the
STT garble «Premium Kat end ستايب» (for "Premium Cut and Style") plus the
call's history, and Azure OpenAI's content management policy rejected the
PROMPT — `400 … 'code': 'content_filter' … 'violence': {'filtered': True,
'severity': 'high'}`. Nothing in the turn was violent; the classifier ran on
a garbled transcript. The request never reached the model, so the caller got
no answer at all (and, before the error-line fix, the raw 400 body).

A prompt-side filter verdict is a property of the provider, not of the
question, so the cheapest correct recovery is to ask a model that is not
behind that filter: the profile's `model_light` when one is configured (the
loader retired it, so normally none), otherwise Gemini (`AGENT_CONFIG.
light_model`). Exactly ONE retry, and only when nothing of the answer has
streamed yet — a refusal mid-answer must not splice two models' text.

Nothing in THIS module logs or returns the provider's error text: it quotes
the caller's (filtered) words back. The log lines here name the exception type
only. (A caller's own error handler may still log the exception it re-raises —
an agent that logs it with `exc_info`; that is a log, never a reply.)

`fallback_allowed` gates the retry on time: a voice turn past its budget
(the realtime service drops a tool result after ~8s) sends its clean failure
line at once instead of a cross-cloud retry that cannot arrive in time. The
same gate bounds the sticky rounds that follow a refusal.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from core.config import AGENT_CONFIG

logger = logging.getLogger(__name__)


class ContentFilterFallbackDeclined(RuntimeError):
    """A sticky (already refused) turn's next round would need the fallback
    model, and `fallback_allowed` said there is no time left for it."""

_OPENAI_FAMILY = ("azure_openai", "openai")
_FILTER_MARKERS = ("content_filter", "content management policy", "responsibleaipolicyviolation")


def is_content_filter_error(exc: BaseException) -> bool:
    """True when `exc` is a provider refusal by its content filter."""
    if getattr(exc, "code", None) == "content_filter":
        return True
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        if body.get("code") == "content_filter":
            return True
        inner = body.get("error")
        if isinstance(inner, dict) and inner.get("code") == "content_filter":
            return True
    text = str(exc).lower()
    return any(marker in text for marker in _FILTER_MARKERS)


def content_filter_fallback_model(
    model_main: Optional[Dict[str, Any]], model_light: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """The model row to retry a filtered prompt on, or None.

    `model_light` first, when it names a different model. Otherwise Gemini —
    but only when the main model is an OpenAI-family one: a Gemini brain was
    not refused by Azure's filter, and retrying it on itself changes nothing.
    """
    main = model_main or {}
    light = model_light or {}
    if light.get("model_string") and (
        light.get("model_string") != main.get("model_string")
        or light.get("provider") != main.get("provider")
    ):
        return dict(light)
    if (main.get("provider") or "").lower() in _OPENAI_FAMILY:
        return {
            "provider": "google_genai",
            "model_string": AGENT_CONFIG.light_model,
            "temperature": float(main.get("temperature", 0.2) or 0.2),
        }
    return None


async def astream_events_with_fallback(
    primary: Any,
    messages: List[Any],
    *,
    make_fallback: Callable[[], Optional[Any]],
    label: str = "agent",
    sticky: Optional[Dict[str, bool]] = None,
    fallback_allowed: Optional[Callable[[], bool]] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """`primary.astream_events(messages)`, retried once on `make_fallback()`
    when the provider's content filter refuses it before any token streamed.

    `make_fallback` is only called on that path, so a turn that is not
    filtered never builds a second client. Any other error, a filter verdict
    after the answer began streaming, or no fallback available: re-raised
    unchanged for the caller's own error handling.

    `sticky` is a per-TURN dict: once a call of the turn has been refused,
    the rest of the turn's rounds go straight to the fallback. The same
    history is refused every round (live 2026-10-01: four refusals in one
    tool loop, 26.7s for the turn).

    `fallback_allowed`, when given, is asked before every fallback call (the
    first retry AND each sticky round). False: the first refusal re-raises
    unchanged, and a sticky round raises `ContentFilterFallbackDeclined`
    (its primary would only be refused again).
    """
    def _allowed() -> bool:
        try:
            return fallback_allowed is None or bool(fallback_allowed())
        except Exception:
            logger.debug("%s: fallback_allowed raised; treating as allowed", label, exc_info=True)
            return True

    if sticky is not None and sticky.get("fallback"):
        if not _allowed():
            logger.warning("%s: content-filter fallback round skipped (out of time)", label)
            raise ContentFilterFallbackDeclined("content-filter fallback skipped: no time left")
        fallback = make_fallback()
        if fallback is not None:
            async for event in fallback.astream_events(messages, version="v2"):
                yield event
            return
    streamed = False
    try:
        async for event in primary.astream_events(messages, version="v2"):
            if event.get("event") == "on_chat_model_stream":
                streamed = True
            yield event
        return
    except Exception as exc:
        if streamed or not is_content_filter_error(exc):
            raise
        if not _allowed():
            logger.warning(
                "%s: prompt refused by the provider's content filter (%s); no time left for "
                "the fallback model", label, type(exc).__name__,
            )
            raise
        try:
            fallback = make_fallback()
        except Exception:
            logger.warning("%s: content-filter fallback model could not be built", label, exc_info=True)
            fallback = None
        if fallback is None:
            logger.warning(
                "%s: prompt refused by the provider's content filter (%s); no fallback model",
                label, type(exc).__name__,
            )
            raise
        logger.warning(
            "%s: prompt refused by the provider's content filter (%s); retrying once on the "
            "fallback model", label, type(exc).__name__,
        )
        if sticky is not None:
            sticky["fallback"] = True
    async for event in fallback.astream_events(messages, version="v2"):
        yield event
