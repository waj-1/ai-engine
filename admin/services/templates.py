"""Fork the protected template catalog into named draft agents.

use_template() forks a mold (recursively for composites) into staging drafts,
rewiring the new parent's downstream_profile_ids to the new sub-agent drafts,
and optionally attaching the new root as a subagent of an existing profile.
Backed by agent_profiles status='template'; reuses fork_profile_row."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from admin.audit import record_change
from admin.services.profiles import (
    ProfileNotFoundError, fork_profile_row, invalidate_profile_loader,
)


class TemplateNotFoundError(Exception):
    pass

import logging

logger = logging.getLogger(__name__)


def _rows(resp) -> List[Dict[str, Any]]:
    return getattr(resp, "data", None) or []


def _served_kinds() -> Optional[set]:
    """The agent kinds this build can run, or None for the full engine (all).

    A scoped (partner) build serves only its scope's kinds — the `agents`
    build runs the four `agents_*` kinds, not the kind the molds are stored
    under. An agent made from such a mold had no runtime and no chat endpoint
    on that build, and the build's agent list (which is scoped to the same
    kinds) never showed it. The gallery therefore offers
    only molds of kinds the build serves, and `use_template` refuses the rest.
    """
    from core.deployment import scoped_build

    if not scoped_build():
        return None
    from admin.seed_agent_profiles import visible_agent_kinds

    return set(visible_agent_kinds())


def _model_rows(client) -> Optional[Dict[str, Dict[str, Any]]]:
    """model_id -> agent_models row; None when the rows cannot be read (then
    nothing is decided about the template's model)."""
    try:
        rows = _rows(client.table("agent_models").select("*").execute())
    except Exception as exc:  # noqa: BLE001 — the picker then shows no default
        logger.warning("templates: model rows unavailable (%s)", exc)
        return None
    return {str(r.get("model_id")): r for r in rows if isinstance(r, dict) and r.get("model_id")}


def default_model_choice(tmpl: Dict[str, Any], models: Dict[str, Dict[str, Any]],
                         environment: str = "staging") -> Dict[str, Any]:
    """The model an agent made from `tmpl` gets when no model is picked.

    The template's own model when it can run on this install; otherwise the
    Automatic row (the first usable model, core/model_resolver.py), so a new
    agent never starts pinned to a provider with no key. Returns
    ``{"auto": bool, "label": str, "template_model": str|None, "reason": str|None}``:
    `label` is what the New agent window shows as the default."""
    from core.model_resolver import (
        AUTO_MODEL_NAME, first_usable_model, is_auto_model, model_unusable_reason,
    )

    row = models.get(str(tmpl.get("model_main_id") or ""))
    name = (row or {}).get("name") or (row or {}).get("model_string")
    reason = "the template's model no longer exists" if row is None else (
        None if is_auto_model(row) else model_unusable_reason(row))
    if row is not None and not is_auto_model(row) and reason is None:
        return {"auto": False, "label": f"The template's model: {name}",
                "template_model": name, "reason": None}
    picked = first_usable_model([m for m in models.values() if m.get("is_active") is not False],
                                environment)
    now = (picked.get("name") or picked.get("model_string")) if picked else None
    label = f"{AUTO_MODEL_NAME}: " + (f"now {now}" if now else "none usable yet")
    if row is not None and not is_auto_model(row):
        label += f" ({name} can't run here: {reason})"
    return {"auto": True, "label": label, "template_model": name, "reason": reason}


def _replace_unusable_models(client, created: List[str], environment: str = "staging") -> None:
    """Point each new agent whose (template's) model cannot run on this
    install at the Automatic row instead."""
    from admin.seed_demo_agent import ensure_auto_model

    from core.model_resolver import is_auto_model

    models = _model_rows(client)
    if models is None:
        return
    auto_id: Optional[str] = None
    for created_id in created:
        row = _rows(client.table("agent_profiles").select("profile_id, model_main_id")
                    .eq("profile_id", created_id).limit(1).execute())
        if not row or not row[0].get("model_main_id") \
                or is_auto_model(models.get(str(row[0]["model_main_id"]))):
            continue
        if not default_model_choice(row[0], models, environment)["auto"]:
            continue
        if auto_id is None:
            auto_id = ensure_auto_model(client, environment)
        client.table("agent_profiles").update({"model_main_id": auto_id}) \
            .eq("profile_id", created_id).execute()


def list_templates(client) -> List[Dict[str, Any]]:
    rows = _rows(client.table("agent_profiles").select("*")
                 .eq("status", "template").eq("environment", "staging").execute())
    served = _served_kinds()
    if served is not None:
        rows = [r for r in rows if r.get("agent_kind") in served]
    # Where the generic agents API is on, an agent made from a template is a
    # generic one (see use_template), so the kind the template happens to be
    # stored under says nothing about what you get.
    from endpoints.agents_v1 import generic_agents_enabled
    makes_generic = generic_agents_enabled()
    models = _model_rows(client)
    out = []
    for r in rows:
        out.append({
            "default_model": default_model_choice(r, models) if models is not None else None,
            "template_id": r["profile_id"], "slug": r.get("slug"),
            "display_name": r.get("display_name"),
            "agent_kind": "" if makes_generic else r.get("agent_kind"),
            "description": r.get("description"),
            "is_composite": bool(r.get("downstream_profile_ids")),
            # What the template brings along (the gallery's "Includes" line).
            "sub_agent_count": len(r.get("downstream_profile_ids") or []),
            "knowledge_count": len(r.get("data_source_ids") or []),
        })
    return out


def slug_from_name(client, display_name: str) -> str:
    """A readable, unused slug for a new generic agent: "Support desk" ->
    `support-desk` (then `support-desk-2`, ...).

    A generic agent's slug is public: it is its address
    (/v1/agents/<slug>/chat) and its hand-off name (ask_<slug>). Left to the
    fork's own default it came out as `mold:blank-fork-21bd4a6a`. A name with
    no Latin letters or digits (an Arabic name, say) gets `agent-<n>`.
    """
    import re

    base = re.sub(r"[^a-z0-9]+", "-", (display_name or "").lower()).strip("-")[:48].strip("-")
    if not base or not base[0].isalpha():
        base = f"agent-{base}".strip("-") if base else "agent"
    taken = {r.get("slug") for r in _rows(
        client.table("agent_profiles").select("slug").eq("environment", "staging").execute())}
    if base not in taken and base != "agent":
        return base
    n = 2 if base != "agent" else 1
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def use_template(*, template_id: str, display_name: str, slug: Optional[str] = None,
                 as_subagent_of: Optional[str] = None, model_id: Optional[str] = None,
                 agent_type: Optional[str] = None,
                 actor_user_id: str) -> Dict[str, Any]:
    from core.auth import admin_service_client
    from endpoints.agents_v1 import generic_agents_enabled
    client = admin_service_client()
    makes_generic = generic_agents_enabled()
    if model_id:
        # Checked before anything is written: the operation is all-or-nothing.
        found = _rows(client.table("agent_models").select("model_id, is_active, extra_config")
                      .eq("model_id", model_id).limit(1).execute())
        if not found or not found[0].get("is_active") \
                or (found[0].get("extra_config") or {}).get("realtime"):
            raise ProfileNotFoundError(f"model {model_id} is not an active chat model")

    # The category (slug or id) for the new agent. Resolved before anything
    # is written, like the model: an unknown one refuses the whole operation
    # (AgentTypeNotFoundError) instead of creating an uncategorised agent.
    agent_type_id = None
    if agent_type:
        from admin.services.agent_types import resolve_type_id
        agent_type_id = resolve_type_id(agent_type, client)

    tmpl = _rows(client.table("agent_profiles").select("*")
                 .eq("profile_id", template_id).limit(1).execute())
    if not tmpl or tmpl[0].get("status") != "template":
        raise TemplateNotFoundError(f"not a template: {template_id}")
    tmpl = tmpl[0]
    served = _served_kinds()
    if served is not None and tmpl.get("agent_kind") not in served:
        raise TemplateNotFoundError(
            f"this build doesn't run {tmpl.get('agent_kind')!r} agents, so an "
            "agent made from this template couldn't answer here")

    # Resolve the as_subagent_of parent (if any) BEFORE forking anything —
    # both failure modes above and below must raise before any write, so
    # this whole operation stays all-or-nothing.
    parent = None
    if as_subagent_of:
        prows = _rows(client.table("agent_profiles").select("*")
                      .eq("profile_id", as_subagent_of).limit(1).execute())
        if not prows:
            raise ProfileNotFoundError(f"as_subagent_of parent not found: {as_subagent_of}")
        parent = prows[0]
        if parent.get("environment") != "staging":
            raise ProfileNotFoundError(
                f"as_subagent_of parent {as_subagent_of} is not in staging")

    created: List[str] = []

    # Fork any sub-agent templates first (composite molds), slug->new id.
    sub_map: Dict[str, str] = {}
    for sub_id in (tmpl.get("downstream_profile_ids") or []):
        sub_rows = _rows(client.table("agent_profiles").select("*")
                         .eq("profile_id", sub_id).limit(1).execute())
        if sub_rows and sub_rows[0].get("status") == "template":
            sub = sub_rows[0]
            sub_name = f"{display_name} — {sub.get('display_name') or 'sub-agent'}"
            forked = fork_profile_row(
                profile_id=sub_id, actor_user_id=actor_user_id, display_name=sub_name,
                **({"slug": slug_from_name(client, sub_name), "exact_slug": True}
                   if makes_generic else {}))
            sub_map[sub_id] = forked["profile_id"]
            created.append(forked["profile_id"])

    # Fork the root mold with the caller's name. An explicit `slug` is
    # honored verbatim (exact_slug) — a mold is instantiated into a named
    # agent, and callers that wire delegation need a predictable slug so the
    # generated `ask_<slug>` tool name is stable. Without an explicit slug we
    # keep the uniqueness suffix (the mold's own slug would otherwise collide).
    if not slug and makes_generic:
        slug = slug_from_name(client, display_name)
    root = fork_profile_row(profile_id=template_id, actor_user_id=actor_user_id,
                            display_name=display_name, slug=slug,
                            exact_slug=bool(slug))
    created.insert(0, root["profile_id"])

    # Where the generic agents API is on, an agent made from a template is a
    # generic (`custom`) agent: it runs on runtime/agents/custom.py and is
    # served at /v1/agents/{slug}/chat with no code of its own. The molds are
    # stored under their historical kind, and with the flag off the fork keeps
    # that kind exactly as before.
    if model_id:
        for created_id in created:
            client.table("agent_profiles").update(
                {"model_main_id": model_id}).eq("profile_id", created_id).execute()
    else:
        # No model picked: keep the template's model only where it can run on
        # this install (a key for its provider, or a local one); otherwise
        # start on the Automatic row, which the New agent window says.
        _replace_unusable_models(client, created)

    if agent_type_id:
        # The new agent and the sub-agents made with it are one team: they
        # start in the same category.
        for created_id in created:
            client.table("agent_profiles").update(
                {"agent_type_id": agent_type_id}).eq("profile_id", created_id).execute()

    if makes_generic:
        from runtime.agents.custom import CUSTOM_AGENT_KIND
        from admin.services import custom_prompts
        for created_id in created:
            changes: Dict[str, Any] = {"agent_kind": CUSTOM_AGENT_KIND}
            if created_id == root["profile_id"]:
                # The copied description describes the TEMPLATE ("Minimal
                # baseline: ... Start here to build up"), and the agents list
                # showed it under every new agent's name. A sub-agent keeps
                # its own: that text is what the parent reads to decide when
                # to hand off.
                changes["description"] = None
            client.table("agent_profiles").update(changes).eq("profile_id", created_id).execute()
            # Each new agent owns its instructions from the start: copy the
            # template's text into the agent's own prompt and move the pin,
            # so an edit never lands in the template's history.
            try:
                fresh = _rows(client.table("agent_profiles").select("*")
                              .eq("profile_id", created_id).limit(1).execute())
                if fresh:
                    custom_prompts.ensure_own_prompt(client, fresh[0], actor_user_id=actor_user_id)
            except Exception as exc:  # noqa: BLE001 — the save route creates it on first edit
                logger.warning("templates: own prompt for %s not created (%s)", created_id, exc)

    # Rewire the root draft's downstream to the NEW sub-agent drafts.
    if sub_map:
        new_downstream = [sub_map.get(s, s) for s in (tmpl.get("downstream_profile_ids") or [])]
        client.table("agent_profiles").update(
            {"downstream_profile_ids": new_downstream}
        ).eq("profile_id", root["profile_id"]).execute()

    # Optionally wire the new root as a subagent of an existing profile
    # (parent already resolved and validated at the top of this function).
    if as_subagent_of:
        ds = list(parent.get("downstream_profile_ids") or [])
        if root["profile_id"] not in ds:
            ds.append(root["profile_id"])
            client.table("agent_profiles").update({"downstream_profile_ids": ds}) \
                .eq("profile_id", as_subagent_of).execute()
            invalidate_profile_loader(parent.get("agent_kind") or "",
                                      parent.get("environment", "staging"))

    record_change(entity_type="agent_profiles", entity_id=root["profile_id"],
                  action="use_template", actor_user_id=actor_user_id,
                  after={"template": tmpl.get("slug"), "created": created,
                         "as_subagent_of": as_subagent_of,
                         **({"agent_type_id": agent_type_id} if agent_type_id else {})})
    return {"root": root["profile_id"], "created": created, "as_subagent_of": as_subagent_of,
            **({"agent_type_id": agent_type_id} if agent_type_id else {})}
