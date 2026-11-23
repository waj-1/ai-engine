"""Eval-run launchers shared by the portal trigger endpoint, eval schedules,
and the MCP trigger_eval_run tool."""
from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from core.auth import admin_service_client


def _build_model_override(model_row, gen_config=None):
    """Partial agent_models dict for X-WAJ-Model-Override, with gen-config
    (temperature/max_output_tokens/top_p) layered on top."""
    if not model_row:
        return None
    gen_config = gen_config or {}
    extra = dict(model_row.get("extra_config") or {})
    if gen_config.get("top_p") is not None:
        extra["top_p"] = gen_config["top_p"]
    # Thinking-mode override for this variant. `thinking_level` wins over
    # `thinking_budget` at build time (see _google_thinking_kwargs), so a
    # budget-only variant must drop any inherited level for the budget to apply.
    if gen_config.get("thinking_level"):
        extra["thinking_level"] = gen_config["thinking_level"]
        extra.pop("thinking_budget", None)
    elif gen_config.get("thinking_budget") is not None:
        extra["thinking_budget"] = int(gen_config["thinking_budget"])
        extra.pop("thinking_level", None)
    override = {
        "provider": model_row.get("provider"),
        "model_string": model_row.get("model_string"),
        "temperature": gen_config.get("temperature", model_row.get("temperature", 0.2)),
        "extra_config": extra,
    }
    mot = gen_config.get("max_output_tokens", model_row.get("max_output_tokens"))
    if mot:
        override["max_output_tokens"] = int(mot)
    return override


def start_live_chat_run(
    *,
    profile_id: str,
    agent_kind: str,
    business_id: Optional[str],
    lookback_days: int,
    sample_size: int,
    judge_model: Optional[str],
    expectations: Optional[str],
    actor_user_id: Optional[str],
    user_token: Optional[str],
    schedule_id: Optional[str] = None,
) -> str:
    """Insert the queued agent_eval_runs row + spawn the tracked job.
    Returns the run_id.

    `schedule_id`, when the caller is a schedule firing (see
    `eval_runs_handlers._fire_schedule`), links the run row back to its
    originating schedule — record-keeping only, not forwarded to the runner.
    Mirrors the `schedule_id` param `start_suite_run` already carries.
    """
    from admin._jobs import start_tracked_job
    from admin.eval_runs_handlers import run_live_chat_eval_sync

    client = admin_service_client()
    run = client.table("agent_eval_runs").insert(
        {
            "suite_id": None,
            "profile_id": profile_id,
            "status": "queued",
            "triggered_by": actor_user_id,
            "context": {
                "source": "live_chats",
                "agent_kind": agent_kind,
                "business_id": business_id,
                "lookback_days": lookback_days,
                "sample_size": sample_size,
                "judge_model": judge_model,
                "expectations": expectations,
            },
            **({"schedule_id": schedule_id} if schedule_id else {}),
        }
    ).execute()
    rows = getattr(run, "data", None) or []
    run_id = rows[0]["run_id"] if rows else str(uuid.uuid4())

    start_tracked_job(
        run_live_chat_eval_sync,
        kind="eval_run",
        label=f"live-eval:{str(run_id)[:8]}",
        actor_user_id=actor_user_id,
        related_entity_type="agent_eval_runs",
        related_entity_id=run_id,
        metadata={"source": "live_chats", "profile_id": profile_id, "agent_kind": agent_kind},
        run_id=run_id,
        profile_id=profile_id,
        agent_kind=agent_kind,
        business_id=business_id or None,
        lookback_days=lookback_days,
        sample_size=sample_size,
        judge_model=judge_model or None,
        expectations=expectations or None,
        user_token=user_token,
    )
    return str(run_id)


def _voice_live_guard(*, runner_kind, suite_default_context, voice_mode,
                      schedule_id, bakeoff_id) -> None:
    """Live voice runs place real, billable provider calls — manual-only
    (spec §4). Effective mode = explicit run override, else the suite's
    default_context.voice.mode, else sim."""
    if (runner_kind or "chat") != "voice_live":
        return
    default_mode = ((suite_default_context or {}).get("voice") or {}).get("mode")
    effective = (voice_mode or default_mode or "sim")
    if effective == "live" and (schedule_id or bakeoff_id):
        raise ValueError(
            "live voice eval runs are manual-only — schedules and bake-offs "
            "must use sim mode (set voice.mode='sim' or trigger manually)"
        )


def resolve_prompt_overrides(client, profile_id: str,
                             overrides: Dict[str, Any]) -> Dict[str, int]:
    """Turn a caller's prompt overrides into the keys the runtime looks up
    (`<agent_kind>/<prompt slug>` -> version number), and refuse any that
    name nothing.

    A short key (`system`) means "this agent's own slot". The check matters
    because the runtime treats an override it cannot find as "no override":
    it serves the agent's live prompt, and the run comes back green with the
    variant's label on it. Raises ValueError naming the entry.
    """
    from core.prompt_resolver import override_slot

    rows = (client.table("agent_profiles").select("agent_kind, slug")
            .eq("profile_id", profile_id).limit(1).execute().data or [])
    profile = rows[0] if rows else {}
    kind, slug = profile.get("agent_kind"), profile.get("slug")
    resolved: Dict[str, int] = {}
    for raw_key, raw_version in overrides.items():
        key = str(raw_key).strip()
        try:
            version = int(raw_version)
        except (TypeError, ValueError):
            raise ValueError(f"prompt override {key!r}: the version must be a number, "
                             f"got {raw_version!r}")
        if "/" in key:
            o_kind, o_slot = key.split("/", 1)
        else:
            o_kind, o_slot = kind, key
        if not o_kind:
            raise ValueError(f"prompt override {key!r}: write it as <agent_kind>/<prompt key>")
        if o_kind == kind and ":" not in o_slot:
            o_slot = override_slot(kind, slug, o_slot)
        prompt = (client.table("agent_prompts").select("prompt_id")
                  .eq("agent_kind", o_kind).eq("slug", o_slot).limit(1).execute().data or [])
        if not prompt:
            raise ValueError(f"prompt override {key!r}: there is no prompt {o_kind}/{o_slot}")
        found = (client.table("agent_prompt_versions").select("prompt_version_id")
                 .eq("prompt_id", prompt[0]["prompt_id"]).eq("version_number", version)
                 .limit(1).execute().data or [])
        if not found:
            raise ValueError(f"prompt override {key!r}: {o_kind}/{o_slot} has no version {version}")
        resolved[f"{o_kind}/{o_slot}"] = version
    return resolved


def start_suite_run(
    *,
    suite_id: str,
    profile_id: str,
    business_id: Optional[str],
    actor_user_id: Optional[str],
    user_token: Optional[str],
    model_id: Optional[str] = None,
    prompt_overrides: Optional[Dict[str, Any]] = None,
    gen_config: Optional[Dict[str, Any]] = None,
    variant_label: Optional[str] = None,
    repeats: int = 1,
    concurrency: Optional[int] = None,
    profile_version_id: Optional[str] = None,
    bakeoff_id: Optional[str] = None,
    schedule_id: Optional[str] = None,
    voice_mode: Optional[str] = None,
) -> str:
    """Insert the queued agent_eval_runs row + spawn the suite runner.
    Reuses `admin.evals.run_eval_suite_sync`. Returns the run_id.

    `repeats` (>1) runs each scenario N times so the comparison can measure
    pass^k (consistency) — see `eval_handlers._compare_runs_n`.

    `concurrency` (how many attempts run at once) rides `run.context` only;
    None leaves the suite's default_context / env default in charge
    (`admin.evals._resolve_concurrency` clamps it).

    `profile_version_id` pins the run to one promoted profile snapshot
    (e.g. a bake-off variant) — persisted on both `context` (read back by
    `run_eval_suite_sync`) and the dedicated `agent_eval_runs.profile_version_id`
    column. `bakeoff_id` / `schedule_id` just link the run row back to its
    originating bake-off / schedule for the runs list — neither is forwarded
    to the runner, they're record-keeping only.
    """
    from admin._jobs import start_tracked_job
    from admin.evals import run_eval_suite_sync

    client = admin_service_client()
    run_context: Dict[str, Any] = {}

    # Hoisted above the comparison-run block below so it always runs (one
    # cheap read): the voice manual-only guard needs runner_kind +
    # default_context regardless of whether this is a comparison run.
    suite_rows = (client.table("agent_eval_suites")
                  .select("evaluator_model_id, runner_kind, default_context, scenarios")
                  .eq("suite_id", suite_id)
                  .limit(1).execute().data or [])
    suite_row = suite_rows[0] if suite_rows else {}
    _voice_live_guard(
        runner_kind=suite_row.get("runner_kind"),
        suite_default_context=suite_row.get("default_context"),
        voice_mode=voice_mode,
        schedule_id=schedule_id,
        bakeoff_id=bakeoff_id,
    )
    if voice_mode in ("sim", "live"):
        run_context["voice"] = {"mode": voice_mode}

    if business_id:
        run_context["business_id"] = business_id
    if model_id:
        rows = (client.table("agent_models").select("*").eq("model_id", model_id)
                .limit(1).execute().data or [])
        if rows:
            run_context["model_override"] = _build_model_override(rows[0], gen_config)
            run_context["model_label"] = rows[0].get("name")
    version_row: Optional[Dict[str, Any]] = None
    if profile_version_id:
        run_context["profile_version_id"] = profile_version_id
        # Guard against a version pinned to a *different* profile than the
        # one this run is attributed to: agent_eval_runs.profile_id and
        # every UI label would say A while load_version's snapshot (keyed
        # off the version row's own profile_id) silently executes B's
        # config — a server-side misattribution bug, not just a UI one.
        # Fail before the run row is inserted so there's no half-write.
        # `snapshot` is included in the select (in addition to the
        # ownership-check columns) so the gen-config-only branch below can
        # read the version's resolved model_main out of it without a
        # second, differently-scoped query (see I3).
        version_rows = (client.table("agent_profile_versions")
                        .select("version_id, profile_id, snapshot")
                        .eq("version_id", profile_version_id)
                        .limit(1).execute().data or [])
        if not version_rows:
            raise ValueError(f"profile version {profile_version_id} not found")
        version_row = version_rows[0]
        if version_row.get("profile_id") != profile_id:
            raise ValueError("profile version does not belong to the selected profile")
    if not model_id and gen_config:
        # I3: a gen-config-only variant (no explicit model_id) has no
        # transport for its gen-config unless we resolve the variant's
        # EFFECTIVE base model and build the override from THAT model +
        # gen_config. Without this, the override is never built, the variant
        # silently runs identically to baseline, and the matrix shows a
        # difference that never existed.
        base_model: Optional[Dict[str, Any]] = None
        if version_row is not None:
            base_model = (version_row.get("snapshot") or {}).get("model_main")
        else:
            profile_rows = (client.table("agent_profiles").select("model_main_id")
                            .eq("profile_id", profile_id).limit(1).execute().data or [])
            model_main_id = profile_rows[0].get("model_main_id") if profile_rows else None
            if model_main_id:
                model_rows = (client.table("agent_models").select("*")
                              .eq("model_id", model_main_id).limit(1).execute().data or [])
                base_model = model_rows[0] if model_rows else None
        if not base_model:
            raise ValueError(
                "variant sets gen-config but no model could be resolved to apply it to"
            )
        run_context["model_override"] = _build_model_override(base_model, gen_config)
        run_context["model_label"] = base_model.get("name")
    if model_id or profile_version_id or run_context.get("model_override"):
        # Comparison runs (model_override set) OR version-pinned runs (a
        # specific promoted snapshot under test, e.g. a bake-off variant)
        # require a model judge so every variant is graded on the same
        # rubric. Refuse early with a clear error so operators know what to
        # fix rather than getting ungraded results. Exception: suites whose
        # runner_kind grades itself deterministically (a plugin runner's
        # default scorer) need no judge — see runner_kind_needs_judge.
        from admin.services.eval_scorers import JUDGE_MODEL_HINT, suite_judge_problem

        problem = suite_judge_problem(client, suite_row)
        if problem:
            raise ValueError(
                f"comparison runs require a model judge on the suite, and {problem}. "
                f"{JUDGE_MODEL_HINT}.",
            )
    if prompt_overrides:
        prompt_overrides = resolve_prompt_overrides(client, profile_id, prompt_overrides)
        run_context["prompt_override"] = prompt_overrides
    if variant_label:
        run_context["variant_label"] = variant_label
    if repeats and int(repeats) > 1:
        run_context["repeats"] = int(repeats)
    if concurrency is not None and int(concurrency) > 0:
        run_context["concurrency"] = int(concurrency)
    run = client.table("agent_eval_runs").insert(
        {
            "suite_id": suite_id,
            "profile_id": profile_id,
            "status": "queued",
            "triggered_by": actor_user_id,
            "context": run_context,
            **({"profile_version_id": profile_version_id} if profile_version_id else {}),
            **({"bakeoff_id": bakeoff_id} if bakeoff_id else {}),
            **({"schedule_id": schedule_id} if schedule_id else {}),
        }
    ).execute()
    rows = getattr(run, "data", None) or []
    run_id = rows[0]["run_id"] if rows else str(uuid.uuid4())

    # Build coerced prompt_override dict: {str(k): int(v)} — skip entries whose
    # value can't be coerced to int (malformed input from the form).
    coerced_prompt_override: Optional[Dict[str, int]] = None
    if prompt_overrides:
        coerced: Dict[str, int] = {}
        for k, v in prompt_overrides.items():
            try:
                coerced[str(k)] = int(v)
            except (TypeError, ValueError):
                pass
        if coerced:
            coerced_prompt_override = coerced

    # NOTE (M2): the spawned job runs in a plain daemon thread, which does not
    # by itself inherit this request's `_ADMIN_ENV` ContextVar (Python
    # ContextVars aren't propagated across `threading.Thread`). That is NOT a
    # live footgun here, though: `start_tracked_job` (admin/_jobs.py) already
    # captures `core.auth.get_admin_env()` in THIS caller's context before
    # spawning, and its worker wrapper calls `core.auth.set_admin_env(...)`
    # with the captured value as the very first thing it does inside the
    # thread — so `run_eval_suite_sync`'s `admin_service_client()` calls see
    # the same env the operator had selected, not always the local branch.
    # See `start_tracked_job` / `_jobs.py` for the capture/re-set mechanics.
    start_tracked_job(
        run_eval_suite_sync,
        kind="eval_run",
        label=f"eval:{str(run_id)[:8]}",
        actor_user_id=actor_user_id,
        related_entity_type="agent_eval_runs",
        related_entity_id=run_id,
        metadata={"source": "suite", "suite_id": suite_id, "profile_id": profile_id},
        run_id=run_id,
        user_token=user_token,
        **({"prompt_override": coerced_prompt_override} if coerced_prompt_override is not None else {}),
        **({"repeats": int(repeats)} if repeats and int(repeats) > 1 else {}),
        **({"profile_version_id": profile_version_id} if profile_version_id else {}),
    )
    return str(run_id)
