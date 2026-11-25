"""The demo's own test round, so Testing is never empty.

After the demo is seeded (at start, and after each nightly reset) this runs,
in a background thread and one after another:

* each demo agent's test suite once, recorded against its schedule
  (Testing > Runs, Testing > Schedules, the agent cards on Testing);
* a few real conversations with the demo's agents, the way a visitor's
  website would hold them (Conversations, and Overview where tracing is on:
  test runs are left out of both on purpose), unless the sample-traffic
  generator is on, which holds them instead, all day
  (demo_mode/sample_traffic.py);
* a model comparison per agent: the same suite on the agents' model and on
  the builder's model (Testing > Model comparison).

These are real runs: real agent turns and real judge calls, counted against
the day's cap like a visitor's (`demo_mode.limits`). A round is skipped when
runs already exist, so a restart does not repeat it. ``DEMO_WARMUP=off``
turns it off.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from demo_mode import content, settings

logger = logging.getLogger(__name__)

_TERMINAL = {"passed", "failed", "errored", "cancelled"}
_WAIT_SEC = 300.0
_POLL_SEC = 3.0
_lock = threading.Lock()


def enabled() -> bool:
    return settings.enabled() and (os.getenv("DEMO_WARMUP") or "on").strip().lower() not in (
        "off", "0", "false", "no")


def _rows(resp: Any) -> List[Dict[str, Any]]:
    return list(getattr(resp, "data", None) or [])


def already_ran(client) -> bool:
    return bool(_rows(client.table("agent_eval_runs").select("run_id").limit(1).execute()))


def _wait(client, run_ids: List[str], sleep=time.sleep) -> Dict[str, str]:
    """Block until every run is finished (or five minutes pass). {run_id: status}."""
    deadline = time.monotonic() + _WAIT_SEC
    status: Dict[str, str] = {}
    while run_ids:
        rows = _rows(client.table("agent_eval_runs").select("run_id, status")
                     .in_("run_id", run_ids).execute())
        status = {r["run_id"]: str(r.get("status") or "") for r in rows}
        if all(status.get(r) in _TERMINAL for r in run_ids) or time.monotonic() > deadline:
            break
        sleep(_POLL_SEC)
    return status


#: Real conversations held once per round: (agent slug, the customer's
#: messages in order). Each is one saved conversation.
CHATS = (
    (content.SUPPORT_SLUG, ("My internet has been down since this morning in Riverside. "
                            "Is there an outage?",
                            "It is still down. Can you open a ticket for a technician?")),
    (content.SUPPORT_SLUG, ("Where is my order NW-1042?",)),
    (content.SALES_SLUG, ("Which plan do you recommend for a family of four that streams a lot?",
                          "Our house is about 200 square metres. Will the Wi-Fi reach?")),
    (content.BILLING_SLUG, ("When is my bill issued, and what happens if I pay late?",)),
    (content.IT_SLUG, ("I forgot my password and I am working from home. What do I do?",)),
)


def run_chats(token: str, env: str) -> int:
    """Hold the conversations in `CHATS` through the agents' own chat
    address, in this process. Returns how many turns were answered. A turn
    that fails is logged and skipped."""
    import asyncio
    import uuid

    from admin_mcp.tools_dispatch import _post_chat, parse_sse

    # The sample channel's own origin, the way a website's page names itself:
    # without one the request matches no channel and the conversation is
    # listed under "Unknown".
    from demo_mode import sample_traffic

    headers = sample_traffic.chat_headers(token)
    answered = 0
    for slug, messages in CHATS:
        session_id = str(uuid.uuid4())
        for message in messages:
            try:
                status, _ctype, text, _target = asyncio.run(_post_chat(
                    f"/v1/agents/{slug}/chat", {"message": message, "session_id": session_id},
                    headers, env, 90))
                if status >= 400:
                    logger.warning("demo warm-up: chat with %s answered %s", slug, status)
                    break
                session_id = parse_sse(text).get("session_id") or session_id
                answered += 1
            except Exception as exc:  # noqa: BLE001 — the round goes on
                logger.warning("demo warm-up: chat with %s failed (%s)", slug, type(exc).__name__)
                break
    return answered


def _next_run(cadence: str, now: datetime) -> str:
    hour = settings.reset_hour_utc()
    nxt = now.replace(hour=hour if hour is not None else 3, minute=10, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    if cadence == "weekly":
        nxt += timedelta(days=6)
    return nxt.isoformat()


def run_round(client=None, *, sleep=time.sleep) -> Dict[str, Any]:
    """Run the round now. Blocking (minutes). Returns what it started."""
    from admin.services.bakeoffs import create_bakeoff, get_bakeoff
    from admin.services.evals import start_suite_run
    from demo_mode import account, seed

    client = client or seed._client()
    if already_ran(client):
        return {"skipped": "runs exist"}
    env = seed._env()
    token = account.sign_in()
    actor = str((account.ensure_demo_user() or {}).get("user_id") or "") or None

    def _id(table: str, pk: str, **where: Any) -> Optional[str]:
        query = client.table(table).select(pk)
        for key, value in where.items():
            query = query.eq(key, value)
        found = _rows(query.limit(1).execute())
        return found[0][pk] if found else None

    agent_model = _id("agent_models", "model_id", name=seed.MODEL_NAMES["agent"], environment=env)
    builder_model = _id("agent_models", "model_id", name=seed.MODEL_NAMES["builder"], environment=env)
    schedules = {s["agent"]: s for s in content.SCHEDULES}
    names = {a["slug"]: a["display_name"] for a in content.AGENTS}
    from demo_mode import sample_traffic

    # The sample-traffic generator holds the demo's conversations (its burst
    # runs at the same moments as this round); `CHATS` is for an install
    # that switched it off.
    chat_turns = 0 if sample_traffic.enabled() else run_chats(token, env)
    out: Dict[str, Any] = {"runs": [], "comparisons": [], "chat_turns": chat_turns}
    for spec in content.SUITES:
        slug = spec["agent"]
        profile_id = _id("agent_profiles", "profile_id", agent_kind="custom", slug=slug,
                         environment=env)
        suite_id = _id("agent_eval_suites", "suite_id", name=spec["name"])
        if not (profile_id and suite_id):
            continue
        try:
            schedule = schedules.get(slug)
            schedule_id = _id("agent_eval_schedules", "schedule_id", name=schedule["name"]) \
                if schedule else None
            run_id = start_suite_run(suite_id=suite_id, profile_id=profile_id, business_id=None,
                                     actor_user_id=actor, user_token=token,
                                     schedule_id=schedule_id)
            out["runs"].append(run_id)
            _wait(client, [run_id], sleep)
            if schedule_id:
                now = datetime.now(timezone.utc)
                client.table("agent_eval_schedules").update({
                    "last_run_at": now.isoformat(),
                    "next_run_at": _next_run(schedule["cadence"], now),
                }).eq("schedule_id", schedule_id).execute()
            if agent_model and builder_model and agent_model != builder_model:
                made = create_bakeoff(
                    name=f"{names.get(slug, slug)}: which model?", profile_id=profile_id,
                    variants=[{"label": seed.MODEL_NAMES["agent"], "model_id": agent_model},
                              {"label": seed.MODEL_NAMES["builder"], "model_id": builder_model}],
                    suite_ids=[suite_id], repeats=1, actor_user_id=actor, user_token=token)
                out["comparisons"].append(made.get("bakeoff_id"))
                _wait(client, list(made.get("run_ids") or []), sleep)
                # The comparisons list shows the stored status, which is only
                # brought up to date when a comparison is read.
                get_bakeoff(made["bakeoff_id"])
        except Exception as exc:  # noqa: BLE001 — one agent's round never stops the rest
            logger.warning("demo warm-up: %s failed (%s: %s)", slug, type(exc).__name__, exc)
    logger.info("demo warm-up: %d chat turn(s), %d test run(s), %d model comparison(s)",
                out["chat_turns"], len(out["runs"]), len(out["comparisons"]))
    return out


def _run_guarded() -> None:
    if not _lock.acquire(blocking=False):
        return  # a round is already running in this process
    try:
        run_round()
    except Exception as exc:  # noqa: BLE001 — the demo works without it
        logger.warning("demo warm-up failed (%s: %s)", type(exc).__name__, exc)
    finally:
        _lock.release()


def start_in_background() -> Optional[threading.Thread]:
    """Start the round in a daemon thread. None when it is switched off."""
    if not enabled():
        return None
    thread = threading.Thread(target=_run_guarded, name="demo-warmup", daemon=True)
    thread.start()
    return thread
