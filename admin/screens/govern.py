# admin/screens/govern.py
"""Context builders for the Ship / Govern screens.

- Activity (/admin/audit): turns raw `agent_audit_log` rows into sentences
  ("Sara published WAJ AI v12 · 2h ago"), resolves actors to people (main DB
  `public.user`, the same table the superuser check reads) and entities to
  names + links, and builds the Who / What / When chip filters.
- Team & access (/admin/users): one table of people with an Admin flag
  (the portal's superuser bit), last activity, and service accounts hidden.
- Releases (/admin/env): the promote overview grid — per agent: live version,
  unpublished changes, last quality score — plus the "what will change" list
  the promote dialog shows before confirming.
- Diagnostics (/admin/drift): findings grouped by agent.

Every function takes its DB clients as arguments (the handlers pass
`client_for(env)` / `get_main_client()`), so tests can hand in fakes and no
lookup here ever picks a branch on its own. All lookups are best-effort: a
failed name lookup falls back to a short id, never a 500.
"""
from __future__ import annotations

import datetime as _dt
import difflib
import json
import logging
import re
import uuid
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

SYSTEM_ACTOR_PREFIX = "00000000-0000-0000-0000-"


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError):
        return False


def _rows(query) -> List[Dict[str, Any]]:
    try:
        return query.execute().data or []
    except Exception as exc:  # noqa: BLE001 — lookups are best-effort
        logger.warning("govern: lookup failed (%s)", exc)
        return []


def parse_ts(value: Any) -> Optional[_dt.datetime]:
    if not value:
        return None
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)
    try:
        s = str(value).replace("Z", "+00:00")
        # postgrest can return 5-digit fractional seconds; fromisoformat in
        # 3.11 accepts any precision.
        d = _dt.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return None


def relative_time(value: Any, now: Optional[_dt.datetime] = None) -> str:
    """'just now' / '5m ago' / '2h ago' / '3d ago' / '7 Oct' / '7 Oct 2025'."""
    d = parse_ts(value)
    if not d:
        return "—"
    now = now or _dt.datetime.now(_dt.timezone.utc)
    secs = (now - d).total_seconds()
    if secs < 0:
        secs = 0
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    if secs < 7 * 86400:
        return f"{int(secs // 86400)}d ago"
    return short_date(d, now)


def short_date(value: Any, now: Optional[_dt.datetime] = None) -> str:
    d = parse_ts(value)
    if not d:
        return "—"
    now = now or _dt.datetime.now(_dt.timezone.utc)
    if d.year == now.year:
        return f"{d.day} {d.strftime('%b')}"
    return f"{d.day} {d.strftime('%b %Y')}"


def full_time(value: Any) -> str:
    d = parse_ts(value)
    if not d:
        return "—"
    return f"{d.day} {d.strftime('%b %Y, %H:%M')} UTC"


def humanize_key(key: str) -> str:
    k = (key or "").strip("_").replace("_", " ")
    return (k[:1].upper() + k[1:]) if k else key


# ---------------------------------------------------------------------------
# People (main DB `public.user`)
# ---------------------------------------------------------------------------


def initial(label: str) -> str:
    return next((ch.upper() for ch in (label or "") if ch.isalnum()), "?")


def person_label(row: Mapping[str, Any]) -> str:
    """email > +phone > short id — the same order /admin/users always used."""
    if row.get("staff_name"):
        return row["staff_name"]
    if row.get("email"):
        return row["email"]
    if row.get("phone_number"):
        cc = row.get("country_code") or ""
        return f"+{cc}{row['phone_number']}"
    return f"User {(row.get('id') or '')[:8]}"


def resolve_people(main_client: Any, ids: Iterable[Optional[str]]) -> Dict[str, Dict[str, Any]]:
    """{user_id: {label, email, is_system}} for the given `public.user` ids.

    Actors are `public.user.id` values (admin/auth.py resolves the signed-in
    superuser to that row). Unknown or system ids still get an entry, so the
    caller never has to special-case a miss.
    """
    wanted = sorted({str(i) for i in ids if i})
    out: Dict[str, Dict[str, Any]] = {}
    lookup = [i for i in wanted if _is_uuid(i)]
    found: Dict[str, Dict[str, Any]] = {}
    cols = "id, auth_id, email, phone_number, country_code, role"
    if lookup and main_client is not None:
        for row in _rows(main_client.table("user").select(cols).in_("id", lookup)):
            if isinstance(row, dict) and row.get("id"):
                found[str(row["id"])] = row
        # Some writers (MCP, scripts) record the auth user id (JWT `sub`)
        # rather than `public.user.id` — resolve those through `auth_id`.
        rest = [i for i in lookup if i not in found]
        if rest:
            for row in _rows(main_client.table("user").select(cols).in_("auth_id", rest)):
                if isinstance(row, dict) and row.get("auth_id"):
                    found[str(row["auth_id"])] = row
    for i in wanted:
        row = found.get(i)
        system = i.startswith(SYSTEM_ACTOR_PREFIX) and not (row and row.get("email"))
        if system:
            out[i] = {"label": "System", "email": None, "is_system": True}
        elif row:
            out[i] = {"label": person_label(row), "email": row.get("email"), "is_system": False}
        else:
            out[i] = {"label": f"Unknown user ({i[:8]})", "email": None, "is_system": False}
    return out


def label_api_key_actors(people: Dict[str, Dict[str, Any]], client: Any) -> Dict[str, Dict[str, Any]]:
    """Name the actors that are API keys, in place.

    A change made over the MCP server with an API key is recorded with the
    key's own id as the actor (admin_mcp/auth.py), which `public.user` knows
    nothing about. Without this the log would say "Unknown user"; with it, it
    says which key. Best effort: a failed lookup leaves the labels as they are.
    """
    unknown = [i for i, p in people.items()
               if _is_uuid(i) and not p.get("is_system") and not p.get("email")
               and str(p.get("label") or "").startswith("Unknown user")]
    if not unknown or client is None:
        return people
    try:
        found = _rows(client.table("api_keys").select("id, name, key_prefix").in_("id", unknown))
    except Exception:  # noqa: BLE001
        return people
    for row in found:
        if isinstance(row, dict) and str(row.get("id")) in people:
            people[str(row["id"])] = {"label": f"API key: {row.get('name') or row.get('key_prefix')}",
                                      "email": None, "is_system": False, "is_api_key": True}
    return people


def actor_display(actor_id: Optional[str], people: Mapping[str, Dict[str, Any]],
                  me: Optional[str] = None) -> Dict[str, Any]:
    if not actor_id:
        return {"label": "System", "email": None, "is_system": True, "is_me": False, "initial": "S"}
    p = dict(people.get(str(actor_id)) or {"label": f"User {str(actor_id)[:8]}", "email": None,
                                             "is_system": False})
    p["is_me"] = bool(me and str(actor_id) == str(me))
    p["initial"] = initial(p.get("label") or "")
    return p


# ---------------------------------------------------------------------------
# Activity — entity registry, verbs, filters
# ---------------------------------------------------------------------------

# entity_type → (noun, table, pk, name columns, link template, area)
# link template: "{id}" is the entity id; None = no page for one item (the
# fallback link is the area page).
ENTITY_TYPES: Dict[str, Tuple[str, Optional[str], Optional[str], Tuple[str, ...], Optional[str], str]] = {
    "agent_profiles": ("agent", "agent_profiles", "profile_id", ("display_name", "slug"),
                       "/admin/agents?profile_id={id}", "agents"),
    "agent_profile_versions": ("agent version", None, None, (), None, "agents"),
    "agent_prompts": ("prompt", "agent_prompts", "prompt_id", ("slug",), "/admin/prompts/{id}", "agents"),
    "agent_prompt_versions": ("prompt version", "agent_prompt_versions", "prompt_version_id", (),
                              None, "agents"),
    "agent_models": ("model", "agent_models", "model_id", ("name",), "/admin/agent-models/{id}", "agents"),
    "agent_profile_skills": ("agent skill", None, None, (), None, "agents"),
    "agent_profile_mcp_servers": ("agent integration", None, None, (), None, "agents"),
    "ai_voice_budgets": ("voice budget", None, None, (), "/admin/voice-budgets", "settings"),
    "api_keys": ("API key", "api_keys", "id", ("name",), "/admin/api-keys", "settings"),
    "provider_keys": ("model key", None, None, (), "/admin/model-keys", "settings"),
    "agent_tools": ("tool", "agent_tools", "tool_id", ("display_name", "python_name"),
                    "/admin/tools/{id}", "connections"),
    "agent_mcp_servers": ("integration", "agent_mcp_servers", "mcp_server_id", ("name",),
                          "/admin/agent-mcp-servers/{id}", "connections"),
    "agent_mcp_tools": ("integration tool", None, None, (), None, "connections"),
    "agent_channels": ("channel", "agent_channels", "channel_id", ("name", "slug"),
                       "/admin/channels/{id}", "connections"),
    "agent_data_sources": ("knowledge base", "agent_data_sources", "source_id", ("name",),
                           "/admin/kb/{id}", "knowledge"),
    "agent_skills": ("skill", "agent_skills", "skill_id", ("display_name", "slug"),
                     "/admin/skills/{id}", "knowledge"),
    "agent_skill_versions": ("skill version", None, None, (), None, "knowledge"),
    "agent_skill_resources": ("skill file", None, None, (), None, "knowledge"),
    "ai_user_memory": ("memory", None, None, (), "/admin/memory", "knowledge"),
    "agent_eval_suites": ("test set", "agent_eval_suites", "suite_id", ("name",),
                          "/admin/evals/suites/{id}", "testing"),
    "agent_eval_runs": ("test run", None, None, (), "/admin/evals/runs/{id}", "testing"),
    "agent_eval_schedules": ("test schedule", "agent_eval_schedules", "schedule_id", ("name",),
                             "/admin/evals/scheduling", "testing"),
    "ai_voice_scenarios": ("voice check", "ai_voice_scenarios", "scenario_id", ("title", "slug"),
                           "/admin/voice-scenarios", "testing"),
    "test_fixtures": ("test fixtures", None, None, (), "/admin/tests", "testing"),
    "agent_test_runs": ("engineering test run", None, None, (), "/admin/tests/{id}", "testing"),
    "agent_env_vars": ("environment variable", "agent_env_vars", "var_id", ("name",),
                       "/admin/env-vars/{id}", "releases"),
    "agent_deployment_targets": ("deploy target", "agent_deployment_targets", "target_id", ("label",),
                                 "/admin/deployments?tab=targets", "releases"),
    "environment": ("environment", None, None, (), "/admin/deployments?tab=history", "releases"),
    "admin_jobs": ("background job", None, None, (), "/admin/live?tab=jobs", "releases"),
    "user": ("team member", None, None, (), "/admin/users", "team"),
    "agent_audit_log": ("activity log", None, None, (), "/admin/audit", "team"),
}

AREA_PAGES = {
    "agents": "/admin/agents", "knowledge": "/admin/kb", "connections": "/admin/tools",
    "testing": "/admin/evals", "releases": "/admin/env", "team": "/admin/users",
    "settings": "/admin/settings",
}

# action (lower-cased) → verb phrase. `{noun}` is replaced by the entity noun;
# a phrase without `{noun}` is followed by the noun automatically.
ACTION_VERBS: Dict[str, str] = {
    "create": "created", "update": "updated", "delete": "deleted", "upsert": "saved",
    "edit": "edited", "set": "set", "archive": "archived", "revoke": "revoked", "deprecate": "deprecated",
    "fork": "copied", "use_template": "created from a template the", "upload": "uploaded files to",
    "export": "exported", "reveal": "revealed the values of", "run": "ran", "cancel": "cancelled",
    "fire": "started", "due_fire": "ran on schedule", "manual_add": "added",
    "import_scenarios": "imported scenarios into", "memory_config_update": "changed memory settings of",
    "prompt_ab_launch": "started an A/B test on", "cors_dirty_clear": "refreshed the allowed sites of",
    "validation_override": "saved past a validation warning on",
    "serve_prompt_version": "switched the live instructions of", "serve": "switched the live version of",
    "pin_served_version": "pinned the served version of", "pin": "pinned",
    "save_version": "saved a version of", "restore_version": "restored an earlier version of",
    "config_promote": "published", "eval_gate_override": "published past a failed quality check",
    "prompt_tool_mismatch_override": "published past a tool check",
    "prod_config_bundle_push": "refreshed the live config bundle for",
    "deploy_triggered": "started a code deploy to", "rollback_triggered": "rolled back",
    "grant_superuser": "gave admin access to", "revoke_superuser": "removed admin access from",
    "callee_verify": "verified", "tool_call": "called a tool for", "job_trigger": "started",
    "human_handoff": "handed off a conversation of", "numeric_guard": "blocked a number in",
    "otp_skip": "skipped an OTP check for", "sql_exec": "ran a query for", "dial": "placed a call for",
}

# Actions whose noun reads better dropped ("published WAJ AI", not "published agent WAJ AI").
_NOUNLESS = {
    "config_promote", "eval_gate_override", "prompt_tool_mismatch_override", "prod_config_bundle_push",
    "deploy_triggered", "rollback_triggered", "grant_superuser", "revoke_superuser",
    "serve_prompt_version", "serve", "pin_served_version", "save_version", "restore_version",
}

# Chip filter "What" → (label, entity types, actions). A row matches when its
# entity type OR its action is listed.
WHAT_FILTERS: Dict[str, Tuple[str, Tuple[str, ...], Tuple[str, ...]]] = {
    "publishing": ("Publishing & releases",
                   ("environment", "agent_deployment_targets", "agent_env_vars", "agent_profile_versions"),
                   ("CONFIG_PROMOTE", "EVAL_GATE_OVERRIDE", "PROMPT_TOOL_MISMATCH_OVERRIDE",
                    "PROD_CONFIG_BUNDLE_PUSH", "DEPLOY_TRIGGERED", "ROLLBACK_TRIGGERED",
                    "serve_prompt_version", "pin_served_version", "SAVE_VERSION", "RESTORE_VERSION")),
    "agents": ("Agents", tuple(k for k, v in ENTITY_TYPES.items() if v[5] == "agents"), ()),
    "knowledge": ("Knowledge", tuple(k for k, v in ENTITY_TYPES.items() if v[5] == "knowledge"), ()),
    "connections": ("Connections", tuple(k for k, v in ENTITY_TYPES.items() if v[5] == "connections"), ()),
    "testing": ("Testing", tuple(k for k, v in ENTITY_TYPES.items() if v[5] == "testing"), ()),
    "team": ("Team & settings", ("user", "agent_audit_log", "ai_voice_budgets", "api_keys", "provider_keys"), ()),
}

WHEN_FILTERS: Dict[str, Tuple[str, Optional[int]]] = {
    "": ("Any time", None), "24h": ("Last 24 hours", 1), "7d": ("Last 7 days", 7), "30d": ("Last 30 days", 30),
}

_SAFE_FILTER_VALUE = re.compile(r"^[A-Za-z0-9_]+$")


def apply_activity_filters(query: Any, *, who: str, what: str, when: str, entity_type: Optional[str],
                           me: Optional[str], now: Optional[_dt.datetime] = None) -> Any:
    """Apply the chip filters to a postgrest query on agent_audit_log."""
    if entity_type:  # legacy ?entity_type= links keep working
        query = query.eq("entity_type", entity_type)
    if who == "me" and me:
        query = query.eq("actor_user_id", me)
    elif who == "system":
        query = query.or_(f"actor_user_id.is.null,actor_user_id.like.{SYSTEM_ACTOR_PREFIX}*")
    elif who and _is_uuid(who):
        query = query.eq("actor_user_id", who)
    if what in WHAT_FILTERS:
        _, types, actions = WHAT_FILTERS[what]
        parts = []
        types = [t for t in types if _SAFE_FILTER_VALUE.match(t)]
        actions = [a for a in actions if _SAFE_FILTER_VALUE.match(a)]
        if types:
            parts.append(f"entity_type.in.({','.join(types)})")
        if actions:
            parts.append(f"action.in.({','.join(actions)})")
        if parts:
            query = query.or_(",".join(parts))
    days = WHEN_FILTERS.get(when, ("", None))[1]
    if days:
        since = (now or _dt.datetime.now(_dt.timezone.utc)) - _dt.timedelta(days=days)
        query = query.gte("created_at", since.isoformat())
    return query


def resolve_entities(client: Any, rows: Sequence[Mapping[str, Any]]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """{(entity_type, entity_id): {name, href}} — one query per entity type."""
    by_type: Dict[str, set] = {}
    for r in rows:
        et, eid = r.get("entity_type"), r.get("entity_id")
        if et and eid:
            by_type.setdefault(et, set()).add(str(eid))
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for et, ids in by_type.items():
        spec = ENTITY_TYPES.get(et)
        if not spec:
            continue
        _, table, pk, name_cols, _, _ = spec
        if not table or not pk or client is None:
            continue
        lookup = [i for i in ids if _is_uuid(i)]
        if not lookup:
            continue
        extra = ", prompt_id, version_number" if et == "agent_prompt_versions" else ""
        cols = ", ".join((pk,) + tuple(name_cols)) + extra
        for row in _rows(client.table(table).select(cols).in_(pk, lookup)):
            if not isinstance(row, dict):
                continue
            rid = str(row.get(pk))
            name = next((row.get(c) for c in name_cols if row.get(c)), None)
            href = None
            if et == "agent_prompt_versions":
                if row.get("version_number") is not None:
                    name = f"v{row['version_number']}"
                if row.get("prompt_id"):
                    href = f"/admin/prompts/{row['prompt_id']}"
            out[(et, rid)] = {"name": name, "href": href, "prompt_id": row.get("prompt_id")}
    # An agent_prompt_versions row whose id is not a version: the portal's
    # "Save anyway" (validation_override) audits under the PROMPT's id. Name it
    # after the agent it was saved for (after.profile_id), else the prompt.
    missing = sorted({str(r.get("entity_id")) for r in rows
                      if r.get("entity_type") == "agent_prompt_versions"
                      and _is_uuid(r.get("entity_id"))
                      and ("agent_prompt_versions", str(r.get("entity_id"))) not in out})
    if missing and client is not None:
        prompts = {str(r.get("prompt_id")): r for r in
                   _rows(client.table("agent_prompts").select("prompt_id, slug")
                         .in_("prompt_id", missing)) if isinstance(r, dict)}
        if prompts:
            prof_ids = sorted({str((r.get("after") or {}).get("profile_id")) for r in rows
                               if isinstance(r.get("after"), dict)
                               and _is_uuid((r.get("after") or {}).get("profile_id"))})
            agents = {str(r.get("profile_id")): r.get("display_name") for r in
                      _rows(client.table("agent_profiles").select("profile_id, display_name")
                            .in_("profile_id", prof_ids)) if isinstance(r, dict)} if prof_ids else {}
            for r in rows:
                eid = str(r.get("entity_id") or "")
                pr = prompts.get(eid)
                if r.get("entity_type") != "agent_prompt_versions" or not pr:
                    continue
                after = r.get("after") if isinstance(r.get("after"), dict) else {}
                what = "instructions" if pr.get("slug") == "system" \
                    else humanize_key(pr.get("slug") or "prompt").lower()
                agent = agents.get(str(after.get("profile_id")))
                out[("agent_prompt_versions", eid)] = {
                    "name": f"{agent} {what}" if agent else what, "href": None,
                    "prompt_id": None}
    # Prompt versions read better with their prompt's name: "voice_persona v3".
    pids = sorted({str(v["prompt_id"]) for (et, _), v in out.items()
                   if et == "agent_prompt_versions" and v.get("prompt_id") and _is_uuid(v.get("prompt_id"))})
    if pids and client is not None:
        slugs = {str(r.get("prompt_id")): r.get("slug") for r in
                 _rows(client.table("agent_prompts").select("prompt_id, slug").in_("prompt_id", pids))
                 if isinstance(r, dict)}
        for (et, _), v in out.items():
            if et == "agent_prompt_versions" and slugs.get(str(v.get("prompt_id"))):
                v["name"] = f"{slugs[str(v['prompt_id'])]} {v['name'] or ''}".strip()
    return out


def prompt_version_labels(client: Any, *payloads: Any) -> Dict[str, str]:
    """{prompt_version_id: 'v3'} for the ids inside `prompt_version_ids`
    values of the given before/after dicts (audit detail readability)."""
    ids = set()
    for p in payloads:
        if isinstance(p, dict) and isinstance(p.get("prompt_version_ids"), dict):
            ids.update(str(v) for v in p["prompt_version_ids"].values() if _is_uuid(v))
    if not ids or client is None:
        return {}
    return {str(r.get("prompt_version_id")): f"v{r.get('version_number')}" for r in
            _rows(client.table("agent_prompt_versions").select("prompt_version_id, version_number")
                  .in_("prompt_version_id", sorted(ids))) if isinstance(r, dict)}


def promote_versions(client: Any, rows: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    """audit_id → version number minted by that CONFIG_PROMOTE row.

    The promote audit row is written just before the snapshot, so match each
    row to the first version of the same profile promoted within 10 minutes
    after it."""
    promotes = [r for r in rows if (r.get("action") or "").upper() == "CONFIG_PROMOTE"
                and _is_uuid(r.get("entity_id"))]
    if not promotes or client is None:
        return {}
    pids = sorted({str(r["entity_id"]) for r in promotes})
    versions = _rows(
        client.table("agent_profile_versions")
        .select("profile_id, version_number, promoted_at")
        .in_("profile_id", pids).order("promoted_at").limit(2000)
    )
    by_pid: Dict[str, List[Tuple[_dt.datetime, int]]] = {}
    for v in versions:
        if not isinstance(v, dict):
            continue
        t = parse_ts(v.get("promoted_at"))
        if t and v.get("version_number") is not None:
            by_pid.setdefault(str(v.get("profile_id")), []).append((t, int(v["version_number"])))
    out: Dict[str, int] = {}
    for r in promotes:
        t0 = parse_ts(r.get("created_at"))
        if not t0:
            continue
        for t, num in sorted(by_pid.get(str(r["entity_id"]), [])):
            if t0 - _dt.timedelta(seconds=5) <= t <= t0 + _dt.timedelta(minutes=10):
                out[str(r.get("audit_id"))] = num
                break
    return out


def _entity_fallback_name(et: str, eid: Optional[str], row: Mapping[str, Any]) -> Optional[str]:
    if et == "environment" and eid:
        return {"prod": "Production", "staging": "Staging"}.get(eid, eid)
    if et == "agent_prompt_versions":
        # The version row is gone (deleted, or a reset DB): name it from the
        # audit payload — "v10 · 11 Oct" — never a raw id prefix.
        after = row.get("after") if isinstance(row.get("after"), dict) else {}
        num = after.get("version_number")
        when = parse_ts(after.get("created_at") or row.get("created_at"))
        date = f"{when.day} {when:%b %Y}" if when else ""
        if num is not None:
            return f"v{num}" + (f" · {date}" if date else "")
        return f"saved {date}" if date else "(deleted)"
    for src in (row.get("after") or {}, row.get("before") or {}):
        if isinstance(src, dict):
            for k in ("display_name", "name", "title", "label", "slug"):
                if src.get(k) and isinstance(src.get(k), str):
                    return src[k]
    return None


def describe_row(row: Mapping[str, Any], *, people: Mapping[str, Dict[str, Any]],
                 entities: Mapping[Tuple[str, str], Dict[str, Any]],
                 versions: Mapping[str, int], me: Optional[str] = None,
                 now: Optional[_dt.datetime] = None) -> Dict[str, Any]:
    """One audit row → the pieces of its sentence + metadata for the template."""
    action_raw = row.get("action") or ""
    action = action_raw.lower()
    et = row.get("entity_type") or ""
    eid = str(row.get("entity_id") or "") or None
    noun, _, _, _, link_tpl, area = ENTITY_TYPES.get(et, (humanize_key(et).lower() or "item", None, None, (),
                                                          None, ""))
    actor = actor_display(row.get("actor_user_id"), people, me)

    ent = entities.get((et, eid or "")) or {}
    name = ent.get("name")
    if et == "user" and eid:
        name = (people.get(eid) or {}).get("label") or name
    if not name:
        name = _entity_fallback_name(et, eid, row)
    href = ent.get("href")
    if not href and link_tpl:
        if "{id}" in link_tpl:
            href = link_tpl.format(id=eid) if eid and _is_uuid(eid) else AREA_PAGES.get(area)
        else:
            href = link_tpl
    if not href and area:
        href = AREA_PAGES.get(area)

    verb = ACTION_VERBS.get(action) or humanize_key(action_raw).lower()
    show_noun = action not in _NOUNLESS
    suffix = ""
    if action == "config_promote":
        num = versions.get(str(row.get("audit_id")))
        if num is not None:
            suffix = f"v{num}"
        after = row.get("after") or {}
        if isinstance(after, dict) and after.get("environment") == "prod":
            suffix = (suffix + " to live").strip()
    if action == "callee_verify":
        name, show_noun, href = "the activity log replica", False, "/admin/audit"

    # A promote's before/after is the source row vs its copy — only the
    # environment differs, so a field count there would mislead.
    changed = [] if action == "config_promote" else changed_keys(row.get("before"), row.get("after"))
    return {
        "audit_id": row.get("audit_id"),
        "actor": actor,
        "verb": verb,
        "noun": noun if show_noun else "",
        "name": name or ("" if not eid else f"{noun} {eid[:8]}"),
        "href": href,
        "suffix": suffix,
        "action": action_raw,
        "entity_type": et,
        "entity_id": eid,
        "area": area,
        "when": relative_time(row.get("created_at"), now),
        "when_full": full_time(row.get("created_at")),
        "created_at": row.get("created_at"),
        "changed": changed,
        "is_override": action in ("eval_gate_override", "prompt_tool_mismatch_override"),
        "reason": ((row.get("after") or {}).get("reason") or (row.get("after") or {}).get("_reason"))
        if isinstance(row.get("after"), dict) else None,
    }


def sentence_text(d: Mapping[str, Any]) -> str:
    """Plain-text sentence (export / tests)."""
    parts = [d["actor"]["label"], d["verb"]]
    if d.get("noun"):
        parts.append(d["noun"])
    if d.get("name"):
        parts.append(d["name"])
    if d.get("suffix"):
        parts.append(d["suffix"])
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Readable before/after diff
# ---------------------------------------------------------------------------

_META_KEYS = {"created_at", "updated_at", "created_by", "updated_by", "_cross_env", "_reason"}


def changed_keys(before: Any, after: Any) -> List[str]:
    b = before if isinstance(before, dict) else {}
    a = after if isinstance(after, dict) else {}
    keys = [k for k in list(a.keys()) + [k for k in b.keys() if k not in a] if k not in _META_KEYS]
    if not b:
        return [k for k in keys if a.get(k) not in (None, "", [], {})]
    return [k for k in keys if b.get(k) != a.get(k)]


def _as_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return json.dumps(v, indent=2, ensure_ascii=False, sort_keys=True, default=str)


def _short(v: Any) -> Optional[str]:
    """Inline text for a short scalar, else None (render as a block)."""
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str) and len(v) <= 80 and "\n" not in v:
        return v or "(empty)"
    if isinstance(v, list) and not v:
        return "(none)"
    if isinstance(v, dict) and not v:
        return "(empty)"
    return None


def field_changes(before: Any, after: Any, max_lines: int = 400,
                  version_labels: Optional[Mapping[str, str]] = None) -> List[Dict[str, Any]]:
    """Per-field changes for the audit detail: short values inline (old → new),
    long text / JSON as a unified diff (`.diff-block` rows). On a create (no
    before) a long value is shown as plain text, not as an all-added diff.
    `version_labels` turns prompt version ids into "v3 (1a2b3c4d)"."""
    b = dict(before) if isinstance(before, dict) else {}
    a = dict(after) if isinstance(after, dict) else {}
    if version_labels:
        def _lab(d):
            if isinstance(d.get("prompt_version_ids"), dict):
                d["prompt_version_ids"] = {
                    k: (f"{version_labels[str(v)]} ({str(v)[:8]})" if str(v) in version_labels else v)
                    for k, v in d["prompt_version_ids"].items()}
        _lab(b)
        _lab(a)
    out: List[Dict[str, Any]] = []
    for k in changed_keys(b, a):
        bv, av = b.get(k), a.get(k)
        kind = "added" if k not in b or bv is None else ("removed" if k not in a or av is None else "changed")
        if not b:
            kind = "set"
        item: Dict[str, Any] = {"key": k, "label": humanize_key(k), "kind": kind,
                                "before": None, "after": None, "diff": None, "text": None}
        bs, as_ = _short(bv), _short(av)
        if not b and as_ is None:
            text = _as_text(av)
            lines = text.splitlines()
            if len(lines) > max_lines:
                text = "\n".join(lines[:max_lines]) + f"\n… {len(lines) - max_lines} more lines"
            item["text"] = text
        elif bs is not None and as_ is not None:
            item["before"], item["after"] = bs, as_
        else:
            lines = list(difflib.unified_diff(_as_text(bv).splitlines(), _as_text(av).splitlines(),
                                              lineterm="", n=2))[2:]
            rows = []
            for ln in lines[:max_lines]:
                cls = ("add" if ln.startswith("+") else "del" if ln.startswith("-")
                       else "hunk" if ln.startswith("@@") else "ctx")
                rows.append((cls, ln))
            if len(lines) > max_lines:
                rows.append(("hunk", f"… {len(lines) - max_lines} more lines"))
            item["diff"] = rows
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Team & access
# ---------------------------------------------------------------------------

_SERVICE_EMAIL_RE = re.compile(r"(@e2e\.test$|^e2e[-_.]|^test[-_.].*@|\+e2e@)", re.I)
# The documentation domains. Test fixtures use them, but so does a real
# admin of a fresh install who typed admin@example.com on the first-run page.
_PLACEHOLDER_EMAIL_RE = re.compile(r"@example\.(com|org)$", re.I)


def _is_superuser_row(row: Mapping[str, Any]) -> bool:
    perm = row.get("permission") or {}
    return bool(perm.get("is_superuser")) if isinstance(perm, dict) else False


def is_service_account(row: Mapping[str, Any]) -> bool:
    """Automation / test identities that clutter the team list.

    `role == 'system'` rows are seeded service users; `@e2e.test` emails are
    the e2e harness's accounts; the zero-uuid row is the system actor. An
    example.com / example.org address hides a user only when it is not an
    admin: an admin can sign in to the portal, so it is a person."""
    if str(row.get("id") or "").startswith(SYSTEM_ACTOR_PREFIX):
        return True
    if (row.get("role") or "").lower() == "system":
        return True
    email = row.get("email") or ""
    if email and _SERVICE_EMAIL_RE.search(email):
        return True
    if email and _PLACEHOLDER_EMAIL_RE.search(email) and not _is_superuser_row(row):
        return True
    return not email and not row.get("phone_number")


# How the Team & access screen words a user's access outside the portal.
# Neutral here; a plugin whose users also sign in to its own apps says so
# through its ``portal_display`` hook (``team_copy()``).
_TEAM_COPY: Dict[str, Optional[str]] = {
    "others": "Everyone else listed here can't open the portal.",
    # "{role}" is the user's `role` column; None = the role is not shown.
    "app_role": None,
}


def team_copy() -> Dict[str, Optional[str]]:
    """The Team & access wording, with a plugin's ``team_copy()`` on top."""
    from core.plugins import call_hook, load_hook

    out = dict(_TEAM_COPY)
    for extra in call_hook(load_hook("portal_display"), "team_copy"):
        out.update({k: v for k, v in (extra or {}).items() if k in _TEAM_COPY})
    return out


def app_role_text(role: Optional[str], copy: Mapping[str, Optional[str]]) -> Optional[str]:
    """``copy["app_role"]`` filled with ``role``; None when either is blank."""
    template = copy.get("app_role")
    if not role or not template:
        return None
    return template.replace("{role}", str(role))


def last_activity_by_actor(client: Any, users: Sequence[Mapping[str, Any]], limit: int = 2000) -> Dict[str, str]:
    """{user_id: latest audit created_at} from the recent activity log.

    Actors are recorded as `public.user.id` by the portal and as the auth id
    (JWT `sub`) by some MCP/script writers, so both are matched."""
    key_of: Dict[str, str] = {}
    for u in users:
        uid = str(u.get("id") or "")
        if _is_uuid(uid):
            key_of[uid] = uid
            if u.get("auth_id") and _is_uuid(u.get("auth_id")):
                key_of[str(u["auth_id"])] = uid
    if not key_of or client is None:
        return {}
    out: Dict[str, str] = {}
    for row in _rows(
        client.table("agent_audit_log").select("actor_user_id, created_at")
        .in_("actor_user_id", sorted(key_of)).order("created_at", desc=True).limit(limit)
    ):
        if not isinstance(row, dict):
            continue
        uid = key_of.get(str(row.get("actor_user_id") or ""))
        if uid and uid not in out:
            out[uid] = row.get("created_at")
    return out


def last_sign_in(main_client: Any, users: Sequence[Mapping[str, Any]], cap: int = 25) -> Dict[str, str]:
    """{user_id: auth last_sign_in_at} for a few users (the admins) via the
    main project's auth admin API. Best-effort; one call per user."""
    out: Dict[str, str] = {}
    for u in list(users)[:cap]:
        auth_id = u.get("auth_id")
        if not auth_id or not _is_uuid(auth_id):
            continue
        try:
            res = main_client.auth.admin.get_user_by_id(str(auth_id))
            ts = getattr(getattr(res, "user", None), "last_sign_in_at", None)
        except Exception as exc:  # noqa: BLE001
            logger.info("team: sign-in lookup failed (%s)", exc)
            continue
        if ts and parse_ts(ts):
            out[str(u.get("id"))] = ts.isoformat() if isinstance(ts, _dt.datetime) else str(ts)
    return out


def merge_latest(*maps: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for m in maps:
        for k, v in m.items():
            if parse_ts(v) and (k not in out or parse_ts(v) > parse_ts(out[k])):
                out[k] = v
    return out


def build_team(rows: Sequence[Mapping[str, Any]], last_seen: Mapping[str, Any], me: Optional[str],
               now: Optional[_dt.datetime] = None) -> List[Dict[str, Any]]:
    people = []
    copy = team_copy()
    for r in rows:
        perm = r.get("permission") or {}
        is_admin = bool(perm.get("is_superuser")) if isinstance(perm, dict) else False
        seen = last_seen.get(str(r.get("id")))
        people.append({
            "id": r.get("id"),
            "label": person_label(r),
            "initial": initial(person_label(r)),
            "email": r.get("email"),
            "phone": (f"+{r.get('country_code') or ''}{r['phone_number']}" if r.get("phone_number") else None),
            "is_admin": is_admin,
            # The signed-in admin is never hidden from their own team list.
            "is_service": is_service_account(r) and not (me and str(r.get("id")) == str(me)),
            "is_me": bool(me and str(r.get("id")) == str(me)),
            "app_role": r.get("role"),
            "app_role_text": app_role_text(r.get("role"), copy),
            "last_active": relative_time(seen, now) if seen else None,
            "last_active_full": full_time(seen) if seen else None,
            "last_active_sort": str(seen or ""),
            "created": short_date(r.get("created_at"), now),
            "business_id": r.get("business_id"),
        })
    # Admins first; within each group the most recently active first.
    admins = [p for p in people if p["is_admin"]]
    others = [p for p in people if not p["is_admin"]]
    for group in (admins, others):
        group.sort(key=lambda p: p["last_active_sort"], reverse=True)
    return admins + others


# ---------------------------------------------------------------------------
# Releases — promote overview + "what will change"
# ---------------------------------------------------------------------------

# Profile columns the promote copies, with plain labels. Anything not listed
# is still copied; it just isn't shown as a change.
PROMOTE_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("display_name", "Name"),
    ("description", "Description"),
    ("prompt_version_ids", "Instructions"),
    ("tool_ids", "Tools"),
    ("data_source_ids", "Knowledge"),
    ("mcp_server_ids", "Integrations"),
    ("model_main_id", "Main model"),
    ("model_light_id", "Light model"),
    ("voice_config", "Voice settings"),
    ("memory_config", "Memory settings"),
    ("escalation_policy", "Escalation rules"),
    ("input_validators", "Input checks"),
    ("downstream_profile_ids", "Sub-agents"),
    ("parent_profile_id", "Parent agent"),
    ("delegatable", "Can be delegated to"),
    ("is_active", "Active"),
    ("endpoint_path", "Endpoint"),
)

_LOOKUP_TABLES = {
    "tool_ids": ("agent_tools", "tool_id", ("display_name", "python_name")),
    "data_source_ids": ("agent_data_sources", "source_id", ("name",)),
    "mcp_server_ids": ("agent_mcp_servers", "mcp_server_id", ("name",)),
}


def env_labels(single_env: Optional[str]) -> Dict[str, str]:
    """Plain names for the promote source/target. Single-env (partner) builds
    have no staging/production pair to show: they read Draft → Live."""
    if single_env:
        return {"source": "Draft", "target": "Live", "single": True,
                "source_long": "your draft", "target_long": "live"}
    return {"source": "Staging", "target": "Production", "single": False,
            "source_long": "staging", "target_long": "production"}


def _names(client: Any, table: str, pk: str, cols: Tuple[str, ...], ids: Iterable[str]) -> Dict[str, str]:
    ids = sorted({str(i) for i in ids if _is_uuid(i)})
    if not ids or client is None:
        return {}
    out = {}
    for r in _rows(client.table(table).select(", ".join((pk,) + cols)).in_(pk, ids)):
        if isinstance(r, dict):
            out[str(r.get(pk))] = next((r.get(c) for c in cols if r.get(c)), None) or str(r.get(pk))[:8]
    return out


class _Lookups:
    """Batched name lookups shared by every diff on the page."""

    def __init__(self, src_client: Any, tgt_client: Any, pairs: Sequence[Tuple[Mapping, Optional[Mapping]]]):
        ids: Dict[str, set] = {k: set() for k in _LOOKUP_TABLES}
        model_ids_src, model_ids_tgt, prompt_vids, profile_ids_src, profile_ids_tgt = set(), set(), set(), set(), set()
        for src, tgt in pairs:
            for row, is_src in ((src, True), (tgt, False)):
                if not row:
                    continue
                for k in _LOOKUP_TABLES:
                    ids[k].update(row.get(k) or [])
                (model_ids_src if is_src else model_ids_tgt).update(
                    i for i in (row.get("model_main_id"), row.get("model_light_id")) if i)
                prompt_vids.update((row.get("prompt_version_ids") or {}).values()
                                   if isinstance(row.get("prompt_version_ids"), dict) else [])
                refs = [row.get("parent_profile_id")] + list(row.get("downstream_profile_ids") or [])
                (profile_ids_src if is_src else profile_ids_tgt).update(i for i in refs if i)
        self.names = {k: _names(src_client, *_LOOKUP_TABLES[k], ids[k]) for k in _LOOKUP_TABLES}
        self.models = {**_names(tgt_client, "agent_models", "model_id", ("name",), model_ids_tgt),
                       **_names(src_client, "agent_models", "model_id", ("name",), model_ids_src)}
        self.prompt_versions: Dict[str, str] = {}
        if prompt_vids:
            for r in _rows(src_client.table("agent_prompt_versions").select("prompt_version_id, version_number")
                           .in_("prompt_version_id", sorted(i for i in prompt_vids if _is_uuid(i)))):
                if isinstance(r, dict):
                    self.prompt_versions[str(r.get("prompt_version_id"))] = f"v{r.get('version_number')}"
        self.profiles = {**_names(tgt_client, "agent_profiles", "profile_id", ("display_name", "slug"),
                                  profile_ids_tgt),
                         **_names(src_client, "agent_profiles", "profile_id", ("display_name", "slug"),
                                  profile_ids_src)}


def _list_change(field: str, label: str, before: Sequence, after: Sequence, names: Mapping[str, str]):
    b, a = [str(x) for x in (before or [])], [str(x) for x in (after or [])]
    added = [x for x in a if x not in b]
    removed = [x for x in b if x not in a]
    if not added and not removed:
        if b != a:
            return {"field": field, "label": label, "summary": "Order changed", "added": [], "removed": []}
        return None
    bits = []
    if added:
        bits.append(f"{len(added)} added")
    if removed:
        bits.append(f"{len(removed)} removed")
    return {"field": field, "label": label, "summary": ", ".join(bits),
            "added": [names.get(x, x[:8]) for x in added], "removed": [names.get(x, x[:8]) for x in removed]}


def promote_changes(src: Mapping[str, Any], tgt: Optional[Mapping[str, Any]], lk: _Lookups) -> List[Dict[str, Any]]:
    """What a promote of `src` over `tgt` (the live row, or None) would change."""
    out: List[Dict[str, Any]] = []
    if not tgt:
        return [{"field": "_new", "label": "Everything", "summary": "Not live yet — this publishes the agent "
                 "for the first time.", "added": [], "removed": []}]
    for field, label in PROMOTE_FIELDS:
        sv, tv = src.get(field), tgt.get(field)
        if field in _LOOKUP_TABLES:
            ch = _list_change(field, label, tv or [], sv or [], lk.names.get(field, {}))
            if ch:
                out.append(ch)
            continue
        if field in ("model_main_id", "model_light_id"):
            sn = lk.models.get(str(sv)) if sv else None
            tn = lk.models.get(str(tv)) if tv else None
            if (sn or None) != (tn or None):
                out.append({"field": field, "label": label, "summary": f"{tn or 'none'} → {sn or 'none'}",
                            "added": [], "removed": []})
            continue
        if field == "downstream_profile_ids":
            sn = sorted(lk.profiles.get(str(x), str(x)[:8]) for x in (sv or []))
            tn = sorted(lk.profiles.get(str(x), str(x)[:8]) for x in (tv or []))
            if sn != tn:
                out.append({"field": field, "label": label, "summary": "Changed",
                            "added": [x for x in sn if x not in tn], "removed": [x for x in tn if x not in sn]})
            continue
        if field == "parent_profile_id":
            sn = lk.profiles.get(str(sv)) if sv else None
            tn = lk.profiles.get(str(tv)) if tv else None
            if sn != tn:
                out.append({"field": field, "label": label, "summary": f"{tn or 'none'} → {sn or 'none'}",
                            "added": [], "removed": []})
            continue
        if field == "prompt_version_ids":
            sd = sv if isinstance(sv, dict) else {}
            td = tv if isinstance(tv, dict) else {}
            roles = [r for r in sorted(set(sd) | set(td)) if sd.get(r) != td.get(r)]
            if roles:
                bits = []
                for r in roles:
                    old = lk.prompt_versions.get(str(td.get(r)), "none") if td.get(r) else "none"
                    new = lk.prompt_versions.get(str(sd.get(r)), "none") if sd.get(r) else "none"
                    bits.append(f"{humanize_key(r)} {old} → {new}")
                out.append({"field": field, "label": label, "summary": "; ".join(bits), "added": [], "removed": []})
            continue
        if isinstance(sv, dict) or isinstance(tv, dict):
            sd = sv if isinstance(sv, dict) else {}
            td = tv if isinstance(tv, dict) else {}
            keys = [k for k in sorted(set(sd) | set(td)) if sd.get(k) != td.get(k)]
            if keys:
                n = len(keys)
                out.append({"field": field, "label": label,
                            "summary": f"{n} setting{'s' if n != 1 else ''} changed",
                            "added": [], "removed": [], "keys": keys})
            continue
        if (sv if sv not in ("", None, []) else None) != (tv if tv not in ("", None, []) else None):
            fmt = lambda v: _short(v) or "changed"  # noqa: E731
            out.append({"field": field, "label": label, "summary": f"{fmt(tv)} → {fmt(sv)}",
                        "added": [], "removed": []})
    return out


def quality_summary(es: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Eval state → plain chip text + whether the gate blocks the promote."""
    if not es:
        return {"text": "No test data", "variant": "neutral", "blocks": False}
    status = es.get("status")

    def pct(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return "?"
        return f"{round(f * 100) if f <= 1 else round(f)}%"

    if status == "pass":
        return {"text": f"{pct(es.get('score'))} · passed", "variant": "ok", "blocks": False,
                "detail": f"Last test run scored {pct(es.get('score'))} (needs {pct(es.get('threshold'))}).",
                "suite": es.get("suite_name"), "run_id": es.get("run_id")}
    if status == "below_threshold":
        return {"text": f"{pct(es.get('score'))} · below {pct(es.get('threshold'))}", "variant": "warn",
                "blocks": True,
                "detail": (f"The last test run scored {pct(es.get('score'))}, below the "
                           f"{pct(es.get('threshold'))} bar."),
                "suite": es.get("suite_name"), "run_id": es.get("run_id")}
    if status == "errored":
        return {"text": "Last test errored", "variant": "err", "blocks": False,
                "detail": "The last test run did not finish.", "run_id": es.get("run_id")}
    return {"text": "Not tested yet", "variant": "neutral", "blocks": False}


def release_rows(*, src_client: Any, tgt_client: Any, src_rows: Sequence[Mapping[str, Any]],
                 tgt_rows: Sequence[Mapping[str, Any]], eval_status: Mapping[str, Any],
                 main_client: Any = None, now: Optional[_dt.datetime] = None) -> List[Dict[str, Any]]:
    """One row per agent for the promote overview grid."""
    tgt_by_key = {(r.get("agent_kind"), r.get("slug")): r for r in tgt_rows}
    src_keys = set()
    pairs: List[Tuple[Mapping, Optional[Mapping]]] = []
    for s in src_rows:
        if s.get("status") == "template":
            continue
        key = (s.get("agent_kind"), s.get("slug"))
        src_keys.add(key)
        pairs.append((s, tgt_by_key.get(key)))
    live_only = [t for t in tgt_rows if (t.get("agent_kind"), t.get("slug")) not in src_keys
                 and t.get("status") != "template"]

    # Live versions: the pinned served version, else the newest snapshot.
    tgt_ids = [str(t["profile_id"]) for _, t in pairs if t] + [str(t["profile_id"]) for t in live_only]
    versions: Dict[str, List[Dict[str, Any]]] = {}
    if tgt_ids:
        for v in _rows(tgt_client.table("agent_profile_versions")
                       .select("version_id, profile_id, version_number, promoted_at, promoted_by")
                       .in_("profile_id", tgt_ids).order("version_number", desc=True).limit(2000)):
            if isinstance(v, dict):
                versions.setdefault(str(v.get("profile_id")), []).append(v)
    people = resolve_people(main_client, [v.get("promoted_by") for vs in versions.values() for v in vs])

    def live_of(t: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
        if not t:
            return None
        vs = versions.get(str(t.get("profile_id"))) or []
        pinned = t.get("served_version_id")
        v = next((x for x in vs if pinned and str(x.get("version_id")) == str(pinned)), None) or (vs[0] if vs else None)
        if not v:
            return {"number": None, "when": short_date(t.get("updated_at"), now), "by": None, "pinned": False}
        who = people.get(str(v.get("promoted_by") or "")) or {}
        by = None if (who.get("is_system") or (who.get("label") or "").startswith("Unknown user")) \
            else who.get("label")
        return {"number": v.get("version_number"), "when": short_date(v.get("promoted_at"), now),
                "when_full": full_time(v.get("promoted_at")), "by": by,
                "pinned": bool(pinned) and str(v.get("version_id")) == str(pinned) and v is not (vs[0] if vs else None)}

    lk = _Lookups(src_client, tgt_client, pairs)
    out: List[Dict[str, Any]] = []
    for s, t in pairs:
        changes = promote_changes(s, t, lk)
        q = quality_summary(eval_status.get(s.get("profile_id")))
        out.append({
            "profile_id": s.get("profile_id"),
            "name": s.get("display_name") or s.get("slug") or s.get("agent_kind"),
            "technical": f"{s.get('agent_kind')} · {s.get('slug')}",
            "live": live_of(t),
            "is_new": t is None,
            "changes": changes,
            "change_count": 0 if t is None else len(changes),
            "has_changes": t is None or bool(changes),
            "draft_edited": short_date(s.get("updated_at"), now),
            "quality": q,
            "can_promote": True,
            "search": " ".join(str(x or "") for x in (s.get("display_name"), s.get("slug"),
                                                      s.get("agent_kind"))).lower(),
        })
    for t in live_only:
        out.append({
            "profile_id": None,
            "name": t.get("display_name") or t.get("slug") or t.get("agent_kind"),
            "technical": f"{t.get('agent_kind')} · {t.get('slug')}",
            "live": live_of(t), "is_new": False, "changes": [], "change_count": 0,
            "has_changes": False, "draft_edited": None,
            "quality": quality_summary(None), "can_promote": False, "live_only": True,
            "search": " ".join(str(x or "") for x in (t.get("display_name"), t.get("slug"),
                                                      t.get("agent_kind"))).lower(),
        })
    out.sort(key=lambda r: (not r["has_changes"], (r["name"] or "").lower()))
    return out


# ---------------------------------------------------------------------------
# Releases › Promotion history — one row per published agent version
# ---------------------------------------------------------------------------

# Snapshot keys (runtime/compiler.py) → the plain area they belong to. The
# resolved copies (`prompt_contents`, `model_main`, ...) fold into the same
# label as the id column they were resolved from, so one change reads once.
_SNAPSHOT_AREAS: Dict[str, str] = {
    **dict(PROMOTE_FIELDS),
    "prompt_contents": "Instructions",
    "model_main": "Main model",
    "voice_model": "Voice settings",
    "tool_python_names": "Tools",
    "data_sources": "Knowledge",
    "mcp_servers": "Integrations",
    "mcp_attachments": "Integrations",
    "skill_attachments": "Skills",
    "downstream_delegates": "Sub-agents",
}
_NOTES_RE = re.compile(r"^Promoted from (\S+) by (.+)$")
HISTORY_LIMIT = 100


def snapshot_changes(before: Optional[Mapping[str, Any]], after: Mapping[str, Any]) -> List[str]:
    """The areas that differ between two published snapshots, in display
    order. Keys starting with `_` (schema version) are not changes."""
    before = before or {}
    seen: List[str] = []
    for key in sorted(set(before) | set(after)):
        if key.startswith("_") or key not in _SNAPSHOT_AREAS:
            continue
        if before.get(key) != after.get(key):
            label = _SNAPSHOT_AREAS[key]
            if label not in seen:
                seen.append(label)
    order = list(dict.fromkeys(_SNAPSHOT_AREAS.values()))
    return sorted(seen, key=order.index)


def promotion_history(*, tgt_client: Any, tgt_env: str = "prod", main_client: Any = None,
                      labels: Optional[Mapping[str, Any]] = None,
                      now: Optional[_dt.datetime] = None,
                      limit: int = HISTORY_LIMIT) -> List[Dict[str, Any]]:
    """Every promote, newest first: which agent, which version it published
    and which one it replaced, from where, by whom, when, and what changed.

    Read from `agent_profile_versions` in the target environment: each
    promote writes exactly one row there (runtime/compiler.py)."""
    labels = labels or env_labels(None)
    versions = [v for v in _rows(
        tgt_client.table("agent_profile_versions")
        .select("version_id, profile_id, version_number, promoted_at, promoted_by, notes, snapshot")
        .eq("environment", tgt_env).order("promoted_at", desc=True).limit(limit)) if isinstance(v, dict)]
    if not versions:
        return []
    versions.sort(key=lambda v: str(v.get("promoted_at") or ""), reverse=True)
    pids = sorted({str(v.get("profile_id")) for v in versions if v.get("profile_id")})
    profiles = {str(r.get("profile_id")): r for r in _rows(
        tgt_client.table("agent_profiles")
        .select("profile_id, display_name, slug, agent_kind, served_version_id")
        .in_("profile_id", pids)) if isinstance(r, dict)}
    people = resolve_people(main_client, [v.get("promoted_by") for v in versions])

    by_profile: Dict[str, List[Dict[str, Any]]] = {}
    for v in versions:
        by_profile.setdefault(str(v.get("profile_id")), []).append(v)
    for vs in by_profile.values():
        vs.sort(key=lambda x: x.get("version_number") or 0, reverse=True)

    out: List[Dict[str, Any]] = []
    for v in versions:
        pid = str(v.get("profile_id"))
        snap = v.get("snapshot") if isinstance(v.get("snapshot"), dict) else {}
        prof = profiles.get(pid) or {}
        siblings = by_profile.get(pid) or []
        number = v.get("version_number")
        older = [x for x in siblings if (x.get("version_number") or 0) < (number or 0)]
        previous = older[0] if older else None
        if previous is not None:
            prev_snap = previous.get("snapshot") if isinstance(previous.get("snapshot"), dict) else {}
            changes = snapshot_changes(prev_snap, snap)
            replaced = previous.get("version_number")
        elif number == 1:
            changes, replaced = ["First publish"], None
        else:  # the older version is past the page: its number is still known
            changes, replaced = [], (number - 1) if isinstance(number, int) else None
        m = _NOTES_RE.match((v.get("notes") or "").strip())
        source = m.group(1) if m else None
        who = people.get(str(v.get("promoted_by") or "")) or {}
        by = None if (who.get("is_system") or (who.get("label") or "").startswith("Unknown user")) \
            else who.get("label")
        served = prof.get("served_version_id")
        live_id = str(served) if served else (str(siblings[0].get("version_id")) if siblings else None)
        out.append({
            "version_id": v.get("version_id"),
            "profile_id": pid,
            "name": prof.get("display_name") or snap.get("display_name") or prof.get("slug")
            or snap.get("slug") or pid[:8],
            "technical": f"{prof.get('agent_kind') or snap.get('agent_kind')} · "
                         f"{prof.get('slug') or snap.get('slug')}",
            "number": number,
            "replaced": replaced,
            # Promotes copy from the source environment: name it the way the
            # Releases page does (Staging, or Draft on a single-env build).
            "source": labels.get("source") if source in (None, "staging", "dev")
            else humanize_key(source),
            "by": by or (m.group(2) if m else None),
            "when": short_date(v.get("promoted_at"), now),
            "when_full": full_time(v.get("promoted_at")),
            "changes": changes,
            "is_live": live_id is not None and str(v.get("version_id")) == live_id,
            "notes": v.get("notes"),
            "search": " ".join(str(x or "") for x in (prof.get("display_name"), prof.get("slug"),
                                                      snap.get("display_name"))).lower(),
        })
    return out


# ---------------------------------------------------------------------------
# Diagnostics — drift findings grouped by agent
# ---------------------------------------------------------------------------

_DRIFT_AGENT_RE = re.compile(r"^([A-Za-z0-9_]+(?:\([a-z]+\))?)")
_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2, "ok": 3}


def group_drift(findings: Sequence[Any]) -> List[Dict[str, Any]]:
    """[{agent, counts, items}] — worst agents first; items worst-first."""
    groups: Dict[str, Dict[str, Any]] = {}
    for f in findings:
        m = _DRIFT_AGENT_RE.match(getattr(f, "target", "") or "")
        agent = m.group(1) if m else (getattr(f, "target", "") or "other")
        g = groups.setdefault(agent, {"agent": agent, "counts": {"critical": 0, "warning": 0, "info": 0, "ok": 0},
                                      "items": []})
        sev = getattr(f, "severity", "info")
        g["counts"][sev] = g["counts"].get(sev, 0) + 1
        g["items"].append(f)
    for g in groups.values():
        g["items"].sort(key=lambda f: _SEVERITY_ORDER.get(getattr(f, "severity", "info"), 9))
        c = g["counts"]
        g["worst"] = ("critical" if c.get("critical") else "warning" if c.get("warning")
                      else "info" if c.get("info") else "ok")
    return sorted(groups.values(), key=lambda g: (_SEVERITY_ORDER.get(g["worst"], 9), g["agent"]))
