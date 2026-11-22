"""Per-turn telemetry summary writer.

Owned by the turn orchestrator: after each completed chat turn it inserts one
row into `agent_traces` (AI project) with tokens, latency, model, and computed
cost. This is the authoritative KPI source for the admin dashboards (repointed
off ClickHouse in a later PR); Laminar keeps the deep span tree, reachable via
`lmnr_trace_id`. Best-effort — never raises into the chat path.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from core.auth import get_service_client

logger = logging.getLogger(__name__)


async def record_turn_telemetry(
    *,
    session_id: Optional[str],
    agent_kind: str,
    profile_id: Optional[str],
    model: Optional[Dict[str, Any]],
    usage: Dict[str, Any],
    latency_ms: int,
    iteration_count: Optional[int] = None,
    lmnr_trace_id: Optional[str] = None,
    status: str = "ok",
) -> None:
    """Insert one per-turn telemetry row into agent_traces. Swallows errors.

    `status` is ``"ok"`` for a completed turn and ``"error"`` for a turn that
    failed mid-stream (the orchestrator records both so the admin dashboards can
    compute an error rate and see failed-turn latency). Legacy rows written
    before the `status` column existed are NULL and should be read as ``"ok"``.
    """
    try:
        from core import pricing

        in_tok = int(usage.get("input_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or 0)
        cache_read = int(usage.get("cache_read_tokens") or 0)
        cache_creation = int(usage.get("cache_creation_tokens") or 0)
        cost = None
        if model:
            cost = pricing.cost_usd(model, in_tok, out_tok,
                                    cache_read_tokens=cache_read,
                                    cache_creation_tokens=cache_creation)

        row: Dict[str, Any] = {
            "session_id": session_id,
            "agent_kind": agent_kind,
            "profile_id": profile_id,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cache_read_tokens": cache_read,
            "cache_creation_tokens": cache_creation,
            "latency_ms": int(latency_ms),
            "iteration_count": iteration_count,
            "lmnr_trace_id": lmnr_trace_id,
            "status": status,
        }
        if model:
            row["model_id"] = model.get("model_id")
            row["model_string"] = model.get("model_string")
        if cost is not None:
            row["cost_usd"] = float(cost)

        client = get_service_client()
        await asyncio.to_thread(
            client.schema("public").table("agent_traces").insert(row).execute
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must never break a turn
        logger.warning("record_turn_telemetry failed for session=%s: %s", session_id, exc)
