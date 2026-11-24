"""Configure-domain MCP tools: agent profiles, prompt versions, models,
tool registry, MCP-server registry, and KB sources."""
from __future__ import annotations

import logging
import os
from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import Field

from admin_mcp._common import (READ_ONLY, Confirm, Env, RowEnv, actor_audit,
                               client_for_env, err, get_row, list_rows,
                               not_confirmed, page, resolve_alias, rows,
                               tool_errors, use_env, validate_env)
from admin_mcp.context import current_actor
from admin_mcp.server import mcp

logger = logging.getLogger(__name__)


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def list_profiles(env: Env = "staging",
                  agent_kind: Optional[str] = None,
                  status: str = "",
                  agent_type: Annotated[str, Field(
                      description="Only agents in this category: a category "
                                  "slug from agent_types(action='list'), or "
                                  "'none' for uncategorised agents. '' = no "
                                  "filter.")] = "",
                  limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    """List agent profiles in one environment (env: staging or prod).
    Optional filters: agent_kind, status (active, draft, specialist, archived
    or template) and agent_type (a category slug). Each row has id, kind,
    slug, status, model ids, endpoint, served version pin and agent_type (the
    category slug, or null). Paged with limit and offset; total and has_more
    say what was cut."""
    client = client_for_env(env)
    profiles = list_rows(
        client, "agent_profiles",
        select="profile_id, agent_kind, slug, display_name, environment, "
               "status, is_active, model_main_id, model_light_id, "
               "endpoint_path, served_version_id, agent_type_id",
        filters={"environment": env, "agent_kind": agent_kind,
                 "status": status or None},
        order_by="agent_kind", desc=False, limit=1000,
    )
    from admin.services import agent_types as _types
    slug_by_id = {tid: t.get("slug") for tid, t in _types.types_by_id(client).items()}
    for prof in profiles:
        prof["agent_type"] = slug_by_id.get(prof.get("agent_type_id"))
    if agent_type:
        wanted = _types.resolve_type_id(agent_type, client)
        profiles = [prof for prof in profiles
                    if prof.get("agent_type_id") == wanted]
    paged = page(profiles, limit, offset, max_limit=500)
    return {"env": env, "count": len(paged["items"]), "total": paged["total"],
            "offset": paged["offset"], "has_more": paged["has_more"],
            "profiles": paged["items"]}


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def get_profile(profile_id: str = "", agent_kind: str = "", slug: str = "",
                env: Env = "staging",
                fields: Annotated[Optional[List[str]], Field(
                    description="Only return these columns of the raw profile "
                                "row (e.g. ['voice_config','memory_config']). "
                                "Omit for the whole row.")] = None,
                ) -> Dict[str, Any]:
    """Return the full config of one profile. Look it up by profile_id, or
    by agent_kind with an optional slug in env. With agent_kind and no slug,
    only the profile with status=active is returned. With a slug, a profile
    of any status can be returned.

    Returns the profile row plus resolved names for its tools, data sources,
    MCP servers, models and prompt version pins. mcp_attachments lists the
    MCP servers the runtime binds tools from and is the source of truth.
    mcp_servers is an older label list and can disagree with it. agent_type
    is the agent's category ({slug, display_name, color}) or null.

    fields limits the profile row to the named columns (the row holds large
    JSON such as voice_config and memory_config). The resolved lists are
    always returned. Prompt text is not included: use
    prompts(action="get", profile_id=...)."""
    # Maintainer notes (kept out of the description clients read):
    # `mcp_servers` is resolved from `mcp_server_ids[]`; `mcp_attachments` from
    # the `agent_profile_mcp_servers` join via
    # core/agent_profile.py::_fetch_mcp_attachments, which is what the tool
    # resolver reads.
    client = client_for_env(env)
    if profile_id:
        profile = get_row(client, "agent_profiles", "profile_id", profile_id)
    elif agent_kind:
        q = (client.table("agent_profiles").select("*")
             .eq("agent_kind", agent_kind).eq("environment", env))
        if slug:
            q = q.eq("slug", slug)
        else:
            q = q.eq("status", "active")
        found = rows(q.limit(1).execute())
        profile = found[0] if found else None
    else:
        return err("pass profile_id, or agent_kind (+ optional slug)")
    if not profile:
        return err("profile not found", hint="list_profiles shows valid ids")

    def _labels(table, id_col, ids, label_col):
        if not ids:
            return []
        found = rows(client.table(table).select(f"{id_col}, {label_col}")
                     .in_(id_col, ids).execute())
        by_id = {r[id_col]: r[label_col] for r in found}
        return [{"id": i, "label": by_id.get(i)} for i in ids]

    from core.agent_profile import _fetch_mcp_attachments
    attachments = _fetch_mcp_attachments(client, profile["profile_id"])
    # Slim the raw join (full mcp_server/tool rows) to JSON-friendly
    # summaries — shared with profile_mcp_attachments' `list` action
    # (helper defined further down this file).
    mcp_attachments = _slim_mcp_attachments(attachments)

    return {
        "profile": ({k: profile.get(k) for k in
                     ["profile_id", "agent_kind", "slug", *fields]}
                    if fields else profile),
        "tools": _labels("agent_tools", "tool_id",
                         profile.get("tool_ids") or [], "python_name"),
        "data_sources": _labels("agent_data_sources", "source_id",
                                profile.get("data_source_ids") or [], "name"),
        "mcp_servers": _labels("agent_mcp_servers", "mcp_server_id",
                               profile.get("mcp_server_ids") or [], "name"),
        "mcp_attachments": mcp_attachments,
        "models": _labels("agent_models", "model_id",
                          [m for m in (profile.get("model_main_id"),
                                       profile.get("model_light_id")) if m],
                          "name"),
        "prompt_pins": profile.get("prompt_version_ids") or {},
        "agent_type": _agent_type_label(client, profile.get("agent_type_id")),
    }


def _agent_type_label(client, agent_type_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """The agent's category as {slug, display_name, color}, or None."""
    if not agent_type_id:
        return None
    row = get_row(client, "agent_types", "agent_type_id", agent_type_id)
    return ({k: row.get(k) for k in ("slug", "display_name", "color")}
            if row else None)


@mcp.tool()
@tool_errors
def update_profile(profile_id: str,
                   display_name: Optional[str] = None,
                   description: Optional[str] = None,
                   row_environment: RowEnv = "",
                   environment: Optional[str] = None,
                   status: Optional[str] = None,
                   model_main_id: Optional[str] = None,
                   model_light_id: Optional[str] = None,
                   endpoint_path: Optional[str] = None,
                   is_active: Optional[bool] = None,
                   delegatable: Optional[bool] = None,
                   prompt_version_ids: Optional[Dict[str, str]] = None,
                   voice_config: Optional[Dict[str, Any]] = None,
                   voice_config_patch: Optional[Dict[str, Any]] = None,
                   tool_ids: Optional[List[str]] = None,
                   data_source_ids: Optional[List[str]] = None,
                   mcp_server_ids: Optional[List[str]] = None,
                   downstream_profile_ids: Optional[List[str]] = None,
                   agent_type: Annotated[Optional[str], Field(
                       description="Put the agent in this category: a "
                                   "category slug from "
                                   "agent_types(action='list'). '' or 'none' "
                                   "takes it out of its category. Omit to "
                                   "leave the category unchanged.")] = None,
                   env: Env = "staging") -> Dict[str, Any]:
    """Update an agent profile's config. Only the fields you pass change.
    List fields replace the stored list. To add or remove one tool, KB
    source, MCP server, skill or sub-agent, use profile_attachments instead.
    Changes are audited. Returns {updated, profile_id, changed, profile}.

    - env: the config branch the profile is looked up in.
    - row_environment: moves the profile to another environment. This is
      rarely what you want; use promote_profile. environment is its
      deprecated alias.
    - status: active, draft, archived or specialist. template is refused;
      copy a template with templates(action="use"). Setting active is refused
      while another active, enabled profile serves the same agent_kind and
      environment. Deactivate or archive that one first.
    - delegatable: expose this profile to its parent as an ask_<slug>
      delegation tool, or stop exposing it.
    - agent_type: the agent's category, by slug (manage categories with
      agent_types). It is organisational only, applies at once to this agent
      in every environment and needs no promote.
    - voice_config: replaces the whole voice config. Every key you do not
      send is removed, with no warning.
    - voice_config_patch: merges into the current voice config. Nested
      objects merge key by key, a null value deletes that key, and scalars
      and lists are replaced. Prefer this to change one setting.

    Voice settings are flat, top-level keys (for example barge_in_enabled,
    answer_watchdog_sec). A top-level "live" object is refused in both voice
    arguments, because it would hide every other voice setting. Passing both
    voice arguments in one call is refused."""
    # Maintainer notes (kept out of the description clients read):
    # The 'template' status is seeder-managed. Two active rows are refused
    # because the core loader has no deterministic tie-break between them.
    # voice_config_patch goes through
    # admin.services.profiles.deep_merge_voice_config; a row still in the old
    # nested voice shape is promoted to flat before the merge, so patching also
    # repairs it.
    from admin.services.profiles import (DualActiveProfileError,
                                         ProfileNotFoundError,
                                         TemplateProtectedError,
                                         deep_merge_voice_config,
                                         update_profile_row)
    from voice.live.config import promote_legacy_voice_config
    environment = resolve_alias(row_environment, environment,
                                new_name="row_environment",
                                old_name="environment") or None
    if voice_config is not None and voice_config_patch is not None:
        return err("pass either voice_config (full replace) or "
                   "voice_config_patch (deep merge), not both")
    # A nested `live` block is refused rather than written. voice_config is flat
    # since 2026-08-17 and the read shim resolves a nested block ALONE, so this
    # would not set one key — it would hide every flat one, silently, on a live
    # tenant. An error is recoverable; that is not.
    for arg_name, arg in (("voice_config", voice_config),
                          ("voice_config_patch", voice_config_patch)):
        if isinstance(arg, dict) and isinstance(arg.get("live"), dict):
            return err(
                f"{arg_name} carries a nested 'live' block; voice_config is "
                "flat since 2026-08-17. Send the keys at the top level — a "
                "'live' sub-object hides every flat key from the runtime "
                "instead of adding to them"
            )
    resolved_voice_config = voice_config
    if voice_config_patch is not None:
        client = client_for_env(env)
        current = get_row(client, "agent_profiles", "profile_id", profile_id)
        if not current:
            return err("profile not found")
        resolved_voice_config = deep_merge_voice_config(
            # Promote first: merging a flat patch onto an unmigrated nested row
            # would leave the patched keys unread behind the compat shim.
            promote_legacy_voice_config(current.get("voice_config")),
            voice_config_patch)
    payload = {k: v for k, v in {
        "display_name": display_name, "description": description,
        "environment": environment, "status": status,
        "model_main_id": model_main_id,
        "model_light_id": model_light_id, "endpoint_path": endpoint_path,
        "is_active": is_active, "delegatable": delegatable,
        "prompt_version_ids": prompt_version_ids,
        "voice_config": resolved_voice_config, "tool_ids": tool_ids,
        "data_source_ids": data_source_ids, "mcp_server_ids": mcp_server_ids,
        "downstream_profile_ids": downstream_profile_ids,
    }.items() if v is not None}
    if agent_type is not None:
        # '' / 'none' clears it, so this one cannot ride the "None means not
        # supplied" filter above. An unknown slug is refused (ValueError).
        from admin.services.agent_types import resolve_type_id
        payload["agent_type_id"] = resolve_type_id(agent_type, client_for_env(env))
    if not payload:
        return err("no fields supplied")
    try:
        with use_env(validate_env(env)):
            after = update_profile_row(profile_id=profile_id, payload=payload,
                                       actor_user_id=current_actor()["user_id"])
    except ProfileNotFoundError:
        return err("profile not found")
    except TemplateProtectedError as exc:
        return err(str(exc))
    except DualActiveProfileError as exc:
        return err(str(exc))
    return {"updated": True, "profile_id": profile_id, "changed": sorted(payload),
            "profile": after}


@mcp.tool()
@tool_errors
def fork_profile(profile_id: str, env: Env = "staging") -> Dict[str, Any]:
    """Copy a profile into a new draft in staging. The slug gets an
    automatic suffix. env is the branch the source profile is read from.

    The copy keeps the source's model, prompt version pins (the same
    versions, not copies), tool, KB and MCP label lists, sub-agents, endpoint,
    and voice and memory config. It does not copy MCP attachments (what binds
    tools at runtime) or platform skills; re-attach them with
    profile_attachments. The copy starts with status=draft and
    delegatable=false, so it serves nothing until it is activated or
    promoted. Returns {forked, profile_id, slug, environment}."""
    from admin.services.profiles import ProfileNotFoundError, fork_profile_row
    try:
        with use_env(validate_env(env)):
            new_row = fork_profile_row(profile_id=profile_id,
                                       actor_user_id=current_actor()["user_id"])
    except ProfileNotFoundError:
        return err("profile not found")
    return {"forked": True, "profile_id": new_row["profile_id"],
            "slug": new_row.get("slug"), "environment": new_row["environment"]}


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def diff_profiles(profile_a: str = "", profile_b: str = "",
                  env: Env = "staging",
                  ids: Annotated[Optional[List[str]], Field(
                      description="The pair as [a, b] profile ids, same "
                                  "as profile_a/profile_b.")] = None,
                  ) -> Dict[str, Any]:
    """Compare two profiles field by field, a to b: scalar fields, tools,
    data sources, MCP servers, sub-agents and prompt versions. Pass the pair
    as ids=[a, b] or as profile_a and profile_b. Returns {a, b, diff}."""
    from admin.profile_fork_handlers import compute_profile_diff
    if ids:
        if len(ids) != 2:
            return err("ids must hold exactly two profile ids [a, b]")
        profile_a, profile_b = ids
    if not (profile_a and profile_b):
        return err("pass ids=[a, b] (or profile_a and profile_b)")
    client = client_for_env(env)
    found = rows(client.table("agent_profiles").select("*")
                 .in_("profile_id", [profile_a, profile_b]).execute())
    by_id = {r["profile_id"]: r for r in found}
    if profile_a not in by_id or profile_b not in by_id:
        return err("one or both profiles not found")
    return {"a": profile_a, "b": profile_b,
            "diff": compute_profile_diff(client, by_id[profile_a], by_id[profile_b])}


@mcp.tool()
@tool_errors
def profile_versions(action: Literal["list", "view", "save", "restore", "pin",
                                     "diff"],
                     profile_id: str, version_id: str = "", notes: str = "",
                     env: Env = "staging",
                     ids: Annotated[Optional[List[str]], Field(
                         description="diff: [version_a, version_b] to diff two "
                                     "saved versions (instead of version_id vs "
                                     "the live row).")] = None,
                     include_detail: Annotated[bool, Field(
                         description="diff: also return the full structured "
                                     "diff (default: summary lines only).")] = False,
                     ) -> Dict[str, Any]:
    """Version history and the served-version pin of one profile in one
    environment. Required: action and profile_id.

    Actions:
    - list: versions in env, newest first: [{version_id, version_number,
      promoted_at, notes, is_served}].
    - view (version_id): that version's config snapshot. The version must
      belong to profile_id.
    - save (notes optional): save the profile's current config as a new
      version. Does not change what is served. Returns {version_id,
      version_number}.
    - restore (version_id): copy that version's config back onto the
      editable profile. Kind, slug, name and description are not touched.
      References to tools, data sources, MCP servers, models or prompt
      versions that no longer exist in this env are left out and listed in
      dropped. Does not change the served version; save afterwards to make a
      new version. Returns {restored, dropped}.
    - pin (version_id, or "" to clear): set or clear the exact version
      served for this profile and env. This changes what callers get.
      Clearing returns to the default. Returns {served_version_id}.
    - diff (version_id, or ids=[version_a, version_b]): compare a version
      with the current profile, or two saved versions. Returns {changes:
      ["model_main_id: 'x' -> 'y'", "tools: +a, -b", ...]}.
      include_detail=true adds the full structured diff.

    Writes are audited. Errors, such as a version that belongs to another
    profile, come back as {error, hint}."""
    # Maintainer notes (kept out of the description clients read):
    # Not annotated READ_ONLY: list/view/diff read, save/restore/pin write.
    # Delegates to admin.services.profile_versions so the portal's version bar
    # and this tool stay in step. Writes run inside use_env(env) and are audited
    # by that service itself, so each write produces exactly one audit row.
    # `save` uses the same runtime.compiler.compile_profile_snapshot as promote;
    # `pin` sets agent_profiles.served_version_id.
    from admin.services.profile_versions import (diff_version, list_versions,
                                                  pin_served_version,
                                                  restore_version, save_version,
                                                  summarize_profile_diff)

    if action == "diff":
        if ids:
            if len(ids) != 2:
                return err("ids must hold exactly two version ids [a, b]")
            a, b = ids
        elif version_id:
            a, b = version_id, None
        else:
            return err("diff needs version_id (vs live) or ids=[a, b]")
        with use_env(env):
            diff = diff_version(profile_id, a, b)
        out = {"profile_id": profile_id, "a": a, "b": b or "live",
               "changes": summarize_profile_diff(diff)}
        if include_detail:
            out["detail"] = diff
        return out

    if action == "list":
        with use_env(env):
            versions = list_versions(profile_id, env)
        return {"profile_id": profile_id, "env": env, "versions": versions}

    if action == "view":
        if not version_id:
            return err("view needs version_id")
        from runtime.compiler import load_snapshot_by_version
        with use_env(env):
            entry = load_snapshot_by_version(version_id)
        if not entry:
            return err("version not found")
        if entry.get("profile_id") != profile_id:
            return err(f"profile version {version_id} does not belong to "
                       f"profile {profile_id}")
        return {"version_id": version_id, "profile_id": profile_id,
                "environment": entry.get("environment"),
                "snapshot": entry.get("snapshot")}

    if action == "save":
        with use_env(env):
            return save_version(profile_id, env,
                                actor_user_id=current_actor()["user_id"],
                                notes=notes or None)

    if action == "restore":
        if not version_id:
            return err("restore needs version_id")
        with use_env(env):
            return restore_version(profile_id, env, version_id,
                                   actor_user_id=current_actor()["user_id"])

    if action == "pin":
        with use_env(env):
            return pin_served_version(profile_id, env, version_id or None,
                                      actor_user_id=current_actor()["user_id"])

    return err(f"unknown action {action!r}",
               hint="list | view | save | restore | pin | diff")


def _validation_refusal(
    content: str, *, name: Optional[str],
    override_validation: bool, confirm: bool,
    audit_entity_type: str, audit_entity_id: str,
    audit_context: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Runs core.prompt_validator.validate_prompt and returns an err()
    refusal dict if an error-severity problem exists and the caller has not
    overridden it — else None (clear to write). Shared by create_version,
    serve and publish_from_repo's confirm branch so a prompt cannot become
    the served version through ANY of the three paths without passing the
    same gate, and so a fix to the gate reaches every path at once.

    `name` is the canonical repo-constant name (e.g. "SUPPORT_SYSTEM_PROMPT")
    when the caller can supply one — only publish_from_repo can, since it
    resolves one from a dotted `constant` path. create_version and serve
    accept arbitrary content with no such provenance, so they pass
    name=None: validate_prompt still runs its universal ask_agent check (the
    rule this task exists for is content-shaped, not indexed by name), but
    the size-budget and bilingual-parity registries — which ARE indexed by
    name — do not fire, and the validator reports that rather than silently
    looking clean.

    An override is a DELIBERATE act: it requires override_validation=true
    AND confirm=true together, and is recorded via actor_audit — with the
    full problem list — so a forced publish stays visible in the audit log
    even though the write it enables succeeds."""
    from core.prompt_validator import has_errors, to_dicts, validate_prompt
    problems = validate_prompt(content, name=name)
    problem_dicts = to_dicts(problems)
    if not has_errors(problems):
        return None
    if not (override_validation and confirm):
        return err(
            "prompt failed validation and was NOT written",
            hint="fix the flagged problem(s), or re-call with "
                 "override_validation=true AND confirm=true to force it "
                 "through anyway (recorded in the audit log)",
            problems=problem_dicts,
        )
    actor_audit(audit_entity_type, audit_entity_id, "validation_override",
               after={"problems": problem_dicts, **audit_context})
    return None


def _insert_prompt_version(client, prompt_id: str, content: str,
                           notes: Optional[str], actor_user_id: str) -> Dict[str, Any]:
    """Shared next-version insert: computes version_number, invalidates the
    resolver cache, and audits — the guts of prompts(action='create_version'),
    reused by publish_from_repo's confirm path so the two can't drift."""
    last = rows(client.table("agent_prompt_versions").select("version_number")
               .eq("prompt_id", prompt_id)
               .order("version_number", desc=True).limit(1).execute())
    next_number = (last[0]["version_number"] + 1) if last else 1
    new_row = {"prompt_id": prompt_id, "version_number": next_number,
              "content": content, "notes": notes,
              "created_by": actor_user_id}
    inserted = rows(client.table("agent_prompt_versions").insert(new_row).execute())
    created = inserted[0] if inserted else new_row
    from core.prompt_resolver import PromptResolver
    PromptResolver.invalidate()
    actor_audit("agent_prompt_versions",
               created.get("prompt_version_id", prompt_id), "create",
               after=created)
    return created


def _serve_prompt_version(client, profile: Dict[str, Any], prompt_key: str,
                          version_id: str) -> Dict[str, str]:
    """Shared pin-and-repoint: sets `prompt_version_ids[prompt_key]`,
    invalidates both the profile loader and the prompt resolver, and
    audits — the guts of prompts(action='serve'), reused by
    publish_from_repo's confirm path so the two can't drift. Returns the
    new pins dict."""
    pins = dict(profile.get("prompt_version_ids") or {})
    old = dict(pins)
    pins[prompt_key] = version_id
    client.table("agent_profiles").update({"prompt_version_ids": pins}) \
        .eq("profile_id", profile["profile_id"]).execute()
    from core.prompt_resolver import PromptResolver
    from admin.services.profiles import invalidate_profile_loader
    invalidate_profile_loader(profile["agent_kind"], profile["environment"])
    PromptResolver.invalidate()
    actor_audit("agent_profiles", profile["profile_id"], "serve_prompt_version",
               before={"prompt_version_ids": old}, after={"prompt_version_ids": pins})
    return pins


# publish_from_repo may only import modules under this package — the guard
# against reading an arbitrary attribute/secret off an unrelated import
# (env vars, other constants, ...) via a caller-supplied dotted path.
_PUBLISH_ALLOWED_PACKAGE = "runtime.prompts"


def _prompt_diff_stat(old: Optional[str], new: str) -> Dict[str, Any]:
    """Small diff summary for a publish_from_repo dry-run — sizes and the
    first differing line number, NEVER the full body: these prompts are
    half Arabic and a client round-trip is exactly what CLAUDE.md warns
    corrupts them, so the dry-run must never carry the text itself."""
    old = old or ""
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    first_diff_line = None
    for i, (a, b) in enumerate(zip(old_lines, new_lines), start=1):
        if a != b:
            first_diff_line = i
            break
    else:
        if len(old_lines) != len(new_lines):
            first_diff_line = min(len(old_lines), len(new_lines)) + 1
    return {
        "chars_before": len(old), "chars_after": len(new),
        "chars_delta": len(new) - len(old),
        "first_differing_line": first_diff_line,
    }


def _resolve_publish_constant(constant: str) -> Any:
    """Import `constant` (a dotted `module.path.ATTR` string) and return its
    value, or an err() dict on any validation failure. Module must be
    `runtime.prompts` itself or a submodule of it (`_PUBLISH_ALLOWED_PACKAGE`)
    — this is what lets publish_from_repo import a caller-named constant at
    all without becoming a read-anything-in-the-process gadget; the
    attribute must also be public (no leading underscore)."""
    if "." not in constant:
        return err("constant must be a dotted path, e.g. "
                   "runtime.prompts.agents_demo.SUPPORT_SYSTEM_PROMPT")
    module_path, _, attr_name = constant.rpartition(".")
    if not (module_path == _PUBLISH_ALLOWED_PACKAGE
            or module_path.startswith(_PUBLISH_ALLOWED_PACKAGE + ".")):
        return err(f"constant must be under the {_PUBLISH_ALLOWED_PACKAGE!r} "
                   "package", hint="e.g. runtime.prompts.agents_demo.SUPPORT_SYSTEM_PROMPT")
    if attr_name.startswith("_"):
        return err("constant must not be a private/dunder attribute")
    import importlib
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        return err(f"could not import {module_path!r}: {exc}")
    if not hasattr(module, attr_name):
        return err(f"{module_path} has no attribute {attr_name!r}")
    value = getattr(module, attr_name)
    if not isinstance(value, str):
        return err(f"{constant} is not a string constant "
                   f"(got {type(value).__name__})")
    return value


_PREVIEW_CHARS = 160


def _preview(text: Optional[str]) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS - 1] + "…"


def _resolve_prompt_version(client, *, version_id: str, prompt_id: str,
                            version_number: int, profile_id: str,
                            prompt_key: str) -> Dict[str, Any]:
    """One agent_prompt_versions row, addressed any of three ways (in this
    order): version_id | prompt_id + version_number (0 = newest) |
    profile_id + prompt_key (the version that profile SERVES). Returns the
    row or an err() dict."""
    if version_id:
        row = get_row(client, "agent_prompt_versions", "prompt_version_id", version_id)
        return row or err("prompt version not found")
    if prompt_id:
        q = (client.table("agent_prompt_versions").select("*")
             .eq("prompt_id", prompt_id))
        if version_number:
            q = q.eq("version_number", version_number)
        found = rows(q.order("version_number", desc=True).limit(1).execute())
        return found[0] if found else err(
            "prompt version not found", hint="prompts action=list_versions")
    if profile_id:
        profile = get_row(client, "agent_profiles", "profile_id", profile_id)
        if not profile:
            return err("profile not found")
        pins = profile.get("prompt_version_ids") or {}
        pinned = pins.get(prompt_key)
        if not pinned:
            return err(f"profile pins no {prompt_key!r} prompt",
                       hint=f"pinned keys: {sorted(pins)}")
        row = get_row(client, "agent_prompt_versions", "prompt_version_id", pinned)
        return row or err("pinned prompt version row not found")
    return err("pass version_id, prompt_id (+ version_number), or "
               "profile_id (+ prompt_key)")


@mcp.tool()
@tool_errors
def prompts(action: Literal["get", "list", "list_versions", "create_version",
                            "serve", "diff", "publish_from_repo"],
            prompt_id: str = "", env: Env = "staging",
            content: str = "", notes: str = "",
            profile_id: str = "", prompt_key: str = "system",
            version_id: str = "",
            version_number: Annotated[int, Field(
                description="get: version number within prompt_id "
                            "(0 = newest).")] = 0,
            agent_kind: Annotated[str, Field(
                description="list: list every prompt slot of this agent_kind.")] = "",
            version_a: int = 0, version_b: int = 0,
            ids: Annotated[Optional[List[str]], Field(
                description="diff: the pair as [version_id_a, version_id_b] "
                            "(prompt_version_id UUIDs).")] = None,
            include_content: Annotated[bool, Field(
                description="diff: also return both full bodies (default: "
                            "unified diff + stats only).")] = False,
            max_chars: Annotated[int, Field(
                description="get: truncate content to this many chars "
                            "(0 = full text).")] = 0,
            constant: str = "", slug: str = "",
            confirm: bool = False,
            override_validation: bool = False) -> Dict[str, Any]:
    """Read and manage prompt text and prompt versions. Required: action.

    Actions:
    - get: the full text of one version. Select it by version_id, by
      prompt_id (plus version_number, default newest), or by profile_id and
      prompt_key (the version that profile serves). Returns {prompt_id, slug,
      agent_kind, version_number, prompt_version_id, content, notes, chars,
      truncated}. max_chars truncates content.
    - list: short previews. profile_id lists that profile's pinned prompts
      (key, version, preview). agent_kind lists every prompt of that kind
      with its newest version. prompt_id lists that prompt's versions.
    - list_versions (prompt_id): versions without content.
    - create_version (prompt_id, content; notes optional): add a version.
      For a custom agent pass profile_id and content instead: its own prompt
      is used and the new version is pinned in the same call.
    - serve (profile_id, version_id; prompt_key, default "system"): pin a
      version on a profile. This changes what the agent says.
    - diff: ids=[version_id_a, version_id_b], or prompt_id with version_a
      and version_b numbers. Returns a unified diff (at most 400 lines) and
      diff_stat. include_content=true adds both texts.
    - publish_from_repo (constant, profile_id, and prompt_id or slug):
      publish a prompt constant from the engine's code. constant is a dotted
      path under runtime.prompts, e.g.
      "runtime.prompts.agents_demo.SUPPORT_SYSTEM_PROMPT". Without prompt_id,
      the prompt is found by the profile's agent_kind and slug. prompt_key
      (default "system") is the pin to repoint. If the served text already
      equals the constant, returns {already_current: true} and writes
      nothing. Without confirm it is a dry run: the current version,
      diff_stat (sizes and the first differing line, never the prompt text)
      and validation problems. With confirm=true it adds a new version, pins
      it and returns {published: true, ...}. If notes is empty, the served
      version's notes are carried over.

    Validation: create_version, serve and a confirmed publish_from_repo check
    the prompt first. A problem of error severity refuses the write and
    returns {error, problems}; nothing is added or pinned.
    override_validation=true forces the write. It only works together with
    confirm=true and is audited. publish_from_repo runs the full checks for
    the named constant (size and both-language rules); create_version and
    serve run only the general checks.

    To rewrite a prompt from a failing eval run, use prompt_optimize."""
    # Maintainer notes (kept out of the description clients read):
    # publish_from_repo imports the constant in-process, so a body with
    # non-ASCII text never passes through the MCP client or a shell, where
    # quoting could corrupt it. Its slug lookup uses the same (agent_kind, slug)
    # key as seed_prompts.py. Notes are carried from the served version so a
    # chat prompt fix never reuses another profile's notes by accident.
    # Every write path runs core.prompt_validator.validate_prompt. `serve` is
    # gated on purpose: pinning an existing bad version is the same failure as
    # publishing a new one. Overrides are recorded with actor_audit
    # (action="validation_override", with the full problem list).
    client = client_for_env(env)
    if action == "get":
        row = _resolve_prompt_version(
            client, version_id=version_id, prompt_id=prompt_id,
            version_number=version_number, profile_id=profile_id,
            prompt_key=prompt_key)
        if "error" in row:
            return row
        prompt = get_row(client, "agent_prompts", "prompt_id", row.get("prompt_id")) or {}
        text = row.get("content") or ""
        out = {"prompt_id": row.get("prompt_id"), "slug": prompt.get("slug"),
               "agent_kind": prompt.get("agent_kind"),
               "prompt_version_id": row.get("prompt_version_id"),
               "version_number": row.get("version_number"),
               "notes": row.get("notes"), "created_at": row.get("created_at"),
               "chars": len(text),
               "content": text[:max_chars] if max_chars > 0 else text,
               "truncated": bool(max_chars > 0 and len(text) > max_chars)}
        if profile_id:
            out["profile_id"] = profile_id
            out["prompt_key"] = prompt_key
        return out
    if action == "list":
        if profile_id:
            profile = get_row(client, "agent_profiles", "profile_id", profile_id)
            if not profile:
                return err("profile not found")
            pins = profile.get("prompt_version_ids") or {}
            by_id = {r["prompt_version_id"]: r for r in rows(
                client.table("agent_prompt_versions").select("*")
                .in_("prompt_version_id", [v for v in pins.values() if v])
                .execute())} if pins else {}
            items = []
            for key, vid in sorted(pins.items()):
                r = by_id.get(vid) or {}
                items.append({"prompt_key": key, "prompt_version_id": vid,
                              "prompt_id": r.get("prompt_id"),
                              "version_number": r.get("version_number"),
                              "chars": len(r.get("content") or ""),
                              "preview": _preview(r.get("content"))})
            return {"profile_id": profile_id, "served_prompts": items,
                    "hint": "prompts action=get profile_id=... prompt_key=... "
                            "for the full text"}
        if agent_kind:
            slots = rows(client.table("agent_prompts").select("*")
                         .eq("agent_kind", agent_kind).order("slug").execute())
            items = []
            for p in slots:
                newest = rows(client.table("agent_prompt_versions").select("*")
                              .eq("prompt_id", p["prompt_id"])
                              .order("version_number", desc=True).limit(1).execute())
                n = newest[0] if newest else {}
                items.append({"prompt_id": p["prompt_id"], "slug": p.get("slug"),
                              "description": p.get("description"),
                              "latest_version": n.get("version_number"),
                              "latest_version_id": n.get("prompt_version_id"),
                              "preview": _preview(n.get("content"))})
            return {"agent_kind": agent_kind, "prompts": items}
        if prompt_id:
            versions = rows(client.table("agent_prompt_versions").select("*")
                            .eq("prompt_id", prompt_id)
                            .order("version_number", desc=True).limit(100).execute())
            return {"prompt_id": prompt_id, "versions": [
                {"prompt_version_id": v.get("prompt_version_id"),
                 "version_number": v.get("version_number"),
                 "notes": v.get("notes"), "created_at": v.get("created_at"),
                 "chars": len(v.get("content") or ""),
                 "preview": _preview(v.get("content"))} for v in versions]}
        return err("list needs profile_id, agent_kind or prompt_id")
    if action == "list_versions":
        versions = list_rows(client, "agent_prompt_versions",
                             select="prompt_version_id, prompt_id, version_number, "
                                    "notes, created_at, created_by",
                             filters={"prompt_id": prompt_id},
                             order_by="version_number", limit=100)
        return {"prompt_id": prompt_id, "versions": versions}
    if action == "create_version":
        # A generic (custom) agent owns its instructions, so for one of those
        # `profile_id` + `content` is the whole call: its own prompt is found
        # (or created on first use) and the new version is pinned on it in the
        # same step — one call to change what the agent says, with no prompt
        # id to look up and no separate `serve`.
        own_profile = None
        if not prompt_id and profile_id and content:
            candidate = get_row(client, "agent_profiles", "profile_id", profile_id)
            if candidate and candidate.get("agent_kind") == "custom":
                own_profile = candidate
        if own_profile is None and not (prompt_id and content):
            return err("create_version needs prompt_id and content",
                       hint="for a custom agent, pass profile_id and content instead")
        refusal = _validation_refusal(
            content, name=None,
            override_validation=override_validation, confirm=confirm,
            audit_entity_type="agent_prompt_versions",
            audit_entity_id=prompt_id or profile_id,
            audit_context={"action": "create_version", "prompt_id": prompt_id,
                           "profile_id": profile_id},
        )
        if refusal:
            return refusal
        if own_profile is not None:
            # After validation, so a refused write leaves no empty prompt row.
            from admin.services import custom_prompts
            prompt_id = custom_prompts.ensure_own_prompt(
                client, own_profile, actor_user_id=current_actor()["user_id"])["prompt_id"]
        created = _insert_prompt_version(client, prompt_id, content,
                                         notes or None, current_actor()["user_id"])
        from core.prompt_validator import to_dicts, validate_prompt
        out = {"created": True, "version_number": created.get("version_number"),
               "prompt_version_id": created.get("prompt_version_id"),
               "validation": {"problems": to_dicts(validate_prompt(content, name=None))}}
        if own_profile is not None and created.get("prompt_version_id"):
            fresh = get_row(client, "agent_profiles", "profile_id", profile_id) or own_profile
            _serve_prompt_version(client, fresh, "system", created["prompt_version_id"])
            out.update(pinned=True, prompt_id=prompt_id)
        return out
    if action == "serve":
        if not (profile_id and version_id):
            return err("serve needs profile_id and version_id")
        profile = get_row(client, "agent_profiles", "profile_id", profile_id)
        if not profile:
            return err("profile not found")
        version_row = get_row(client, "agent_prompt_versions",
                              "prompt_version_id", version_id)
        if not version_row:
            return err("prompt version not found")
        refusal = _validation_refusal(
            version_row.get("content") or "", name=None,
            override_validation=override_validation, confirm=confirm,
            audit_entity_type="agent_profiles", audit_entity_id=profile_id,
            audit_context={"action": "serve", "prompt_key": prompt_key,
                          "version_id": version_id},
        )
        if refusal:
            return refusal
        _serve_prompt_version(client, profile, prompt_key, version_id)
        from core.prompt_validator import to_dicts, validate_prompt
        return {"pinned": True, "prompt_key": prompt_key, "version_id": version_id,
                "validation": {"problems": to_dicts(
                    validate_prompt(version_row.get("content") or "", name=None))}}
    if action == "diff":
        if ids:
            if len(ids) != 2:
                return err("ids must hold exactly two prompt_version_ids [a, b]")
            row_a = get_row(client, "agent_prompt_versions", "prompt_version_id", ids[0])
            row_b = get_row(client, "agent_prompt_versions", "prompt_version_id", ids[1])
            if not (row_a and row_b):
                return err("version not found", hint="prompts action=list")
        else:
            found = rows(client.table("agent_prompt_versions").select("*")
                         .eq("prompt_id", prompt_id)
                         .in_("version_number", [version_a, version_b]).execute())
            by_num = {r["version_number"]: r for r in found}
            if version_a not in by_num or version_b not in by_num:
                return err("version not found", hint="prompts action=list_versions")
            row_a, row_b = by_num[version_a], by_num[version_b]
        from admin.prompt_handlers import build_unified_diff
        text_a, text_b = row_a.get("content") or "", row_b.get("content") or ""
        diff_rows = build_unified_diff(
            text_a, text_b,
            label_a=f"v{row_a.get('version_number')}",
            label_b=f"v{row_b.get('version_number')}")
        marks = {"add": "+", "del": "-", "ctx": " "}
        lines = [marks.get(r["kind"], "") + r["text"] for r in diff_rows]
        cap = 400

        def _meta(r):
            return {k: r.get(k) for k in ("prompt_version_id", "prompt_id",
                                          "version_number", "notes", "created_at")}
        out = {"a": _meta(row_a), "b": _meta(row_b),
               "diff_stat": {**_prompt_diff_stat(text_a, text_b),
                             "lines_added": sum(1 for r in diff_rows if r["kind"] == "add"),
                             "lines_removed": sum(1 for r in diff_rows if r["kind"] == "del")},
               "unified_diff": "\n".join(lines[:cap]),
               "diff_truncated": len(lines) > cap}
        if include_content:
            out["a"]["content"] = text_a
            out["b"]["content"] = text_b
        return out
    if action == "publish_from_repo":
        if not constant:
            return err("publish_from_repo needs constant (dotted path under "
                       "runtime.prompts)")
        value = _resolve_publish_constant(constant)
        if isinstance(value, dict):  # _resolve_publish_constant's err() shape
            return value

        profile = None
        if profile_id:
            profile = get_row(client, "agent_profiles", "profile_id", profile_id)
            if not profile:
                return err("profile not found")

        resolved_prompt_id = prompt_id
        if not resolved_prompt_id:
            if not slug:
                return err("publish_from_repo needs prompt_id, or profile_id + slug")
            if not profile:
                return err("publish_from_repo needs profile_id to resolve "
                           "slug to a prompt_id")
            found = rows(client.table("agent_prompts").select("prompt_id")
                        .eq("agent_kind", profile["agent_kind"]).eq("slug", slug)
                        .limit(1).execute())
            if not found:
                return err(f"no agent_prompts row for "
                          f"({profile['agent_kind']!r}, {slug!r})")
            resolved_prompt_id = found[0]["prompt_id"]

        if not profile_id:
            return err("publish_from_repo needs profile_id (for the repoint)")
        if profile is None:
            profile = get_row(client, "agent_profiles", "profile_id", profile_id)
            if not profile:
                return err("profile not found")

        pins = profile.get("prompt_version_ids") or {}
        served_version_id = pins.get(prompt_key)
        served_version = (get_row(client, "agent_prompt_versions",
                                  "prompt_version_id", served_version_id)
                          if served_version_id else None)
        served_content = served_version.get("content") if served_version else None

        if served_version is not None and served_content == value:
            return {"already_current": True, "prompt_id": resolved_prompt_id,
                    "version_id": served_version_id,
                    "version_number": served_version.get("version_number")}

        # The canonical registry name — publish_from_repo is the one action
        # that actually knows it, since it resolved `value` from a dotted
        # `constant` path. This is what lets its validation run the FULL
        # check (size budget + bilingual parity, not just the universal
        # ask_agent rule create_version/serve are limited to).
        canonical_name = constant.rpartition(".")[-1]
        from core.prompt_validator import to_dicts, validate_prompt
        problems = to_dicts(validate_prompt(value, name=canonical_name))

        if not confirm:
            return {
                "dry_run": True, "prompt_id": resolved_prompt_id,
                "current_version_id": served_version_id,
                "current_version_number": (served_version.get("version_number")
                                           if served_version else None),
                "content_differs": True,
                "diff_stat": _prompt_diff_stat(served_content, value),
                "validation": {"problems": problems},
                "hint": "re-call with confirm=true to insert a new version "
                       "and repoint",
            }

        refusal = _validation_refusal(
            value, name=canonical_name,
            override_validation=override_validation, confirm=confirm,
            audit_entity_type="agent_prompts", audit_entity_id=resolved_prompt_id,
            audit_context={"action": "publish_from_repo", "constant": constant,
                          "profile_id": profile_id, "prompt_key": prompt_key},
        )
        if refusal:
            return refusal

        carried_notes = notes or (served_version.get("notes")
                                  if served_version else None)
        created = _insert_prompt_version(client, resolved_prompt_id, value,
                                         carried_notes, current_actor()["user_id"])
        new_version_id = created.get("prompt_version_id")
        _serve_prompt_version(client, profile, prompt_key, new_version_id)
        return {"published": True, "prompt_id": resolved_prompt_id,
                "prompt_key": prompt_key, "version_id": new_version_id,
                "version_number": created.get("version_number"),
                "notes": carried_notes, "validation": {"problems": problems}}
    return err(f"unknown action {action!r}",
               hint="get | list | list_versions | create_version | serve | "
                   "diff | publish_from_repo")


@mcp.tool()
@tool_errors
async def prompt_optimize(
        prompt_id: Annotated[str, Field(description="Prompt slot to rewrite "
                                                    "(its NEWEST version is the input).")],
        run_id: Annotated[str, Field(description="Eval run whose FAILING "
                                                 "scenarios drive the rewrite.")],
        model_id: Annotated[str, Field(description="Model UUID of the chat "
                                                   "model that writes the rewrite.")],
        env: Env = "staging",
        confirm: Annotated[bool, Field(
            description="false (default) = return the proposal only. true = "
                        "also save it as a NEW prompt version (not served; pin "
                        "it with prompts action=serve).")] = False,
        notes: Annotated[str, Field(description="Notes for the saved version.")] = "",
        override_validation: Annotated[bool, Field(
            description="Save even if the proposal fails prompt validation "
                        "(needs confirm=true; audited).")] = False,
        include_content: Annotated[bool, Field(
            description="Return the proposed prompt text (default true).")] = True,
) -> Dict[str, Any]:
    """Ask a model to rewrite a prompt so that a failing eval run passes.
    Required: prompt_id, run_id (a run with failing scenarios) and model_id
    (the chat model that writes the rewrite). The prompt's newest version and
    the run's failing scenarios are the input. Takes as long as one model
    call.

    Returns {proposal: {content, chars}, diff_stat (against the newest
    version), validation: {problems}, failing_scenarios}. With confirm=true
    the proposal is also saved as a new version, after the same validation as
    prompts(action="create_version"). It is not served until you pin it with
    prompts(action="serve")."""
    # Maintainer notes (kept out of the description clients read):
    # Non-streaming twin of the portal's /admin/prompts/{id}/optimize, using
    # the same helpers (build_optimizer_meta_prompt). The proposal is saved
    # in-process so a non-ASCII body never round-trips through the client.
    import json as _json

    from admin.prompt_handlers import (_failing_scenarios_from_run,
                                       _stream_optimizer,
                                       build_optimizer_meta_prompt)
    from core.prompt_validator import to_dicts, validate_prompt
    client = client_for_env(env)
    if not get_row(client, "agent_prompts", "prompt_id", prompt_id):
        return err("prompt not found", hint="prompts action=list agent_kind=...")
    run = get_row(client, "agent_eval_runs", "run_id", run_id)
    if not run:
        return err("eval run not found")
    failing = _failing_scenarios_from_run(run)
    if not failing:
        return err("selected run has no failing scenarios — pick a run that "
                   "actually regressed something")
    latest = rows(client.table("agent_prompt_versions").select("*")
                  .eq("prompt_id", prompt_id)
                  .order("version_number", desc=True).limit(1).execute())
    if not latest:
        return err("prompt has no versions yet — write a v1 before optimizing")
    current = latest[0].get("content") or ""
    model_row = get_row(client, "agent_models", "model_id", model_id)
    if not model_row:
        return err("model not found")
    meta_prompt = build_optimizer_meta_prompt(current_prompt_body=current,
                                              failing_scenarios=failing)
    proposal = None
    async for frame in _stream_optimizer(model_row, meta_prompt):
        payload = _json.loads(frame[len("data: "):].strip())
        if payload.get("type") == "error":
            return err(f"optimizer failed: {payload.get('content')}")
        if payload.get("type") == "done":
            proposal = payload.get("content") or ""
    if not proposal:
        return err("optimizer returned no text")
    problems = to_dicts(validate_prompt(proposal, name=None))
    out: Dict[str, Any] = {
        "prompt_id": prompt_id, "based_on_version": latest[0].get("version_number"),
        "run_id": run_id, "failing_scenarios": len(failing),
        "proposal": {"chars": len(proposal),
                     **({"content": proposal} if include_content else {})},
        "diff_stat": _prompt_diff_stat(current, proposal),
        "validation": {"problems": problems},
    }
    if not confirm:
        out["hint"] = ("re-call with confirm=true to save it as a new version "
                       "(it is not served until prompts action=serve)")
        return out
    refusal = _validation_refusal(
        proposal, name=None, override_validation=override_validation,
        confirm=confirm, audit_entity_type="agent_prompt_versions",
        audit_entity_id=prompt_id,
        audit_context={"action": "optimize", "prompt_id": prompt_id,
                       "run_id": run_id})
    if refusal:
        return {**out, **refusal}
    created = _insert_prompt_version(
        client, prompt_id, proposal,
        notes or f"optimized from eval run {run_id}", current_actor()["user_id"])
    out.update({"saved": True, "prompt_version_id": created.get("prompt_version_id"),
                "version_number": created.get("version_number")})
    return out


@mcp.tool()
@tool_errors
def models(action: Literal["list", "get", "create", "update", "promote"],
           model_id: str = "", env: Env = "staging",
           name: str = "", provider: str = "", model_string: str = "",
           temperature: Optional[float] = None,
           max_output_tokens: Optional[int] = None,
           extra_config: Optional[Dict[str, Any]] = None,
           is_active: Optional[bool] = None,
           target_env: Literal["", "staging", "prod"] = "") -> Dict[str, Any]:
    """Model registry. Required: action.

    Actions:
    - list: the models in env.
    - get (model_id).
    - create (name, provider, model_string; optional temperature,
      max_output_tokens, extra_config, is_active).
    - update (model_id plus the fields to change).
    - promote (model_id, target_env): copy the model to the other
      environment. If a model with the same name already exists there,
      nothing is written and its id is returned."""
    client = client_for_env(env)
    if action == "list":
        return {"models": list_rows(client, "agent_models",
                                    filters={"environment": env}, limit=200)}
    if action == "get":
        row = get_row(client, "agent_models", "model_id", model_id)
        return {"model": row} if row else err("model not found")
    if action in ("create", "update"):
        if provider:
            from runtime.llm_factory import SUPPORTED_PROVIDERS, UNSUPPORTED_PROVIDERS
            if provider in UNSUPPORTED_PROVIDERS or provider not in SUPPORTED_PROVIDERS:
                return err(f"provider {provider!r} is not supported",
                           hint=f"supported: {sorted(SUPPORTED_PROVIDERS)}")
        payload = {k: v for k, v in {
            "name": name or None, "provider": provider or None,
            "model_string": model_string or None,
            "temperature": temperature, "max_output_tokens": max_output_tokens,
            "extra_config": extra_config, "is_active": is_active,
        }.items() if v is not None}
        if action == "create":
            payload.setdefault("environment", env)
            inserted = rows(client.table("agent_models").insert(payload).execute())
            created = inserted[0] if inserted else payload
            actor_audit("agent_models", created.get("model_id", ""), "create",
                        after=created)
            return {"created": True, "model": created}
        before = get_row(client, "agent_models", "model_id", model_id)
        if not before:
            return err("model not found")
        client.table("agent_models").update(payload).eq("model_id", model_id).execute()
        actor_audit("agent_models", model_id, "update",
                    before=before, after={**before, **payload})
        return {"updated": True, "model_id": model_id, "changed": sorted(payload)}
    if action == "promote":
        if target_env not in ("staging", "prod"):
            return err("promote needs target_env staging|prod")
        src = get_row(client, "agent_models", "model_id", model_id)
        if not src:
            return err("model not found")
        existing = rows(client.table("agent_models").select("model_id")
                        .eq("name", src["name"]).eq("environment", target_env)
                        .limit(1).execute())
        if existing:
            return {"promoted": False, "already_exists": True,
                    "model_id": existing[0]["model_id"]}
        new_row = {"name": src["name"], "environment": target_env,
                   "provider": src.get("provider"),
                   "model_string": src.get("model_string"),
                   "temperature": src.get("temperature"),
                   "max_output_tokens": src.get("max_output_tokens"),
                   "extra_config": src.get("extra_config") or {},
                   "is_active": src.get("is_active", True)}
        inserted = rows(client.table("agent_models").insert(new_row).execute())
        new_id = inserted[0].get("model_id") if inserted else None
        actor_audit("agent_models", new_id or model_id, "CONFIG_PROMOTE",
                    before=src, after=new_row)
        return {"promoted": True, "model_id": new_id}
    return err(f"unknown action {action!r}",
               hint="list | get | create | update | promote")


@mcp.tool()
@tool_errors
async def provider_keys(
        action: Annotated[Literal["list", "set", "remove", "test"], Field(
            description="list = where each provider's key comes from; set = "
                        "store or replace a key; remove = delete a stored key; "
                        "test = one free call to the provider with the key in use.")],
        provider: Annotated[str, Field(
            description="google_genai | openai | anthropic | azure_openai. "
                        "Required for set, remove and test.")] = "",
        value: Annotated[str, Field(
            description="set: the key to store. It is encrypted and never "
                        "returned by any tool.")] = "",
        name: Annotated[str, Field(
            description="'' = the provider's default key. A name stores a second, "
                        "named credential that a model selects with "
                        "extra_config.credential.")] = "",
        env: Env = "staging",
        confirm: Confirm = False) -> Dict[str, Any]:
    """Model provider API keys, stored encrypted. A stored key is used
    before the provider's environment variable (GOOGLE_API_KEY,
    OPENAI_API_KEY, ANTHROPIC_API_KEY, AZURE_OPENAI_API_KEY). No action ever
    returns a key: only its hint (last four characters), who set it and when.

    Actions:
    - list: per provider, the source (portal, environment or none), the
      stored default key and named credentials. can_store is false, with the
      reason in problem, when the server has no SECRET_KEY.
    - set (provider, value; optional name): store or replace a key. Refused
      when SECRET_KEY is unset; a key is never stored as plain text.
    - remove (provider; optional name; confirm=true): delete a stored key.
      The provider falls back to its environment variable.
    - test (provider; optional name): ask the provider for its model list.
      Returns {ok, source, detail}, with the provider's own error on failure."""
    import asyncio

    from admin.services import provider_keys as service
    from core import provider_keys as store

    validate_env(env)
    client = client_for_env(env)
    if action == "list":
        return await asyncio.to_thread(service.key_status, env=env, client=client)
    if not provider.strip():
        return err("provider is required for set | remove | test",
                   hint=" | ".join(sorted(store.PROVIDERS)))
    actor = current_actor()["user_id"]
    try:
        if action == "set":
            row = await asyncio.to_thread(
                service.set_key, provider, value, actor, name or None, env=env, client=client)
            return {"stored": True, "key": row}
        if action == "remove":
            if not confirm:
                return not_confirmed(
                    f"remove the stored {provider} key"
                    + (f" named {name!r}" if name else "")
                    + "; the provider falls back to its environment variable, if set")
            removed = await asyncio.to_thread(
                service.remove_key, provider, actor, name or None, env=env, client=client)
            return {"removed": True, "provider": provider, "name": name} if removed else err(
                "no stored key for this provider" + (f" under the name {name!r}" if name else ""))
        if action == "test":
            return await asyncio.to_thread(
                service.check_key, provider, name or None, env=env, client=client)
    except store.SecretKeyMissing as exc:
        return err(str(exc), hint="Nothing was stored. Set SECRET_KEY on the engine and restart it.")
    except service.ProviderKeyError as exc:
        return err(str(exc))
    return err(f"unknown action {action!r}", hint="list | set | remove | test")


@mcp.tool()
@tool_errors
def voice_budgets(action: Literal["list", "get", "set", "delete"],
                  business_id: str = "",
                  daily_seconds: Optional[int] = None,
                  note: str = "",
                  env: Env = "staging") -> Dict[str, Any]:
    """Daily voice-call allowance per tenant, in audio seconds per day. The
    allowance is per tenant, not per model. Required: action.

    Actions:
    - list: every allowance, each with used_today, plus the engine default.
    - get (business_id).
    - set (business_id, daily_seconds; optional note): create or update an
      allowance. business_id may be any tenant id; one that is not a UUID is
      mapped to a stable UUID.
    - delete (business_id): the tenant falls back to the engine default
      (VOICE_LIVE_DAILY_SECONDS)."""
    # Maintainer notes (kept out of the description clients read):
    # Table ai_voice_budgets. Mirrors the portal's Duplex-voice budgets card
    # (POST /admin/voice-budgets is the portal's write route). A non-UUID
    # business_id maps to the same derived row the voice edge charges.
    import uuid as _uuid
    from datetime import datetime, timezone

    client = client_for_env(env)

    def _resolve_bid(raw: str) -> str:
        try:
            return str(_uuid.UUID(raw.strip()))
        except (ValueError, AttributeError):
            from endpoints.voice_live import _ANON_BUDGET_NAMESPACE
            return str(_uuid.uuid5(
                _ANON_BUDGET_NAMESPACE, f"voice-live-business:{raw.strip()}"
            ))

    if action == "list":
        budget_rows = list_rows(client, "ai_voice_budgets",
                                order_by="business_id", desc=False, limit=200)
        today = datetime.now(timezone.utc).date().isoformat()
        usage = {r["business_id"]: r.get("seconds_used") or 0 for r in rows(
            client.table("ai_voice_usage_daily")
            .select("business_id, seconds_used").eq("usage_date", today).execute()
        )}
        for r in budget_rows:
            r["used_today"] = usage.get(r["business_id"], 0)
        from voice.live.budget import DEFAULT_DAILY_SECONDS
        return {"budgets": budget_rows,
                "default_daily_seconds": DEFAULT_DAILY_SECONDS}
    if not business_id:
        return err("business_id is required for get|set|delete")
    bid = _resolve_bid(business_id)
    if action == "get":
        row = get_row(client, "ai_voice_budgets", "business_id", bid)
        return {"budget": row} if row else err(
            "no allowance for this tenant (engine default applies)")
    if action == "set":
        if daily_seconds is None or daily_seconds < 0:
            return err("set needs daily_seconds >= 0")
        payload = {"business_id": bid, "daily_seconds": int(daily_seconds),
                   "note": note.strip() or None,
                   "updated_at": datetime.now(timezone.utc).isoformat()}
        client.table("ai_voice_budgets").upsert(
            payload, on_conflict="business_id").execute()
        actor_audit("ai_voice_budgets", bid, "upsert",
                    after={**payload, "raw_business_id": business_id})
        return {"set": True, "budget": payload}
    if action == "delete":
        before = get_row(client, "ai_voice_budgets", "business_id", bid)
        if not before:
            return err("no allowance for this tenant")
        client.table("ai_voice_budgets").delete().eq("business_id", bid).execute()
        actor_audit("ai_voice_budgets", bid, "delete", before=before)
        return {"deleted": True, "business_id": bid}
    return err(f"unknown action {action!r}", hint="list | get | set | delete")


@mcp.tool()
@tool_errors
def tools(action: Literal["list", "get", "create", "update", "delete", "dry_run"],
          tool_id: str = "", env: Env = "staging",
          python_name: str = "", display_name: str = "", description: str = "",
          category: str = "", scope: str = "", agent_kind: str = "",
          additional_agent_kinds: Optional[List[str]] = None,
          endpoint_url: str = "", http_method: str = "",
          auth_env_var: Optional[str] = None,
          timeout_seconds: Optional[int] = None,
          argument_schema: Optional[Dict[str, Any]] = None,
          usage_guidance: str = "",
          context_fields: Optional[Dict[str, Any]] = None,
          args: Optional[Dict[str, Any]] = None,
          confirm: bool = False) -> Dict[str, Any]:
    """Tool registry. Only HTTP tools (kind="http") can be created, changed
    or deleted here; Python tools are defined in code. Required: action.

    Actions:
    - list.
    - get (tool_id).
    - create (python_name, description, endpoint_url, agent_kind,
      argument_schema; optional display_name, category, scope (read or
      write, default read), http_method (default POST), auth_env_var,
      timeout_seconds (1-300, default 30), additional_agent_kinds).
    - update (tool_id plus the fields to change): the HTTP settings and/or
      display_name, description, category, scope, additional_agent_kinds.
    - delete (tool_id, confirm=true): marks the tool deprecated. The row is
      kept.
    - dry_run: call a tool once to test it. Pass tool_id for a saved tool,
      or endpoint_url, http_method, auth_env_var, timeout_seconds and
      argument_schema for an unsaved one. args holds the sample arguments.

    usage_guidance (text shown to the model with the tool) and context_fields
    (a JSON object) are optional on create and update. They are stored as
    given."""
    from admin.tool_handlers import (_HTTP_METHODS, _PYTHON_NAME_RE,
                                     _SCOPE_OPTIONS, _parse_additional_kinds,
                                     _tool_agent_kinds)
    client = client_for_env(env)
    if action == "list":
        return {"tools": list_rows(client, "agent_tools", limit=300)}
    if action == "get":
        row = get_row(client, "agent_tools", "tool_id", tool_id)
        return {"tool": row} if row else err("tool not found")
    if action == "dry_run":
        from admin.tool_handlers import run_tool_dry_run
        base: Dict[str, Any] = {}
        if tool_id:
            base = get_row(client, "agent_tools", "tool_id", tool_id) or {}
            if not base:
                return err("tool not found")
        dry_payload = {
            "endpoint_url": endpoint_url or base.get("endpoint_url", ""),
            "http_method": http_method or base.get("http_method", "POST"),
            "auth_env_var": auth_env_var if auth_env_var is not None
                           else base.get("auth_env_var"),
            "timeout_seconds": timeout_seconds if timeout_seconds is not None
                              else base.get("timeout_seconds", 30),
            "argument_schema": argument_schema if argument_schema is not None
                               else base.get("argument_schema") or {},
            "args": args or {},
        }
        return run_tool_dry_run(dry_payload, actor_user_id=current_actor()["user_id"])
    if action == "create":
        name = python_name.strip().lower()
        if not _PYTHON_NAME_RE.match(name):
            return err("python_name must be lowercase + underscores, 2-63 "
                       "chars, starting with a letter")
        if not description.strip():
            return err("description is required — this is what the LLM sees")
        url = endpoint_url.strip()
        if not url.lower().startswith(("http://", "https://")):
            return err("endpoint_url must be an absolute http(s) URL")
        method = (http_method or "POST").strip().upper()
        if method not in _HTTP_METHODS:
            return err(f"http_method must be one of {_HTTP_METHODS}")
        scope_val = (scope or "read").strip().lower()
        if scope_val not in _SCOPE_OPTIONS:
            return err(f"scope must be one of {_SCOPE_OPTIONS}")
        ak = (agent_kind or "").strip()
        if ak not in _tool_agent_kinds():
            return err(f"agent_kind must be one of {list(_tool_agent_kinds())}")
        # Mirror the portal's _validate_argument_schema: a schema is required
        # (an explicit {} object is fine; omission is not), must be a JSON
        # object, and its root type — if given — must be "object".
        if argument_schema is None:
            return err("argument_schema is required")
        schema = argument_schema
        if not isinstance(schema, dict):
            return err("argument_schema must be a JSON object")
        if schema.get("type") and schema["type"] != "object":
            return err("argument_schema root type must be 'object'")
        try:
            timeout = int(timeout_seconds) if timeout_seconds is not None else 30
            if not (1 <= timeout <= 300):
                raise ValueError
        except (TypeError, ValueError):
            return err("timeout_seconds must be an integer between 1 and 300")
        payload = {
            "python_name": name,
            "display_name": display_name.strip() or name,
            "description": description.strip(),
            "category": (category or "http").strip(),
            "scope": scope_val,
            "agent_kind": ak,
            "additional_agent_kinds": _parse_additional_kinds(
                ",".join(additional_agent_kinds or [])),
            "kind": "http",
            "endpoint_url": url,
            "http_method": method,
            "auth_env_var": (auth_env_var or "").strip() or None,
            "timeout_seconds": timeout,
            "argument_schema": schema,
            "source_file": "__portal__",
            "is_deprecated": False,
        }
        if usage_guidance.strip():
            payload["usage_guidance"] = usage_guidance.strip()
        if context_fields is not None:
            payload["context_fields"] = context_fields
        inserted = rows(client.table("agent_tools").insert(payload).execute())
        created = inserted[0] if inserted else payload
        actor_audit("agent_tools", created.get("tool_id", ""), "create", after=created)
        from core.agent_profile import AgentProfileLoader
        AgentProfileLoader.invalidate()
        return {"created": True, "tool": created}
    if action == "update":
        before = get_row(client, "agent_tools", "tool_id", tool_id)
        if not before:
            return err("tool not found")
        payload = {}
        # HTTP-config fields — only meaningful for kind="http" tools, mirrors
        # tool_http_edit's refusal of Python tools.
        http_touched = any([endpoint_url, http_method, auth_env_var is not None,
                            timeout_seconds is not None, argument_schema is not None])
        if http_touched:
            if before.get("kind") != "http":
                return err("only HTTP tools are editable here")
            url = (endpoint_url or before.get("endpoint_url") or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                return err("endpoint_url must be an absolute http(s) URL")
            method = (http_method or before.get("http_method") or "POST").strip().upper()
            if method not in _HTTP_METHODS:
                return err(f"http_method must be one of {_HTTP_METHODS}")
            try:
                timeout = (int(timeout_seconds) if timeout_seconds is not None
                          else int(before.get("timeout_seconds") or 30))
                if not (1 <= timeout <= 300):
                    raise ValueError
            except (TypeError, ValueError):
                return err("timeout_seconds must be an integer between 1 and 300")
            schema = (argument_schema if argument_schema is not None
                     else before.get("argument_schema") or {})
            if not isinstance(schema, dict):
                return err("argument_schema must be a JSON object")
            if schema.get("type") and schema["type"] != "object":
                return err("argument_schema root type must be 'object'")
            payload.update({
                "endpoint_url": url, "http_method": method,
                "auth_env_var": (auth_env_var if auth_env_var is not None
                                else before.get("auth_env_var")) or None,
                "timeout_seconds": timeout, "argument_schema": schema,
            })
        # Metadata fields — mirror tool_update (applies to any tool kind).
        for field_name, value in (("display_name", display_name),
                                  ("description", description),
                                  ("category", category)):
            if value:
                payload[field_name] = value.strip()
        if scope:
            scope_val = scope.strip().lower()
            if scope_val not in _SCOPE_OPTIONS:
                return err(f"scope must be one of {_SCOPE_OPTIONS}")
            payload["scope"] = scope_val
        if additional_agent_kinds is not None:
            payload["additional_agent_kinds"] = _parse_additional_kinds(
                ",".join(additional_agent_kinds))
        if usage_guidance.strip():
            payload["usage_guidance"] = usage_guidance.strip()
        if context_fields is not None:
            payload["context_fields"] = context_fields
        if not payload:
            return err("no fields supplied")
        client.table("agent_tools").update(payload).eq("tool_id", tool_id).execute()
        actor_audit("agent_tools", tool_id, "update", before=before,
                    after={**before, **payload})
        from core.agent_profile import AgentProfileLoader
        AgentProfileLoader.invalidate()
        return {"updated": True, "tool_id": tool_id, "changed": sorted(payload)}
    if action == "delete":
        before = get_row(client, "agent_tools", "tool_id", tool_id)
        if not before:
            return err("tool not found")
        if before.get("kind") != "http":
            return err("only HTTP tools can be deleted here")
        if not confirm:
            return not_confirmed(f"deprecate tool {before.get('python_name')!r}")
        client.table("agent_tools").update({"is_deprecated": True}) \
            .eq("tool_id", tool_id).execute()
        actor_audit("agent_tools", tool_id, "deprecate", before=before,
                    after={**before, "is_deprecated": True})
        from core.agent_profile import AgentProfileLoader
        AgentProfileLoader.invalidate()
        return {"deleted": True, "tool_id": tool_id}
    return err(f"unknown action {action!r}",
               hint="list | get | create | update | delete | dry_run")


_MCP_BUILTIN_ERROR = "built-in MCPs are managed by the seeder"


def _mcp_load_members(client, mcp_server_id: str) -> List[Dict[str, Any]]:
    """Bundle membership (agent_mcp_tools) joined to agent_tools for
    python_name, ordered by position. Slimmed mirror of
    admin/mcp_handlers.py::_load_members ({tool_id, python_name, position}
    instead of the full merged tool row — this is a summary for the MCP
    caller, not a form-picker render)."""
    member_rows = rows(
        client.table("agent_mcp_tools").select("tool_id, position")
        .eq("mcp_server_id", mcp_server_id).order("position").execute())
    if not member_rows:
        return []
    tool_ids = [m["tool_id"] for m in member_rows]
    tool_rows = rows(client.table("agent_tools").select("tool_id, python_name")
                     .in_("tool_id", tool_ids).execute())
    names_by_id = {t["tool_id"]: t.get("python_name") for t in tool_rows}
    return [
        {"tool_id": m["tool_id"], "python_name": names_by_id.get(m["tool_id"]),
         "position": m.get("position") or 0}
        for m in sorted(member_rows, key=lambda r: r.get("position") or 0)
    ]


def _mcp_valid_tool_ids(client) -> set:
    """Non-deprecated agent_tools ids — the same set
    admin/mcp_manage_handlers.py::_load_tools_for_picker (and its
    admin/mcp_handlers.py copy) validate bundle membership against."""
    return {t["tool_id"] for t in rows(
        client.table("agent_tools").select("tool_id")
        .eq("is_deprecated", False).execute())}


def _mcp_discover(endpoint: str, auth_env_var: Optional[str]) -> Dict[str, Any]:
    """Live tools/list probe of an external MCP endpoint. Mirrors
    admin/mcp_manage_handlers.py::mcp_discover's SSRF guard and
    never-raise {"ok": bool} envelope exactly. Read-only: no DB access,
    no audit trail — a discover call never persists anything."""
    ep = (endpoint or "").strip()
    if not ep or not ep.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "endpoint must be an absolute http(s) URL",
                "error_class": "ConfigError", "tools": []}

    from runtime.tools._http_adapter import _classify_endpoint
    try:
        classification = _classify_endpoint(ep)
    except Exception as exc:  # noqa: BLE001 — malformed URL, never raise
        return {"ok": False, "error": f"could not parse endpoint: {exc}",
                "error_class": "ConfigError", "tools": []}

    allow_local = os.getenv("MCP_ALLOW_LOCAL_ENDPOINTS", "").strip().lower() \
        in {"1", "true", "yes", "on"}
    if classification == "blocked" and not allow_local:
        return {"ok": False,
                "error": "endpoint resolves to a loopback, link-local, or "
                         "cloud-metadata address and was blocked",
                "error_class": "ConfigError", "tools": []}

    # Never forward the secret to a target we didn't classify as public —
    # same rule build_http_tool applies to operator-authored HTTP tools.
    auth_token = (os.getenv(auth_env_var)
                  if (auth_env_var and classification == "public") else None)

    from runtime.tools._mcp_adapter import MCPError, list_tools_cached
    try:
        found = list_tools_cached(ep, auth_token)
    except MCPError as exc:
        return {"ok": False, "error": str(exc), "error_class": "MCPError", "tools": []}
    except Exception as exc:  # noqa: BLE001 — never leak a stack to the caller
        return {"ok": False, "error": f"discover failed: {exc}",
                "error_class": type(exc).__name__, "tools": []}

    slim = [
        {"name": t.get("name"), "description": t.get("description"),
         "inputSchema": t.get("inputSchema")}
        for t in (found or []) if isinstance(t, dict) and t.get("name")
    ]
    return {"ok": True, "tools": slim, "error": None, "error_class": None}


@mcp.tool()
@tool_errors
def mcp_servers(action: Literal["list", "get", "create", "update", "delete",
                                "members", "discover"],
                 mcp_server_id: str = "", env: Env = "staging",
                 name: str = "", source: str = "", transport: str = "",
                 endpoint: str = "", auth_env_var: Optional[str] = None,
                 tool_subset: Optional[List[str]] = None,
                 context_fields: Optional[Dict[str, Any]] = None,
                 usage_guidance: str = "",
                 member_tool_ids: Optional[List[str]] = None,
                 is_active: Optional[bool] = None,
                 timeout_seconds: Optional[int] = None,
                 confirm: bool = False) -> Dict[str, Any]:
    """Registry of the MCP servers that agents get tools from. A server is
    either external (source="external": a remote MCP endpoint reached over
    transport and endpoint) or a bundle (source="bundle": a named group of
    registered tools, with no endpoint of its own). Built-in bundles are
    read-only: update, delete and members refuse them. Names starting with
    "__builtin__:" are reserved. Required: action.

    Actions:
    - list: every server of both kinds.
    - get (mcp_server_id): the server, its members ([{tool_id, python_name,
      position}]) and attached_count (profiles that use it).
    - create (source, name): external also needs endpoint; optional
      transport (stdio, sse or http; default http), auth_env_var,
      tool_subset, context_fields. A bundle takes optional usage_guidance,
      context_fields and member_tool_ids (its tools, in order; each must be a
      registered tool that is not deprecated).
    - update (mcp_server_id plus fields): name, transport, endpoint,
      auth_env_var, usage_guidance, tool_subset, context_fields, is_active.
    - delete (mcp_server_id, confirm=true): detaches the server from every
      profile, then deletes it and its membership.
    - members (mcp_server_id, member_tool_ids): replace a bundle's tool
      list, in the given order. Bundles only.
    - discover (endpoint; optional auth_env_var): connect to a candidate
      external endpoint and list its tools. Writes nothing. Loopback,
      link-local and cloud-metadata hosts are blocked unless
      MCP_ALLOW_LOCAL_ENDPOINTS is set. Always returns {ok, tools, error,
      error_class}."""
    # Maintainer notes (kept out of the description clients read):
    # Tables: agent_mcp_servers, agent_mcp_tools (bundle membership) and
    # agent_profile_mcp_servers (attachments). Built-in bundles (is_builtin) are
    # seeder-managed and refused with the same message the portal uses. The
    # server-level tool_subset is the legacy filter; per-profile subsets live on
    # the attachment.
    if action == "discover":
        return _mcp_discover(endpoint, auth_env_var)

    from admin.mcp_handlers import TRANSPORT_OPTIONS
    client = client_for_env(env)

    if action == "list":
        return {"mcp_servers": list_rows(client, "agent_mcp_servers", limit=200)}

    if action == "get":
        row = get_row(client, "agent_mcp_servers", "mcp_server_id", mcp_server_id)
        if not row:
            return err("mcp server not found")
        attached_count = len(rows(
            client.table("agent_profile_mcp_servers").select("profile_id")
            .eq("mcp_server_id", mcp_server_id).execute()))
        return {"mcp_server": row,
                "members": _mcp_load_members(client, mcp_server_id),
                "attached_count": attached_count}

    if action == "create":
        if source not in ("bundle", "external"):
            return err('source must be "bundle" or "external"')
        if name.startswith("__builtin__:"):
            return err("The __builtin__: prefix is reserved for "
                       "seeder-managed MCPs")
        if source == "external":
            if not (name and endpoint):
                return err("external create needs name and endpoint")
            transport_val = transport or "http"
            if transport_val not in TRANSPORT_OPTIONS:
                return err(f"transport must be one of {TRANSPORT_OPTIONS}")
            payload = {
                "name": name, "source": "external", "is_builtin": False,
                "transport": transport_val, "endpoint": endpoint,
                "auth_env_var": auth_env_var or None,
                "tool_subset": tool_subset,
                "context_fields": context_fields or {},
                "is_active": True,
            }
        else:  # bundle
            if not name:
                return err("bundle create needs name")
            payload = {
                "name": name, "source": "bundle", "is_builtin": False,
                "usage_guidance": usage_guidance or None,
                "context_fields": context_fields or {},
                "is_active": True,
            }
        if source == "bundle" and member_tool_ids:
            valid_ids = _mcp_valid_tool_ids(client)
            bad = [t for t in member_tool_ids if t not in valid_ids]
            if bad:
                return err(f"unknown or deprecated tool ids: {bad}")
        # agent_mcp_servers has UNIQUE(name): a duplicate (double-submit or a
        # real collision) surfaces from PostgREST as an exception — route it
        # to a clean err() instead of raising through, same as the portal's
        # mcp_connect_create/mcp_create_bundle handlers.
        try:
            inserted = rows(client.table("agent_mcp_servers").insert(payload).execute())
        except Exception as exc:  # noqa: BLE001 — mirrors portal friendly-error behavior
            return err("An MCP with that name already exists, or the write failed",
                       detail=str(exc))
        created = inserted[0] if inserted else payload
        mcp_id = created.get("mcp_server_id")
        if source == "bundle" and member_tool_ids:
            # Seed membership through the shared reconcile (one upsert, not a
            # per-row insert loop) so a mid-seed failure can't leave a
            # half-built bundle. Ids are already validated above.
            from admin.mcp_membership import reconcile_bundle_members
            reconcile_bundle_members(client, mcp_id, member_tool_ids)
        actor_audit("agent_mcp_servers", mcp_id or "", "create", after=created)
        return {"created": True, "mcp_server": created, "source": source}

    if action == "update":
        before = get_row(client, "agent_mcp_servers", "mcp_server_id", mcp_server_id)
        if not before:
            return err("mcp server not found")
        if before.get("is_builtin"):
            return err(_MCP_BUILTIN_ERROR)
        if name and name.startswith("__builtin__:"):
            return err("The __builtin__: prefix is reserved for "
                       "seeder-managed MCPs")
        payload = {k: v for k, v in {
            "name": name or None, "transport": transport or None,
            "endpoint": endpoint or None, "auth_env_var": auth_env_var or None,
            "usage_guidance": usage_guidance or None,
        }.items() if v is not None}
        if tool_subset is not None:
            payload["tool_subset"] = tool_subset
        if context_fields is not None:
            payload["context_fields"] = context_fields
        if is_active is not None:
            payload["is_active"] = is_active
        if not payload:
            return err("no fields supplied")
        # Same UNIQUE(name) concern as create — a rename that collides
        # surfaces from PostgREST as an exception.
        try:
            client.table("agent_mcp_servers").update(payload) \
                .eq("mcp_server_id", mcp_server_id).execute()
        except Exception as exc:  # noqa: BLE001 — mirrors portal friendly-error behavior
            return err("An MCP with that name already exists, or the write failed",
                       detail=str(exc))
        actor_audit("agent_mcp_servers", mcp_server_id, "update",
                    before=before, after={**before, **payload})
        return {"updated": True, "mcp_server_id": mcp_server_id,
                "changed": sorted(payload)}

    if action == "delete":
        before = get_row(client, "agent_mcp_servers", "mcp_server_id", mcp_server_id)
        if not before:
            return err("mcp server not found")
        if before.get("is_builtin"):
            return err(_MCP_BUILTIN_ERROR)
        if not confirm:
            return not_confirmed(
                f"delete MCP server {before.get('name')!r} and detach it "
                "from every profile")
        all_profiles = rows(client.table("agent_profiles").select("*").execute())
        for profile in all_profiles:
            ids = list(profile.get("mcp_server_ids") or [])
            if mcp_server_id in ids:
                updated_ids = [i for i in ids if i != mcp_server_id]
                client.table("agent_profiles").update(
                    {"mcp_server_ids": updated_ids}
                ).eq("profile_id", profile["profile_id"]).execute()
        client.table("agent_mcp_tools").delete() \
            .eq("mcp_server_id", mcp_server_id).execute()
        client.table("agent_profile_mcp_servers").delete() \
            .eq("mcp_server_id", mcp_server_id).execute()
        client.table("agent_mcp_servers").delete() \
            .eq("mcp_server_id", mcp_server_id).execute()
        actor_audit("agent_mcp_servers", mcp_server_id, "delete",
                    before=before, after=None)
        return {"deleted": True, "mcp_server_id": mcp_server_id}

    if action == "members":
        before = get_row(client, "agent_mcp_servers", "mcp_server_id", mcp_server_id)
        if not before:
            return err("mcp server not found")
        if before.get("is_builtin"):
            return err(_MCP_BUILTIN_ERROR)
        if before.get("source") != "bundle":
            return err("membership editing is only available for bundle MCPs")

        # Dedupe while preserving first-occurrence order, mirroring
        # mcp_server_members_update: a duplicated id can't collide on the
        # (mcp_server_id, tool_id) PK during re-insert.
        seen: set = set()
        member_ids: List[str] = []
        for t in (member_tool_ids or []):
            if t and t not in seen:
                seen.add(t)
                member_ids.append(t)

        valid_ids = _mcp_valid_tool_ids(client)
        bad = [t for t in member_ids if t not in valid_ids]
        if bad:
            return err(f"unknown or deprecated tool ids: {bad}")

        # Upsert-then-delete-stale: the bundle's tool set is never emptied
        # mid-write (see admin/mcp_membership.py). Returns the prior members
        # for an honest audit before.
        from admin.mcp_membership import reconcile_bundle_members
        before_members = reconcile_bundle_members(client, mcp_server_id, member_ids)
        actor_audit("agent_mcp_servers", mcp_server_id, "update",
                    before={"member_tool_ids": before_members},
                    after={"member_tool_ids": member_ids})
        return {"updated": True, "members": member_ids}

    return err(f"unknown action {action!r}",
               hint="list | get | create | update | delete | members | discover")


@mcp.tool()
@tool_errors
def kb_sources(action: Literal["list", "get", "create", "update", "delete",
                               "upload_document"],
               source_id: str = "", env: Env = "staging",
               name: str = "", kind: str = "", agent_kind: str = "",
               config: Optional[Dict[str, Any]] = None,
               refresh_cadence: str = "",
               freshness_sla_hours: Optional[int] = None,
               is_active: Optional[bool] = None,
               confirm: bool = False,
               filename: str = "", content: str = "", title: str = "",
               page_type: str = "user_guide", language: str = "en") -> Dict[str, Any]:
    """Knowledge base (KB) sources and document upload. Required: action.

    Actions:
    - list: every source, plus embedding (the embedding model in use).
    - get (source_id).
    - create (name, kind; optional agent_kind, config, refresh_cadence,
      freshness_sla_hours, is_active): kind is pgvector, ms_drive, gitbook,
      supabase_table or http. A pgvector source without config.table uses
      the shared KB tables.
    - update (source_id plus the fields to change).
    - delete (source_id, confirm=true): detaches the source from every
      profile, then deletes it permanently.
    - upload_document (source_id, filename, content; optional title,
      page_type, language): store one document and queue chunking and
      embedding in the background. pgvector sources only. Refused, with what
      to set, when no embedding model is configured. Returns {queued, job_id,
      page_id}; poll with jobs(action="get")."""
    from admin.kb_handlers import KB_KINDS, _is_shared_kb, embedding_status, upload_blocker
    client = client_for_env(env)
    if action == "list":
        return {"sources": list_rows(client, "agent_data_sources", limit=200),
                # The model that makes documents searchable on THIS engine,
                # and what to set when there is none.
                "embedding": embedding_status()}
    if action == "get":
        row = get_row(client, "agent_data_sources", "source_id", source_id)
        return {"source": row} if row else err("source not found")
    if action in ("create", "update"):
        if kind and kind not in KB_KINDS:
            return err(f"kind must be one of {KB_KINDS}")
        payload = {k: v for k, v in {
            "name": name or None, "kind": kind or None,
            "agent_kind": agent_kind or None, "config": config,
            "refresh_cadence": refresh_cadence or None,
            "freshness_sla_hours": freshness_sla_hours,
            "is_active": is_active,
        }.items() if v is not None}
        if action == "create":
            if not (payload.get("name") and payload.get("kind")):
                return err("create needs name and kind")
            if payload.get("kind") == "pgvector" and not (payload.get("config") or {}).get("table"):
                payload["config"] = {**(payload.get("config") or {}),
                                     "table": "ai_kb_embeddings",
                                     "pages_table": "ai_kb_pages"}
            payload.setdefault("config", {})
            inserted = rows(client.table("agent_data_sources").insert(payload).execute())
            created = inserted[0] if inserted else payload
            actor_audit("agent_data_sources", created.get("source_id", ""),
                        "create", after=created)
            return {"created": True, "source": created}
        if not payload:
            return err("no fields supplied")
        before = get_row(client, "agent_data_sources", "source_id", source_id)
        if not before:
            return err("source not found")
        client.table("agent_data_sources").update(payload) \
            .eq("source_id", source_id).execute()
        actor_audit("agent_data_sources", source_id, "update",
                    before=before, after={**before, **payload})
        return {"updated": True, "source_id": source_id, "changed": sorted(payload)}
    if action == "delete":
        before = get_row(client, "agent_data_sources", "source_id", source_id)
        if not before:
            return err("source not found")
        if not confirm:
            return not_confirmed(
                f"delete KB source {before.get('name')!r} and detach it "
                "from every profile")
        all_profiles = rows(client.table("agent_profiles").select("*").execute())
        for profile in all_profiles:
            ids = list(profile.get("data_source_ids") or [])
            if source_id in ids:
                updated_ids = [i for i in ids if i != source_id]
                client.table("agent_profiles").update(
                    {"data_source_ids": updated_ids}
                ).eq("profile_id", profile["profile_id"]).execute()
        client.table("agent_data_sources").delete().eq("source_id", source_id).execute()
        actor_audit("agent_data_sources", source_id, "delete",
                    before=before, after=None)
        return {"deleted": True, "source_id": source_id}
    if action == "upload_document":
        if not (source_id and filename and content):
            return err("upload_document needs source_id, filename, content")
        src = get_row(client, "agent_data_sources", "source_id", source_id)
        if not src:
            return err("source not found")
        if src.get("kind") != "pgvector":
            return err("uploads only apply to pgvector sources")
        cfg = src.get("config") or {}
        blocker = upload_blocker(client, source_id, cfg)
        if blocker:
            return err(blocker)
        pages_table = cfg.get("pages_table", "ai_kb_pages")
        embeddings_table = cfg.get("table", "ai_kb_embeddings")
        page = {"title": title or filename, "type": page_type,
                "language": language, "content": content,
                "source": "admin_upload"}
        if _is_shared_kb(cfg):
            page["source_id"] = source_id
        inserted = rows(client.table(pages_table).insert(page).execute())
        page_id = inserted[0].get("id") if inserted else None
        from admin._jobs import start_tracked_job
        from admin.kb_handlers import _kb_ingest_worker
        actor = current_actor()
        job_id = start_tracked_job(
            _kb_ingest_worker, kind="kb_upload",
            label=f"kb:{(src.get('name') or '')[:32]}",
            actor_user_id=actor["user_id"],
            related_entity_type="agent_data_sources",
            related_entity_id=source_id,
            metadata={"file_count": 1, "embeddings_table": embeddings_table,
                      "pages_table": pages_table},
            source_id=source_id, table_name=embeddings_table,
            page_ids_by_filename={filename: page_id},
            file_contents_by_filename={filename: content},
            page_titles_by_filename={filename: title or filename},
            page_type=page_type, language=language,
            max_tokens=int(cfg.get("chunk_max_tokens", 512)),
            overlap_tokens=int(cfg.get("chunk_overlap_tokens", 64)),
            embed_model=cfg.get("embed_model", "gemini-embedding-001"),
        )
        actor_audit("agent_data_sources", source_id, "upload",
                    after={"file_count": 1, "job_id": job_id})
        return {"queued": True, "job_id": job_id, "page_id": page_id,
                "hint": "poll jobs action=get"}
    return err(f"unknown action {action!r}",
               hint="list | get | create | update | delete | upload_document")


def _profile_visible_mcp_gate(client) -> Optional[List[Dict[str, Any]]]:
    """The `visible_profiles` argument `_attachable_mcps` expects — mirrors
    `admin/profile_mcp_attach_handlers.py::profile_mcp_attachments_update`'s
    resolution exactly: on a scoped build, restrict to profiles whose
    agent_kind is one of `visible_agent_kinds()`; a lookup failure fails
    CLOSED (empty list, never a 500 — same posture as the handler). `None`
    on a normal WAJ build = no gate."""
    from core.deployment import scoped_build
    if not scoped_build():
        return None
    from admin.seed_agent_profiles import visible_agent_kinds
    visible_kinds = set(visible_agent_kinds())
    try:
        all_profiles = rows(client.table("agent_profiles").select("*").execute())
    except Exception as exc:  # noqa: BLE001 — fail closed, never raise
        logger.warning("profile_mcp_attachments: visible-profile lookup failed (%s)", exc)
        all_profiles = []
    return [p for p in all_profiles if p.get("agent_kind") in visible_kinds]


def _slim_mcp_attachments(attachments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Denormalized `_fetch_mcp_attachments` rows -> JSON-friendly summaries.
    Same slimming `get_profile` does for its `mcp_attachments` key."""
    return [{
        "mcp_server_id": (a.get("mcp_server") or {}).get("mcp_server_id"),
        "name": (a.get("mcp_server") or {}).get("name"),
        "source": (a.get("mcp_server") or {}).get("source"),
        "is_builtin": (a.get("mcp_server") or {}).get("is_builtin"),
        "tool_subset": a.get("tool_subset"),
        "position": a.get("position"),
        "member_tools": [(m.get("tool") or {}).get("python_name")
                         for m in (a.get("member_tools") or [])],
    } for a in (attachments or [])]


def _apply_mcp_attachments(client, profile: Dict[str, Any],
                           desired: Dict[str, Optional[List[str]]],
                           attachable_by_id: Dict[str, Dict[str, Any]],
                           validate_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Validate then reconcile a profile's agent_profile_mcp_servers to
    exactly `desired` ({mcp_server_id: tool_subset|None}, in position
    order). Both guards run BEFORE any write, over `validate_ids` (default:
    every desired id — what `set` needs; profile_attachments passes only the
    ids it is ADDING, so an existing attachment can always be kept or
    removed). Shared by profile_mcp_attachments(set) and
    profile_attachments(add/remove)."""
    order = list(desired.keys())
    check = order if validate_ids is None else [m for m in order if m in validate_ids]
    # Guard 1 — attach-ability, BEFORE any write.
    for mid in check:
        if mid not in attachable_by_id:
            return err(f"MCP {mid} is not attachable to this profile")
    # Guard 2 — tool_subset validity, BEFORE any write.
    for mid in check:
        subset = desired[mid]
        if subset:
            from admin.mcp_membership import subset_names_for
            valid, why = subset_names_for(attachable_by_id[mid])
            if valid is None:
                return err(why)
            bad = [x for x in subset if x not in valid]
            if bad:
                name = attachable_by_id[mid].get("name") or mid
                return err(f"{name}: unknown tool(s) {', '.join(bad)}")
    # Both guards passed — safe to reconcile. Upsert-then-delete-stale
    # (see admin/mcp_membership.py) never empties the profile's
    # attachments mid-write; returns the prior ids for the audit before.
    from admin.mcp_membership import reconcile_profile_mcp_attachments
    profile_id = profile["profile_id"]
    before_ids = set(reconcile_profile_mcp_attachments(
        client, profile_id, [(mid, desired[mid]) for mid in order]))
    from admin.services.profiles import invalidate_profile_loader
    invalidate_profile_loader(profile.get("agent_kind"), profile.get("environment"))
    actor_audit("agent_profile_mcp_servers", profile_id, "update",
                before={"attachments": sorted(before_ids)},
                after={"attachments": sorted(order)})
    return {"updated": True, "attachments": sorted(order)}


@mcp.tool()
@tool_errors
def profile_mcp_attachments(profile_id: str, action: Literal["list", "set"] = "list",
                            attachments: Optional[List[Dict[str, Any]]] = None,
                            env: Env = "staging") -> Dict[str, Any]:
    """List or replace the MCP servers attached to a profile. These
    attachments are what the runtime binds the agent's tools from. Required:
    profile_id.

    Actions:
    - list (default): the current attachments, plus attachable (the MCP
      servers this profile may attach).
    - set (attachments): replace the attachments with a list of
      {mcp_server_id, tool_subset?}. A missing or null tool_subset means all
      of that server's tools. Any server not in the list is detached. Every
      server must be attachable and every tool_subset name must be one of
      its real tools; this is checked before anything is written. There is
      no confirm step.

    To add or remove one server, use profile_attachments(kind="mcp")."""
    # Maintainer notes (kept out of the description clients read):
    # Writes the agent_profile_mcp_servers join. No confirm gate, matching the
    # portal, which reconciles this join freely (reversible config, not a
    # destructive delete).
    client = client_for_env(env)
    profile = get_row(client, "agent_profiles", "profile_id", profile_id)
    if not profile:
        return err("profile not found")

    from admin.screens.agents import _attachable_mcps
    visible_profiles = _profile_visible_mcp_gate(client)
    attachable = _attachable_mcps(client, visible_profiles=visible_profiles)
    by_id = {m["mcp_server_id"]: m for m in attachable}

    if action == "list":
        from core.agent_profile import _fetch_mcp_attachments
        current = _slim_mcp_attachments(_fetch_mcp_attachments(client, profile_id))
        return {"attachments": current, "attachable": attachable}

    if action == "set":
        if attachments is None:
            return err("set needs attachments — a list of "
                       "{mcp_server_id, tool_subset?}")
        # Dedupe by mcp_server_id preserving first-occurrence order (→ position).
        desired: Dict[str, Optional[List[str]]] = {}
        for a in attachments:
            mid = (a or {}).get("mcp_server_id")
            if mid and mid not in desired:
                desired[mid] = a.get("tool_subset")
        return _apply_mcp_attachments(client, profile, desired, by_id)

    return err(f"unknown action {action!r}", hint="list | set")


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def mentionables(profile_id: str, env: Env = "staging") -> Dict[str, Any]:
    """List the names a profile's system prompt can mention: its own tools,
    MCP servers and KB sources, usable as @[tool:name], @[mcp:name] and
    @[kb:name] tokens. Only this profile's items are returned. Returns
    {tools, mcps, kbs}."""
    client = client_for_env(env)
    profile = get_row(client, "agent_profiles", "profile_id", profile_id)
    if not profile:
        return err("profile not found")

    tools_seen: Dict[str, None] = {}
    mcps_seen: Dict[str, None] = {}
    try:
        from core.agent_profile import _fetch_mcp_attachments
        for att in _fetch_mcp_attachments(client, profile_id):
            server = att.get("mcp_server") or {}
            if server.get("name"):
                mcps_seen.setdefault(server["name"], None)
            for m in att.get("member_tools") or []:
                name = (m.get("tool") or {}).get("python_name")
                if name:
                    tools_seen.setdefault(name, None)
    except Exception as exc:  # noqa: BLE001 — degrade to empty, never raise
        logger.warning("mentionables: attachment lookup failed for %s (%s)",
                       profile_id, exc)

    kbs: List[Dict[str, str]] = []
    ds_ids = profile.get("data_source_ids") or []
    if ds_ids:
        try:
            ds_rows = rows(client.table("agent_data_sources").select("source_id, name")
                          .in_("source_id", ds_ids).execute())
            for r in ds_rows:
                if r.get("name"):
                    kbs.append({"name": r["name"]})
        except Exception as exc:  # noqa: BLE001 — degrade to empty, never raise
            logger.warning("mentionables: data-source lookup failed for %s (%s)",
                           profile_id, exc)

    return {
        "tools": [{"name": n} for n in tools_seen],
        "mcps": [{"name": n} for n in mcps_seen],
        "kbs": kbs,
    }


@mcp.tool()
@tool_errors
def templates(action: Literal["list", "get", "use"] = "list",
              template_id: str = "", display_name: str = "",
              slug: str = "", as_subagent_of: str = "", model_id: str = "",
              agent_type: Annotated[str, Field(
                  description="use: put the new agent (and the sub-agents "
                              "made with it) in this category: a category "
                              "slug from agent_types(action='list'). '' = "
                              "uncategorised.")] = "",
              env: Env = "staging") -> Dict[str, Any]:
    """Catalog of agent templates that can be copied into a new agent.
    Templates are read-only: there is no create, edit or delete.

    Actions:
    - list (default): browse the templates.
    - get (template_id): a template's full config.
    - use (template_id, display_name): create a new draft agent from the
      template. A composite template also copies its sub-agent templates.
      Optional: as_subagent_of (a profile_id; adds the new agent to that
      parent's sub-agents), model_id (use this model instead of the
      template's), slug (left blank where generic agents are enabled, it is
      made from the name: "Support desk" becomes support-desk) and agent_type
      (a category slug; an unknown category refuses the call before anything
      is created)."""
    client = client_for_env(env)
    if action == "list":
        from admin.services.templates import list_templates
        return {"templates": list_templates(client)}
    if action == "get":
        row = get_row(client, "agent_profiles", "profile_id", template_id)
        if not row or row.get("status") != "template":
            return err("not a template", hint="templates action=list shows valid ids")
        return {"template": row}
    if action == "use":
        if not (template_id and display_name):
            return err("use needs template_id and display_name")
        from admin.services.templates import TemplateNotFoundError, use_template
        from admin.services.profiles import ProfileNotFoundError
        try:
            with use_env(validate_env(env)):
                return use_template(
                    template_id=template_id, display_name=display_name,
                    slug=slug or None, as_subagent_of=as_subagent_of or None,
                    model_id=model_id or None,
                    agent_type=agent_type or None,
                    actor_user_id=current_actor()["user_id"])
        except TemplateNotFoundError:
            return err("not a template", hint="templates action=list shows valid ids")
        except ProfileNotFoundError as exc:
            return err(str(exc))
    return err(f"unknown action {action!r}", hint="list | get | use")


@mcp.tool()
@tool_errors
def agent_types(
        action: Annotated[Literal["list", "create", "update", "delete"], Field(
            description="list | create | update | delete.")] = "list",
        agent_type: Annotated[str, Field(
            description="The category to update or delete: its slug (or its "
                        "agent_type_id).")] = "",
        display_name: Annotated[Optional[str], Field(
            description="create: the category's name (required). update: "
                        "rename it; the slug stays the same.")] = None,
        slug: Annotated[str, Field(
            description="create only: the category's stable identifier "
                        "(lowercase letters, digits, hyphens). Blank = made "
                        "from the name. It cannot be changed later.")] = "",
        description: Annotated[Optional[str], Field(
            description="What kind of agents belong in the category "
                        "(optional, at most 280 characters).")] = None,
        color: Annotated[Optional[str], Field(
            description="Chip colour, one of: slate, teal, blue, violet, "
                        "pink, red, amber, green (default slate).")] = None,
        sort_order: Annotated[Optional[int], Field(
            description="Position in pickers and lists, ascending. create: "
                        "default is last. update: set it to move the "
                        "category.")] = None,
        confirm: Confirm = False,
        env: Env = "staging") -> Dict[str, Any]:
    """Agent categories, such as Support, Sales or Internal tools. They
    group agents in the agent list, its filter and reports. A category never
    changes how an agent runs and is not part of a promote. A fresh install
    has none.

    Actions:
    - list (default): every category in order, each with agent_count.
    - create (display_name; optional slug, description, color, sort_order).
    - update (agent_type plus any of display_name, description, color,
      sort_order).
    - delete (agent_type, confirm=true): the agents in the category are not
      deleted; they become uncategorised. Without confirm it reports how
      many agents that is.

    Put an agent in a category with update_profile(agent_type=...) or
    templates(action="use", agent_type=...). Filter with
    list_profiles(agent_type=...)."""
    from admin.services import agent_types as svc
    client = client_for_env(env)
    actor_id = current_actor()["user_id"]
    with use_env(validate_env(env)):
        if action == "list":
            found = svc.list_types(client)
            return {"count": len(found), "agent_types": found,
                    "colors": list(svc.PALETTE)}
        if action == "create":
            if not (display_name or "").strip():
                return err("create needs display_name")
            row = svc.create_type(
                display_name=display_name, slug=slug,
                description=description or "", color=color or "",
                sort_order=sort_order, actor_user_id=actor_id, client=client)
            return {"created": True, "agent_type": row}
        if action in ("update", "delete") and not agent_type:
            return err(f"{action} needs agent_type (the category's slug)",
                       hint="agent_types action=list shows the slugs")
        if action == "update":
            changes = {k: v for k, v in {
                "display_name": display_name, "description": description,
                "color": color, "sort_order": sort_order}.items()
                if v is not None}
            if slug:
                return err("a category's slug cannot be changed; rename it "
                           "with display_name")
            row = svc.update_type(agent_type, changes,
                                  actor_user_id=actor_id, client=client)
            return {"updated": True, "changed": sorted(changes),
                    "agent_type": row}
        if action == "delete":
            target = svc.get_type(agent_type, client)
            count = svc.agent_counts(client).get(target["agent_type_id"], 0)
            if not confirm:
                return not_confirmed(
                    f"delete the category {target['slug']!r}; {count} agent"
                    f"{'' if count == 1 else 's'} in it would become "
                    "uncategorised (no agent is deleted)",
                    agents_uncategorised=count)
            out = svc.delete_type(agent_type, actor_user_id=actor_id,
                                  client=client)
            return {"deleted": True, "agent_type": out["deleted"]["slug"],
                    "agents_uncategorised": out["agents_uncategorised"]}
    return err(f"unknown action {action!r}",
               hint="list | create | update | delete")


# ---------------------------------------------------------------------------
# Skills (spec 2026-07-22-agent-skills-design.md)


_SKILL_STATUSES = ("active", "draft", "archived")


@mcp.tool()
@tool_errors
def skills(action: Literal["list", "get", "create", "update", "create_version",
                           "list_resources", "add_resource", "delete_resource"],
           env: Env = "staging",
           skill_id: str = "",
           slug: str = "",
           display_name: str = "",
           description: str = "",
           agent_kind: str = "",
           business_id: str = "",
           always_on: Optional[bool] = None,
           linked_data_source_ids: Optional[List[str]] = None,
           status: Optional[Literal["active", "draft", "archived"]] = None,
           is_active: Optional[bool] = None,
           content: str = "",
           notes: str = "",
           version_id: Annotated[str, Field(
               description="Resource actions: the skill version the file "
                           "belongs to ('' = the newest version).")] = "",
           resource_id: Annotated[str, Field(
               description="delete_resource: the resource id (list_resources).")] = "",
           filename: Annotated[str, Field(
               description="add_resource: file name (re-adding the same name "
                           "on the same version replaces it).")] = "",
           content_type: Annotated[str, Field(
               description="add_resource: MIME type (default text/markdown).")] = "",
           confirm: Confirm = False) -> Dict[str, Any]:
    """Agent skills: versioned instruction documents. The agent sees a
    catalog of its skills and reads a skill's body when it needs it.
    Required: action.

    Actions:
    - list: all skills; filter with agent_kind and business_id.
    - get (skill_id): the skill, its versions and the newest version's
      resources.
    - create (slug, display_name, description, agent_kind, content; optional
      business_id for a tenant skill, always_on, linked_data_source_ids,
      notes).
    - update (skill_id plus fields): display_name, description, always_on,
      linked_data_source_ids, status (active, draft or archived) and
      is_active. status="archived" also sets is_active=false. A skill is
      served only when it is both is_active and status="active".
    - create_version (skill_id, content; optional notes).
    - list_resources (skill_id; optional version_id): the version's files
      (filename, content_type, size; no content).
    - add_resource (skill_id, filename, content; optional version_id,
      content_type): attach a file the agent can read on demand. At most
      64 KB. A file with the same name on the same version is replaced.
    - delete_resource (skill_id, resource_id, confirm=true)."""
    # Maintainer notes (kept out of the description clients read):
    # Archiving mirrors the portal's "Archive skill" button
    # (admin/skill_handlers.py::skill_archive), which always writes status and
    # is_active together because core/skills.py's resolver requires both.
    from core.skills import (MAX_BODY_BYTES, MAX_RESOURCE_BYTES,
                             invalidate_skills_cache, validate_skill_metadata)

    client = client_for_env(env)

    def _body_error(text: str) -> Optional[str]:
        if not (text or "").strip():
            return "content is required"
        if len(text.encode("utf-8")) > MAX_BODY_BYTES:
            return f"content exceeds {MAX_BODY_BYTES // 1024} KB"
        return None

    if action == "list":
        filters: Dict[str, Any] = {}
        if agent_kind:
            filters["agent_kind"] = agent_kind
        if business_id:
            filters["business_id"] = business_id
        return {"skills": list_rows(client, "agent_skills", filters=filters)}

    if action == "get":
        if not skill_id:
            return err("skill_id is required for get")
        skill = get_row(client, "agent_skills", "skill_id", skill_id)
        if not skill:
            return err(f"skill {skill_id} not found")
        versions = rows(client.table("agent_skill_versions").select("*")
                        .eq("skill_id", skill_id)
                        .order("version_number", desc=True).execute())
        resources = rows(client.table("agent_skill_resources").select("*")
                         .eq("skill_version_id", versions[0]["skill_version_id"])
                         .execute()) if versions else []
        return {"skill": skill, "versions": versions, "latest_resources": resources}

    if action == "create":
        msg = validate_skill_metadata(slug, display_name, description)
        if msg is None and not agent_kind:
            msg = "agent_kind is required"
        if msg is None:
            msg = _body_error(content)
        if msg:
            return err(msg)
        inserted = rows(client.table("agent_skills").insert({
            "slug": slug, "display_name": display_name,
            "description": description, "agent_kind": agent_kind,
            "business_id": business_id or None,
            "always_on": bool(always_on),
            "linked_data_source_ids": linked_data_source_ids or [],
        }).execute())
        if not inserted:
            return err("insert failed")
        skill = inserted[0]
        client.table("agent_skill_versions").insert({
            "skill_id": skill["skill_id"], "version_number": 1,
            "content": content, "notes": notes or "initial version",
        }).execute()
        actor_audit("agent_skills", skill["skill_id"], "create",
                    after={"slug": slug, "agent_kind": agent_kind})
        invalidate_skills_cache()
        return {"skill": skill, "version_number": 1}

    if action == "update":
        if not skill_id:
            return err("skill_id is required for update")
        before = get_row(client, "agent_skills", "skill_id", skill_id)
        if not before:
            return err(f"skill {skill_id} not found")
        if status is not None and status not in _SKILL_STATUSES:
            return err(f"status must be one of {_SKILL_STATUSES}, got {status!r}")
        patch_payload: Dict[str, Any] = {}
        if display_name or description:
            msg = validate_skill_metadata(
                before["slug"],
                display_name or before["display_name"],
                description or before["description"])
            if msg:
                return err(msg)
        for key, val in (("display_name", display_name or None),
                         ("description", description or None),
                         ("always_on", always_on),
                         ("linked_data_source_ids", linked_data_source_ids),
                         ("status", status),
                         ("is_active", is_active)):
            if val is not None:
                patch_payload[key] = val
        if not patch_payload:
            return err("nothing to update")
        # Archive/unarchive: mirror admin/skill_handlers.py::skill_archive
        # exactly. That route always writes status="archived" AND
        # is_active=False together (never one without the other) because
        # core/skills.py's live-resolution queries require BOTH
        # is_active=True and status="active" to serve a skill — force the
        # pairing here too so a caller can't leave the row half-archived.
        was_archived = before.get("status") == "archived"
        if patch_payload.get("status") == "archived":
            patch_payload["is_active"] = False
            audit_action = "archive"
        elif "status" in patch_payload and was_archived:
            # The portal has no unarchive route (one-way button); this is
            # the general update surface reversing a status the portal can
            # only set forward, so its own audit action name is used rather
            # than fabricating portal parity that doesn't exist there.
            audit_action = "unarchive"
        else:
            audit_action = "update"
        client.table("agent_skills").update(patch_payload).eq(
            "skill_id", skill_id).execute()
        actor_audit("agent_skills", skill_id, audit_action,
                    before={k: before.get(k) for k in patch_payload},
                    after=patch_payload)
        invalidate_skills_cache()
        if audit_action in ("archive", "unarchive"):
            # skill_archive also invalidates AgentProfileLoader (platform
            # skills are frozen into profile snapshots) — match it on any
            # status flip, not just archiving, so the reverse direction
            # doesn't wait out the loader's 60s cache.
            from core.agent_profile import AgentProfileLoader
            AgentProfileLoader.invalidate()
        return {"skill_id": skill_id, "updated": sorted(patch_payload)}

    if action == "create_version":
        if not skill_id:
            return err("skill_id is required for create_version")
        msg = _body_error(content)
        if msg:
            return err(msg)
        existing = rows(client.table("agent_skill_versions")
                        .select("version_number").eq("skill_id", skill_id)
                        .order("version_number", desc=True).limit(1).execute())
        next_num = (max(r["version_number"] for r in existing) + 1) if existing else 1
        client.table("agent_skill_versions").insert({
            "skill_id": skill_id, "version_number": next_num,
            "content": content, "notes": notes or None,
        }).execute()
        actor_audit("agent_skill_versions", skill_id, "create",
                    after={"version_number": next_num})
        invalidate_skills_cache()
        return {"skill_id": skill_id, "version_number": next_num}

    if action in ("list_resources", "add_resource", "delete_resource"):
        if not skill_id:
            return err(f"skill_id is required for {action}")
        if not get_row(client, "agent_skills", "skill_id", skill_id):
            return err(f"skill {skill_id} not found")
        versions = rows(client.table("agent_skill_versions").select("*")
                        .eq("skill_id", skill_id)
                        .order("version_number", desc=True).execute())
        if version_id:
            version = next((v for v in versions
                            if v.get("skill_version_id") == version_id), None)
            if not version:
                return err(f"version {version_id} does not belong to skill {skill_id}")
        else:
            version = versions[0] if versions else None
            if not version:
                return err("skill has no versions")
        vid = version["skill_version_id"]

        if action == "list_resources":
            res = rows(client.table("agent_skill_resources").select("*")
                       .eq("skill_version_id", vid).execute())
            return {"skill_id": skill_id, "skill_version_id": vid,
                    "version_number": version.get("version_number"),
                    "resources": [{"resource_id": r.get("resource_id"),
                                   "filename": r.get("filename"),
                                   "content_type": r.get("content_type"),
                                   "bytes": len((r.get("content") or "").encode("utf-8")),
                                   "position": r.get("position")}
                                  for r in sorted(res, key=lambda r: r.get("position") or 0)]}

        from core.agent_profile import AgentProfileLoader
        if action == "add_resource":
            name = (filename or "").strip()
            if not name:
                return err("filename is required")
            if len((content or "").encode("utf-8")) > MAX_RESOURCE_BYTES:
                return err(f"resource exceeds {MAX_RESOURCE_BYTES // 1024} KB")
            # Same idempotent-per-(version, filename) replace the portal's
            # skill_resource_create does, rather than tripping the unique key.
            client.table("agent_skill_resources").delete().eq(
                "skill_version_id", vid).eq("filename", name).execute()
            actor = current_actor()["user_id"]
            inserted = rows(client.table("agent_skill_resources").insert({
                "skill_version_id": vid, "filename": name,
                "content_type": (content_type or "text/markdown").strip(),
                "content": content or "", "position": 0,
                "created_by": actor, "updated_by": actor,
            }).execute())
            actor_audit("agent_skill_resources", skill_id, "create",
                        after={"filename": name, "skill_version_id": vid})
            invalidate_skills_cache()
            AgentProfileLoader.invalidate()
            return {"added": True, "skill_id": skill_id, "skill_version_id": vid,
                    "resource_id": (inserted[0].get("resource_id")
                                    if inserted else None),
                    "filename": name}

        # delete_resource
        if not resource_id:
            return err("delete_resource needs resource_id")
        res = get_row(client, "agent_skill_resources", "resource_id", resource_id)
        if not res or res.get("skill_version_id") not in {
                v.get("skill_version_id") for v in versions}:
            return err(f"resource {resource_id} does not belong to skill {skill_id}")
        if not confirm:
            return not_confirmed(f"delete resource {res.get('filename')!r} "
                                 f"from skill {skill_id}")
        client.table("agent_skill_resources").delete().eq(
            "resource_id", resource_id).execute()
        actor_audit("agent_skill_resources", skill_id, "delete",
                    before={"resource_id": resource_id,
                            "filename": res.get("filename")}, after=None)
        invalidate_skills_cache()
        AgentProfileLoader.invalidate()
        return {"deleted": True, "resource_id": resource_id}

    return err(f"unknown action {action!r}",
               hint="one of: list, get, create, update, create_version, "
                    "list_resources, add_resource, delete_resource")


def _apply_skill_attachments(client, profile: Dict[str, Any],
                             desired: List[str]) -> Dict[str, Any]:
    """Validate then replace a profile's agent_profile_skills with
    `desired` (position = index) — profile_skill_attachments(set)."""
    # Guard BEFORE any write (mirrors profile_mcp_attachments).
    for sid in desired:
        skill = get_row(client, "agent_skills", "skill_id", sid)
        if (not skill or not skill.get("is_active")
                or skill.get("status") != "active"
                or skill.get("business_id") is not None
                or skill.get("agent_kind") != profile.get("agent_kind")):
            return err(f"skill {sid} is not attachable to this profile",
                       hint="active platform skill with matching agent_kind required")
    return _write_skill_attachments(client, profile, desired)


def _write_skill_attachments(client, profile: Dict[str, Any],
                             desired: List[str]) -> Dict[str, Any]:
    """Replace agent_profile_skills with `desired` (already validated),
    invalidate caches and audit."""
    from core.skills import invalidate_skills_cache
    profile_id = profile["profile_id"]
    before = rows(client.table("agent_profile_skills").select("skill_id")
                  .eq("profile_id", profile_id).execute())
    client.table("agent_profile_skills").delete().eq(
        "profile_id", profile_id).execute()
    for pos, sid in enumerate(desired):
        client.table("agent_profile_skills").insert({
            "profile_id": profile_id, "skill_id": sid, "position": pos,
        }).execute()
    from core.agent_profile import AgentProfileLoader
    AgentProfileLoader.invalidate(profile.get("agent_kind"),
                                  profile.get("environment"))
    invalidate_skills_cache()
    actor_audit("agent_profile_skills", profile_id, "update",
                before={"attachments": sorted(r["skill_id"] for r in before)},
                after={"attachments": desired})
    return {"profile_id": profile_id, "attachments": desired}


@mcp.tool()
@tool_errors
def profile_skill_attachments(profile_id: str,
                              action: Literal["list", "set"] = "list",
                              skill_ids: Optional[List[str]] = None,
                              env: Env = "staging") -> Dict[str, Any]:
    """List or replace the platform skills attached to a profile. Required:
    profile_id.

    Actions:
    - list (default): the current attachments in order.
    - set (skill_ids): replace the attachments with skill_ids, in that
      order. Only active platform skills (no business_id) of the profile's
      agent_kind can be attached. Tenant skills are resolved at run time and
      are never attached. There is no confirm step.

    To add or remove one skill, use profile_attachments(kind="skills")."""
    client = client_for_env(env)
    profile = get_row(client, "agent_profiles", "profile_id", profile_id)
    if not profile:
        return err(f"profile {profile_id} not found")

    if action == "list":
        atts = rows(client.table("agent_profile_skills").select("*")
                    .eq("profile_id", profile_id).execute())
        return {"attachments": sorted(atts, key=lambda r: r.get("position") or 0)}

    if action == "set":
        desired: List[str] = []
        seen: set = set()
        for sid in (skill_ids or []):
            if sid and sid not in seen:
                seen.add(sid)
                desired.append(sid)
        return _apply_skill_attachments(client, profile, desired)

    return err(f"unknown action {action!r}", hint="one of: list, set")


# ---------------------------------------------------------------------------
# Incremental attachments — add/remove ONE item without re-sending the list.

# kind -> (agent_profiles list column, referenced table, its PK, label column)
_ATTACH_LIST_COLUMNS = {
    "tools": ("tool_ids", "agent_tools", "tool_id", "python_name"),
    "kb": ("data_source_ids", "agent_data_sources", "source_id", "name"),
    "downstream": ("downstream_profile_ids", "agent_profiles", "profile_id",
                   "display_name"),
}


@mcp.tool()
@tool_errors
def profile_attachments(
        profile_id: Annotated[str, Field(description="Profile to change.")],
        kind: Annotated[Literal["tools", "kb", "mcp", "skills", "downstream"], Field(
            description="What to attach: tools (tool ids, the profile's "
                        "tool_ids list) | kb (KB source ids) | mcp (MCP server "
                        "ids: the attachments the runtime binds tools from) | "
                        "skills (platform skill ids) | downstream (sub-agent "
                        "profile ids).")],
        action: Annotated[Literal["list", "add", "remove"], Field(
            description="list = current items; add/remove = change ONLY the "
                        "given ids, keeping everything else attached.")] = "list",
        ids: Annotated[Optional[List[str]], Field(
            description="add/remove: the ids to attach or detach.")] = None,
        tool_subset: Annotated[Optional[List[str]], Field(
            description="kind=mcp add only: pin these tool names on every MCP "
                        "being added (omit = all of the MCP's tools).")] = None,
        env: Env = "staging") -> Dict[str, Any]:
    """Attach or detach single items on a profile without re-sending the
    whole list. Required: profile_id and kind (tools, kb, mcp, skills, or
    downstream for sub-agents).

    Actions:
    - list (default): the current items.
    - add (ids): append the ids. Existing items keep their order and
      tool_subset. For kind=mcp, tool_subset limits the added servers to
      those tool names.
    - remove (ids): detach only those ids.

    Adding an id that is already attached, or removing one that is not,
    changes nothing and is reported in unchanged. Every new id is checked
    before any write. Unknown or deprecated tools, missing KB sources or
    profiles, MCP servers that cannot be attached, and skills that are not
    active platform skills of the profile's kind are refused, and nothing is
    written. Changes are audited.

    Returns {kind, profile_id, items, added, removed, unchanged}. The
    replace-all tools still work: update_profile(tool_ids=...),
    profile_mcp_attachments(action="set") and
    profile_skill_attachments(action="set")."""
    client = client_for_env(env)
    profile = get_row(client, "agent_profiles", "profile_id", profile_id)
    if not profile:
        return err("profile not found", hint="list_profiles shows valid ids")
    want = []
    for i in ids or []:
        if i and i not in want:
            want.append(i)
    if action in ("add", "remove") and not want:
        return err(f"{action} needs ids")

    if kind in _ATTACH_LIST_COLUMNS:
        column, table, pk, label = _ATTACH_LIST_COLUMNS[kind]
        current = list(profile.get(column) or [])

        def _labelled(id_list):
            if not id_list:
                return []
            found = {r[pk]: r.get(label) for r in rows(
                client.table(table).select(f"{pk}, {label}")
                .in_(pk, id_list).execute())}
            return [{"id": i, "label": found.get(i)} for i in id_list]

        if action == "list":
            return {"kind": kind, "profile_id": profile_id,
                    "items": _labelled(current)}
        if action == "add":
            new_ids = [i for i in want if i not in current]
            if new_ids:
                q = client.table(table).select("*").in_(pk, new_ids)
                found = rows(q.execute())
                if kind == "tools":
                    found = [r for r in found if not r.get("is_deprecated")]
                if kind == "downstream" and profile_id in new_ids:
                    return err("a profile cannot be its own sub-agent")
                missing = sorted(set(new_ids) - {r[pk] for r in found})
                if missing:
                    return err(f"unknown or deprecated {kind} id(s): {missing}")
            after = current + new_ids
            changed, unchanged = new_ids, [i for i in want if i not in new_ids]
        else:  # remove
            changed = [i for i in want if i in current]
            unchanged = [i for i in want if i not in current]
            after = [i for i in current if i not in set(changed)]
        if not changed:
            return {"kind": kind, "profile_id": profile_id, "updated": False,
                    "items": _labelled(current), "added": [], "removed": [],
                    "unchanged": unchanged}
        from admin.services.profiles import (ProfileNotFoundError,
                                             TemplateProtectedError,
                                             update_profile_row)
        try:
            with use_env(validate_env(env)):
                update_profile_row(profile_id=profile_id, payload={column: after},
                                   actor_user_id=current_actor()["user_id"])
        except ProfileNotFoundError:
            return err("profile not found")
        except TemplateProtectedError as exc:
            return err(str(exc))
        return {"kind": kind, "profile_id": profile_id, "updated": True,
                "items": _labelled(after),
                "added": changed if action == "add" else [],
                "removed": changed if action == "remove" else [],
                "unchanged": unchanged}

    if kind == "mcp":
        from admin.screens.agents import _attachable_mcps
        from core.agent_profile import _fetch_mcp_attachments
        current_atts = _fetch_mcp_attachments(client, profile_id) or []
        current: Dict[str, Optional[List[str]]] = {}
        for a in sorted(current_atts, key=lambda a: a.get("position") or 0):
            mid = (a.get("mcp_server") or {}).get("mcp_server_id")
            if mid and mid not in current:
                current[mid] = a.get("tool_subset")
        if action == "list":
            return {"kind": kind, "profile_id": profile_id,
                    "items": _slim_mcp_attachments(current_atts)}
        attachable = _attachable_mcps(
            client, visible_profiles=_profile_visible_mcp_gate(client))
        by_id = {m["mcp_server_id"]: m for m in attachable}
        if action == "add":
            changed = [i for i in want if i not in current]
            desired = {**current, **{i: tool_subset for i in changed}}
        else:
            changed = [i for i in want if i in current]
            desired = {k: v for k, v in current.items() if k not in set(changed)}
        unchanged = [i for i in want if i not in changed]
        if not changed:
            return {"kind": kind, "profile_id": profile_id, "updated": False,
                    "items": _slim_mcp_attachments(current_atts),
                    "added": [], "removed": [], "unchanged": unchanged}
        # Only the ids being ADDED are validated; existing attachments keep
        # their order and tool_subset untouched (`set` stays the strict,
        # validate-everything path).
        result = _apply_mcp_attachments(
            client, profile, desired, by_id,
            validate_ids=changed if action == "add" else [])
        if "error" in result:
            return result
        return {"kind": kind, "profile_id": profile_id, "updated": True,
                "items": [{"mcp_server_id": k, "name": (by_id.get(k) or {}).get("name"),
                           "tool_subset": v} for k, v in desired.items()],
                "added": changed if action == "add" else [],
                "removed": changed if action == "remove" else [],
                "unchanged": unchanged}

    # skills
    current_rows = sorted(rows(client.table("agent_profile_skills").select("*")
                               .eq("profile_id", profile_id).execute()),
                          key=lambda r: r.get("position") or 0)
    current = [r["skill_id"] for r in current_rows]
    if action == "list":
        return {"kind": kind, "profile_id": profile_id, "items": current_rows}
    if action == "add":
        changed = [i for i in want if i not in current]
        desired = current + changed
    else:
        changed = [i for i in want if i in current]
        desired = [i for i in current if i not in set(changed)]
    unchanged = [i for i in want if i not in changed]
    if not changed:
        return {"kind": kind, "profile_id": profile_id, "updated": False,
                "items": current_rows, "added": [], "removed": [],
                "unchanged": unchanged}
    if action == "add":
        # Validate only the NEW ids: a previously-attached skill that was
        # archived since must not block adding another one (the runtime
        # already skips inactive skills); `set` remains the strict path.
        for sid in changed:
            skill = get_row(client, "agent_skills", "skill_id", sid)
            if (not skill or not skill.get("is_active")
                    or skill.get("status") != "active"
                    or skill.get("business_id") is not None
                    or skill.get("agent_kind") != profile.get("agent_kind")):
                return err(f"skill {sid} is not attachable to this profile",
                           hint="active platform skill with matching agent_kind required")
    result = _write_skill_attachments(client, profile, desired)
    return {"kind": kind, "profile_id": profile_id, "updated": True,
            "items": result["attachments"],
            "added": changed if action == "add" else [],
            "removed": changed if action == "remove" else [],
            "unchanged": unchanged}
