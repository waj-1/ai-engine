"""Shared helper for the model picker used across the admin portal.

Every screen that asks an operator for a model (eval suite create dialog,
suite editor, prompt optimizer, agent profile editor) reads the same shape:
list of `{model_id, name, model_string, provider, is_active, environment}`
ordered by name.  Keep that fetch in one place so the columns + ordering
stay consistent.

`agent_models` lives in the AI project, so this helper builds its own AI
service-role client via `admin_service_client()`.  Empty list on any failure
(the picker just renders empty) so the page never crashes on a DB outage.
"""
from __future__ import annotations

import logging
from typing import List, Dict, Any

from core.auth import get_service_client, admin_service_client

logger = logging.getLogger(__name__)

_MAX_MODELS = 200


def is_chat_model(row: Dict[str, Any]) -> bool:
    """False for a row flagged `extra_config.realtime` (the duplex-voice
    ears-and-mouth model, e.g. "Gemini Live (duplex voice)").

    The single definition of "this is not a chat model" — both model
    pickers (this module's `fetch_models_for_picker`, backing the eval
    suite/prompt-optimizer dialogs, and `admin/screens/agents.py`'s inline
    `model_main_id` dropdown query) import this rather than duplicating the
    condition, so a second realtime provider only needs to set the same
    flag to be excluded everywhere. Realtime rows are resolved directly by
    `voice/live/config.py::hydrate_live_model` via `voice_config.model_id`
    — they're never meant to be assignable as an agent's `model_main_id`
    (a realtime-only model id, e.g. a native-audio-dialog Gemini variant,
    is not served by the ordinary `generateContent` chat path
    `runtime/llm_factory.py` builds against).
    """
    return not bool((row.get("extra_config") or {}).get("realtime"))


def is_realtime_model(row: Dict[str, Any]) -> bool:
    """True for a row flagged `extra_config.realtime` -- the complement of
    `is_chat_model`.

    Powers the duplex-voice model picker on `/admin/agents`
    (`voice_config.model_id`), which must show ONLY realtime rows -- the
    inverse of every chat-model picker's gate. Kept as its own predicate
    (rather than `not is_chat_model(row)`) so the "what counts as realtime"
    definition has one owner that both directions read, the same reasoning
    `is_chat_model`'s docstring gives for not duplicating its condition.
    """
    return bool((row.get("extra_config") or {}).get("realtime"))


def fetch_models_for_picker(
    *,
    active_only: bool = True,
    environment: str | None = None,
) -> List[Dict[str, Any]]:
    """Return agent_models rows as picker dicts.

    Parameters
    ----------
    active_only:
        When True (default) only rows with ``is_active = True`` are returned.
        Pass False to include inactive models (shown with an "(inactive)" hint
        in the template).
    environment:
        Optional filter; when given, only rows whose ``environment`` column
        equals this value are returned.

    Returns
    -------
    list of dicts with keys: model_id, name, model_string, provider,
    is_active, environment.  Empty list on any error. Realtime-flagged rows
    (see `is_chat_model`) are always excluded — this picker is chat-model-only.
    """
    try:
        client = admin_service_client()
        query = (
            client.table("agent_models")
            .select("model_id, name, model_string, provider, is_active, environment, extra_config")
        )
        if active_only:
            query = query.eq("is_active", True)
        if environment is not None:
            query = query.eq("environment", environment)
        result = query.order("name").limit(_MAX_MODELS).execute()
        rows = result.data or []
    except Exception as exc:
        logger.warning("model picker: fetch failed (%s)", exc)
        return []

    return [
        {
            "model_id": r["model_id"],
            "name": r.get("name") or r["model_id"],
            "model_string": r.get("model_string") or "",
            "provider": r.get("provider") or "",
            "is_active": bool(r.get("is_active")),
            "environment": r.get("environment") or "",
        }
        for r in rows
        if r.get("model_id") and is_chat_model(r)
    ]
