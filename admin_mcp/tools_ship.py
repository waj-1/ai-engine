"""Ship-domain MCP tools: promotion, deploys, rollbacks, deployment
targets, and env vars."""
from __future__ import annotations

from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import Field

from admin_mcp._common import (DESTRUCTIVE, READ_ONLY, Confirm, Env,
                               EnvFilter, RowEnv, actor_audit, client_for_env,
                               err, get_row, list_rows, not_confirmed,
                               resolve_alias, rows, tool_errors, use_env,
                               validate_env)
from admin_mcp.context import current_actor
from admin_mcp.server import mcp


def _label_ids(client, table: str, id_col: str, label_col: str,
               ids: List[str]) -> Dict[str, str]:
    if not ids:
        return {}
    found = rows(client.table(table).select(f"{id_col}, {label_col}")
                 .in_(id_col, list(ids)).execute())
    return {r[id_col]: r.get(label_col) or r[id_col] for r in found}


def _prompt_pin_labels(client, pins: Dict[str, str]) -> Dict[str, str]:
    """{prompt_key: "v<N>"} for a prompt_version_ids dict (falls back to the
    raw id when the version row isn't readable)."""
    ids = [v for v in (pins or {}).values() if v]
    by_id = {}
    if ids:
        for r in rows(client.table("agent_prompt_versions")
                      .select("prompt_version_id, version_number")
                      .in_("prompt_version_id", ids).execute()):
            by_id[r["prompt_version_id"]] = f"v{r.get('version_number')}"
    return {k: by_id.get(v, v) for k, v in (pins or {}).items()}


def _mcp_attachment_names(client, profile_id: Optional[str]) -> List[str]:
    if not profile_id:
        return []
    try:
        from core.agent_profile import _fetch_mcp_attachments
        atts = _fetch_mcp_attachments(client, profile_id) or []
    except Exception:  # noqa: BLE001 — preview is best-effort
        return []
    out = []
    for a in atts:
        server = a.get("mcp_server") or {}
        name = server.get("name") or server.get("mcp_server_id")
        subset = a.get("tool_subset")
        out.append(f"{name}[{','.join(subset)}]" if subset else str(name))
    return sorted(out)


def promote_preview(profile_id: str, target_env: str, env: str) -> Dict[str, Any]:
    """Plain-language "what will change" for a promote, computed WITHOUT
    writing anything: the source row vs the row the target env serves today
    (same (agent_kind, slug)), the eval gate and the prompt-vs-bound-tools
    guard — the two checks admin.services.promotion.promote runs first."""
    from admin.profile_fork_handlers import compute_profile_diff
    from admin.services.profile_versions import summarize_profile_diff
    from admin.services.promotion import (_latest_eval_for_profile,
                                          _prompt_mandated_unbound_tools)
    client = client_for_env(env)
    src = get_row(client, "agent_profiles", "profile_id", profile_id)
    if not src:
        return err("profile not found", hint="list_profiles shows valid ids")
    if src.get("status") == "template":
        return err("cannot promote a template",
                   hint="fork it first with templates(action='use')")
    target_client = client_for_env(target_env)
    found = rows(target_client.table("agent_profiles").select("*")
                 .eq("agent_kind", src.get("agent_kind"))
                 .eq("slug", src.get("slug"))
                 .eq("environment", target_env).limit(1).execute())
    target = found[0] if found else None

    gate = _latest_eval_for_profile(client, src)
    missing_tools = _prompt_mandated_unbound_tools(client, src)

    changes: List[str] = []
    if target is None:
        changes.append(
            f"creates a NEW {target_env} profile for {src.get('agent_kind')}/"
            f"{src.get('slug')} (nothing is served there for it today)")
        compared_to = None
    else:
        compared_to = target.get("profile_id")
        diff = compute_profile_diff(client, target, src)
        # environment always differs (that is the promote) and status is
        # the target row's own lifecycle — neither is a config change.
        # "toolsets:" add/remove is reported below from each env's own
        # attachments ("MCP attachments: ..."); action-level changes stay.
        changes.extend(line for line in summarize_profile_diff(diff)
                       if not line.startswith(("environment:", "status:", "toolsets:")))
    old_pins = _prompt_pin_labels(target_client, (target or {}).get("prompt_version_ids") or {})
    new_pins = _prompt_pin_labels(client, src.get("prompt_version_ids") or {})
    for key in sorted(set(old_pins) | set(new_pins)):
        if old_pins.get(key) != new_pins.get(key):
            changes.append(f"prompt '{key}': {old_pins.get(key) or '(none)'} -> "
                           f"{new_pins.get(key) or '(none)'}")
    old_mcp = _mcp_attachment_names(target_client, (target or {}).get("profile_id"))
    new_mcp = _mcp_attachment_names(client, profile_id)
    if old_mcp != new_mcp:
        added = [m for m in new_mcp if m not in old_mcp]
        removed = [m for m in old_mcp if m not in new_mcp]
        changes.append("MCP attachments: " + ", ".join(
            [f"+{a}" for a in added] + [f"-{r}" for r in removed]))
    if target is not None and not changes:
        changes.append("no config differences — re-promoting only re-snapshots "
                       "the same config")

    blockers: List[str] = []
    if gate and gate.get("status") == "below_threshold":
        blockers.append(f"eval gate: latest score {gate.get('score')} is below "
                        f"threshold {gate.get('threshold')} "
                        f"({gate.get('suite_name')}) — needs force=true")
    if missing_tools:
        blockers.append("prompt mandates tools the binding omits: "
                        f"{', '.join(missing_tools)} — needs force=true")
    return {
        "profile_id": profile_id,
        "display_name": src.get("display_name"),
        "agent_kind": src.get("agent_kind"), "slug": src.get("slug"),
        "from_env": src.get("environment"), "target_env": target_env,
        "replaces_profile_id": compared_to,
        "changes": changes,
        "eval_gate": gate,
        "prompt_tool_mismatch": missing_tools,
        "blockers": blockers,
    }


@mcp.tool(annotations=DESTRUCTIVE)
@tool_errors
def promote_profile(
        profile_id: Annotated[str, Field(description="Source profile to promote.")],
        target_env: Annotated[Literal["staging", "prod"], Field(
            description="Environment to promote INTO (prod = what live "
                        "callers get).")],
        force: Annotated[bool, Field(
            description="Override a failing eval gate / prompt-tool guard "
                        "(audited). Needs confirm=true too.")] = False,
        confirm: Confirm = False,
        env: Env = "staging") -> Dict[str, Any]:
    """Promote a profile to target_env (staging|prod): eval-gate check,
    cross-env write, compile-on-promote snapshot.

    DRY-RUN BY DEFAULT. Without confirm=true nothing is written: you get
    {"confirmed": false, "would": ..., "preview": {changes, eval_gate,
    prompt_tool_mismatch, blockers}} — a plain list of what the promote
    would change in target_env (model, prompt versions, tools, KB, MCP
    attachments, sub-agents; or "creates a NEW profile"), and what would
    block it. Re-call with confirm=true to apply.

    With confirm=true: a failing eval gate comes back as {error:
    eval_gate_failed, score, threshold, hint} — fix and re-run evals, or
    re-call with force=true AND confirm=true (audited override)."""
    from admin.services.profiles import TemplateProtectedError
    from admin.services.promotion import (EvalGateFailed, ProfileNotFound,
                                          PromotionError, promote)
    validate_env(target_env)
    validate_env(env)
    if not confirm:
        preview = promote_preview(profile_id, target_env, env)
        if "error" in preview:
            return preview
        verb = "force-promote" if force else "promote"
        return not_confirmed(
            f"{verb} {preview.get('display_name') or profile_id} "
            f"({preview['agent_kind']}/{preview['slug']}) from "
            f"{preview.get('from_env')} to {target_env}: "
            f"{len(preview['changes'])} change(s)"
            + (f", {len(preview['blockers'])} blocker(s)" if preview["blockers"] else ""),
            preview=preview)
    actor = current_actor()
    try:
        with use_env(env):
            result = promote(profile_id=profile_id, target_env=target_env,
                             force=force, actor_user_id=actor["user_id"],
                             actor_email=actor["email"])
    except EvalGateFailed as exc:
        return {**exc.detail}
    except ProfileNotFound:
        return err("profile not found")
    except TemplateProtectedError as exc:
        return err(str(exc))
    except PromotionError as exc:
        return err(str(exc.detail), code=exc.status_code)
    return {"promoted": True, **result}


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def promotion_status(
        agent_kind: Annotated[str, Field(
            description="Only this agent_kind ('' = every kind).")] = "",
) -> Dict[str, Any]:
    """The staging-vs-prod promotion board (the portal's /admin/env Promote
    tab) as data: per agent_kind, the staging and prod profile rows (slug,
    status, served version pin), each one's latest eval vs its suite
    threshold, and `promotable` — true when a staging row passes (or has
    no) eval gate. Read-only; promote with promote_profile."""
    from admin.services.promotion import _latest_eval_for_profile
    board: Dict[str, Dict[str, Any]] = {}
    for e in ("staging", "prod"):
        client = client_for_env(e)
        q = (client.table("agent_profiles")
             .select("profile_id, agent_kind, slug, display_name, status, "
                     "is_active, environment, served_version_id, "
                     "model_main_id, updated_at")
             .eq("environment", e))
        if agent_kind:
            q = q.eq("agent_kind", agent_kind)
        for p in rows(q.order("agent_kind").execute()):
            if p.get("status") == "template":
                continue
            gate = _latest_eval_for_profile(client, p) or {}
            entry = board.setdefault(p["agent_kind"],
                                     {"agent_kind": p["agent_kind"],
                                      "staging": [], "prod": []})
            item = {**p, "eval_gate": gate}
            if e == "staging":
                item["promotable"] = gate.get("status") in ("pass", "none", None)
            entry[e].append(item)
    kinds = sorted(board.values(), key=lambda b: b["agent_kind"])
    return {"agent_kinds": kinds, "count": len(kinds),
            "hint": "promote_profile(profile_id, target_env='prod') previews; "
                    "add confirm=true to apply"}


@mcp.tool(annotations=DESTRUCTIVE)
@tool_errors
async def deploy(environment: Literal["prod"], git_ref: str = "main",
                 confirm: bool = False) -> Dict[str, Any]:
    """Re-ship the prod chat service (ai-engine) at `git_ref` (default "main").
    Only environment="prod" is valid — "staging" is an env column, not a service.
    Note: the *canonical* deploy is a merge to main, which ships both the
    control-plane and prod chat via CI; use this only to re-ship prod at a specific
    ref. Requires confirm=true. Watch progress with list_deploy_runs."""
    from admin.services.deploys import DeployError, dispatch_deploy
    if not confirm:
        return not_confirmed(f"deploy git ref {git_ref!r} to {environment}")
    try:
        return await dispatch_deploy(environment=environment, git_ref=git_ref,
                                     actor_user_id=current_actor()["user_id"])
    except DeployError as exc:
        return err(str(exc.detail), code=exc.status_code)


@mcp.tool(annotations=DESTRUCTIVE)
@tool_errors
async def rollback(environment: Literal["prod"], target_revision: str,
                   confirm: bool = False) -> Dict[str, Any]:
    """Roll the prod chat service (ai-engine) back to a previous Cloud Run
    revision. Only environment="prod" is valid — "staging" is an env column,
    not a deployable service. Requires confirm=true."""
    from admin.services.deploys import DeployError, dispatch_rollback
    if not confirm:
        return not_confirmed(f"roll back {environment} to {target_revision}")
    try:
        return await dispatch_rollback(environment=environment,
                                       target_revision=target_revision,
                                       actor_user_id=current_actor()["user_id"])
    except DeployError as exc:
        return err(str(exc.detail), code=exc.status_code)


@mcp.tool(annotations=READ_ONLY)
@tool_errors
async def list_deploy_runs(environment: EnvFilter = "", limit: int = 20) -> Dict[str, Any]:
    """Recent GitHub Actions deploy/rollback runs (status, conclusion,
    git ref, timestamps)."""
    from admin.deploy_handlers import _fetch_recent_deploy_runs
    runs = await _fetch_recent_deploy_runs(limit=limit)
    if environment:
        # _shape_run (admin/deploy_handlers.py) keys this "target_env", not
        # "environment" — it's parsed from the workflow run's display_title
        # and can be None when GitHub's dispatch inputs aren't echoed back
        # clearly, so an unfiltered/ambiguous run still passes through.
        runs = [r for r in runs if r.get("target_env") in (environment, None, "")]
    return {"runs": runs}


@mcp.tool()
@tool_errors
def deployment_targets(action: Literal["list", "get", "create", "update", "delete"],
                       target_id: str = "", env: Env = "staging",
                       row_environment: RowEnv = "",
                       environment: EnvFilter = "", label: str = "", base_url: str = "",
                       auth_env_var: Optional[str] = None,
                       description: Optional[str] = None,
                       is_default: Optional[bool] = None,
                       is_active: Optional[bool] = None,
                       confirm: bool = False) -> Dict[str, Any]:
    """CRUD for Cloud Run deployment targets (agent_deployment_targets) —
    per-(environment, label) service URLs the E2E runner and cross-env
    promote read instead of AI_AGENT_BASE_URL_* env vars. `env` selects
    which branch DB to operate on (client_for_env); `environment` is the
    row's own environment column (staging|prod, unique together with
    label) and defaults to `env` when omitted. action: list (optional
    environment filter) | get (target_id) | create (environment, label,
    base_url, ...) | update (target_id + fields) | delete (target_id,
    confirm=true). Setting is_default=true clears the default flag on
    every sibling row sharing the same environment (only one default per
    env), mirroring the portal's /admin/deployments Targets tab."""
    environment = resolve_alias(row_environment, environment,
                                new_name="row_environment", old_name="environment")
    client = client_for_env(env)
    if action == "list":
        # Default to the branch's own env (matches list_profiles' convention)
        # so single-DB dev setups — where both envs' rows can coexist in one
        # table — don't leak the sibling env's targets by default; pass an
        # explicit environment= to see the other one.
        return {"targets": list_rows(
            client, "agent_deployment_targets", order_by="environment",
            desc=False, filters={"environment": environment or env}, limit=200)}
    if action == "get":
        row = get_row(client, "agent_deployment_targets", "target_id", target_id)
        return {"target": row} if row else err("target not found")
    if action == "create":
        target_environment = validate_env((environment or env).strip())
        label = label.strip()
        base_url = base_url.strip()
        if not (label and base_url):
            return err("create needs label and base_url")
        existing = rows(client.table("agent_deployment_targets").select("target_id")
                        .eq("environment", target_environment).eq("label", label)
                        .limit(1).execute())
        if existing:
            return err(f"a target named {label!r} already exists for "
                       f"{target_environment}")
        payload = {
            "environment": target_environment, "label": label, "base_url": base_url,
            "auth_env_var": (auth_env_var or "").strip() or None,
            "description": (description or "").strip() or None,
            "is_default": bool(is_default),
        }
        if payload["is_default"]:
            client.table("agent_deployment_targets").update({"is_default": False}) \
                .eq("environment", target_environment).execute()
        inserted = rows(client.table("agent_deployment_targets").insert(payload).execute())
        created = inserted[0] if inserted else payload
        actor_audit("agent_deployment_targets", created.get("target_id", ""),
                    "create", after=created)
        return {"created": True, "target": created}
    if action == "update":
        before = get_row(client, "agent_deployment_targets", "target_id", target_id)
        if not before:
            return err("target not found")
        payload: Dict[str, Any] = {}
        if environment.strip():
            payload["environment"] = validate_env(environment.strip())
        if label.strip():
            payload["label"] = label.strip()
        if base_url.strip():
            payload["base_url"] = base_url.strip()
        if auth_env_var is not None:
            payload["auth_env_var"] = auth_env_var.strip() or None
        if description is not None:
            payload["description"] = description.strip() or None
        if is_default is not None:
            payload["is_default"] = is_default
        if is_active is not None:
            payload["is_active"] = is_active
        if not payload:
            return err("no fields supplied")
        if payload.get("is_default"):
            client.table("agent_deployment_targets").update({"is_default": False}) \
                .eq("environment", payload.get("environment") or before["environment"]) \
                .execute()
        client.table("agent_deployment_targets").update(payload) \
            .eq("target_id", target_id).execute()
        actor_audit("agent_deployment_targets", target_id, "update",
                    before=before, after={**before, **payload})
        return {"updated": True, "target_id": target_id, "changed": sorted(payload)}
    if action == "delete":
        before = get_row(client, "agent_deployment_targets", "target_id", target_id)
        if not before:
            return err("target not found")
        if not confirm:
            return not_confirmed(
                f"delete deployment target {before.get('label')!r} "
                f"({before.get('environment')})")
        client.table("agent_deployment_targets").delete().eq("target_id", target_id).execute()
        actor_audit("agent_deployment_targets", target_id, "delete", before=before)
        return {"deleted": True, "target_id": target_id}
    return err(f"unknown action {action!r}",
               hint="list | get | create | update | delete")


def _mask_for_audit(row: Dict[str, Any], secret: bool) -> Dict[str, Any]:
    """Mask ``row``'s value for an audit payload when ``secret`` is True,
    independent of ``row``'s own is_secret flag. `env_vars`' update action
    uses this (with secret = before.is_secret OR merged.is_secret) instead
    of calling `_mask` directly on the merged/after row: merging
    {**before, **payload} and then masking on the merged flag alone means
    unmarking a row as secret (before.is_secret=True, payload.is_secret=
    False, no new value) carries the still-plaintext `value` straight
    through _mask's own is_secret check and into agent_audit_log."""
    if not secret:
        return row
    from admin.env_vars_handlers import _mask
    return {**_mask({**row, "is_secret": True}), "is_secret": row.get("is_secret")}


@mcp.tool()
@tool_errors
def env_vars(action: Literal["list", "get", "create", "update", "delete"],
            var_id: str = "", env: Env = "staging",
            row_environment: RowEnv = "",
            environment: EnvFilter = "", name: str = "", value: Optional[str] = None,
            source: str = "", description: Optional[str] = None,
            is_secret: Optional[bool] = None, reveal: bool = False,
            confirm: bool = False) -> Dict[str, Any]:
    """CRUD for agent_env_vars — a third env-var lookup location (behind
    os.environ) resolved by core.env_resolver. `env` selects which branch
    DB to operate on (client_for_env); `row_environment` is the row's own
    environment column and defaults to `env` when omitted (`environment` is
    its deprecated alias, still accepted). Secret rows
    (is_secret=true) are masked to "••••" in list/get output AND in audit
    before/after payloads, exactly like the portal's /admin/env Variables
    tab. action: list (optional environment filter) | get (var_id;
    reveal=true bypasses the mask and returns the real value, additionally
    audited as action="reveal") | create (environment, name, value,
    source, description, is_secret) | update (var_id + fields — pass
    value="" to clear a stored value) | delete (var_id, confirm=true)."""
    from admin.env_vars_handlers import _mask
    environment = resolve_alias(row_environment, environment,
                                new_name="row_environment", old_name="environment")
    client = client_for_env(env)
    if action == "list":
        # Defaults to the branch's own env, same rationale as
        # deployment_targets' list — pass environment= to see the sibling.
        found = list_rows(client, "agent_env_vars", order_by="name", desc=False,
                          filters={"environment": environment or env}, limit=300)
        return {"vars": [_mask(r) for r in found]}
    if action == "get":
        row = get_row(client, "agent_env_vars", "var_id", var_id)
        if not row:
            return err("env var not found")
        if reveal:
            actor_audit("agent_env_vars", var_id, "reveal",
                       after={"name": row.get("name")})
            return {"var": row}
        return {"var": _mask(row)}
    if action == "create":
        target_environment = validate_env((environment or env).strip())
        name = name.strip()
        if not name:
            return err("name is required")
        actor = current_actor()
        payload = {
            "environment": target_environment, "name": name,
            "value": value if value not in (None, "") else None,
            "source": (source or "db").strip() or "db",
            "description": description or None,
            "is_secret": bool(is_secret),
            "created_by": actor["user_id"],
            "updated_by": actor["user_id"],
        }
        inserted = rows(client.table("agent_env_vars").insert(payload).execute())
        created = inserted[0] if inserted else payload
        actor_audit("agent_env_vars", created.get("var_id", ""), "create",
                    after=_mask(created))
        return {"created": True, "var": _mask(created)}
    if action == "update":
        before = get_row(client, "agent_env_vars", "var_id", var_id)
        if not before:
            return err("env var not found")
        payload: Dict[str, Any] = {}
        if environment.strip():
            payload["environment"] = validate_env(environment.strip())
        if name.strip():
            payload["name"] = name.strip()
        if source.strip():
            payload["source"] = source.strip()
        if description is not None:
            payload["description"] = description or None
        if value is not None:
            payload["value"] = value if value != "" else None
        if is_secret is not None:
            payload["is_secret"] = is_secret
        if not payload:
            return err("no fields supplied")
        payload["updated_by"] = current_actor()["user_id"]
        # Snapshot + decide the audit masking BEFORE the mutating update
        # call: some clients (see tests/admin_mcp/conftest.py's FakeTable)
        # mutate the row dict returned by the earlier read in place, so
        # reading `before.get("is_secret")` after `.update().execute()` can
        # silently observe the POST-update flag instead of the original one.
        before_snapshot = dict(before)
        merged = {**before_snapshot, **payload}
        # Mask on EITHER side's is_secret, not just the merged/after flag —
        # otherwise unmarking a row as secret (before.is_secret=True,
        # payload.is_secret=False, no new value) carries the still-plaintext
        # `value` through {**before, **payload} and _mask (which only masks
        # when is_secret is True) waves it straight into agent_audit_log.
        audit_secret = bool(before_snapshot.get("is_secret")) or bool(merged.get("is_secret"))
        client.table("agent_env_vars").update(payload).eq("var_id", var_id).execute()
        actor_audit("agent_env_vars", var_id, "update",
                    before=_mask_for_audit(before_snapshot, audit_secret),
                    after=_mask_for_audit(merged, audit_secret))
        return {"updated": True, "var_id": var_id, "changed": sorted(payload)}
    if action == "delete":
        before = get_row(client, "agent_env_vars", "var_id", var_id)
        if not before:
            return err("env var not found")
        if not confirm:
            return not_confirmed(
                f"delete env var {before.get('name')!r} ({before.get('environment')})")
        client.table("agent_env_vars").delete().eq("var_id", var_id).execute()
        actor_audit("agent_env_vars", var_id, "delete", before=_mask(before))
        return {"deleted": True, "var_id": var_id}
    return err(f"unknown action {action!r}",
               hint="list | get | create | update | delete")
