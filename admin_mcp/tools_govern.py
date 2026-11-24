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

from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import Field

from admin_mcp._common import (READ_ONLY, Confirm, Env, actor_audit, client_for_env,
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
    """List portal users, newest first, each with its is_superuser flag.
    superusers_only=true returns only superusers. Read-only. Grant or revoke
    superuser with users(action="set_superuser")."""
    found = _list_users(limit)
    if superusers_only:
        found = [u for u in found if u.get("is_superuser")]
    return {"users": found, "count": len(found)}


@mcp.tool()
@tool_errors
def users(action: Literal["list", "set_superuser"] = "list",
          user_id: str = "", grant: bool = False,
          confirm: bool = False, limit: int = 100) -> Dict[str, Any]:
    """Portal users.

    Actions:
    - list (default): users with their superuser flag. The read-only
      list_users tool returns the same.
    - set_superuser (user_id, grant=true or false, confirm=true): grant or
      revoke superuser. A superuser has FULL admin access, including this MCP
      server."""
    # Maintainer notes (kept out of the description clients read):
    # Not annotated: `list` reads, `set_superuser` writes. A destructive hint
    # made every plain listing trip the host's destructive-action prompt;
    # set_superuser stays confirm-gated.
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
def api_keys(
    action: Annotated[Literal["list", "create", "revoke"], Field(
        description="list | create | revoke.")] = "list",
    name: Annotated[str, Field(
        description="create: what will use the key (shown in lists, 1-120 characters).")] = "",
    scopes: Annotated[Optional[List[Literal["chat", "admin"]]], Field(
        description="create: `chat` (call agents over the chat API) and/or `admin` "
                    "(chat plus this MCP server: full control). Default [\"chat\"].")] = None,
    expires_in_days: Annotated[int, Field(
        description="create: days until the key stops working (1-3650). 0 = never expires.")] = 0,
    key_id: Annotated[str, Field(description="revoke: the key's id (from list).")] = "",
    include_revoked: Annotated[bool, Field(
        description="list: also return revoked keys (default true).")] = True,
    confirm: Confirm = False,
) -> Dict[str, Any]:
    """API keys issued by this engine. A key is sent as "Authorization:
    Bearer wae_..." to POST /v1/agents/{slug}/chat (scope chat or admin) or
    to this MCP server (scope admin only).

    Actions:
    - list (default): name, key_prefix, scopes, created_at, last_used_at,
      expires_at and status (active, expired or revoked). Never the key.
    - create (name; optional scopes, expires_in_days): returns the key ONCE,
      in key.
    - revoke (key_id, confirm=true): takes effect on the key's next request
      and cannot be undone.

    Only a digest of a key is stored, so it can never be read again after
    create. Hand it to the user straight away, and do not write it to a file,
    a log or a commit. An admin key gives full control of this engine. Keys
    belong to the engine instance, not to an environment, so there is no env.
    Create and revoke are audited under the caller."""
    from admin.services import api_keys as service

    actor_id = current_actor()["user_id"]
    if action == "list":
        found = service.list_keys(include_revoked=include_revoked)
        return {"api_keys": found, "count": len(found)}
    if action == "create":
        row, key = service.create_key(
            name, scopes or ["chat"], created_by=actor_id,
            expires_at=service.expiry_from_days(expires_in_days))
        return {"created": True, "api_key": row, "key": key,
                "note": "This is the only time the key is shown. Store it now; "
                        "it cannot be retrieved later."}
    if action == "revoke":
        if not key_id:
            return err("revoke needs key_id", hint="api_keys action=list")
        existing = service.get_key(key_id)
        if existing is None:
            return err("API key not found")
        if not confirm:
            return not_confirmed(
                f"revoke API key {existing['name']!r} ({existing['key_prefix']}...): "
                "anything using it is refused from its next request", api_key=existing)
        return {"revoked": True, "api_key": service.revoke_key(key_id, revoked_by=actor_id)}
    return err(f"unknown action {action!r}", hint="list | create | revoke")


@mcp.tool()
@tool_errors
def audit(action: Literal["list", "get", "probe_write", "verify"] = "list",
          env: Env = "staging", entity_type: str = "",
          audit_id: str = "", limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """The audit log of config changes.

    Actions:
    - list (default): newest first, paged with limit and offset;
      entity_type filters.
    - get (audit_id).
    - probe_write: WRITES one marker row to prove the log can still be
      written. It verifies nothing else; there is no integrity check. verify
      is its old, deprecated name."""
    # Maintainer notes (kept out of the description clients read):
    # Mirrors GET /admin/audit, GET /admin/audit/{id} and POST
    # /admin/audit/verify. probe_write records
    # record_change(entity_type="agent_audit_log", entity_id=<all-zero uuid>,
    # action="CALLEE_VERIFY"), like the portal's "verify" button. There is no
    # hash chain to walk.
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
                           "pin", "delete", "assign"] = "list",
          agent_kind: str = _DEFAULT_MEMORY_KIND, env: Env = "staging",
          memory_id: str = "", user_id: str = "", business_id: str = "",
          content: str = "", pinned: Optional[bool] = None,
          config: Optional[Dict[str, Any]] = None,
          confirm: bool = False,
          limit: int = 50,
          profile_id: str = "") -> Dict[str, Any]:
    """Per-user memories and each agent's memory settings.

    Actions:
    - get_config (agent_kind, or profile_id): the memory settings, merged
      with the defaults.
    - set_config (config, with agent_kind or profile_id): replace the memory
      settings with config, a nested object in the stored shape.
    - list (default; agent_kind, user_id as a UUID or email): that user's
      memories, at most limit (default 50).
    - add (user_id as a UUID or email, content, agent_kind; business_id
      where the build has tenants).
    - assign (memory_id, agent_kind=custom:<slug>): give an unassigned
      custom-agent memory to that agent.
    - edit (memory_id, content): replace the text. It is re-embedded; if
      embedding fails the text is still saved.
    - pin (memory_id, pinned=true or false).
    - delete (memory_id, confirm=true): archives the memory.

    Each custom agent keeps its own memories: pass agent_kind=custom:<slug>
    to list or add one agent's. list for such an agent also returns
    unassigned_memories: older custom-agent memories that no agent recalls
    until they are assigned.

    get_config and set_config use the active profile for agent_kind in env.
    Pass profile_id to read or write one SPECIFIC profile instead: a draft, a
    copy or a voice specialist, which the agent_kind lookup never reaches."""
    # Maintainer notes (kept out of the description clients read):
    # Mirrors /admin/memory. Memories are ai_user_memory rows; settings are
    # agent_profiles.memory_config. set_config takes structured JSON, so it
    # skips the HTML form's checkbox/flat-field parsing. `edit` uses the same
    # embedder as the runtime. custom:<slug> is the value the Memory screen's
    # picker uses. A voice brain's memory_config lives on the specialist row.
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
        from admin.agent_kinds import split_agent_choice
        from admin.memory_handlers import memory_row

        if not (user_id and content):
            return err("add needs user_id, content")
        agent_kind = _resolve_agent_kind(agent_kind)
        kind, slug = split_agent_choice(agent_kind)
        if kind == "custom" and not slug:
            return err("a custom agent's memory names the agent",
                       hint="agent_kind=custom:<agent slug>")
        # user_id may be an email, as for `list`: resolve it the same way
        # instead of handing the database a non-uuid.
        resolved = memory_screen.resolve_user_id(user_id)
        if not resolved:
            return err("no user with that id or email",
                       hint="pass the user's id (uuid) or the email they sign in with")
        user_id = resolved
        embedding = embed_text(content)
        row = memory_row(user_id=user_id, agent_kind=agent_kind, content=content,
                         business_id=business_id or None)
        if embedding is not None:
            from core.memory.embeddings import to_vector_literal
            row["embedding"] = to_vector_literal(embedding)
        inserted = rows(client.table("ai_user_memory").insert(row).execute())
        # Mirrors memory_handlers.memory_add's own fallback: if the insert
        # response carries no row back, fall back to user_id as the id.
        new_id = inserted[0]["id"] if inserted else user_id
        actor_audit("ai_user_memory", new_id, "manual_add", after={"content": content})
        return {"added": True, "memory_id": new_id, "agent_kind": agent_kind,
                "user_id": user_id}

    if action == "assign":
        from admin.agent_kinds import split_agent_choice

        kind, slug = split_agent_choice(_resolve_agent_kind(agent_kind))
        if not (memory_id and slug):
            return err("assign needs memory_id and agent_kind=custom:<agent slug>")
        moved = rows(client.table("ai_user_memory").update({"agent_slug": slug})
                     .eq("id", memory_id).eq("agent_kind", kind)
                     .is_("agent_slug", "null").execute())
        if not moved:
            return err("no unassigned custom-agent memory with that id")
        actor_audit("ai_user_memory", memory_id, "assign", after={"agent_slug": slug})
        return {"assigned": True, "memory_id": memory_id, "agent_slug": slug}

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
               hint="get_config | set_config | list | add | assign | edit | pin | delete")


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
    """Channels: the entry points agents are reached through, each with its
    allowed origins, allowed agents, auth and rate limit.

    Actions:
    - list (default): kind filters; an unknown kind is ignored.
    - get (channel_id).
    - create (slug, name, kind; optional description, allowed_origins,
      allowed_agent_kinds, default_profile_id, auth_required, rate_limit_rpm,
      config, is_active; tenant channel kinds also need business_id).
    - update (channel_id plus the fields to change): same checks as create.
    - delete (channel_id, confirm=true): switches the channel off
      (is_active=false). It is never removed, because traces and analytics
      refer to it."""
    # Maintainer notes (kept out of the description clients read):
    # Table agent_channels (mirrors /admin/channels*). Reads AND writes go
    # through the env-branch client (client_for_env), matching the portal,
    # whose channel writes follow the env switcher (admin_service_client).
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
            from admin.channel_handlers import _channel_choice_known

            allowed = _channel_agent_kinds()
            # A generic agent is named `custom:<slug>` (one per agent; the
            # bare `custom` kind would allow every one of them).
            payload["allowed_agent_kinds"] = [
                k for k in allowed_agent_kinds
                if k in allowed or _channel_choice_known(k)]
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
    """Stored test values (tokens, ids, secrets) that test and eval runs
    use against endpoints that need auth. Values of kind jwt and secret are
    shown as "••••" in list output and in the audit log.

    Actions:
    - list (default): the fixtures in env.
    - create (name, value; optional kind, description): kind is jwt, uuid,
      string (default), json or secret.
    - update (fixture_id plus the fields to change).
    - delete (fixture_id, confirm=true).
    - export: returns the REAL, unmasked values as {"lines":
      ["WAJ_TEST_<UPPER_NAME>=<value>", ...]}. The export is audited."""
    # Maintainer notes (kept out of the description clients read):
    # Table test_fixtures. Mirrors the fixtures section of /admin/tests and
    # GET /admin/test-fixtures/export (the .env.test download). Masking reuses
    # test_fixtures_handlers._mask verbatim.
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
    """Background jobs started by other tools (KB uploads, test runs,
    end-to-end runs). Pass the env the job was started for, or it will not be
    found. On a single-database install any env works.

    Actions:
    - list (default): recent jobs; status and kind filter. An invalid
      filter value returns an empty list, not an error.
    - get (job_id).
    - cancel (job_id): marks a queued or running job cancelled.
      cancel_applied=false means it had already finished. The request is
      audited either way."""
    # Maintainer notes (kept out of the description clients read):
    # Table admin_jobs (mirrors the Jobs tab of /admin/live and its
    # polling/cancel routes). An env-pinned spawn (e2e_runs trigger,
    # voice_scenarios run; both wrap use_env(env)) writes its row to that env's
    # branch, so get/list/cancel must resolve the same one. Cancel is
    # cooperative.
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
