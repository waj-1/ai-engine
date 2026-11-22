"""Conversations screen (/admin/observability) — sidecar route module.

Routes attached to the shared admin `router`:

    GET /admin/observability/explorer
        The Conversations page (same page as /admin/observability; both
        render via ``render_conversations``). Left = one row per
        conversation, right = the conversation viewer (Conversation · Call
        audio · Technical). Selection is hash-routed (``#trace=…&span=…``).

    GET /admin/observability/explorer/trace/{trace_id}/transcript.json
    GET /admin/observability/explorer/trace/{trace_id}/overview.json
    GET /admin/observability/explorer/trace/{trace_id}/audio.json
    GET /admin/observability/explorer/trace/{trace_id}/spans.json
    GET /admin/observability/explorer/span/{trace_id}/{span_id}.json
        Pane payloads. Each carries ``found`` / ``unavailable`` so a bad or
        expired link reads "not found" and an outage reads "temporarily
        unavailable" — never a zero-filled record.

All of them resolve the environment with ``current_env_live`` — the same
default (production) as the Overview and Telemetry diagnose.

Why a sidecar instead of extending screens/observability.py and adding
routes to router.py: the file constraints for this iteration say
router.py is off-limits, and the existing pattern in this codebase for
"attach more routes to /admin" is exactly this — a module that
``from admin.router import router`` and decorates functions with it.
See ``tool_handlers.py``/``env_vars_handlers.py`` for the precedent.

Loading: this module is imported by ``main.py`` right after
``admin_router`` is included. The import itself triggers route
registration via decorator side-effect. We then reorder the route table
so the explorer's ``/observability/explorer*`` paths match BEFORE the
existing ``/observability/{trace_id}`` catch-all — without that nudge,
Starlette's first-match-wins dispatch would route ``/explorer`` into
the trace-detail handler with ``trace_id="explorer"``.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from admin.auth import require_superuser
from admin.router import current_env_live, router, templates

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTML shell
# ---------------------------------------------------------------------------


def render_conversations(
    request: Request,
    *,
    env: str,
    ctx: dict,
    hours: int,
    status: Optional[str],
    outcome: Optional[str],
    search: Optional[str],
    tool: Optional[str],
    channel_slug: Optional[str],
    agent_kind: Optional[str],
    group: str,
    background: int,
) -> HTMLResponse:
    """Render the Conversations screen — shared by /admin/observability and
    /admin/observability/explorer (same page, two historical URLs).

    The shell must render even when ClickHouse is unreachable: the list shows
    the page's status strip and the panes hydrate client-side from the JSON
    endpoints below, each with its own not-found / unavailable state."""
    from admin.screens.conversations import build_conversations_context

    context = build_conversations_context(
        env=env, hours=hours, search=search, status=status, outcome=outcome,
        tool=tool, channel_slug=channel_slug, agent_kind=agent_kind,
        group=group, background=background,
    )
    return templates.TemplateResponse(
        request,
        "observability_explorer.html",
        {"ctx": ctx, "active": "observability", "current_env": env, **context},
    )


@router.get("/observability/explorer", response_class=HTMLResponse)
async def observability_explorer(
    request: Request,
    env: str = Depends(current_env_live),
    hours: int = Query(24, ge=1, le=8760),
    status: Optional[str] = Query(None, pattern=r"^(success|error)?$"),
    outcome: Optional[str] = Query(None, pattern=r"^(attention|handoff|ok)?$"),
    search: Optional[str] = Query(None),
    tool: Optional[str] = Query(None),
    channel_slug: Optional[str] = Query(None),
    agent_kind: Optional[str] = Query(None),
    group: str = Query("session", pattern=r"^(session|none)?$"),
    background: int = Query(0, ge=0, le=1),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    """The Conversations screen (historical URL; /admin/observability renders
    the same page). Pane state lives in the URL hash (``#trace=…&span=…``)."""
    from fastapi.concurrency import run_in_threadpool

    return await run_in_threadpool(
        render_conversations, request, env=env, ctx=ctx, hours=hours,
        status=status, outcome=outcome, search=search, tool=tool,
        channel_slug=channel_slug, agent_kind=agent_kind, group=group,
        background=background,
    )


# ---------------------------------------------------------------------------
# JSON endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/observability/explorer/trace/{trace_id}/spans.json",
    response_class=JSONResponse,
)
async def observability_explorer_spans_json(
    trace_id: str,
    _ctx: dict = Depends(require_superuser),
) -> JSONResponse:
    """Return ``{trace, tree, found, unavailable, notice}`` for the Technical
    view's steps waterfall."""
    from admin.screens.observability import load_span_tree_json

    return JSONResponse(load_span_tree_json(trace_id))


@router.get(
    "/observability/explorer/span/{trace_id}/{span_id}.json",
    response_class=JSONResponse,
)
async def observability_explorer_span_detail_json(
    trace_id: str,
    span_id: str,
    _ctx: dict = Depends(require_superuser),
) -> JSONResponse:
    """Return ``{span, notice}`` for the right pane."""
    from admin.screens.observability import load_span_detail_json

    return JSONResponse(load_span_detail_json(trace_id, span_id))


@router.get(
    "/observability/explorer/trace/{trace_id}/transcript.json",
    response_class=JSONResponse,
)
async def observability_explorer_transcript_json(
    trace_id: str,
    env: str = Depends(current_env_live),
    _ctx: dict = Depends(require_superuser),
) -> JSONResponse:
    """Return ``{trace, session_id, turns, notice}`` for the Transcript tab.

    The conversation spine comes from the linked chat session's persisted
    messages (``env`` selects the right environment's DB); this trace's tool
    calls + raw model output are merged in from the spans. The JS swaps this in
    when the operator toggles to "Transcript" — no reload."""
    from fastapi.concurrency import run_in_threadpool

    from admin.screens.observability import load_transcript_json

    return JSONResponse(
        await run_in_threadpool(load_transcript_json, trace_id, env=env))


@router.get(
    "/observability/explorer/trace/{trace_id}/overview.json",
    response_class=JSONResponse,
)
async def observability_explorer_overview_json(
    trace_id: str,
    env: str = Depends(current_env_live),
    _ctx: dict = Depends(require_superuser),
) -> JSONResponse:
    """Return ``{trace, session, tools, tool_calls, errors, notice}`` for the
    Details tab — trace cost/latency/tokens, the linked chat-session metadata,
    and a per-tool call/error rollup (consolidates the old Sessions page)."""
    from fastapi.concurrency import run_in_threadpool

    from admin.screens.observability import load_trace_overview_json

    return JSONResponse(
        await run_in_threadpool(load_trace_overview_json, trace_id, env=env))


@router.get(
    "/observability/explorer/trace/{trace_id}/audio.json",
    response_class=JSONResponse,
)
async def observability_explorer_audio_json(
    trace_id: str,
    env: str = Depends(current_env_live),
    _ctx: dict = Depends(require_superuser),
) -> JSONResponse:
    """Return ``{trace, session_id, recordings, notice}`` for the Audio tab —
    every playable call recording of the trace's conversation (real calls and
    simulated eval runs alike), newest first. See
    ``voice/live/recorder.py`` for how the audio gets captured.

    Off-loop via ``to_thread``: the loaders use the sync supabase client (and
    may probe several envs) — run inline they'd stall every other request on
    the control-plane app for the duration."""
    import asyncio

    from admin.screens.observability import load_voice_audio_json

    return JSONResponse(
        await asyncio.to_thread(load_voice_audio_json, trace_id, env=env))


@router.get("/observability/explorer/recording/{recording_id}.wav")
async def observability_explorer_recording_wav(
    recording_id: str,
    env: str = Query(None),
    _ctx: dict = Depends(require_superuser),
) -> Response:
    """Stream one call recording as a playable WAV (stereo: left = caller,
    right = assistant). ``env`` selects the branch DB the recording was found
    in by ``audio.json`` — the audio bytes live wherever the call ran.

    Off-loop via ``to_thread``: fetching + base64-decoding a multi-minute
    recording through the sync supabase client takes seconds — run inline it
    would freeze the whole control-plane event loop for that long."""
    import asyncio

    from admin.screens.observability import load_recording_wav

    wav, _row = await asyncio.to_thread(load_recording_wav, recording_id, env=env or None)
    if not wav:
        return JSONResponse(
            {"error": "Recording not found or carries no audio."}, status_code=404
        )
    return Response(
        content=wav,
        media_type="audio/wav",
        headers={
            # A recording never changes once stored — let the browser cache
            # it so scrubbing/replays don't refetch megabytes.
            "Cache-Control": "private, max-age=86400",
            "Content-Disposition": f'inline; filename="voice-call-{recording_id}.wav"',
        },
    )


# ---------------------------------------------------------------------------
# Route ordering shim
# ---------------------------------------------------------------------------
#
# router.py declares /observability/{trace_id} as a catch-all path-param
# route. Our explorer endpoints are added AFTER that route in the
# decorator-registration order (router.py imports this module at the
# bottom of its module body), so Starlette would match
# /observability/explorer against /observability/{trace_id} first and
# route the request into observability_trace_detail with trace_id =
# "explorer". Reordering router.routes here moves the explorer routes to
# the front of the routing table so the more-specific paths win.

def _reorder_routes() -> None:
    """Move explorer routes ahead of any /observability/{trace_id} route.

    Idempotent — safe to call multiple times (the loop uses index-based
    comparisons against the current ordering).
    """
    target_prefix = "/admin/observability/explorer"
    catch_all = "/admin/observability/{trace_id}"

    routes = router.routes
    explorer_indexes = [i for i, r in enumerate(routes)
                        if getattr(r, "path", "").startswith(target_prefix)]
    catch_all_indexes = [i for i, r in enumerate(routes)
                         if getattr(r, "path", "") == catch_all]

    if not explorer_indexes or not catch_all_indexes:
        return
    first_catch_all = min(catch_all_indexes)
    # If every explorer route already appears before the catch-all, no-op.
    if max(explorer_indexes) < first_catch_all:
        return

    # Extract explorer routes (in original order) and re-insert them just
    # before the catch-all.
    explorer_routes = [routes[i] for i in explorer_indexes]
    # Remove from the back so indexes stay valid.
    for i in sorted(explorer_indexes, reverse=True):
        routes.pop(i)
    # Re-find the catch-all after removals.
    insert_at = next(
        (i for i, r in enumerate(routes) if getattr(r, "path", "") == catch_all),
        len(routes),
    )
    for offset, route in enumerate(explorer_routes):
        routes.insert(insert_at + offset, route)


_reorder_routes()
