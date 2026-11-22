"""Promotion service — the single implementation behind both the portal's
POST /admin/env/promote and the MCP promote_profile tool. Eval gate,
cross-env sibling remap, upsert, compile-on-promote."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from admin.audit import record_change

logger = logging.getLogger(__name__)


class PromotionError(Exception):
    def __init__(self, status_code: int, detail: Any):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class ProfileNotFound(PromotionError):
    pass


class EvalGateFailed(PromotionError):
    pass


class SnapshotWriteFailed(PromotionError):
    pass


class PromptToolMismatch(PromotionError):
    pass


def _safe_list(execute_result: Any) -> List[Dict[str, Any]]:
    return execute_result.data or []


def _prompt_mandated_unbound_tools(source_client: Any, src: Dict[str, Any]) -> List[str]:
    """Tools the profile's system prompt mandates but its binding would omit.

    Predicts what will bind from the frozen MCP attachments (subset filter) and
    compares against the tools the compiled system prompt tells the model to
    call. Best-effort: any lookup failure returns [] rather than blocking a
    promote on a check-infra hiccup — the runtime guard in RuntimeAgent.prepare()
    still logs the mismatch if one slips through.
    """
    try:
        from core.agent_profile import _fetch_mcp_attachments
        from runtime.compiler import _fetch_prompt_contents
        from runtime.tools._mcp_resolver import bound_tool_names_from_attachments
        from runtime.prompts._tool_guard import prompt_mandated_unbound_tools

        attachments = _fetch_mcp_attachments(source_client, src.get("profile_id"))
        system_prompt = (
            _fetch_prompt_contents(source_client, src.get("prompt_version_ids") or {}) or {}
        ).get("system", "")
        member_names = {
            (m.get("tool") or {}).get("python_name")
            for att in attachments or []
            for m in (att.get("member_tools") or [])
            if (m.get("tool") or {}).get("python_name")
        }
        bound_names = bound_tool_names_from_attachments(attachments)
        return prompt_mandated_unbound_tools(system_prompt, member_names, bound_names)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("promote: prompt-tool guard check failed (%s); not blocking", exc)
        return []


def _remap_cross_env_refs(source_client, target_client, target_env: str,
                          src: Dict[str, Any], new_row: Dict[str, Any]) -> None:
    """Remap `new_row`'s parent_profile_id / downstream_profile_ids (which point
    at SOURCE-env rows) to their TARGET-env siblings, in place.

    Copied verbatim across envs those ids orphan the target row (sub-agents
    vanish from the rail; downstream checkboxes never tick; a delegate can't be
    resolved). Match each reference on (agent_kind, slug), NOT agent_kind alone:
    a delegation specialist shares its PARENT's agent_kind (e.g. a `business`
    specialist under the `business` main), so an agent_kind-only map collapses
    parent and delegate onto whichever same-kind target row the query returned
    last — which produced a self-reference (main.downstream pointing at the
    main) and left the delegate id unremapped. (agent_kind, slug, environment)
    is unique, so keying on it resolves each reference to exactly its sibling.
    An id with no target-env sibling yet is kept as-is (not silently dropped).
    """
    ref_ids = [i for i in ([src.get("parent_profile_id")] +
                           list(src.get("downstream_profile_ids") or [])) if i]
    if not ref_ids:
        return
    ref_key = {
        r["profile_id"]: (r["agent_kind"], r.get("slug"))
        for r in _safe_list(
            source_client.table("agent_profiles")
            .select("profile_id, agent_kind, slug").in_("profile_id", ref_ids).execute()
        )
    }
    tgt_by_kind_slug = {
        (r["agent_kind"], r.get("slug")): r["profile_id"]
        for r in _safe_list(
            target_client.table("agent_profiles")
            .select("profile_id, agent_kind, slug").eq("environment", target_env).execute()
        )
    }

    def _sibling(pid: str) -> str:
        return tgt_by_kind_slug.get(ref_key.get(pid), pid)

    if new_row.get("parent_profile_id"):
        new_row["parent_profile_id"] = _sibling(new_row["parent_profile_id"])
    if new_row.get("downstream_profile_ids"):
        new_row["downstream_profile_ids"] = [
            _sibling(d) for d in new_row["downstream_profile_ids"]
        ]


def promote(*, profile_id: str, target_env: str, force: bool,
            actor_user_id: str, actor_email: str,
            reason: Optional[str] = None) -> Dict[str, Any]:
    """Promote a profile from the current environment to staging or prod.

    Cross-environment topology: when `{TARGET}_SUPABASE_URL` +
    `{TARGET}_SUPABASE_SERVICE_KEY` are set, the profile row + snapshot
    are written directly to the *remote* Supabase project. Without
    those vars the write falls back to the local DB (single-DB mode) so
    development setups keep working.

    Hard eval gate: if the profile's most recent eval-run scored below
    the suite's `pass_threshold` (default 0.7), the promote raises
    `EvalGateFailed` unless `force=true` is supplied. The override is
    audited; an optional `reason` (the portal's confirm dialog requires one
    when a gate fails) is recorded on the override rows and on the
    CONFIG_PROMOTE row.
    """
    reason = (reason or "").strip() or None
    from core.agent_profile import AgentProfileLoader
    from core.auth import (
        CrossEnvNotConfiguredError,
        admin_service_client,
        get_service_client_for_env,
    )

    source_client = admin_service_client()
    rows = _safe_list(
        source_client.table("agent_profiles").select("*").eq("profile_id", profile_id).execute()
    )
    if not rows:
        raise ProfileNotFound(404, "profile not found")
    src = rows[0]
    if src.get("status") == "template":
        from admin.services.profiles import TemplateProtectedError
        raise TemplateProtectedError("cannot promote a template")

    # Hard eval gate — refuse to promote if the most recent eval run
    # scored below the suite's pass_threshold. Force=true overrides with
    # an audit entry so the bypass is visible in agent_audit_log.
    eval_state = _latest_eval_for_profile(source_client, src)
    forced = False
    if eval_state and eval_state.get("status") == "below_threshold" and not force:
        raise EvalGateFailed(
            422,
            {
                "error": "eval_gate_failed",
                "score": eval_state.get("score"),
                "threshold": eval_state.get("threshold"),
                "suite_name": eval_state.get("suite_name"),
                "hint": (
                    "Re-run evals after fixing the regression, or POST the "
                    "same form with force=true to override (the bypass is "
                    "audited in agent_audit_log)."
                ),
            },
        )
    if eval_state and eval_state.get("status") == "below_threshold" and force:
        forced = True
        logger.warning(
            "promote: profile %s force-promoted past eval gate (score=%s threshold=%s actor=%s)",
            profile_id, eval_state.get("score"), eval_state.get("threshold"), actor_email,
        )
        record_change(
            entity_type="agent_profiles", entity_id=profile_id,
            action="EVAL_GATE_OVERRIDE", actor_user_id=actor_user_id,
            after={
                "score": eval_state.get("score"),
                "threshold": eval_state.get("threshold"),
                "suite_name": eval_state.get("suite_name"),
                "target_env": target_env,
                **({"reason": reason} if reason else {}),
            },
        )

    # Prompt-vs-bound-tools gate — refuse to promote a profile whose system
    # prompt mandates a tool its binding omits. That mismatch is silent at
    # runtime (the model calls the tool, dispatch returns "Tool not found", the
    # agent apologizes and hallucinates) and caused #1032 twice. Force=true
    # overrides with an audited entry, mirroring the eval gate.
    missing_tools = _prompt_mandated_unbound_tools(source_client, src)
    if missing_tools and not force:
        raise PromptToolMismatch(
            422,
            {
                "error": "prompt_tool_mismatch",
                "missing_tools": missing_tools,
                "hint": (
                    "The system prompt tells the model to call these tools, but "
                    "the profile's MCP tool binding omits them — at runtime the "
                    "model would call them and get 'Tool not found'. Add them to "
                    "the profile's bundle tool_subset (or drop the mandate from "
                    "the prompt), then re-promote. POST force=true to override "
                    "(the bypass is audited in agent_audit_log)."
                ),
            },
        )
    if missing_tools and force:
        forced = True
        logger.warning(
            "promote: profile %s force-promoted past prompt-tool guard "
            "(missing=%s actor=%s)", profile_id, missing_tools, actor_email,
        )
        record_change(
            entity_type="agent_profiles", entity_id=profile_id,
            action="PROMPT_TOOL_MISMATCH_OVERRIDE", actor_user_id=actor_user_id,
            after={"missing_tools": missing_tools, "target_env": target_env,
                   **({"reason": reason} if reason else {})},
        )

    # Resolve the target-environment client. When the {TARGET}_SUPABASE_*
    # env vars are unset we fall back to the source client (single-DB
    # mode); the rest of the handler works either way.
    try:
        target_client = get_service_client_for_env(target_env)
    except CrossEnvNotConfiguredError:
        logger.info(
            "env_promote: %s_SUPABASE_* unset; writing to current DB (single-DB mode)",
            target_env.upper(),
        )
        target_client = source_client

    new_row = {k: v for k, v in src.items()
               if k not in {"profile_id", "created_at", "updated_at", "created_by", "updated_by"}}
    new_row["environment"] = target_env

    _remap_cross_env_refs(source_client, target_client, target_env, src, new_row)

    # Cross-env model references: model_main_id / model_light_id point at
    # SOURCE-env agent_models rows. Copied verbatim they violate the target
    # DB's agent_profiles_model_*_id_fkey when the source model id doesn't
    # exist there. Remap each to the target env's same-name sibling (models
    # are promoted as a (name, target_env) sibling via
    # /agent-models/{id}/promote); keep the original id when no sibling exists
    # so the promote surfaces a clear missing-model FK error rather than
    # silently nulling the model. (Ported from #1235.)
    model_ids = [i for i in (src.get("model_main_id"), src.get("model_light_id")) if i]
    if model_ids:
        src_model_name = {
            r["model_id"]: r.get("name")
            for r in _safe_list(
                source_client.table("agent_models")
                .select("model_id, name").in_("model_id", model_ids).execute()
            )
        }
        tgt_model_by_name = {
            r["name"]: r["model_id"]
            for r in _safe_list(
                target_client.table("agent_models")
                .select("model_id, name").eq("environment", target_env).execute()
            )
            if r.get("name")
        }

        def _model_sibling(mid: str) -> str:
            return tgt_model_by_name.get(src_model_name.get(mid, ""), mid)

        if new_row.get("model_main_id"):
            new_row["model_main_id"] = _model_sibling(new_row["model_main_id"])
        if new_row.get("model_light_id"):
            new_row["model_light_id"] = _model_sibling(new_row["model_light_id"])

    # The category rides on the row, not in the snapshot. In one database the
    # id is already right; across two it is matched by slug (and created in
    # the target when missing). Copied verbatim it would fail the target's
    # agent_profiles_agent_type_id_fkey and take the whole promote with it.
    if new_row.get("agent_type_id"):
        from admin.services.agent_types import carry_to

        new_row["agent_type_id"] = carry_to(
            source_client, target_client, new_row["agent_type_id"],
            actor_user_id=actor_user_id)

    # Upsert into target DB so re-promoting an already-promoted profile
    # syncs columns instead of failing the (agent_kind, slug, environment)
    # unique constraint.
    new_id = _upsert_target_profile(target_client, new_row)
    cross_env = target_client is not source_client
    record_change(
        entity_type="agent_profiles", entity_id=new_id or profile_id,
        action="CONFIG_PROMOTE", actor_user_id=actor_user_id,
        before=src,
        after={**new_row, "_cross_env": cross_env,
               **({"_reason": reason} if reason else {})},
    )
    AgentProfileLoader.invalidate(src["agent_kind"], target_env)

    # Compile: snapshot resolves models, prompts, tools, etc. from the
    # source client (so the snapshot is self-contained) and writes the
    # version into the target client.
    if new_id:
        from runtime.compiler import compile_profile_snapshot, SnapshotCompileError
        try:
            compile_profile_snapshot(
                profile_id=new_id,
                source_profile_id=profile_id,
                environment=target_env,
                promoted_by=actor_user_id,
                notes=f"Promoted from {src['environment']} by {actor_email}",
                source_client=source_client,
                target_client=target_client,
            )
        except SnapshotCompileError as exc:
            logger.error("env_promote: snapshot failed (%s)", exc)
            raise SnapshotWriteFailed(
                502,
                (
                    f"Profile row was inserted into {target_env} but the "
                    f"agent_profile_versions snapshot failed to write: {exc}. "
                    f"Re-click promote to retry — the snapshot writer is "
                    f"idempotent on success."
                ),
            ) from exc

    return {
        "profile_id": new_id or profile_id,
        "source_profile_id": profile_id,
        "target_env": target_env,
        "forced": forced,
        "cross_env": cross_env,
        "eval_gate": eval_state,
    }


def _upsert_target_profile(target_client, new_row: Dict[str, Any]) -> Optional[str]:
    """Insert or update the profile row in the target DB.

    `agent_profiles` has `UNIQUE (agent_kind, slug, environment)`. On
    re-promote the existing row is updated in place (so audit
    history sticks to one profile_id per env). Returns the profile_id.
    """
    existing = (
        target_client.table("agent_profiles").select("profile_id")
        .eq("agent_kind", new_row["agent_kind"])
        .eq("slug", new_row["slug"])
        .eq("environment", new_row["environment"])
        .limit(1).execute().data or []
    )
    if existing:
        existing_id = existing[0]["profile_id"]
        target_client.table("agent_profiles").update(new_row).eq(
            "profile_id", existing_id
        ).execute()
        return existing_id
    inserted = target_client.table("agent_profiles").insert(new_row).execute()
    return inserted.data[0]["profile_id"] if inserted.data else None


def _latest_eval_for_profile(client, profile: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the most-recent eval-run summary for `profile`.

    Shape: {status: 'pass'|'below_threshold'|'errored'|'none',
            score, threshold, suite_name, run_id, finished_at}
    `None` if the schema lookup itself fails — caller should treat as
    'no data' and proceed.
    """
    try:
        runs = (client.table("agent_eval_runs").select("*")
                .eq("profile_id", profile["profile_id"])
                .order("created_at", desc=True).limit(1)
                .execute().data or [])
        if not runs:
            return {"status": "none"}
        run = runs[0]
        suites = ((client.table("agent_eval_suites").select("name, pass_threshold")
                   .eq("suite_id", run["suite_id"]).limit(1)
                   .execute().data or []) if run.get("suite_id") else [])
        threshold = suites[0]["pass_threshold"] if suites else 0
        score = run.get("score")
        if run.get("status") == "errored":
            status = "errored"
        elif score is None:
            status = "none"
        elif score >= threshold:
            status = "pass"
        else:
            status = "below_threshold"
        return {
            "status": status,
            "score": score,
            "threshold": threshold,
            "suite_name": suites[0]["name"] if suites else "?",
            "run_id": run.get("run_id"),
            "finished_at": run.get("finished_at"),
        }
    except Exception:
        return None
