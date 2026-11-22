"""Govern-domain MCP tools: admin users/superusers, the audit log, per-user
memory + memory_config, the channel registry, the test-fixture vault, and
the admin_jobs tracker.

Each tool mirrors a portal handler as closely as possible — same table,
same columns, same audit action strings, same client (env-branched vs the
process's own default service client) — rather than inventing a cleaner
API surface. See the per-tool docstring for the handler it mirrors and any
deliberate deviation.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from admin_mcp._common import (READ_ONLY, Env, actor_audit, client_for_env,
                               err, get_row, not_confirmed, rows, tool_errors,
                               use_env, validate_env)
from admin_mcp.context import current_actor
from admin_mcp.server import mcp


def _list_users(limit: int) -> List[Dict[str, Any]]:
    from core.auth import get_main_client
    client = get_main_client()
    found = rows(client.table("user")
                 .select("id, email, phone_number, role, permission, "
                         "business_id, created_at")
                 .eq("is_deleted", False)
                 .order("created_at", desc=True)
                 .limit(max(1, min(500, limit))).execute())
    for r in found:
        r["is_superuser"] = bool((r.get("permission") or {}).get("is_superuser"))
        r.pop("permission", None)
    return found


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def list_users(limit: int = 100,
               superusers_only: bool = False) -> Dict[str, Any]:
    """Admin users (main WAJ DB `user` table, newest first) with their
    is_superuser flag. Read-only twin of users(action="list") — safe for a
    host to auto-approve. Grant/revoke with users(action="set_superuser")."""
    found = _list_users(limit)
    if superusers_only:
        found = [u for u in found if u.get("is_superuser")]
    return {"users": found, "count": len(found)}


@mcp.tool()
@tool_errors
def users(action: Literal["list", "set_superuser"] = "list",
          user_id: str = "", grant: bool = False,
          confirm: bool = False, limit: int = 100) -> Dict[str, Any]:
    """Admin users. action: list (users with superuser flag — or use the
    read-only list_users tool) | set_superuser (user_id + grant=true/false,
    confirm required — this grants/revokes FULL admin+MCP access).
    Unannotated (mixed read/write): `list` no longer trips a host's
    destructive-action prompt, `set_superuser` is still confirm-gated."""
    if action == "set_superuser" and not confirm:
        return not_confirmed(
            f"{'grant' if grant else 'revoke'} superuser for user {user_id}")
    from core.auth import get_main_client
    if action == "list":
        return {"users": _list_users(limit)}
    client = get_main_client()
    if action == "set_superuser":
        found = rows(client.table("user").select("permission")
                     .eq("id", user_id).limit(1).execute())
        if not found:
            return err("user not found")
        perm = dict(found[0].get("permission") or {})
        perm["is_superuser"] = grant
        client.table("user").update({"permission": perm}).eq("id", user_id).execute()
        actor_audit("user", user_id,
                    "GRANT_SUPERUSER" if grant else "REVOKE_SUPERUSER",
                    after={"is_superuser": grant})
        return {"updated": True, "user_id": user_id, "is_superuser": grant}
    return err(f"unknown action {action!r}", hint="list | set_superuser")


@mcp.tool()
@tool_errors
def audit(action: Literal["list", "get", "probe_write", "verify"] = "list",
          env: Env = "staging", entity_type: str = "",
          audit_id: str = "", limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """agent_audit_log operations (mirrors GET /admin/audit,
    GET /admin/audit/{id}, POST /admin/audit/verify). action:
    list (optional entity_type filter, newest first, paged via
    limit/offset) | get (audit_id) | probe_write (`verify` is its old,
    deprecated name).

    probe_write WRITES one row and verifies nothing: like the portal's
    "verify" button it records a liveness marker
    (record_change(entity_type="agent_audit_log", entity_id=<all-zero
    uuid>, action="CALLEE_VERIFY")) proving a superuser can still write to
    the log. There is no hash chain to walk."""
    if action in ("probe_write", "verify"):
        actor_audit("agent_audit_log", "00000000-0000-0000-0000-000000000000",
                    "CALLEE_VERIFY")
        return {"verified_marker_recorded": True, "probe_write": "ok",
                "note": "a liveness marker row was written; no integrity "
                        "check was performed"}
    client = client_for_env(env)
    if action == "list":
        q = client.table("agent_audit_log").select("*").order("created_at", desc=True)
        if entity_type:
            q = q.eq("entity_type", entity_type)
        found = rows(q.range(offset, offset + max(limit, 1) - 1).execute())
        return {"audit_log": found, "count": len(found), "offset": offset,
                "limit": limit, "has_more": len(found) >= max(limit, 1)}
    if action == "get":
        if not audit_id:
            return err("get needs audit_id")
        row = get_row(client, "agent_audit_log", "audit_id", audit_id)
        return {"audit": row} if row else err("audit entry not found")
    return err(f"unknown action {action!r}", hint="list | get | probe_write")


# The kind memory tools default to: the plugin's (`agent_catalog.default_kind()`),
# else the generic one — same as the portal's memory screens.
from admin.agent_kinds import catalog_tuple as _catalog_tuple  # noqa: E402

_DEFAULT_MEMORY_KIND = (_catalog_tuple("default_kind") or ("custom",))[0]


@mcp.tool()
@tool_errors
def memory(action: Literal["get_config", "set_config", "list", "add", "edit",
                           "pin", "delete"] = "list",
          agent_kind: str = _DEFAULT_MEMORY_KIND, env: Env = "staging",
          memory_id: str = "", user_id: str = "", business_id: str = "",
          content: str = "", pinned: Optional[bool] = None,
          config: Optional[Dict[str, Any]] = None,
          confirm: bool = False,
          limit: int = 50,
          profile_id: str = "") -> Dict[str, Any]:
    """Per-user memory + per-agent memory_config (mirrors /admin/memory).
    action: get_config (agent_kind -> the active profile's memory_config,
    defaults-merged) | set_config (agent_kind, config: a nested
    memory_config dict — same shape agent_profiles.memory_config stores;
    unlike the HTML form this skips the checkbox/flat-field parsing layer
    since a tool call already supplies structured JSON) | list (agent_kind,
    user_id: uuid or email -> that user's ai_user_memory rows) | add
    (user_id, business_id, content, agent_kind) | edit (memory_id, content
    — re-embeds via the same Gemini embedder the runtime uses; proceeds
    without an embedding if the embed call fails, same as the handler) |
    pin (memory_id, pinned=true/false) | delete (memory_id, confirm=true —
    soft-archive, status='archived').

    get_config/set_config target "the active profile for agent_kind in env"
    by default. Pass `profile_id` to read/write one SPECIFIC profile's
    memory_config instead — a draft, a fork, or a voice specialist (the
    voice brain's memory_config lives on the specialist row, which the
    agent_kind lookup never reaches). list returns at most `limit`
    memories (default 50)."""
    from admin.memory_handlers import _resolve_agent_kind
    from admin.screens import memory as memory_screen
    from core.memory.embeddings import embed_text

    client = client_for_env(env)

    if action == "get_config":
        if profile_id:
            row = get_row(client, "agent_profiles", "profile_id", profile_id)
            if not row:
                return err("profile not found")
            return {"agent_kind": row.get("agent_kind"),
                    "profile": {k: row.get(k) for k in
                                ("profile_id", "slug", "status", "memory_config")},
                    "config_form": memory_screen.shape_config_for_form(
                        row.get("memory_config") or {})}
        agent_kind = _resolve_agent_kind(agent_kind)
        return {"agent_kind": agent_kind,
                **memory_screen.load_config_context(client, agent_kind, env)}

    if action == "list":
        agent_kind = _resolve_agent_kind(agent_kind)
        ctx = memory_screen.load_memories_context(client, user_id or None, agent_kind)
        mems = ctx.get("memories") or []
        cap = max(1, min(200, limit))
        return {"agent_kind": agent_kind, **ctx, "memories": mems[:cap],
                "total": len(mems), "has_more": len(mems) > cap}

    if action == "set_config":
        if config is None:
            return err("set_config needs config (nested memory_config dict)")
        if profile_id:
            found = get_row(client, "agent_profiles", "profile_id", profile_id)
            if not found:
                return err("profile not found")
            agent_kind = found.get("agent_kind") or agent_kind
            env = found.get("environment") or env
        else:
            agent_kind = _resolve_agent_kind(agent_kind)
            found = memory_screen.active_profile_row(
                client, agent_kind, env, select="profile_id, memory_config")
            if not found:
                return err("no active profile for agent_kind", hint=f"{agent_kind}/{env}")
            profile_id = found["profile_id"]
        before = found.get("memory_config")
        client.table("agent_profiles").update({"memory_config": config}) \
            .eq("profile_id", profile_id).execute()
        actor_audit("agent_profiles", profile_id, "memory_config_update",
                    before={"memory_config": before}, after={"memory_config": config})
        from admin.services.profiles import invalidate_profile_loader
        invalidate_profile_loader(agent_kind, env)
        return {"updated": True, "profile_id": profile_id, "memory_config": config}

    if action == "add":
        if not (user_id and business_id and content):
            return err("add needs user_id, business_id, content")
        embedding = embed_text(content)
        row = {"user_id": user_id, "business_id": business_id,
               "agent_kind": agent_kind, "content": content, "source": "manual"}
        if embedding is not None:
            from core.memory.embeddings import to_vector_literal
            row["embedding"] = to_vector_literal(embedding)
        inserted = rows(client.table("ai_user_memory").insert(row).execute())
        # Mirrors memory_handlers.memory_add's own fallback: if the insert
        # response carries no row back, fall back to user_id as the id.
        new_id = inserted[0]["id"] if inserted else user_id
        actor_audit("ai_user_memory", new_id, "manual_add", after={"content": content})
        return {"added": True, "memory_id": new_id}

    if action == "edit":
        if not (memory_id and content):
            return err("edit needs memory_id, content")
        existing = rows(client.table("ai_user_memory").select("content")
                        .eq("id", memory_id).limit(1).execute())
        before = {"content": existing[0]["content"]} if existing else None
        embedding = embed_text(content)
        patch: Dict[str, Any] = {"content": content}
        if embedding is not None:
            from core.memory.embeddings import to_vector_literal
            patch["embedding"] = to_vector_literal(embedding)
        client.table("ai_user_memory").update(patch).eq("id", memory_id).execute()
        actor_audit("ai_user_memory", memory_id, "edit", before=before,
                    after={"content": content})
        return {"edited": True, "memory_id": memory_id}

    if action == "pin":
        if not memory_id or pinned is None:
            return err("pin needs memory_id and pinned (true/false)")
        client.table("ai_user_memory").update({"pinned": pinned}) \
            .eq("id", memory_id).execute()
        actor_audit("ai_user_memory", memory_id, "pin" if pinned else "unpin")
        return {"pinned": pinned, "memory_id": memory_id}

    if action == "delete":
        if not memory_id:
            return err("delete needs memory_id")
        if not confirm:
            return not_confirmed(f"archive memory {memory_id}")
        client.table("ai_user_memory").update({"status": "archived"}) \
            .eq("id", memory_id).execute()
        actor_audit("ai_user_memory", memory_id, "archive")
        return {"deleted": True, "memory_id": memory_id}

    return err(f"unknown action {action!r}",
               hint="get_config | set_config | list | add | edit | pin | delete")


@mcp.tool()
@tool_errors
def channels(action: Literal["list", "get", "create", "update", "delete"] = "list",
            channel_id: str = "", env: Env = "staging",
            slug: str = "", name: str = "", kind: str = "",
            is_active: Optional[bool] = None,
            allowed_origins: Optional[List[str]] = None,
            allowed_agent_kinds: Optional[List[str]] = None,
            default_profile_id: str = "", business_id: str = "",
            auth_required: Optional[bool] = None,
            rate_limit_rpm: Optional[int] = None,
            config: Optional[Dict[str, Any]] = None,
            description: str = "", confirm: bool = False) -> Dict[str, Any]:
    """agent_channels registry (mirrors /admin/channels*). action:
    list (optional kind filter — an unrecognized kind is silently
    ignored, same as the portal's chip filter) | get (channel_id) |
    create (slug, name, kind, ...) | update (channel_id + fields — same
    validation as create) | delete (channel_id, confirm=true — soft
    is_active=false; channels are referenced from traces/log analytics so
    this never hard-deletes).

    Reads AND writes go through the env-branch client (client_for_env),
    matching the portal, whose channel writes follow the env switcher
    (admin_service_client) since the env-client-symmetry sweep."""
    from admin.channel_handlers import (_CHANNEL_KINDS, _ORIGIN_RE,
                                        _RATE_LIMIT_MAX, _RATE_LIMIT_MIN,
                                        _SLUG_RE, _TENANT_KINDS, CHANNEL_KIND_LABELS,
                                        _channel_agent_kinds,
                                        _invalidate_channels_cache)

    if action == "list":
        client = client_for_env(env)
        q = client.table("agent_channels").select("*").order("kind").order("slug")
        if kind and kind in _CHANNEL_KINDS:
            q = q.eq("kind", kind)
        return {"channels": rows(q.execute())}

    if action == "get":
        client = client_for_env(env)
        row = get_row(client, "agent_channels", "channel_id", channel_id)
        return {"channel": row} if row else err("channel not found")

    if action in ("create", "update"):
        payload: Dict[str, Any] = {}
        if slug:
            slug_val = slug.strip().lower()
            if not _SLUG_RE.match(slug_val):
                return err("slug must be lowercase letters/digits/_- (2-63 "
                           "chars), starting with a letter")
            payload["slug"] = slug_val
        if name:
            payload["name"] = name.strip()
        if kind:
            if kind not in _CHANNEL_KINDS:
                return err(f"kind must be one of {list(_CHANNEL_KINDS)}")
            payload["kind"] = kind
        if allowed_origins is not None:
            bad = next((o for o in allowed_origins if not _ORIGIN_RE.match(o)), None)
            if bad is not None:
                return err(f"allowed_origins entry {bad!r} is not a valid "
                           "origin (expected http(s)://host[:port], no "
                           "trailing slash or path)")
            payload["allowed_origins"] = list(allowed_origins)
        if allowed_agent_kinds is not None:
            allowed = _channel_agent_kinds()
            payload["allowed_agent_kinds"] = [k for k in allowed_agent_kinds if k in allowed]
        if default_profile_id:
            payload["default_profile_id"] = default_profile_id
        if is_active is not None:
            payload["is_active"] = is_active
        if auth_required is not None:
            payload["auth_required"] = auth_required
        if rate_limit_rpm is not None:
            if not (_RATE_LIMIT_MIN <= rate_limit_rpm <= _RATE_LIMIT_MAX):
                return err(f"rate_limit_rpm must be between {_RATE_LIMIT_MIN} "
                           f"and {_RATE_LIMIT_MAX}")
            payload["rate_limit_rpm"] = rate_limit_rpm
        if config is not None:
            payload["config"] = config
        if description:
            payload["description"] = description.strip()

        effective_kind = payload.get("kind") or kind
        if business_id:
            payload["business_id"] = business_id
        if effective_kind in _TENANT_KINDS and not payload.get("business_id"):
            return err(f"{CHANNEL_KIND_LABELS.get(effective_kind, effective_kind)} "
                       "channels require a business_id (pick one from the "
                       "business picker)")
        # Don't let a stray business_id leak onto any other kind.
        if effective_kind and effective_kind not in _TENANT_KINDS:
            payload["business_id"] = None

        client = client_for_env(env)
        if action == "create":
            if not (payload.get("slug") and payload.get("name") and payload.get("kind")):
                return err("create needs slug, name, kind")
            payload.setdefault("allowed_origins", [])
            payload.setdefault("allowed_agent_kinds", [])
            payload.setdefault("is_active", True)
            payload.setdefault("auth_required", True)
            payload.setdefault("rate_limit_rpm", 60)
            payload.setdefault("config", {})
            payload["created_by"] = current_actor()["user_id"]
            inserted = rows(client.table("agent_channels").insert(payload).execute())
            created = inserted[0] if inserted else payload
            actor_audit("agent_channels", created.get("channel_id", ""), "create",
                        after=created)
            _invalidate_channels_cache()
            return {"created": True, "channel": created}

        if not payload:
            return err("no fields supplied")
        before = get_row(client, "agent_channels", "channel_id", channel_id)
        if not before:
            return err("channel not found")
        client.table("agent_channels").update(payload).eq("channel_id", channel_id).execute()
        actor_audit("agent_channels", channel_id, "update", before=before,
                    after={**before, **payload})
        _invalidate_channels_cache()
        return {"updated": True, "channel_id": channel_id, "changed": sorted(payload)}

    if action == "delete":
        client = client_for_env(env)
        before = get_row(client, "agent_channels", "channel_id", channel_id)
        if not before:
            return err("channel not found")
        if not confirm:
            return not_confirmed(f"deprecate channel {before.get('slug')!r}")
        client.table("agent_channels").update({"is_active": False}) \
            .eq("channel_id", channel_id).execute()
        actor_audit("agent_channels", channel_id, "deprecate", before=before,
                    after={**before, "is_active": False})
        _invalidate_channels_cache()
        return {"deleted": True, "channel_id": channel_id}

    return err(f"unknown action {action!r}",
               hint="list | get | create | update | delete")


@mcp.tool()
@tool_errors
def test_fixtures(action: Literal["list", "create", "update", "delete",
                                  "export"] = "list",
                  fixture_id: str = "", env: Env = "staging",
                  name: str = "", kind: str = "", value: str = "",
                  description: str = "", confirm: bool = False) -> Dict[str, Any]:
    """test_fixtures vault (mirrors the fixtures section of /admin/tests +
    GET /admin/test-fixtures/export) — real JWTs/UUIDs/secrets an operator
    records for pytest/eval runs against auth-gated endpoints. jwt/secret
    kinds are masked "••••" in list output and audit payloads (reuses
    test_fixtures_handlers._mask verbatim); export reveals real values,
    same as the .env.test download it mirrors. action: list (env) |
    create (name, kind, value, description) | update (fixture_id +
    fields) | delete (fixture_id, confirm=true) | export (env ->
    {"lines": ["WAJ_TEST_<UPPER_NAME>=<value>", ...]})."""
    from admin.test_fixtures_handlers import _KINDS, _NAME_PATTERN, _mask

    if action == "list":
        client = client_for_env(env)
        found = rows(client.table("test_fixtures").select("*")
                     .eq("environment", env).order("name").execute())
        return {"fixtures": [_mask(r) for r in found]}

    if action == "export":
        client = client_for_env(env)
        found = rows(client.table("test_fixtures").select("name, value")
                     .eq("environment", env).order("name").execute())
        # A bulk reveal of every stored secret must leave a trace — same
        # posture as env_vars' audited reveal. Names only, never values.
        actor_audit("test_fixtures", None, "export",
                    after={"environment": env, "count": len(found),
                           "names": sorted(r.get("name", "") for r in found)})
        return {"lines": [f"WAJ_TEST_{r['name'].upper()}={r['value']}" for r in found]}

    if action == "create":
        validate_env(env)
        name_val = (name or "").strip()
        if not _NAME_PATTERN.match(name_val):
            return err("name must match ^[a-z][a-z0-9_]{0,62}$ — lowercase "
                       "letters, digits, underscore; starts with a letter; "
                       "max 63 chars.")
        kind_val = (kind or "string").strip()
        if kind_val not in _KINDS:
            return err(f"kind must be one of {_KINDS}")
        if value == "":
            return err("value is required")
        payload = {"name": name_val, "environment": env, "kind": kind_val,
                   "value": value, "description": description.strip() or None,
                   "created_by": current_actor()["user_id"]}
        client = client_for_env(env)
        inserted = rows(client.table("test_fixtures").insert(payload).execute())
        created = inserted[0] if inserted else payload
        actor_audit("test_fixtures", created.get("fixture_id", ""), "create",
                    after=_mask(created))
        return {"created": True, "fixture": _mask(created)}

    if action == "update":
        client = client_for_env(env)
        before = get_row(client, "test_fixtures", "fixture_id", fixture_id)
        if not before:
            return err("test fixture not found")
        payload: Dict[str, Any] = {}
        if name:
            name_val = name.strip()
            if not _NAME_PATTERN.match(name_val):
                return err("name must match ^[a-z][a-z0-9_]{0,62}$")
            payload["name"] = name_val
        if kind:
            if kind not in _KINDS:
                return err(f"kind must be one of {_KINDS}")
            payload["kind"] = kind
        if value:
            payload["value"] = value
        if description:
            payload["description"] = description.strip() or None
        if not payload:
            return err("no fields supplied")
        from datetime import datetime, timezone
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        client.table("test_fixtures").update(payload).eq("fixture_id", fixture_id).execute()
        actor_audit("test_fixtures", fixture_id, "update",
                    before=_mask(before), after=_mask({**before, **payload}))
        return {"updated": True, "fixture_id": fixture_id, "changed": sorted(payload)}

    if action == "delete":
        client = client_for_env(env)
        before = get_row(client, "test_fixtures", "fixture_id", fixture_id)
        if not before:
            return err("test fixture not found")
        if not confirm:
            return not_confirmed(f"delete test fixture {before.get('name')!r}")
        client.table("test_fixtures").delete().eq("fixture_id", fixture_id).execute()
        actor_audit("test_fixtures", fixture_id, "delete", before=_mask(before))
        return {"deleted": True, "fixture_id": fixture_id}

    return err(f"unknown action {action!r}",
               hint="list | create | update | delete | export")


@mcp.tool()
@tool_errors
def jobs(action: Literal["list", "get", "cancel"] = "list",
        job_id: str = "", status: str = "",
        kind: str = "", env: Env = "staging",
        limit: int = 50) -> Dict[str, Any]:
    """admin_jobs tracker (mirrors the Jobs tab of /admin/live + the
    polling/cancel routes). `env` selects the branch the tracker rows live
    on: an env-pinned spawn (the e2e_runs trigger, voice_scenarios run —
    both wrap use_env(env)) writes its admin_jobs row to that env's branch,
    so get/list/cancel must resolve the SAME one — pass the env the job was
    triggered for. Single-DB deployments (cross-env vars unset) fall back
    to the local branch, where any env value works. action: list
    (status/kind filters — an invalid filter value returns an empty list
    rather than erroring, same as the portal) | get (job_id) | cancel
    (job_id — cooperative: flips status='cancelled' if the job is still
    queued/running; cancel_applied=false means it had already finished.
    Always audits the operator's intent, unconditionally, same as the
    portal)."""
    from admin._jobs import cancel_job, get_job, list_recent_jobs

    with use_env(env):
        if action == "list":
            found = list_recent_jobs(limit=limit, status=status or None, kind=kind or None)
            return {"jobs": found, "count": len(found)}
        if action == "get":
            if not job_id:
                return err("get needs job_id")
            row = get_job(job_id)
            return {"job": row} if row else err("job not found")
        if action == "cancel":
            if not job_id:
                return err("cancel needs job_id")
            flipped = cancel_job(job_id)
            actor_audit("admin_jobs", job_id, "cancel", after={"cancel_applied": flipped})
            return {"cancel_applied": flipped, "job_id": job_id}
    return err(f"unknown action {action!r}", hint="list | get | cancel")
