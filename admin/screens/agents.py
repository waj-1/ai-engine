"""Helpers backing the /admin/agents screen.

Reads four profiles for the current env, the active agent_kind's prompts,
its tool whitelist, and its data sources. Render is delegated to the
agents.html template.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from admin._model_picker import is_chat_model
from admin._query_batch import run_parallel
from admin.services import profile_versions as pv
from core.agent_profile import _fetch_mcp_attachments
# The writer-side half of the flat-voice_config contract, defined next to the
# read shim it mirrors so the two can never drift apart.
from voice.live.config import promote_legacy_voice_config

logger = logging.getLogger(__name__)


# The 26-key "Duplex voice (live)" fieldset and its writer
# (`_build_live_voice_config_from_form` / `build_voice_config_from_form`,
# gated on the `live_fieldset=1` marker) stood here until 2026-08-26. They
# are DELETED, not moved: the fieldset was the trap the voice-cockpit change
# removes — it wrote `voice_config` to whichever row the operator was
# VIEWING, which on a voice specialist is a row the runtime never reads, and
# it could only express 26 of the keys the runtime honours. The single portal
# writer of `voice_config` is now the voice cockpit's POST
# (`/admin/agents/{id}/voice` → `admin/screens/agent_voice.py::
# voice_config_from_form`), which resolves the OWNER row from the DB by slug
# and owns every `_VOICE_FIELDS` key. `admin/router.py::profile_update` no
# longer touches the column at all.


def resolve_cross_env_refs(profiles, client) -> None:
    """Remap parent/downstream profile ids that point OUTSIDE this env.

    Promoted rows historically copied ``parent_profile_id`` /
    ``downstream_profile_ids`` verbatim from the source env, so a prod
    sub-agent pointed at its dev parent: it rendered nowhere in the left
    rail (not top-level, parent not in the env's id set) and downstream
    checkboxes never ticked even though the count KPI showed. Resolve any
    foreign id to this env's sibling by agent_kind, in place. Ids that
    can't be resolved are left untouched.
    """
    env_ids = {p["profile_id"] for p in profiles}
    by_kind = {}
    for p in profiles:
        # Prefer the active row per kind as the remap target.
        if p["agent_kind"] not in by_kind or (p.get("status") or "active") == "active":
            by_kind[p["agent_kind"]] = p["profile_id"]
    foreign: set = set()
    for p in profiles:
        pid = p.get("parent_profile_id")
        if pid and pid not in env_ids:
            foreign.add(pid)
        for d in p.get("downstream_profile_ids") or []:
            if d not in env_ids:
                foreign.add(d)
    if not foreign:
        return
    try:
        rows = (
            client.table("agent_profiles").select("profile_id, agent_kind")
            .in_("profile_id", list(foreign)).execute().data or []
        )
    except Exception:
        return
    foreign_kind = {r["profile_id"]: r["agent_kind"] for r in rows}

    def _remap(pid):
        if not pid or pid in env_ids:
            return pid
        return by_kind.get(foreign_kind.get(pid, ""), pid)

    for p in profiles:
        if p.get("parent_profile_id"):
            p["parent_profile_id"] = _remap(p["parent_profile_id"])
        if p.get("downstream_profile_ids"):
            p["downstream_profile_ids"] = [_remap(d) for d in p["downstream_profile_ids"]]


def build_bound_tools_panel(
    mcp_attachments: Optional[List[Dict[str, Any]]],
    tool_ids: Optional[List[str]],
    all_tools: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """The "Bound at runtime" panel data for the Agents screen's Tools card.

    The runtime's tool binding is `resolve_mcp_tools_for_profile` over the
    profile's `agent_profile_mcp_servers` attachments narrowed by each
    attachment's `tool_subset` (runtime/tools/_mcp_resolver.py). It never
    reads `agent_profiles.tool_ids` — yet the Tools card used to render
    tool_ids as its primary table with a KPI claiming "Unmapped tools never
    reach the LLM", the exact opposite of the truth (live incident: a
    client demo's voice profile showed 16 tools while serving 13, hiding a
    missing data tool). This panel shows what actually binds.

    The bound NAME set comes from `bound_tool_names_from_attachments` — the
    same helper the promote gate's prompt-tool guard uses — so this display
    can never drift from the resolver's subset semantics. The per-server
    grouping re-walks the attachments only to attach tool_id/display_name
    for linking; a name is only ever shown if the helper says it binds.

    Returns:
      groups        — [{server_name, mcp_server_id, tools: [{python_name,
                       tool_id, display_name}]}] for bundle attachments
      external      — [{name, endpoint_only, subset}] attachments whose tools
                       resolve only at connect time (can't be enumerated
                       statically; the helper omits them on purpose)
      bound_count   — len of the bound name set
      legacy_not_bound — python_names present in the LEGACY tool_ids list but
                       absent from the bound set: listed on the old table,
                       never reaching the LLM.
    """
    # Function-level import, same as admin/services/promotion.py: keeps the
    # admin package from importing the runtime tool stack at module load.
    from runtime.tools._mcp_resolver import bound_tool_names_from_attachments

    attachments = mcp_attachments or []
    bound_names = bound_tool_names_from_attachments(attachments)

    groups: List[Dict[str, Any]] = []
    external: List[Dict[str, Any]] = []
    for att in attachments:
        server = att.get("mcp_server") or {}
        if server.get("endpoint_only") or server.get("source") != "bundle":
            external.append({
                "name": server.get("name") or server.get("mcp_server_id"),
                "endpoint_only": bool(server.get("endpoint_only")),
                "subset": att.get("tool_subset") or [],
            })
            continue
        subset = att.get("tool_subset")
        allowed = set(subset) if subset else None
        tools: List[Dict[str, Any]] = []
        for member in att.get("member_tools") or []:
            row = member.get("tool") or {}
            name = row.get("python_name")
            if not name or (allowed is not None and name not in allowed):
                continue
            if name not in bound_names:
                continue  # the helper is authoritative on the SET
            tools.append({
                "python_name": name,
                "tool_id": row.get("tool_id"),
                "display_name": row.get("display_name"),
            })
        groups.append({
            "server_name": server.get("name") or server.get("mcp_server_id"),
            "mcp_server_id": server.get("mcp_server_id"),
            "tools": tools,
        })

    by_id = {t.get("tool_id"): t for t in (all_tools or [])}
    legacy_not_bound: List[str] = []
    for tid in tool_ids or []:
        name = (by_id.get(tid) or {}).get("python_name")
        if not name or name not in bound_names:
            legacy_not_bound.append(name or (str(tid)[:8] + "…"))

    return {
        "groups": groups,
        "external": external,
        "bound_count": len(bound_names),
        "legacy_not_bound": legacy_not_bound,
    }


# How many ids one `in.(…)` filter carries. 100 UUIDs is ~3.7 KB of URL, well
# inside the 8 KB request line most gateways allow.
_IN_FILTER_BATCH = 100


def _attachable_mcps(
    client,
    visible_profiles: Optional[List[Dict[str, Any]]] = None,
    *,
    servers: Optional[List[Dict[str, Any]]] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Every active `agent_mcp_servers` row enriched with `tool_names`: the
    names a profile's pinned `tool_subset` (agent_profile_mcp_servers) can
    reference. Mirrors core.agent_profile._fetch_mcp_attachments's bundle
    resolution (agent_mcp_tools -> non-deprecated agent_tools.python_name)
    so the attachment editor's picker matches what actually resolves at
    request time; external servers use their own `tool_subset` verbatim.

    On a scoped build the caller passes ``visible_profiles`` (the
    env's already-kind-filtered profiles); the result is then restricted to
    MCP servers referenced by one of those profiles — via the legacy
    ``agent_profiles.mcp_server_ids`` array (the SAME rule the legacy
    ``mcp_servers`` list is filtered on in load_agents_context) UNION the
    new ``agent_profile_mcp_servers`` join table. Without this a
    scoped build's operator could attach a WAJ-only bundle and wire their agent
    to WAJ's internal tools (attachments are runtime-consumed by
    AgentProfileLoader). ``None`` = no gate (full WAJ engine).

    Any failure degrades to ``[]`` (logged) rather than 500-ing the Agents
    page — same posture as core.agent_profile._fetch_mcp_attachments, since
    this runs unguarded on every /admin/agents load.

    ``servers`` / ``tools`` let load_agents_context hand in the
    ``agent_mcp_servers`` / non-deprecated ``agent_tools`` lists it already
    fetched for the mapping editor, so the Agents page stops re-querying those
    two tables a second time per load (they were the top redundant round-trips).
    When omitted (direct callers / tests) we fetch them ourselves as before.
    """
    try:
        if servers is None:
            server_rows = (
                client.table("agent_mcp_servers").select("*")
                .eq("is_active", True).order("name").execute().data or []
            )
        else:
            # Reuse the caller's already-fetched agent_mcp_servers, applying the
            # same is_active gate the query above would have.
            server_rows = [s for s in servers if s.get("is_active")]
        bundle_ids = [s["mcp_server_id"] for s in server_rows if s.get("source") == "bundle"]
        names_by_server: Dict[str, List[str]] = {}
        if bundle_ids:
            member_rows = (
                client.table("agent_mcp_tools").select("*")
                .in_("mcp_server_id", bundle_ids).execute().data or []
            )
            if tools is None:
                # Batched: an `in.(…)` filter rides the URL, and a few hundred
                # UUIDs (the full engine seeds 200+ tools) overflow a gateway's
                # request-line limit. Behind the bundled stack's gateway that
                # was a 414 "URI too long", which the except below turned into
                # an EMPTY attachable list — so no MCP server could be attached
                # to any agent, with nothing on screen saying why (2026-11-22).
                tool_ids = list(dict.fromkeys(m["tool_id"] for m in member_rows))
                tool_rows = []
                for start in range(0, len(tool_ids), _IN_FILTER_BATCH):
                    tool_rows.extend(
                        client.table("agent_tools").select("*")
                        .in_("tool_id", tool_ids[start:start + _IN_FILTER_BATCH])
                        .eq("is_deprecated", False).execute().data or []
                    )
            else:
                # Reuse the caller's already-fetched non-deprecated agent_tools;
                # the dict lookup below only reads ids present in member_rows.
                tool_rows = tools
            names_by_id = {t["tool_id"]: t["python_name"] for t in tool_rows}
            for m in sorted(member_rows, key=lambda r: r.get("position") or 0):
                name = names_by_id.get(m.get("tool_id"))
                if name is None:
                    continue  # deprecated or vanished
                names_by_server.setdefault(m["mcp_server_id"], []).append(name)

        result: List[Dict[str, Any]] = []
        for row in server_rows:
            if row.get("source") == "bundle":
                tool_names = names_by_server.get(row.get("mcp_server_id"), [])
            else:
                tool_names = row.get("tool_subset") or []
            result.append({
                "mcp_server_id": row.get("mcp_server_id"),
                "name": row.get("name"),
                "source": row.get("source"),
                "is_builtin": row.get("is_builtin"),
                "tool_names": tool_names,
                # An external server with no subset of its own exposes every
                # tool it lists, so the valid names are only known by asking
                # it (admin.mcp_membership.subset_names_for).
                "exposes_all_tools": row.get("source") == "external" and not tool_names,
                "endpoint": row.get("endpoint"),
                "auth_env_var": row.get("auth_env_var"),
            })

        # Deployment-scope gate: keep only servers a visible profile references.
        # Mirrors the legacy mcp_servers filter (mcp_server_ids) and unions
        # the new join table so an already-attached tenant MCP still shows.
        if visible_profiles is not None:
            allowed: set = set()
            for _p in visible_profiles:
                allowed.update(_p.get("mcp_server_ids") or [])
            visible_pids = [
                p["profile_id"] for p in visible_profiles if p.get("profile_id")
            ]
            if visible_pids:
                join_rows = (
                    client.table("agent_profile_mcp_servers").select("mcp_server_id")
                    .in_("profile_id", visible_pids).execute().data or []
                )
                allowed.update(r["mcp_server_id"] for r in join_rows)
            result = [r for r in result if r["mcp_server_id"] in allowed]

        return result
    except Exception as exc:
        logger.warning("agents: attachable MCP lookup failed (%s)", exc)
        return []


def _attachable_skills(
    client,
    visible_profiles: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Active PLATFORM skills (business_id IS NULL) attachable from the
    Agents screen. Tenant skills never appear — they resolve dynamically per
    request, not via profile pins. On a scoped build ``visible_profiles``
    restricts the set to the client's visible agent kinds (mirror of
    _attachable_mcps' visibility semantics — skills are kind-owned, so the
    kind filter is the whole gate). Any failure degrades to [] (logged).
    """
    try:
        rows = (
            client.table("agent_skills").select(
                "skill_id, slug, display_name, description, agent_kind, "
                "always_on, business_id")
            .eq("is_active", True).eq("status", "active")
            .order("display_name").execute().data or []
        )
        rows = [r for r in rows if r.get("business_id") is None]
        if visible_profiles is not None:
            visible_kinds = {p.get("agent_kind") for p in visible_profiles}
            rows = [r for r in rows if r.get("agent_kind") in visible_kinds]
        return rows
    except Exception as exc:
        logger.warning("agents: attachable skill lookup failed (%s)", exc)
        return []


def resolve_live_voice_config(
    active_profile: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """The active profile's flat duplex block, resolved through the runtime's shim.

    The "Duplex voice (live)" card this used to feed is gone (2026-08-26 — the
    voice cockpit at `/admin/agents/{id}/voice` renders and writes the block
    now); what remains on this screen is `_load_persona_versions`' read of
    `agent_profile_slug`, which still must resolve exactly the way the runtime
    does, malformed shapes included.

    agents.html used to compute this itself as `vc.get('live') or vc` — a THIRD
    implementation of the compat shim, on Jinja truthiness instead of
    ``isinstance(nested, dict)``, which diverged from the runtime on two shapes:

    * ``live: {}`` — the runtime resolves the empty block (duplex disabled); the
      ``or`` fell through to the TOP level and rendered the retired turn-based
      keys as duplex values. The save path reads managed keys from the POSTed
      form only, so one Save persisted that misread as real flat config.
    * ``live: "somestring"`` — ``.get`` on a string raised ``UndefinedError``
      and, with no try/except in ``agents_view``, 500'd the whole screen. The
      unify migration's ``jsonb_typeof = 'object'`` gate deliberately skips that
      shape, so nothing ever repaired the row.

    Going through ``promote_legacy_voice_config`` — the same function the save
    path applies to ``existing`` — means the card renders exactly what a Save
    would then persist, and leaves the shim ONE definition to delete when the
    rollback window closes.
    """
    stored = (active_profile or {}).get("voice_config")
    if stored is not None and not isinstance(stored, dict):
        # The WHOLE column is malformed, not just `live`. Every reader raises on
        # that shape, and this screen is where an operator would repair it — so
        # render an empty card rather than 500 the one page that can fix the row.
        return {}
    return promote_legacy_voice_config(stored)


def resolve_voice_config_owner(
    profiles: List[Dict[str, Any]],
    active_profile: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """The profile whose voice calls are SERVED BY ``active_profile``, if any.

    A voice specialist does not configure itself: the parent holds
    ``voice_config`` and names the specialist by slug through
    ``agent_profile_slug`` (a client demo seeds its voice brain as a
    separate, latency-tuned profile). The specialist's own
    voice cards are therefore blank and unchecked — accurate, and impossible to
    tell apart from "this agent has no persona and cannot be configured", which
    is exactly how it got reported on a client demo.

    Matched on the slug pointer, deliberately NOT on ``parent_profile_id``:
    those are different relationships and the title / suggestions sub-agents
    share the same parent while owning no voice config. Keying off parentage
    would hang a bogus "configured elsewhere" notice on both of them.

    ``profiles`` must be the env's UNFILTERED profile list (spec D10). It used
    to be the rail's list — already narrowed by the scoped-build
    ``visible_agent_kinds()`` gate — so an owner filtered out of the rail
    resolved to None, the notice vanished, and the specialist's blank-but-
    editable voice card was left looking authoritative. The relationship is a
    property of the tenant's data, not of what the rail happens to be showing,
    so it is resolved against every profile in the environment and only the
    LINK it produces is subject to the rail's filtering.

    Returns ``{profile_id, display_name, live}`` for the owner, or None.
    """
    active_slug = (active_profile or {}).get("slug")
    if not active_slug:
        return None
    from runtime.agents.voice_registry import voice_agent_slug_addressed

    if voice_agent_slug_addressed(str(active_profile.get("agent_kind") or "")):
        # A generic agent is called at its own address and owns its own voice
        # settings, even when another agent borrows it as a brain.
        return None
    for p in profiles:
        if p.get("profile_id") == active_profile.get("profile_id"):
            continue
        sibling_voice = p.get("voice_config")
        if sibling_voice is not None and not isinstance(sibling_voice, dict):
            # Same malformed-column guard as resolve_live_voice_config: a
            # sibling row anywhere in the list with a non-dict voice_config
            # (e.g. a bare string) makes promote_legacy_voice_config's
            # `dict(profile_voice or {})` raise. One bad row must not 500 the
            # whole /admin/agents page for every OTHER profile — skip it, it
            # cannot legitimately be the owner match anyway.
            continue
        live = promote_legacy_voice_config(sibling_voice)
        if live.get("agent_profile_slug") == active_slug:
            return {
                "profile_id": p.get("profile_id"),
                "display_name": p.get("display_name") or p.get("agent_kind"),
                "live": live,
            }
    return None


def load_agents_context(
    env: str,
    selected_kind: Optional[str],
    profile_id: Optional[str] = None,
) -> Dict[str, Any]:
    # Imported inside the function to avoid a circular import at module load
    # (admin.router imports this module's screen helpers).
    from admin.router import client_for

    from admin.seed_agent_profiles import visible_agent_kinds
    from core.deployment import scoped_build

    client = client_for(env)
    profiles = (
        client.table("agent_profiles").select("*")
        .eq("environment", env).order("agent_kind").execute().data or []
    )
    # Every profile in this environment, captured BEFORE the deployment-scope gate
    # below narrows the rail. `resolve_voice_config_owner` reads this list, not
    # the filtered one: the parent/specialist relationship is a fact about the
    # tenant's data, and resolving it from a presentational filter is what made
    # the "configured elsewhere" notice disappear on exactly the builds where
    # the parent is hidden (spec D10).
    all_env_profiles = list(profiles)
    # Scoped build : hide WAJ's catalog so
    # the operator only sees the active client's agents. The rows still exist
    # in the DB (the seeders just stop adding new ones) — this filter is
    # purely presentational. Only applied when a client is active:
    # _AGENT_KINDS is a curated seeder list, not the full kind catalog
    # (it lacks the per-agent _title/_suggestions/_field_edit sub-kinds),
    # so filtering against it unconditionally hid every sub-agent except
    # a partner's and any kind newer than the deployed image.
    # visible_agent_kinds() is already client-parameterized (the active
    # client's main + title + suggestions kinds) — an older gate checked for
    # one scope by name and skipped it entirely for every other client,
    # leaking the full WAJ profile rail into their builds.
    if scoped_build():
        _visible = set(visible_agent_kinds())
        profiles = [p for p in profiles if p.get("agent_kind") in _visible]
    # Surface drafts + archived in the left rail so a forked profile is
    # visible at a glance. Sort active first (operators land on the
    # production-shaped row), drafts next, archived last.
    _status_order = {"active": 0, "draft": 1, "archived": 2}
    resolve_cross_env_refs(profiles, client)
    profiles = sorted(
        profiles,
        key=lambda p: (
            p.get("agent_kind") or "",
            _status_order.get(p.get("status") or "active", 99),
        ),
    )
    # When the operator follows /admin/agents?profile_id=... we honour
    # that explicit pick first (so the fork redirect lands on the new
    # draft, not the still-active sibling), then fall back to the
    # agent_kind selection (legacy URL). Either way we never auto-pick
    # an archived row over an active one.
    active_profile = None
    if profile_id:
        active_profile = next(
            (p for p in profiles if p.get("profile_id") == profile_id), None,
        )
    if active_profile is None and selected_kind:
        active_profile = next(
            (p for p in profiles if p.get("agent_kind") == selected_kind
             and (p.get("status") or "active") == "active"),
            None,
        )
    if active_profile is None and selected_kind:
        active_profile = next(
            (p for p in profiles if p.get("agent_kind") == selected_kind), None,
        )
    if active_profile is None:
        active_profile = next(
            (p for p in profiles if (p.get("status") or "active") == "active"),
            profiles[0] if profiles else None,
        )
    # ------------------------------------------------------------------ #
    # Fan the active profile's independent reads out across a thread pool.
    # Each is a small PostgREST round-trip with a ~0.3-1s floor (network RTT +
    # PostgREST overhead) regardless of payload size; run sequentially they
    # stacked into multi-second page loads. They share no state, so we run
    # them concurrently and collapse ~10 serial round-trips into ~1 wall-clock
    # one. attachable_mcps runs just after the batch — it reuses the tools /
    # mcp_servers fetched here rather than re-querying those two tables.
    # ------------------------------------------------------------------ #
    _kind = active_profile["agent_kind"] if active_profile else None
    _pid = active_profile["profile_id"] if active_profile else None
    # `extra_config` carries the `realtime` flag `is_chat_model` reads. It was
    # missing from this list, so the filter below saw no flag on any row and
    # the duplex-voice model was offered as an agent's main model.
    _model_cols = "model_id, name, provider, model_string, is_active, environment, extra_config"

    def _load_prompts():
        # The editor shows the version THIS profile is pinned to
        # (`prompt_version_ids[slug]`), never "the newest version of the kind".
        # `agent_prompts` rows are shared by every profile of an agent_kind
        # (the active agent, its draft copies, specialists), so the newest
        # version is often one a DIFFERENT copy saved. Seeding the textarea
        # with it made a draft copy's edit appear on the live original, and a
        # Save there would then pin the copy's text onto the original; it also
        # made a Versions-tab Restore (which repoints the pin) look like it
        # had not happened. Only a prompt with no pin on this profile falls
        # back to the newest version, the same convention the seeders and
        # `PromptResolver.resolve_by_slug` use. All versions still come back
        # in ONE .in_() query (the #1401 N+1 fix).
        if not active_profile:
            return []
        if _kind == "custom":
            # A custom agent owns its instructions; it does not share its
            # kind's prompt slots (admin/services/custom_prompts.py).
            from admin.services import custom_prompts
            return [custom_prompts.instructions_view(client, active_profile)]
        rows = (
            client.table("agent_prompts").select("*")
            .eq("agent_kind", _kind).execute().data or []
        )
        prompt_ids = [p["prompt_id"] for p in rows]
        pins = active_profile.get("prompt_version_ids") or {}
        latest_by_prompt: Dict[str, Dict[str, Any]] = {}
        by_version_id: Dict[str, Dict[str, Any]] = {}
        if prompt_ids:
            versions = (
                client.table("agent_prompt_versions")
                .select("prompt_version_id, prompt_id, content, version_number")
                .in_("prompt_id", prompt_ids)
                .order("version_number", desc=True).execute().data or []
            )
            for v in versions:  # globally DESC → first seen per prompt is newest
                latest_by_prompt.setdefault(v["prompt_id"], v)
                if v.get("prompt_version_id"):
                    by_version_id[v["prompt_version_id"]] = v
        for p in rows:
            latest = latest_by_prompt.get(p["prompt_id"]) or {}
            pinned = by_version_id.get(pins.get(p.get("slug")) or "")
            if pinned and pinned.get("prompt_id") != p["prompt_id"]:
                pinned = None  # a pin into another prompt's history: ignore
            shown = pinned or latest
            p["content"] = shown.get("content") or ""
            # Instructions tab: "Version N" is the version this agent serves;
            # the prompt-diff link still spans the newest two versions.
            p["version_number"] = shown.get("version_number")
            p["latest_version_number"] = latest.get("version_number")
        return rows

    def _load_profile_versions():
        # Version bar (Phase P1): the active profile's agent_profile_versions
        # history for this env, via admin.services.profile_versions (the single
        # write/read path for served_version_id). `pv.list_versions` uses its
        # own `admin_service_client()`, so a lookup failure degrades to [] here
        # (logged) rather than 500-ing the Agents page — same posture as the
        # attachable_* loaders.
        if not active_profile:
            return []
        try:
            return pv.list_versions(active_profile["profile_id"], env)
        except Exception as exc:
            logger.warning("agents: profile version list failed (%s)", exc)
            return []

    def _load_models():
        # Scoped to the active env so admins don't accidentally point a dev
        # profile at a prod-tuned model row. Profile mappings still resolve
        # cross-env via AgentProfileLoader._fetch_model's sibling lookup.
        # `model_main_id` is a CHAT model. Realtime-flagged rows (the duplex
        # voice ears-and-mouth model, resolved separately via
        # voice_config.model_id) are excluded through the shared
        # `is_chat_model` predicate so they can't be picked here by accident --
        # building one for text chat breaks every turn for that profile. One
        # definition is shared with the other picker; see admin/_model_picker.py.
        rows = [
            m for m in (
                client.table("agent_models").select(_model_cols)
                .eq("is_active", True).eq("environment", env).order("name").execute().data or []
            )
            if is_chat_model(m)
        ]
        if not rows:
            # Some DBs label model rows with envs outside the portal's
            # staging/prod pair (e.g. "dev" seeds) — an empty picker would
            # make the assigned model invisible and uneditable, which is
            # worse than showing cross-env rows (flagged in the template).
            rows = [
                m for m in (
                    client.table("agent_models").select(_model_cols)
                    .eq("is_active", True).order("name").execute().data or []
                )
                if is_chat_model(m)
            ]
        # The assigned model must always be present (and selected) even when
        # it is inactive or filtered out above, so the picker reflects reality
        # and a Save without touching it is a no-op.
        assigned_id = (active_profile or {}).get("model_main_id")
        if assigned_id and not any(m.get("model_id") == assigned_id for m in rows):
            assigned_rows = (
                client.table("agent_models").select(_model_cols)
                .eq("model_id", assigned_id).execute().data or []
            )
            rows = assigned_rows + rows
        return rows

    # (`_load_realtime_models` — the duplex-voice model picker's loader — left
    # with the fieldset on 2026-08-26. The voice cockpit keeps its own copy:
    # admin/screens/agent_voice.py::_load_realtime_models.)

    def _load_e2e():
        if not active_profile:
            return []
        return (
            client.table("agent_e2e_runs").select("*")
            .eq("profile_id", _pid)
            .order("created_at", desc=True).limit(10).execute().data or []
        )

    def _load_mcp_attachments():
        return _fetch_mcp_attachments(client, _pid) if active_profile else []

    def _load_skill_attachments():
        if not active_profile:
            return []
        from core.skills import fetch_skill_attachments

        return fetch_skill_attachments(client, _pid)

    def _load_attachable_skills():
        if not active_profile:
            return []
        return _attachable_skills(client, profiles if scoped_build() else None)

    served_version_id = active_profile.get("served_version_id") if active_profile else None

    # Resolved BEFORE the parallel batch (it is a pure function over the
    # profile list, which is already in hand) so `_load_persona_versions` can
    # join the batch rather than adding a serial round-trip after it. Fed the
    # UNFILTERED env list — see resolve_voice_config_owner's docstring: a rail
    # filter that hides the owner must not be able to hide the relationship.
    voice_config_owner = resolve_voice_config_owner(all_env_profiles, active_profile)

    def _load_persona_versions():
        """Served-vs-latest for the duplex persona — see admin/_persona_versions.py.

        The pair is always (owner, specialist), whichever of the two the
        operator is looking at: the owner names the slug and holds the pin, the
        specialist is only needed to detect a legacy misplaced one. Viewing the
        specialist must show the SAME answer as viewing the parent — the
        divergence is a property of the tenant, not of the page.

        Degrades to None on any failure: this is a diagnostic banner, and a
        Supabase hiccup must not take the whole Agents screen with it.
        """
        if not active_profile:
            return None
        try:
            from admin._persona_versions import collect_persona_version_status

            if voice_config_owner:
                # `all_env_profiles`, not the rail's filtered list: the owner
                # can be filtered out of the rail while still being the row
                # whose pin decides what a caller hears.
                owner_row = next(
                    (p for p in all_env_profiles
                     if p.get("profile_id") == voice_config_owner["profile_id"]),
                    None,
                )
                return collect_persona_version_status(
                    client, environment=env,
                    parent_row=owner_row, specialist_row=active_profile,
                )
            served_slug = resolve_live_voice_config(active_profile).get("agent_profile_slug")
            specialist_row = next(
                (p for p in all_env_profiles
                 if served_slug and p.get("slug") == served_slug
                 and p.get("profile_id") != active_profile.get("profile_id")),
                None,
            )
            return collect_persona_version_status(
                client, environment=env,
                parent_row=active_profile, specialist_row=specialist_row,
            )
        except Exception as exc:
            logger.warning("agents: persona version status failed (%s)", exc)
            return None

    # Show EVERY non-deprecated tool / source / MCP server on the mapping
    # editor. The agent_kind metadata on each row drives row ordering
    # (suggested mappings sort first), but every checkbox is selectable
    # so admins can attach a tool to any profile they want.
    _batch = run_parallel({
        "prompts": _load_prompts,
        "profile_versions": _load_profile_versions,
        "tools": lambda: (
            client.table("agent_tools").select("*")
            .eq("is_deprecated", False).order("python_name").execute().data or []
        ),
        "data_sources": lambda: (
            client.table("agent_data_sources").select("*").order("name").execute().data or []
        ),
        "mcp_servers": lambda: (
            client.table("agent_mcp_servers").select("*").order("name").execute().data or []
        ),
        "e2e": _load_e2e,
        "models": _load_models,
        "mcp_attachments": _load_mcp_attachments,
        "skill_attachments": _load_skill_attachments,
        "attachable_skills": _load_attachable_skills,
        "persona_versions": _load_persona_versions,
    })
    prompts = _batch["prompts"]
    profile_versions = _batch["profile_versions"]
    tools = _batch["tools"]
    data_sources = _batch["data_sources"]
    mcp_servers = _batch["mcp_servers"]
    e2e_runs = _batch["e2e"]
    all_models = _batch["models"]
    mcp_attachments = _batch["mcp_attachments"]
    skill_attachments = _batch["skill_attachments"]
    attachable_skills = _batch["attachable_skills"]
    persona_versions = _batch["persona_versions"]
    # Unfiltered snapshots for _attachable_mcps' dedup: the deployment-scope gate
    # below may reassign `tools` / `mcp_servers` to filtered lists, but the
    # MCP picker must see the full set (it applies its own tenant gate).
    _tools_all = tools
    _mcp_servers_all = mcp_servers

    # Scoped build: hide other agents' tool/data-source rows (their
    # agent_kind chips would otherwise expose WAJ's full catalog). Keep rows
    # whose primary or additional kind is visible. No-op when no client is
    # active (full catalog). Same gate swap as the profile filter above —
    # the by-name check used to leave other scopes unfiltered.
    if scoped_build():
        _vis = set(visible_agent_kinds())

        def _kind_visible(row: Dict[str, Any]) -> bool:
            return (row.get("agent_kind") in _vis) or bool(
                _vis & set(row.get("additional_agent_kinds") or [])
            )

        tools = [t for t in tools if _kind_visible(t)]
        data_sources = [d for d in data_sources if _kind_visible(d)]

        # MCP servers carry no agent_kind, so kind-filtering can't apply. Keep
        # only servers referenced by a visible profile's mcp_server_ids — on a
        # shared staging/prod DB this hides WAJ's MCP servers, which the
        # by-kind filter above would otherwise leave on the mapping editor.
        _referenced: set = set()
        for _p in profiles:
            _referenced.update(_p.get("mcp_server_ids") or [])
        mcp_servers = [m for m in mcp_servers if m.get("mcp_server_id") in _referenced]

    def _is_suggested(row: Dict[str, Any], kind: str) -> bool:
        primary = row.get("agent_kind")
        extras = row.get("additional_agent_kinds") or []
        return primary == kind or kind in extras

    if active_profile:
        kind = active_profile["agent_kind"]
        # Suggested rows (primary or additional kind match) sort first.
        tools = sorted(tools, key=lambda t: (0 if _is_suggested(t, kind) else 1, t.get("python_name", "")))
        data_sources = sorted(data_sources, key=lambda d: (0 if _is_suggested(d, kind) else 1, d.get("name", "")))
        for t in tools:
            t["__suggested"] = _is_suggested(t, kind)
        for d in data_sources:
            d["__suggested"] = _is_suggested(d, kind)

    mapped = {
        "tool_ids": set(active_profile.get("tool_ids") or []) if active_profile else set(),
        "source_ids": set(active_profile.get("data_source_ids") or []) if active_profile else set(),
        "mcp_ids": set(active_profile.get("mcp_server_ids") or []) if active_profile else set(),
        "downstream_ids": set(active_profile.get("downstream_profile_ids") or []) if active_profile else set(),
    }
    # Eligible downstream picks = every other profile in the same env.
    downstream_candidates = [
        p for p in profiles
        if active_profile is None or p.get("profile_id") != active_profile.get("profile_id")
    ]
    # e2e_runs and all_models were fetched in the parallel batch above.

    # Split top-level vs sub-agents so the left rail can render parents
    # with their children indented underneath.
    top_level = [p for p in profiles if not p.get("parent_profile_id")]
    by_parent: Dict[str, List[Dict[str, Any]]] = {}
    for p in profiles:
        parent_id = p.get("parent_profile_id")
        if parent_id:
            by_parent.setdefault(parent_id, []).append(p)

    # Auto-extracted FastAPI Pydantic schema for the active profile's
    # endpoint_path (read-only) + operator-stashed input_validators
    # (writable). The card in agents.html shows both side by side.
    input_schema: Optional[Dict[str, Any]] = None
    input_validators: Dict[str, Any] = {}
    if active_profile and active_profile.get("endpoint_path"):
        try:
            from admin._endpoint_schemas import get_input_schema_for_endpoint

            input_schema = get_input_schema_for_endpoint(active_profile["endpoint_path"])
        except Exception:
            input_schema = None
        raw_validators = active_profile.get("input_validators") or {}
        if isinstance(raw_validators, dict):
            input_validators = raw_validators

    # Phase 3b attachment editor: the active profile's current
    # agent_profile_mcp_servers join rows + every attachable MCP with its
    # exposable tool names. mcp_attachments / skill_attachments /
    # attachable_skills were fetched in the parallel batch above; only the
    # MCP picker is computed here because it reuses the tools / mcp_servers
    # already loaded (dedup) rather than re-querying those two tables.
    # Additive to the legacy mcp_servers / mapped.mcp_ids
    # (agent_profiles.mcp_server_ids) keys, which the join-table editor
    # does not replace here.
    attachable_mcps: List[Dict[str, Any]] = []
    if active_profile:
        # On a scoped build restrict the picker to servers this tenant's
        # (already kind-filtered) profiles reference; `profiles` is None-gated
        # so the full WAJ engine sees every server. `servers` / `tools` hand
        # in the lists the batch already fetched so this doesn't re-query them.
        attachable_mcps = _attachable_mcps(
            client,
            profiles if scoped_build() else None,
            servers=_mcp_servers_all,
            tools=_tools_all,
        )

    # "Bound at runtime" panel: what the MCP resolver actually gives the LLM
    # for this profile — computed from the attachments already fetched, against
    # the UNFILTERED tool list (the legacy comparison must see every tool_ids
    # row, not the scope-narrowed view). Degrades to None so a helper
    # failure can't take the Agents screen down.
    bound_tools: Optional[Dict[str, Any]] = None
    if active_profile:
        try:
            bound_tools = build_bound_tools_panel(
                mcp_attachments,
                active_profile.get("tool_ids") or [],
                _tools_all,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("agents: bound-tools panel failed (%s)", exc)

    return {
        # An explicit ?profile_id= that is not in this environment (e.g. the
        # env switcher flipped under an open agent): the route shows the
        # list with a notice instead of silently opening another agent.
        "profile_missing": bool(profile_id) and (
            active_profile is None or active_profile.get("profile_id") != profile_id),
        "profiles": profiles,
        "voice_config_owner": voice_config_owner,
        # Served-vs-latest for the duplex persona (None when this profile pair
        # has no persona) — the mandatory mitigation for the persona's promote
        # governance, per the voice-config-unification spec's Decision 2.
        "persona_versions": persona_versions,
        "top_level_profiles": top_level,
        "sub_profiles_by_parent": by_parent,
        "active_profile": active_profile,
        "profile_versions": profile_versions,
        "served_version_id": served_version_id,
        "prompts": prompts,
        "tools": tools,
        "data_sources": data_sources,
        "mcp_servers": mcp_servers,
        "mcp_attachments": mcp_attachments,
        "attachable_mcps": attachable_mcps,
        "bound_tools": bound_tools,
        "skill_attachments": skill_attachments,
        "attachable_skills": attachable_skills,
        "downstream_candidates": downstream_candidates,
        "e2e_runs": e2e_runs,
        "mapped": mapped,
        "all_models": all_models,
        "input_schema": input_schema,
        "input_validators": input_validators,
    }
