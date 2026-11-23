"""View models for the redesigned Agents area (list table + tabbed detail).

Two entry points:

* ``load_agents_list(env)`` — the /admin/agents table: one row per agent with
  plain-language Type / Channel / Model / "Can use" / Status / Last changed,
  sub-agents nested under their parent, templates included but flagged so the
  page hides them by default. Bulk reads only (one query per table, run in
  parallel) — the old screen opened the first agent instead of listing.

* ``build_detail_view(ctx, env, user_id)`` — pure function over the context
  ``admin.screens.agents.load_agents_context`` already returns. It derives
  what the tabs show (attached-only toolsets, knowledge and skills, the
  pickers' search payloads, sub-agent candidates, Overview tiles) without a
  single extra DB read, and tolerates missing keys so a test that patches the
  loader with a minimal context still renders.

No POST contract changes here: every form the tabs render still posts to the
same handlers (/admin/profiles/{id}, …/mcp-attachments, …/skill-attachments,
/admin/prompts/{id}/versions, the version routes). See
docs/admin-portal/02-configure.md.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from admin._query_batch import run_parallel
from admin.screens import agents_present as P

logger = logging.getLogger(__name__)

DETAIL_TABS = ("overview", "instructions", "tools", "subagents", "voice",
               "versions", "settings")
_TAB_LABELS = {
    "overview": "Overview", "instructions": "Instructions",
    "tools": "Tools & knowledge", "subagents": "Sub-agents", "voice": "Voice",
    "versions": "Versions", "settings": "Settings",
}
# Old in-page anchors (bookmarks, docs, other screens) → the tab that now
# holds that section. agents.js reads this map from the page and switches.
LEGACY_ANCHORS = {
    "prompt": "instructions", "version-bar": "versions", "model": "settings",
    "status": "settings", "tools": "tools", "sources": "tools", "mcp": "tools",
    "mcp-attachments": "tools", "skill-attachments": "tools",
    "downstream": "subagents", "endpoint": "settings", "voice-live": "voice",
}



def _handoff_tool_name(slug) -> str:
    """`ask_support_desk` for slug `support-desk` — the runtime's own rule
    (runtime/delegation._tool_name), so the portal never shows a name the
    model is not actually given."""
    if not slug:
        return ""
    from runtime.delegation import _tool_name

    return _tool_name(slug)

def normalize_tab(tab: Optional[str]) -> str:
    t = (tab or "").strip().lower()
    if t in ("", "agents", "agent"):
        return "overview"
    return t if t in DETAIL_TABS else "overview"


# --------------------------------------------------------------------------
# Bound-tool counting, in bulk (mirrors bound_tool_names_from_attachments)
# --------------------------------------------------------------------------


def _attachments_by_profile(att_rows, servers, members, tools) -> Dict[str, List[Dict[str, Any]]]:
    servers_by_id = {s["mcp_server_id"]: s for s in servers if s.get("is_active", True)}
    names_by_tool = {t["tool_id"]: t.get("python_name") for t in tools}
    members_by_server: Dict[str, List[Dict[str, Any]]] = {}
    for m in sorted(members, key=lambda r: r.get("position") or 0):
        name = names_by_tool.get(m.get("tool_id"))
        if name:
            members_by_server.setdefault(m["mcp_server_id"], []).append(
                {"tool": {"python_name": name, "tool_id": m.get("tool_id")}})
    out: Dict[str, List[Dict[str, Any]]] = {}
    for att in sorted(att_rows, key=lambda r: r.get("position") or 0):
        server = servers_by_id.get(att.get("mcp_server_id"))
        if server is None:
            continue
        out.setdefault(att["profile_id"], []).append({
            "mcp_server": server,
            "tool_subset": att.get("tool_subset"),
            "member_tools": members_by_server.get(server["mcp_server_id"], []),
        })
    return out


def _bound_count(attachments: List[Dict[str, Any]],
                 listed: Optional[Dict[str, Optional[List[Dict[str, Any]]]]] = None) -> Dict[str, Any]:
    """``listed`` (admin.external_mcp_tools.listed_tools) holds the tools an
    external server lists; with it, a server attached with no pinned subset
    counts the tools it really offers instead of none."""
    from runtime.tools._mcp_resolver import bound_tool_names_from_attachments

    names = set(bound_tool_names_from_attachments(attachments))
    # An external server's tools are only listed for certain at connect time,
    # but when the agent (or the server row) pins a subset, those names ARE
    # what it gets. Counting them ended "0 actions + external" for an agent
    # with three chosen actions. `external` stays for a server whose list is
    # open-ended, where the count really is a floor.
    open_ended = False
    for a in attachments:
        server = a.get("mcp_server") or {}
        if server.get("endpoint_only") or server.get("source") == "bundle":
            continue
        pinned = a.get("tool_subset") or server.get("tool_subset") or []
        known = (listed or {}).get(str(server.get("mcp_server_id")))
        if pinned:
            names.update(pinned)
        elif known is not None:
            names.update(t["name"] for t in known)
        else:
            open_ended = True
    return {"count": len(names), "external": open_ended, "toolsets": len(attachments)}


# --------------------------------------------------------------------------
# List page
# --------------------------------------------------------------------------


def load_agents_list(env: str, user_id: Optional[str] = None) -> Dict[str, Any]:
    from admin.router import client_for
    from admin.screens.agents import resolve_cross_env_refs
    from admin.seed_agent_profiles import visible_agent_kinds
    from core.deployment import scoped_build

    client = client_for(env)

    def _q(table, cols="*", **eq):
        def run():
            try:
                q = client.table(table).select(cols)
                for k, v in eq.items():
                    q = q.eq(k, v)
                return q.execute().data or []
            except Exception as exc:  # noqa: BLE001 — a count must not 500 the list
                logger.warning("agents list: %s lookup failed (%s)", table, exc)
                return []
        return run

    def _profiles():
        return (client.table("agent_profiles").select("*")
                .eq("environment", env).order("agent_kind").execute().data or [])

    batch = run_parallel({
        "profiles": _profiles,
        "models": _q("agent_models", "model_id, name"),
        "atts": _q("agent_profile_mcp_servers", "profile_id, mcp_server_id, tool_subset, position"),
        "servers": _q("agent_mcp_servers", "mcp_server_id, name, source, endpoint_only, is_active, "
                                           "endpoint, auth_env_var, tool_subset"),
        "members": _q("agent_mcp_tools", "mcp_server_id, tool_id, position"),
        "tools": _q("agent_tools", "tool_id, python_name", is_deprecated=False),
        "skills": _q("agent_profile_skills", "profile_id, skill_id"),
        # User-defined categories (admin/services/agent_types.py).
        "types": _q("agent_types", "agent_type_id, slug, display_name, description, color, sort_order"),
    })
    profiles = batch["profiles"]
    all_env_profiles = list(profiles)
    if scoped_build():
        visible = set(visible_agent_kinds())
        profiles = [p for p in profiles if p.get("agent_kind") in visible]
    resolve_cross_env_refs(profiles, client)

    model_names = {m["model_id"]: m.get("name") for m in batch["models"]}
    env_ids = {p["profile_id"] for p in profiles}
    atts = _attachments_by_profile(
        [a for a in batch["atts"] if a.get("profile_id") in env_ids],
        batch["servers"], batch["members"], batch["tools"])
    # What each attached external server lists (admin/external_mcp_tools.py).
    from admin.external_mcp_tools import listed_tools

    attached_ids = {a.get("mcp_server_id") for a in batch["atts"] if a.get("profile_id") in env_ids}
    listed = listed_tools([sv for sv in batch["servers"]
                           if sv.get("mcp_server_id") in attached_ids and sv.get("is_active", True)])
    skills_by_profile: Dict[str, int] = {}
    for s in batch["skills"]:
        skills_by_profile[s["profile_id"]] = skills_by_profile.get(s["profile_id"], 0) + 1
    names_by_id = {p["profile_id"]: (p.get("display_name") or p.get("agent_kind")) for p in all_env_profiles}
    # Voice "brain" relation: the owner names the specialist by slug.
    voice_owner_by_slug: Dict[str, str] = {}
    for p in all_env_profiles:
        slug = P.voice_block(p).get("agent_profile_slug")
        if slug:
            voice_owner_by_slug[slug] = p.get("display_name") or p.get("agent_kind")

    categories = sorted(
        [t for t in batch["types"] if isinstance(t, dict) and t.get("agent_type_id")],
        key=lambda t: (t.get("sort_order") or 0, (t.get("display_name") or "").lower()))
    category_by_id = {t["agent_type_id"]: t for t in categories}

    def _row(p: Dict[str, Any]) -> Dict[str, Any]:
        pid = p["profile_id"]
        st = P.status_info(p.get("status"))
        category = category_by_id.get(p.get("agent_type_id"))
        parent_id = p.get("parent_profile_id")
        is_template = st["key"] == "template"
        is_sub = bool(parent_id and parent_id in env_ids)
        kind_label = ("Template" if is_template
                      else ("Sub-agent" if is_sub else "Main agent"))
        answers_for = voice_owner_by_slug.get(p.get("slug") or "")
        if answers_for == (p.get("display_name") or p.get("agent_kind")):
            answers_for = None
        channels = P.channels_for(p, answers_voice_for=answers_for)
        bound = _bound_count(atts.get(pid, []), listed)
        knowledge = len(p.get("data_source_ids") or [])
        subs = len(p.get("downstream_profile_ids") or [])
        return {
            "profile_id": pid,
            "name": p.get("display_name") or P.humanize_slug(p.get("agent_kind") or ""),
            "description": (p.get("description") or "").strip(),
            "kind": p.get("agent_kind"),
            "slug": p.get("slug"),
            "status": st,
            "type_key": "template" if is_template else ("sub" if is_sub else "main"),
            "type_label": kind_label,
            # The user-defined category, or None (uncategorised). Any kind of
            # agent can carry one; it never affects how the agent runs.
            "category": category,
            "parent_name": names_by_id.get(parent_id) if is_sub else None,
            "channels": channels,
            "channel_keys": " ".join(sorted({c["key"] for c in channels})),
            "model_name": model_names.get(p.get("model_main_id")) or "—",
            "tool_count": bound["count"],
            "toolsets": bound["toolsets"],
            "external_tools": bound["external"],
            "knowledge": knowledge,
            "subagents": subs,
            "skills": skills_by_profile.get(pid, 0),
            "can_use": P.can_use_summary(bound["count"], knowledge, subs, bound["external"]),
            "updated_rel": P.relative_time(p.get("updated_at")),
            "updated_at": p.get("updated_at") or "",
            "updated_by_you": bool(user_id and p.get("updated_by") == user_id),
            "children": [],
            "search": " ".join(filter(None, [
                p.get("display_name"), p.get("description"), p.get("agent_kind"),
                p.get("slug"), (category or {}).get("display_name")])).lower(),
        }

    rows = {p["profile_id"]: _row(p) for p in profiles}
    top: List[Dict[str, Any]] = []
    for p in profiles:
        r = rows[p["profile_id"]]
        parent_id = p.get("parent_profile_id")
        if parent_id and parent_id in rows and parent_id != p["profile_id"]:
            rows[parent_id]["children"].append(r)
        else:
            top.append(r)
    order = {"active": 0, "specialist": 0, "draft": 1, "archived": 2, "template": 3}
    top.sort(key=lambda r: (order.get(r["status"]["key"], 9), r["name"].lower()))
    for r in rows.values():
        r["children"].sort(key=lambda c: c["name"].lower())
        # A parent row's search text includes its children, so filtering on a
        # sub-agent's name keeps the family visible.
        r["family_search"] = " ".join([r["search"]] + [c["search"] for c in r["children"]])

    counts = {
        "all": sum(1 for r in rows.values() if r["type_key"] != "template"),
        "templates": sum(1 for r in rows.values() if r["type_key"] == "template"),
        "draft": sum(1 for r in rows.values() if r["status"]["bucket"] == "draft"),
    }
    compare_options = [
        {"profile_id": r["profile_id"], "name": r["name"], "status": r["status"]["label"]}
        for r in sorted(rows.values(), key=lambda r: r["name"].lower())
        if r["type_key"] != "template"
    ]
    return {"agent_rows": top, "agent_counts": counts, "compare_options": compare_options,
            "agent_categories": categories}


# --------------------------------------------------------------------------
# Detail page
# --------------------------------------------------------------------------


def _tree(ctx: Dict[str, Any], active_id: Optional[str]) -> List[Dict[str, Any]]:
    """The header's "switch agent" list: parents then indented children.
    Templates only appear when one is open (they are not live agents)."""
    top = ctx.get("top_level_profiles") or ctx.get("profiles") or []
    by_parent = ctx.get("sub_profiles_by_parent") or {}
    out: List[Dict[str, Any]] = []

    def add(p, depth):
        if (p.get("status") == "template") and p.get("profile_id") != active_id:
            return
        out.append({
            "profile_id": p.get("profile_id"),
            "name": p.get("display_name") or p.get("agent_kind"),
            "status": P.status_info(p.get("status")),
            "depth": depth,
            "active": p.get("profile_id") == active_id,
        })

    for p in top:
        add(p, 0)
        for c in by_parent.get(p.get("profile_id"), []) or []:
            add(c, 1)
    return out


def build_detail_view(ctx: Dict[str, Any], env: str,
                      user_id: Optional[str] = None) -> Dict[str, Any]:
    ap = ctx.get("active_profile") or {}
    pid = ap.get("profile_id")
    profiles = ctx.get("profiles") or []
    by_id = {p.get("profile_id"): p for p in profiles}
    owner = ctx.get("voice_config_owner")
    channels = P.channels_for(ap, answers_voice_for=(owner or {}).get("display_name"))
    status = P.status_info(ap.get("status"))

    # ---- model names ----------------------------------------------------
    models = ctx.get("all_models") or []
    model_by_id = {m.get("model_id"): m for m in models}
    main_model = model_by_id.get(ap.get("model_main_id"))
    light_model = model_by_id.get(ap.get("model_light_id"))

    # ---- action metadata (display name / description / scope) ------------
    tool_rows = ctx.get("tools") or []
    tool_meta = {t.get("python_name"): t for t in tool_rows if t.get("python_name")}

    def _action(name: str) -> Dict[str, Any]:
        t = tool_meta.get(name) or {}
        return {
            "name": name,
            "label": t.get("display_name") or P.humanize_slug(name),
            "description": (t.get("description") or "")[:240],
            "group": P.action_group(name),
        }

    # ---- toolsets (attached only) -----------------------------------------
    attachable = ctx.get("attachable_mcps") or []
    attachable_by_id = {m.get("mcp_server_id"): m for m in attachable}
    bound = ctx.get("bound_tools") or {}
    bound_by_server = {g.get("mcp_server_id"): [t.get("python_name") for t in (g.get("tools") or [])]
                       for g in (bound.get("groups") or [])}
    toolsets: List[Dict[str, Any]] = []
    attached_ids = set()
    # The tools each attached external server lists itself; the resolver
    # leaves them out (they are not catalog rows), so without this an agent
    # whose server it really calls read "0 available right now".
    from admin.external_mcp_tools import listed_tools

    listed = listed_tools(
        [(a.get("mcp_server") or {}) for a in ctx.get("mcp_attachments") or []
         if (a.get("mcp_server") or {}).get("is_active", True)]
        + [m for m in attachable if m.get("is_active", True)])
    for att in ctx.get("mcp_attachments") or []:
        srv = att.get("mcp_server") or {}
        mid = srv.get("mcp_server_id")
        if not mid:
            continue
        attached_ids.add(mid)
        pick = attachable_by_id.get(mid) or {}
        tool_names = list(pick.get("tool_names") or [])
        known = listed.get(str(mid))
        if not tool_names and known:
            tool_names = [t["name"] for t in known]
        subset = list(att.get("tool_subset") or [])
        is_bundle_list = (pick.get("source") or srv.get("source")) == "bundle" and bool(tool_names)
        if srv.get("endpoint_only"):
            count_label = "Connection only — adds no actions"
        elif subset:
            count_label = (f"{len(subset)} of {len(tool_names)} actions"
                           if tool_names else P.plural(len(subset), "action"))
        elif tool_names:
            count_label = f"All {len(tool_names)} actions"
        else:
            count_label = "All actions (listed when it connects)"
        shown = bound_by_server.get(mid)
        if shown is None:
            shown = subset or tool_names
        toolsets.append({
            "mcp_server_id": mid,
            "label": P.toolset_label(srv),
            "tech": srv.get("name") or mid,
            "description": P.toolset_description(srv),
            "source": "Built-in" if (srv.get("source") == "bundle") else "External",
            "is_builtin": bool(srv.get("is_builtin")),
            "endpoint_only": bool(srv.get("endpoint_only")),
            "mode": "list" if is_bundle_list else "text",
            "tool_names": tool_names,
            "subset": subset,
            "count_label": count_label,
            "chips": [_action(n) for n in shown],
        })
    toolset_catalog = []
    for m in attachable:
        names = list(m.get("tool_names") or [])
        if not names and listed.get(str(m.get("mcp_server_id"))):
            names = [t["name"] for t in listed[str(m.get("mcp_server_id"))]]
        toolset_catalog.append({
            "id": m.get("mcp_server_id"),
            "label": P.toolset_label(m),
            "tech": m.get("name"),
            "description": P.toolset_description(m),
            "source": "Built-in" if m.get("source") == "bundle" else "External",
            "mode": "list" if (m.get("source") == "bundle" and names) else "text",
            "count": len(names),
            "actions": [_action(n) for n in names],
            "attached": m.get("mcp_server_id") in attached_ids,
        })

    # ---- knowledge sources -------------------------------------------------
    mapped = ctx.get("mapped") or {}
    source_ids = set(mapped.get("source_ids") or ap.get("data_source_ids") or [])
    sources = ctx.get("data_sources") or []
    knowledge_attached = []
    knowledge_catalog = []
    for d in sources:
        raw_name = d.get("name") or "Unnamed source"
        item = {
            "id": d.get("source_id"),
            "label": P.humanize_slug(raw_name) if (" " not in raw_name) else raw_name,
            "tech": raw_name,
            "kind": P.humanize_slug(d.get("kind") or ""),
            "refresh": d.get("refresh_cadence") or "manual",
            "active": bool(d.get("is_active", True)),
            "suggested": bool(d.get("__suggested")),
        }
        knowledge_catalog.append(item)
        if item["id"] in source_ids:
            knowledge_attached.append(item)
    known_source_ids = {k["id"] for k in knowledge_catalog}
    # Attached ids hidden by a scoped-build filter still post back, untouched.
    hidden_source_ids = [s for s in source_ids if s not in known_source_ids]

    # ---- skills ----------------------------------------------------------
    skill_attached = []
    for att in ctx.get("skill_attachments") or []:
        sk = att.get("skill") or {}
        skill_attached.append({
            "id": sk.get("skill_id"), "label": sk.get("display_name") or P.humanize_slug(sk.get("slug") or ""),
            "tech": sk.get("slug"), "description": sk.get("description") or "",
            "always_on": bool(sk.get("always_on")),
            "version": (att.get("version") or {}).get("version_number"),
        })
    attached_skill_ids = {s["id"] for s in skill_attached}
    skill_catalog = [{
        "id": s.get("skill_id"), "label": s.get("display_name") or P.humanize_slug(s.get("slug") or ""),
        "tech": s.get("slug"), "description": s.get("description") or "",
        "always_on": bool(s.get("always_on")),
        "attached": s.get("skill_id") in attached_skill_ids,
    } for s in (ctx.get("attachable_skills") or []) if s.get("agent_kind") == ap.get("agent_kind")]

    # ---- sub-agents --------------------------------------------------------
    downstream_ids = list(ap.get("downstream_profile_ids") or mapped.get("downstream_ids") or [])
    candidates = ctx.get("downstream_candidates") or [p for p in profiles if p.get("profile_id") != pid]

    def _agent_card(p):
        return {
            "id": p.get("profile_id"),
            "label": p.get("display_name") or P.humanize_slug(p.get("agent_kind") or ""),
            "description": (p.get("description") or "").strip(),
            "status": P.status_info(p.get("status")),
            "delegatable": bool(p.get("delegatable")),
            "tech": f"{p.get('agent_kind')} · {p.get('slug')}",
            # The name the parent's model sees, which is what its
            # instructions must use. Not `ask_<slug>` verbatim: a slug's
            # hyphens and colons become underscores.
            "tool_name": _handoff_tool_name(p.get("slug")),
        }

    sub_selected = []
    for did in downstream_ids:
        p = by_id.get(did)
        sub_selected.append(_agent_card(p) if p else {
            "id": did, "label": "Agent not in this environment", "description": "",
            "status": P.status_info("archived"), "delegatable": False, "tech": did})
    sub_catalog = sorted(
        [_agent_card(p) for p in candidates
         if p.get("status") != "template" and p.get("profile_id") != pid],
        key=lambda c: (0 if c["delegatable"] else 1, c["label"].lower()))
    for c in sub_catalog:
        c["attached"] = c["id"] in set(downstream_ids)
    used_by = [
        {"id": p.get("profile_id"), "label": p.get("display_name") or p.get("agent_kind")}
        for p in profiles
        if pid and (pid in (p.get("downstream_profile_ids") or []))
        and p.get("profile_id") != pid
    ]
    parent = by_id.get(ap.get("parent_profile_id"))

    # ---- prompts -----------------------------------------------------------
    prompts = ctx.get("prompts") or []
    system_prompt = next((p for p in prompts if p.get("slug") == "system"), None)

    # ---- versions ----------------------------------------------------------
    versions = ctx.get("profile_versions") or []
    served_id = ctx.get("served_version_id")
    served_v = next((v for v in versions if v.get("version_id") == served_id), None) if served_id else None

    # ---- voice -------------------------------------------------------------
    own_vc = P.voice_block(ap)
    vc = (owner or {}).get("live") or own_vc
    voice_enabled = bool(owner) or P.has_voice(ap)
    pv = ctx.get("persona_versions") or None
    voice_summary = None
    if voice_enabled:
        langs = list((vc.get("voice_by_language") or {}).keys()) if isinstance(vc.get("voice_by_language"), dict) else []
        voice_summary = {
            "owner_name": (owner or {}).get("display_name"),
            "owner_id": (owner or {}).get("profile_id") or pid,
            "enabled": vc.get("enabled") is not False,
            "voice": vc.get("voice_id") or "Provider default",
            "language": vc.get("language") or "Follows the caller",
            "languages": langs,
            "greeting": (vc.get("greeting_trigger") or "").strip(),
            "persona_slug": vc.get("persona_prompt_slug"),
            "persona_served": ((pv or {}).get("served") or {}).get("version_number") if pv else None,
            "persona_latest": ((pv or {}).get("latest") or {}).get("version_number") if pv else None,
            "persona_diverged": bool((pv or {}).get("diverged")),
            "has_model": bool(vc.get("model_id")),
        }

    # ---- health ------------------------------------------------------------
    e2e = [r for r in (ctx.get("e2e_runs") or []) if r.get("environment", env) == env]
    last_e2e = e2e[0] if e2e else None

    # ---- tabs ----------------------------------------------------------------
    base = f"/admin/agents?env={env}&profile_id={pid}"
    tabs = []
    for key in DETAIL_TABS:
        if key == "voice" and not voice_enabled:
            continue
        href = (f"/admin/agents/{pid}/voice?env={env}" if key == "voice"
                else f"{base}&tab={key}")
        tabs.append((key, _TAB_LABELS[key], href))

    # A single-environment build (every scoped build — the partner
    # builds) has no second environment to promote into or compare against,
    # so it offers neither (no "Promote to Production…", no "Compare with
    # Production"; promote_counterpart keys off this target).
    from core.deployment import single_env_deployment

    promote_target = None
    if env != "prod" and status["key"] != "template" and not single_env_deployment():
        promote_target = {"env": "prod", "label": "Production"} if env == "staging" else {
            "env": "staging", "label": "Staging"}

    # The larger of the resolver's static count and the names shown as chips:
    # the resolver leaves out an external server's tools (they are listed at
    # connect time), so an agent with one chosen external action read
    # "0 actions in 1 toolset" above a chip naming that action.
    shown = len({c["name"] for t in toolsets for c in t["chips"]})
    bound_count = max(bound.get("bound_count") or 0, shown)
    top_chips = P.unique(c["label"] for t in toolsets for c in t["chips"])

    return {
        "agent": {
            "id": pid,
            "name": ap.get("display_name") or P.humanize_slug(ap.get("agent_kind") or ""),
            "description": (ap.get("description") or "").strip(),
            "kind": ap.get("agent_kind"),
            "slug": ap.get("slug"),
            "status": status,
            "channels": channels,
            "is_template": status["key"] == "template",
            "updated_rel": P.relative_time(ap.get("updated_at")),
            "updated_by_you": bool(user_id and ap.get("updated_by") == user_id),
            "parent": ({"id": parent.get("profile_id"),
                        "label": parent.get("display_name") or parent.get("agent_kind")}
                       if parent else None),
        },
        "switch_agents": _tree(ctx, pid),
        "detail_tabs": tabs,
        "legacy_anchors": LEGACY_ANCHORS,
        "main_model": main_model,
        "light_model": light_model,
        "toolsets": toolsets,
        "toolset_catalog": toolset_catalog,
        "external_bound": bound.get("external") or [],
        "legacy_not_bound": bound.get("legacy_not_bound") or [],
        "bound_count": bound_count,
        "top_tool_chips": top_chips[:6],
        "more_tool_chips": max(0, len(top_chips) - 6),
        "knowledge_attached": knowledge_attached,
        "knowledge_catalog": knowledge_catalog,
        "hidden_source_ids": hidden_source_ids,
        "skill_attached": skill_attached,
        "skill_catalog": skill_catalog,
        "sub_selected": sub_selected,
        "own_handoff_tool": _handoff_tool_name(ap.get("slug")),
        "sub_catalog": sub_catalog,
        "used_by": used_by,
        "system_prompt": system_prompt,
        "system_prompt_preview": P.first_lines((system_prompt or {}).get("content")),
        # Template ("mold:*") prompts share the agent kind but only templates
        # use them — listed separately, collapsed, on a real agent.
        "other_prompts": [p for p in prompts if p.get("slug") != "system"
                          and (status["key"] == "template"
                               or not str(p.get("slug") or "").startswith("mold:"))],
        "template_prompts": [p for p in prompts if status["key"] != "template"
                             and str(p.get("slug") or "").startswith("mold:")],
        "served_version": served_v,
        "latest_version": versions[0] if versions else None,
        "voice_enabled": voice_enabled,
        "voice_summary": voice_summary,
        "last_e2e": last_e2e,
        "promote_target": promote_target,
        "compare_options": [
            {"profile_id": p.get("profile_id"),
             "name": p.get("display_name") or p.get("agent_kind"),
             "status": P.status_info(p.get("status"))["label"]}
            for p in profiles if p.get("profile_id") != pid and p.get("status") != "template"
        ],
    }


# --------------------------------------------------------------------------
# Header helpers shared with the voice settings screen (the Voice tab)
# --------------------------------------------------------------------------


def promote_counterpart(target_env: Optional[str], agent_kind: Optional[str],
                        slug: Optional[str]) -> Optional[Dict[str, Any]]:
    """The row a promote to ``target_env`` would replace, if one exists."""
    if not (target_env and agent_kind and slug):
        return None
    try:
        from admin.router import client_for

        rows = (client_for(target_env).table("agent_profiles")
                .select("profile_id, display_name")
                .eq("environment", target_env).eq("agent_kind", agent_kind)
                .eq("slug", slug).limit(1).execute().data or [])
        return rows[0] if rows else None
    except Exception as exc:  # noqa: BLE001 — the dialog degrades to "creates one"
        logger.warning("agents: promote counterpart lookup failed (%s)", exc)
        return None


def load_header_view(env: str, profile_id: str,
                     user_id: Optional[str] = None) -> Dict[str, Any]:
    """Just what agents/_header.html needs, for screens that are not the
    detail route (the voice settings page). One profiles query."""
    from admin.router import client_for
    from admin.screens.agents import resolve_cross_env_refs, resolve_voice_config_owner
    from admin.seed_agent_profiles import visible_agent_kinds
    from core.deployment import scoped_build

    try:
        client = client_for(env)
        profiles = (client.table("agent_profiles").select("*")
                    .eq("environment", env).order("agent_kind").execute().data or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("agents: header lookup failed (%s)", exc)
        return {}
    all_env = list(profiles)
    if scoped_build():
        visible = set(visible_agent_kinds())
        profiles = [p for p in profiles if p.get("agent_kind") in visible]
    resolve_cross_env_refs(profiles, client)
    active = next((p for p in all_env if p.get("profile_id") == profile_id), None)
    if active is None:
        return {}
    by_parent: Dict[str, List[Dict[str, Any]]] = {}
    for p in profiles:
        if p.get("parent_profile_id"):
            by_parent.setdefault(p["parent_profile_id"], []).append(p)
    ctx = {
        "active_profile": active,
        "profiles": profiles,
        "top_level_profiles": [p for p in profiles if not p.get("parent_profile_id")],
        "sub_profiles_by_parent": by_parent,
        "voice_config_owner": resolve_voice_config_owner(all_env, active),
    }
    view = build_detail_view(ctx, env, user_id)
    tabs = list(view["detail_tabs"])
    if not any(k == "voice" for k, _l, _h in tabs):
        # Opened "set up voice" on an agent without one: still show the tab.
        at = next((i for i, (k, _l, _h) in enumerate(tabs) if k == "versions"), len(tabs))
        tabs.insert(at, ("voice", _TAB_LABELS["voice"], f"/admin/agents/{profile_id}/voice?env={env}"))
    view["detail_tabs"] = tabs
    target = (view.get("promote_target") or {}).get("env")
    return {
        "agent": view["agent"],
        "switch_agents": view["switch_agents"],
        "detail_tabs": view["detail_tabs"],
        "promote_target": view["promote_target"],
        "promote_counterpart": promote_counterpart(target, active.get("agent_kind"), active.get("slug")),
        "compare_options": view["compare_options"],
        "header_profile": active,
    }
