"""
SSE framing helpers shared by all streaming endpoints.

Pulled out of `main.py` so any router can wrap
async event generators in Server-Sent Events without importing the legacy
composition root.
"""

import json
import logging
import os
import time
from typing import AsyncGenerator, Callable

from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)


def _expose_error_detail() -> bool:
    """True everywhere except production.

    The chat endpoints deliberately collapse every failure into one generic
    operator-facing message so we never leak internals to real customers. But
    in dev/staging — where the only callers are operators using the admin
    "Test agent" widget or internal QA — that generic message hides the actual
    fault (a missing agent_profile, a bad model id, a DB transport error) and
    forces a log dig. Outside production we append the exception class + text
    to the streamed error so the operator can self-diagnose. Production stays
    generic.
    """
    return os.getenv("ENVIRONMENT", "development").lower() not in ("production", "prod")


def _error_event(error_message: str, exc: Exception) -> dict:
    """Build the SSE error payload, enriching with detail outside production."""
    if _expose_error_detail():
        detail = f"{type(exc).__name__}: {exc}"
        return {
            "type": "error",
            "message": f"{error_message} [{detail[:500]}]",
            "detail": detail[:2000],
        }
    return {"type": "error", "message": error_message}

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Disable nginx buffering so SSE frames flush promptly.
    "X-Accel-Buffering": "no",
}


_CONTENT_TYPES = frozenset({"answer", "final", "result", "final_response", "output", "chart"})


def _stamp_ttft_ttlt(ttft_ms: float | None, ttlt_ms: float | None) -> None:
    """Best-effort: set waj.ttft_ms / waj.ttlt_ms on the active Laminar span."""
    try:
        from lmnr import Laminar

        attrs: dict = {}
        if ttft_ms is not None:
            attrs["waj.ttft_ms"] = ttft_ms
        if ttlt_ms is not None:
            attrs["waj.ttlt_ms"] = ttlt_ms
        if attrs:
            Laminar.set_span_attributes(attrs)
    except Exception:
        pass


async def _sse_format(
    event_source: AsyncGenerator[dict, None],
    *,
    error_message: str,
    stop_on_done: bool = True,
) -> AsyncGenerator[str, None]:
    """Wrap an agent event stream in SSE `data:` framing."""
    start = time.monotonic()
    ttft_ms: float | None = None
    ttlt_ms: float | None = None
    try:
        async for event in event_source:
            now = time.monotonic()
            event_type = event.get("type")
            if ttft_ms is None and event_type in _CONTENT_TYPES:
                ttft_ms = (now - start) * 1000
            ttlt_ms = (now - start) * 1000
            payload = json.dumps(event, ensure_ascii=False)
            yield f"data: {payload}\n\n"
            if stop_on_done and event_type == "done":
                break
    except Exception as exc:
        logger.error(f"SSE stream error: {exc}", exc_info=True)
        err = _error_event(error_message, exc)
        yield f"data: {json.dumps(err)}\n\n"
    finally:
        _stamp_ttft_ttlt(ttft_ms, ttlt_ms)


def sse_response(
    event_source_factory: Callable[[], AsyncGenerator[dict, None]],
    *,
    error_message: str = "An error occurred while processing your request. Please try again.",
    stop_on_done: bool = True,
    trace_name: str | None = None,
    trace_attributes: dict | None = None,
) -> StreamingResponse:
    """Build a StreamingResponse that wraps an agent stream in SSE framing.

    The factory is invoked lazily inside the streaming body so any exception
    raised during agent construction is surfaced as an SSE error event rather
    than a 500 status.

    When ``trace_name`` is given, the whole stream runs inside an explicit
    Laminar root span (``core.observability.root_span``) tagged with
    ``trace_attributes``. The agent's auto-instrumented LLM/LangChain spans —
    created lazily *during* iteration, after the handler has returned — nest
    under it, so request-level attributes (``waj.agent_kind``,
    ``waj.channel.*``) land on the trace's TOP span. Without this the top span
    has no ``agent_kind`` and the observability views render the trace as
    "legacy". No-op when Laminar isn't initialized.
    """

    async def _gen() -> AsyncGenerator[str, None]:
        # Lazy import keeps common.sse free of a hard core.* dependency at
        # module load and avoids any import cycle.
        from core.observability import root_span

        with root_span(trace_name, trace_attributes):
            try:
                source = event_source_factory()
            except Exception as exc:
                logger.error(f"Failed to build agent stream: {exc}", exc_info=True)
                err = _error_event(error_message, exc)
                yield f"data: {json.dumps(err)}\n\n"
                return
            async for chunk in _sse_format(
                source, error_message=error_message, stop_on_done=stop_on_done
            ):
                yield chunk

    return StreamingResponse(
        _gen(), media_type="text/event-stream", headers=SSE_HEADERS
    )
