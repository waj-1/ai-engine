"""Test-domain MCP tools: eval runs/suites/schedules, pytest, e2e."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import Field

from admin_mcp._common import (READ_ONLY, Env, actor_audit, client_for_env,
                               err, get_row, list_rows, not_confirmed, rows,
                               tool_errors, use_env, validate_env)
from admin_mcp.context import current_actor
from admin_mcp.server import mcp
from admin.services.eval_runners import runner_kinds as _runner_kinds

# The runner kinds a suite may name: the engine's chat and voice_live plus a
# plugin's own (admin/services/eval_runners.py). A Literal, so the MCP schema
# lists them.
RunnerKind = Literal[("",) + _runner_kinds()]  # type: ignore[valid-type]


@mcp.tool()
@tool_errors
def trigger_eval_run(profile_id: str,
                     source: Literal["suite", "live_chats"] = "suite",
                     suite_id: str = "", business_id: str = "",
                     model_id: str = "", prompt_overrides: Optional[Dict[str, int]] = None,
                     temperature: Optional[float] = None,
                     max_output_tokens: Optional[int] = None,
                     top_p: Optional[float] = None,
                     thinking_level: Literal["", "minimal", "low", "medium",
                                             "high"] = "",
                     thinking_budget: Optional[int] = None,
                     variant_label: str = "", repeats: int = 1,
                     concurrency: int = 0,
                     profile_version_id: str = "",
                     voice_mode: Literal["", "sim", "live"] = "",
                     agent_kind: str = "", lookback_days: int = 30,
                     sample_size: int = 10, judge_model: str = "",
                     expectations: str = "", env: Env = "staging") -> Dict[str, Any]:
    """Start an eval run in the background and return its run_id at once.
    Required: profile_id. Poll with eval_runs(action="get").

    source="suite" (default) grades the scenarios of suite_id (required).
    Options:
    - model_id, temperature, max_output_tokens, top_p, thinking_level,
      thinking_budget, variant_label: run a variant, to compare models or
      settings.
    - prompt_overrides: map a prompt to the version NUMBER to test instead
      of the pinned one, e.g. {"system": 3} for the agent's own instructions
      or {"<agent_kind>/<prompt slug>": 3}. An entry that names no such
      prompt or version is refused, never ignored.
    - repeats: run each scenario 1-20 times.
    - concurrency: 1-8 scenario attempts at once. 0 uses the suite's
      default_context.concurrency, then EVAL_CONCURRENCY, then 4. voice_live
      suites always run one at a time.
    - profile_version_id: pin the run to one saved profile version. It must
      belong to profile_id, and the suite needs an evaluator model.
    - voice_mode: sim or live, for a suite with runner_kind=voice_live.
      Blank uses the suite default, which is sim unless the suite says
      otherwise. live places a REAL, billable provider call. Only a direct
      call of this tool may request live; schedules and bake-offs may not.

    source="live_chats" grades a sample of recent real chats. agent_kind,
    judge_model and expectations are required. lookback_days and sample_size
    (1-100) set the sample."""
    from admin.services.evals import start_live_chat_run, start_suite_run
    actor = current_actor()
    validate_env(env)
    if source not in ("suite", "live_chats"):
        return err("source must be suite | live_chats")
    with use_env(env):
        if source == "suite":
            if not suite_id:
                return err("suite_id is required for a suite run")
            gen_config: Dict[str, Any] = {}
            if temperature is not None:
                gen_config["temperature"] = temperature
            if max_output_tokens is not None:
                gen_config["max_output_tokens"] = max_output_tokens
            if top_p is not None:
                gen_config["top_p"] = top_p
            if thinking_level in ("minimal", "low", "medium", "high"):
                gen_config["thinking_level"] = thinking_level
            if thinking_budget is not None:
                gen_config["thinking_budget"] = thinking_budget
            try:
                run_id = start_suite_run(
                    suite_id=suite_id, profile_id=profile_id,
                    business_id=business_id or None,
                    actor_user_id=actor["user_id"],
                    user_token=actor["user_token"],
                    model_id=model_id or None,
                    prompt_overrides=prompt_overrides,
                    gen_config=gen_config or None,
                    variant_label=variant_label or None,
                    repeats=max(1, min(20, repeats)),
                    concurrency=concurrency if concurrency and concurrency > 0 else None,
                    profile_version_id=profile_version_id or None,
                    voice_mode=voice_mode or None,
                )
            except ValueError as exc:
                return err(str(exc))
        else:
            if not agent_kind:
                return err("agent_kind is required for live_chats")
            from admin.eval_runs_handlers import _eval_agent_kinds
            if agent_kind not in _eval_agent_kinds():
                return err(f"agent_kind must be one of {list(_eval_agent_kinds())}")
            if not judge_model:
                return err("judge_model is required for live_chats")
            if not expectations:
                return err("expectations are required for live_chats")
            run_id = start_live_chat_run(
                profile_id=profile_id, agent_kind=agent_kind,
                business_id=business_id or None,
                lookback_days=lookback_days,
                sample_size=max(1, min(100, sample_size)),
                judge_model=judge_model, expectations=expectations,
                actor_user_id=actor["user_id"], user_token=actor["user_token"],
            )
    actor_audit("agent_eval_runs", run_id, f"trigger_{source}",
                after={"source": source, "profile_id": profile_id})
    return {"run_id": run_id, "status": "queued",
            "hint": "poll eval_runs action=get run_id=..."}


# _result_chip's labels → the plain words an agent iterating on a prompt
# reads: did this scenario hold, get fixed, break, or stay broken?
_COMPARE_LABELS = {"held_pass": "held", "gained": "gained",
                   "regressed": "regressed", "held_fail": "still_failing",
                   "only_a": "dropped", "only_b": "new"}


def _scenario_brief(r: Dict[str, Any]) -> Dict[str, Any]:
    judge = r.get("judge") or {}
    return {"scenario_id": r.get("scenario_id") or r.get("id") or r.get("name"),
            "passed": r.get("passed"), "score": r.get("score"),
            "judge_score": judge.get("score") if isinstance(judge, dict) else None,
            **({"tool_errors": r["tool_errors"]} if r.get("tool_errors") else {}),
            **({"error": r["error"]} if r.get("error") else {}),
            # The agent's own trace(s) for this attempt — read them with the
            # traces tool to see its messages, tool calls and output.
            **({"trace_ids": r["trace_ids"]} if r.get("trace_ids") else {})}


def _attach_trace_ids(run: Dict[str, Any]) -> None:
    """Resolve each result's `eval_ref` to the agent trace(s) it produced
    (admin.screens.observability.eval_trace_ids), in place. Best-effort."""
    results = run.get("results") or []
    if not any(isinstance(r, dict) and r.get("eval_ref") for r in results):
        return
    try:
        from admin.screens.observability import eval_trace_ids

        by_ref = eval_trace_ids(run.get("run_id") or "",
                                run.get("started_at") or run.get("created_at"))
    except Exception:  # noqa: BLE001
        by_ref = None
    if not by_ref:
        return
    for r in results:
        if isinstance(r, dict) and r.get("eval_ref"):
            r["trace_ids"] = by_ref.get(str(r["eval_ref"]).lower(), [])


def _run_summary(run: Dict[str, Any], include_results: bool) -> Dict[str, Any]:
    """A run row with its per-scenario `results` collapsed to counts + the
    failing scenario ids (full bodies only with include_results=true)."""
    results = run.get("results") or []
    out = {k: v for k, v in run.items() if k != "results"}
    passed = sum(1 for r in results if r.get("passed"))
    out["results_summary"] = {
        "scenarios": len(results), "passed": passed,
        "failed": len(results) - passed,
        "pass_rate": round(passed / len(results), 3) if results else None,
        "failing": [_scenario_brief(r) for r in results if not r.get("passed")][:50],
        # Tool calls whose RESULT was error-shaped (admin/evals.py) — a
        # passing scenario can still have them. None = run predates it.
        "tool_errors": (sum(int(r.get("tool_error_count") or 0) for r in results)
                        if any("tool_error_count" in r for r in results) else None),
        "with_tool_errors": [
            {"scenario_id": r.get("scenario_id") or r.get("name"),
             **({"attempt": r["attempt"]} if r.get("attempt") is not None else {}),
             "passed": r.get("passed"), "tool_errors": r.get("tool_errors")}
            for r in results if r.get("tool_errors")][:50],
    }
    if include_results:
        out["results"] = results
    return out


def _compare_n(ordered: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Per-scenario labels across N runs (baseline = the first): each later
    run is classified against the baseline with the portal's own
    eval_handlers._result_chip, and the headline label is baseline → LAST
    run. Plus the portal's N-column metric matrix (_compare_runs_n)."""
    from admin.eval_handlers import (_compare_runs_n, _result_chip,
                                     _scenario_id_for_result)
    by_run: List[Dict[str, Dict[str, Any]]] = []
    order: List[str] = []
    for run in ordered:
        m: Dict[str, Dict[str, Any]] = {}
        for idx, res in enumerate(run.get("results") or []):
            sid = _scenario_id_for_result(res, idx)
            m[sid] = res
            if sid not in order:
                order.append(sid)
        by_run.append(m)
    base, last = by_run[0], by_run[-1]
    scenarios = []
    counts = {label: 0 for label in _COMPARE_LABELS.values()}
    for sid in order:
        label = _COMPARE_LABELS[_result_chip(base.get(sid), last.get(sid))]
        counts[label] += 1
        scenarios.append({
            "scenario_id": sid, "label": label,
            "passed": [(m.get(sid) or {}).get("passed") if sid in m else None
                       for m in by_run],
            "score": [(m.get(sid) or {}).get("score") for m in by_run],
            "vs_baseline": [None] + [
                _COMPARE_LABELS[_result_chip(base.get(sid), m.get(sid))]
                for m in by_run[1:]],
        })
    rank = {"regressed": 0, "gained": 1, "still_failing": 2, "new": 3,
            "dropped": 4, "held": 5}
    scenarios.sort(key=lambda x: rank.get(x["label"], 9))
    return {"labels": counts, "scenarios": scenarios,
            "matrix": _compare_runs_n(ordered)}


def _voice_public(run: Dict[str, Any]) -> Dict[str, Any]:
    """An eval-shaped voice run (admin/voice_checks.as_eval_run) without the
    portal-only underscore fields; keeps the check's display name."""
    out = {k: v for k, v in run.items() if not k.startswith("_")}
    out["check_name"] = run.get("_check_name")
    return out


def _overview_card(card: Dict[str, Any]) -> Dict[str, Any]:
    """A Testing Overview card, JSON-safe: verdict + one row per text suite /
    the agent's voice checks (kind="voice")."""
    def _row(x: Dict[str, Any]) -> Dict[str, Any]:
        latest = x.get("latest") or {}
        return {"kind": x.get("kind") or "text",
                "suite_id": (x.get("suite") or {}).get("suite_id"),
                "name": (x.get("suite") or {}).get("name"),
                "score": latest.get("score"), "target": x.get("target"),
                "verdict": x.get("verdict"), "delta": x.get("delta"),
                "failing": x.get("failing"),
                "latest_run_id": latest.get("run_id"),
                "finished_at": latest.get("finished_at") or latest.get("created_at"),
                **({"checks": [{"slug": c.get("slug"), "name": c.get("name"),
                                "status": (c.get("latest") or {}).get("status"),
                                "run_id": (c.get("latest") or {}).get("run_id")}
                               for c in x.get("checks") or []]}
                   if x.get("kind") == "voice" and x.get("checks") is not None else {})}
    return {"profile_id": card.get("profile_id"), "name": card.get("name"),
            "agent_kind": card.get("agent_kind"), "verdict": card.get("verdict"),
            "suites": [_row(x) for x in card.get("suites") or []]}


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def eval_runs(action: Literal["list", "get", "report", "compare",
                              "matrix", "overview"] = "list",
              run_id: str = "", run_b: str = "",
              suite_id: str = "", profile_id: str = "", days: int = 30,
              limit: int = 25, run_ids: Optional[List[str]] = None,
              include_results: Annotated[bool, Field(
                  description="get/compare: include every scenario's full "
                              "result body (default: counts + failing "
                              "scenario ids only).")] = False,
              kind: Annotated[Literal["all", "text", "voice"], Field(
                  description="list: text eval runs, voice-check runs "
                              "(suite_id 'voice:<slug>'), or both "
                              "(default).")] = "all",
              env: Env = "staging") -> Dict[str, Any]:
    """Read eval run results.

    Actions:
    - list (default): recent runs. Filter with suite_id, profile_id and
      kind (all, text or voice). Rows include profile_id, schedule_id,
      bakeoff_id and profile_version_id, which show what started the run.
    - get (run_id): status, score, the same links, and results_summary
      {scenarios, passed, failed, pass_rate, failing[]}. Each failing case
      has trace_ids; read them with the traces tool. include_results=true
      adds every scenario's full result.
    - report (suite_id): pass-rate trend over the last `days`.
    - compare (run_ids=[baseline, ..., latest], 2 or more; run_id with
      run_b also works for a pair): labels every scenario held, gained,
      regressed or still_failing (new or dropped when it is missing on one
      side), from the baseline to the latest run. Includes a per-run
      vs_baseline trail and label counts; regressions sort first. Also
      returns runs (summaries), matrix (pass rate, latency and cost per run)
      and, for a pair, a and b.
    - matrix (run_ids or suite_id): per suite and variant, and per variant:
      mean score, pass rate, p50/p95 latency and time to first token, tokens
      and cost.
    - overview: one card per agent with its verdict, a row per text suite
      and one kind="voice" row for its voice checks (score = share of checks
      whose latest call passed; target 1.0).

    Voice checks are runs too. list, get and compare accept voice-check run
    ids, and every row has kind (text or voice). A voice run's results are
    its checked call steps. compare refuses a mix of text and voice runs."""
    from admin.voice_checks import (is_voice_suite_id, load_voice_runs,
                                    load_voice_runs_by_ids, slug_of)
    client = client_for_env(env)
    if action == "overview":
        from admin.eval_runs_handlers import build_testing_overview_context
        with use_env(env):
            ctx = build_testing_overview_context()
        sm = dict(ctx.get("summary") or {})
        sm.pop("last_run", None)
        return {"summary": sm, "agents": [_overview_card(c) for c in ctx.get("cards") or []]}
    if action == "list":
        runs: List[Dict[str, Any]] = []
        if kind != "voice" and not is_voice_suite_id(suite_id):
            runs = [{**r, "kind": "text"} for r in list_rows(
                client, "agent_eval_runs",
                select="run_id, suite_id, profile_id, schedule_id, "
                       "bakeoff_id, profile_version_id, status, score, "
                       "context, started_at, finished_at, error, created_at",
                filters={"suite_id": suite_id or None,
                         "profile_id": profile_id or None},
                limit=limit)]
        if kind != "text" and (not suite_id or is_voice_suite_id(suite_id)):
            with use_env(env):
                voice = load_voice_runs(client, limit=limit,
                                        slug=slug_of(suite_id) or None, env=env)
            vr = [_voice_public(r) for r in voice["runs"]
                  if not profile_id or r.get("profile_id") == profile_id]
            for r in vr:
                r.pop("results", None)
            runs = sorted(runs + vr, key=lambda r: str(r.get("created_at") or ""),
                          reverse=True)[:limit]
        return {"runs": runs}
    if action == "get":
        # get_row selects "*" already, so schedule_id/bakeoff_id/
        # profile_version_id ride along with every other column.
        run = get_row(client, "agent_eval_runs", "run_id", run_id)
        if run:
            _attach_trace_ids(run)
            return {"run": {**_run_summary(run, include_results), "kind": "text"}}
        with use_env(env):
            voice = load_voice_runs_by_ids(client, [run_id])
        if voice:
            return {"run": _run_summary(_voice_public(voice[0]), include_results)}
        return err("run not found")
    if action == "report":
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
        query = (client.table("agent_eval_runs")
                .select("run_id, created_at, score, status, suite_id")
                .gte("created_at", cutoff))
        if suite_id:
            query = query.eq("suite_id", suite_id)
        query = query.order("created_at", desc=False).limit(200)
        runs = rows(query.execute())
        suite = get_row(client, "agent_eval_suites", "suite_id", suite_id) \
            if suite_id else None
        return {"points": runs,
                "pass_threshold": (suite or {}).get("pass_threshold", 0.7)}
    if action == "compare":
        ids: List[str] = []
        for rid in (run_ids or [r for r in (run_id, run_b) if r]):
            if rid and rid not in ids:
                ids.append(rid)
        if len(ids) < 2:
            return err("compare needs at least two runs: run_ids=[baseline, "
                       "..., latest] (or run_id + run_b)")
        found = rows(client.table("agent_eval_runs").select("*")
                     .in_("run_id", ids).execute())
        by_id = {r["run_id"]: {**r, "kind": "text"} for r in found}
        missing = [rid for rid in ids if rid not in by_id]
        if missing:
            with use_env(env):
                voice = load_voice_runs_by_ids(client, missing)
            by_id.update({r["run_id"]: _voice_public(r) for r in voice})
            missing = [rid for rid in ids if rid not in by_id]
        if missing:
            return err(f"run(s) not found: {missing}")
        ordered = [by_id[rid] for rid in ids]
        if len({r.get("kind") for r in ordered}) > 1:
            return err("can't compare voice-check runs with text eval runs — "
                       "a call's steps and a suite's questions don't line up; "
                       "pass runs of one kind")
        out = {"run_ids": ids, "baseline_run_id": ids[0], "latest_run_id": ids[-1],
               **_compare_n(ordered),
               "runs": [_run_summary(r, include_results) for r in ordered]}
        if len(ordered) == 2:
            out["a"], out["b"] = out["runs"]
        return out
    if action == "matrix":
        from admin.services.eval_matrix import build_variant_matrix
        select = ("run_id, suite_id, profile_id, status, score, context, results")
        if run_ids:
            fetched = rows(client.table("agent_eval_runs").select(select)
                           .in_("run_id", run_ids).execute())
        elif suite_id:
            fetched = rows(client.table("agent_eval_runs").select(select)
                           .eq("suite_id", suite_id)
                           .order("created_at", desc=True).limit(limit).execute())
        else:
            return err("matrix needs run_ids or suite_id")
        thr = 0.7
        if suite_id:
            s = get_row(client, "agent_eval_suites", "suite_id", suite_id)
            thr = (s or {}).get("pass_threshold", 0.7)
        return {"matrix": build_variant_matrix(fetched, pass_threshold=thr),
                "run_ids": [r.get("run_id") for r in fetched]}
    return err(f"unknown action {action!r}",
               hint="list | get | report | compare | matrix | overview")


@mcp.tool()
@tool_errors
def bakeoffs(action: Literal["create", "list", "get"] = "list",
            name: str = "", profile_id: str = "",
            variants: Optional[List[Dict[str, Any]]] = None,
            suite_ids: Optional[List[str]] = None, repeats: int = 1,
            bakeoff_id: str = "", suite_id: str = "", limit: int = 25,
            env: Env = "staging") -> Dict[str, Any]:
    """Bake-offs: compare several variants of one profile across one or more
    eval suites. Creating one starts an eval run for every variant and suite.
    Results come back as a matrix in the same shape as
    eval_runs(action="matrix").

    Actions:
    - create (name, profile_id, variants, suite_ids; optional repeats, 1-20
      per variant and suite): variants is a list of 2 or more {label?,
      profile_version_id?, model_id?, gen_config?}. gen_config keys:
      temperature, thinking_level, max_output_tokens, top_p. A blank or
      duplicate label is renamed ("variant N", or a " (2)" suffix). Every
      suite needs an evaluator model, and each profile_version_id must
      belong to profile_id. Both are checked before anything is written.
      Returns {bakeoff_id, run_ids}.
    - list (default): recent bake-offs with n_variants and n_suites. Does
      not load their runs.
    - get (bakeoff_id): {bakeoff, matrix, status}. status is complete when
      every run has finished, running otherwise, or errored when creation
      failed. suite_id limits the matrix to that suite ({rows}); without it
      the matrix is {rows, by_variant}."""
    # Maintainer notes (kept out of the description clients read):
    # Not annotated READ_ONLY: create writes, list/get read (same as
    # eval_suites and eval_schedules). Rows live in agent_bakeoffs; each
    # (variant, suite) run is an agent_eval_runs row started through the same
    # start_suite_run that trigger_eval_run uses. create_bakeoff clamps repeats
    # itself. 'errored' is sticky: set when create's pre-flight or spawn loop
    # failed.
    from admin.services.bakeoffs import create_bakeoff, get_bakeoff, list_bakeoffs
    if action == "create":
        if not name:
            return err("name is required")
        if not profile_id:
            return err("profile_id is required")
        if not variants:
            return err("variants is required (list of >=2 variants)")
        if not suite_ids:
            return err("suite_ids is required (non-empty list)")
        actor = current_actor()
        with use_env(env):
            result = create_bakeoff(
                name=name, profile_id=profile_id, variants=variants,
                suite_ids=suite_ids, repeats=repeats,
                actor_user_id=actor["user_id"], user_token=actor["user_token"],
            )
        actor_audit("agent_bakeoffs", result.get("bakeoff_id"), "create",
                    after={"name": name, "n_variants": len(variants),
                           "n_suites": len(suite_ids)})
        return result
    if action == "list":
        with use_env(env):
            return {"bakeoffs": list_bakeoffs(limit=limit)}
    if action == "get":
        with use_env(env):
            return get_bakeoff(bakeoff_id, suite_focus=suite_id or None)
    return err(f"unknown action {action!r}", hint="create | list | get")


def _clean_scenarios(scenarios: List[Any]) -> tuple:
    """Check a caller's scenarios and fill in a missing id: ``(list, None)``
    or ``([], reason)``. Shared by create and import_scenarios."""
    from admin.eval_handlers import _slugify_question_first_words
    cleaned: List[Dict[str, Any]] = []
    for sc in scenarios:
        if not isinstance(sc, dict):
            return [], "each scenario must be a JSON object"
        question = str(sc.get("question") or "").strip()
        if not question:
            return [], "every scenario needs a question"
        sc = dict(sc)
        sc["question"] = question
        name = str(sc.get("name") or "").strip()
        if name:
            sc["name"] = name  # what the portal calls the case
        else:
            sc.pop("name", None)
        if not sc.get("id"):
            sc["id"] = _slugify_question_first_words(name or question)
        cleaned.append(sc)
    return cleaned, None


@mcp.tool()
@tool_errors
def eval_suites(action: Literal["list", "get", "create", "update",
                                "import_scenarios", "sync_from_repo"],
                suite_id: str = "", env: Env = "staging",
                name: str = "", agent_kind: str = "",
                profile_id: str = "",
                runner_kind: RunnerKind = "",
                pass_threshold: Optional[float] = None,
                is_active: Optional[bool] = None,
                evaluator_model_id: Optional[str] = None,
                scenarios: Optional[List[Dict[str, Any]]] = None,
                default_context: Optional[Dict[str, Any]] = None,
                clear_profile: bool = False,
                confirm: bool = False) -> Dict[str, Any]:
    """Eval suites: sets of test scenarios for an agent. Required: action.

    Actions:
    - list.
    - get (suite_id).
    - create (name, and profile_id or agent_kind): with profile_id the agent
      kind is taken from that profile, overriding any agent_kind passed, and
      an unknown profile is an error. Without it, agent_kind must be one of
      this build's suite kinds. Defaults: runner_kind chat (voice_live grades
      a whole voice conversation; any other kind is a plugin's own runner),
      pass_threshold 0.7 (0 to 1), is_active false. Optional:
      evaluator_model_id, default_context and scenarios (each needs a
      question).
    - update (suite_id plus fields): only the fields you pass change. name
      renames the suite. profile_id rebinds the suite and resets agent_kind
      from that profile. clear_profile=true unbinds the profile and leaves
      agent_kind as it is; a profile_id in the same call wins. runner_kind
      must be one of this build's runner kinds. scenarios REPLACES the whole
      list.
    - import_scenarios (suite_id, scenarios): APPEND scenarios. Each needs a
      question. An optional name is what the portal shows for the case. A
      missing id is made from the name, or else the question.
    - sync_from_repo (name): reset an existing suite from the suite
      definition of the same name that ships with this build. name is the
      suite's name, not a suite_id. Without confirm it returns a diff:
      scenario ids added, removed and changed, and whether runner_kind,
      default_context or pass_threshold differ. With confirm=true it
      overwrites the suite's scenarios, runner_kind, default_context and
      pass_threshold. Errors if no suite of that name exists yet, or the
      build has no definition with that name.

    default_context is stored as given. The runner reads from it: voice
    settings (e.g. {"voice": {"mode": "sim"}}), concurrency (1-8 scenario
    attempts at once, default 4; use 1 for scenarios that must not overlap)
    and fail_on_tool_error (true fails a scenario on any tool error)."""
    # Maintainer notes (kept out of the description clients read):
    # Table agent_eval_suites. A bound profile is the single source of truth
    # for agent_kind (_sync_agent_kind). create/update mirror the portal's suite
    # dialog, including the "none" Profile option that clear_profile maps to.
    # Voice suites: docs/admin-portal/03-test.md (runner_kind voice_live).
    # sync_from_repo reads admin.seed_eval_suites._SUITES (a plugin's specs):
    # seeders are insert-if-missing, so a repo edit to an already-seeded suite
    # never reaches a live database on its own.
    from admin.eval_handlers import (RUNNER_KINDS, _AGENT_KINDS_FOR_SUITES, _sync_agent_kind,
                                     _slugify_question_first_words)
    client = client_for_env(env)
    if action == "list":
        return {"suites": list_rows(client, "agent_eval_suites", limit=200)}
    if action == "get":
        row = get_row(client, "agent_eval_suites", "suite_id", suite_id)
        return {"suite": row} if row else err("suite not found")
    if action == "create":
        if not name.strip():
            return err("name is required")
        if profile_id:
            derived_kind = _sync_agent_kind(client, profile_id)
            if not derived_kind:
                return err(f"unknown profile_id {profile_id!r}")
            resolved_kind = derived_kind
        else:
            if agent_kind not in _AGENT_KINDS_FOR_SUITES:
                return err(f"agent_kind must be one of {list(_AGENT_KINDS_FOR_SUITES)}")
            resolved_kind = agent_kind
        if runner_kind and runner_kind not in RUNNER_KINDS:
            return err(f"runner_kind must be one of {list(RUNNER_KINDS)}")
        threshold = 0.7 if pass_threshold is None else pass_threshold
        if not (0.0 <= threshold <= 1.0):
            return err("pass_threshold must be between 0 and 1")
        # Scenarios passed with create used to be dropped without a word: the
        # suite was made empty and its first run errored "suite has no
        # scenarios" (2026-11-22). They are checked and stored like an import.
        first_scenarios, why = _clean_scenarios(scenarios or [])
        if why:
            return err(why)
        payload: Dict[str, Any] = {
            "name": name.strip(), "agent_kind": resolved_kind,
            "scenarios": first_scenarios, "pass_threshold": threshold,
            "is_active": bool(is_active),
            "runner_kind": runner_kind or "chat",
        }
        if profile_id:
            payload["profile_id"] = profile_id
        if evaluator_model_id:
            payload["evaluator_model_id"] = evaluator_model_id
        if default_context is not None:
            payload["default_context"] = default_context
        inserted = rows(client.table("agent_eval_suites").insert(payload).execute())
        created = inserted[0] if inserted else payload
        actor_audit("agent_eval_suites", created.get("suite_id", ""), "create",
                    after=created)
        return {"created": True, "suite": created}
    if action == "update":
        before = get_row(client, "agent_eval_suites", "suite_id", suite_id)
        if not before:
            return err("suite not found")
        payload = {}
        if profile_id:
            derived_kind = _sync_agent_kind(client, profile_id)
            if not derived_kind:
                return err(f"unknown profile_id {profile_id!r}")
            payload["profile_id"] = profile_id
            payload["agent_kind"] = derived_kind
        elif clear_profile:
            # Unbind: NULL the profile_id, leave agent_kind untouched — the
            # suite reverts to manually-managed, matching the portal edit
            # form's "— none —" Profile option (see eval_handlers.py's
            # `raw_profile_id_field` sentinel handling). Only reachable when
            # profile_id itself is empty, so a real profile_id always wins.
            payload["profile_id"] = None
        if name.strip():
            payload["name"] = name.strip()
        if runner_kind:
            if runner_kind not in RUNNER_KINDS:
                return err(f"runner_kind must be one of {list(RUNNER_KINDS)}")
            payload["runner_kind"] = runner_kind
        if scenarios is not None:
            payload["scenarios"] = scenarios
        if pass_threshold is not None:
            if not (0.0 <= pass_threshold <= 1.0):
                return err("pass_threshold must be between 0 and 1")
            payload["pass_threshold"] = pass_threshold
        if is_active is not None:
            payload["is_active"] = is_active
        if evaluator_model_id is not None:
            payload["evaluator_model_id"] = evaluator_model_id or None
        if default_context is not None:
            payload["default_context"] = default_context
        if not payload:
            return err("no fields supplied")
        client.table("agent_eval_suites").update(payload).eq("suite_id", suite_id).execute()
        actor_audit("agent_eval_suites", suite_id, "update",
                    before=before, after={**before, **payload})
        return {"updated": True, "suite_id": suite_id, "changed": sorted(payload)}
    if action == "import_scenarios":
        before = get_row(client, "agent_eval_suites", "suite_id", suite_id)
        if not before:
            return err("suite not found")
        if not scenarios:
            return err("scenarios is required (non-empty list)")
        appended, why = _clean_scenarios(scenarios)
        if why:
            return err(why)
        combined = list(before.get("scenarios") or []) + appended
        client.table("agent_eval_suites").update({"scenarios": combined}) \
            .eq("suite_id", suite_id).execute()
        actor_audit("agent_eval_suites", suite_id, "import_scenarios",
                    after={"count": len(appended)})
        return {"imported": True, "count": len(appended), "total": len(combined)}
    if action == "sync_from_repo":
        if not name.strip():
            return err("name is required — the suite's `name` field in "
                       "admin.seed_eval_suites._SUITES, not a suite_id")
        from core.deployment import active_scope
        from admin.seed_eval_suites import (_SUITES, _localize_voice_ws_path,
                                            _resolve_scenarios)
        entry = next((s for s in _SUITES if s.get("name") == name), None)
        if entry is None:
            return err(f"no _SUITES entry named {name!r}",
                       hint="name must match a suite's `name` field in "
                            "admin.seed_eval_suites._SUITES")
        existing = next(iter(list_rows(client, "agent_eval_suites",
                                       filters={"name": name}, limit=1)), None)
        if not existing:
            return err(f"no existing suite named {name!r} in env={env!r}",
                       hint="sync_from_repo re-syncs an already-seeded suite, "
                            "it does not create one — the boot seeder inserts "
                            "it on next restart, or use action=create now")
        repo_scenarios = _resolve_scenarios(entry) or []
        try:
            scope = active_scope()
        except ValueError:
            scope = None
        repo_runner_kind = entry.get("runner_kind", "chat")
        repo_default_context = entry.get("default_context")
        if repo_default_context is not None:
            repo_default_context = _localize_voice_ws_path(repo_default_context, scope)
        repo_threshold = entry.get("pass_threshold", 0.7)

        existing_by_id = {s.get("id"): s for s in (existing.get("scenarios") or [])
                          if isinstance(s, dict)}
        repo_by_id = {s.get("id"): s for s in repo_scenarios if isinstance(s, dict)}
        adds = sorted(set(repo_by_id) - set(existing_by_id))
        removes = sorted(set(existing_by_id) - set(repo_by_id))
        changed = sorted(sid for sid in (set(repo_by_id) & set(existing_by_id))
                         if repo_by_id[sid] != existing_by_id[sid])
        diff = {
            "suite_id": existing.get("suite_id"),
            "scenario_adds": adds,
            "scenario_removes": removes,
            "scenario_changed": changed,
            "runner_kind_differs": repo_runner_kind != (existing.get("runner_kind") or "chat"),
            "default_context_differs": repo_default_context != existing.get("default_context"),
            "pass_threshold_differs": repo_threshold != existing.get("pass_threshold"),
        }
        if not confirm:
            changes = ([f"{len(adds)} scenario add(s)"] if adds else []) \
                + ([f"{len(removes)} scenario remove(s)"] if removes else []) \
                + ([f"{len(changed)} scenario change(s)"] if changed else []) \
                + (["runner_kind"] if diff["runner_kind_differs"] else []) \
                + (["default_context"] if diff["default_context_differs"] else []) \
                + (["pass_threshold"] if diff["pass_threshold_differs"] else [])
            return {
                "confirmed": False,
                "diff": diff,
                "would": f"sync suite {name!r} from the repo: "
                        + (", ".join(changes) if changes else "no changes"),
                "hint": "No change was made. Re-call with confirm=true to execute.",
            }
        payload = {
            "scenarios": repo_scenarios,
            "runner_kind": repo_runner_kind,
            "pass_threshold": repo_threshold,
        }
        if repo_default_context is not None:
            payload["default_context"] = repo_default_context
        client.table("agent_eval_suites").update(payload) \
            .eq("suite_id", existing["suite_id"]).execute()
        actor_audit("agent_eval_suites", existing.get("suite_id"), "sync_from_repo",
                    before=existing, after={**existing, **payload})
        return {"synced": True, "suite_id": existing.get("suite_id"), "diff": diff}
    return err(f"unknown action {action!r}",
               hint="list | get | create | update | import_scenarios | sync_from_repo")


@mcp.tool()
@tool_errors
def eval_schedules(action: Literal["list", "create", "update", "delete", "fire"],
                   schedule_id: str = "", env: Env = "staging",
                   name: str = "", suite_id: str = "", profile_id: str = "",
                   agent_kind: str = "",
                   source: Literal["suite", "live_chats"] = "suite",
                   cadence: Literal["manual", "daily", "weekly",
                                    "monthly"] = "manual",
                   business_id: str = "",
                   lookback_days: int = 30, sample_size: int = 10,
                   judge_model: str = "", expectations: str = "",
                   is_enabled: bool = True,
                   confirm: bool = False) -> Dict[str, Any]:
    """Scheduled eval runs. Required: action.

    Actions:
    - list.
    - create / update (name; update also needs schedule_id): both write the
      FULL schedule, so on update pass every field you want kept, not only
      the ones that change. source="suite" needs suite_id.
      source="live_chats" needs judge_model and agent_kind (directly or
      through profile_id); blank expectations use that agent kind's default
      rubric. A profile_id sets agent_kind from that profile, overriding any
      agent_kind passed; a profile_id that does not resolve is ignored.
      cadence: manual, daily, weekly or monthly. The next run time is
      computed from it, and manual schedules never run on their own.
    - delete (schedule_id, confirm=true).
    - fire (schedule_id): run the schedule now, then move its last and next
      run times forward. Returns {fired, run_id}."""
    # Maintainer notes (kept out of the description clients read):
    # Table agent_eval_schedules. Full replace mirrors the portal's edit form,
    # which always resubmits every field. agent_kind derives through
    # _sync_agent_kind like eval_suites, but a stale profile_id is ignored here
    # (as the portal's schedule form does) instead of erroring. `fire` uses the
    # same launcher as trigger_eval_run.
    from admin.eval_handlers import _sync_agent_kind
    from admin.eval_runs_handlers import (_CADENCES, _eval_agent_kinds,
                                          _SOURCES, _fire_schedule,
                                          compute_next_run_at,
                                          default_expectation_for)
    client = client_for_env(env)
    if action == "list":
        return {"schedules": list_rows(client, "agent_eval_schedules", limit=200)}
    if action in ("create", "update"):
        if not name.strip():
            return err("name is required")
        if source not in _SOURCES:
            return err(f"source must be one of {list(_SOURCES)}")
        if cadence not in _CADENCES:
            return err(f"cadence must be one of {list(_CADENCES)}")
        # Profile binding: a bound profile is the single source of truth for
        # agent_kind, same rule eval_suites enforces — only overrides when
        # the profile actually resolves (mirrors
        # eval_runs_handlers._schedule_payload_from_form).
        if profile_id:
            derived_kind = _sync_agent_kind(client, profile_id)
            if derived_kind:
                agent_kind = derived_kind
        coverage: Dict[str, Any] = {}
        if business_id:
            coverage["business_id"] = business_id
        if source == "live_chats":
            if not agent_kind:
                return err("agent_kind is required for a live_chats schedule")
            if agent_kind not in _eval_agent_kinds():
                return err(f"live_chats schedules can't target agent_kind={agent_kind!r} "
                           f"(not a sampled chat agent); pick one of {list(_eval_agent_kinds())}")
            if not judge_model:
                return err("judge_model is required for a live_chats schedule")
            coverage["lookback_days"] = lookback_days
            coverage["sample_size"] = sample_size
            coverage["judge_model"] = judge_model
            coverage["expectations"] = expectations.strip() or default_expectation_for(agent_kind)
        elif not suite_id:
            return err("suite_id is required for a suite schedule")
        actor = current_actor()
        payload = {
            "name": name.strip(), "suite_id": suite_id or None,
            "profile_id": profile_id or None, "agent_kind": agent_kind or None,
            "source": source, "cadence": cadence, "coverage": coverage,
            "is_enabled": is_enabled,
            "next_run_at": compute_next_run_at(cadence),
            "updated_by": actor["user_id"],
        }
        if action == "create":
            payload["created_by"] = actor["user_id"]
            inserted = rows(client.table("agent_eval_schedules").insert(payload).execute())
            created = inserted[0] if inserted else payload
            actor_audit("agent_eval_schedules", created.get("schedule_id", ""),
                        "create", after=created)
            return {"created": True, "schedule": created}
        before = get_row(client, "agent_eval_schedules", "schedule_id", schedule_id)
        if not before:
            return err("schedule not found")
        client.table("agent_eval_schedules").update(payload) \
            .eq("schedule_id", schedule_id).execute()
        actor_audit("agent_eval_schedules", schedule_id, "update",
                    before=before, after=payload)
        return {"updated": True, "schedule_id": schedule_id, "schedule": payload}
    if action == "delete":
        before = get_row(client, "agent_eval_schedules", "schedule_id", schedule_id)
        if not before:
            return err("schedule not found")
        if not confirm:
            return not_confirmed(f"delete eval schedule {before.get('name')!r}")
        client.table("agent_eval_schedules").delete().eq("schedule_id", schedule_id).execute()
        actor_audit("agent_eval_schedules", schedule_id, "delete", before=before)
        return {"deleted": True, "schedule_id": schedule_id}
    if action == "fire":
        schedule = get_row(client, "agent_eval_schedules", "schedule_id", schedule_id)
        if not schedule:
            return err("schedule not found")
        actor = current_actor()
        with use_env(env):
            run_id = _fire_schedule(schedule, actor_user_id=actor["user_id"],
                                    user_token=actor["user_token"])
        actor_audit("agent_eval_schedules", schedule_id, "fire",
                    after={"fired_at": datetime.now(timezone.utc).isoformat()})
        return {"fired": True, "run_id": run_id}
    return err(f"unknown action {action!r}",
               hint="list | create | update | delete | fire")


@mcp.tool()
@tool_errors
def pytest_runs(action: Literal["trigger", "get", "list"] = "list",
                run_id: str = "",
                suite: Literal["smoke", "unit_runtime", "unit_admin",
                               "all"] = "smoke",
                env: Env = "staging", limit: int = 25) -> Dict[str, Any]:
    """Run the engine's own pytest suites and read the results.

    Actions:
    - trigger (suite: smoke, unit_runtime, unit_admin or all; default
      smoke): start a background job and return job_id at once. Poll with
      jobs(action="get") or pytest_runs(action="get"). The tests run inside
      this engine against its own code, so env does not change what is
      tested.
    - get (run_id).
    - list (default): recent runs."""
    # Maintainer notes (kept out of the description clients read):
    # Table agent_test_runs. Unlike the portal's /admin/tests page, which
    # blocks the HTTP request until pytest finishes, trigger returns at once.
    if action == "trigger":
        if suite not in ("smoke", "unit_runtime", "unit_admin", "all"):
            return err("suite must be one of smoke | unit_runtime | unit_admin | all")
        from admin._jobs import start_tracked_job
        from admin.test_runner import run_suite
        actor = current_actor()
        job_id = start_tracked_job(
            run_suite, kind="other", label=f"pytest:{suite}",
            actor_user_id=actor["user_id"], suite=suite,
            triggered_by=actor["user_id"],
        )
        return {"job_id": job_id, "status": "queued",
                "hint": "poll jobs action=get job_id=..."}
    client = client_for_env(env)
    if action == "get":
        row = get_row(client, "agent_test_runs", "run_id", run_id)
        return {"run": row} if row else err("run not found")
    if action == "list":
        return {"runs": list_rows(client, "agent_test_runs", limit=limit)}
    return err(f"unknown action {action!r}", hint="trigger | get | list")


@mcp.tool()
@tool_errors
def voice_scenarios(action: Literal["list", "get", "set", "delete",
                                    "run", "runs"] = "list",
                    slug: str = "", agent_kind: str = "", title: str = "",
                    ws_path: str = "", locale: str = "",
                    steps: Optional[List[Dict[str, Any]]] = None,
                    notes: str = "", enabled: Optional[bool] = None,
                    target: str = "", headers: Optional[Dict[str, str]] = None,
                    env: Env = "staging", limit: int = 25) -> Dict[str, Any]:
    """Scripted voice-call checks. A run places a REAL call and verifies
    what happens.

    Actions:
    - list (default).
    - get (slug).
    - set (slug; creating one also needs agent_kind and steps): create or
      update a scenario. Step types: say, expect_transcript, expect_frame,
      http_check, wait. Optional: title, ws_path, locale, notes, enabled.
    - delete (slug): deletes at once; there is no confirm step.
    - run (slug): start a background job that calls this engine's own
      /voice/live, or target (a ws:// or wss:// URL). headers are sent with
      the call and added to every http_check step. They apply to this run
      only and are never stored, so they can carry a login cookie or other
      secret. Returns job_id.
    - runs: recent results; slug filters.

    Runs need AZURE_SPEECH_KEY on the service. Without it they end with
    status=skipped."""
    # Maintainer notes (kept out of the description clients read):
    # Table ai_voice_scenarios (runs in ai_voice_scenario_runs); the runner is
    # admin/voice_scenario_runner.py, whose module docstring has the step
    # shapes.
    client = client_for_env(env)
    if action == "list":
        return {"scenarios": list_rows(client, "ai_voice_scenarios",
                                       order_by="slug", desc=False, limit=200)}
    if action == "runs":
        return {"runs": list_rows(client, "ai_voice_scenario_runs",
                                  filters={"slug": slug or None},
                                  order_by="started_at", limit=limit)}
    if not slug:
        return err("slug is required for get|set|delete|run")
    if action == "get":
        row = get_row(client, "ai_voice_scenarios", "slug", slug)
        return {"scenario": row} if row else err("scenario not found")
    if action == "set":
        from admin.voice_scenario_runner import STEP_TYPES
        if steps is not None:
            for s in steps:
                if not isinstance(s, dict) or s.get("type") not in STEP_TYPES:
                    return err(f"each step needs a type in {STEP_TYPES}")
        before = get_row(client, "ai_voice_scenarios", "slug", slug)
        if before is None and (not agent_kind or steps is None):
            return err("creating a scenario needs agent_kind and steps")
        from datetime import datetime, timezone
        payload = {k: v for k, v in {
            "slug": slug, "agent_kind": agent_kind or None,
            "title": title or None, "ws_path": ws_path or None,
            "locale": locale or None, "steps": steps, "notes": notes or None,
            "enabled": enabled,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }.items() if v is not None}
        client.table("ai_voice_scenarios").upsert(
            payload, on_conflict="slug").execute()
        actor_audit("ai_voice_scenarios", slug, "upsert",
                    before=before, after=payload)
        return {"set": True, "slug": slug}
    if action == "delete":
        before = get_row(client, "ai_voice_scenarios", "slug", slug)
        if not before:
            return err("scenario not found")
        client.table("ai_voice_scenarios").delete().eq("slug", slug).execute()
        actor_audit("ai_voice_scenarios", slug, "delete", before=before)
        return {"deleted": True, "slug": slug}
    if action == "run":
        from admin._jobs import start_tracked_job
        from admin.voice_scenario_runner import run_scenario_job
        actor = current_actor()
        # use_env: /mcp requests never run the /admin env middleware, so
        # without this the spawn captures no admin env and the tracker's
        # queued/terminal rows land on the LOCAL branch while the runner
        # (which pins its explicit `environment`) writes the env branch —
        # same parity wrapper as the e2e_runs trigger.
        with use_env(env):
            job_id = start_tracked_job(
                run_scenario_job, kind="other", label=f"voice-scenario:{slug}",
                actor_user_id=actor["user_id"],
                related_entity_type="ai_voice_scenarios", related_entity_id=slug,
                metadata={"environment": env},
                slug=slug, environment=env, triggered_by=actor["user_id"],
                target=target or None, headers=headers or None,
            )
        return {"job_id": job_id, "status": "queued",
                "hint": "poll voice_scenarios action=runs slug=... for the result"}
    return err(f"unknown action {action!r}",
               hint="list | get | set | delete | run | runs")


@mcp.tool()
@tool_errors
def e2e_runs(action: Literal["trigger", "get", "list"] = "list",
            run_id: str = "", profile_id: str = "",
            env: Env = "staging", limit: int = 25) -> Dict[str, Any]:
    """End-to-end test runs against a deployed environment.

    Actions:
    - trigger (profile_id, env): run the profile's active eval suites,
      scenario by scenario, against its live endpoint in env. A judge scores
      the answers when the suite has an evaluator model. Starts a background
      job and returns job_id at once.
    - get (run_id).
    - list (default): recent runs in env; profile_id filters."""
    if action == "trigger":
        if not profile_id:
            return err("profile_id is required")
        from admin._jobs import start_tracked_job
        from admin.e2e_runner import run_e2e
        actor = current_actor()
        with use_env(env):
            job_id = start_tracked_job(
                run_e2e, kind="eval_run", label=f"e2e:{profile_id[:8]}/{env}",
                actor_user_id=actor["user_id"],
                related_entity_type="agent_profiles",
                related_entity_id=profile_id,
                metadata={"environment": env},
                profile_id=profile_id, environment=env,
                triggered_by=actor["user_id"],
            )
        return {"job_id": job_id, "status": "queued",
                "hint": "poll jobs action=get job_id=..., or e2e_runs "
                        "action=list profile_id=... once it lands"}
    client = client_for_env(env)
    if action == "get":
        row = get_row(client, "agent_e2e_runs", "run_id", run_id)
        return {"run": row} if row else err("run not found")
    if action == "list":
        return {"runs": list_rows(
            client, "agent_e2e_runs",
            filters={"profile_id": profile_id or None, "environment": env}, limit=limit)}
    return err(f"unknown action {action!r}", hint="trigger | get | list")
