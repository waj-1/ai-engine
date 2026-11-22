"""Fork-and-diff handlers for agent profiles.

Attached as a sidecar to `admin.router.router` so the diff/fork logic
stays out of the already-large router module. The router imports this
file at the bottom for its side-effects (matching how tool_handlers /
kb_handlers / model_handlers are wired in).

Two endpoints:
  POST /admin/agents/{profile_id}/fork
       Clones an agent_profiles row into a new staging/draft row so the
       operator can iterate without breaking the active runtime
       config. Audit-logged.
  GET  /admin/agents/diff?a=<id>&b=<id>
       Side-by-side diff of two profile rows: scalar fields, array
       memberships (tools / data sources / MCP / sub-agents), and
       prompt assignments. Array IDs are resolved to display names
       so the operator can read the diff without bouncing tabs.

Prompts deliberately do NOT fork. `agent_prompts` rows are keyed by
`agent_kind`, not profile_id — so the parent + the fork share the same
prompt version history, and operators iterate on prompts via the
prompt-version system instead of by cloning rows.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from admin.auth import require_superuser
from admin.router import router, templates, _safe_list
from admin.services.profiles import ProfileNotFoundError, fork_profile_row
from core.auth import get_service_client, admin_service_client

logger = logging.getLogger(__name__)


@router.post("/agents/{profile_id}/fork")
async def agents_fork(
    profile_id: str,
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Clone a profile row into a draft for iteration.

    The fork always lands in `staging` regardless of the source
    environment, as a `draft` so the loader's status='active' filter hides
    it from the runtime until an operator promotes it.
    """
    try:
        new_row = fork_profile_row(profile_id=profile_id, actor_user_id=ctx.get("user_id"))
    except ProfileNotFoundError:
        raise HTTPException(status_code=404, detail="profile not found")
    return RedirectResponse(
        url=f"/admin/agents?profile_id={new_row['profile_id']}",
        status_code=302,
    )


@router.get("/agents/diff", response_class=HTMLResponse)
async def agents_diff(
    request: Request,
    a: str = Query(..., description="profile_id A (left)"),
    b: str = Query(..., description="profile_id B (right)"),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    """Render a side-by-side diff of two agent_profiles rows."""
    client = admin_service_client()
    rows = _safe_list(
        client.table("agent_profiles").select("*").in_("profile_id", [a, b]).execute()
    )
    by_id = {r["profile_id"]: r for r in rows}
    profile_a = by_id.get(a)
    profile_b = by_id.get(b)
    if profile_a is None or profile_b is None:
        missing = a if profile_a is None else b
        raise HTTPException(status_code=404, detail=f"profile not found: {missing}")

    diff = compute_profile_diff(client, profile_a, profile_b)

    return templates.TemplateResponse(
        request, "agents_diff.html",
        {
            "ctx": ctx,
            "active": "agents",
            "current_env": profile_a.get("environment") or "staging",
            "profile_a": profile_a,
            "profile_b": profile_b,
            "diff": diff,
            **diff_display(client, profile_a, profile_b),
        },
    )


# Plain-language labels for the scalar rows (the field name stays visible as
# a muted technical name).
FIELD_LABELS = {
    "display_name": "Name",
    "description": "Description",
    "model_main_id": "Model",
    "model_light_id": "Light model",
    "endpoint_path": "Channel address",
    "parent_profile_id": "Parent agent",
    "environment": "Environment",
    "status": "Status",
    "agent_kind": "Kind",
}
_ID_FIELDS = {"model_main_id": "model", "model_light_id": "model",
              "parent_profile_id": "profile"}


def diff_display(client, profile_a: Dict[str, Any],
                 profile_b: Dict[str, Any]) -> Dict[str, Any]:
    """Names for the ids the scalar rows carry (models, parent agent), so the
    diff reads "Gemini 3 Flash" instead of a UUID. Best-effort: an id that
    can't be resolved renders as its short prefix."""
    model_ids = [p.get(f) for p in (profile_a, profile_b)
                 for f in ("model_main_id", "model_light_id") if p.get(f)]
    parent_ids = [p.get("parent_profile_id") for p in (profile_a, profile_b)
                  if p.get("parent_profile_id")]
    names: Dict[str, str] = {}
    names.update(_resolve_labels(client, "agent_models", "model_id", "name", model_ids))
    names.update(_resolve_labels(client, "agent_profiles", "profile_id", "display_name", parent_ids))
    return {"field_labels": FIELD_LABELS, "value_names": names, "id_fields": _ID_FIELDS}


# ---------------------------------------------------------------------------
# Diff computation — pure function so we can test it without TestClient.
# ---------------------------------------------------------------------------


SCALAR_FIELDS = (
    "display_name",
    "description",
    "model_main_id",
    "model_light_id",
    "endpoint_path",
    "parent_profile_id",
    "environment",
    "status",
    "agent_kind",
)


def compute_profile_diff(
    client,
    profile_a: Dict[str, Any],
    profile_b: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the diff dict consumed by agents_diff.html.

    Shape:
      {
        scalars: [{field, a, b, equal}, ...],
        tools / data_sources / mcp_servers / sub_agents: {
            added_in_b: [{id, label}],
            removed_in_b: [{id, label}],
            shared: [{id, label}],
        },
        prompts: {shared: [...], kind_mismatch: bool},
      }
    """
    scalars = [
        {
            "field": f,
            "a": profile_a.get(f),
            "b": profile_b.get(f),
            "equal": profile_a.get(f) == profile_b.get(f),
        }
        for f in SCALAR_FIELDS
    ]

    # Resolve all the ids we'll need to label in one pass per table.
    tool_labels = _resolve_labels(
        client, "agent_tools", "tool_id", "python_name",
        (profile_a.get("tool_ids") or []) + (profile_b.get("tool_ids") or []),
    )
    source_labels = _resolve_labels(
        client, "agent_data_sources", "source_id", "name",
        (profile_a.get("data_source_ids") or []) + (profile_b.get("data_source_ids") or []),
    )
    mcp_labels = _resolve_labels(
        client, "agent_mcp_servers", "mcp_server_id", "name",
        (profile_a.get("mcp_server_ids") or []) + (profile_b.get("mcp_server_ids") or []),
    )
    sub_labels = _resolve_labels(
        client, "agent_profiles", "profile_id", "display_name",
        (profile_a.get("downstream_profile_ids") or [])
        + (profile_b.get("downstream_profile_ids") or []),
    )

    tools = _array_diff(
        profile_a.get("tool_ids") or [], profile_b.get("tool_ids") or [], tool_labels,
    )
    data_sources = _array_diff(
        profile_a.get("data_source_ids") or [],
        profile_b.get("data_source_ids") or [],
        source_labels,
    )
    mcp_servers = _array_diff(
        profile_a.get("mcp_server_ids") or [],
        profile_b.get("mcp_server_ids") or [],
        mcp_labels,
    )
    sub_agents = _array_diff(
        profile_a.get("downstream_profile_ids") or [],
        profile_b.get("downstream_profile_ids") or [],
        sub_labels,
    )

    # Prompts: agent_prompts is keyed by agent_kind. If the two profiles
    # share a kind they share their prompt rows verbatim — operators iterate
    # on prompts via versions, not by forking. If the kinds DIFFER, the
    # fork is suspicious (you can't fork one kind of agent into another
    # without rewriting everything) — surface a banner.
    kind_a = profile_a.get("agent_kind")
    kind_b = profile_b.get("agent_kind")
    prompts: Dict[str, Any] = {"kind_mismatch": kind_a != kind_b, "shared": []}
    if kind_a == kind_b and kind_a:
        try:
            prompt_rows = (
                client.table("agent_prompts").select("*")
                .eq("agent_kind", kind_a).execute().data or []
            )
            for p in prompt_rows:
                versions = (
                    client.table("agent_prompt_versions")
                    .select("version_number, created_at")
                    .eq("prompt_id", p["prompt_id"])
                    .order("version_number", desc=True).limit(1)
                    .execute().data or []
                )
                latest = versions[0]["version_number"] if versions else None
                prompts["shared"].append({
                    "prompt_id": p["prompt_id"],
                    "slug": p.get("slug"),
                    "latest_version": latest,
                })
        except Exception as exc:
            logger.warning("compute_profile_diff: prompt load failed (%s)", exc)

    # Toolsets (agent_profile_mcp_servers) + chosen actions (tool_subset) and
    # skills (agent_profile_skills) — what the runtime actually binds. The
    # legacy tool_ids / mcp_server_ids lists above are kept for reference.
    atts_a = _mcp_attachments_for(client, profile_a)
    atts_b = _mcp_attachments_for(client, profile_b)
    toolsets = toolsets_diff(atts_a, atts_b)
    skills_a = _skill_ids_for(client, profile_a)
    skills_b = _skill_ids_for(client, profile_b)
    skill_labels = {**(skills_a or {}), **(skills_b or {})}
    skills = _array_diff(list(skills_a or {}), list(skills_b or {}), skill_labels)
    skills["unknown"] = skills_a is None or skills_b is None

    return {
        "scalars": scalars,
        "toolsets": toolsets,
        "skills": skills,
        "tools": tools,
        "data_sources": data_sources,
        "mcp_servers": mcp_servers,
        "sub_agents": sub_agents,
        "prompts": prompts,
    }


# ---------------------------------------------------------------------------
# Toolsets + chosen actions, skills
# ---------------------------------------------------------------------------


def _mcp_attachments_for(client, profile: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """A profile's toolset attachments in core.agent_profile._fetch_mcp_attachments
    shape. A saved-version snapshot (``_snapshot``) carries its own frozen copy;
    None = that version predates attachment freezing (unknown, not empty)."""
    if profile.get("_snapshot"):
        return profile.get("mcp_attachments")
    if "mcp_attachments" in profile:
        return profile.get("mcp_attachments") or []
    if not profile.get("profile_id"):
        return []
    try:
        from core.agent_profile import _fetch_mcp_attachments

        return _fetch_mcp_attachments(client, profile["profile_id"])
    except Exception as exc:  # noqa: BLE001 — a diff section must not 500 the page
        logger.warning("compute_profile_diff: toolset load failed (%s)", exc)
        return []


def _skill_ids_for(client, profile: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """``{skill_id: name}`` of a profile's attached platform skills, in order.
    Snapshot: from its frozen ``skill_attachments`` (None when not frozen)."""
    def _from_entries(entries) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for e in entries or []:
            sk = (e or {}).get("skill") or {}
            if sk.get("skill_id"):
                out[sk["skill_id"]] = sk.get("display_name") or sk.get("slug") or sk["skill_id"][:8]
        return out

    if profile.get("_snapshot"):
        entries = profile.get("skill_attachments")
        return None if entries is None else _from_entries(entries)
    if "skill_attachments" in profile:
        return _from_entries(profile.get("skill_attachments"))
    if not profile.get("profile_id"):
        return {}
    try:
        rows = (client.table("agent_profile_skills").select("skill_id, position")
                .eq("profile_id", profile["profile_id"]).execute().data or [])
        ids = [r["skill_id"] for r in sorted(rows, key=lambda r: r.get("position") or 0)
               if r.get("skill_id")]
        names = _resolve_labels(client, "agent_skills", "skill_id", "display_name", ids)
        return {i: names.get(i, i[:8]) for i in ids}
    except Exception as exc:  # noqa: BLE001
        logger.warning("compute_profile_diff: skill load failed (%s)", exc)
        return {}


def _action_label(name: str, labels: Dict[str, str]) -> str:
    if labels.get(name):
        return labels[name]
    from admin.testing_view import humanize

    return humanize(name) or name


def _toolset_view(att: Dict[str, Any]) -> Dict[str, Any]:
    """One attachment -> {id, name, source, offered[], chosen[] | None (= all),
    labels}. ``offered`` is empty for an external server (its actions are only
    known at connect time)."""
    server = att.get("mcp_server") or {}
    offered: List[str] = []
    labels: Dict[str, str] = {}
    for m in att.get("member_tools") or []:
        tool = (m or {}).get("tool") or {}
        name = tool.get("python_name")
        if name:
            offered.append(name)
            if tool.get("display_name"):
                labels[name] = tool["display_name"]
    subset = att.get("tool_subset")
    return {
        "id": server.get("mcp_server_id") or server.get("name"),
        "name": server.get("name") or str(server.get("mcp_server_id") or "")[:8],
        "source": server.get("source"),
        "offered": offered,
        "chosen": list(subset) if subset else None,
        "labels": labels,
    }


def _effective(view: Dict[str, Any]) -> Optional[List[str]]:
    """The actions this attachment binds; None = every action an external
    server offers (not enumerable here)."""
    if view["chosen"] is not None:
        if not view["offered"]:
            return view["chosen"]
        offered = set(view["offered"])
        return [a for a in view["chosen"] if a in offered]
    return list(view["offered"]) if view["offered"] else None


def _summary(view: Optional[Dict[str, Any]]) -> str:
    if view is None:
        return ""
    eff = _effective(view)
    n_off = len(view["offered"])
    if eff is None:
        return "All actions"
    if view["chosen"] is None or (n_off and len(eff) == n_off):
        return f"All {len(eff)} action{'s' if len(eff) != 1 else ''}"
    return f"{len(eff)} of {n_off} actions" if n_off else f"{len(eff)} action{'s' if len(eff) != 1 else ''}"


def toolsets_diff(atts_a: Optional[List[Dict[str, Any]]],
                  atts_b: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Diff two profiles' toolsets and the actions chosen in each.

    Pure (takes ``_fetch_mcp_attachments``-shaped lists). Each item:
    ``{id, name, state: added|removed|changed|same, summary_a, summary_b,
    actions: [{name, label, state: added|removed|same}]}``; ``actions`` is
    empty when an external server's action list can't be enumerated.
    ``unknown`` is True when either side has no record (an old version)."""
    unknown = atts_a is None or atts_b is None
    va = {v["id"]: v for v in (_toolset_view(a) for a in atts_a or [])}
    vb = {v["id"]: v for v in (_toolset_view(a) for a in atts_b or [])}
    order = list(va) + [k for k in vb if k not in va]
    items: List[Dict[str, Any]] = []
    for sid in order:
        a, b = va.get(sid), vb.get(sid)
        labels = {**((a or {}).get("labels") or {}), **((b or {}).get("labels") or {})}
        ea = _effective(a) if a else []
        eb = _effective(b) if b else []
        actions: List[Dict[str, Any]] = []
        if a is None:
            state = "added"
            actions = [{"name": n, "label": _action_label(n, labels), "state": "added"} for n in eb or []]
        elif b is None:
            state = "removed"
            actions = [{"name": n, "label": _action_label(n, labels), "state": "removed"} for n in ea or []]
        elif ea is None or eb is None:
            state = "same" if ea == eb else "changed"
            for n in (ea or eb or []):
                actions.append({"name": n, "label": _action_label(n, labels),
                                "state": "removed" if ea is not None else "added"})
        else:
            sa, sb = set(ea), set(eb)
            for n in ea:
                actions.append({"name": n, "label": _action_label(n, labels),
                                "state": "same" if n in sb else "removed"})
            for n in eb:
                if n not in sa:
                    actions.append({"name": n, "label": _action_label(n, labels), "state": "added"})
            state = "same" if sa == sb else "changed"
        items.append({
            "id": sid, "name": (b or a)["name"], "source": (b or a).get("source"),
            "state": state, "summary_a": _summary(a), "summary_b": _summary(b),
            "actions": actions,
            "n_added": sum(1 for x in actions if x["state"] == "added"),
            "n_removed": sum(1 for x in actions if x["state"] == "removed"),
            "n_same": sum(1 for x in actions if x["state"] == "same"),
        })
    rank = {"added": 0, "removed": 1, "changed": 2, "same": 3}
    items.sort(key=lambda x: (rank[x["state"]], x["name"].lower()))
    counts = {k: sum(1 for x in items if x["state"] == k) for k in rank}
    return {"items": items, "counts": counts, "unknown": unknown,
            "n_changed": counts["added"] + counts["removed"] + counts["changed"]}


def _array_diff(
    list_a: List[str], list_b: List[str], labels: Dict[str, str],
) -> Dict[str, List[Dict[str, str]]]:
    """Diff two id-lists, labelling each id from the lookup map."""
    set_a = set(list_a or [])
    set_b = set(list_b or [])
    return {
        "added_in_b": [
            {"id": i, "label": labels.get(i, i[:8] if i else "")}
            for i in sorted(set_b - set_a)
        ],
        "removed_in_b": [
            {"id": i, "label": labels.get(i, i[:8] if i else "")}
            for i in sorted(set_a - set_b)
        ],
        "shared": [
            {"id": i, "label": labels.get(i, i[:8] if i else "")}
            for i in sorted(set_a & set_b)
        ],
    }


def _resolve_labels(
    client,
    table: str,
    id_field: str,
    label_field: str,
    ids: List[str],
) -> Dict[str, str]:
    """Bulk-resolve `{id: label}` for the given ids. Empty input → empty map."""
    unique_ids = sorted({i for i in (ids or []) if i})
    if not unique_ids:
        return {}
    try:
        rows = (
            client.table(table)
            .select(f"{id_field}, {label_field}")
            .in_(id_field, unique_ids)
            .execute().data or []
        )
    except Exception as exc:
        logger.warning(
            "_resolve_labels: %s.%s lookup failed (%s); falling back to id prefixes",
            table, label_field, exc,
        )
        return {}
    return {r[id_field]: (r.get(label_field) or r[id_field][:8]) for r in rows}


# Re-exported for the diff endpoint's test suite.
__all__ = ["compute_profile_diff", "agents_fork", "agents_diff"]
