"""The nightly reset: wipe what visitors made, then build the demo again.

`reset()` deletes every row a visitor could have created or changed (agents
and their versions, instructions, knowledge bases and documents, MCP servers
and HTTP tools, skills, test suites, runs and fixtures, conversations,
traces (the rows here and, where the demo has a trace store, the store itself:
demo_mode/trace_store.py), memories, channels, categories, background jobs and
the activity log) and calls `demo_mode.seed.seed()`. The one exception is the
demo's own sample conversations, which hold nothing a visitor wrote: where the
demo has a trace store they are kept for ``DEMO_SAMPLE_KEEP_DAYS`` days
(demo_mode/retention.py says exactly which, and deletes on any doubt). Templates go back to their first
instructions version. It keeps what the engine itself seeds at start and
visitors cannot change: model rows, the template catalog, the built-in tool bundles and the settings rows, the
demo admin, and the day's usage counters (a reset never refills the daily
cap).

When it runs
    * Every day at ``DEMO_RESET_HOUR_UTC`` (default 3), from a background task
      in the engine (`run_loop`). With several engine processes, exactly one
      runs it each day: each first takes the day's ``reset`` unit
      (`demo_mode.limits.take`), which the database hands out once.
    * By hand, or from an outside scheduler: ``python -m scripts.demo_reset``.
    * At start, the seed alone runs (`on_boot`): a new install gets the demo,
      an existing one gets back anything missing.

After a reset the demo's own test round runs again (demo_mode/warmup.py) and
its sample-traffic generator holds its opening burst
(demo_mode/sample_traffic.py), so no report stays empty.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from demo_mode import settings

logger = logging.getLogger(__name__)

_NIL = "00000000-0000-0000-0000-000000000000"

# (table, primary key) wiped whole, children before parents.
_WIPE_ALL = (
    ("agent_eval_runs", "run_id"),
    ("agent_eval_schedules", "schedule_id"),
    ("agent_bakeoffs", "bakeoff_id"),
    ("agent_eval_suites", "suite_id"),
    ("agent_e2e_runs", "run_id"),
    ("agent_test_runs", "run_id"),
    ("test_fixtures", "fixture_id"),
    ("ai_voice_recording_chunks", "recording_id"),
    ("ai_voice_recordings", "recording_id"),
    ("ai_chat_messages", "id"),
    ("ai_chat_sessions", "id"),
    ("agent_traces", "trace_id"),
    ("ai_conversation_insights", "session_id"),
    ("ai_user_memory", "id"),
    ("agent_skills", "skill_id"),
    ("ai_kb_embeddings", "id"),
    ("ai_kb_pages", "id"),
    ("agent_data_sources", "source_id"),
    ("agent_channels", "channel_id"),
    ("agent_types", "agent_type_id"),
    ("admin_jobs", "job_id"),
    ("agent_audit_log", "audit_id"),
)


def _client():
    from core.auth import get_service_client

    return get_service_client()


def _rows(resp: Any) -> List[Dict[str, Any]]:
    return list(getattr(resp, "data", None) or [])


# Conversation tables: (primary key, the column that names the session). With
# sample conversations to keep, only the other sessions' rows are deleted.
_BY_SESSION = {
    "ai_chat_messages": ("id", "session_id"),
    "ai_chat_sessions": ("id", "id"),
    "agent_traces": ("trace_id", "session_id"),
    "ai_conversation_insights": ("session_id", "session_id"),
}


def _delete_all(client, table: str, pk: str) -> None:
    """DELETE every row. The REST layer refuses a DELETE with no filter, so
    the filter is one every row passes."""
    try:
        client.table(table).delete().neq(pk, _NIL).execute()
    except Exception as exc:  # noqa: BLE001 — a missing table on an older schema
        logger.warning("demo reset: could not clear %s (%s)", table, type(exc).__name__)


def _restore_template_instructions(client, templates: List[Dict[str, Any]]) -> int:
    """Templates cannot be edited, but their instructions are prompt rows a
    visitor could add a version to and serve. Put each template back on its
    prompt's first version and delete the later ones. Returns the versions
    removed."""
    profiles = [t for t in templates if t.get("profile_id")]
    if not profiles:
        return 0
    full = _rows(client.table("agent_profiles").select("profile_id, prompt_version_ids")
                 .in_("profile_id", [t["profile_id"] for t in profiles]).execute())
    removed = 0
    for row in full:
        pins = dict(row.get("prompt_version_ids") or {})
        changed = False
        for key, version_id in list(pins.items()):
            pinned = _rows(client.table("agent_prompt_versions").select("prompt_id")
                           .eq("prompt_version_id", version_id).limit(1).execute())
            if not pinned:
                continue
            versions = sorted(
                _rows(client.table("agent_prompt_versions")
                      .select("prompt_version_id, version_number")
                      .eq("prompt_id", pinned[0]["prompt_id"]).execute()),
                key=lambda v: int(v.get("version_number") or 0))
            if not versions:
                continue
            first = versions[0]["prompt_version_id"]
            later = [v["prompt_version_id"] for v in versions[1:]]
            if later:
                client.table("agent_prompt_versions").delete().in_("prompt_version_id", later).execute()
                removed += len(later)
            if pins[key] != first:
                pins[key], changed = first, True
        if changed:
            client.table("agent_profiles").update({"prompt_version_ids": pins}) \
                .eq("profile_id", row["profile_id"]).execute()
    return removed


def _delete_but_kept(client, table: str, keep: frozenset) -> None:
    """Delete every row of a conversation table but the kept sessions'. If
    the table cannot be read, it is cleared whole."""
    from demo_mode import retention

    pk, column = _BY_SESSION[table]
    try:
        retention.delete_except(client, table, pk, column, keep)
    except Exception as exc:  # noqa: BLE001 — in doubt, everything goes
        logger.warning("demo reset: %s not filtered (%s); clearing it", table, type(exc).__name__)
        _delete_all(client, table, pk)


def wipe(client, keep_sessions: frozenset = frozenset()) -> Dict[str, int]:
    """Delete what visitors could have made. Returns rows removed per kind.
    `keep_sessions`: the sample conversations this reset keeps
    (demo_mode/retention.py); every other conversation goes."""
    counts: Dict[str, int] = {}
    profiles = _rows(client.table("agent_profiles").select("profile_id, slug, status")
                     .eq("agent_kind", "custom").execute())
    templates = {p["slug"] for p in profiles if p.get("status") == "template"}
    doomed = [p for p in profiles if p.get("status") != "template"]
    for table, pk in _WIPE_ALL:
        if keep_sessions and table in _BY_SESSION:
            _delete_but_kept(client, table, keep_sessions)
        else:
            _delete_all(client, table, pk)
    # Agents: every custom agent but the templates (versions, attachments and
    # hand-off rows go with them: ON DELETE CASCADE).
    ids = [p["profile_id"] for p in doomed]
    if ids:
        client.table("agent_profiles").update({"served_version_id": None,
                                               "parent_profile_id": None}) \
            .in_("profile_id", ids).execute()
        client.table("agent_profiles").delete().in_("profile_id", ids).execute()
    counts["agents"] = len(ids)
    # Each agent's own instructions (`<slug>:system`), never a template's.
    from admin.services.custom_prompts import own_prompt_slug

    prompt_slugs = sorted({own_prompt_slug(p["slug"]) for p in doomed if p["slug"] not in templates})
    if prompt_slugs:
        client.table("agent_prompts").delete().eq("agent_kind", "custom") \
            .in_("slug", prompt_slugs).execute()
    counts["instructions"] = len(prompt_slugs)
    counts["template_instructions"] = _restore_template_instructions(
        client, [p for p in profiles if p.get("status") == "template"])
    # Connections a visitor added: external MCP servers and HTTP tools.
    client.table("agent_mcp_servers").delete().eq("source", "external") \
        .eq("is_builtin", False).execute()
    client.table("agent_tools").delete().eq("kind", "http").execute()
    try:
        from runtime.tools._mcp_adapter import clear_mcp_pool

        clear_mcp_pool()
    except Exception:  # noqa: BLE001
        pass
    return counts


def reseed_engine() -> List[str]:
    """Run the engine's own boot seeders again (all insert-if-missing), so
    a row they make that the wipe took (a default channel, say) is back
    before visitors return. The ones demo mode skips at start are skipped
    here too. Returns the seeders that failed."""
    import importlib
    import sys

    main = sys.modules.get("main") or importlib.import_module("main")
    failed = []
    for module_path in main._BOOT_SEEDERS:
        if module_path in main._DEMO_SKIPPED_SEEDERS:
            continue
        try:
            importlib.import_module(module_path).run()
        except Exception as exc:  # noqa: BLE001 — one seeder never blocks the rest
            logger.warning("demo reset: seeder %s failed (%s)", module_path, type(exc).__name__)
            failed.append(module_path)
    return failed


def reset(client=None, *, engine_seeders: bool = True) -> Dict[str, Any]:
    """Wipe and seed again. Raises on a database error (the caller logs it)."""
    from demo_mode import seed

    from demo_mode import retention, trace_store

    client = client or _client()
    # Which of the demo's own sample conversations stay (none on any doubt).
    # Decided before anything is deleted; the trace store goes first, and if
    # it could not keep them the database keeps none either.
    kept = retention.plan(client)
    traces = trace_store.wipe(kept.traces)
    if traces is not None and kept.traces and not traces.get("kept_traces"):
        kept = retention.NOTHING
    removed = wipe(client, kept.sessions)
    if traces is not None:
        removed["trace_tables"] = len(traces.get("emptied") or [])
    removed["sample_conversations_kept"] = len(kept.sessions)
    retention.mark_verified(client, kept.to_verify)
    if engine_seeders:
        reseed_engine()
    seeded = seed.seed(client)
    # The agents are as seeded now: what later sample conversations are compared with.
    retention.write_baseline(client)
    try:
        from core.agent_profile import AgentProfileLoader

        AgentProfileLoader.invalidate("custom", seeded["environment"])
    except Exception:  # noqa: BLE001
        pass
    try:
        from demo_mode import protect

        protect.forget()  # new ids: the tour's reset id changes with them
    except Exception:  # noqa: BLE001
        pass
    logger.info("demo reset: removed %s; seeded the Northwind demo again", removed)
    return {"removed": removed, "seeded": seeded}


# --------------------------------------------------------------------------
# Boot and the nightly loop
# --------------------------------------------------------------------------


def _boot_sync() -> None:
    from demo_mode import account, seed

    try:
        account.ensure_demo_user()
    except Exception as exc:  # noqa: BLE001 — the entry page says so instead
        logger.error("demo: could not make the demo account (%s: %s)", type(exc).__name__, exc)
    new_install = _no_demo_agents()
    try:
        seed.seed()
    except Exception as exc:  # noqa: BLE001 — never gate the engine's start
        logger.error("demo: seeding the Northwind demo failed (%s: %s)", type(exc).__name__, exc)
        return
    if new_install:
        # Only a seed that built the demo from nothing is "as seeded". After
        # a restart the agents may carry a visitor's edits: the baseline of
        # the last reset stays.
        from demo_mode import retention

        retention.write_baseline()
    _start_warmup()


def _no_demo_agents() -> bool:
    """True when none of the demo's agents exists yet (a new install)."""
    from demo_mode import content, seed

    try:
        slugs = [a["slug"] for a in content.AGENTS]
        return not seed._rows(seed._client().table("agent_profiles").select("profile_id")
                              .eq("agent_kind", "custom").in_("slug", slugs).limit(1).execute())
    except Exception:  # noqa: BLE001 — unknown: not a new install
        return False


def _start_warmup() -> None:
    """The demo's own test round (demo_mode/warmup.py), in the background."""
    try:
        from demo_mode import warmup

        warmup.start_in_background()
    except Exception as exc:  # noqa: BLE001 — the demo works without it
        logger.warning("demo: test round not started (%s)", type(exc).__name__)


def _request_sample_burst() -> None:
    """After a reset the reports are empty: ask this process's sample-traffic
    generator (demo_mode/sample_traffic.py) for its opening burst."""
    try:
        from demo_mode import sample_traffic

        sample_traffic.request_burst()
    except Exception as exc:  # noqa: BLE001 — the demo works without it
        logger.warning("demo: sample burst not requested (%s)", type(exc).__name__)


def seconds_until(hour: int, now: Optional[datetime] = None) -> float:
    now = now or datetime.now(timezone.utc)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def run_once_today() -> bool:
    """Reset unless another process already did today. True when this one did."""
    from demo_mode import limits

    if not limits.take(limits.RESET_BUCKET, 1):
        return False
    reset()
    _start_warmup()
    _request_sample_burst()
    return True


async def run_loop() -> None:
    while True:
        hour = settings.reset_hour_utc()
        if hour is None:
            return
        await asyncio.sleep(seconds_until(hour))
        try:
            await asyncio.to_thread(run_once_today)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — try again tomorrow
            logger.error("demo reset failed (%s: %s)", type(exc).__name__, exc)
        await asyncio.sleep(60)


async def on_boot() -> Optional[asyncio.Task]:
    """At start in demo mode: the demo account and the demo, then the
    background task (the nightly reset and the sample traffic; returned, for
    the lifespan to cancel). None outside demo mode."""
    if not settings.enabled():
        return None
    await asyncio.to_thread(_boot_sync)
    if settings.reset_hour_utc() is None:
        logger.info("demo: nightly reset is off (DEMO_RESET_HOUR_UTC=off)")
    return asyncio.create_task(_background())


async def _background() -> None:
    """The demo's background work, as one task the lifespan cancels: the
    nightly reset (returns at once when it is off) and the sample traffic
    (returns at once when it is off)."""
    from demo_mode import sample_traffic

    await asyncio.gather(run_loop(), sample_traffic.run_loop())
