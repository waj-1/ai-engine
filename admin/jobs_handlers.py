"""Admin /jobs UI — sidecar route module.

Attaches these routes to the shared admin router:

    GET  /admin/jobs                 — Background jobs (Settings › Diagnostics)
    GET  /admin/jobs/{job_id}.json   — polling source for progress panels
    POST /admin/jobs/{job_id}/cancel — cooperative cancel (see
                                       admin/_jobs.py module docstring).

The screen lived as a "Jobs" tab of the Overview (/admin/live?tab=jobs) until
the 2026-10 portal revamp moved it to Settings › Diagnostics — background admin
work is not monitoring. /admin/live?tab=jobs now 307s here. jobs.html includes
_tab_jobs.html, whose context comes from ``build_jobs_context`` below.

Lives outside ``admin/router.py`` because router.py is off-limits for
this iteration. Pattern matches the other sidecars (kb_handlers,
tool_handlers, observability_explorer_handlers): import the shared
``router`` from admin.router and decorate.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from admin._jobs import (
    ALLOWED_KINDS,
    ALLOWED_STATUSES,
    cancel_job,
    get_job,
    list_recent_jobs,
)
from admin.audit import record_change
from admin.auth import require_superuser
from admin.router import current_env, router, templates

logger = logging.getLogger(__name__)


# Ordered so the UI filter pills render in the most natural reading
# order (queued/running first — that's what an operator opening the
# page actually wants to see). frozensets in _jobs.py are unordered,
# so we keep an authoritative tuple here for templating.
_KIND_OPTIONS = (
    "eval_run", "kb_upload", "deploy_dispatch",
    "env_promote", "tool_dry_run", "other",
)
_STATUS_OPTIONS = ("queued", "running", "succeeded", "failed", "cancelled")


def _status_variant(status: str) -> str:
    """Map a job status to a chip variant used by templates/_components/chip.html.

    Centralized here so the template stays declarative and we can tweak
    the palette without grepping Jinja files.
    """
    return {
        "queued": "ghost",
        "running": "brand",
        "succeeded": "mauve",
        "failed": "err",
        "cancelled": "warn",
    }.get(status, "ghost")


def _kind_variant(kind: str) -> str:
    """Kind chips share a softer palette than status — kinds are
    descriptive metadata, not action-required state."""
    return {
        "eval_run": "brand",
        "kb_upload": "mauve",
        "deploy_dispatch": "warn",
        "env_promote": "warn",
        "tool_dry_run": "ghost",
        "other": "ghost",
    }.get(kind, "ghost")


# ---------------------------------------------------------------------------
# Jobs tab of /admin/live (build_jobs_context) + back-compat redirect for
# GET /admin/jobs
# ---------------------------------------------------------------------------


def build_jobs_context(
    status: Optional[str] = None,
    kind: Optional[str] = None,
    limit: "int | str" = 50,
) -> Dict[str, Any]:
    """Build the template context for the Jobs tab of /admin/live.

    Recent-jobs list with kind/status filters, newest-first, plus the
    in-place poller flag. Filter values that fail the allow-list check
    produce an empty result (handled inside ``list_recent_jobs``) rather
    than a 422 — operators typing in the URL bar shouldn't get a stack
    trace, just an empty list and a chance to clear filters. Returns the
    data keys only — the caller adds request/ctx/active/current_env
    boilerplate.
    """
    # Normalize empty-string query params to None so the filter pills
    # behave (an "all" link sends `?status=` which arrives as "").
    status_filter = status or None
    kind_filter = kind or None
    # Clamp limit by hand — Query(ge=..., le=...) validation used to do
    # this; the host route forwards a raw query param now. Garbage input
    # falls back to the default instead of a 422/500.
    try:
        limit = int(limit or 50)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 500))

    rows = list_recent_jobs(limit=limit, status=status_filter, kind=kind_filter)
    has_in_flight = any(r.get("status") in ("queued", "running") for r in rows)

    return {
        "rows": rows,
        "status_options": _STATUS_OPTIONS,
        "kind_options": _KIND_OPTIONS,
        "status_filter": status_filter,
        "kind_filter": kind_filter,
        "status_variant": _status_variant,
        "kind_variant": _kind_variant,
        "has_in_flight": has_in_flight,
    }


@router.get("/jobs", name="jobs_list")
async def jobs_list(
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Background jobs — eval runs, KB ingestion, deploy dispatches.

    Settings › Diagnostics (staff screen). Filters arrive as raw query params
    and are normalised by ``build_jobs_context``."""
    qp = request.query_params
    jobs_ctx = build_jobs_context(
        status=qp.get("status", ""),
        kind=qp.get("kind", ""),
        limit=qp.get("limit", "") or 50,
    )
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {"ctx": ctx, "active": "jobs", "current_env": env, **jobs_ctx},
    )


# ---------------------------------------------------------------------------
# GET /admin/jobs/{job_id}.json — polling source for live-progress panels
# ---------------------------------------------------------------------------


@router.get("/jobs/{job_id}.json", name="jobs_json")
async def jobs_json(
    job_id: str,
    ctx: dict = Depends(require_superuser),
) -> JSONResponse:
    """Return the raw admin_jobs row as JSON for client-side polling.

    Used by the KB ingestion progress panel (templates/kb_detail.html)
    to surface live chunk counts + status transitions without a full
    page reload. 404s when the row is missing so the client can stop
    polling instead of looping forever.
    """
    row = get_job(job_id)
    if row is None:
        raise HTTPException(404, "job not found")
    return JSONResponse(row)


# ---------------------------------------------------------------------------
# POST /admin/jobs/{job_id}/cancel
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/cancel", name="jobs_cancel")
async def jobs_cancel(
    job_id: str,
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Flip the row to status='cancelled' if it's still in flight.

    Audits unconditionally so we have a record of the operator's intent
    even when the worker had already finished (returns False from
    cancel_job in that case — that's not an error, it's "too late").
    """
    flipped = cancel_job(job_id)
    record_change(
        entity_type="admin_jobs",
        entity_id=job_id,
        action="cancel",
        actor_user_id=ctx["user_id"],
        after={"cancel_applied": flipped},
    )
    # Preserve filters via the Referer header so the operator lands back
    # on the filtered view they were already looking at. Falls back to
    # the Jobs screen when Referer is missing (e.g. curl).
    referer = request.headers.get("referer") or "/admin/jobs"
    return RedirectResponse(url=referer, status_code=302)
