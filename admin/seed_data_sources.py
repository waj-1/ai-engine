"""Seed the knowledge sources this deployment's agents read.

Idempotent on (name) unique key. Called from main.py lifespan via
asyncio.to_thread alongside seed_prompts and sync_tools.

The engine ships none of its own: an operator adds knowledge sources in the
portal or over MCP. A product's sources come from its plugin's
`data_source_seeds` hook (`data_source_specs()` and, optionally,
`superseded_names()` — see core/plugins.py). Until 2026-11-22 the catalog
lived here, so every install was seeded with one deployment's five sources.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from core.auth import get_service_client

logger = logging.getLogger(__name__)

def _sources() -> List[Dict[str, Any]]:
    from core.plugins import call_hook, load_hook

    out: List[Dict[str, Any]] = []
    for found in call_hook(load_hook("data_source_seeds"), "data_source_specs"):
        out.extend(found)
    return out


def _superseded_names() -> List[str]:
    from core.plugins import call_hook, load_hook

    out: List[str] = []
    for found in call_hook(load_hook("data_source_seeds"), "superseded_names"):
        out.extend(found)
    return out


def _deactivate_superseded(client) -> int:
    """Mark legacy source rows inactive so the portal stops listing them."""
    deactivated = 0
    for name in _superseded_names():
        existing = (
            client.table("agent_data_sources").select("source_id, is_active")
            .eq("name", name).limit(1).execute().data or []
        )
        if existing and existing[0].get("is_active"):
            client.table("agent_data_sources").update({"is_active": False}).eq(
                "source_id", existing[0]["source_id"]
            ).execute()
            deactivated += 1
    return deactivated


def run() -> Dict[str, int]:
    client = get_service_client()
    inserted = 0
    updated = 0
    for src in _sources():
        existing = (
            client.table("agent_data_sources").select("source_id, agent_kind, additional_agent_kinds, config")
            .eq("name", src["name"]).limit(1).execute().data or []
        )
        if existing:
            patch: Dict[str, Any] = {}
            # Backfill agent_kind on rows seeded before the column existed.
            if not existing[0].get("agent_kind"):
                patch["agent_kind"] = src["agent_kind"]
            # Backfill additional_agent_kinds on pre-existing rows.
            if src.get("additional_agent_kinds") and not (existing[0].get("additional_agent_kinds") or []):
                patch["additional_agent_kinds"] = src["additional_agent_kinds"]
            # Backfill shared-KB config keys on docs sources seeded before unification.
            desired_cfg = src.get("config") or {}
            if desired_cfg.get("table") == "ai_kb_embeddings":
                cur_cfg = existing[0].get("config") or {}
                merged = {**cur_cfg, **{k: desired_cfg[k] for k in
                          ("table", "pages_table", "embed_model", "source_tag")}}
                if merged != cur_cfg:
                    patch["config"] = merged
            if patch:
                client.table("agent_data_sources").update(patch).eq(
                    "source_id", existing[0]["source_id"]
                ).execute()
                updated += 1
            continue
        client.table("agent_data_sources").insert(src).execute()
        inserted += 1
    deactivated = _deactivate_superseded(client)
    return {"inserted": inserted, "updated": updated, "deactivated": deactivated}
