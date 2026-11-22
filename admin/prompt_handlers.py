"""Prompt-engineering handlers for the admin portal.

Attached as a sidecar to `admin.router.router` (the same pattern as
`profile_fork_handlers`, `tool_handlers`, etc.). The router file is
already 1700+ lines — keeping prompt features here makes the diff easier
to read and lets us iterate on the optimizer without churning router.py.

Routes attached
---------------
GET  /admin/prompts/{prompt_id}/diff?a=<vn>&b=<vn>
     Server-side unified diff between two prompt versions. Renders the
     `prompt_diff.html` template with line-level adds/dels coloured.

GET  /admin/prompts/{prompt_id}/compare
POST /admin/prompts/{prompt_id}/compare
     Prompt A/B that actually drives the resolver: the operator picks a
     suite + two prompt versions + the usual context inputs, and the
     handler kicks off TWO parallel eval runs. Each run sends a
     different `X-WAJ-Prompt-Override` header so the runtime serves the
     pinned prompt version. After the inserts the user is redirected to
     `/admin/evals/compare?a=<runA>&b=<runB>` — the existing diff view
     handles the side-by-side once both runs finish.

POST /admin/prompts/{prompt_id}/optimize
GET  /admin/prompts/{prompt_id}/optimize
     LLM-as-optimizer. POST streams the rewrite over SSE; GET renders
     the form. After the stream completes the operator can copy the
     output into the "Save as new version" textarea — saving happens
     via the existing `/admin/prompts/{prompt_id}/versions` handler.

Why a sidecar
-------------
- Keeps the optimizer streaming code, meta-prompt construction, and the
  diff template loader isolated and testable.
- Lets us add background-worker pieces later (e.g. an "auto-iterate
  until eval passes" loop) without bloating router.py further.
- Mirrors the pattern operators already understand from
  `profile_fork_handlers` — diff, fork, and now optimize all live in
  small handler files glued in at the bottom of router.py.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse

from admin.auth import require_superuser
from admin.audit import record_change
from admin._jobs import start_tracked_job
from admin._model_picker import is_chat_model
from admin.eval_context import eval_context_keys, run_context_from_form
from admin.evals import run_eval_suite_sync
from admin.router import _safe_list, router, templates
from core.auth import get_service_client, admin_service_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Diff between two prompt versions
# ---------------------------------------------------------------------------


def _load_version(client, prompt_id: str, version_number: int) -> Optional[Dict[str, Any]]:
    """Fetch one (prompt_id, version_number) row or None.

    Pulled out so tests can monkey-patch the loader without standing up a
    full Supabase mock.
    """
    rows = _safe_list(
        client.table("agent_prompt_versions").select("*")
        .eq("prompt_id", prompt_id)
        .eq("version_number", version_number)
        .limit(1).execute()
    )
    return rows[0] if rows else None


def build_unified_diff(
    content_a: str,
    content_b: str,
    *,
    label_a: str = "version_a",
    label_b: str = "version_b",
    context_lines: int = 3,
) -> List[Dict[str, Any]]:
    """Server-side unified diff broken into rows the template can colour.

    Each row is one of:
      {"kind": "hunk",  "text": "@@ -1,4 +1,4 @@"}
      {"kind": "add",   "text": "added line"}
      {"kind": "del",   "text": "deleted line"}
      {"kind": "ctx",   "text": "unchanged line"}
      {"kind": "meta",  "text": "+++ b/version_b"}

    We strip the trailing \n that difflib emits per-line so the template
    can render each row in its own <div> without trailing whitespace
    bleeding into the CSS layout.
    """
    a_lines = (content_a or "").splitlines()
    b_lines = (content_b or "").splitlines()
    raw = list(difflib.unified_diff(
        a_lines, b_lines,
        fromfile=label_a, tofile=label_b,
        n=context_lines,
        lineterm="",
    ))
    rows: List[Dict[str, Any]] = []
    for line in raw:
        if line.startswith("@@"):
            rows.append({"kind": "hunk", "text": line})
        elif line.startswith("+++") or line.startswith("---"):
            rows.append({"kind": "meta", "text": line})
        elif line.startswith("+"):
            rows.append({"kind": "add", "text": line[1:]})
        elif line.startswith("-"):
            rows.append({"kind": "del", "text": line[1:]})
        else:
            # difflib's context lines start with a single space.
            rows.append({"kind": "ctx", "text": line[1:] if line.startswith(" ") else line})
    return rows


@router.get("/prompts/{prompt_id}/diff", response_class=HTMLResponse)
async def prompt_diff_view(
    prompt_id: str,
    request: Request,
    a: int = Query(..., ge=1, description="Older version number"),
    b: int = Query(..., ge=1, description="Newer version number"),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    """Render a unified diff between two prompt versions.

    Either version missing → 404 (the picker in agents.html only offers
    valid version numbers, so this is a guard against URL hand-typing).
    """
    client = admin_service_client()
    prompt_rows = _safe_list(
        client.table("agent_prompts").select("*").eq("prompt_id", prompt_id).execute()
    )
    if not prompt_rows:
        raise HTTPException(404, "prompt not found")
    prompt = prompt_rows[0]

    version_a = _load_version(client, prompt_id, a)
    version_b = _load_version(client, prompt_id, b)
    if version_a is None or version_b is None:
        raise HTTPException(404, f"prompt versions v{a} or v{b} not found")

    diff_rows = build_unified_diff(
        version_a.get("content") or "",
        version_b.get("content") or "",
        label_a=f"v{a}",
        label_b=f"v{b}",
    )
    length_delta = len(version_b.get("content") or "") - len(version_a.get("content") or "")

    return templates.TemplateResponse(
        request, "prompt_diff.html",
        {
            "ctx": ctx,
            "active": "agents",
            "current_env": request.query_params.get("env", "staging"),
            "prompt": prompt,
            "version_a": version_a,
            "version_b": version_b,
            "diff_rows": diff_rows,
            "length_delta": length_delta,
        },
    )


# ---------------------------------------------------------------------------
# Prompt A/B compare — header-driven runtime override
#
# One click on "Launch A/B" inserts two `agent_eval_runs` rows targeting
# the same suite + profile, fires both in background threads with
# different `prompt_override` dicts, and redirects to the existing
# run-vs-run compare view. Each background run sends
# `X-WAJ-Prompt-Override: {"<agent_kind>/system": <vN>}` on every
# scenario call; the agent process's `prompt_override_middleware`
# (main.py) pins the chosen version for the duration of that request via
# the `_PROMPT_OVERRIDE` ContextVar that `PromptResolver` consults.
# ---------------------------------------------------------------------------


def _prompt_compare_context(
    client,
    prompt_id: str,
) -> Optional[Dict[str, Any]]:
    """Collect every list the compare form needs to render.

    Returns None when the prompt itself is missing — the handler turns
    that into a 404. The shape is kept tight on purpose: the form only
    needs (prompt, suites, versions, profiles, businesses) and the
    operator gets one click from form to launched A/B.
    """
    prompt_rows = _safe_list(
        client.table("agent_prompts").select("*").eq("prompt_id", prompt_id).execute()
    )
    if not prompt_rows:
        return None
    prompt = prompt_rows[0]

    suites = _safe_list(
        client.table("agent_eval_suites").select("*")
        .eq("agent_kind", prompt["agent_kind"]).order("name").execute()
    )
    versions = _safe_list(
        client.table("agent_prompt_versions").select("*")
        .eq("prompt_id", prompt_id)
        .order("version_number", desc=True).execute()
    )
    # The agent endpoint to hit is read off the profile (endpoint_path),
    # so we need a picker scoped to this agent_kind. Operators almost
    # always have one canonical "_default" profile per env, but exposing
    # the list keeps draft / forked profiles A/B-testable too.
    profiles = _safe_list(
        client.table("agent_profiles").select("*")
        .eq("agent_kind", prompt["agent_kind"])
        .order("slug").execute()
    )
    from admin._business_picker import fetch_businesses_for_picker
    businesses = fetch_businesses_for_picker(client)
    # Find the "active" version so the default for `version_b` lands on
    # the one currently served — the typical A/B is "candidate vs.
    # what's live", and pre-selecting the live row removes one click.
    active_version = next(
        (v for v in versions if v.get("is_active")), versions[0] if versions else None,
    )
    return {
        "prompt": prompt,
        "suites": suites,
        "versions": versions,
        "profiles": profiles,
        "businesses": businesses,
        "active_version_number": active_version.get("version_number") if active_version else None,
    }


@router.get("/prompts/{prompt_id}/compare", response_class=HTMLResponse)
async def prompt_compare_form(
    prompt_id: str,
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    """Render the A/B launch form.

    Pickers: suite, version_a, version_b (default = active), profile,
    business, optional branch/customer/etc.
    """
    client = admin_service_client()
    bundle = _prompt_compare_context(client, prompt_id)
    if bundle is None:
        raise HTTPException(404, "prompt not found")

    return templates.TemplateResponse(
        request, "prompt_compare.html",
        {
            "ctx": ctx,
            "active": "agents",
            "current_env": request.query_params.get("env", "staging"),
            **bundle,
        },
    )


def _build_run_context_from_form(form_values: Dict[str, str]) -> Dict[str, Any]:
    """Mirror the per-run context extraction `router.py:evals_run` does.

    Pulled out so both the existing single-run trigger and this A/B
    handler stay in lockstep on which form fields land in `run.context`.
    Empty strings are dropped so the eval runner only sees keys the
    operator actually filled in.
    """
    run_context: Dict[str, Any] = {}
    for form_key, db_key in (
        ("ctx_business_id", "business_id"),
        ("ctx_branch_id", "branch_id"),
        ("ctx_customer_id", "customer_id"),
        *((f"ctx_{key}", key) for key in eval_context_keys()),
    ):
        raw = (form_values.get(form_key) or "").strip()
        if raw:
            run_context[db_key] = raw
    ctx_json = (form_values.get("ctx_json") or "").strip()
    if ctx_json:
        try:
            extra = json.loads(ctx_json)
            if not isinstance(extra, dict):
                raise ValueError("ctx_json must be a JSON object")
            run_context.update(extra)
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(
                400, f"ctx_json could not be parsed as a JSON object: {exc}",
            )
    return run_context


@router.post("/prompts/{prompt_id}/compare")
async def prompt_compare_submit(
    prompt_id: str,
    request: Request,
    suite_id: str = Form(...),
    profile_id: str = Form(...),
    version_a: int = Form(...),
    version_b: int = Form(...),
    ctx_business_id: str = Form(""),
    ctx_branch_id: str = Form(""),
    ctx_customer_id: str = Form(""),
    ctx_json: str = Form(""),
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Launch two parallel eval runs pinned to two prompt versions.

    Versions are passed as integers — the form's `<select>` lists every
    version_number on the prompt. We deliberately allow `version_a ==
    version_b`: the operator may want to validate the A/B plumbing
    itself, or sanity-check run-to-run variance on the same version, and
    refusing would force a workaround. The compare view's "held_pass /
    regressed" categorisation makes the degenerate case obvious anyway.
    """
    client = admin_service_client()
    prompt_rows = _safe_list(
        client.table("agent_prompts").select("*").eq("prompt_id", prompt_id).execute()
    )
    if not prompt_rows:
        raise HTTPException(404, "prompt not found")
    prompt = prompt_rows[0]
    agent_kind = prompt["agent_kind"]
    slug = prompt["slug"]
    override_key = f"{agent_kind}/{slug}"

    form_values = {
        "ctx_business_id": ctx_business_id,
        "ctx_branch_id": ctx_branch_id,
        "ctx_customer_id": ctx_customer_id,
        **{f"ctx_{key}": value for key, value in run_context_from_form(await request.form()).items()},
        "ctx_json": ctx_json,
    }
    run_context = _build_run_context_from_form(form_values)

    def _insert(version_number: int) -> str:
        """Insert one `agent_eval_runs` row pinned to `version_number`.

        The override dict is stashed under the well-known
        `_prompt_override` key in `context` so the run detail page can
        surface what was pinned without re-reading the prompt versions
        list. The eval runner ignores it on the request payload (only
        the keys in `_CONTEXT_FIELDS` are forwarded to the agent body),
        but we ALSO pass it as a separate `prompt_override` kwarg to
        `run_eval_suite_sync` so the header gets attached on every call.
        """
        merged_context = dict(run_context)
        merged_context["_prompt_override"] = {override_key: version_number}
        row = client.table("agent_eval_runs").insert(
            {
                "suite_id": suite_id,
                "profile_id": profile_id,
                "status": "queued",
                "triggered_by": ctx["user_id"],
                "context": merged_context,
            }
        ).execute()
        return row.data[0]["run_id"]

    run_a_id = _insert(version_a)
    run_b_id = _insert(version_b)

    record_change(
        entity_type="agent_eval_runs",
        entity_id=run_a_id,
        action="prompt_ab_launch",
        actor_user_id=ctx["user_id"],
        after={"pair": [run_a_id, run_b_id], "prompt_id": prompt_id,
               "versions": [version_a, version_b]},
    )

    start_tracked_job(
        run_eval_suite_sync,
        kind="eval_run",
        label=f"eval:{run_a_id[:8]}",
        actor_user_id=ctx["user_id"],
        related_entity_type="agent_eval_runs",
        related_entity_id=run_a_id,
        metadata={"source": "prompt_ab", "suite_id": suite_id, "profile_id": profile_id},
        run_id=run_a_id,
        user_token=ctx["user_token"],
        prompt_override={override_key: version_a},
    )
    start_tracked_job(
        run_eval_suite_sync,
        kind="eval_run",
        label=f"eval:{run_b_id[:8]}",
        actor_user_id=ctx["user_id"],
        related_entity_type="agent_eval_runs",
        related_entity_id=run_b_id,
        metadata={"source": "prompt_ab", "suite_id": suite_id, "profile_id": profile_id},
        run_id=run_b_id,
        user_token=ctx["user_token"],
        prompt_override={override_key: version_b},
    )

    return RedirectResponse(
        url=f"/admin/evals/compare?a={run_a_id}&b={run_b_id}",
        status_code=302,
    )


# ---------------------------------------------------------------------------
# LLM-as-optimizer
# ---------------------------------------------------------------------------


def _failing_scenarios_from_run(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pluck `results` entries where `passed` is False.

    Falls back to an empty list when the run hasn't finished or stored
    nothing — the caller treats this as "no failing scenarios to
    optimize against" and returns a 400 to the operator.
    """
    results = run.get("results") or []
    out: List[Dict[str, Any]] = []
    for r in results:
        if isinstance(r, dict) and r.get("passed") is False:
            out.append(r)
    return out


def build_optimizer_meta_prompt(
    *,
    current_prompt_body: str,
    failing_scenarios: List[Dict[str, Any]],
    max_scenarios: int = 12,
) -> str:
    """Construct the meta-prompt sent to the optimizer LLM.

    Style is DSPy-inspired (problem → context → constraint → output
    shape). We cap the scenario dump at `max_scenarios` so the meta-
    prompt fits in modest context windows; the worst-failing scenarios
    are favoured (lowest score first when judge scores exist).
    """
    def _score_key(r: Dict[str, Any]) -> float:
        judge = r.get("judge") or {}
        if "score" in judge:
            return float(judge["score"])
        return float(r.get("score") or 0.0)

    ranked = sorted(failing_scenarios, key=_score_key)
    keep = ranked[:max_scenarios]

    scenario_blocks: List[str] = []
    for idx, s in enumerate(keep, start=1):
        question = s.get("question") or s.get("input") or ""
        expected = s.get("expected") or s.get("expected_answer") or ""
        actual = s.get("actual") or s.get("actual_output_excerpt") or ""
        judge = s.get("judge") or {}
        rationale = judge.get("rationale") or ""
        scenario_blocks.append(
            f"--- Scenario {idx} ({s.get('scenario_id') or s.get('name', '?')}) ---\n"
            f"Question:\n{question}\n\n"
            f"Expected answer:\n{expected}\n\n"
            f"Actual answer:\n{actual}\n\n"
            f"Judge rationale:\n{rationale}\n"
        )
    scenario_dump = "\n".join(scenario_blocks) if scenario_blocks else "(no failing scenarios captured)"

    return (
        "You are a prompt engineer. Below is a system prompt and "
        f"{len(keep)} test scenarios where it failed.\n"
        "Rewrite the prompt to fix the failures without breaking what's "
        "currently working. Output ONLY the new prompt body — no preamble, "
        "no explanation, no markdown code fences.\n\n"
        "Current prompt:\n---\n"
        f"{current_prompt_body}\n"
        "---\n\n"
        "Failing scenarios:\n"
        f"{scenario_dump}\n"
    )


def _load_model_row(client, model_id: str) -> Optional[Dict[str, Any]]:
    rows = _safe_list(
        client.table("agent_models").select("*").eq("model_id", model_id).limit(1).execute()
    )
    return rows[0] if rows else None


@router.get("/prompts/{prompt_id}/optimize", response_class=HTMLResponse)
async def prompt_optimize_form(
    prompt_id: str,
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    """Render the optimizer launch screen.

    Pickers: which eval run to learn from, which model to drive the
    optimizer. Both lists scope to the prompt's agent_kind so the
    operator can't accidentally cross wires.
    """
    client = admin_service_client()
    prompt_rows = _safe_list(
        client.table("agent_prompts").select("*").eq("prompt_id", prompt_id).execute()
    )
    if not prompt_rows:
        raise HTTPException(404, "prompt not found")
    prompt = prompt_rows[0]

    suites = _safe_list(
        client.table("agent_eval_suites").select("suite_id")
        .eq("agent_kind", prompt["agent_kind"]).execute()
    )
    suite_ids = [s["suite_id"] for s in suites]
    runs: List[Dict[str, Any]] = []
    if suite_ids:
        runs = _safe_list(
            client.table("agent_eval_runs").select("*")
            .in_("suite_id", suite_ids)
            .order("created_at", desc=True).limit(30).execute()
        )
    # Chat models only -- a realtime (duplex-voice) row would otherwise show
    # up as a pickable "Optimizer model", then prompt_optimize_submit hands
    # it to runtime.llm_factory.build_chat_model, which breaks on a
    # native-audio model id. Same `is_chat_model` predicate the eval-suite
    # picker (admin/_model_picker.py) and the profile editor's inline picker
    # (admin/screens/agents.py) already filter through.
    models = [
        m for m in _safe_list(
            client.table("agent_models").select("*").eq("is_active", True).order("name").execute()
        )
        if is_chat_model(m)
    ]
    latest_version_rows = _safe_list(
        client.table("agent_prompt_versions").select("*")
        .eq("prompt_id", prompt_id)
        .order("version_number", desc=True).limit(1).execute()
    )
    return templates.TemplateResponse(
        request, "prompt_optimize.html",
        {
            "ctx": ctx,
            "active": "agents",
            "current_env": request.query_params.get("env", "staging"),
            "prompt": prompt,
            "runs": runs,
            "models": models,
            "latest_version": latest_version_rows[0] if latest_version_rows else None,
        },
    )


async def _stream_optimizer(
    model_row: Dict[str, Any],
    meta_prompt: str,
) -> AsyncIterator[str]:
    """Drive the optimizer LLM and emit SSE chunks.

    Wraps the LangChain chat model in `astream`. Each token / chunk is
    yielded as `data: {"type": "delta", "content": "<chunk>"}\\n\\n`. On
    completion we send `data: {"type": "done", "content": "<full>"}` so
    the client knows when to enable the "Save as new version" button.
    """
    from runtime.llm_factory import build_chat_model

    full: List[str] = []
    try:
        chat = build_chat_model(model_row)
        if chat is None:
            yield _sse({"type": "error", "content": "model row has no model_string"})
            return
    except Exception as exc:  # pragma: no cover — env-dependent (missing API keys)
        yield _sse({"type": "error", "content": f"model build failed: {exc}"})
        return

    try:
        # LangChain BaseChatModel.astream accepts a list of messages or a
        # bare string; we use the message-list form so HumanMessage shows
        # up correctly in Laminar traces.
        from langchain_core.messages import HumanMessage

        async for chunk in chat.astream([HumanMessage(content=meta_prompt)]):
            piece = getattr(chunk, "content", None) or ""
            if not piece:
                continue
            full.append(piece)
            yield _sse({"type": "delta", "content": piece})
            # Cooperative yield so the client sees chunks promptly.
            await asyncio.sleep(0)
    except Exception as exc:
        yield _sse({"type": "error", "content": f"stream error: {exc}"})
        return

    yield _sse({"type": "done", "content": "".join(full)})


def _sse(payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@router.post("/prompts/{prompt_id}/optimize")
async def prompt_optimize_submit(
    prompt_id: str,
    based_on_run_id: str = Form(...),
    model_id: str = Form(...),
    ctx: dict = Depends(require_superuser),
) -> StreamingResponse:
    """Stream the optimizer LLM's rewrite over SSE.

    Validates the run / model / prompt, builds the meta-prompt server-
    side, then defers to `_stream_optimizer` for the actual LLM call.
    The client renders the stream into a textarea + diff and uses the
    existing `/admin/prompts/{prompt_id}/versions` POST to save.
    """
    client = admin_service_client()
    prompt_rows = _safe_list(
        client.table("agent_prompts").select("*").eq("prompt_id", prompt_id).execute()
    )
    if not prompt_rows:
        raise HTTPException(404, "prompt not found")

    run_rows = _safe_list(
        client.table("agent_eval_runs").select("*").eq("run_id", based_on_run_id).execute()
    )
    if not run_rows:
        raise HTTPException(404, "eval run not found")
    run = run_rows[0]

    failing = _failing_scenarios_from_run(run)
    if not failing:
        raise HTTPException(
            400,
            "selected run has no failing scenarios — pick a run that actually "
            "regressed something so the optimizer has signal to work with",
        )

    latest_version_rows = _safe_list(
        client.table("agent_prompt_versions").select("*")
        .eq("prompt_id", prompt_id)
        .order("version_number", desc=True).limit(1).execute()
    )
    if not latest_version_rows:
        raise HTTPException(400, "prompt has no versions yet — write a v1 before optimizing")
    current_body = latest_version_rows[0].get("content") or ""

    model_row = _load_model_row(client, model_id)
    if not model_row:
        raise HTTPException(404, "model not found")

    meta_prompt = build_optimizer_meta_prompt(
        current_prompt_body=current_body,
        failing_scenarios=failing,
    )

    return StreamingResponse(
        _stream_optimizer(model_row, meta_prompt),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable proxy buffering for SSE
        },
    )


# ---------------------------------------------------------------------------
# input_validators — operator-curated request-body docs / examples
#
# The real schema is read-only (extracted from main.py Pydantic models)
# but the operator stash lives in `agent_profiles.input_validators`. We
# add a small sidecar route so the agents.html form can save the JSON
# without us extending the existing /admin/profiles/{id} handler in
# router.py.
# ---------------------------------------------------------------------------


@router.post("/profiles/{profile_id}/input-validators")
async def profile_input_validators_save(
    profile_id: str,
    input_validators: str = Form(""),
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Persist the operator's example payloads / notes JSON blob.

    Accepts the raw JSON the operator pasted into the textarea, parses
    it as a dict, and writes it back into the `input_validators` jsonb
    column. An empty string clears the column to `{}` so the operator
    can wipe a stale set of notes without raw SQL.
    """
    client = admin_service_client()
    rows = _safe_list(
        client.table("agent_profiles").select("input_validators, agent_kind, environment")
        .eq("profile_id", profile_id).limit(1).execute()
    )
    if not rows:
        raise HTTPException(404, "profile not found")

    raw = (input_validators or "").strip()
    if not raw:
        parsed: Dict[str, Any] = {}
    else:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"input_validators must be valid JSON: {exc}")
        if not isinstance(parsed, dict):
            raise HTTPException(400, "input_validators must be a JSON object")

    client.table("agent_profiles").update({"input_validators": parsed}).eq(
        "profile_id", profile_id
    ).execute()

    # Best-effort audit trail.
    try:
        from admin.audit import record_change

        record_change(
            entity_type="agent_profiles",
            entity_id=profile_id,
            action="update",
            actor_user_id=ctx.get("user_id"),
            after={"input_validators": parsed},
        )
    except Exception:
        logger.warning("input_validators: audit log skipped", exc_info=True)

    return RedirectResponse(url=f"/admin/agents?profile_id={profile_id}", status_code=302)
