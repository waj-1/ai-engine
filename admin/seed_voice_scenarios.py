"""Seed starter live-voice scenarios (ai_voice_scenarios).

Insert-only and idempotent on `slug`, like every other boot seeder: a row an
operator edited in the portal is never overwritten, and a deleted row IS
recreated on next boot (delete = disable it instead, same convention as
seed_eval_suites).

The engine ships none of its own: every starter scenario calls one of a
deployment's own voice agents, so they come from its plugin's
`voice_scenario_seeds` hook (`voice_scenario_specs()`, see core/plugins.py).
Deep protected-flow scenarios (side-effect assertions behind a portal login)
need a per-deployment login cookie; author those in /admin/voice-scenarios
with an `http_check` step rather than seeding them.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def _scenarios() -> List[Dict[str, Any]]:
    from core.plugins import call_hook, load_hook

    out: List[Dict[str, Any]] = []
    for found in call_hook(load_hook("voice_scenario_seeds"), "voice_scenario_specs"):
        out.extend(found)
    return out


def run() -> Dict[str, int]:
    from core.auth import get_service_client

    scenarios = _scenarios()
    if not scenarios:
        return {"created": 0, "skipped": 0}

    client = get_service_client()
    created = 0
    try:
        existing = {
            r["slug"]
            for r in (client.table("ai_voice_scenarios").select("slug").execute().data or [])
        }
    except Exception:
        # Table missing (migration not applied yet) — a boot seeder must
        # never take the engine down.
        logger.info("seed_voice_scenarios: table unavailable; skipping", exc_info=True)
        return {"created": 0, "skipped": len(scenarios)}
    for scenario in scenarios:
        if scenario["slug"] in existing:
            continue
        try:
            client.table("ai_voice_scenarios").insert(scenario).execute()
            created += 1
        except Exception:
            logger.warning(
                "seed_voice_scenarios: could not insert %s", scenario["slug"], exc_info=True
            )
    if created:
        logger.info("seed_voice_scenarios: created %d scenario(s)", created)
    return {"created": created, "skipped": len(scenarios) - created}
