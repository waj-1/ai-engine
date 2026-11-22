"""A generic (`custom`) agent's own instructions.

Every other agent kind has ONE set of prompt slots (`agent_prompts` rows keyed
by `(agent_kind, slug)`: system, title, …) that all profiles of the kind share,
each profile pinning the version it serves. That model cannot hold for
`custom`: there are many custom agents and each has different instructions, so
a shared `(custom, system)` row would interleave every agent's history and an
unpinned agent would be shown another agent's text.

So each custom agent owns its prompt row, slugged `<agent slug>:<key>`
(`helpdesk:system`). The profile's pin key stays the plain key (`system`),
which is all the runtime reads — it resolves `prompt_version_ids["system"]` by
version id and never looks a prompt up by slug.

A custom agent made from a template starts pinned to the TEMPLATE's prompt
version. `ensure_own_prompt` moves it onto its own row: it copies the pinned
text in as version 1 and repoints the pin, so the template's history is never
written to by an agent's edits.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

CUSTOM_AGENT_KIND = "custom"
SYSTEM_KEY = "system"


def own_prompt_slug(profile_slug: str, key: str = SYSTEM_KEY) -> str:
    return f"{profile_slug}:{key}"


def _first(resp: Any) -> Optional[Dict[str, Any]]:
    data = getattr(resp, "data", None) or []
    return data[0] if data else None


def find_own_prompt(client, profile: Dict[str, Any], key: str = SYSTEM_KEY) -> Optional[Dict[str, Any]]:
    """The agent's own prompt row for `key`, or None. Read-only."""
    return _first(
        client.table("agent_prompts").select("*")
        .eq("agent_kind", CUSTOM_AGENT_KIND)
        .eq("slug", own_prompt_slug(profile.get("slug") or "", key))
        .limit(1).execute()
    )


def pinned_version(client, profile: Dict[str, Any], key: str = SYSTEM_KEY) -> Optional[Dict[str, Any]]:
    """The `agent_prompt_versions` row this profile's pin for `key` points at."""
    version_id = (profile.get("prompt_version_ids") or {}).get(key)
    if not version_id:
        return None
    return _first(
        client.table("agent_prompt_versions")
        .select("prompt_version_id, prompt_id, content, version_number")
        .eq("prompt_version_id", version_id).limit(1).execute()
    )


def ensure_own_prompt(client, profile: Dict[str, Any], *, key: str = SYSTEM_KEY,
                      actor_user_id: Optional[str] = None) -> Dict[str, Any]:
    """Return the agent's own prompt row for `key`, creating it if needed.

    On creation, text the agent is currently pinned to in ANOTHER prompt's
    history (its template's) is copied in as version 1 and the pin is moved
    onto the copy. Idempotent: an agent that already owns its prompt is
    returned untouched. Raises ValueError for a profile that is not a custom
    agent — other kinds share their kind's prompt slots by design.
    """
    if profile.get("agent_kind") != CUSTOM_AGENT_KIND:
        raise ValueError("only a custom agent owns its prompt")
    if not profile.get("slug") or not profile.get("profile_id"):
        raise ValueError("profile needs a slug and a profile_id")
    existing = find_own_prompt(client, profile, key)
    if existing:
        return existing

    prompt = _first(client.table("agent_prompts").insert({
        "agent_kind": CUSTOM_AGENT_KIND,
        "slug": own_prompt_slug(profile["slug"], key),
        "description": f"Instructions for {profile.get('display_name') or profile['slug']}",
        "created_by": actor_user_id,
    }).execute())
    if not prompt:
        raise RuntimeError("could not create the agent's prompt")

    inherited = pinned_version(client, profile, key)
    if inherited and (inherited.get("content") or "").strip():
        version = _first(client.table("agent_prompt_versions").insert({
            "prompt_id": prompt["prompt_id"],
            "version_number": 1,
            "content": inherited["content"],
            "notes": "Starting text, copied from the template",
            "created_by": actor_user_id,
        }).execute())
        if version and version.get("prompt_version_id"):
            pins = dict(profile.get("prompt_version_ids") or {})
            pins[key] = version["prompt_version_id"]
            client.table("agent_profiles").update(
                {"prompt_version_ids": pins}
            ).eq("profile_id", profile["profile_id"]).execute()
            profile["prompt_version_ids"] = pins
    return prompt


def instructions_view(client, profile: Dict[str, Any]) -> Dict[str, Any]:
    """The Instructions tab's entry for a custom agent's system prompt.

    Read-only. Shaped like the rows `load_agents_context` builds for other
    kinds (`slug` is the pin key, so the tab's `slug == "system"` lookup finds
    it), plus `post_url`: saves go to the agent-scoped route, which creates the
    agent's own prompt on first save. Before that first save the text shown is
    whatever the agent is pinned to (its template's), and `prompt_id` is None.
    """
    own = find_own_prompt(client, profile)
    shown = pinned_version(client, profile)
    if own and shown and shown.get("prompt_id") != own["prompt_id"]:
        shown = None  # a pin into another prompt's history: not this agent's text
    latest_number = None
    if own:
        latest = _first(
            client.table("agent_prompt_versions").select("version_number, content")
            .eq("prompt_id", own["prompt_id"]).order("version_number", desc=True).limit(1).execute()
        )
        latest_number = (latest or {}).get("version_number")
        if shown is None:
            shown = latest
    return {
        "prompt_id": (own or {}).get("prompt_id"),
        "slug": SYSTEM_KEY,
        "agent_kind": CUSTOM_AGENT_KIND,
        "content": (shown or {}).get("content") or "",
        "version_number": (shown or {}).get("version_number") if own else None,
        "latest_version_number": latest_number,
        "post_url": f"/admin/agents/{profile['profile_id']}/instructions",
    }
