"""Lightweight tracker for admin fire-and-forget daemon threads.

The admin portal triggers a handful of long-running side-effects
(eval-suite runs, KB ingestion, deploy dispatches) by spawning a daemon
thread and returning the HTTP redirect immediately. Before this module
existed, the only observability for those threads was a log line if they
crashed and whatever the worker happened to write to its own status
column (e.g. ``agent_eval_runs.status``). Operators kept asking "did
that eval ever start?" / "is the KB upload stuck?" — the answer required
SSH'ing into the server to grep logs.

`start_tracked_job` is a thin wrapper around the existing daemon-thread
pattern. It writes a row to ``public.admin_jobs`` with status='queued',
spawns the worker, and the worker (via the wrapper) transitions
running → succeeded/failed and stamps ``duration_ms``. A crashed worker
lands as status='failed' with ``error`` set to the traceback class name
+ message, so we never silently lose telemetry again.

### Cancellation semantics — COOPERATIVE, not preemptive

Python's threading module can't safely kill a thread mid-stride
(``Thread._stop()`` is gone, and PyThreadState_SetAsyncExc is racy and
brittle on third-party C extensions like google-generativeai). So
`cancel_job` only flips the row to status='cancelled' and the operator
sees the intent reflected in the UI immediately. Workers that want to
honor cancellation must call `is_cancelled(job_id)` at natural
checkpoints (between scenarios in an eval, between files in an upload,
between RPC retries in a deploy poll, …) and break out of their loop
when it returns True. Workers that don't check will finish naturally
and the row will end up `succeeded`/`failed` — that's a feature, not a
bug: it means partial progress already committed (e.g. half the eval
scenarios) stays committed.

### DB writes per transition

To keep the tracker honest about progress we commit on every state
edge: insert at queued, update to running once the worker starts,
update to succeeded/failed/cancelled at finish. That's 2-3 round trips
per job — negligible against the eval runtime (10-60s/scenario) and
worth it for the operator-visible progress in the UI.
"""

from __future__ import annotations

import contextvars
import inspect
import logging
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

# Job-tracker rows follow the env-switcher: `start_tracked_job` inserts the
# queued row in the REQUEST context (middleware-set `_ADMIN_ENV`), the worker
# re-sets the captured env before its running/terminal updates (daemon
# threads don't inherit ContextVars — see `_run`), and the /admin/jobs reads
# run inside admin requests — so all three resolve the SAME branch. With no
# env selected (/mcp, local dev) this falls back to the local branch exactly
# like the old bare `get_service_client()`.
from core.auth import admin_service_client

logger = logging.getLogger(__name__)


# Allowed kinds — mirrors the CHECK constraint in
# supabase/migrations/20260520210000_admin_jobs.sql. Kept here so callers
# get a clear ValueError instead of a Postgres 23514.
ALLOWED_KINDS = frozenset(
    {"eval_run", "kb_upload", "deploy_dispatch", "env_promote", "tool_dry_run", "other"}
)
ALLOWED_STATUSES = frozenset(
    {"queued", "running", "succeeded", "failed", "cancelled"}
)


def _now_iso() -> str:
    """UTC ISO-8601 with a trailing ``+00:00`` so Postgres parses it as
    timestamptz cleanly across all migrations."""
    return datetime.now(timezone.utc).isoformat()


def _insert_queued_row(
    *,
    kind: str,
    label: str,
    actor_user_id: Optional[str],
    related_entity_type: Optional[str],
    related_entity_id: Optional[str],
    metadata: Optional[Dict[str, Any]],
) -> str:
    """Write the initial queued row and return the generated job_id.

    We let Postgres generate the uuid (``gen_random_uuid()`` default)
    instead of minting one in Python so callers don't need a uuid import
    and there's a single source of truth for the id.
    """
    client = admin_service_client()
    payload: Dict[str, Any] = {
        "kind": kind,
        "label": label,
        "status": "queued",
        "triggered_by": actor_user_id,
        "metadata": metadata or {},
    }
    if related_entity_type:
        payload["related_entity_type"] = related_entity_type
    if related_entity_id:
        payload["related_entity_id"] = related_entity_id

    result = client.table("admin_jobs").insert(payload).execute()
    rows = result.data or []
    if not rows:
        # Supabase should always return the inserted row, but if the
        # supabase-py client is misconfigured (e.g. no `returning=*`)
        # we'd rather crash loudly than silently lose the tracker row.
        raise RuntimeError("admin_jobs insert returned no rows")
    return str(rows[0]["job_id"])


def _update_job(job_id: str, payload: Dict[str, Any]) -> None:
    """Best-effort row update. We don't want tracker failures to mask the
    underlying worker's real outcome — log and move on."""
    try:
        client = admin_service_client()
        client.table("admin_jobs").update(payload).eq("job_id", job_id).execute()
    except Exception as exc:  # pragma: no cover - depends on remote DB
        logger.warning("admin_jobs update failed for %s (%s)", job_id, exc)


def _fetch_status(job_id: str) -> Optional[str]:
    """Read the current row status. Returns None on missing row / DB error."""
    try:
        client = admin_service_client()
        result = (
            client.table("admin_jobs")
            .select("status")
            .eq("job_id", job_id)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            return None
        return rows[0].get("status")
    except Exception as exc:  # pragma: no cover - depends on remote DB
        logger.warning("admin_jobs status fetch failed for %s (%s)", job_id, exc)
        return None


def start_tracked_job(
    fn: Callable,
    *,
    kind: str,
    label: str,
    actor_user_id: Optional[str],
    related_entity_type: Optional[str] = None,
    related_entity_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    **fn_kwargs,
) -> str:
    """Insert a queued admin_jobs row, spawn a daemon thread, return job_id.

    The spawned thread:
      1. Updates the row to status='running' with ``started_at``.
      2. Calls ``fn(**fn_kwargs)``.
      3. On clean return → status='succeeded' + duration_ms.
      4. On exception   → status='failed', error = ``f"{cls}: {msg}"``,
                          plus the full traceback in ``metadata.traceback``
                          so the UI can show it without bloating the
                          primary ``error`` column.
      5. If the row was flipped to 'cancelled' before the worker
         finished, we preserve that status — we trust the operator's
         intent over the worker's natural ending. (The worker's own
         tables like agent_eval_runs still reflect whatever partial
         progress committed.)

    Returns the job_id so the caller can attach it to a redirect (e.g.
    ``?job=<id>``) for deep-linking into the jobs UI.

    ### Cooperative-cancellation convention: ``_job_id`` kwarg

    Workers that want to honor cancel mid-run can opt in by accepting an
    ``_job_id`` keyword argument in their signature. When present, the
    tracker threads the freshly-inserted ``job_id`` through as
    ``fn(_job_id=job_id, **fn_kwargs)`` so the worker can poll
    ``is_cancelled(_job_id)`` at natural checkpoints (between scenarios,
    between files, …) and break out of its loop.

    Workers that don't care just don't declare ``_job_id`` and get the
    legacy ``fn(**fn_kwargs)`` call — fully back-compat. Detection uses
    `inspect.signature` so the choice is per-function, not per-caller.
    """
    if kind not in ALLOWED_KINDS:
        raise ValueError(f"admin_jobs: unknown kind {kind!r}")

    job_id = _insert_queued_row(
        kind=kind,
        label=label,
        actor_user_id=actor_user_id,
        related_entity_type=related_entity_type,
        related_entity_id=related_entity_id,
        metadata=metadata,
    )

    # Sniff the worker signature once, here, so the hot path of `_run`
    # below stays branch-free. `inspect.signature` can throw on builtins
    # / C-extensions — fall back to "no opt-in" in that case rather than
    # blowing up the whole job kickoff.
    accepts_job_id = False
    try:
        sig = inspect.signature(fn)
        params = sig.parameters
        # Explicit `_job_id` parameter, OR a **kwargs catch-all (in which
        # case it's safe to forward — the worker can ignore it).
        accepts_job_id = "_job_id" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        accepts_job_id = False

    started_at = _now_iso()
    started_perf = time.perf_counter()

    # Capture the request-scoped admin env (the /admin env switcher) NOW, in
    # the caller's context. The daemon thread below does NOT inherit
    # ContextVars, so we re-set it inside `_run` — this makes background jobs
    # (eval/e2e/KB-ingest runners) read AND write the SAME branch the operator
    # had selected, instead of always the deployment's local branch.
    from core.auth import get_admin_env as _get_admin_env
    _captured_admin_env = _get_admin_env()

    def _run() -> None:
        from core.auth import set_admin_env as _set_admin_env
        _set_admin_env(_captured_admin_env)
        # Transition queued → running. We do this from inside the worker
        # rather than the caller so a runaway thread pool doesn't leave
        # us with rows that say "running" but never actually started.
        _update_job(job_id, {"status": "running", "started_at": started_at})
        try:
            # Run the worker inside a COPY of this thread's context (captured
            # env already applied), mirroring _query_batch.run_parallel. A
            # worker that pins its own env — voice_scenario_runner.
            # run_scenario_job calls set_admin_env(environment) — must not
            # leak that pin into the tracker's terminal update below: the
            # queued/running rows land on the captured branch, so a leaked
            # pin would send the terminal UPDATE to a different branch — a
            # silent zero-row PATCH and a job stuck "running" forever.
            _worker_ctx = contextvars.copy_context()
            if accepts_job_id:
                _worker_ctx.run(fn, _job_id=job_id, **fn_kwargs)
            else:
                _worker_ctx.run(fn, **fn_kwargs)
            duration_ms = int((time.perf_counter() - started_perf) * 1000)
            # Cancellation guard: if the operator cancelled mid-run,
            # don't clobber that status with 'succeeded'.
            current = _fetch_status(job_id)
            final_status = "cancelled" if current == "cancelled" else "succeeded"
            _update_job(
                job_id,
                {
                    "status": final_status,
                    "finished_at": _now_iso(),
                    "duration_ms": duration_ms,
                },
            )
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started_perf) * 1000)
            tb = traceback.format_exc()
            cls_name = type(exc).__name__
            error_msg = f"{cls_name}: {exc}"
            # Preserve full traceback in metadata so the detail panel can
            # render it without making the error column unreadable.
            existing_meta = dict(metadata or {})
            existing_meta["traceback"] = tb
            current = _fetch_status(job_id)
            final_status = "cancelled" if current == "cancelled" else "failed"
            _update_job(
                job_id,
                {
                    "status": final_status,
                    "finished_at": _now_iso(),
                    "duration_ms": duration_ms,
                    "error": error_msg,
                    "metadata": existing_meta,
                },
            )
            logger.exception("admin_jobs[%s] (%s) crashed", job_id, label)

    t = threading.Thread(target=_run, name=f"job:{label}", daemon=True)
    t.start()
    return job_id


def cancel_job(job_id: str) -> bool:
    """Cooperative cancel — flip status='cancelled' so the worker sees it
    on its next ``is_cancelled`` checkpoint and the UI shows the
    operator's intent. We do NOT preempt the thread (see module docstring).

    Returns True if the flip happened, False if the row was already in a
    terminal state (succeeded/failed/cancelled) — that way the UI can
    show a "too late" toast instead of pretending the cancel landed.
    """
    current = _fetch_status(job_id)
    if current is None:
        return False
    if current in ("succeeded", "failed", "cancelled"):
        return False
    _update_job(
        job_id,
        {"status": "cancelled", "finished_at": _now_iso()},
    )
    return True


def is_cancelled(job_id: str) -> bool:
    """Read the row status; True iff status='cancelled' in the DB.

    Designed to be called from inside long worker loops (between eval
    scenarios, between uploaded files, between deploy poll attempts).
    Returns False on missing row / DB error so a transient Supabase
    blip doesn't accidentally abort the worker.
    """
    return _fetch_status(job_id) == "cancelled"


def merge_job_metadata(job_id: str, patch: Dict[str, Any]) -> None:
    """Merge ``patch`` into the row's ``metadata`` jsonb column.

    Used by workers that want to publish progress mid-run (e.g. KB
    ingestion stamps ``chunk_count`` / ``embed_ms_total`` so the
    detail panel + polling JSON show live counters). Best-effort —
    swallows DB errors so a transient Supabase blip never crashes the
    worker thread (the tracker's terminal-state update still runs).
    """
    if not patch:
        return
    try:
        client = admin_service_client()
        result = (
            client.table("admin_jobs")
            .select("metadata")
            .eq("job_id", job_id)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        current = (rows[0].get("metadata") if rows else None) or {}
        if not isinstance(current, dict):
            current = {}
        merged = {**current, **patch}
        client.table("admin_jobs").update({"metadata": merged}).eq(
            "job_id", job_id
        ).execute()
    except Exception as exc:  # pragma: no cover - depends on remote DB
        logger.warning(
            "admin_jobs metadata merge failed for %s (%s)", job_id, exc
        )


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single admin_jobs row by id. Returns None on missing row
    or DB error — the JSON polling endpoint maps that to a 404.
    """
    try:
        client = admin_service_client()
        result = (
            client.table("admin_jobs")
            .select("*")
            .eq("job_id", job_id)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        return dict(rows[0]) if rows else None
    except Exception as exc:  # pragma: no cover - depends on remote DB
        logger.warning("admin_jobs fetch failed for %s (%s)", job_id, exc)
        return None


def list_recent_jobs(
    *,
    limit: int = 50,
    status: Optional[str] = None,
    kind: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Source for the /admin/jobs UI.

    Newest first by ``created_at``. ``status`` / ``kind`` filters are
    optional and validated against the CHECK-constrained allow-lists so
    a malicious query param can't smuggle SQL through the supabase-py
    ``eq`` call (it parameterizes anyway, but tightening here means we
    fail closed early instead of returning an empty result silently).
    """
    if status is not None and status not in ALLOWED_STATUSES:
        return []
    if kind is not None and kind not in ALLOWED_KINDS:
        return []

    client = admin_service_client()
    q = client.table("admin_jobs").select("*")
    if status:
        q = q.eq("status", status)
    if kind:
        q = q.eq("kind", kind)
    q = q.order("created_at", desc=True).limit(max(1, min(limit, 500)))
    try:
        result = q.execute()
        return list(result.data or [])
    except Exception as exc:  # pragma: no cover - depends on remote DB
        logger.warning("admin_jobs list failed (%s)", exc)
        return []
