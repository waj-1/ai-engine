"""Eval runner for the admin portal — Laminar-instrumented.

Suites are stored in `agent_eval_suites.scenarios` (jsonb) and results land
in `agent_eval_runs` (so the admin tables/UI keep working). Each suite run
opens a Laminar root span, and every scenario is a child span tagged with
the scenario metadata and pass/fail event. The rich drill-down lives in
the Laminar dashboard — the admin portal renders a flat summary plus a
deep-link via `lmnr_trace_id`.

Each scenario is a dict like:
    {
        "id": "refund_after_30_days",
        "question": "Can I still return an order I received 40 days ago?",
        "expected_substrings": ["30 days", "store credit"],
        "expected_answer": "...optional reference for judge mode...",
        "expected_tools": ["get_order"],
        "forbidden_tools": ["calculator"]
    }

Scoring: each scenario hits the profile's `endpoint_path` over SSE,
accumulates `answer`-typed event content into `actual_output`, and then:

  1. **Substring mode** (always runs): every `expected_substrings`
     fragment must appear case-insensitively in the actual output.
  2. **Judge mode** (when the suite has `evaluator_model` set and the
     scenario has `expected_answer`): an LLM judge from
     `admin.e2e_runner._invoke_judge` rates the output 0–1; the
     suite's `pass_threshold` (default 0.7) gates the verdict.
  3. **Tool assertion mode** (optional `expected_tools`/`forbidden_tools`,
     HARD gate): `_extract_executed_tools()` parses the actual tool names
     the agent invoked out of the SSE transcript — either from structured
     `tool`/`tool_call`/`tool_result`/`action` events (most agents emit
     these with the raw tool id) or, when an agent only narrates tool use
     via `{"type": "thought", "content": "Using X..."}` (a manual tool
     loop — the reason `tool_count` used to read 0 for one), by
     regex-matching that narration
     and canonicalizing the display name the same way the agent built it
     (`core.base_agent.BaseStreamingAgent.format_tool_name`). A scenario
     with `expected_tools` set FAILS — regardless of judge/substring
     score — if ANY listed tool wasn't called; one with `forbidden_tools`
     set FAILS if ANY listed tool WAS called. This is what makes a suite
     verify *mechanism* (a sub-agent was actually delegated to via
     `ask_*`, a skill was actually read via `read_skill`) rather than
     just "the final answer read well" — a judge grades prose, not which
     code path produced it.

  4. **Tool-result errors**: every tool RESULT in the transcript
     (`observation`/`tool_result` events, or a plugin runner's own list)
     is checked for an error shape (`{"error": ...}` a few dicts deep,
     `{"ok": false}`, top-level `status: error`, `"Error: ..."` text) and
     recorded per scenario as `tool_errors` / `tool_error_count`. An
     `expected_tools` entry whose every result errored fails the tool
     assertion (`errored_expected_tools`); other tool errors are recorded
     but only fail the scenario with `fail_on_tool_error: true` (scenario
     or suite/run context).

Attempts run on a bounded thread pool (`concurrency`, see
`_resolve_concurrency` / `_dispatch_attempts`); results keep scenario-then-
attempt order regardless of finish order.

The modes compose: the scenario only passes when every configured
signal agrees (substring AND judge AND tool-assertion). Score is the judge
score when present, otherwise 1.0/0.0 from the substring check — the tool
assertion does not change the reported `score`, only `passed` (mirroring
how a substring mismatch already overrides `passed` without changing
`score`).
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx

from admin.services.eval_runners import runner_for
from core.auth import admin_service_client, get_service_client

logger = logging.getLogger(__name__)

# Ceiling on per-scenario attempts (pass^k). The /admin/evals run form offers
# up to 10; clamp here so a hand-crafted context can't launch a runaway suite.
_MAX_REPEATS = 10


def _resolve_repeats(context: Optional[Dict[str, Any]], fallback: int = 1) -> int:
    """Pick the per-scenario attempt count from a run's trigger context.

    The queued run path (`/admin/evals/run` → `start_tracked_job`) never
    threads `repeats` as a kwarg — the form packs it into `run.context`
    (the same place `model_override` lives). Without reading it here every
    scenario runs exactly once, so pass^k can never measure consistency.

    Coerces strings (the form posts `"5"`), clamps to ``[1, _MAX_REPEATS]``,
    and falls back to ``fallback`` for missing / zero / unparseable values.
    """
    if not isinstance(context, dict):
        return fallback
    raw = context.get("repeats")
    if raw is None:
        return fallback
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return fallback
    if val <= 0:
        return fallback
    return min(val, _MAX_REPEATS)


# Scenario attempts run on a bounded thread pool. One agent's reply took ~80s,
# so a 15-scenario x 3-repeat suite ran for over an hour one attempt at a
# time. 4 keeps a run well inside the engine's per-IP rate limit (100/min)
# and the judge provider's quota; 8 is the ceiling a hand-crafted context
# can ask for.
_DEFAULT_CONCURRENCY = 4
_MAX_CONCURRENCY = 8
# Kinds that must stay one-at-a-time. voice_live drives a duplex WS call per
# scenario: anonymous voice is capped per IP, a live call is billable and
# rides the tenant's daily audio budget, and the caller LLM is the judge.
_SEQUENTIAL_RUNNER_KINDS = frozenset({"voice_live"})


def _resolve_concurrency(
    context: Optional[Dict[str, Any]], *, runner_kind: str = "chat"
) -> int:
    """How many scenario attempts may run at once.

    `context["concurrency"]` wins (the merged suite default_context + run
    trigger context, so a suite can pin `concurrency: 1` for scenarios that
    must not overlap, e.g. ones writing bookings for one test customer). Else
    the `EVAL_CONCURRENCY` env var, else `_DEFAULT_CONCURRENCY`. Strings are
    coerced (the form posts `"4"`), the value is clamped to
    ``[1, _MAX_CONCURRENCY]``, and missing / zero / unparseable values fall
    back to the default. Runner kinds in `_SEQUENTIAL_RUNNER_KINDS` always
    get 1.
    """
    if runner_kind in _SEQUENTIAL_RUNNER_KINDS:
        return 1

    def _parse(raw: Any) -> Optional[int]:
        if raw is None or raw == "":
            return None
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return None
        return val if val > 0 else None

    default = _parse(os.getenv("EVAL_CONCURRENCY")) or _DEFAULT_CONCURRENCY
    raw = context.get("concurrency") if isinstance(context, dict) else None
    val = _parse(raw) or default
    return max(1, min(val, _MAX_CONCURRENCY))


def _dispatch_attempts(
    tasks: List[Tuple[int, int]],
    run_task: Any,
    *,
    concurrency: int,
    should_cancel: Any,
) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    """Run every `(scenario_idx, attempt)` task, at most `concurrency` at once.

    Returns `(results, cancelled_at)`: results in TASK order (scenario order,
    then attempt) whatever order they finished in, and the index of the first
    task that was never started because `should_cancel()` said stop (None
    when everything ran).

    The cancel poll runs on the calling thread, right before each task is
    started, so cancelling stops new work and lets in-flight attempts finish
    (the same contract as the old sequential loop, which also could not
    interrupt a scenario mid-request).

    Each task runs inside its OWN `contextvars.copy_context()`: worker threads
    do not inherit the caller's ContextVars, and two things here live in
    them: the Laminar/OTel current span (without it every scenario span would
    be a root span in a separate trace instead of a child of the suite span),
    and `core.auth`'s admin env (without it `admin_service_client()` in a
    worker would read the local branch, not the env the operator picked). A
    fresh copy per task, because one Context cannot be entered by two
    threads at once.
    """
    slots: List[Optional[Dict[str, Any]]] = [None] * len(tasks)
    cancelled_at: Optional[int] = None
    if concurrency <= 1:
        for i, task in enumerate(tasks):
            if should_cancel():
                cancelled_at = i
                break
            slots[i] = run_task(*task)
    else:
        from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

        with ThreadPoolExecutor(max_workers=concurrency,
                                thread_name_prefix="eval-scenario") as pool:
            inflight: Dict[Any, int] = {}

            def _collect(done: Any) -> None:
                for fut in done:
                    slots[inflight.pop(fut)] = fut.result()

            for i, task in enumerate(tasks):
                while len(inflight) >= concurrency:
                    done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
                    _collect(done)
                if should_cancel():
                    cancelled_at = i
                    break
                ctx = contextvars.copy_context()
                inflight[pool.submit(ctx.run, run_task, *task)] = i
            if inflight:
                done, _ = wait(list(inflight))
                _collect(done)
    return [r for r in slots if r is not None], cancelled_at


@contextlib.contextmanager
def _lmnr_span(name: str, span_type: str = "DEFAULT", attributes: Optional[Dict[str, Any]] = None):
    """Start a Laminar span if the SDK is initialized, otherwise no-op.

    A context manager so callers don't have to branch on instrumentation
    state. `attributes` are set on the span at open time; the span closes
    automatically on exit.
    """
    try:
        from lmnr import Laminar
    except Exception:
        yield None
        return

    try:
        cm = Laminar.start_as_current_span(name=name, span_type=span_type)
    except Exception:
        yield None
        return

    with cm as span:
        if attributes:
            try:
                # Filter out None values and stringify anything non-scalar so
                # the OTel exporter doesn't reject the payload.
                flat: Dict[str, Any] = {}
                for key, value in attributes.items():
                    if value is None:
                        continue
                    if isinstance(value, (str, int, float, bool)):
                        flat[key] = value
                    else:
                        flat[key] = str(value)
                Laminar.set_span_attributes(flat)
            except Exception:
                pass
        yield span


def _current_lmnr_trace_id() -> Optional[str]:
    try:
        from lmnr import Laminar

        trace_id = Laminar.get_trace_id()
        return str(trace_id) if trace_id is not None else None
    except Exception:
        return None


# Correlation between a scenario and the agent's own trace. The run id is set
# once per run and inherited by each attempt's copied context; the ref is
# minted per attempt in `_run_scenario`. `_override_headers` forwards both,
# the engine stamps them on its request root span (`waj.eval.run_id` /
# `waj.eval.ref`, core/channel_resolver.eval_trace_attrs), and the run detail
# page resolves ref → trace id to link each case to the model's reasoning.
_EVAL_RUN_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("eval_run_id", default=None)
_EVAL_REF: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("eval_ref", default=None)


def _eval_correlation_headers() -> Dict[str, str]:
    from core.channel_resolver import EVAL_REF_HEADER, EVAL_RUN_HEADER

    headers: Dict[str, str] = {}
    run_id, ref = _EVAL_RUN_ID.get(), _EVAL_REF.get()
    if run_id:
        headers[EVAL_RUN_HEADER] = run_id
    if ref:
        headers[EVAL_REF_HEADER] = ref
    return headers


def _mark_span_error(message: str) -> None:
    """Add an `error` event to the current span — what the Conversations
    outcome and the span list's status read — so a timed-out or failed
    scenario is visibly an error in its trace, not just in the results."""
    try:
        from opentelemetry import trace as _otel_trace

        span = _otel_trace.get_current_span()
        if span is not None and span.is_recording():
            span.add_event("error", {"message": message[:500]})
    except Exception:
        pass


def run_eval_suite_sync(
    run_id: str,
    user_token: Optional[str] = None,
    prompt_override: Optional[Dict[str, int]] = None,
    _job_id: Optional[str] = None,
    repeats: int = 1,
    profile_version_id: Optional[str] = None,
) -> None:
    """Resolve the run, execute every scenario, write results.

    `user_token` is the Supabase JWT captured from the operator who
    triggered the run. We forward it as the agent's `Authorization`
    header so endpoints that decode the JWT (auth_user dependency)
    see a real session — the placeholder `"Bearer eval-runner"` we
    used to send returns 401 "Invalid token: Not enough segments".

    `prompt_override` is an optional `{"<agent_kind>/<prompt_key>":
    version_number}` dict the runner forwards as the
    `X-WAJ-Prompt-Override` header on every endpoint call. The agent
    process's PromptResolver picks up the header (via the
    `prompt_override_middleware` in main.py) and pins those versions
    for the lifetime of each scenario request. Used by the admin
    portal's prompt A/B button to launch two parallel runs that share a
    suite but differ only on which prompt version is active.

    `_job_id` is the admin_jobs row id threaded by `start_tracked_job`
    when the worker opts into cooperative cancellation. We poll
    `admin._jobs.is_cancelled(_job_id)` between scenarios and bail out
    cleanly when the operator hits Cancel on /admin/jobs. None for
    legacy callers — no cancel poll, original behavior preserved.

    `profile_version_id` pins every scenario request to one promoted
    profile snapshot via `X-WAJ-Profile-Version` (a version-pinned run,
    e.g. a bake-off variant). The param wins when passed directly; when
    None we fall back to `run.context.profile_version_id` — the queued
    trigger path (`start_suite_run`) packs it into the run row's context
    the same way it does `model_override` / `repeats`.
    """
    client = admin_service_client()
    started_at = _now_iso()
    client.table("agent_eval_runs").update(
        {"status": "running", "started_at": started_at}
    ).eq("run_id", run_id).execute()

    try:
        run_rows = (
            client.table("agent_eval_runs")
            .select("*, agent_eval_suites(*)")
            .eq("run_id", run_id)
            .limit(1)
            .execute()
            .data
        )
        if not run_rows:
            raise RuntimeError(f"run {run_id} not found")
        run = run_rows[0]
        # The queued trigger packs the attempt count into run.context, not as a
        # kwarg — honor it so repeats>1 actually runs each scenario N times
        # (this is what makes the pass^k / consistency metric real).
        repeats = _resolve_repeats(run.get("context"), fallback=repeats)
        # Param wins over context; context is the fallback for the queued
        # trigger path (start_suite_run), which packs it into run.context
        # rather than threading it as a start_tracked_job kwarg for every
        # caller (mirrors how `repeats`/`model_override` are resolved above).
        if profile_version_id is None:
            _run_context_raw = run.get("context")
            if isinstance(_run_context_raw, dict):
                profile_version_id = _run_context_raw.get("profile_version_id")
        suite = run.get("agent_eval_suites") or {}
        scenarios: List[Dict[str, Any]] = suite.get("scenarios") or []
        if not scenarios:
            _finalize(client, run_id, status="errored", error="suite has no scenarios")
            return

        suite_name = suite.get("name") or "unnamed_suite"
        agent_kind = suite.get("agent_kind")
        # 'chat' (default) keeps the SSE-endpoint + substring/judge path; a
        # plugin runner kind (admin/services/eval_runners.py) brings its own
        # request and, optionally, its own scorer — see _run_scenario.
        runner_kind = str(suite.get("runner_kind") or "chat")
        pass_threshold = float(suite.get("pass_threshold") or 0.7)
        evaluator_model = _resolve_evaluator_model(client, suite)

        # Resolve the profile's endpoint + the base URL for the current env
        # ONCE so every scenario shares the same target.
        profile = _resolve_profile(client, run["profile_id"])
        endpoint = profile.get("endpoint_path") if profile else None
        environment = profile.get("environment") if profile else "staging"
        # Kinds whose endpoint needs its OWN body (the AI-agents demo: `agent`
        # + `case_id` + the state token) go through the same builder as the
        # portal's Test drawer and the MCP `chat` tool (admin.services.
        # test_chat) — the generic body 422'd every case of a demo suite.
        from admin.services.test_chat import own_body_shape, voice_target

        kind_shape = own_body_shape(profile) if runner_kind == "chat" else None
        if kind_shape is not None:
            if not kind_shape.get("supported"):
                _finalize(client, run_id, status="errored",
                          error=kind_shape.get("reason") or "this agent can't be tested here")
                return
            endpoint = kind_shape["endpoint"]
        if not endpoint and runner_kind != "voice_live":
            _finalize(
                client, run_id, status="errored",
                error=(
                    f"profile {run['profile_id']} has no endpoint_path — "
                    "set one on /admin/agents/{profile_id} so the runner "
                    "knows where to POST."
                ),
            )
            return
        base_url = _base_url_for(client, environment)

        # Fetch the profile's main model row ONCE so baseline runs can be
        # priced (spec §5.4).  Wrap defensively — a missing model is not fatal.
        profile_model: Optional[Dict[str, Any]] = None
        try:
            profile_rows = (
                client.table("agent_profiles")
                .select("model_main_id")
                .eq("profile_id", run["profile_id"])
                .limit(1)
                .execute()
                .data or []
            )
            model_main_id = (profile_rows[0].get("model_main_id") if profile_rows else None)
            if model_main_id:
                model_rows = (
                    client.table("agent_models")
                    .select("*")
                    .eq("model_id", model_main_id)
                    .limit(1)
                    .execute()
                    .data or []
                )
                profile_model = model_rows[0] if model_rows else None
        except Exception as _pm_exc:
            logger.warning("eval: profile model lookup failed (%s)", _pm_exc)

        # Context merge order (later wins):
        #   1. suite.default_context — set once via the suite editor, the
        #      common case (every run of a suite targets the same tenant)
        #   2. run.context — per-run overrides from the trigger form
        #   3. scenario.business_id / branch_id / etc. — explicit per
        #      scenario (handled inside _payload_for)
        # This is the fix for "eval suites failing due to lacking inputs"
        # — operators set business_id once on the suite, never have to
        # remember it again.
        # Copy, don't alias: `suite.get("default_context")` returns the SAME
        # dict object every call, so popping "extra" out of it below would
        # otherwise mutate the suite's own default_context in place and
        # leave the voice_ctx build further down (which re-reads
        # `suite.get("default_context")` fresh) unable to see extra.voice.
        suite_default = dict(suite.get("default_context") or {})
        run_form = run.get("context") or {}
        run_context: Dict[str, Any] = {}
        # Flatten suite_default.extra into the top level so the runner's
        # _CONTEXT_FIELDS lookup finds keys like screen_context.
        suite_extra = suite_default.pop("extra", None) if isinstance(suite_default, dict) else None
        if isinstance(suite_default, dict):
            run_context.update(suite_default)
        if isinstance(suite_extra, dict):
            run_context.update(suite_extra)
        if isinstance(run_form, dict):
            run_context.update(run_form)

        # Build the judge once if the suite has one configured. Falls back
        # to None when the eval LLM can't be built — substring scoring
        # still runs and produces a useful result. A runner kind with its own
        # scorer doesn't use an LLM judge at all — skip the (wasted)
        # construction entirely.
        from admin.services.eval_scorers import runner_kind_needs_judge

        judge = (
            _try_build_judge(evaluator_model, model_id=suite.get("evaluator_model_id"))
            if evaluator_model and runner_kind_needs_judge(runner_kind)
            else None
        )

        voice_ctx: Optional[Dict[str, Any]] = None
        caller_llm = None
        if runner_kind == "voice_live":
            if judge is None:
                _finalize(client, run_id, status="errored",
                          error="voice_live suites require an evaluator model "
                                "(set one in the suite editor)")
                return
            # Re-read fresh (not the mutated `suite_default` above) — the
            # suite-side voice config can live at top-level
            # default_context.voice (the normal editor save path) OR
            # nested under default_context.extra.voice (an operator who
            # pasted `{"voice": {...}}` into the Advanced JSON box before
            # the eval_suite_save fix landed); top-level wins when both
            # are present.
            fresh_default_context = suite.get("default_context") or {}
            top_voice = (
                fresh_default_context.get("voice")
                if isinstance(fresh_default_context, dict) else None
            )
            extra_voice = None
            if isinstance(fresh_default_context, dict):
                fresh_extra = fresh_default_context.get("extra")
                if isinstance(fresh_extra, dict):
                    extra_voice = fresh_extra.get("voice")
            suite_voice_base = (
                top_voice if isinstance(top_voice, dict)
                else (extra_voice if isinstance(extra_voice, dict) else {})
            )
            merged_voice = dict(suite_voice_base or {})
            run_voice = (run.get("context") or {}).get("voice") or {}
            if isinstance(run_voice, dict):
                merged_voice.update(run_voice)
            merged_voice.setdefault("mode", "sim")
            merged_voice.setdefault("agent_kind", agent_kind)
            # A demo agent's duplex WS is mounted under the build's portal
            # prefix (/agents/voice/live on the agents build) and plays a case.
            for _k, _v in (voice_target(profile or {}) or {}).items():
                merged_voice.setdefault(_k, _v)
            merged_voice.setdefault("ws_path", "/voice/live")
            if run_context.get("business_id"):
                merged_voice.setdefault("business_id", run_context["business_id"])
            if merged_voice.get("auth") is not None and user_token:
                # Auth-gated kinds (business) get the operator's JWT in the
                # driver's first `auth` frame, same as a chat run's header.
                merged_voice["user_token"] = user_token
            voice_ctx = merged_voice
            # The caller speaks with the same model that judges (spec §1);
            # judge was built from the suite's evaluator model above.
            caller_llm = judge

        concurrency = _resolve_concurrency(run_context, runner_kind=runner_kind)
        # One judge per worker thread when attempts overlap: the judge is a
        # LangChain chat model whose provider SDK client is not documented as
        # thread-safe, and building one is a cheap object construction. The
        # shared judge stays the fallback (and is used as-is when sequential).
        _judge_local = threading.local()

        def _judge_for_thread() -> Optional[Any]:
            if judge is None or concurrency <= 1:
                return judge
            own = getattr(_judge_local, "judge", None)
            if own is None:
                own = _try_build_judge(
                    evaluator_model, model_id=suite.get("evaluator_model_id")
                ) or judge
                _judge_local.judge = own
            return own

        def _run_attempt(idx: int, attempt: int) -> Dict[str, Any]:
            scenario = scenarios[idx]
            if runner_kind == "voice_live":
                from admin.voice_eval_runner import run_voice_scenario_sync

                scenario_result = run_voice_scenario_sync(
                    scenario,
                    voice_ctx=voice_ctx,
                    judge=judge,
                    pass_threshold=pass_threshold,
                    caller_llm=caller_llm,
                )
            else:
                scenario_result = _run_scenario(
                    scenario,
                    profile_id=run["profile_id"],
                    base_url=base_url,
                    endpoint=endpoint,
                    judge=_judge_for_thread(),
                    pass_threshold=pass_threshold,
                    run_context=run_context,
                    user_token=user_token,
                    prompt_override=prompt_override,
                    model_override=(run_context or {}).get("model_override"),
                    profile_version_id=profile_version_id,
                    runner_kind=runner_kind,
                    profile_model=profile_model,
                    request_shape=kind_shape,
                    environment=environment,
                )
            if repeats > 1:
                scenario_result["attempt"] = attempt
            return scenario_result

        with _lmnr_span(
            name=f"eval_suite::{suite_name}",
            span_type="DEFAULT",
            attributes={
                "lmnr.span.type": "EVALUATION",
                "waj.eval.run_id": run_id,
                "waj.eval.suite_id": suite.get("suite_id"),
                "waj.eval.suite_name": suite_name,
                "waj.eval.agent_kind": agent_kind,
                "waj.eval.profile_id": run.get("profile_id"),
                "waj.eval.scenario_count": len(scenarios),
                "waj.eval.judge_model": evaluator_model,
                "waj.eval.endpoint": endpoint,
                "waj.eval.base_url": base_url,
                "waj.eval.concurrency": concurrency,
            },
        ):
            lmnr_trace_id = _current_lmnr_trace_id()
            run_token = _EVAL_RUN_ID.set(run_id)
            total_scenarios = len(scenarios)
            # Run each scenario `repeats` times. With repeats>1 the
            # comparison can measure pass^k (consistency) — see
            # `eval_handlers._compare_runs_n`. Each attempt keeps the same
            # `scenario_id` and carries an `attempt` index so the grouping
            # can count successes c out of n attempts per scenario. A
            # multi-turn scenario is ONE task, so its turns stay sequential
            # inside `_run_turns` whatever the concurrency.
            tasks = [(idx, attempt)
                     for idx in range(total_scenarios)
                     for attempt in range(max(1, repeats))]
            # Cooperative cancellation checkpoint — only when the tracker
            # threaded a `_job_id` through to us. Polled before every attempt
            # starts: keeps the contract honest (the operator sees
            # 'cancelled' in the UI the moment they click) at the cost of
            # one Supabase round-trip per attempt, negligible against
            # scenario LLM latency.
            try:
                results, cancelled_at = _dispatch_attempts(
                    tasks, _run_attempt, concurrency=concurrency,
                    should_cancel=lambda: bool(_job_id) and _is_job_cancelled(_job_id),
                )
            finally:
                _EVAL_RUN_ID.reset(run_token)
                _after_run = getattr(runner_for(runner_kind), "after_run", None)
                if _after_run is not None:
                    _after_run(run_context, since=started_at, run_id=run_id)
            passed = sum(1 for r in results if r.get("passed"))
            cancelled = cancelled_at is not None
            cancelled_at_idx = tasks[cancelled_at][0] if cancelled else 0

            if cancelled:
                # Stamp 'cancelled' on the eval row + record where we
                # stopped so the detail page can show "stopped at N/M".
                # We deliberately skip the Laminar success event here:
                # an aborted run isn't a meaningful pass/fail signal.
                _finalize(
                    client,
                    run_id,
                    status="cancelled",
                    results=results,
                    error=(
                        f"cancelled by operator at scenario "
                        f"{cancelled_at_idx + 1}/{total_scenarios}"
                    ),
                    lmnr_trace_id=lmnr_trace_id,
                )
                return

            # Aggregate score: prefer judge scores when present (true
            # 0–1 signal), fall back to substring pass rate.
            judge_scores = [
                r["judge"]["score"] for r in results
                if r.get("judge") and "score" in r["judge"]
            ]
            if judge_scores:
                score = sum(judge_scores) / len(judge_scores)
            else:
                # Divide by attempts actually run (len(results)), NOT
                # len(scenarios): with repeats>1, `passed` counts every passing
                # attempt, so dividing by scenario count inflated the score by a
                # factor of `repeats` (e.g. 3.0 with repeats=3 all-passing) and
                # made status trivially "passed". len(results) == scenarios *
                # repeats (minus cancelled), giving a true [0,1] pass rate.
                score = passed / len(results) if results else 0.0
            # Tool-assertion hard gate at the run level, mirroring the
            # spirit of e2e_runner.run_e2e's `fail_count == 0` requirement:
            # a judge grades prose, so a scenario whose expected_tools/
            # forbidden_tools assertion HARD-FAILED (persisted per-scenario
            # as `tool_assertion_passed is False` in `_run_scenario`) can
            # still pull a high judge score on its answer text — but the
            # run must not read "passed" if the agent didn't actually
            # exercise the asserted mechanism. `tool_assertion_passed` is
            # None when the scenario has no expected_tools/forbidden_tools
            # (back-compat; doesn't count as a failure), so we only count
            # explicit False.
            tool_assertion_failures = sum(
                1 for r in results if r.get("tool_assertion_passed") is False
            )
            status = (
                "passed"
                if score >= pass_threshold and tool_assertion_failures == 0
                else "failed"
            )
            _finalize(
                client,
                run_id,
                status=status,
                results=results,
                score=round(score, 3),
                lmnr_trace_id=lmnr_trace_id,
            )
    except Exception as exc:
        logger.exception("eval run %s failed", run_id)
        _finalize(client, run_id, status="errored", error=str(exc))


def _run_scenario(
    scenario: Dict[str, Any],
    *,
    profile_id: str,
    base_url: str,
    endpoint: str,
    judge: Optional[Any],
    pass_threshold: float,
    run_context: Optional[Dict[str, Any]] = None,
    user_token: Optional[str] = None,
    prompt_override: Optional[Dict[str, int]] = None,
    model_override: Optional[Dict[str, Any]] = None,
    profile_version_id: Optional[str] = None,
    runner_kind: str = "chat",
    profile_model: Optional[Dict[str, Any]] = None,
    request_shape: Optional[Dict[str, Any]] = None,
    environment: str = "staging",
) -> Dict[str, Any]:
    """Hit the agent endpoint with the scenario, capture SSE answer chunks,
    then score against expected_substrings and (optionally) the judge.

    `run_context` is the per-run override (business_id, customer_id, …)
    captured at trigger time. It's merged into the synthesized payload
    so endpoint Pydantic validators stop rejecting requests with
    "business_id required". Per-scenario `payload` keys still win.

    `runner_kind` feeds the `admin.services.eval_scorers.SCORERS` registry
    lookup (via `RUNNER_DEFAULT_SCORER`, overridable per-scenario with
    `assert.type`) that picks the scoring path. `"chat"` (default) has no
    registry entry, so it resolves to no scorer and keeps today's
    substring+judge composition entirely unchanged. A plugin runner kind with
    a `default_scorer` resolves that scorer instead — substring/judge logic is
    skipped entirely for that suite kind.
    """
    started = time.monotonic()
    scenario_id = scenario.get("id") or scenario.get("name", "unnamed")
    question = scenario.get("question") or scenario.get("input") or ""
    expected = scenario.get("expected_answer") or scenario.get("expected") or ""
    expected_substrings: List[str] = scenario.get("expected_substrings") or []
    eval_ref = str(uuid.uuid4())
    ref_token = _EVAL_REF.set(eval_ref)
    try:
        with _lmnr_span(
            name=f"eval_scenario::{scenario_id}",
            span_type="DEFAULT",
            attributes={
                "waj.eval.ref": eval_ref,
                "waj.eval.scenario_id": scenario_id,
                "waj.eval.category": scenario.get("category"),
                "waj.eval.profile_id": profile_id,
                "waj.eval.question": question,
                "waj.eval.expected": expected,
            },
        ):
            invoke_error: Optional[str] = None
            tool_trace = ""
            metrics: Dict[str, Any] = {}
            # Deterministic per-turn checks (admin/services/eval_checks.py): a
            # scenario either has `checks` on its single question, or `turns`
            # [{question, checks}] played in ONE session (multi-turn: "yes" never
            # saves, a clarification then the answer).
            turn_specs: List[Dict[str, Any]] = (
                list(scenario.get("turns") or [])
                or ([{"question": question, "checks": scenario.get("checks")}]
                    if scenario.get("checks") else [])
            )
            check_results: List[Dict[str, Any]] = []
            runner_turn_failures: Optional[List[str]] = None
            plugin_runner = runner_for(runner_kind)
            runner_turns = getattr(plugin_runner, "run_turns", None)
            runner_invoke = getattr(plugin_runner, "invoke", None)
            try:
                if runner_turns is not None and scenario.get("turns"):
                    (actual_output, tool_trace, http_status, latency_ms,
                     runner_turn_failures) = runner_turns(
                        scenario, list(scenario["turns"]),
                        metrics=metrics,
                        base_url=base_url,
                        endpoint=endpoint,
                        run_context=run_context,
                        user_token=user_token,
                        prompt_override=prompt_override,
                        model_override=model_override,
                        profile_version_id=profile_version_id,
                        environment=environment,
                    )
                elif runner_invoke is not None:
                    actual_output, tool_trace, http_status, latency_ms = runner_invoke(
                        base_url=base_url,
                        endpoint=endpoint,
                        scenario=scenario,
                        run_context=run_context,
                        user_token=user_token,
                        prompt_override=prompt_override,
                        model_override=model_override,
                        profile_version_id=profile_version_id,
                        metrics=metrics,
                        environment=environment,
                    )
                elif turn_specs:
                    actual_output, tool_trace, http_status, latency_ms, check_results = _run_turns(
                        scenario, turn_specs,
                        base_url=base_url, endpoint=endpoint, run_context=run_context,
                        user_token=user_token, prompt_override=prompt_override,
                        model_override=model_override, profile_version_id=profile_version_id,
                        metrics=metrics, request_shape=request_shape,
                    )
                else:
                    actual_output, tool_trace, http_status, latency_ms = _invoke_endpoint(
                        base_url=base_url,
                        endpoint=endpoint,
                        scenario=scenario,
                        run_context=run_context,
                        user_token=user_token,
                        prompt_override=prompt_override,
                        model_override=model_override,
                        profile_version_id=profile_version_id,
                        metrics=metrics,
                        request_shape=request_shape,
                    )
            except Exception as exc:
                actual_output, http_status, latency_ms = "", 0, int((time.monotonic() - started) * 1000)
                invoke_error = str(exc)[:500]

            # Derive per-scenario metrics + cost (metrics stays {} on error → defaults apply)
            ttft_ms = metrics.get("ttft_ms", latency_ms)
            usage = metrics.get("usage") or {"input_tokens": 0, "output_tokens": 0}
            _usage_from_output = getattr(plugin_runner, "usage_from_output", None)
            if _usage_from_output is not None and not (usage.get("input_tokens") or usage.get("output_tokens")):
                # A runner whose endpoint answers plain JSON, not SSE: the SSE
                # usage harvest saw nothing, so read the body's own usage.
                usage = _usage_from_output(actual_output) or usage
            runmetrics = metrics.get("runmetrics") or {"iterations": 1, "tool_count": 0}
            executed_tools: List[str] = metrics.get("executed_tools") or []
            tool_summary = _tool_error_summary(metrics.get("tool_calls") or [])
            tool_errors: List[Dict[str, Any]] = tool_summary["tool_errors"]
            from core import pricing
            # Spec §5.4: price against the override model OR the profile's own model.
            # Baseline runs (no model_override) fall back to profile_model so the
            # baseline column shows cost rather than "—".
            pricing_model = model_override or profile_model
            cost = pricing.cost_usd(
                pricing_model, usage["input_tokens"], usage["output_tokens"]
            ) if pricing_model else None

            # 2xx is a precondition for any kind of pass. A 422 with a
            # non-empty error body used to slip through the "non-empty
            # actual = pass" fallback below — that's what produced the
            # bogus 100% score against an endpoint whose required
            # business_id wasn't provided.
            http_ok = 200 <= (http_status or 0) < 300

            missing: List[str] = []
            substring_passed: Optional[bool] = None
            judge_result: Optional[Dict[str, Any]] = None
            grading_note: Optional[str] = None
            scorer_detail: Optional[Dict[str, Any]] = None
            scorer_score: Optional[float] = None

            # Resolve a non-chat scorer through the registry: an explicit
            # scenario `assert.type` wins, else the suite's runner_kind maps to
            # its runner's default scorer. 'chat' has no default entry, so chat
            # suites resolve NO scorer and always take the legacy substring/judge
            # path below, unchanged.
            from admin.services.eval_scorers import SCORERS, RUNNER_DEFAULT_SCORER

            scorer_type = (
                (scenario.get("assert") or {}).get("type")
                or RUNNER_DEFAULT_SCORER.get(runner_kind, "")
            )
            scorer = SCORERS.get(scorer_type)

            if scorer is not None:
                # Non-chat scorer (a plugin's). No substring/judge signal
                # applies here — the registry scorer owns passed/score. It
                # grades against the scenario's reference data, nested at
                # payload["catalog"] (the dict the runner actually POSTs) or
                # given as `scenario["catalog"]`.
                catalog = (scenario.get("payload") or {}).get("catalog") or scenario.get("catalog") or {}
                _r = scorer(actual_output or "", catalog)
                passed = http_ok and _r["passed"]
                scorer_score = _r["score"] if http_ok else 0.0
                scorer_detail = _r["detail"]
            else:
                # Substring scoring — always runs (but only counts when http_ok).
                missing = [
                    frag for frag in expected_substrings
                    if frag.lower() not in (actual_output or "").lower()
                ]
                substring_passed = (not missing) if expected_substrings else None

                # Judge scoring — only when wired up and an expected answer exists.
                # Skip judging on non-2xx so we don't burn judge tokens grading
                # error bodies.
                if judge is not None and expected and actual_output and http_ok:
                    try:
                        from admin.e2e_runner import _invoke_judge

                        judge_result = _invoke_judge(
                            judge, _judge_question(scenario, question, run_context=run_context), expected,
                            actual_output, pass_threshold,
                            tool_trace=tool_trace,
                            expected_tools=scenario.get("expected_tools") or [],
                            rubric=scenario.get("rubric"),
                        )
                    except Exception as exc:
                        judge_result = {
                            "score": 0.0, "pass": False,
                            "rationale": f"judge_error: {exc}"[:300],
                        }

                # Pass/fail decision: 2xx is required. Then substring AND judge
                # must both agree when both are present; otherwise the one
                # configured signal decides.
                if not http_ok:
                    passed = False
                elif judge_result is not None and substring_passed is not None:
                    passed = bool(judge_result["pass"]) and substring_passed
                elif judge_result is not None:
                    passed = bool(judge_result["pass"])
                elif substring_passed is not None:
                    passed = substring_passed
                elif expected:
                    # The scenario declares an expected_answer that needs an LLM judge,
                    # but none ran (no evaluator_model configured on the suite, or the
                    # judge LLM couldn't be built — e.g. GOOGLE_API_KEY unset). We must
                    # NOT silently pass an answer we never graded — that's the bug where
                    # everything showed "passed". Fail it with a clear, actionable note.
                    #
                    # With no judge the suite editor promises "check expected
                    # words only", so that is what happens: the reply passes
                    # when it contains the expected text (case and spacing
                    # ignored). Until 2026-11-22 it was failed unread, so a
                    # suite written in the portal with the default "no judge"
                    # could not pass a single case. A full-sentence expected
                    # answer will rarely appear verbatim; the note says what
                    # to do about that.
                    def _norm(text: Any) -> str:
                        return " ".join(str(text or "").lower().split())

                    passed = bool(_norm(expected)) and _norm(expected) in _norm(actual_output)
                    grading_note = (
                        "no judge model: checked that the reply contains the expected text"
                        if passed else
                        "no judge model: the reply does not contain the expected text. "
                        "Without a judge the expected answer is matched as words; pick a "
                        "judge model in the suite's settings to grade it for meaning"
                    )
                else:
                    # Pure smoke scenario: no expected_answer and no substrings, so any
                    # non-empty 2xx body is a legitimate pass.
                    passed = bool(actual_output) and not invoke_error

            # Tool assertion — a HARD gate, independent of (and applied AFTER) the
            # judge/substring verdict above. Verifies the agent actually exercised
            # the intended MECHANISM (delegated via an `ask_*` tool, read a skill
            # via `read_skill`, …), not just that the final answer reads well — a
            # judge grades prose, it can't tell "answered directly" from
            # "delegated then answered". Both fields are optional; absent means
            # no assertion (back-compat with every existing scenario).
            expected_tools_field: List[str] = scenario.get("expected_tools") or []
            forbidden_tools_field: List[str] = scenario.get("forbidden_tools") or []
            missing_expected_tools: List[str] = []
            forbidden_tools_used: List[str] = []
            errored_expected_tools: List[str] = []
            tool_assertion_passed: Optional[bool] = None
            if expected_tools_field or forbidden_tools_field:
                # Canonicalised on BOTH sides. A scenario with per-turn `checks`
                # collects its executed tools as raw ids (`lookup_customer`)
                # while the single-turn path collects display names
                # (`lookup customer`), so comparing the expected names against
                # the raw set failed every scenario that used `expected_tools`
                # and `checks` together, whatever the agent did (2026-11-22).
                executed_set = {_canonical_tool_name(t) for t in executed_tools}
                missing_expected_tools = [
                    t for t in expected_tools_field
                    if _canonical_tool_name(t) not in executed_set
                ]
                forbidden_tools_used = [
                    t for t in forbidden_tools_field
                    if _canonical_tool_name(t) in executed_set
                ]
                # An expected tool that was CALLED but only ever came back with an
                # error did not do its job: "called get_customer_summary" is not
                # the mechanism the scenario asserts if the lookup 500'd. A retry
                # that succeeded clears it (see `_tool_error_summary`).
                errored_expected_tools = [
                    t for t in expected_tools_field
                    if t not in missing_expected_tools
                    and _canonical_tool_name(t) in tool_summary["failed_tools"]
                ]
                tool_assertion_passed = (not missing_expected_tools and not forbidden_tools_used
                                         and not errored_expected_tools)
                if not tool_assertion_passed:
                    passed = False
            checks_passed: Optional[bool] = None
            # A runner-played chat's per-reply checks are its
            # reply_check_failures below; this gate is the SSE agents' per-turn
            # `checks`.
            if turn_specs and runner_turn_failures is None:
                # Hard gate, like the tool assertion: facts the judge cannot see.
                checks_passed = bool(check_results) and all(c.get("ok") for c in check_results)
                if not checks_passed:
                    passed = False
                if (not scenario.get("expected_answer") and not expected_substrings and checks_passed
                        and http_ok and not invoke_error and tool_assertion_passed is not False):
                    # A checks-only scenario: the checks ARE the grade (never
                    # overriding a failed expected/forbidden-tools assertion).
                    passed = True
                if tool_assertion_passed is None:
                    tool_assertion_passed = checks_passed

            # Incidental tool errors (a tool the scenario didn't assert on) are
            # recorded but don't fail it: an agent that hits an error and says
            # "I can't confirm that right now" is doing what its prompt asks, and
            # the judge (which sees the results in the trace) grades that. A
            # scenario or suite that wants ANY tool error to fail opts in with
            # `fail_on_tool_error: true`.
            fail_on_tool_error = bool(
                scenario.get("fail_on_tool_error")
                if scenario.get("fail_on_tool_error") is not None
                else (run_context or {}).get("fail_on_tool_error")
            )
            if tool_errors and fail_on_tool_error:
                passed = False
                tool_assertion_passed = False

            # Reply checks — the same kind of HARD gate for things code can check
            # exactly (reply language by script, emoji count, forbidden fragments),
            # so a judge can't wave through an English answer to an Arabic question.
            # Absent → no check (every existing scenario is unaffected).
            reply_check_failures: List[str] = []
            if runner_turn_failures is not None:
                # A chat's transcript holds the customer's words too: its
                # checks already ran on each AI reply (the runner's run_turns).
                reply_check_failures = runner_turn_failures if http_ok else []
                if reply_check_failures:
                    passed = False
            elif scenario.get("reply_checks") and http_ok:
                from admin.services.eval_scorers import reply_check_failures as _reply_checks
                reply_check_failures = _reply_checks(actual_output or "", scenario["reply_checks"])
                if reply_check_failures:
                    passed = False

            if invoke_error:
                _mark_span_error(invoke_error)
            elif not http_ok:
                _mark_span_error(f"HTTP {http_status} from the agent")
            try:
                from lmnr import Laminar

                Laminar.event(
                    "eval.scenario.result",
                    attributes={
                        "scenario_id": scenario_id,
                        "passed": passed,
                        "missing_count": len(missing),
                        "judge_score": judge_result["score"] if judge_result else None,
                        "http_status": http_status,
                        "latency_ms": latency_ms,
                        "tool_assertion_passed": tool_assertion_passed,
                        "executed_tools": ", ".join(executed_tools) if executed_tools else None,
                        "tool_error_count": len(tool_errors),
                        "tool_errors": (", ".join(sorted({e["tool"] for e in tool_errors}))
                                        if tool_errors else None),
                    },
                )
            except Exception:
                pass

            return {
                "scenario_id": scenario_id,
                "eval_ref": eval_ref,
                "name": scenario.get("name", scenario_id),
                "category": scenario.get("category"),
                "question": question,
                "expected": expected,
                "expected_substrings": expected_substrings,
                "passed": passed,
                "score": (
                    scorer_score if scorer is not None
                    else (judge_result["score"] if judge_result else (1.0 if passed else 0.0))
                ),
                "missing_substrings": missing,
                "judge": judge_result,
                "criteria": (judge_result.get("criteria") if judge_result else None),
                "grading_note": grading_note,
                "http_status": http_status,
                "latency_ms": latency_ms,
                "ttft_ms": ttft_ms,
                "total_ms": int((time.monotonic() - started) * 1000),
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "cost_usd": cost,
                "iterations": runmetrics.get("iterations", 1),
                "tool_count": runmetrics.get("tool_count", 0),
                "executed_tools": executed_tools,
                "expected_tools": expected_tools_field or None,
                "forbidden_tools": forbidden_tools_field or None,
                "missing_expected_tools": missing_expected_tools or None,
                "forbidden_tools_used": forbidden_tools_used or None,
                "errored_expected_tools": errored_expected_tools or None,
                "tool_assertion_passed": tool_assertion_passed,
                # Tool results that came back error-shaped ({"error": ...},
                # {"ok": false}, status=error, "Error: ..."), one entry per failed
                # call: {tool, error (excerpt), turn?}. Recorded whether or not
                # they fail the scenario — see the policy above.
                "tool_errors": tool_errors or None,
                "tool_error_count": len(tool_errors),
                "empty_tool_results": tool_summary["empty_tool_results"] or None,
                "reply_check_failures": reply_check_failures or None,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "error": invoke_error,
                "actual": actual_output,
                "actual_output_excerpt": (
                    (actual_output[:200] + "…")
                    if actual_output and len(actual_output) > 200
                    else (actual_output or "")
                ),
                # Additive — only non-chat scorers populate
                # this; existing 'chat' results are unaffected (stays None).
                "scorer_detail": scorer_detail,
                "checks": check_results or None,
                "checks_passed": checks_passed,
            }


    # -- HTTP / SSE plumbing -----------------------------------------------------
    finally:
        _EVAL_REF.reset(ref_token)


def _run_turns(
    scenario: Dict[str, Any],
    turn_specs: List[Dict[str, Any]],
    *,
    base_url: str,
    endpoint: str,
    run_context: Optional[Dict[str, Any]],
    user_token: Optional[str],
    prompt_override: Optional[Dict[str, int]],
    model_override: Optional[Dict[str, Any]],
    profile_version_id: Optional[str],
    metrics: Dict[str, Any],
    request_shape: Optional[Dict[str, Any]] = None,
) -> tuple:
    """Play `turn_specs` in ONE chat session and run each turn's checks.

    Returns (actual_output, trace, http_status, latency_ms, check_results):
    the answers joined as a transcript, the last status, the summed latency,
    and every check result tagged with its turn.
    """
    from admin.services.eval_checks import proposals_from_sse, run_checks, tools_from_sse

    session_id = str(uuid.uuid4())
    business_id = (scenario.get("business_id") or (run_context or {}).get("business_id"))
    parts: List[str] = []
    traces: List[str] = []
    results: List[Dict[str, Any]] = []
    tools_all: set = set()
    tool_calls_all: List[Dict[str, Any]] = []
    status, total_ms, worst_status = 0, 0, 0
    state: Optional[str] = None  # the demo's stateless turns carry verification here
    for i, spec in enumerate(turn_specs, 1):
        turn_scenario = {k: v for k, v in scenario.items() if k not in ("turns", "checks", "payload")}
        turn_scenario["question"] = spec.get("question") or ""
        turn_scenario["session_id"] = session_id
        turn_scenario["is_first_message"] = i == 1
        if state:
            turn_scenario["_state"] = state
        turn_metrics: Dict[str, Any] = {"keep_body": True}
        answer, trace, status, ms = _invoke_endpoint(
            base_url=base_url, endpoint=endpoint, scenario=turn_scenario, run_context=run_context,
            user_token=user_token, prompt_override=prompt_override, model_override=model_override,
            profile_version_id=profile_version_id, metrics=turn_metrics,
            request_shape=request_shape,
        )
        total_ms += ms
        if not (200 <= (status or 0) < 300):
            worst_status = worst_status or status   # any failed turn fails the scenario
        body = turn_metrics.get("body") or ""
        if request_shape and request_shape.get("state_token"):
            from admin.services.test_chat import state_token_from_sse

            state = state_token_from_sse(body) or state
        tools = tools_from_sse(body)
        tools_all.update(tools)
        for r in run_checks(spec.get("checks") or [], answer=answer,
                            proposals=proposals_from_sse(body), tools=tools,
                            caller_text=turn_scenario["question"], business_id=business_id,
                            user_token=user_token, charts=body.count('"type": "chart"')):
            results.append({"turn": i, **r})
        parts.append(f"[turn {i}] USER: {turn_scenario['question']}\nASSISTANT: {answer}")
        traces.append(trace)
        tool_calls_all.extend({**c, "turn": i} for c in (turn_metrics.get("tool_calls") or []))
        for key in ("ttft_ms", "usage", "runmetrics"):
            if key in turn_metrics and key not in metrics:
                metrics[key] = turn_metrics[key]
    metrics["executed_tools"] = sorted(tools_all)
    metrics["tool_calls"] = tool_calls_all
    return "\n\n".join(parts), "\n".join(traces)[:4000], worst_status or status, total_ms, results


def _resolve_profile(client, profile_id: str) -> Optional[Dict[str, Any]]:
    try:
        rows = (
            client.table("agent_profiles")
            .select("profile_id, agent_kind, slug, endpoint_path, environment")
            .eq("profile_id", profile_id).limit(1).execute().data or []
        )
        return rows[0] if rows else None
    except Exception as exc:
        logger.warning("eval: profile lookup failed (%s)", exc)
        return None


def _base_url_for(client, environment: str) -> str:
    """Mirror of `admin.e2e_runner._base_url_for` so evals hit the same
    deployment target as E2E runs. Local dev defaults to 127.0.0.1:8000.
    """
    env_key = (environment or "staging").upper()
    override = os.getenv(f"AI_AGENT_BASE_URL_{env_key}")
    if override:
        return override
    try:
        rows = (
            client.table("agent_deployment_targets").select("base_url")
            .eq("environment", environment).eq("is_default", True).eq("is_active", True)
            .limit(1).execute().data or []
        )
        if rows and rows[0].get("base_url"):
            return rows[0]["base_url"]
    except Exception as exc:
        logger.warning("eval: deployment_target lookup failed (%s)", exc)
    return os.getenv("AI_AGENT_BASE_URL") or f"http://127.0.0.1:{os.getenv('PORT') or 8000}"


def _resolve_evaluator_model(client: Any, suite: Dict[str, Any]) -> Optional[str]:
    """Resolve the suite's judge model NAME from its ``evaluator_model_id``.

    ``agent_eval_suites.evaluator_model_id`` is a FK into ``agent_models``; the
    judge LLM (``_try_build_judge``) is built from the model's ``name``. Returns
    None when the suite has no evaluator configured — the suite editor's
    "— substring only (no LLM judge) —" option — in which case scenarios that
    declare an ``expected_answer`` cannot be graded and are failed rather than
    passed (see ``_run_scenario``).

    Previously the runner read a non-existent ``suite["evaluator_model"]`` key
    (the column is ``evaluator_model_id``), so the operator's choice was always
    ignored and the judge never ran — which is why every scenario "passed".
    """
    model_id = suite.get("evaluator_model_id")
    if not model_id:
        return None
    try:
        rows = (
            client.table("agent_models")
            .select("model_string")
            .eq("model_id", model_id)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception as exc:
        logger.warning("eval: evaluator model lookup failed (%s)", exc)
        return None
    # Use `model_string` (the real provider model id, e.g.
    # "gemini-3.5-flash"), NOT the friendly `name`
    # ("…-default") — the latter isn't a valid model id for the SDK.
    return (rows[0].get("model_string") if rows else None) or None


def _try_build_judge(model_string: Optional[str], model_id: Optional[str] = None) -> Optional[Any]:
    """Same shape as `admin.e2e_runner._try_build_judge` — reused so eval
    and E2E judge behavior stay in sync. `model_id` is the suite's
    evaluator_model_id, so the judge is built from that exact row."""
    if not model_string:
        return None
    try:
        from admin.e2e_runner import _try_build_judge as _build

        return _build(model_string, model_id=model_id)
    except Exception as exc:
        logger.warning("eval: judge build failed (%s)", exc)
        return None


_SSE_DATA_RE = re.compile(r"^data:\s*(.+)$", re.MULTILINE)

# Structured SSE event types that carry a raw tool identifier directly in a
# "tool"/"tool_name"/"name" field — an on_tool_start `action` event, a
# `tool_call`, and any future agent adopting the same shape.
_TOOL_EVENT_TYPES = ("tool", "tool_call", "tool_result", "action")

# Fallback for agents whose manual tool loop never emits a structured tool
# event — only `{"type": "thought", "content": "..."}"` narration. These are
# the EXACT English templates `runtime/prompts/_ui.py` renders via
# `get_ui_message("using_tool"/"retrieved_data")` and the analogous
# `on_tool_start`/`on_tool_end` thought text a LangGraph agent emits.
# Matched generically — no scenario- or tool-specific strings — so any tool
# name produces a recognizable pair.
_THOUGHT_TOOL_PATTERNS = [
    re.compile(r"^Using\s+(.+?)\.\.\.$", re.IGNORECASE),           # using_tool
    re.compile(r"^Retrieved data from\s+(.+)$", re.IGNORECASE),    # retrieved_data
    re.compile(r"^Fetching\s+(.+?)\.\.\.$", re.IGNORECASE),        # on_tool_start
    re.compile(r"^Retrieved\s+(.+)\s+data$", re.IGNORECASE),       # on_tool_end
]


def _canonical_tool_name(name: str) -> str:
    """Canonicalize a tool identifier for comparison.

    Mirrors `core.base_agent.BaseStreamingAgent.format_tool_name` — the exact
    transform business.py already applies before narrating "Using X..." — so
    a raw tool id (``"read_skill"``) and its narrated display
    (``"read skill"``) collapse to the identical string. Falls back to a
    plain underscore→space swap if the import ever fails (defensive; keeps
    this module importable standalone in tests without the runtime package).
    """
    raw = (name or "").strip()
    if not raw:
        return ""
    try:
        from core.base_agent import BaseStreamingAgent

        display = BaseStreamingAgent.format_tool_name(raw)
    except Exception:
        display = raw.replace("_", " ")
    return display.strip().lower()


def _extract_executed_tools(body: str) -> List[str]:
    """Return the DISTINCT canonicalized tool names invoked during a turn.

    Two extraction strategies, tried in order:

    1. **Structured events** — any SSE event whose ``type`` is in
       ``_TOOL_EVENT_TYPES`` and carries a ``tool``/``tool_name``/``name``
       field. Used verbatim (canonicalized). If the transcript has ANY such
       event we trust it as the agent's tool-event protocol and stop there
       — even if a particular event happens to lack a name — rather than
       falling through to narration parsing and risking a double count for
       an agent that emits both a structured event AND a thought.
    2. **Thought-narration fallback** — engaged ONLY when the transcript has
       NO structured tool event at all. A manual tool-calling loop may
       emit no `action`/`tool_call` events, just
       `{"type": "thought", "content": "Using <display>..."}` /
       `"Retrieved data from <display>"` pairs — the root cause of
       `tool_count` once reading 0 for every scenario of such an agent.
       Regex-matches those generic templates and canonicalizes the captured
       display name the same way the agent produced it, so it lines up with
       strategy 1's canonicalization of a raw tool id.

    Used both for `tool_count` (below) and the `expected_tools`/
    `forbidden_tools` hard assertion in `_run_scenario` — a presence/
    distinct-name signal, not a raw event count (calling the same tool
    twice in one turn still counts once).
    """
    names: set[str] = set()
    thought_lines: List[str] = []
    saw_structured = False
    for m in _SSE_DATA_RE.finditer(body):
        raw = m.group(1).strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            evt = json.loads(raw)
        except Exception:
            continue
        if not isinstance(evt, dict):
            continue
        etype = evt.get("type")
        if etype in _TOOL_EVENT_TYPES:
            saw_structured = True
            name = evt.get("tool") or evt.get("tool_name") or evt.get("name")
            if name:
                names.add(_canonical_tool_name(str(name)))
        elif etype == "thought":
            content = evt.get("content")
            if content:
                thought_lines.append(str(content))

    if saw_structured:
        return sorted(names)

    for line in thought_lines:
        stripped = line.strip()
        for pattern in _THOUGHT_TOOL_PATTERNS:
            found = pattern.match(stripped)
            if found:
                names.add(_canonical_tool_name(found.group(1)))
                break
    return sorted(names)


def _extract_usage_from_sse(body: str) -> Dict[str, Any]:
    """Pull the last `usage` event's token counts from an SSE body.

    Also captures `iterations` and `tool_count` when the agent emits them
    in the usage event (some agents do; others may not). The caller
    should treat those fields as optional extras and only act on them when
    present — they are absent for agents that don't emit them.
    """
    usage: Dict[str, Any] = {"input_tokens": 0, "output_tokens": 0}
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            evt = json.loads(line[len("data:"):].strip())
        except Exception:
            continue
        if isinstance(evt, dict) and evt.get("type") == "usage":
            usage["input_tokens"] = int(evt.get("input_tokens") or 0)
            usage["output_tokens"] = int(evt.get("output_tokens") or 0)
            # Exact counts emitted by agents that track them (not every agent does).
            # We propagate them as-is; absent keys stay absent so the caller
            # can distinguish "agent emitted 0 tools" from "agent didn't emit".
            if "iterations" in evt:
                usage["iterations"] = int(evt["iterations"])
            if "tool_count" in evt:
                usage["tool_count"] = int(evt["tool_count"])
    return usage


def _extract_run_metrics(body: str) -> Dict[str, int]:
    """Count tool calls + LLM iterations from the SSE transcript.

    ``tool_count`` = number of DISTINCT tools invoked, from
    `_extract_executed_tools()` — which recognizes both structured
    tool/tool_call/tool_result/action events AND, as a fallback when no
    structured event exists, an agent's thought-narration pairs. That
    fallback is the fix for `tool_count` reading 0 for an agent whose manual
    tool loop never emits a structured event.

    ``iterations`` = tool_count + 1 (each tool round triggers another LLM
    pass before the final answer). A pure answer with no tools is 1
    iteration, 0 tools.

    NOTE: this is DISTINCT tool names, not raw invocation count — calling
    the same tool twice in one turn still counts once. This formalizes the
    previous pair-collapsing heuristic (which already approximated
    invocation count rather than raw event count) and matches what
    `expected_tools`/`forbidden_tools` need: "was tool X used", not "how
    many times".
    """
    tool_count = len(_extract_executed_tools(body))
    return {"tool_count": tool_count, "iterations": (tool_count + 1) if tool_count else 1}


# -- tool-result errors -------------------------------------------------------
# A tool that fails rarely raises: an edge-function wrapper returns
# `{"error": "<fn> returned 500", "details": ...}`, other modules answer
# `{"ok": false, ...}` / `{"status": "error"}`, and the agent then carries on
# and phrases a reply around the gap. Nothing on the span is marked failed, so
# the run read "0 tool errors" while get_customer_summary had 500'd. These
# helpers read the RESULT each tool handed back to the model.

# Events carrying a tool's result: `observation` (`{"type", "tool",
# "result"}`, result clipped to 500 chars by the agent) and `tool_result` (any agent adopting that name).
_TOOL_RESULT_EVENT_TYPES = ("observation", "tool_result")
_ERROR_STATUSES = frozenset({"error", "failed", "failure"})
_TOOL_ERROR_EXCERPT = 160
_EMPTY_LITERALS = frozenset({"", "[]", "{}", "null", "none", '""'})
# For results the agent clipped mid-JSON (json.loads fails): the same shapes,
# matched on the text. Only a non-empty error value counts.
_ERROR_TEXT_RES = (
    re.compile(r"""["']error["']\s*:\s*(?!null\b|None\b|false\b|False\b|""|''|\[\]|\{\})"""),
    re.compile(r"""["'](?:ok|success)["']\s*:\s*(?:false|False)\b"""),
    re.compile(r"""^\s*\{\s*["']status["']\s*:\s*["'](?:error|failed|failure)["']""", re.IGNORECASE),
    re.compile(r"""["']isError["']\s*:\s*(?:true|True)\b"""),
)
# Plain-text failures (LangChain's handle_tool_error, MCP adapters).
_ERROR_PREFIX_RE = re.compile(r"^\s*(?:error|exception|tool error)\b\s*[:\-]", re.IGNORECASE)


def _clip_excerpt(text: str, limit: int = _TOOL_ERROR_EXCERPT) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _error_in_obj(obj: Any, depth: int = 0) -> Optional[str]:
    """The error message if `obj` is (or wraps, a few dicts deep) an error.

    Lists are NOT walked: a list of records (bookings, payments) can carry a
    `status: "failed"` that is data, not a tool failure. `status` is only read
    at the top level for the same reason.
    """
    if not isinstance(obj, dict) or depth > 3:
        return None
    err = obj.get("error")
    if err not in (None, False, "", [], {}):
        if isinstance(err, dict):
            msg = err.get("message") or err.get("error") or json.dumps(err, ensure_ascii=False)
        else:
            msg = str(err)
        details = obj.get("details") or obj.get("detail") or obj.get("message")
        if details and isinstance(details, (str, int, float)):
            msg = f"{msg} — {details}"
        return str(msg)
    if obj.get("ok") is False or obj.get("success") is False or obj.get("isError") is True:
        why = (obj.get("message") or obj.get("reason") or obj.get("detail")
               or obj.get("details") or json.dumps(obj, ensure_ascii=False))
        return str(why)
    status = obj.get("status")
    if depth == 0 and isinstance(status, str) and status.strip().lower() in _ERROR_STATUSES:
        return str(obj.get("message") or obj.get("reason") or obj.get("detail") or f"status: {status}")
    for value in obj.values():
        if isinstance(value, dict):
            found = _error_in_obj(value, depth + 1)
            if found:
                return found
    return None


def _tool_result_error(result: Any) -> Optional[str]:
    """A short excerpt of the error a tool result reports, or None.

    Accepts the parsed value or its text (the SSE `observation.result` is the
    tool's string output, often JSON, clipped to 500 chars).
    """
    if result is None:
        return None
    if isinstance(result, (dict, list)):
        found = _error_in_obj(result)
        return _clip_excerpt(found) if found else None
    text = str(result)
    stripped = text.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        found = _error_in_obj(parsed)
        return _clip_excerpt(found) if found else None
    if parsed is not None:
        return None  # a list / scalar parsed fine — data, not an error shape
    if _ERROR_PREFIX_RE.match(stripped):
        return _clip_excerpt(stripped)
    if stripped[:1] in "{[":
        for pattern in _ERROR_TEXT_RES:
            m = pattern.search(stripped)
            if m:
                return _clip_excerpt(stripped[m.start():])
    return None


def _tool_result_empty(result: Any) -> bool:
    """True when a tool came back with nothing (`[]`, `{}`, `null`, `""`, or a
    small envelope of empty values like `{"data": []}`). Not an error — a
    lookup can legitimately find nothing — but worth seeing next to one."""
    if result is None:
        return True
    parsed = result
    if not isinstance(result, (dict, list)):
        stripped = str(result).strip()
        if stripped.lower() in _EMPTY_LITERALS:
            return True
        try:
            parsed = json.loads(stripped)
        except Exception:
            return False
    if parsed in (None, "", [], {}):
        return True
    if isinstance(parsed, dict) and 0 < len(parsed) <= 3:
        return all(v in (None, "", [], {}) for v in parsed.values())
    return False


def _tool_call_record(tool: Any, result: Any) -> Dict[str, Any]:
    error = _tool_result_error(result)
    return {
        "tool": str(tool or "tool"),
        "error": error,
        "empty": (not error) and _tool_result_empty(result),
    }


def _extract_tool_calls(body: str) -> List[Dict[str, Any]]:
    """One `{tool, error, empty}` record per tool RESULT in an SSE transcript,
    in stream order (a tool called twice gives two records, so a retry that
    succeeded is distinguishable from one that never did)."""
    calls: List[Dict[str, Any]] = []
    for m in _SSE_DATA_RE.finditer(body or ""):
        raw = m.group(1).strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            evt = json.loads(raw)
        except Exception:
            continue
        if not isinstance(evt, dict) or evt.get("type") not in _TOOL_RESULT_EVENT_TYPES:
            continue
        name = evt.get("tool") or evt.get("tool_name") or evt.get("name")
        if not name:
            continue
        result = next((evt[k] for k in ("result", "output", "content") if k in evt), None)
        calls.append(_tool_call_record(name, result))
    return calls


def _tool_error_summary(calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Fold `_extract_tool_calls` records into the scenario-result fields.

    `failed_tools` (canonical names) = tools with an errored result and NO
    successful one, i.e. a retry that worked does not count against the tool.
    """
    errors = [
        {"tool": c["tool"], "error": c["error"],
         **({"turn": c["turn"]} if c.get("turn") is not None else {})}
        for c in calls if c.get("error")
    ]
    errored = {_canonical_tool_name(c["tool"]) for c in calls if c.get("error")}
    succeeded = {_canonical_tool_name(c["tool"]) for c in calls if not c.get("error")}
    empty = sorted({c["tool"] for c in calls if c.get("empty")})
    return {
        "tool_errors": errors,
        "failed_tools": errored - succeeded,
        "empty_tool_results": empty,
    }


def _override_headers(
    prompt_override: Optional[Dict[str, int]],
    model_override: Optional[Dict[str, Any]],
    profile_version_id: Optional[str],
) -> Dict[str, str]:
    """The X-WAJ-* headers that run a request against a variant. The agent
    process reads them in middleware (main.py) and pins those prompt
    versions / that model / that promoted snapshot for the request — that's
    what makes A/B work without any per-call profile editing. The middleware
    only honours them with a verified operator JWT in `Authorization`. An
    empty dict would still be parsed as "no override"; only forward when
    non-empty to keep the headers off normal eval runs."""
    headers: Dict[str, str] = {}
    if prompt_override:
        headers["X-WAJ-Prompt-Override"] = json.dumps(prompt_override)
    if model_override:
        headers["X-WAJ-Model-Override"] = json.dumps(model_override)
    if profile_version_id:
        headers["X-WAJ-Profile-Version"] = profile_version_id
    headers.update(_eval_correlation_headers())
    return headers


def _invoke_endpoint(
    *,
    base_url: str,
    endpoint: str,
    scenario: Dict[str, Any],
    run_context: Optional[Dict[str, Any]] = None,
    user_token: Optional[str] = None,
    prompt_override: Optional[Dict[str, int]] = None,
    model_override: Optional[Dict[str, Any]] = None,
    profile_version_id: Optional[str] = None,
    metrics: Optional[Dict[str, Any]] = None,
    request_shape: Optional[Dict[str, Any]] = None,
) -> tuple[str, str, int, int]:
    """POST the scenario at the agent endpoint and harvest the SSE stream.
    Returns `(actual_output, tool_trace, http_status, latency_ms)` — the
    answer text plus the captured reasoning/tool-step trace for the judge.
    Raises on transport errors so the caller can record
    the scenario as errored.

    `user_token` should be the operator's Supabase JWT — the agent
    validates it as a normal auth_user request, so the eval exercises
    the real RLS path. When None we fall back to a sentinel; agents
    that require auth will reject the request with 401 (which is the
    correct failure mode to surface).

    `profile_version_id` is forwarded as `X-WAJ-Profile-Version` so the
    agent process's version-pin middleware can run the request against
    that immutable promoted snapshot instead of the live profile.
    """
    url = f"{base_url.rstrip('/')}{endpoint}"
    payload = _payload_for(scenario, run_context=run_context, request_shape=request_shape)
    bearer = user_token if user_token else "eval-runner"
    headers = {"Authorization": f"Bearer {bearer}",
               **_override_headers(prompt_override, model_override, profile_version_id)}
    if (request_shape or {}).get("pin_profile") and not profile_version_id:
        headers["X-WAJ-Profile-Id"] = request_shape["pin_profile"]
    start = time.monotonic()
    # 120s: grounded queries can legitimately call several reporting tools and
    # synthesize a long answer. The CSV→GCS sidecar is now fail-fast/no-op in
    # non-prod (see shared/cloud_storage.py), so this ceiling is a safety net,
    # not a workaround for a hang.
    #
    # Use a STREAMING read (h.stream) rather than h.post(...).text. The agent
    # endpoints return `text/event-stream` with `Connection: keep-alive` and
    # chunked transfer; a buffered `.text` read intermittently returned a
    # partial body (sometimes only the first `session` event, or a half-line)
    # before the server had flushed every `answer` chunk — which scored the
    # scenario 0 even though the agent answered correctly. Streaming lets us
    # drain the socket to EOF and accumulate every byte, so the captured
    # `actual` always contains the full set of `answer`/content events.
    body_parts: List[str] = []
    status_code = 0
    ttft_ms: Optional[int] = None
    with httpx.Client(timeout=120.0) as h:
        with h.stream("POST", url, json=payload, headers=headers) as resp:
            status_code = resp.status_code
            # iter_text drains the response to completion (decoding chunked
            # transfer-encoding for us) — we keep reading until the generator
            # is exhausted, i.e. the server closed the stream.
            for piece in resp.iter_text():
                if piece:
                    if ttft_ms is None and ('"type": "answer"' in piece or '"type":"answer"' in piece):
                        ttft_ms = int((time.monotonic() - start) * 1000)
                    body_parts.append(piece)
    body = "".join(body_parts)
    latency_ms = int((time.monotonic() - start) * 1000)
    actual = _extract_answer_chunks(body)
    trace = _extract_trace_chunks(body)
    # Many endpoints return JSON (not SSE) — e.g. a one-shot generator.
    # When that happens just use the body verbatim so substring matching
    # still works.
    if not actual:
        actual = body
    # Side-channel metrics via an out-param so the return arity stays a 4-tuple
    # and no existing caller/test breaks. Only _run_scenario passes a dict.
    if metrics is not None:
        metrics["ttft_ms"] = ttft_ms if ttft_ms is not None else latency_ms
        _usage = _extract_usage_from_sse(body)
        metrics["usage"] = {
            "input_tokens": _usage["input_tokens"],
            "output_tokens": _usage["output_tokens"],
        }
        # Prefer exact iterations/tool_count from the usage event (emitted by
        # agents that track them). Fall back to the SSE-event
        # proxy for agents that don't emit them — back-compatible.
        _proxy = _extract_run_metrics(body)
        metrics["runmetrics"] = {
            "iterations": _usage.get("iterations", _proxy["iterations"]),
            "tool_count": _usage.get("tool_count", _proxy["tool_count"]),
        }
        # Distinct tool NAMES (not just a count) for the expected_tools /
        # forbidden_tools hard assertion in `_run_scenario`. Independent of
        # the iterations/tool_count numeric fields above — those can come
        # from a `usage` event override on agents that report exact counts,
        # but no agent currently reports tool NAMES via `usage`, so this
        # always comes from the SSE-transcript extraction.
        metrics["executed_tools"] = _extract_executed_tools(body)
        # Per-result records ({tool, error, empty}) for the tool-error
        # detection in `_run_scenario`.
        metrics["tool_calls"] = _extract_tool_calls(body)
        if metrics.get("keep_body"):
            metrics["body"] = body
    return actual, trace, status_code, latency_ms


def _extract_answer_chunks(body: str) -> str:
    """Pull `content` out of every `data: {...}` SSE event.

    SSE frames are `data: {json}\\n\\n`. We concatenate the content of EVERY
    answer/content-bearing event (the agent streams the reply token-by-token as
    many `answer` chunks), and tolerate non-answer frames — `session`, `title`,
    `thought`, `proposal`, `done`, keep-alive comments (`: ...`) and `[DONE]`
    sentinels — by skipping them rather than letting them short-circuit the
    assembly. Multi-line `data:` segments and non-JSON payloads are handled
    gracefully (they just don't contribute to the actual string)."""
    chunks: List[str] = []
    for m in _SSE_DATA_RE.finditer(body):
        raw = m.group(1).strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            evt = json.loads(raw)
        except Exception:
            continue
        if not isinstance(evt, dict):
            continue
        if evt.get("type") == "answer" and evt.get("content"):
            chunks.append(str(evt["content"]))
        # Some endpoints emit `final_response` or `output` instead.
        elif evt.get("type") in ("final", "result", "final_response", "output") and evt.get("content"):
            chunks.append(str(evt["content"]))
    return "".join(chunks)


def _extract_trace_chunks(body: str) -> str:
    """Pull the agent's reasoning / tool steps out of the SSE stream.

    Captures `thought`/`action`/`tool*`/`proposal` events so the judge can see
    HOW the answer was grounded (which tools ran, what they returned). Without
    this the judge sees only the final answer text and under-credits correct,
    tool-grounded responses — it can't tell a real lookup from a confident guess
    (this is what once capped a docs-grounded suite around 0.5).
    """
    parts: List[str] = []
    for m in _SSE_DATA_RE.finditer(body):
        raw = m.group(1).strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            evt = json.loads(raw)
        except Exception:
            continue
        if not isinstance(evt, dict):
            continue
        t = evt.get("type")
        if t in ("thought", "action", "tool", "tool_call", "tool_result", "proposal"):
            for k in ("content", "text", "tool", "tool_name", "name", "result", "output", "detail"):
                v = evt.get(k)
                if v:
                    label = t if k in ("content", "text") else f"{t}.{k}"
                    parts.append(f"[{label}] {v}")
    return "\n".join(parts)[:4000]


_CONTEXT_FIELDS = (
    "business_id", "branch_id", "customer_id",
    "screen_context", "previous_messages",
    "is_first_message", "locale", "currency",
    # ai_access gates some agents' tools — without it they refuse
    # ("You don't have permission to access AI features"), so an eval that omits
    # it never exercises the agent. Suites set it in default_context (e.g.
    # {"all": true}); scenarios can override for permission-gating tests.
    "ai_access",
    # write actions bind only for a client that can render cards,
    # and a multi-turn scenario pins its turns to one session.
    "client_capabilities", "session_id",
    # Website-editing request fields (camelCase). An editor needs a full
    # website snapshot per request; suites carry it ONCE in default_context
    # (businessId + websiteDetails + catalog [+ themes/fonts]) so scenarios only
    # specify the message. Harmless for other agents (only merged when present).
    "businessId", "websiteDetails", "catalog", "themes", "fonts",
)


def _payload_for(
    scenario: Dict[str, Any],
    *,
    run_context: Optional[Dict[str, Any]] = None,
    request_shape: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Map a scenario to the endpoint's JSON body.

    Merge order (later wins):
      1. Synthesized defaults (message, session_id, locale=en)
      2. `run_context` — fields supplied on the trigger form
         (business_id, customer_id, etc.)
      3. Scenario-level context fields (same names as run_context)
      4. Scenario's explicit `payload` dict — fully replaces #1–3 when
         set, so authors can hard-code exact bodies (legacy E2E behavior)
    """
    if isinstance(scenario.get("payload"), dict):
        return scenario["payload"]
    if request_shape is not None:
        # The kind's own body (admin.services.test_chat — the same builder
        # the Test drawer and MCP `chat` use): only the fields that endpoint
        # takes. A case comes from the scenario, else the run / suite context.
        ctx = run_context or {}
        from admin.services.test_chat import build_body

        return build_body(
            request_shape,
            message=scenario.get("question") or scenario.get("input") or "",
            session_id=scenario.get("session_id") or ctx.get("session_id") or str(uuid.uuid4()),
            locale=scenario.get("locale") or ctx.get("locale") or "en",
            business_id=scenario.get("business_id") or ctx.get("business_id"),
            agent_kind=str(scenario.get("agent_kind") or ""),
            case_id=scenario.get("case_id") or ctx.get("case_id"),
            state=scenario.get("_state"),
        )
    body: Dict[str, Any] = {
        "message": scenario.get("question") or scenario.get("input") or "",
        "session_id": str(uuid.uuid4()),
        "locale": "en",
    }
    # Run-level overrides — least specific.
    from admin.eval_context import eval_context_keys

    fields = (*_CONTEXT_FIELDS, *eval_context_keys())
    if run_context:
        for opt in fields:
            if run_context.get(opt) is not None:
                body[opt] = run_context[opt]
    # Scenario-level overrides win over run-level.
    for opt in fields:
        if scenario.get(opt) is not None:
            body[opt] = scenario[opt]
    return body


def _judge_question(
    scenario: Dict[str, Any],
    question: str,
    *,
    run_context: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> str:
    """The question as the judge should see it: with the scenario's earlier
    chat turns when it carries any (`history`), since
    a reply like "Yes, 5pm works" can't be graded without them.

    A scenario with `judge_knows_today: true` also tells the judge today's
    date, so "tomorrow is 13 Oct" or "no slots before now" can be graded. The
    date is taken in the suite's `timezone` (default Asia/Dubai), not the
    host's UTC clock, which names the wrong day for the first hours of a GCC
    day — the agent itself reads the business's timezone the same way.
    """
    preamble: List[str] = []
    if scenario.get("judge_knows_today"):
        from zoneinfo import ZoneInfo

        tz_name = (run_context or {}).get("timezone") or "Asia/Dubai"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("Asia/Dubai")
        local = (now or datetime.now(tz)).astimezone(tz)
        tomorrow = local + timedelta(days=1)
        preamble = [
            f"Right now at the business it is {local:%A %d %B %Y, %H:%M} ({tz.key}); "
            f"tomorrow is {tomorrow:%A %d %B %Y}.",
            "",
        ]
    history = scenario.get("history")
    if scenario.get("turns"):
        # The answer graded is the whole transcript (a runner's run_turns).
        lines = [*preamble,
                 "This is a whole chat, played one customer message at a time. The "
                 "reply below is the full transcript: grade every Business reply in it, "
                 "and the chat as a whole."]
        if isinstance(history, list) and history:
            lines.append("Before it, earlier in this chat:")
            lines += [f"{'Customer' if t.get('role') == 'user' else 'Business'}: {t['content']}"
                      for t in history if isinstance(t, dict) and t.get("content")]
        return "\n".join(lines)
    if not isinstance(history, list) or not history:
        return "\n".join([*preamble, question]) if preamble else question
    lines = [*preamble, "Earlier in this chat:"]
    for turn in history:
        if isinstance(turn, dict) and turn.get("content"):
            who = "Customer" if turn.get("role") == "user" else "Business"
            lines.append(f"{who}: {turn['content']}")
    lines.append("")
    lines.append(f"Latest customer message: {question}")
    return "\n".join(lines)


def _is_job_cancelled(job_id: str) -> bool:
    """Indirection so tests can monkeypatch the cancel poll without
    needing a real Supabase. The real `is_cancelled` lives in
    `admin._jobs` and reads `admin_jobs.status`."""
    try:
        from admin._jobs import is_cancelled

        return is_cancelled(job_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("eval: is_cancelled poll failed (%s)", exc)
        return False


def _finalize(
    client,
    run_id: str,
    *,
    status: str,
    results: Optional[List[Dict[str, Any]]] = None,
    score: Optional[float] = None,
    error: Optional[str] = None,
    lmnr_trace_id: Optional[str] = None,
) -> None:
    """Write the terminal eval_runs row.

    Race-safety for cancellation: if the row is *already* status
    ='cancelled' (because the operator clicked Cancel between the time
    we entered the try/except and now), we don't overwrite it with
    'passed'/'failed'/'errored'. The exception is when WE are the ones
    writing 'cancelled' — that path is always allowed through. This
    keeps the operator's intent authoritative when the worker's natural
    finish races with the cancel flip.
    """
    if status != "cancelled":
        try:
            existing = (
                client.table("agent_eval_runs").select("status")
                .eq("run_id", run_id).limit(1).execute().data or []
            )
            if existing and existing[0].get("status") == "cancelled":
                return
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("eval: pre-finalize status read failed (%s)", exc)
    payload: Dict[str, Any] = {
        "status": status,
        "finished_at": _now_iso(),
    }
    if results is not None:
        payload["results"] = results
    if score is not None:
        payload["score"] = score
    if error is not None:
        payload["error"] = error
    if lmnr_trace_id is not None:
        payload["lmnr_trace_id"] = lmnr_trace_id
    client.table("agent_eval_runs").update(payload).eq("run_id", run_id).execute()


def _now_iso() -> str:
    import datetime as _dt

    return _dt.datetime.utcnow().isoformat() + "Z"
