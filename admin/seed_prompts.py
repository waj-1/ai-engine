"""Seed agent_prompts + agent_prompt_versions from the four agents' prompts.py.

Called once at FastAPI startup. Idempotent: a slug whose latest version is
v1 with matching content is left untouched. A new version is only inserted
when no version row exists yet for the (agent_kind, slug) pair.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from core.auth import get_service_client

logger = logging.getLogger(__name__)

from core.deployment import active_scope, all_scope_kinds, scope_kinds

# Every agent kind owned by ANY deployment scope (core/deployment.py). A full
# build seeds none of these; a scoped build seeds only the active scope's.
_ALL_SCOPE_KINDS = all_scope_kinds()


# (agent_kind, slug, description, callable returning the string content)
def _prompt_specs() -> List[Tuple[str, str, str, callable]]:
    """Every prompt slot to seed: `(agent_kind, slug, description, content_fn)`.

    The engine itself ships none. A product's prompts come from its plugin's
    `prompt_seeds` hook (`<plugin>.prompt_seeds.prompt_specs()`, see
    core/plugins.py), in plugin order; a generic (`custom`) agent owns its
    prompt and the demo agents seed their own.
    """
    from core.plugins import call_hook, load_hook

    specs: List[Tuple[str, str, str, callable]] = []
    for found in call_hook(load_hook("prompt_seeds"), "prompt_specs"):
        specs.extend(found)
    return specs


def run() -> Dict[str, int]:
    """Returns counts of inserted prompts and versions."""
    client = get_service_client()
    prompts_inserted = 0
    versions_inserted = 0
    specs = _prompt_specs()
    # Two-way gate (mirrors seed_eval_suites.run()): a scoped build seeds
    # ONLY the active client's prompt(s) — including its own sub-agents
    # (<key>_title / <key>_suggestions) — while a normal WAJ build seeds
    # everything EXCEPT the union of ALL deployment scopes' kinds, so no
    # client's prompt rows ever land in the shared WAJ DB (including a
    # scope whose specs do not exist yet). Existing rows of the other flavour are left untouched
    # either way.
    scope = active_scope()
    if scope is not None:
        keep = scope_kinds(scope)
        specs = [s for s in specs if s[0] in keep]
    else:
        specs = [s for s in specs if s[0] not in _ALL_SCOPE_KINDS]
    for agent_kind, slug, description, content_fn in specs:
        prompt_id = _upsert_prompt(client, agent_kind, slug, description)
        if prompt_id is None:
            continue
        if _prompt_has_any_version(client, prompt_id):
            continue
        try:
            content = content_fn()
        except Exception as exc:
            logger.warning("seed_prompts: skipping %s/%s (%s)", agent_kind, slug, exc)
            continue
        client.table("agent_prompt_versions").insert({
            "prompt_id": prompt_id,
            "version_number": 1,
            "content": content,
            "notes": "Seeded from file-system prompts.py at boot.",
        }).execute()
        versions_inserted += 1
    return {"prompts": prompts_inserted, "versions": versions_inserted}


def _upsert_prompt(client, agent_kind: str, slug: str, description: str) -> Optional[str]:
    existing = (
        client.table("agent_prompts").select("prompt_id")
        .eq("agent_kind", agent_kind).eq("slug", slug)
        .limit(1).execute().data or []
    )
    if existing:
        return existing[0]["prompt_id"]
    inserted = client.table("agent_prompts").insert({
        "agent_kind": agent_kind, "slug": slug, "description": description,
    }).execute()
    return inserted.data[0]["prompt_id"] if inserted.data else None


def _prompt_has_any_version(client, prompt_id: str) -> bool:
    rows = (
        client.table("agent_prompt_versions").select("prompt_version_id")
        .eq("prompt_id", prompt_id).limit(1).execute().data or []
    )
    return bool(rows)
