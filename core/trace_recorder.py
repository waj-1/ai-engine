"""Trace recorder — persist per-request agent telemetry into agent_traces
*and* tag the active Laminar span so the rich view lives in Laminar.

Architecture: Laminar (lmnr) is the source of truth for rich traces — span
hierarchy, LLM call inputs/outputs, latency, tool args, the lot. This
recorder additionally pins a flat summary row into the Supabase
`agent_traces` table so the admin portal's Live Monitor / Audit tabs can
render KPIs without round-tripping to Laminar's HTTP API on every page
load. Each row carries an `lmnr_trace_id` that the portal uses to deep-link
into the Laminar dashboard for full drill-down.

Best-effort: every call is wrapped in a broad except so a recorder failure
never breaks an in-flight agent stream. Mirrors `core/chat_persistence.py`'s
philosophy.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from core.auth import get_service_client

logger = logging.getLogger(__name__)


def _current_lmnr_trace_id() -> Optional[str]:
    """Read the current Laminar trace id, if any.

    Returns None when Laminar isn't initialized or there's no active span,
    which is the common case for unit tests and background tasks. Stringify
    because the SDK returns a `uuid.UUID` and supabase-py's JSON encoder
    rejects raw UUIDs (`Object of type UUID is not JSON serializable`).
    """
    try:
        from lmnr import Laminar

        trace_id = Laminar.get_trace_id()
        return str(trace_id) if trace_id is not None else None
    except Exception:
        return None


def current_lmnr_trace_id() -> Optional[str]:
    """Public accessor: the active Laminar trace id, or None. Used by the turn
    orchestrator so a telemetry row can deep-link to its Laminar span."""
    return _current_lmnr_trace_id()


def _tag_lmnr_span(metadata: Dict[str, Any]) -> None:
    """Attach the same metadata to the active Laminar span. Best-effort."""
    if not metadata:
        return
    try:
        from lmnr import Laminar

        # Laminar's set_span_attributes wants flat scalars; serialize anything
        # complex (tool_calls list, dicts) so the call doesn't reject the payload.
        flat: Dict[str, Any] = {}
        for key, value in metadata.items():
            if value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                flat[key] = value
            else:
                # str() over json.dumps so we don't crash on non-JSON inputs.
                flat[key] = str(value)
        if flat:
            Laminar.set_span_attributes(flat)
    except Exception:
        # Span tagging is purely additive — never let it surface to the caller.
        pass


def record_trace(
    agent_kind: str,
    *,
    session_id: Optional[str] = None,
    message_id: Optional[str] = None,
    profile_id: Optional[str] = None,
    model_id: Optional[str] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    latency_ms: Optional[int] = None,
    iteration_count: Optional[int] = None,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    error: Optional[str] = None,
) -> None:
    """Write a single trace row + tag the active Laminar span. Swallows every
    exception so a recorder failure never breaks an in-flight agent stream.
    """
    if os.getenv("AGENT_TRACE_RECORDER_DISABLED", "").lower() == "true":
        return

    lmnr_trace_id = _current_lmnr_trace_id()

    # Tag the current Laminar span first — even if Supabase is unreachable we
    # still want the metadata in the rich view.
    _tag_lmnr_span(
        {
            "waj.agent_kind": agent_kind,
            "waj.session_id": session_id,
            "waj.message_id": message_id,
            "waj.profile_id": profile_id,
            "waj.model_id": model_id,
            "waj.iteration_count": iteration_count,
            "waj.error": error,
        }
    )

    try:
        client = get_service_client()
        # agent_traces is now a lean session<->trace index; the rich payload
        # (tokens, latency, tool I/O) lives in Laminar and is tagged onto the
        # span above. Keep only the bridge columns here.
        payload: Dict[str, Any] = {
            "agent_kind": agent_kind,
            "session_id": session_id,
            "lmnr_trace_id": lmnr_trace_id,
        }
        # Strip Nones so we don't override DB defaults.
        payload = {k: v for k, v in payload.items() if v is not None}
        client.table("agent_traces").insert(payload).execute()
    except Exception as exc:
        logger.warning("trace_recorder: insert failed (%s)", exc)
