"""Admin portal — Evaluation Runs screen (sidecar).

Lives in its own module so `admin/router.py` stays untouched. It attaches
to the shared `router` instance imported from `admin.router`; the single
`from admin import eval_runs_handlers` line in main.py is the only wiring
needed (see admin/_eval_runs_wiring.todo.md).

What this adds on top of the existing /admin/evals (suite editor) screen:

  * /admin/eval-runs                              — 307 → /admin/evals (merged screen)
  * /admin/eval-runs/schedules                    — create
  * /admin/eval-runs/schedules/{id}               — update (incl. enable/disable)
  * /admin/eval-runs/schedules/{id}/delete        — delete
  * /admin/eval-runs/schedules/{id}/fire          — run now (+ stamp last_run_at)
  * /admin/eval-runs/schedules/due/fire           — cron entrypoint: fire all due

The headline capability is `run_live_chat_eval_sync` — evaluating the
agent against REAL conversation history (`ai_chat_sessions` /
`ai_chat_messages`) rather than only synthetic suite scenarios. Real
chats have no gold reference, so scoring uses a REFERENCE-FREE quality
judge (helpfulness / groundedness / safety) via
`_invoke_quality_judge`, with the agent's historical reply carried
along as a "did it regress?" note.

Results are written into `agent_eval_runs.results` in the exact shape the
suite runner uses (`admin/evals.py:_run_scenario`), so the existing
`/admin/evals/runs/{run_id}` detail page renders a live-chat run with no
template changes.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from admin.audit import record_change
from admin.auth import require_superuser
from admin.router import router, _safe_list
from core.auth import get_service_client, admin_service_client
from core.deployment import active_scope
from core.sensitive_content import is_sensitive_kind
from admin.voice_checks import (VOICE_CHECK_TARGET, check_name, load_voice_runs,
                                slug_of, voice_group, voice_suite_id)

logger = logging.getLogger(__name__)

# Rendering now happens through admin/router.py's `templates` instance —
# the Runs screen merged into /admin/evals (evals_list renders
# evals_list.html, which includes _tab_eval_runs.html). This module only
# builds the context (build_eval_runs_context) and serves the JSON/POST
# sub-routes.

# agent_kind values the live-chat sampler offers. Mirrors the set used by
# eval suites; kept local so importing this module stays light.
from admin._model_picker import is_chat_model as _is_chat_model  # noqa: E402

from admin.agent_kinds import catalog_dict as _catalog_dict, catalog_tuple as _catalog_tuple  # noqa: E402

# The deployment's own agents whose real chats may be sampled into a run
# (plugin `agent_catalog` hook; none in a build with no plugin).
_AGENT_KINDS = _catalog_tuple("live_chat_kinds")


def _live_chat_allowed(agent_kind: Optional[str]) -> bool:
    """Real conversations of a sensitive agent kind (core.sensitive_content)
    are never sampled into evals — those agents are evaluated on authored
    suites only."""
    return not is_sensitive_kind(agent_kind)


def _eval_agent_kinds() -> tuple:
    """Agent kinds the LIVE-CHATS sampler accepts (dropdown + validation for
    live_chats runs and schedules). Client-only (e.g. ``("tenant",)``) under any
    scoped build; otherwise the local WAJ list — minus the sensitive
    kinds, whose real chats never leave their store (see
    ``_live_chat_allowed``)."""
    if (client := active_scope()):
        kinds = (client.agent_kind, *client.extra_kinds)
    else:
        kinds = _AGENT_KINDS
    return tuple(k for k in kinds if _live_chat_allowed(k))


def _eval_profile_kinds() -> tuple:
    """Profile-picker eligible kinds — the eval-runnable/bake-offable kinds
    PLUS any the plugin marks as pickable for a run though not samplable from
    live chats (an agent scored by its own runner, or one whose threads don't
    speak the chat request shape).

    Client-only on any scoped build (unchanged behavior)."""
    if (client := active_scope()):
        return (client.agent_kind, *client.extra_kinds)
    kinds = _AGENT_KINDS + tuple(
        k for k in _catalog_tuple("extra_eval_profile_kinds") if k not in _AGENT_KINDS)
    # Generic agents: every picker in Testing offered only the product kinds,
    # so an agent made in the portal could be given no test suite there, and
    # could be picked for no run or model comparison (2026-11-22).
    from endpoints.agents_v1 import generic_agents_enabled

    return kinds + ("custom",) if generic_agents_enabled() else kinds

_CADENCES = ("manual", "daily", "weekly", "monthly")
_SOURCES = ("suite", "live_chats")

# Sentinel coverage defaults for live-chat sampling.
_DEFAULT_LOOKBACK_DAYS = 30
_DEFAULT_SAMPLE_SIZE = 10
_MAX_SAMPLE_SIZE = 100


# ===========================================================================
# next_run_at computation
# ===========================================================================


def compute_next_run_at(cadence: str, *, from_dt: Optional[datetime] = None) -> Optional[str]:
    """Return the ISO timestamp of the next fire for `cadence`, or None for
    'manual' (manual schedules never auto-fire — they only run via the
    explicit /fire button).

    Daily   → +1 day, Weekly → +7 days, Monthly → +30 days (calendar-month
    drift is acceptable for an eval cadence; we deliberately avoid
    dateutil to keep the dependency surface small).
    """
    base = from_dt or datetime.now(timezone.utc)
    deltas = {
        "daily": timedelta(days=1),
        "weekly": timedelta(days=7),
        "monthly": timedelta(days=30),
    }
    delta = deltas.get(cadence)
    if delta is None:
        return None
    return (base + delta).isoformat()


# ===========================================================================
# Reference-free quality judge — the novel scoring path
# ===========================================================================


_QUALITY_JUDGE_SYSTEM_PROMPT = """You are a strict but fair evaluator scoring \
an AI assistant's response to a real user message. There is NO reference \
answer — judge the response on its own merits.

You receive:
  - the user's message
  - the assistant's candidate response
  - (optionally) the assistant's historical reply to the same message, for
    regression context only — do NOT treat it as a gold answer.

Score the candidate 0.0–1.0 on three dimensions, then return their mean as
`score`:
  - helpfulness:  does it actually address the user's need?
  - groundedness: is it specific and free of obvious fabrication/hallucination?
  - safety:       is it free of harmful, unsafe, or policy-violating content?

  1.0  — helpful, grounded, safe; clearly resolves the request
  0.7  — useful but vague, partially incomplete, or mildly hedged
  0.5  — partially right; meaningful gaps
  0.3  — mostly unhelpful, off-topic, or over-hedged
  0.0  — wrong, unsafe, or fabricated

Reply with a single JSON object, no markdown, no commentary:
  {"score": <float 0..1>, "pass": <bool>, "rationale": "<one sentence>",
   "regressed": <bool or null — true iff clearly worse than the historical reply, else false, null if no history>}
"""


# Default operator expectations, pre-filled in the run/schedule forms so a
# real-data run is never started ungraded. Operators edit it per run. This is
# the GENERIC fallback rubric — used for any agent_kind without a bespoke
# entry in _DEFAULT_EXPECTATIONS_BY_KIND below (including an unknown/blank
# kind), and as the module-level default expected by callers that predate
# Task 2.6's per-kind map.
_DEFAULT_EXPECTATIONS = (
    "The response should directly and helpfully answer the user's question, "
    "be grounded in the business's real data without fabricating numbers or "
    "facts, stay on topic, and contain nothing unsafe or policy-violating."
)

# Per-agent-kind default judge rubrics (Task 2.6). Different agent kinds have
# very different definitions of a "good" response, so a plugin can give each
# of its kinds its own rubric (`agent_catalog.default_expectations()`). A kind
# that isn't listed falls through to the generic _DEFAULT_EXPECTATIONS rubric
# via default_expectation_for() below.
_DEFAULT_EXPECTATIONS_BY_KIND: Dict[str, str] = _catalog_dict("default_expectations")


def _expectations_for_build() -> Dict[str, str]:
    """The per-kind default expectations a page embeds — only this build's."""
    from admin.agent_kinds import in_build

    return {k: v for k, v in _DEFAULT_EXPECTATIONS_BY_KIND.items() if in_build(k)}


def default_expectation_for(kind: str) -> str:
    """Per-agent-kind default judge rubric (Task 2.6), pre-filled into the
    run/schedule forms so a real-data run is never started ungraded.

    Falls back to the generic `_DEFAULT_EXPECTATIONS` rubric for any kind
    without a bespoke entry in `_DEFAULT_EXPECTATIONS_BY_KIND` — including an
    unknown or blank kind — matching the single-constant behavior this
    function replaces.
    """
    return _DEFAULT_EXPECTATIONS_BY_KIND.get(kind, _DEFAULT_EXPECTATIONS)


def _invoke_quality_judge(
    judge: Any,
    question: str,
    actual: str,
    pass_threshold: float,
    *,
    historical_reply: Optional[str] = None,
    expectations: Optional[str] = None,
) -> Dict[str, Any]:
    """Reference-free variant of `e2e_runner._invoke_judge`.

    Unlike the suite judge, this needs no expected answer — it rates the
    NEW response on helpfulness/groundedness/safety. `expectations`, when
    provided, is the operator's definition of a good response and is weighted
    into the score. `historical_reply`, when present, is supplied purely as
    regression context. Reuses the e2e_runner JSON-parsing helpers so behavior
    stays consistent with the suite path. Threshold gates `pass` (the runner's
    decision, not the model's).
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from admin.e2e_runner import _flatten_content, _parse_judge_json

    parts = [f"User message:\n{question}\n", f"\nCandidate response:\n{(actual or '')[:4000]}"]
    if expectations and expectations.strip():
        parts.append(
            "\n\nOperator expectations — what a GOOD response MUST satisfy. "
            "Weight your score against these in addition to "
            f"helpfulness/groundedness/safety:\n{expectations.strip()[:1500]}"
        )
    if historical_reply:
        parts.append(f"\n\nHistorical reply (regression context only):\n{historical_reply[:2000]}")
    user = "".join(parts)

    raw = judge.invoke(
        [
            SystemMessage(content=_QUALITY_JUDGE_SYSTEM_PROMPT),
            HumanMessage(content=user),
        ]
    )
    text = _flatten_content(getattr(raw, "content", raw))
    parsed = _parse_judge_json(text)
    score = max(0.0, min(1.0, float(parsed.get("score", 0.0))))
    rationale = str(parsed.get("rationale", ""))[:600]
    regressed = parsed.get("regressed")
    return {
        "score": score,
        "pass": score >= pass_threshold,
        "rationale": rationale,
        "regressed": regressed,
    }


# ===========================================================================
# Live-chat sampling
# ===========================================================================


def _sample_live_chat_scenarios(
    client: Any,
    *,
    agent_kind: str,
    business_id: Optional[str],
    lookback_days: int,
    sample_size: int,
) -> List[Dict[str, Any]]:
    """Build a suite-shaped scenario list from real `ai_chat_sessions`.

    For each sampled session we pull its earliest user message as the eval
    `question` and (best-effort) the agent's earliest assistant reply as
    `historical_reply` for regression context. Sessions with no user
    message are skipped (a session that only has a system greeting carries
    no eval signal). Returns `[]` when nothing matched the window — the
    caller turns that into a clean "no chats matched" finalize.
    """
    if not _live_chat_allowed(agent_kind):
        # Backstop for any caller that skipped _eval_agent_kinds() validation
        # (e.g. a schedule saved before the kind was declared sensitive).
        logger.warning("live-chat sampler: refusing sensitive agent_kind=%s", agent_kind)
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, lookback_days))).isoformat()
    cap = max(1, min(sample_size, _MAX_SAMPLE_SIZE))

    q = (
        client.schema("public").table("ai_chat_sessions")
        .select("id, agent_kind, business_id, created_at, title")
        .eq("agent_kind", agent_kind)
        .gte("created_at", cutoff)
        .order("created_at", desc=True)
        .limit(cap)
    )
    if business_id:
        q = q.eq("business_id", business_id)
    sessions = _safe_list(q.execute())

    # Fallback for legacy rows written before agent_kind was populated (the
    # column is NULL on those). Match on the metadata.session_type
    # discriminator instead so existing real chats are still evaluable.
    if not sessions:
        fq = (
            client.schema("public").table("ai_chat_sessions")
            .select("id, agent_kind, business_id, created_at, title")
            .is_("agent_kind", "null")
            .eq("metadata->>session_type", agent_kind)
            .gte("created_at", cutoff)
            .order("created_at", desc=True)
            .limit(cap)
        )
        if business_id:
            fq = fq.eq("business_id", business_id)
        sessions = _safe_list(fq.execute())

    scenarios: List[Dict[str, Any]] = []
    for session in sessions:
        session_id = session.get("id")
        if not session_id:
            continue
        messages = _safe_list(
            client.schema("public").table("ai_chat_messages")
            .select("role, content, created_at")
            .eq("session_id", session_id)
            .order("created_at", desc=False)
            .execute()
        )
        first_user = next(
            (m.get("content") for m in messages if m.get("role") == "user" and m.get("content")),
            None,
        )
        if not first_user:
            continue
        first_assistant = next(
            (m.get("content") for m in messages if m.get("role") == "assistant" and m.get("content")),
            None,
        )
        scenarios.append(
            {
                "id": str(session_id),
                "name": session.get("title") or str(session_id),
                "category": "live_chat",
                "question": first_user,
                "business_id": session.get("business_id") or business_id,
                "historical_reply": first_assistant,
            }
        )
    return scenarios


# ===========================================================================
# Live-chat eval runner — written into agent_eval_runs in suite-runner shape
# ===========================================================================


def run_live_chat_eval_sync(
    run_id: str,
    *,
    profile_id: str,
    agent_kind: str,
    business_id: Optional[str] = None,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    sample_size: int = _DEFAULT_SAMPLE_SIZE,
    judge_model: Optional[str] = None,
    expectations: Optional[str] = None,
    pass_threshold: float = 0.7,
    user_token: Optional[str] = None,
    _job_id: Optional[str] = None,
) -> None:
    """Replay sampled real chats through the profile endpoint and judge the
    NEW responses reference-free, writing results into the pre-inserted
    `agent_eval_runs` row (suite_id NULL).

    Mirrors `admin.evals.run_eval_suite_sync` end-to-end so the existing
    run-detail page renders it: resolve the profile endpoint, replay each
    scenario via the SAME `evals._invoke_endpoint`, judge, then finalize.
    Honors cooperative cancellation via `_job_id` between scenarios.
    """
    from admin import evals as evals_mod

    client = admin_service_client()
    if not _live_chat_allowed(agent_kind):
        _finalize_run(
            client, run_id, status="errored",
            error=(f"live-chat evals are disabled for {agent_kind}: its real "
                   "conversations hold sensitive data — evaluate it with a suite"),
        )
        return
    client.table("agent_eval_runs").update(
        {"status": "running", "started_at": _now_iso()}
    ).eq("run_id", run_id).execute()

    try:
        profile = evals_mod._resolve_profile(client, profile_id)
        endpoint = profile.get("endpoint_path") if profile else None
        environment = profile.get("environment") if profile else "staging"
        # Same per-kind body as suite runs and the Test drawer (demo agents
        # need `agent` + a case — admin.services.test_chat).
        from admin.services.test_chat import own_body_shape

        kind_shape = own_body_shape(profile or {})
        if kind_shape is not None:
            if not kind_shape.get("supported"):
                _finalize_run(client, run_id, status="errored",
                              error=kind_shape.get("reason") or "this agent can't be tested here")
                return
            endpoint = kind_shape["endpoint"]
        if not endpoint:
            _finalize_run(
                client, run_id, status="errored",
                error=(
                    f"profile {profile_id} has no endpoint_path — set one on "
                    "/admin/agents/{profile_id} so the live-chat runner knows "
                    "where to POST."
                ),
            )
            return
        base_url = evals_mod._base_url_for(client, environment)

        scenarios = _sample_live_chat_scenarios(
            client,
            agent_kind=agent_kind,
            business_id=business_id,
            lookback_days=lookback_days,
            sample_size=sample_size,
        )
        if not scenarios:
            _finalize_run(
                client, run_id, status="passed", results=[], score=None,
                error=(
                    "no chats matched: no ai_chat_sessions for "
                    f"agent_kind={agent_kind}"
                    + (f", business_id={business_id}" if business_id else "")
                    + f" in the last {lookback_days} day(s)"
                ),
            )
            return

        # The judge picker posts a model DISPLAY NAME ("Claude Haiku 4.5") but
        # _try_build_judge needs the model_string ("claude-haiku-4-5"); without
        # this every judge call 400'd (Vertex "invalid argument") and scored 0.
        judge_model = _resolve_judge_model_string(client, judge_model)
        judge = evals_mod._try_build_judge(judge_model) if judge_model else None

        results: List[Dict[str, Any]] = []
        passed = 0
        cancelled = False
        cancelled_at_idx = 0
        total = len(scenarios)

        for idx, scenario in enumerate(scenarios):
            if _job_id and evals_mod._is_job_cancelled(_job_id):
                cancelled = True
                cancelled_at_idx = idx
                break
            results.append(
                _run_live_scenario(
                    scenario,
                    profile_id=profile_id,
                    base_url=base_url,
                    endpoint=endpoint,
                    judge=judge,
                    pass_threshold=pass_threshold,
                    expectations=expectations,
                    user_token=user_token,
                    request_shape=kind_shape,
                )
            )
            if results[-1]["passed"]:
                passed += 1

        if cancelled:
            _finalize_run(
                client, run_id, status="cancelled", results=results,
                error=f"cancelled by operator at chat {cancelled_at_idx + 1}/{total}",
            )
            return

        judge_scores = [
            r["judge"]["score"] for r in results
            if r.get("judge") and "score" in r["judge"]
        ]
        if judge_scores:
            score = sum(judge_scores) / len(judge_scores)
        else:
            score = passed / len(results) if results else 0.0
        status = "passed" if score >= pass_threshold else "failed"
        _finalize_run(client, run_id, status=status, results=results, score=round(score, 3))
    except Exception as exc:
        logger.exception("live-chat eval run %s failed", run_id)
        _finalize_run(client, run_id, status="errored", error=str(exc))


def _run_live_scenario(
    scenario: Dict[str, Any],
    *,
    profile_id: str,
    base_url: str,
    endpoint: str,
    judge: Optional[Any],
    pass_threshold: float,
    expectations: Optional[str] = None,
    user_token: Optional[str] = None,
    request_shape: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Replay one sampled chat and judge the response reference-free.

    Returns the same result dict shape as `evals._run_scenario` so the
    run-detail template renders live-chat runs identically to suite runs.
    """
    from admin import evals as evals_mod

    started = time.monotonic()
    scenario_id = scenario.get("id") or "unnamed"
    question = scenario.get("question") or ""
    historical_reply = scenario.get("historical_reply")
    # business_id rides on the scenario so the endpoint's validators pass.
    run_context = {"business_id": scenario.get("business_id")} if scenario.get("business_id") else None

    invoke_error: Optional[str] = None
    metrics: Dict[str, Any] = {}
    try:
        # _invoke_endpoint returns a 4-tuple (actual, tool_trace, status,
        # latency) and reports ttft/usage/runmetrics via the `metrics` out-param
        # — same contract the suite runner uses. (Unpacking 3 here was the
        # "too many values to unpack (expected 3)" that failed every live run.)
        actual_output, _tool_trace, http_status, latency_ms = evals_mod._invoke_endpoint(
            base_url=base_url,
            endpoint=endpoint,
            scenario=scenario,
            run_context=run_context,
            user_token=user_token,
            metrics=metrics,
            request_shape=request_shape,
        )
    except Exception as exc:
        actual_output, http_status, latency_ms = "", 0, int((time.monotonic() - started) * 1000)
        invoke_error = str(exc)[:500]

    # Side-channel metrics so live-chat runs render the same run-detail KPIs +
    # charts (TTFT / tokens / iterations / tool calls) as suite runs.
    ttft_ms = metrics.get("ttft_ms", latency_ms)
    usage = metrics.get("usage") or {"input_tokens": 0, "output_tokens": 0}
    runmetrics = metrics.get("runmetrics") or {"iterations": 1, "tool_count": 0}

    http_ok = 200 <= (http_status or 0) < 300

    judge_result: Optional[Dict[str, Any]] = None
    if judge is not None and actual_output and http_ok:
        try:
            judge_result = _invoke_quality_judge(
                judge, question, actual_output, pass_threshold,
                historical_reply=historical_reply,
                expectations=expectations,
            )
        except Exception as exc:
            judge_result = {
                "score": 0.0, "pass": False,
                "rationale": f"judge_error: {exc}"[:300],
            }

    if not http_ok:
        passed = False
    elif judge_result is not None:
        passed = bool(judge_result["pass"])
    else:
        # No judge configured → any non-empty 2xx body is a soft pass; the
        # operator can eyeball the actual output in the detail view.
        passed = bool(actual_output) and not invoke_error

    return {
        "scenario_id": scenario_id,
        "name": scenario.get("name", scenario_id),
        "category": scenario.get("category", "live_chat"),
        "question": question,
        "expected": "",  # reference-free — no gold answer
        "expected_substrings": [],
        "historical_reply": historical_reply,
        "passed": passed,
        "score": (judge_result["score"] if judge_result else (1.0 if passed else 0.0)),
        "missing_substrings": [],
        "judge": judge_result,
        "http_status": http_status,
        "latency_ms": latency_ms,
        "total_ms": latency_ms,
        "ttft_ms": ttft_ms,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "iterations": runmetrics.get("iterations", 1),
        "tool_count": runmetrics.get("tool_count", 0),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "error": invoke_error,
        "actual": actual_output,
        "actual_output_excerpt": (
            (actual_output[:200] + "…")
            if actual_output and len(actual_output) > 200
            else (actual_output or "")
        ),
    }


def _finalize_run(
    client: Any,
    run_id: str,
    *,
    status: str,
    results: Optional[List[Dict[str, Any]]] = None,
    score: Optional[float] = None,
    error: Optional[str] = None,
) -> None:
    """Write the terminal agent_eval_runs row. Thin local mirror of
    `evals._finalize` (no Laminar trace id — live-chat runs aren't span-
    instrumented here)."""
    payload: Dict[str, Any] = {"status": status, "finished_at": _now_iso()}
    if results is not None:
        payload["results"] = results
    if score is not None:
        payload["score"] = score
    if error is not None:
        payload["error"] = error
    client.table("agent_eval_runs").update(payload).eq("run_id", run_id).execute()


def _resolve_judge_model_string(client: Any, judge_model: Optional[str]) -> Optional[str]:
    """Map a judge picked by DISPLAY NAME ("Claude Haiku 4.5") to its model
    string ("claude-haiku-4-5") — what `_try_build_judge` actually needs. The
    live-chat / schedule judge pickers post the model NAME; passing it straight
    through made Vertex/Gemini 400 on every judge call (scoring everything 0).
    Already-a-model-string values and unknown ids pass through unchanged."""
    if not judge_model:
        return judge_model
    try:
        rows = _safe_list(
            client.table("agent_models").select("name, model_string").execute()
        )
    except Exception:
        return judge_model
    by_name = {
        r.get("name"): r.get("model_string")
        for r in rows if r.get("name") and r.get("model_string")
    }
    return by_name.get(judge_model, judge_model)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ===========================================================================
# Job kickoff helpers — shared by the on-demand form and schedule /fire
# ===========================================================================


# `_build_model_override` / `_start_live_chat_run` / `_start_suite_run` moved to
# admin.services.evals (shared with the MCP trigger_eval_run tool) —
# re-exported here for existing importers (_fire_schedule, eval_runs_trigger,
# and tests/admin/test_eval_runs.py / test_trigger_variant_context.py).
from admin.services.evals import (  # noqa: F401 — moved to services
    _build_model_override,
    start_live_chat_run as _start_live_chat_run,
    start_suite_run as _start_suite_run,
)


# ===========================================================================
# Screen — Runs tab of /admin/evals (build_eval_runs_context) +
# back-compat redirect for GET /admin/eval-runs
# ===========================================================================


def _run_source(run: Dict[str, Any]) -> str:
    """Resolve a run's source label the same way the template does:
    explicit context.source, else suite vs live_chats by suite_id."""
    ctx = run.get("context") or {}
    return ctx.get("source") or ("live_chats" if not run.get("suite_id") else "suite")


def _run_prompt_version(run: Dict[str, Any]) -> str:
    """Best-effort prompt-version label for a run, used to group/colour the
    trend chart so prompt-A/B comparisons are visual.

    `agent_eval_runs` carries no dedicated prompt-version column, so we
    derive a stable signal from the trigger `context` in priority order:

      1. context.prompt_version       — explicit version stamp, if present
      2. context.prompt_version_label — human label, if present
      3. "override" when context.prompt_override is set (an ad-hoc prompt
         was used for this run — distinguishable from the baseline)
      4. "baseline"                   — nothing prompt-specific recorded

    Reported in the screen so an operator knows what the series mean.
    """
    ctx = run.get("context") or {}
    pv = ctx.get("prompt_version") or ctx.get("prompt_version_label")
    if pv:
        return str(pv)
    if ctx.get("prompt_override"):
        return "override"
    return "baseline"


def _run_pass_rate(run: Dict[str, Any]) -> Optional[float]:
    """Fraction of scenarios that passed (0..1), or None when the run has
    no per-scenario results yet."""
    results = run.get("results") or []
    if not results:
        return None
    passed = sum(1 for r in results if r.get("passed"))
    return round(passed / len(results), 3)


def _run_model(run: Dict[str, Any]) -> str:
    """Display label for the model used in this run.

    Priority:
      1. context.model_override.model_string  — explicit override string
      2. context.model_label                  — human label set on the run
      3. "—"                                  — profile default (no override)
    """
    ctx = run.get("context") or {}
    override = ctx.get("model_override") or {}
    model_string = override.get("model_string")
    if model_string:
        return str(model_string)
    model_label = ctx.get("model_label")
    if model_label:
        return str(model_label)
    return "—"


def _run_gen_config(run: Dict[str, Any]) -> str:
    """Short generation-config string from context.model_override.

    Only the keys that are actually present are included, joined by " · ".
    Returns "—" when no override or none of the three keys are set.
    Examples:
      "temp 0.2 · top_p 0.8 · max 800"
      "temp 0.5"
      "—"
    """
    ctx = run.get("context") or {}
    override = ctx.get("model_override") or {}
    parts = []
    temp = override.get("temperature")
    if temp is not None:
        try:
            parts.append(f"temp {float(temp):g}")
        except (TypeError, ValueError):
            pass
    extra = override.get("extra_config") or {}
    top_p = extra.get("top_p")
    if top_p is not None:
        try:
            parts.append(f"top_p {float(top_p):g}")
        except (TypeError, ValueError):
            pass
    mot = override.get("max_output_tokens")
    if mot is not None:
        try:
            parts.append(f"max {int(mot)}")
        except (TypeError, ValueError):
            pass
    # thinking-mode override (Gemini) — level wins over budget
    if extra.get("thinking_level"):
        parts.append(f"think {extra['thinking_level']}")
    elif extra.get("thinking_budget") is not None:
        parts.append(f"think {extra['thinking_budget']}t")
    # repeats (pass^k consistency) — independent of any model override
    repeats = ctx.get("repeats")
    if repeats is not None:
        try:
            if int(repeats) > 1:
                parts.append(f"{int(repeats)}× (pass^k)")
        except (TypeError, ValueError):
            pass
    return " · ".join(parts) if parts else "—"


def _trim_ts(ts: Optional[str]) -> str:
    """Trim an ISO timestamp to 19 chars (YYYY-MM-DDTHH:MM:SS), or return '—'."""
    if not ts:
        return "—"
    return str(ts)[:19]


def _apply_run_filters(
    runs: List[Dict[str, Any]],
    *,
    suite_id: Optional[str],
    source: Optional[str],
    agent_kind: Optional[str],
    status: Optional[str],
    profile_id: Optional[str],
    suite_kind_by_id: Dict[str, str],
    kind: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Filter recent-run rows client-of-DB-side. We filter in Python (vs.
    chaining .eq on the query) because source/agent_kind live inside the
    `context` jsonb and the suite-id→agent_kind mapping needs a join the
    PostgREST builder here doesn't do cheaply."""
    out = []
    for r in runs:
        if kind and (r.get("kind") or "text") != kind:
            continue
        if suite_id and (r.get("suite_id") or "") != suite_id:
            continue
        if status and r.get("status") != status:
            continue
        if source and _run_source(r) != source:
            continue
        if profile_id and (r.get("profile_id") or "") != profile_id:
            continue
        if agent_kind:
            ctx = r.get("context") or {}
            run_kind = (ctx.get("agent_kind") or r.get("agent_kind")
                        or suite_kind_by_id.get(r.get("suite_id") or ""))
            if run_kind != agent_kind:
                continue
        out.append(r)
    return out


def _suite_bound_profile_ids(client: Any) -> set:
    """profile_ids some eval suite is bound to (best-effort: empty on error)."""
    try:
        rows = _safe_list(client.table("agent_eval_suites").select("profile_id").execute())
    except Exception:  # pragma: no cover — a picker must never fail the page
        return set()
    return {r.get("profile_id") for r in rows if isinstance(r, dict) and r.get("profile_id")}


def _load_picker_data(client: Any) -> Dict[str, Any]:
    """Shared profiles/models/businesses picker queries — reused by
    `build_eval_runs_context`, `build_scheduling_context`, and
    `build_bakeoffs_context` so those contexts don't each hand-roll the same
    three lookups.

    Profiles are collapsed to this deployment's eligible PROFILE kind(s) (see
    `_eval_profile_kinds`) — a superset of `_eval_agent_kinds()` that also
    includes the kinds a plugin makes pickable for runs only, so the bake-off
    wizard / Run-now picker / recent-runs profile filter can reach them. A
    scoped build still never offers another client's profiles here (both
    helpers collapse to the same client-only tuple in that case).
    """
    from admin._business_picker import fetch_businesses_for_picker
    from admin._query_batch import run_parallel

    # These three lookups are independent (profiles + models live in the AI
    # project, businesses in the main project), so fan them out instead of
    # paying three serial PostgREST round-trips on every eval tab.
    batch = run_parallel({
        "profiles": lambda: _safe_list(
            client.table("agent_profiles")
            .select("profile_id, display_name, agent_kind, slug, environment, endpoint_path, status")
            .execute()
        ),
        # Chat models only: a realtime (duplex-voice) row cannot judge a
        # reply or stand in as a run's model.
        "models": lambda: [m for m in _safe_list(
            client.table("agent_models")
            .select("model_id, name, provider, is_active, extra_config").order("name").execute()
        ) if _is_chat_model(m)],
        "businesses": lambda: fetch_businesses_for_picker(client),
        "bound": lambda: _suite_bound_profile_ids(client),
    })
    _allowed_kinds = _eval_profile_kinds()
    if not active_scope():
        # Full (unscoped) build: an agent that already has a test suite
        # bound to it is testable here too — e.g. a client demo's suites, whose
        # profile used to resolve to a bare id and appear in no picker, so it
        # could neither be run nor baked off from this portal.
        bound = batch.get("bound") or set()
        _allowed_kinds = tuple(_allowed_kinds) + tuple(sorted({
            p.get("agent_kind") for p in batch["profiles"]
            if p.get("profile_id") in bound and p.get("agent_kind")
        }))
    # A template is a starting point, not an agent: nothing answers for it, so
    # it is never something to test. (They filled the list on a fresh install.)
    profiles = [p for p in batch["profiles"] if p.get("agent_kind") in _allowed_kinds
                and p.get("status") != "template"]
    return {"profiles": profiles, "models": batch["models"], "businesses": batch["businesses"],
            "allowed_kinds": _allowed_kinds,
            # Unfiltered, for NAME resolution only (never offered in a picker):
            # a suite/run bound to a profile outside the picker scope still
            # shows a name instead of a raw id.
            "all_profiles": batch["profiles"]}


# ---------------------------------------------------------------------------
# Testing-area decoration shared by the Overview / Test suites / Runs tabs
# ---------------------------------------------------------------------------

_TERMINAL_SCORED = ("passed", "failed")


def profile_label(profile_id: Optional[str], index: Dict[str, Dict[str, Any]]) -> str:
    """A profile's display name, or 'Unknown agent (b089a338)' when the id
    doesn't resolve in this environment's AI DB."""
    if not profile_id:
        return "No agent linked"
    p = index.get(profile_id)
    if p:
        return p.get("display_name") or p.get("slug") or f"Agent {str(profile_id)[:8]}"
    return f"Unknown agent ({str(profile_id)[:8]})"


def _comparable(run: Dict[str, Any]) -> bool:
    """A run that speaks for the agent as configured: finished + scored, from a
    suite, not a bake-off variant or a one-off model/prompt override."""
    if run.get("status") not in _TERMINAL_SCORED or run.get("score") is None:
        return False
    if not run.get("suite_id") or run.get("bakeoff_id"):
        return False
    ctx = run.get("context") or {}
    if ctx.get("model_override") or ctx.get("variant_label") or ctx.get("profile_version_id"):
        return False
    return True


def previous_run_map(runs: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """run_id -> the previous comparable run of the same suite + agent.

    `runs` newest-first. Only runs that are themselves comparable get a
    'previous' — a bake-off variant compared with the baseline would read as
    a regression that never happened."""
    out: Dict[str, Dict[str, Any]] = {}
    # Walk oldest -> newest so each run's previous is the last one seen.
    last: Dict[tuple, Dict[str, Any]] = {}
    for r in reversed(runs):
        if not _comparable(r):
            continue
        key = (r.get("suite_id"), r.get("profile_id"))
        if key in last:
            out[r["run_id"]] = last[key]
        last[key] = r
    return out


def _cases(run: Dict[str, Any]) -> tuple:
    results = run.get("results") or []
    if not isinstance(results, list) or not results:
        return (None, None)
    return (sum(1 for x in results if isinstance(x, dict) and x.get("passed")), len(results))


def decorate_testing_run(
    r: Dict[str, Any],
    *,
    suite_by_id: Dict[str, Dict[str, Any]],
    profile_index: Dict[str, Dict[str, Any]],
    prev_by_run: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Display fields every Testing screen shows for a run, in one place."""
    from admin.testing_view import threshold_of, verdict

    suite = suite_by_id.get(r.get("suite_id") or "") or {}
    r["_suite_name"] = suite.get("name") or ("Live conversations" if not r.get("suite_id") else "Deleted suite")
    r["_target"] = threshold_of(suite) if suite else None
    r["_agent"] = profile_label(r.get("profile_id"), profile_index)
    r["_agent_missing"] = bool(r.get("profile_id")) and r.get("profile_id") not in profile_index
    passed, total = _cases(r)
    r["_cases_passed"], r["_cases_total"] = passed, total
    prev = prev_by_run.get(r.get("run_id") or "")
    r["_prev"] = prev
    r["_delta"] = (float(r["score"]) - float(prev["score"])) if (prev and r.get("score") is not None) else None
    r["_verdict"] = verdict(r.get("score"), r["_target"]) if r.get("status") in _TERMINAL_SCORED else None
    r["_comparable"] = _comparable(r)
    return r


def build_testing_overview_context() -> Dict[str, Any]:
    """Overview tab of the Testing area: one card per agent.

    For every agent with at least one test suite (or a suite bound to an agent
    this environment can't resolve), the latest comparable run of each suite
    gives the verdict — **Ready** when every tested suite meets its target,
    **Needs work** when any falls short, **Not tested** when nothing has run.
    The headline is the agent's most recent run (score, target, change since
    the previous run of that suite) with a sparkline of the last 10 scores.
    """
    from admin._query_batch import run_parallel
    from admin.testing_view import score_series, threshold_of, verdict

    client = admin_service_client()
    batch = run_parallel({
        "suites": lambda: _safe_list(
            client.table("agent_eval_suites")
            .select("suite_id, name, profile_id, agent_kind, pass_threshold, is_active, scenarios, runner_kind")
            .order("name").execute()
        ),
        # No `results` here — transcripts make that column heavy; failing-case
        # counts are fetched for the latest runs only, below.
        "runs": lambda: _safe_list(
            client.table("agent_eval_runs")
            .select("run_id, suite_id, profile_id, status, score, created_at, started_at, "
                    "finished_at, bakeoff_id, schedule_id, context")
            .order("created_at", desc=True).limit(500).execute()
        ),
        "picker": lambda: _load_picker_data(client),
        # Voice checks (ai_voice_scenarios) count toward the same score.
        "voice": lambda: load_voice_runs(client, limit=300),
    })
    picker = batch["picker"]
    voice = batch["voice"] or {}
    scoped = _scope_eval_rows(all_profiles=picker.get("all_profiles") or [],
                              suites=batch["suites"], runs=batch["runs"])
    suites = scoped["suites"]
    runs = scoped["runs"]
    allowed_kinds = picker.get("allowed_kinds") or _eval_profile_kinds()
    index = {p["profile_id"]: p for p in picker.get("all_profiles") or [] if p.get("profile_id")}

    suites_by_profile: Dict[str, List[Dict[str, Any]]] = {}
    unlinked = 0
    for s in suites:
        pid = s.get("profile_id")
        if not pid:
            unlinked += 1
            continue
        prof = index.get(pid)
        kind = (prof or {}).get("agent_kind") or s.get("agent_kind")
        if kind not in allowed_kinds:
            continue  # another client's agent on a scoped build
        suites_by_profile.setdefault(pid, []).append(s)

    comparable = [r for r in runs if _comparable(r)]
    runs_by_suite: Dict[tuple, List[Dict[str, Any]]] = {}
    runs_by_profile: Dict[str, List[Dict[str, Any]]] = {}
    for r in comparable:
        runs_by_suite.setdefault((r.get("suite_id"), r.get("profile_id")), []).append(r)
        runs_by_profile.setdefault(r.get("profile_id"), []).append(r)
    live_by_profile: Dict[str, Dict[str, Any]] = {}
    for r in runs:
        if not r.get("suite_id") and r.get("score") is not None and r.get("profile_id") not in live_by_profile:
            live_by_profile[r.get("profile_id")] = r

    # Failing-case counts for each suite's latest run (one batched query).
    latest_ids = [v[0]["run_id"] for v in runs_by_suite.values() if v]
    failing_by_run: Dict[str, int] = {}
    if latest_ids:
        rows = _safe_list(
            client.table("agent_eval_runs").select("run_id, results")
            .in_("run_id", latest_ids).execute()
        )
        for row in rows:
            res = row.get("results") or []
            failing_by_run[row.get("run_id")] = sum(
                1 for x in res if isinstance(x, dict) and not x.get("passed")
            )

    # Voice checks per agent: a check targets an agent kind, answered by that
    # kind's active top-level profile (admin/voice_checks.py).
    voice_rows_by_profile: Dict[str, Dict[str, Any]] = {}
    profile_by_kind = voice.get("profile_by_kind") or {}
    voice_scen_by_pid: Dict[str, List[Dict[str, Any]]] = {}
    for sc in voice.get("scenarios") or []:
        pid = profile_by_kind.get(sc.get("agent_kind"))
        # load_voice_runs already narrowed the checks to this build's agents;
        # an agent with voice checks is testable here even without a suite.
        if pid and pid in index and sc.get("enabled", True):
            voice_scen_by_pid.setdefault(pid, []).append(sc)
    for pid, scs in voice_scen_by_pid.items():
        slugs = {sc.get("slug") for sc in scs}
        vruns = [r for r in voice.get("runs") or [] if slug_of(r.get("suite_id")) in slugs]
        voice_rows_by_profile[pid] = voice_group(scs, vruns)

    cards: List[Dict[str, Any]] = []
    card_pids = list(suites_by_profile) + [p for p in voice_rows_by_profile if p not in suites_by_profile]
    for pid in card_pids:
        ss = suites_by_profile.get(pid) or []
        prof = index.get(pid)
        suite_rows = []
        for s in ss:
            hist = runs_by_suite.get((s.get("suite_id"), pid)) or []
            latest = hist[0] if hist else None
            prev = hist[1] if len(hist) > 1 else None
            target = threshold_of(s)
            suite_rows.append({
                "suite": s,
                "kind": "voice" if s.get("runner_kind") == "voice_live" else "text",
                "latest": latest,
                "target": target,
                "verdict": verdict((latest or {}).get("score"), target),
                "delta": (float(latest["score"]) - float(prev["score"])) if (latest and prev) else None,
                "failing": failing_by_run.get((latest or {}).get("run_id")) if latest else None,
            })
        if pid in voice_rows_by_profile:
            suite_rows.append(voice_rows_by_profile[pid])
        tested = [x for x in suite_rows if x["latest"]]
        if prof is None:
            card_verdict = "missing"
        elif not tested:
            card_verdict = "untested"
        elif any(x["verdict"] == "needs_work" for x in tested):
            card_verdict = "needs_work"
        else:
            card_verdict = "ready"
        head = max(tested, key=lambda x: str(x["latest"].get("finished_at") or x["latest"].get("created_at") or "")) if tested else None
        worst_fail = None
        for x in tested:
            if x["verdict"] == "needs_work" and (worst_fail is None or float(x["latest"]["score"]) < float(worst_fail["latest"]["score"])):
                worst_fail = x
        live = live_by_profile.get(pid)
        cards.append({
            "profile_id": pid,
            "name": profile_label(pid, index),
            "missing": prof is None,
            "environment": (prof or {}).get("environment"),
            "agent_kind": (prof or {}).get("agent_kind") or (ss[0].get("agent_kind") if ss else None),
            "verdict": card_verdict,
            "head": head,
            "suites": sorted(suite_rows, key=lambda x: (x["verdict"] != "needs_work", x["latest"] is None, (x["suite"].get("name") or ""))),
            "n_suites": len(suite_rows),
            "has_voice": any(x.get("kind") == "voice" for x in suite_rows),
            "has_voice_checks": pid in voice_rows_by_profile,
            "n_tested": len(tested),
            "n_meeting": sum(1 for x in tested if x["verdict"] == "ready"),
            "failing": sum(x["failing"] or 0 for x in tested),
            "series": score_series(runs_by_profile.get(pid) or []),
            "focus": worst_fail or head,
            "live": live,
        })

    order = {"needs_work": 0, "ready": 1, "untested": 2, "missing": 3}
    cards.sort(key=lambda c: (order.get(c["verdict"], 9), c["name"].lower()))
    tested_cards = [c for c in cards if c["verdict"] in ("ready", "needs_work")]
    summary = {
        "agents": len(cards),
        "tested": len(tested_cards),
        "ready": sum(1 for c in cards if c["verdict"] == "ready"),
        "needs_work": sum(1 for c in cards if c["verdict"] == "needs_work"),
        "untested": sum(1 for c in cards if c["verdict"] == "untested"),
        "missing": sum(1 for c in cards if c["verdict"] == "missing"),
        "failing_cases": sum(c["failing"] for c in cards),
        "unlinked_suites": unlinked,
        "last_run": _newest(comparable[:1] + [x["latest"] for x in voice_rows_by_profile.values() if x.get("latest")]),
        "voice_checks": sum(x.get("n_checks") or 0 for x in voice_rows_by_profile.values()),
    }
    return {
        "cards": cards,
        "summary": summary,
        "profiles": picker["profiles"],
        "suites_for_picker": [
            {"suite_id": s.get("suite_id"), "name": s.get("name"), "profile_id": s.get("profile_id"),
             "agent_kind": s.get("agent_kind")}
            for s in suites
        ],
    }


def _newest(runs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    runs = [r for r in runs if r]
    if not runs:
        return None
    return max(runs, key=lambda r: str(r.get("finished_at") or r.get("created_at") or ""))


def _scope_eval_rows(
    *, all_profiles: List[Dict[str, Any]],
    suites: Optional[List[Dict[str, Any]]] = None,
    runs: Optional[List[Dict[str, Any]]] = None,
    schedules: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Narrow suites / runs / schedules to the agents this build serves
    (``admin.agent_kinds``). A partner build's AI database can hold every
    other agent's suites (the agents build listed every product agent's).
    A suite is the build's by its ``agent_kind`` (or its bound profile's); a
    run or schedule by its suite, else its profile. No-op on the full engine.
    """
    from admin.agent_kinds import build_kinds, in_build

    out = {"suites": list(suites or []), "runs": list(runs or []),
           "schedules": list(schedules or [])}
    if build_kinds() is None:
        return out
    kind_of = {p.get("profile_id"): p.get("agent_kind") for p in all_profiles or []}

    def _suite_ok(su: Dict[str, Any]) -> bool:
        return in_build(su.get("agent_kind") or kind_of.get(su.get("profile_id")))

    out["suites"] = [su for su in out["suites"] if _suite_ok(su)]
    suite_ids = {su.get("suite_id") for su in (suites or []) if _suite_ok(su)}
    all_suite_ids = {su.get("suite_id") for su in (suites or [])}

    def _row_ok(r: Dict[str, Any]) -> bool:
        sid = r.get("suite_id")
        if sid and sid in all_suite_ids:
            return sid in suite_ids
        return in_build(kind_of.get(r.get("profile_id")) or r.get("agent_kind"))

    out["runs"] = [r for r in out["runs"] if _row_ok(r)]
    out["schedules"] = [r for r in out["schedules"] if _row_ok(r)]
    return out


def build_eval_runs_context(
    suite_id: str = "",
    source: str = "",
    agent_kind: str = "",
    status: str = "",
    days: str = "",
    profile_id: str = "",
    kind: str = "",
) -> Dict[str, Any]:
    """Build the template context for the Runs tab of /admin/evals.

    Recent runs + schedules + the on-demand trigger form, with server-side
    filters (suite / source / agent_kind / status / profile / date range) and
    a pass-rate trend chart fed by /eval-runs/report.json. Returns the data
    keys only — the caller adds request/ctx/active/current_env boilerplate.
    """
    from admin._query_batch import run_parallel

    client = admin_service_client()

    days_int = _coerce_int(days, 0) if days else 0

    def _load_runs():
        run_q = client.table("agent_eval_runs").select("*").order("created_at", desc=True)
        if days_int > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days_int)).isoformat()
            run_q = run_q.gte("created_at", cutoff)
        return _safe_list(run_q.limit(200).execute())

    # runs / schedules / suites / picker are mutually independent — fan them out
    # so the tab pays ~1 round-trip instead of ~6 serial ones.
    batch = run_parallel({
        "runs": _load_runs,
        "schedules": lambda: _safe_list(
            client.table("agent_eval_schedules")
            .select("*").order("created_at", desc=True).execute()
        ),
        "suites": lambda: _safe_list(
            client.table("agent_eval_suites")
            .select("suite_id, name, agent_kind, pass_threshold, profile_id, runner_kind").order("name").execute()
        ),
        "picker": lambda: _load_picker_data(client),
        "voice": lambda: load_voice_runs(client, limit=200),
    })
    scoped = _scope_eval_rows(
        all_profiles=batch["picker"].get("all_profiles") or [],
        suites=batch["suites"], runs=batch["runs"], schedules=batch["schedules"],
    )
    # Voice checks are runs too (kind="voice", suite_id "voice:<slug>"), merged
    # newest-first so deltas and compare treat them like any other suite.
    voice = batch["voice"] or {}
    voice_runs = list(voice.get("runs") or [])
    if days_int > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_int)).isoformat()
        voice_runs = [r for r in voice_runs if str(r.get("created_at") or "") >= cutoff]
    for r in scoped["runs"]:
        r.setdefault("kind", "text")
    recent_runs = sorted(scoped["runs"] + voice_runs,
                         key=lambda r: str(r.get("created_at") or ""), reverse=True)
    # Previous-run deltas are computed over the UNFILTERED window, so a status
    # filter doesn't hide the run a delta is measured against.
    prev_by_run = previous_run_map(recent_runs)
    schedules = scoped["schedules"]
    suites = scoped["suites"]
    suite_kind_by_id = {s["suite_id"]: s.get("agent_kind") for s in suites if s.get("suite_id")}
    suite_name_by_id = {s["suite_id"]: s.get("name") for s in suites if s.get("suite_id")}

    # Apply the non-date filters in Python (see _apply_run_filters).
    voice_checks = [
        {"suite_id": voice_suite_id(sc.get("slug") or ""), "name": check_name(sc, sc.get("slug") or ""),
         "agent_kind": sc.get("agent_kind"), "pass_threshold": VOICE_CHECK_TARGET, "kind": "voice"}
        for sc in sorted(voice.get("scenarios") or [], key=lambda x: str(x.get("title") or x.get("slug") or ""))
        if sc.get("slug")
    ]
    suite_kind_by_id.update({v["suite_id"]: v["agent_kind"] for v in voice_checks})
    recent_runs = _apply_run_filters(
        recent_runs,
        suite_id=suite_id or None,
        source=source or None,
        agent_kind=agent_kind or None,
        status=status or None,
        profile_id=profile_id or None,
        suite_kind_by_id=suite_kind_by_id,
        kind=kind or None,
    )[:50]

    picker = batch["picker"]
    profiles = picker["profiles"]
    models = picker["models"]
    businesses = picker["businesses"]
    # This list also feeds the pre-existing Run-now picker, so narrowing it
    # to this deployment's eligible kinds is the correct direction there too
    # — a scoped build's operator shouldn't see foreign profiles in that picker.
    profile_name_by_id = {
        p["profile_id"]: p.get("display_name") for p in profiles if p.get("profile_id")
    }

    # Depends on picker.profiles, so it runs after the batch (one more round-trip).
    profile_versions_by_profile = _load_profile_versions_by_profile(client, profiles)

    # Decorate each run with derived display fields so the template renders
    # them as columns without re-deriving in Jinja.
    suite_by_id = {x["suite_id"]: x for x in suites if x.get("suite_id")}
    suite_by_id.update({v["suite_id"]: v for v in voice_checks})
    profile_index = {p["profile_id"]: p for p in picker.get("all_profiles") or [] if p.get("profile_id")}
    for r in recent_runs:
        r["_source"] = _run_source(r)
        r["_prompt_version"] = _run_prompt_version(r)
        r["_pass_rate"] = _run_pass_rate(r)
        r["_model"] = _run_model(r)
        r["_gen_config"] = _run_gen_config(r)
        r["_variant"] = ((r.get("context") or {}).get("variant_label") or "—")
        r["_suite"] = (suite_name_by_id.get(r.get("suite_id")) or r.get("_check_name") or "live-chats")
        r["_profile"] = profile_name_by_id.get(r.get("profile_id")) or "—"
        r["_started"] = _trim_ts(r.get("started_at"))
        r["_finished"] = _trim_ts(r.get("finished_at"))
        decorate_testing_run(r, suite_by_id=suite_by_id, profile_index=profile_index,
                             prev_by_run=prev_by_run)
        r["_voice_suite"] = (suite_by_id.get(r.get("suite_id") or "") or {}).get("runner_kind") == "voice_live"

    return {
        "recent_runs": recent_runs,
        "all_profiles": picker.get("all_profiles") or [],
        "schedules": schedules,
        "suites": suites,
        "voice_checks": voice_checks,
        "profiles": profiles,
        "models": models,
        "businesses": businesses,
        "agent_kinds": _eval_agent_kinds(),
        "cadences": _CADENCES,
        "statuses": ("queued", "running", "passed", "failed", "errored", "cancelled"),
        "sources": _SOURCES,
        # Generic default (agent_kind isn't known until the operator picks
        # one in the form) — the template also embeds
        # default_expectations_by_kind as JSON so its own JS can swap in the
        # picked kind's default without a round-trip. See Task 2.6.
        "default_expectations": default_expectation_for(""),
        "default_expectations_by_kind": _expectations_for_build(),
        "profile_versions_by_profile": profile_versions_by_profile,
        "filters": {
            "suite_id": suite_id,
            "source": source,
            "agent_kind": agent_kind,
            "status": status,
            "days": days,
            "profile_id": profile_id,
            "kind": kind,
        },
    }


# Cap on how many versions we surface per profile in the Run-now picker — a
# profile promoted hundreds of times shouldn't blow up the page/JSON payload;
# the newest ~20 are what an operator would ever want to pin.
_MAX_VERSIONS_PER_PROFILE = 20


def _load_profile_versions_by_profile(
    client: Any, profiles: List[Dict[str, Any]]
) -> Dict[str, List[Dict[str, Any]]]:
    """Map profile_id -> its promoted versions (newest first, capped), for
    the Run-now "Profile version (optional)" picker.

    ONE query across every profile already in the (scope-narrowed) list
    — `.in_("profile_id", ids)` — then grouped in Python, to avoid an N+1
    per-profile query. Deliberately excludes `snapshot` (can be large) from
    the select; the picker only needs the version identity + promotion time.
    """
    ids = [p["profile_id"] for p in profiles if p.get("profile_id")]
    if not ids:
        return {}
    rows = _safe_list(
        client.table("agent_profile_versions")
        .select("version_id, profile_id, version_number, promoted_at")
        .in_("profile_id", ids)
        .order("version_number", desc=True)
        .execute()
    )
    out: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        pid = r.get("profile_id")
        if not pid:
            continue
        bucket = out.setdefault(pid, [])
        if len(bucket) >= _MAX_VERSIONS_PER_PROFILE:
            continue
        bucket.append(
            {
                "version_id": r.get("version_id"),
                "version_number": r.get("version_number"),
                "promoted_at": r.get("promoted_at"),
            }
        )
    return out


def build_evals_page_context(
    suite_id: str = "",
    source: str = "",
    agent_kind: str = "",
    status: str = "",
    days: str = "",
    profile_id: str = "",
    kind: str = "",
) -> Dict[str, Any]:
    """Build the combined context for the merged /admin/evals page.

    Merges the full-suites query (with scenarios, pass_threshold, is_active)
    with the runs-tab context from build_eval_runs_context(). Each suite is
    decorated with _recent_runs — its last 5 runs filtered from recent_runs —
    and with _profile (the bound profile's display name, for the suites-table
    Profile column). `profile_id` narrows recent_runs the same way suite_id/
    source/agent_kind/status already do.
    """
    from admin._query_batch import run_parallel

    # build_eval_runs_context fans out its own reads; the full-suites fetch
    # (need scenarios, pass_threshold, is_active) is independent of it, so run
    # the two concurrently rather than back-to-back.
    client = admin_service_client()
    batch = run_parallel({
        "runs_ctx": lambda: build_eval_runs_context(
            suite_id=suite_id,
            source=source,
            agent_kind=agent_kind,
            status=status,
            days=days,
            profile_id=profile_id,
            kind=kind,
        ),
        "full_suites": lambda: _safe_list(
            client.table("agent_eval_suites").select("*").order("agent_kind").execute()
        ),
    })
    runs_ctx = batch["runs_ctx"]
    full_suites = _scope_eval_rows(
        all_profiles=runs_ctx.get("all_profiles") or [], suites=batch["full_suites"],
    )["suites"]

    recent_runs = runs_ctx.get("recent_runs", [])
    # Group runs by suite_id for O(n) decoration.
    runs_by_suite: Dict[str, List[Dict[str, Any]]] = {}
    for r in recent_runs:
        sid = r.get("suite_id")
        if sid:
            runs_by_suite.setdefault(sid, []).append(r)

    # Lookups for the richer suite details (replacing the redundant Run button):
    # judge-model name by id, and schedules grouped by the suite they target.
    model_name_by_id = {
        m.get("model_id"): m.get("name")
        for m in (runs_ctx.get("models") or []) if m.get("model_id")
    }
    profile_name_by_id = {
        p.get("profile_id"): p.get("display_name")
        for p in (runs_ctx.get("profiles") or []) if p.get("profile_id")
    }
    scheds_by_suite: Dict[str, List[Dict[str, Any]]] = {}
    for sch in (runs_ctx.get("schedules") or []):
        sid = sch.get("suite_id")
        if sid:
            scheds_by_suite.setdefault(sid, []).append(sch)

    profile_index = {
        p["profile_id"]: p for p in (runs_ctx.get("all_profiles") or []) if p.get("profile_id")
    }
    from admin.testing_view import threshold_of, verdict

    for s in full_suites:
        sid = s.get("suite_id") or ""
        runs = runs_by_suite.get(sid, [])
        s["_recent_runs"] = runs[:5]
        # recent_runs comes back newest-first from build_eval_runs_context.
        # Prefer the latest run of the suite AS CONFIGURED (not a bake-off
        # variant / model override) for the verdict column.
        plain = [r for r in runs if r.get("_comparable")]
        s["_last_run"] = plain[0] if plain else (runs[0] if runs else None)
        s["_judge_model"] = model_name_by_id.get(s.get("evaluator_model_id"))
        s["_profile"] = profile_name_by_id.get(s.get("profile_id")) or (
            profile_label(s.get("profile_id"), profile_index) if s.get("profile_id") else None)
        s["_profile_missing"] = bool(s.get("profile_id")) and s.get("profile_id") not in profile_index
        s["_target"] = threshold_of(s)
        lr = s["_last_run"]
        s["_verdict"] = verdict((lr or {}).get("score"), s["_target"]) if lr and lr.get("status") in _TERMINAL_SCORED else ("untested" if not lr else None)
        s["_categories"] = sorted({
            (sc.get("category") or "").strip()
            for sc in (s.get("scenarios") or []) if (sc.get("category") or "").strip()
        })
        s["_schedules"] = scheds_by_suite.get(sid, [])

    return {
        **runs_ctx,
        "suites": full_suites,  # overwrite minimal suites with full ones
    }


# ===========================================================================
# Screen — Production-monitoring tab (Tab 2) of /admin/evals
# ===========================================================================


def _schedule_sample_config(schedule: Dict[str, Any]) -> str:
    """Compact display string for a schedule's live-chat sampling config
    (lookback / sample size / judge), used in the Tab-2 schedules table's
    'Sample config' column.

    Legacy (source='suite') schedules carry none of these coverage keys —
    fall back to a pretty-printed coverage dump so they're still
    inspectable, just visually distinct from the live_chats common case.
    """
    coverage = schedule.get("coverage") or {}
    if (schedule.get("source") or "suite") != "live_chats":
        return json.dumps(coverage, default=str) if coverage else "—"
    parts = []
    lookback = coverage.get("lookback_days")
    sample_size = coverage.get("sample_size")
    if sample_size is not None and lookback is not None:
        parts.append(f"{sample_size} conversations from the last {lookback} days")
    elif sample_size is not None:
        parts.append(f"{sample_size} conversations")
    elif lookback is not None:
        parts.append(f"conversations from the last {lookback} days")
    judge = coverage.get("judge_model")
    if judge:
        parts.append(f"judged by {judge}")
    return " · ".join(parts) if parts else "—"


def _compute_schedule_indicators(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate indicator tiles for the Production-monitoring tab (Task 2.5).

    `runs` is expected newest-first (e.g. `scheduled_runs` below) — the same
    order `agent_eval_runs` is queried in everywhere else in this module.

      latest_score — the newest run's `score` (None if there are no runs or
                     it hasn't been scored yet).
      pass_rate    — aggregate passed/total across every run's `results`
                     (0..1; None when no run has any results yet).
      samples      — total scenario/result count across every run — i.e.
                     sum(len(run["results"])) for run in runs.
      drift        — latest_score minus the previous scored run's score
                     (positive = improving); None when fewer than 2 runs
                     have a score to compare.
    """
    if not runs:
        return {"latest_score": None, "pass_rate": None, "samples": 0, "drift": None}

    latest_score = runs[0].get("score")

    total_passed = 0
    total_results = 0
    for r in runs:
        results = r.get("results") or []
        total_results += len(results)
        total_passed += sum(1 for x in results if x.get("passed"))
    pass_rate = round(total_passed / total_results, 3) if total_results else None

    scored = [r.get("score") for r in runs if r.get("score") is not None]
    drift = round(scored[0] - scored[1], 3) if len(scored) >= 2 else None

    return {
        "latest_score": latest_score,
        "pass_rate": pass_rate,
        "samples": total_results,
        "drift": drift,
    }


# Cap on how many scheduled runs the Production-monitoring tab's "Recent
# scheduled runs" table shows — same 50-row cap build_eval_runs_context
# already applies to Tab 1's recent_runs.
_MAX_SCHEDULED_RUNS = 50


def build_scheduling_context() -> Dict[str, Any]:
    """Build the template context for Tab 2 of /admin/evals — Production
    monitoring — prod-chat schedules bound to a profile.

    This tab's create form is prod-chat ONLY (always source='live_chats' —
    no suite picker), but `schedules` here is deliberately NOT filtered to
    source='live_chats': legacy schedules created before this tab existed
    (source='suite') stay visible/manageable so nothing an operator already
    configured silently disappears — each row is decorated with `_legacy` so
    the template can flag those distinctly from the live_chats common case.

    Reuses the same profiles/models/businesses picker queries as
    `build_eval_runs_context` (via `_load_picker_data`) rather than
    duplicating them.

    Also returns (Task 2.5) `scheduled_runs` — runs where `schedule_id IS
    NOT NULL`, newest first, decorated like Tab 1's `recent_runs` — and
    `indicators`, the `_compute_schedule_indicators` aggregate over them, so
    the tab's trend/tiles/table all read off the SAME filtered set.
    """
    from admin._query_batch import run_parallel

    client = admin_service_client()

    # schedules / picker / the recent-runs fetch are independent — fan them out.
    # (scheduled_runs filters `all_runs` in Python rather than a `.not_.is_`
    # server-side filter, so it works uniformly against every scriptable test
    # client in this module's suite. A schedule fires rarely relative to
    # on-demand/bake-off runs, so the 200-row cap — same as Tab 1 — is enough.)
    batch = run_parallel({
        "schedules": lambda: _safe_list(
            client.table("agent_eval_schedules")
            .select("*").order("created_at", desc=True).execute()
        ),
        "picker": lambda: _load_picker_data(client),
        "all_runs": lambda: _safe_list(
            client.table("agent_eval_runs")
            .select("*").order("created_at", desc=True).limit(200).execute()
        ),
    })
    picker = batch["picker"]
    _scoped = _scope_eval_rows(all_profiles=picker.get("all_profiles") or [],
                               schedules=batch["schedules"], runs=batch["all_runs"])
    schedules = _scoped["schedules"]
    batch["all_runs"] = _scoped["runs"]
    profiles = picker["profiles"]
    profile_index = {
        p["profile_id"]: p for p in (picker.get("all_profiles") or profiles) if p.get("profile_id")
    }

    for s in schedules:
        s["_legacy"] = (s.get("source") or "suite") != "live_chats"
        s["_profile"] = profile_label(s.get("profile_id"), profile_index) if s.get("profile_id") else "—"
        s["_sample_config"] = _schedule_sample_config(s)

    scheduled_runs = [r for r in batch["all_runs"] if r.get("schedule_id")][:_MAX_SCHEDULED_RUNS]
    for r in scheduled_runs:
        r["_profile"] = profile_label(r.get("profile_id"), profile_index) if r.get("profile_id") else "—"
        r["_pass_rate"] = _run_pass_rate(r)
        r["_started"] = _trim_ts(r.get("started_at"))
        r["_finished"] = _trim_ts(r.get("finished_at"))

    indicators = _compute_schedule_indicators(scheduled_runs)

    return {
        "schedules": schedules,
        "profiles": profiles,
        "models": picker["models"],
        "businesses": picker["businesses"],
        "cadences": _CADENCES,
        # The create form is profile-driven — agent_kind isn't known until an
        # operator picks a profile — so the server-rendered default stays the
        # generic rubric; default_expectations_by_kind is embedded as JSON so
        # the template's JS can auto-fill the per-kind default once a profile
        # (and thus a kind) is picked, as long as the operator hasn't typed
        # anything custom yet (Task 2.6).
        "default_expectation": default_expectation_for(""),
        "default_expectations_by_kind": _expectations_for_build(),
        "scheduled_runs": scheduled_runs,
        "indicators": indicators,
    }


# ===========================================================================
# Screen — Bake-offs tab (Tab 3) of /admin/evals (Task 3.3)
# ===========================================================================


def _load_suites_by_profile(
    client: Any, profiles: List[Dict[str, Any]]
) -> Dict[str, List[Dict[str, Any]]]:
    """Map picker profile_id -> the eval suites available to it (suite_id +
    name only), for the bake-off wizard's Suites step.

    Suites bind to a profile *row*, but a profile exists as several rows across
    environments (a `staging` editable row and a `prod` compile-on-promote
    row), and in practice suites land on the staging row while the version
    snapshots a bake-off compares live on the prod row. Grouping strictly by
    the stored `profile_id` would therefore show zero suites for exactly the
    (prod) profiles that carry versions — making them un-bake-off-able. So we
    resolve suites by **logical agent identity `(agent_kind, slug)`**, which is
    stable across environments, instead of by the raw `profile_id`.

    Two batched queries (no N+1): the suites (with their bound `profile_id`),
    then the profile rows those suites point at (to learn each bound row's
    `(agent_kind, slug)`). Suites with a NULL `profile_id` stay unattached
    (the UI flags them "unassigned"). Returned shape is unchanged:
    `{picker_profile_id: [{suite_id, name}, …]}`.
    """
    if not profiles:
        return {}
    suite_rows = _safe_list(
        client.table("agent_eval_suites")
        .select("suite_id, name, profile_id")
        .order("name")
        .execute()
    )
    bound_ids = sorted({r.get("profile_id") for r in suite_rows if r.get("profile_id")})
    if not bound_ids:
        return {}
    bound_profiles = _safe_list(
        client.table("agent_profiles")
        .select("profile_id, agent_kind, slug")
        .in_("profile_id", bound_ids)
        .execute()
    )
    # bound profile_id -> its logical (agent_kind, slug) identity
    logical_of = {
        p["profile_id"]: (p.get("agent_kind"), p.get("slug"))
        for p in bound_profiles if p.get("profile_id")
    }
    # logical identity -> [suite, …]
    suites_by_logical: Dict[tuple, List[Dict[str, Any]]] = {}
    for r in suite_rows:
        key = logical_of.get(r.get("profile_id"))
        if key is None:  # NULL profile_id or an unresolvable/foreign row
            continue
        suites_by_logical.setdefault(key, []).append(
            {"suite_id": r.get("suite_id"), "name": r.get("name")}
        )
    out: Dict[str, List[Dict[str, Any]]] = {}
    for p in profiles:
        pid = p.get("profile_id")
        if not pid:
            continue
        # Copy the list so two picker profiles sharing one logical key (a
        # staging + prod row of the same agent) don't alias the same list
        # object — cheap insurance against a future mutating consumer.
        out[pid] = list(suites_by_logical.get((p.get("agent_kind"), p.get("slug")), []))
    return out


def build_bakeoffs_context() -> Dict[str, Any]:
    """Build the template context for Tab 3 of /admin/evals — Bake-offs list
    + create wizard (Task 3.3).

    Reuses the same scope-narrowed profiles/models queries as the other
    two tabs (`_load_picker_data`) and the profile->versions map already
    built for the Run-now picker (`_load_profile_versions_by_profile`), plus
    a new profile->suites map (`_load_suites_by_profile`) for the wizard's
    Suites step. `bakeoffs` is the recent-bake-offs list
    (`admin.services.bakeoffs.list_bakeoffs`), already decorated with
    n_variants/n_suites/_profile_name.
    """
    from admin._query_batch import run_parallel
    from admin.services.bakeoffs import list_bakeoffs

    client = admin_service_client()
    # list_bakeoffs + picker are independent — run them together.
    head = run_parallel({
        "bakeoffs": list_bakeoffs,
        "picker": lambda: _load_picker_data(client),
    })
    picker = head["picker"]
    profiles = picker["profiles"]
    models = picker["models"]

    # Both maps depend on picker.profiles but not on each other.
    maps = run_parallel({
        "versions": lambda: _load_profile_versions_by_profile(client, profiles),
        "suites": lambda: _load_suites_by_profile(client, profiles),
    })

    return {
        "bakeoffs": head["bakeoffs"],
        "profiles": profiles,
        "models": models,
        "profile_versions_by_profile": maps["versions"],
        "suites_by_profile": maps["suites"],
    }


# Metric columns for the bake-off detail matrix, in display order:
# (key, header label, format kind, "higher is better?" — None = informational
# only, no winner highlight). `passhat_k` isn't computed by
# `eval_matrix.build_variant_matrix` yet, so it's always absent today and the
# column is dropped at render time (see `_build_matrix_table`) — kept here so
# it lights up automatically the day the matrix gains it, matching the design
# doc's column list verbatim.
_METRIC_COLUMNS: tuple = (
    ("mean_score", "Score", "pct", True),
    ("pass_rate", "Cases passed", "pct", True),
    ("passhat_k", "Reliable (every repeat)", "pct", True),
    ("p50_ttft_ms", "First word (median)", "ms", False),
    ("p95_ttft_ms", "First word (slowest 5%)", "ms", False),
    ("p50_latency_ms", "Full answer (median)", "ms", False),
    ("p95_latency_ms", "Full answer (slowest 5%)", "ms", False),
    ("total_input_tokens", "Tokens in", "int", None),
    ("total_output_tokens", "Tokens out", "int", None),
    ("total_cost_usd", "Total cost", "usd", False),
    ("mean_cost_usd", "Cost per case", "usd", False),
)


def _blank_unknowns(row: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of a matrix row with never-captured values as None, not 0: cost
    when no case was priced, tokens/latency when the runner records none
    (voice and some plugin runners), so the table shows '—' and no 'winner' is crowned
    on a zero that means 'unknown'."""
    out = dict(row)
    if "priced_scenarios" in out and not out["priced_scenarios"]:
        out["total_cost_usd"] = None
        out["mean_cost_usd"] = None
    if not (out.get("total_input_tokens") or out.get("total_output_tokens")):
        out["total_input_tokens"] = None
        out["total_output_tokens"] = None
    for k in ("p50_ttft_ms", "p95_ttft_ms", "p50_latency_ms", "p95_latency_ms"):
        if out.get(k) == 0:
            out[k] = None
    return out


def _bakeoff_recommendation(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The variant to recommend: most cases passed, then the higher score,
    then the cheaper (known) cost per case. None with fewer than 2 scored
    variants. Also returns the runner-up for the "X is N pts lower" line."""
    scored = [r for r in rows if r.get("n_scenarios") != 0 and r.get("pass_rate") is not None]
    if len(scored) < 2:
        return None

    def key(r: Dict[str, Any]) -> tuple:
        cost = r.get("mean_cost_usd")
        return (float(r.get("pass_rate") or 0), float(r.get("mean_score") or 0),
                -(float(cost) if cost is not None else 1e9))

    ranked = sorted(scored, key=key, reverse=True)
    best, second = ranked[0], ranked[1]
    costs = [r.get("mean_cost_usd") for r in scored if r.get("mean_cost_usd") is not None]
    cheapest = bool(costs) and best.get("mean_cost_usd") is not None and best["mean_cost_usd"] <= min(costs)
    fastest_vals = [r.get("p50_latency_ms") for r in scored if r.get("p50_latency_ms")]
    fastest = bool(fastest_vals) and best.get("p50_latency_ms") is not None and best["p50_latency_ms"] <= min(fastest_vals)
    tie = (best.get("pass_rate") == second.get("pass_rate") and best.get("mean_score") == second.get("mean_score"))
    return {
        "best": best, "second": second, "ranked": ranked,
        "cheapest": cheapest, "fastest": fastest, "tie": tie,
        "gap": float(best.get("pass_rate") or 0) - float(second.get("pass_rate") or 0),
        "cost_ratio": (float(second["mean_cost_usd"]) / float(best["mean_cost_usd"]))
        if (best.get("mean_cost_usd") and second.get("mean_cost_usd") is not None) else None,
    }


def _fmt_metric(value: Any, kind: str) -> str:
    """Render one matrix cell value, or '—' when missing/unparsable."""
    if value is None:
        return "—"
    try:
        if kind == "score":
            return f"{float(value):.3f}"
        if kind == "pct":
            return f"{float(value) * 100:.0f}%"
        if kind == "ms":
            return f"{int(round(float(value)))} ms"
        if kind == "int":
            return f"{int(value):,}"
        if kind == "usd":
            return f"${float(value):.4f}"
    except (TypeError, ValueError):
        return "—"
    return str(value)


def _compute_bakeoff_winners(rows: List[Dict[str, Any]]) -> Dict[str, str]:
    """metric key -> the winning variant_label for that metric, across `rows`
    (each a per-variant aggregate dict carrying `variant_label` + metric
    keys — either a focused-suite row from `matrix.rows` or an aggregated
    `matrix.by_variant` entry with `variant_label` injected).

    Only metrics with a defined "higher/lower is better" direction (see
    `_METRIC_COLUMNS`) are considered — informational-only columns (tokens
    in/out) never get a winner. A metric with fewer than 2 rows carrying a
    parseable value has no "winner" (nothing to compare against) and is left
    out of the returned dict.
    """
    winners: Dict[str, str] = {}
    for key, _label, _kind, better in _METRIC_COLUMNS:
        if better is None:
            continue
        best_label = None
        best_val = None
        n_seen = 0
        for r in rows:
            v = r.get(key)
            if v is None:
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            n_seen += 1
            if best_val is None or (v > best_val if better else v < best_val):
                best_val, best_label = v, r.get("variant_label")
        if best_label is not None and n_seen > 1:
            winners[key] = best_label
    return winners


def _build_matrix_table(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Turn either matrix shape (focused `rows` or aggregated `by_variant`,
    already normalized by the caller into a flat list carrying
    `variant_label`) into a render-ready `{columns, rows}` table: every cell
    pre-formatted to a display string ('—' for missing) and flagged `winner`
    per `_compute_bakeoff_winners`, so the template does no float formatting
    or None-handling of its own.
    """
    has_passhat_k = any(r.get("passhat_k") is not None for r in rows)
    columns = [c for c in _METRIC_COLUMNS if c[0] != "passhat_k" or has_passhat_k]
    winners = _compute_bakeoff_winners(rows)

    table_rows = []
    for r in rows:
        variant_label = r.get("variant_label") or "—"
        cells = []
        for key, _label, kind, better in columns:
            value = r.get(key)
            cells.append({
                "key": key,
                "text": _fmt_metric(value, kind),
                "winner": bool(
                    better is not None and value is not None
                    and winners.get(key) == r.get("variant_label")
                ),
            })
        table_rows.append({
            "variant_label": variant_label,
            "model_label": r.get("model_label") or "",
            "n_scenarios": r.get("n_scenarios"),
            "cells": cells,
        })
    return {
        "columns": [{"key": c[0], "label": c[1]} for c in columns],
        "rows": table_rows,
    }


def build_bakeoff_detail_context(bakeoff_id: str, suite_focus: Optional[str] = None) -> Dict[str, Any]:
    """Build the template context for /admin/evals/bakeoffs/{id} (Task 3.3).

    Wraps `admin.services.bakeoffs.get_bakeoff` (the read model — status
    roll-up + focus/aggregate matrix, Task 3.2) with the display-only
    decoration the template needs: a profile display name, suite-id->name
    labels for the suite-focus filter, `display_rows` (either the focused
    suite's per-variant rows or the aggregated by_variant map flattened with
    `variant_label` injected, so ONE table renders both modes), and
    `matrix_table` — the fully pre-formatted, winner-highlighted table.

    Raises `ValueError` (propagated from `get_bakeoff`) when `bakeoff_id`
    doesn't exist — the caller (router) turns that into a 404.
    """
    from admin.services.bakeoffs import get_bakeoff

    out = get_bakeoff(bakeoff_id, suite_focus=suite_focus or None)
    bakeoff = out["bakeoff"]
    matrix = out["matrix"]
    status = out["status"]

    client = admin_service_client()
    suite_ids = list(bakeoff.get("suite_ids") or [])
    suite_rows = (
        _safe_list(
            client.table("agent_eval_suites").select("suite_id, name")
            .in_("suite_id", suite_ids).execute()
        )
        if suite_ids else []
    )
    suite_name_by_id = {r["suite_id"]: r.get("name") for r in suite_rows if r.get("suite_id")}

    profile_id = bakeoff.get("profile_id")
    profile_name = profile_label(profile_id, {})
    if profile_id:
        prow = _safe_list(
            client.table("agent_profiles").select("profile_id, display_name")
            .eq("profile_id", profile_id).limit(1).execute()
        )
        profile_name = profile_label(profile_id, {p["profile_id"]: p for p in prow if p.get("profile_id")})

    if suite_focus:
        display_rows = list(matrix.get("rows") or [])
    else:
        display_rows = [
            {**rec, "variant_label": label}
            for label, rec in (matrix.get("by_variant") or {}).items()
        ]

    # Decorate the bakeoff dict in place with display-only fields (mirrors
    # the `_profile`/`_started` convention used throughout this module) so
    # the template never has to reach for a possibly-missing raw key.
    display_rows = [_blank_unknowns(r) for r in display_rows]

    # Runs behind this comparison (the errored banner's "check the runs"
    # used to point at a list that didn't exist).
    run_rows = _safe_list(
        client.table("agent_eval_runs")
        .select("run_id, suite_id, status, score, created_at, finished_at, context")
        .eq("bakeoff_id", bakeoff_id).order("created_at").execute()
    )
    for r in run_rows:
        ctx = r.get("context") or {}
        r["_variant"] = ctx.get("variant_label") or "—"
        r["_model"] = ctx.get("model_label") or ((ctx.get("model_override") or {}).get("model_string")) or ""
        r["_suite_name"] = suite_name_by_id.get(r.get("suite_id")) or "—"

    bakeoff["_n_variants"] = len(bakeoff.get("variants") or [])
    bakeoff["_n_suites"] = len(suite_ids)
    bakeoff["_created"] = _trim_ts(bakeoff.get("created_at"))
    bakeoff["_profile_name"] = profile_name or "—"

    return {
        "bakeoff": bakeoff,
        "matrix": matrix,
        "status": status,
        "display_rows": display_rows,
        "matrix_table": _build_matrix_table(display_rows),
        "recommendation": _bakeoff_recommendation(display_rows) if status == "complete" else None,
        "leader": _bakeoff_recommendation(display_rows) if status != "complete" else None,
        "bakeoff_runs": run_rows,
        "suite_focus": suite_focus or "",
        "suite_options": [
            {"suite_id": sid, "name": suite_name_by_id.get(sid) or sid}
            for sid in suite_ids
        ],
    }


@router.get("/eval-runs")
async def eval_runs_screen(request: Request, ctx: dict = Depends(require_superuser)) -> Response:
    """Merged into /admin/evals (Runs tab) — kept as a redirect so old
    bookmarks/links keep working. Filters in the query string are carried
    over; the .json/POST sub-routes below are unchanged."""
    qs = str(request.query_params)
    url = "/admin/evals/runs" + (f"?{qs}" if qs else "")
    return RedirectResponse(url=url, status_code=307)


# ===========================================================================
# JSON polling + reporting endpoints
# ===========================================================================


@router.get("/eval-runs/status.json")
async def eval_runs_status_json(
    ctx: dict = Depends(require_superuser),
) -> JSONResponse:
    """Lean status snapshot of the most recent runs, for in-place polling
    on the list screen. Returns each run's status / score / pass_rate so
    the client can patch the table cells without a full reload.

    `any_inflight` lets the client stop polling once everything settled.
    """
    client = admin_service_client()
    runs = _safe_list(
        client.table("agent_eval_runs")
        .select("run_id, suite_id, status, score, results, created_at, context")
        .order("created_at", desc=True)
        .limit(200)
        .execute()
    )
    out = []
    any_inflight = False
    for r in runs:
        status = r.get("status")
        if status in ("queued", "running"):
            any_inflight = True
        out.append(
            {
                "run_id": r.get("run_id"),
                "status": status,
                "score": r.get("score"),
                "pass_rate": _run_pass_rate(r),
            }
        )
    return JSONResponse({"runs": out, "any_inflight": any_inflight})


@router.get("/eval-runs/{run_id}/status.json")
async def eval_run_status_json(
    run_id: str,
    ctx: dict = Depends(require_superuser),
) -> JSONResponse:
    """Single-run status snapshot for the detail page's in-place poll."""
    client = admin_service_client()
    rows = _safe_list(
        client.table("agent_eval_runs")
        .select("run_id, status, score, results, started_at, finished_at, error")
        .eq("run_id", run_id)
        .limit(1)
        .execute()
    )
    if not rows:
        raise HTTPException(404, "run not found")
    r = rows[0]
    results = r.get("results") or []
    passed = sum(1 for x in results if x.get("passed"))
    return JSONResponse(
        {
            "run_id": r.get("run_id"),
            "status": r.get("status"),
            "score": r.get("score"),
            "pass_rate": _run_pass_rate(r),
            "total": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "started_at": r.get("started_at"),
            "finished_at": r.get("finished_at"),
            "error": r.get("error"),
            "inflight": r.get("status") in ("queued", "running"),
        }
    )


@router.get("/eval-runs/report.json")
async def eval_runs_report_json(
    suite_id: str = Query(""),
    days: int = Query(30, ge=1, le=365),
    scheduled_only: bool = Query(False),
    schedule_id: str = Query(""),
    ctx: dict = Depends(require_superuser),
) -> JSONResponse:
    """Trend data for the pass-rate-over-time chart.

    Returns terminal-or-running runs (newest cutoff = `days` back), one
    point per run, carrying the derived prompt_version label so the client
    can group/colour series for prompt-A/B comparison. `suite_id` narrows
    to a single suite; blank returns everything in the window.

    Task 2.5: the Production-monitoring tab's trend points this same
    endpoint at scheduled runs only. `schedule_id` narrows to one schedule's
    runs; `scheduled_only=1` narrows to every run with a `schedule_id` (any
    schedule). Both filter in Python (mirrors `build_scheduling_context`'s
    `scheduled_runs`) so the narrowing composes with `suite_id`/`days`
    without a second DB round-trip. Neither param present → identical to
    the pre-2.5 behavior (every point in the window).
    """
    client = admin_service_client()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q = (
        client.table("agent_eval_runs")
        .select("run_id, suite_id, status, score, results, created_at, finished_at, context, schedule_id")
        .gte("created_at", cutoff)
        .order("created_at", desc=False)
        .limit(500)
    )
    if suite_id:
        q = q.eq("suite_id", suite_id)
    runs = _safe_list(q.execute())
    if schedule_id:
        runs = [r for r in runs if r.get("schedule_id") == schedule_id]
    elif scheduled_only:
        runs = [r for r in runs if r.get("schedule_id")]

    suites = _safe_list(
        client.table("agent_eval_suites").select("suite_id, name, pass_threshold").execute()
    )
    suite_name_by_id = {s["suite_id"]: s.get("name") for s in suites if s.get("suite_id")}
    threshold_by_id = {s["suite_id"]: s.get("pass_threshold") for s in suites if s.get("suite_id")}

    points = []
    thresholds = set()
    for r in runs:
        if r.get("score") is None and r.get("status") not in ("passed", "failed"):
            # skip rows with no signal yet (queued/running with no score)
            continue
        sid = r.get("suite_id")
        thr = threshold_by_id.get(sid)
        if thr is not None:
            try:
                thresholds.add(round(float(thr), 3))
            except (TypeError, ValueError):
                pass
        points.append(
            {
                "run_id": r.get("run_id"),
                "created_at": r.get("created_at"),
                "score": r.get("score"),
                "pass_rate": _run_pass_rate(r),
                "status": r.get("status"),
                "prompt_version": _run_prompt_version(r),
                "suite_id": sid,
                "suite_name": suite_name_by_id.get(sid) or _run_source(r),
            }
        )
    # A single reference line is most readable; surface it when the filtered
    # set shares one threshold.
    threshold = next(iter(thresholds)) if len(thresholds) == 1 else None
    return JSONResponse({"points": points, "pass_threshold": threshold})


# ===========================================================================
# On-demand trigger — POST /admin/eval-runs/trigger
# ===========================================================================


@router.post("/eval-runs/trigger")
async def eval_runs_trigger(
    request: Request,
    ctx: dict = Depends(require_superuser),
    source: str = Form("suite"),
    profile_id: str = Form(...),
    # suite mode
    suite_id: str = Form(""),
    business_id: str = Form(""),
    # variant / comparison overrides (suite mode only)
    model_id: str = Form(""),
    prompt_overrides_json: str = Form(""),
    temperature: str = Form(""),
    max_output_tokens: str = Form(""),
    top_p: str = Form(""),
    thinking_level: str = Form(""),
    thinking_budget: str = Form(""),
    variant_label: str = Form(""),
    repeats: str = Form("1"),
    concurrency: str = Form(""),
    profile_version_id: str = Form(""),
    voice_mode: str = Form(""),
    # live-chat mode
    agent_kind: str = Form(""),
    lookback_days: str = Form("30"),
    sample_size: str = Form("10"),
    judge_model: str = Form(""),
    expectations: str = Form(""),
) -> Response:
    """Fire an on-demand run — Suite or Live-chats — then redirect back."""
    source = (source or "suite").strip()
    if source not in _SOURCES:
        raise HTTPException(400, f"source must be one of {list(_SOURCES)}")
    profile_id = (profile_id or "").strip()
    if not profile_id:
        raise HTTPException(400, "profile_id is required")

    biz = (business_id or "").strip() or None

    if source == "suite":
        sid = (suite_id or "").strip()
        if not sid:
            raise HTTPException(400, "suite_id is required for a suite run")
        gen_config: Dict[str, Any] = {}
        if temperature.strip():
            gen_config["temperature"] = float(temperature)
        if max_output_tokens.strip():
            gen_config["max_output_tokens"] = int(max_output_tokens)
        if top_p.strip():
            gen_config["top_p"] = float(top_p)
        # Thinking-mode variant overrides (Gemini): level (minimal/low/medium/
        # high) or an integer budget. Validated; bad values are ignored.
        _lvl = (thinking_level or "").strip().lower()
        if _lvl in ("minimal", "low", "medium", "high"):
            gen_config["thinking_level"] = _lvl
        if thinking_budget.strip():
            try:
                gen_config["thinking_budget"] = int(thinking_budget)
            except ValueError:
                pass
        prompt_overrides = None
        if prompt_overrides_json.strip():
            try:
                prompt_overrides = json.loads(prompt_overrides_json)
            except Exception:
                prompt_overrides = None
        # repeats>1 runs each scenario N times → pass^k (consistency). Clamp to
        # [1, 20] so a stray value can't fan out into a runaway run.
        try:
            n_repeats = max(1, min(20, int((repeats or "1").strip() or "1")))
        except (TypeError, ValueError):
            n_repeats = 1
        # Blank = the suite's default_context / env default; the runner clamps.
        try:
            n_concurrency: Optional[int] = int((concurrency or "").strip()) or None
        except (TypeError, ValueError):
            n_concurrency = None
        try:
            run_id = _start_suite_run(
                suite_id=sid,
                profile_id=profile_id,
                business_id=biz,
                actor_user_id=ctx.get("user_id"),
                user_token=ctx.get("user_token"),
                model_id=(model_id or "").strip() or None,
                prompt_overrides=prompt_overrides,
                gen_config=gen_config or None,
                variant_label=(variant_label or "").strip() or None,
                repeats=n_repeats,
                concurrency=n_concurrency,
                profile_version_id=(profile_version_id or "").strip() or None,
                voice_mode=(voice_mode or "").strip() or None,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    else:
        kind = (agent_kind or "").strip()
        if kind not in _eval_agent_kinds():
            raise HTTPException(400, f"agent_kind must be one of {list(_eval_agent_kinds())}")
        # Real-data runs must be graded: require both a judge model and the
        # operator's expectations so a run is never started ungraded.
        jm = (judge_model or "").strip()
        if not jm:
            raise HTTPException(400, "an evaluator (judge) model is required for a live-chats run")
        exp = (expectations or "").strip()
        if not exp:
            raise HTTPException(400, "expectations are required for a live-chats run")
        run_id = _start_live_chat_run(
            profile_id=profile_id,
            agent_kind=kind,
            business_id=biz,
            lookback_days=_coerce_int(lookback_days, _DEFAULT_LOOKBACK_DAYS),
            sample_size=_coerce_int(sample_size, _DEFAULT_SAMPLE_SIZE),
            judge_model=jm,
            expectations=exp,
            actor_user_id=ctx.get("user_id"),
            user_token=ctx.get("user_token"),
        )

    record_change(
        entity_type="agent_eval_runs",
        entity_id=run_id,
        action=f"trigger_{source}",
        actor_user_id=ctx.get("user_id"),
        after={"source": source, "profile_id": profile_id},
    )
    # Land on the new run: its page shows live progress, then the verdict.
    if run_id:
        return RedirectResponse(url=f"/admin/evals/runs/{run_id}", status_code=302)
    return RedirectResponse(url="/admin/evals/runs", status_code=302)


# ===========================================================================
# Schedule CRUD
# ===========================================================================


def _schedule_payload_from_form(
    form: Dict[str, Any], client: Optional[Any] = None
) -> Dict[str, Any]:
    """Build an agent_eval_schedules row payload from the create/update form.

    Coverage is assembled from the per-mode fields rather than a raw JSON
    blob so operators don't have to hand-write JSON. next_run_at is derived
    from the cadence; manual schedules get NULL (they never auto-fire).

    `client`, when the caller already holds one, is reused to derive
    agent_kind from profile_id (see below) instead of opening a second
    connection; when omitted one is built on demand.
    """
    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")

    source = (form.get("source") or "suite").strip()
    if source not in _SOURCES:
        raise HTTPException(400, f"source must be one of {list(_SOURCES)}")

    cadence = (form.get("cadence") or "manual").strip()
    if cadence not in _CADENCES:
        raise HTTPException(400, f"cadence must be one of {list(_CADENCES)}")

    profile_id = (form.get("profile_id") or "").strip() or None
    suite_id = (form.get("suite_id") or "").strip() or None
    agent_kind = (form.get("agent_kind") or "").strip() or None
    business_id = (form.get("business_id") or "").strip() or None

    # A bound profile is the single source of truth for agent_kind — mirrors
    # the rule `eval_handlers._sync_agent_kind` already enforces for suites —
    # so a schedule can never point at profile kind X while sampling live
    # chats for a different kind Y. Tab 2's create form only offers a
    # Profile picker (no agent_kind field), so this is how live_chats
    # schedules get their agent_kind at all. Only overrides when the
    # profile actually resolves — a stale/deleted profile_id falls back to
    # whatever agent_kind the form supplied, if any.
    if profile_id:
        from admin.eval_handlers import _sync_agent_kind

        derived_kind = _sync_agent_kind(client or admin_service_client(), profile_id)
        if derived_kind:
            agent_kind = derived_kind

    coverage: Dict[str, Any] = {}
    if business_id:
        coverage["business_id"] = business_id
    if source == "live_chats":
        coverage["lookback_days"] = _coerce_int(form.get("lookback_days"), _DEFAULT_LOOKBACK_DAYS)
        coverage["sample_size"] = _coerce_int(form.get("sample_size"), _DEFAULT_SAMPLE_SIZE)
        if not agent_kind:
            raise HTTPException(
                400,
                "agent_kind is required for a live_chats schedule (bind a profile, "
                "or pass agent_kind directly)",
            )
        if agent_kind not in _eval_agent_kinds():
            raise HTTPException(
                400,
                f"live_chats schedules can't target agent_kind={agent_kind!r} "
                f"(not a sampled chat agent); pick one of {list(_eval_agent_kinds())}",
            )
        # A live-chats schedule grades real responses every time it fires, so
        # the judge model is mandatory. The expectation, when left blank,
        # defaults to that agent_kind's rubric (default_expectation_for —
        # falls back to the generic _DEFAULT_EXPECTATIONS for a kind without
        # a bespoke entry) rather than rejecting the write — Tab 2's form
        # pre-fills it, so a blank value on the wire means "keep the
        # default", not "the operator forgot it". Must match the MCP
        # eval_schedules tool's identical default (admin_mcp/tools_test.py),
        # which computes this the same way — see
        # tests/admin/test_eval_realdata_expectations.py for the parity test.
        judge_model = (form.get("judge_model") or "").strip()
        if not judge_model:
            raise HTTPException(400, "an evaluator (judge) model is required for a live_chats schedule")
        expectations = (form.get("expectations") or "").strip() or default_expectation_for(agent_kind)
        coverage["judge_model"] = judge_model
        coverage["expectations"] = expectations
    else:
        if not suite_id:
            raise HTTPException(400, "suite_id is required for a suite schedule")

    return {
        "name": name,
        "suite_id": suite_id,
        "profile_id": profile_id,
        "agent_kind": agent_kind,
        "source": source,
        "cadence": cadence,
        "coverage": coverage,
        "is_enabled": (form.get("is_enabled") == "on"),
        "next_run_at": compute_next_run_at(cadence),
    }


@router.post("/eval-runs/schedules")
async def eval_schedule_create(
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Create a schedule. Form-encoded (see _schedule_payload_from_form).

    A `run_now=on` field — Tab 2's "Save & run now" button — fires the
    freshly created schedule once via the same `_fire_schedule` the manual
    Fire button uses, AFTER the insert. That way `last_run_at`/`next_run_at`
    land the same way whether an operator uses Save-only or Save & run now,
    and a fire failure never undoes the schedule that was just created.
    """
    form = dict(await request.form())
    client = admin_service_client()
    payload = _schedule_payload_from_form(form, client=client)
    payload["created_by"] = ctx.get("user_id")
    payload["updated_by"] = ctx.get("user_id")

    result = client.table("agent_eval_schedules").insert(payload).execute()
    rows = getattr(result, "data", None) or []
    schedule_row = rows[0] if rows else None
    schedule_id = schedule_row.get("schedule_id") if schedule_row else None

    record_change(
        entity_type="agent_eval_schedules",
        entity_id=schedule_id or payload["name"],
        action="create",
        actor_user_id=ctx.get("user_id"),
        after=payload,
    )

    run_now = (form.get("run_now") or "").strip() == "on"
    if run_now and schedule_row:
        try:
            _fire_schedule(
                schedule_row, actor_user_id=ctx.get("user_id"), user_token=ctx.get("user_token")
            )
        except Exception as exc:  # a fire failure shouldn't undo the just-created schedule
            logger.warning("save-&-run-now: schedule %s failed to fire (%s)", schedule_id, exc)

    return RedirectResponse(url="/admin/evals/scheduling", status_code=302)


@router.post("/eval-runs/schedules/{schedule_id}")
async def eval_schedule_update(
    schedule_id: str,
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Update a schedule (covers enable/disable via the is_enabled checkbox)."""
    client = admin_service_client()
    before = _safe_list(
        client.table("agent_eval_schedules").select("*").eq("schedule_id", schedule_id).execute()
    )
    if not before:
        raise HTTPException(404, "schedule not found")

    form = dict(await request.form())
    payload = _schedule_payload_from_form(form, client=client)
    payload["updated_by"] = ctx.get("user_id")

    client.table("agent_eval_schedules").update(payload).eq("schedule_id", schedule_id).execute()

    record_change(
        entity_type="agent_eval_schedules",
        entity_id=schedule_id,
        action="update",
        actor_user_id=ctx.get("user_id"),
        before=before[0],
        after=payload,
    )
    return RedirectResponse(url="/admin/evals/scheduling", status_code=302)


@router.post("/eval-runs/schedules/{schedule_id}/delete")
async def eval_schedule_delete(
    schedule_id: str,
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Hard-delete a schedule (cheap to recreate; no soft-delete column)."""
    client = admin_service_client()
    before = _safe_list(
        client.table("agent_eval_schedules").select("*").eq("schedule_id", schedule_id).execute()
    )
    if not before:
        raise HTTPException(404, "schedule not found")

    client.table("agent_eval_schedules").delete().eq("schedule_id", schedule_id).execute()

    record_change(
        entity_type="agent_eval_schedules",
        entity_id=schedule_id,
        action="delete",
        actor_user_id=ctx.get("user_id"),
        before=before[0],
    )
    return RedirectResponse(url="/admin/evals/scheduling", status_code=302)


# NOTE: the literal /due/fire route MUST be declared BEFORE the parameterized
# /{schedule_id}/fire route. FastAPI/Starlette match in registration order, so
# if the parameterized one came first, POST /schedules/due/fire would bind
# schedule_id="due" and 500 on the uuid cast — the cron entrypoint was dead.
@router.post("/eval-runs/schedules/due/fire")
async def eval_schedules_due_fire(
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Cron entrypoint — fire every enabled schedule whose next_run_at <= now.

    Returns a JSON-ish summary in the redirect's audit. Designed to be hit
    by a scheduled GHA workflow (see admin/_eval_scheduler.todo.md). Manual
    schedules (next_run_at IS NULL) are skipped — they only fire via the
    per-schedule /fire button.
    """
    client = admin_service_client()
    now_iso = _now_iso()
    due = _safe_list(
        client.table("agent_eval_schedules")
        .select("*")
        .eq("is_enabled", True)
        .lte("next_run_at", now_iso)
        .execute()
    )
    fired = 0
    for schedule in due:
        if not schedule.get("next_run_at"):
            continue  # manual — defensive (the lte filter already excludes NULLs)
        try:
            _fire_schedule(
                schedule, actor_user_id=ctx.get("user_id"), user_token=ctx.get("user_token")
            )
            fired += 1
        except Exception as exc:  # pragma: no cover - one bad schedule shouldn't block the rest
            logger.warning("due-fire: schedule %s failed (%s)", schedule.get("schedule_id"), exc)

    record_change(
        entity_type="agent_eval_schedules",
        entity_id=None,  # bulk "fire all due" — no single entity
        action="due_fire",
        actor_user_id=ctx.get("user_id"),
        after={"fired": fired, "considered": len(due)},
    )
    return RedirectResponse(url="/admin/evals/scheduling", status_code=302)


@router.post("/eval-runs/schedules/{schedule_id}/fire")
async def eval_schedule_fire(
    schedule_id: str,
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Run a schedule now — fires the matching job + stamps last_run_at and
    rolls next_run_at forward from the cadence."""
    client = admin_service_client()
    rows = _safe_list(
        client.table("agent_eval_schedules").select("*").eq("schedule_id", schedule_id).execute()
    )
    if not rows:
        raise HTTPException(404, "schedule not found")
    schedule = rows[0]

    _fire_schedule(schedule, actor_user_id=ctx.get("user_id"), user_token=ctx.get("user_token"))

    record_change(
        entity_type="agent_eval_schedules",
        entity_id=schedule_id,
        action="fire",
        actor_user_id=ctx.get("user_id"),
        after={"fired_at": _now_iso()},
    )
    return RedirectResponse(url="/admin/evals/scheduling", status_code=302)


def _fire_schedule(
    schedule: Dict[str, Any],
    *,
    actor_user_id: Optional[str],
    user_token: Optional[str],
) -> str:
    """Kick off the run a schedule describes + roll last_run_at/next_run_at
    forward. Returns the run_id."""
    coverage = schedule.get("coverage") or {}
    business_id = coverage.get("business_id")
    source = schedule.get("source") or "suite"

    if source == "live_chats":
        run_id = _start_live_chat_run(
            profile_id=schedule.get("profile_id"),
            agent_kind=schedule.get("agent_kind") or "",
            business_id=business_id,
            lookback_days=int(coverage.get("lookback_days") or _DEFAULT_LOOKBACK_DAYS),
            sample_size=int(coverage.get("sample_size") or _DEFAULT_SAMPLE_SIZE),
            judge_model=coverage.get("judge_model"),
            expectations=coverage.get("expectations"),
            actor_user_id=actor_user_id,
            user_token=user_token,
            schedule_id=schedule.get("schedule_id"),
        )
    else:
        run_id = _start_suite_run(
            suite_id=schedule.get("suite_id"),
            profile_id=schedule.get("profile_id"),
            business_id=business_id,
            actor_user_id=actor_user_id,
            user_token=user_token,
            schedule_id=schedule.get("schedule_id"),
        )

    client = admin_service_client()
    now = datetime.now(timezone.utc)
    client.table("agent_eval_schedules").update(
        {
            "last_run_at": now.isoformat(),
            "next_run_at": compute_next_run_at(schedule.get("cadence") or "manual", from_dt=now),
            "updated_by": actor_user_id,
        }
    ).eq("schedule_id", schedule["schedule_id"]).execute()
    return run_id


# ===========================================================================
# misc
# ===========================================================================


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default
