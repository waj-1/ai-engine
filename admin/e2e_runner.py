"""E2E test runner with judge-model scoring.

Drives the dev → staging → prod test loop. For each scenario in the
profile's active eval suites the runner:

  1. POSTs the scenario payload at the profile's `endpoint_path`.
  2. (If the scenario carries an `expected_answer` and the suite has an
     `evaluator_model`) invokes the judge with `{question, expected,
     actual}` and reads back `{score: 0-1, rationale, pass}`.
  3. Falls back to a smoke heuristic (`HTTP 200 + non-empty body`) when
     no judge is configured or the scenario lacks an expected answer.

Per-scenario verdicts and aggregate score land in `agent_e2e_runs`.
The aggregate `score` is the mean of judge scores (or pass-rate for
smoke-only runs); the suite's `pass_threshold` (default 0.7) gates the
overall run.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import httpx

from core.auth import get_service_client, admin_service_client

logger = logging.getLogger(__name__)


# Default judge model. Cheap, fast, and good enough at numeric grading;
# the user can override per-suite via `agent_eval_suites.evaluator_model`.
DEFAULT_JUDGE_MODEL = "gemini-3.5-flash"

JUDGE_SYSTEM_PROMPT = """You are a strict but fair evaluator scoring an AI \
assistant's response against a reference answer.

You receive:
  - the original question / input
  - the reference answer (gold standard)
  - the candidate response

Score how well the candidate captures the *important* content of the \
reference on a 0.0–1.0 scale:
  1.0  — captures all key points; correct conclusion; safe phrasing
  0.85 — correct main point; missing one minor detail
  0.7  — correct direction but vague or incomplete; still useful
  0.5  — partially right; meaningful gaps or minor errors
  0.3  — mostly wrong, off-topic, or so hedged it gives no value
  0.0  — wrong, contradicts the reference, or fabricates data

If the candidate's reasoning / tool steps are provided, treat an answer that is
grounded in real tool results as correct even when its wording or exact numbers
differ from the reference (the reference may be illustrative, not authoritative).
Reward verified, tool-grounded answers; only penalize fabrication, wrong
conclusions, or unsafe phrasing.

Reply with a single JSON object, no markdown, no commentary:
  {"score": <float 0..1>, "pass": <bool>, "rationale": "<one-sentence why>"}
"""

RUBRIC_JUDGE_SYSTEM_PROMPT = """You are a strict but fair evaluator grading an AI \
assistant's response against a set of explicit criteria (a rubric).

You receive:
  - the original question / input
  - the reference answer (gold standard)
  - the candidate response
  - a list of criteria, each with an "id" and a "criterion" description

Grade EACH criterion independently using one of these verdicts:
  pass     — the criterion is clearly satisfied
  partial  — the criterion is partially satisfied (meaningful but incomplete)
  fail     — the criterion is clearly not satisfied
  unknown  — there is not enough evidence in the candidate response to judge
              (use this escape hatch rather than guessing)

For each criterion also provide:
  score    — a float 0.0–1.0 reflecting degree of satisfaction
              (1.0=pass, ~0.5=partial, 0.0=fail, 0.0=unknown)
  rationale — one concise sentence explaining your verdict

Grading guidance:
- Grade the *outcome*, not the wording. Credit answers that are grounded in
  real tool results even when worded differently from the reference; the
  reference may be illustrative, not authoritative.
- Reward verified, tool-grounded answers; only penalise fabrication, wrong
  conclusions, or unsafe phrasing.
- Return "unknown" when there is genuinely not enough information — do NOT guess.

Reply with a single JSON object, no markdown, no commentary:
{
  "criteria": [
    {"id": "<criterion id>", "verdict": "<pass|fail|partial|unknown>",
     "score": <float 0..1>, "rationale": "<one sentence>"},
    ...
  ],
  "overall_score": <float 0..1>,
  "overall": "<pass|fail|partial>",
  "rationale": "<one sentence summarising the overall verdict>"
}
"""


def run_e2e(
    *,
    profile_id: str,
    environment: str,
    triggered_by: Optional[str] = None,
    _job_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the active eval suite(s) for `profile_id` against its endpoint.

    Returns the run summary. Status is one of:
      passed    - score >= pass_threshold and every scenario judged pass
      failed    - score < pass_threshold or any scenario judged fail
      errored   - the run itself crashed (no endpoint, network, etc.)
      cancelled - operator hit Cancel on /admin/jobs mid-suite

    `_job_id` is the admin_jobs row id threaded by `start_tracked_job`
    when the worker opts into cooperative cancellation. Between
    scenarios we poll `admin._jobs.is_cancelled(_job_id)` and bail out
    cleanly. None for legacy callers — no cancel poll, original
    behavior preserved.
    """
    client = admin_service_client()
    profile_rows = (
        client.table("agent_profiles").select("*")
        .eq("profile_id", profile_id).limit(1).execute().data or []
    )
    if not profile_rows:
        return _record_errored(client, profile_id, environment, triggered_by, "profile not found")
    profile = profile_rows[0]
    endpoint = profile.get("endpoint_path")
    # Kinds with their own request body (the AI-agents demo needs `agent` + a
    # case) — the same builder the Test drawer and suite runs use.
    from admin.services.test_chat import own_body_shape

    kind_shape = own_body_shape(profile)
    if kind_shape is not None:
        if not kind_shape.get("supported"):
            return _record_errored(client, profile_id, environment, triggered_by,
                                   kind_shape.get("reason") or "this agent can't be tested here")
        endpoint = kind_shape["endpoint"]
    if not endpoint:
        return _record_errored(client, profile_id, environment, triggered_by,
                               "profile has no endpoint_path configured")

    suites = (
        client.table("agent_eval_suites").select("*")
        .eq("agent_kind", profile["agent_kind"]).eq("is_active", True)
        .execute().data or []
    )
    if profile.get("agent_kind") == "custom":
        # Every generic agent shares the one kind, so "the kind's suites"
        # would test this agent with another agent's cases. Only suites
        # bound to this agent count.
        suites = [s for s in suites if s.get("profile_id") == profile_id]
    if not suites:
        return _record_errored(client, profile_id, environment, triggered_by,
                               "no active eval suites for this agent_kind")

    base_url = _base_url_for(environment)
    suite_id = suites[0]["suite_id"]
    pass_threshold = float(suites[0].get("pass_threshold") or 0.70)
    evaluator_model = _resolve_evaluator_model_id(client, suites[0])
    # The exact agent_models row the suite picked (None on the default judge).
    judge_model_id = suites[0].get("evaluator_model_id") if evaluator_model else None
    evaluator_model = evaluator_model or DEFAULT_JUDGE_MODEL
    # Carry each scenario's OWN suite pass_threshold so a scenario from a
    # second active suite is graded against its suite's threshold, not
    # suites[0]'s. The run-level pass_threshold above stays as the summary
    # default recorded on the agent_e2e_runs row.
    scenarios: List[Tuple[Dict[str, Any], float]] = []
    for suite in suites:
        suite_threshold = float(suite.get("pass_threshold") or 0.70)
        for scn in (suite.get("scenarios") or []):
            scenarios.append((scn, suite_threshold))

    judge = _try_build_judge(evaluator_model, model_id=judge_model_id)

    run_id = str(uuid4())
    started = _iso_now()
    pass_count = 0
    fail_count = 0
    judge_scores: List[float] = []
    results: List[Dict[str, Any]] = []

    client.table("agent_e2e_runs").insert({
        "run_id": run_id,
        "profile_id": profile_id,
        "environment": environment,
        "suite_id": suite_id,
        "status": "running",
        "triggered_by": triggered_by,
        "started_at": started,
    }).execute()

    cancelled = False
    cancelled_at_idx = 0
    total_scenarios = len(scenarios)
    try:
        for idx, (scenario, scenario_threshold) in enumerate(scenarios):
            # Cooperative cancellation checkpoint between scenarios.
            # Only polls when the tracker threaded a `_job_id` to us;
            # legacy callers (no _job_id) keep the original behavior.
            if _job_id and _is_job_cancelled(_job_id):
                cancelled = True
                cancelled_at_idx = idx
                break
            outcome = _run_one_scenario(base_url, endpoint, scenario, judge, scenario_threshold,
                                        request_shape=kind_shape)
            results.append(outcome)
            judge_block = outcome.get("judge") or {}
            if "score" in judge_block:
                judge_scores.append(float(judge_block["score"]))
            if outcome["passed"]:
                pass_count += 1
            else:
                fail_count += 1
    except Exception as exc:
        return _record_errored(client, profile_id, environment, triggered_by,
                               f"runner crashed: {exc}", run_id=run_id, results=results)

    if cancelled:
        # Stamp 'cancelled' on the e2e row + record where we stopped so
        # the detail page can show "stopped at N/M".
        client.table("agent_e2e_runs").update({
            "status": "cancelled",
            "results": results,
            "pass_count": pass_count,
            "fail_count": fail_count,
            "error": (
                f"cancelled by operator at scenario "
                f"{cancelled_at_idx + 1}/{total_scenarios}"
            ),
            "finished_at": _iso_now(),
        }).eq("run_id", run_id).execute()
        return {
            "run_id": run_id, "status": "cancelled",
            "pass_count": pass_count, "fail_count": fail_count,
            "cancelled_at": cancelled_at_idx + 1,
            "total_scenarios": total_scenarios,
        }

    total = pass_count + fail_count
    if judge_scores:
        # Judge-mode: aggregate is mean of per-scenario judge scores.
        score = round(sum(judge_scores) / len(judge_scores), 3)
    else:
        # Smoke-only fallback: aggregate is pass rate.
        score = round(pass_count / max(total, 1), 3)

    status = "passed" if (
        total > 0 and fail_count == 0 and score >= pass_threshold
    ) else "failed"

    client.table("agent_e2e_runs").update({
        "status": status,
        "score": score,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "results": results,
        "finished_at": _iso_now(),
    }).eq("run_id", run_id).execute()

    return {
        "run_id": run_id, "status": status, "score": score,
        "pass_count": pass_count, "fail_count": fail_count,
        "judge_model": evaluator_model if judge else None,
        "pass_threshold": pass_threshold,
    }


def _run_one_scenario(
    base_url: str,
    endpoint: str,
    scenario: Dict[str, Any],
    judge: Optional[Any],
    pass_threshold: float,
    request_shape: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Hit the endpoint and (optionally) score the response with the judge."""
    url = f"{base_url.rstrip('/')}{endpoint}"
    payload = _payload_for(scenario)
    if request_shape is not None and "payload" not in scenario:
        import uuid

        from admin.services.test_chat import build_body

        payload = build_body(request_shape, message=payload.get("message") or "",
                             session_id=str(uuid.uuid4()), locale=payload.get("locale") or "en",
                             business_id=None, agent_kind="",
                             case_id=scenario.get("case_id"))
    question = scenario.get("question") or scenario.get("input") or ""
    expected = scenario.get("expected_answer") or scenario.get("expected") or ""

    start = time.monotonic()
    body_preview = ""
    http_status = 0
    body_full = ""
    try:
        with httpx.Client(timeout=30.0) as h:
            resp = h.post(url, json=payload, headers={"Authorization": "Bearer e2e-smoke"})
        http_status = resp.status_code
        body_full = resp.text or ""
        body_preview = body_full[:500]
    except Exception as exc:
        return {
            "id": scenario.get("id") or "unnamed",
            "question": question,
            "http_status": 0,
            "latency_ms": int((time.monotonic() - start) * 1000),
            "body_preview": str(exc)[:500],
            "passed": False,
            "judge": None,
        }

    latency_ms = int((time.monotonic() - start) * 1000)
    smoke_ok = 200 <= http_status < 300 and bool(body_preview.strip())

    # Judge-mode: only invoke when we have an expected answer AND the
    # endpoint responded with something to grade. Otherwise fall back to
    # the smoke verdict — a transport-level failure shouldn't burn judge
    # tokens.
    judge_result: Optional[Dict[str, Any]] = None
    if judge is not None and expected and smoke_ok:
        try:
            judge_result = _invoke_judge(judge, question, expected, body_full, pass_threshold)
        except Exception as exc:
            logger.warning("e2e: judge invocation failed (%s)", exc)
            judge_result = {"score": 0.5, "pass": False, "rationale": f"judge_error: {exc}"}

    if judge_result is not None:
        passed = bool(judge_result.get("pass"))
    elif expected:
        # The scenario declares an expected_answer that needs a judge, but none
        # ran (no evaluator model / GOOGLE_API_KEY). Don't pass an answer we
        # never graded — surface why instead of a bogus smoke "pass".
        passed = False
        judge_result = {
            "score": 0.0,
            "pass": False,
            "rationale": (
                "ungraded: expected_answer set but no evaluator model is "
                "configured / judge unavailable"
            ),
        }
    else:
        passed = smoke_ok

    return {
        "id": scenario.get("id") or scenario.get("name") or "unnamed",
        "question": question[:240],
        "http_status": http_status,
        "latency_ms": latency_ms,
        "body_preview": body_preview,
        "passed": passed,
        "judge": judge_result,
    }


def _resolve_evaluator_model_id(client: Any, suite: Dict[str, Any]) -> Optional[str]:
    """Resolve a suite's judge model NAME from ``evaluator_model_id`` (FK into
    ``agent_models``). Returns None when unset.

    The suite column is ``evaluator_model_id``; the runner used to read a
    non-existent ``evaluator_model`` key, so the operator's choice was silently
    ignored and every run used ``DEFAULT_JUDGE_MODEL``.
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
        logger.warning("e2e: evaluator model lookup failed (%s)", exc)
        return None
    # Use `model_string` (real provider model id), not the friendly `name`.
    return (rows[0].get("model_string") if rows else None) or None


def _try_build_azure_judge(model_string: str, model_id: Optional[str] = None) -> Optional[Any]:
    """A judge built from an azure_openai `agent_models` row, or None when
    there is no such row or it can't be built. With `model_id` (the suite's
    evaluator_model_id) that exact row is built: model_string is not unique
    (rows are unique on name + environment), so looking it up by model_string
    could take another row's endpoint or settings. Without one, the first
    active row whose model_string (the deployment name) matches. Temperature 0
    (dropped again by the row's omit_temperature for reasoning models) and
    room for the rubric verdict."""
    try:
        from core.auth import get_service_client
        from runtime.llm_factory import build_chat_model

        query = get_service_client().table("agent_models").select("*").eq("provider", "azure_openai")
        if model_id:
            query = query.eq("model_id", model_id)
        else:
            query = query.eq("model_string", model_string).eq("is_active", True)
        rows = query.limit(1).execute().data or []
        if not rows:
            return None
        return build_chat_model({**rows[0], "temperature": 0.0, "max_output_tokens": 4096})
    except Exception as exc:
        logger.warning("e2e: failed to build Azure judge %s (%s)", model_string, exc)
        return None


def _try_build_judge(model_string: str, model_id: Optional[str] = None) -> Optional[Any]:
    """Best-effort build of a LangChain judge LLM, PROVIDER-AWARE.

    Gemini model ids build a `ChatGoogleGenerativeAI` judge; Claude model ids
    (``claude*``) build a direct-Anthropic-API judge via the LLM factory.
    Previously this always built a Gemini client regardless of the id, so picking
    a Claude model as a suite's evaluator made every judge call throw and
    silently scored every scenario 0.0. Returns None when the judge can't be
    built (missing creds, etc.) so the runner falls back to smoke checks.
    `model_id` is the suite's chosen agent_models row; only the Azure branch
    needs it (see _try_build_azure_judge).
    """
    if not model_string:
        return None

    # --- Claude evaluators → direct-Anthropic-API judge -------------------
    # The Gemini SDK can't serve a `claude-*` id, so route it to the Anthropic
    # builder (single invoke, no tool loop).
    #
    # Was `_build_anthropic_vertex` until 2026-08-16. That builder needed GCP
    # Application Default Credentials, which do not exist on Azure compute, so
    # this branch had already stopped producing a judge — it raised, got caught
    # below, and every Claude-evaluator suite quietly downgraded to smoke-only.
    # `_build_anthropic` needs ANTHROPIC_API_KEY instead; unset → still
    # smoke-only, but now for a reason an operator can fix.
    #
    # "unset → smoke-only" is only true because `_build_anthropic` RAISES on an
    # empty key. It used to pass `api_key=os.getenv(..., "")` straight through,
    # and `''` is ChatAnthropic's own field default — so the builder returned a
    # perfectly-constructed judge that 401'd on every invoke. `_run_one_scenario`
    # catches that as {'score': 0.5, 'pass': False, 'rationale': 'judge_error:
    # …'}, i.e. a red run scored 0.5 after a ~30s failed API round-trip per
    # scenario, instead of the documented degrade. ANTHROPIC_API_KEY is bound
    # nowhere in this repo today, so that was the default outcome of the first
    # claude-evaluator suite anyone ran.
    if "claude" in model_string.lower():
        try:
            from runtime.llm_factory import _build_anthropic

            extra: Dict[str, Any] = {}
            # Opus 4.8+ deprecated `temperature` (the API 400s if it's sent).
            if "opus" in model_string.lower():
                extra["omit_temperature"] = True
            return _build_anthropic({
                "model_string": model_string,
                "temperature": 0.0,
                "max_output_tokens": 4096,
                "extra_config": extra,
            })
        except Exception as exc:
            logger.warning(
                "e2e: failed to build Claude judge %s (%s); smoke-only",
                model_string, exc,
            )
            return None

    # --- Azure OpenAI evaluators ---------------------------------------------
    # A stack whose only keys are Azure (no GOOGLE_API_KEY / ANTHROPIC_API_KEY)
    # had no judge at all. An Azure deployment is only reachable through its
    # agent_models row (endpoint, api_version, omit_temperature, …), so look
    # the row up by model_string and build it the way the runtime does.
    if "gemini" not in model_string.lower():
        azure_judge = _try_build_azure_judge(model_string, model_id)
        if azure_judge is not None:
            return azure_judge

    # --- Gemini evaluators (default) ---------------------------------------
    try:
        # Lazy import to keep langchain optional for callers that don't
        # need the judge path (e.g. the smoke tests).
        from langchain_google_genai import ChatGoogleGenerativeAI
    except Exception as exc:
        logger.warning("e2e: judge LLM unavailable (%s); falling back to smoke", exc)
        return None
    if not os.getenv("GOOGLE_API_KEY"):
        logger.info("e2e: GOOGLE_API_KEY unset; judge mode disabled, smoke-only")
        return None
    try:
        return ChatGoogleGenerativeAI(
            model=model_string,
            google_api_key=os.getenv("GOOGLE_API_KEY", ""),
            temperature=0.0,
            # 4096 (not 512): Gemini 3 "thinking" models spend output budget on
            # invisible reasoning, so a 512 cap truncated the multi-criteria
            # RUBRIC json to ~10 tokens (criteria came back empty). 4096 leaves
            # room for thinking + the full rubric verdict json.
            max_output_tokens=4096,
        )
    except Exception as exc:
        logger.warning("e2e: failed to build judge %s (%s); smoke-only", model_string, exc)
        return None


def _invoke_judge(
    judge: Any, question: str, expected: str, actual: str, pass_threshold: float,
    tool_trace: Optional[str] = None, expected_tools: Optional[List[str]] = None,
    rubric: Optional[List[Dict]] = None,
) -> Dict[str, Any]:
    """Call the judge and parse a verdict.

    Without `rubric` (back-compat): returns `{score, pass, rationale}`.

    With `rubric` (a list of ``{"id", "criterion", "weight"?}`` dicts):
      - Sends criteria to the judge with `RUBRIC_JUDGE_SYSTEM_PROMPT`.
      - Parses `{criteria:[{id,verdict,score,rationale}], overall_score, ...}`.
      - Computes `score` = weighted mean of criteria whose verdict != "unknown"
        (weights default to 1, or ``rubric[i]["weight"]`` when present).
      - Returns `{score, pass, rationale, criteria}`.

    In both modes score is clamped to [0, 1] and `pass` is the runner's
    decision (score >= pass_threshold), not the model's.

    `tool_trace` and `expected_tools`, when provided, are shown to the judge
    so it can credit answers grounded in real tool results.
    """
    from langchain_core.messages import HumanMessage, SystemMessage  # local import

    user = (
        f"Question:\n{question}\n\n"
        f"Reference answer:\n{expected}\n\n"
        f"Candidate response:\n{actual[:4000]}"
    )
    if tool_trace:
        user += (
            "\n\nAgent reasoning / tool steps (the candidate used these to ground "
            "its answer — credit facts/figures drawn from real tool results, even "
            "if worded differently from the reference):\n" + str(tool_trace)[:JUDGE_TRACE_CHARS]
        )
    if expected_tools:
        user += "\n\nTools expected for grounding: " + ", ".join(expected_tools)

    if rubric:
        # Build criteria block for the judge.
        criteria_lines = "\n".join(
            f'  {i + 1}. id="{c["id"]}" — {c["criterion"]}'
            for i, c in enumerate(rubric)
        )
        user += f"\n\nRubric criteria to grade:\n{criteria_lines}"

        raw = judge.invoke([
            SystemMessage(content=RUBRIC_JUDGE_SYSTEM_PROMPT),
            HumanMessage(content=user),
        ])
        text = _flatten_content(getattr(raw, "content", raw))
        parsed = _parse_judge_json(text)

        # Build a weight lookup from the rubric definition.
        weight_by_id: Dict[str, float] = {
            c["id"]: float(c.get("weight", 1)) for c in rubric
        }

        criteria_out: List[Dict[str, Any]] = []
        for item in parsed.get("criteria", []):
            crit_id = item.get("id", "")
            verdict = str(item.get("verdict", "unknown")).lower()
            crit_score = max(0.0, min(1.0, float(item.get("score", 0.0))))
            criteria_out.append({
                "id": crit_id,
                "verdict": verdict,
                "score": crit_score,
                "rationale": str(item.get("rationale", ""))[:300],
            })

        # Weighted mean excluding unknown criteria.
        total_weight = 0.0
        weighted_sum = 0.0
        for item in criteria_out:
            if item["verdict"] == "unknown":
                continue
            w = weight_by_id.get(item["id"], 1.0)
            weighted_sum += item["score"] * w
            total_weight += w

        score = (weighted_sum / total_weight) if total_weight > 0 else 0.0
        score = max(0.0, min(1.0, score))
        rationale = str(parsed.get("rationale", ""))[:600]
        return {
            "score": score,
            "pass": score >= pass_threshold,
            "rationale": rationale,
            "criteria": criteria_out,
        }

    # --- Back-compat single-score path ---
    raw = judge.invoke([
        SystemMessage(content=JUDGE_SYSTEM_PROMPT),
        HumanMessage(content=user),
    ])
    text = _flatten_content(getattr(raw, "content", raw))
    parsed = _parse_judge_json(text)
    score = max(0.0, min(1.0, float(parsed.get("score", 0.0))))
    rationale = str(parsed.get("rationale", ""))[:600]
    return {
        "score": score,
        "pass": score >= pass_threshold,
        "rationale": rationale,
    }


def _flatten_content(raw: Any) -> str:
    """Normalise LangChain message content (str | list[dict])."""
    if isinstance(raw, list):
        parts: List[str] = []
        for item in raw:
            if isinstance(item, dict):
                if item.get("type") == "thinking":
                    continue
                if "text" in item:
                    parts.append(str(item["text"]))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(raw) if raw is not None else ""


_JSON_BLOCK_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)

# How much of the tool trace the judge sees. At 3000 a grounded answer whose
# facts sat past the cut was graded as invented (opening hours, 2026-10-14); at
# 8000 a whole services list (up to 20000 since the catalog lookups slimmed)
# still didn't fit, and real services were graded as invented (2026-10-19).
JUDGE_TRACE_CHARS = 44000


def _parse_judge_json(text: str) -> Dict[str, Any]:
    """Pull the first JSON object out of the judge response.

    Models sometimes wrap the JSON in markdown fences or trailing text.
    Falls through to a `{score: 0.5}` default when nothing parses so the
    runner records the scenario as "uncertain" rather than crashing.
    """
    text = text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, (int, float)):
            # Bare number returned by a legacy judge prompt.
            return {"score": float(parsed), "rationale": ""}
    except Exception:
        pass
    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    # Last resort: pull a leading decimal so very-old judge prompts that
    # answered with just a number still work.
    num = re.search(r"\d+(?:\.\d+)?", text)
    if num:
        return {"score": float(num.group(0)), "rationale": text[:200]}
    return {"score": 0.5, "rationale": "judge response unparseable"}


def _payload_for(scenario: Dict[str, Any]) -> Dict[str, Any]:
    """Map a scenario row to the endpoint's expected JSON body."""
    if "payload" in scenario:
        return scenario["payload"]
    return {
        "message": scenario.get("question") or scenario.get("input") or "",
        "locale": scenario.get("locale", "en"),
    }


def _base_url_for(environment: str) -> str:
    """Resolve the base URL for `environment`.

    Lookup order:
      1. AI_AGENT_BASE_URL_<ENV> env var (CI / overrides)
      2. The default `agent_deployment_targets` row for that env
      3. AI_AGENT_BASE_URL env var (legacy)
      4. http://127.0.0.1:8000 (local fallback)
    """
    env_key = environment.upper()
    override = os.getenv(f"AI_AGENT_BASE_URL_{env_key}")
    if override:
        return override
    try:
        client = admin_service_client()
        rows = (
            client.table("agent_deployment_targets").select("base_url")
            .eq("environment", environment).eq("is_default", True).eq("is_active", True)
            .limit(1).execute().data or []
        )
        if rows and rows[0].get("base_url"):
            return rows[0]["base_url"]
    except Exception as exc:
        logger.warning("e2e: deployment_target lookup failed (%s)", exc)
    return os.getenv("AI_AGENT_BASE_URL") or f"http://127.0.0.1:{os.getenv('PORT') or 8000}"


def _record_errored(
    client, profile_id: str, environment: str, triggered_by: Optional[str],
    message: str, *, run_id: Optional[str] = None, results: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    payload = {
        "profile_id": profile_id,
        "environment": environment,
        "status": "errored",
        "error": message,
        "triggered_by": triggered_by,
        "results": results or [],
        "finished_at": _iso_now(),
    }
    if run_id:
        client.table("agent_e2e_runs").update(payload).eq("run_id", run_id).execute()
        return {"run_id": run_id, "status": "errored", "error": message}
    inserted = client.table("agent_e2e_runs").insert(payload).execute()
    new_id = inserted.data[0]["run_id"] if inserted.data else None
    return {"run_id": new_id, "status": "errored", "error": message}


def _is_job_cancelled(job_id: str) -> bool:
    """Indirection so tests can monkeypatch the cancel poll without
    needing a real Supabase. The real `is_cancelled` lives in
    `admin._jobs` and reads `admin_jobs.status`."""
    try:
        from admin._jobs import is_cancelled

        return is_cancelled(job_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("e2e: is_cancelled poll failed (%s)", exc)
        return False


def _iso_now() -> str:
    import datetime as _dt
    return _dt.datetime.utcnow().isoformat() + "Z"
