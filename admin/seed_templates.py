"""Seed the protected "architecture mold" template catalog.

Insert-only + idempotent (keyed on (agent_kind, slug, environment)); runs in
the lifespan AFTER seed_agent_models. Templates are agent_profiles rows with
status='template', environment='staging' — inert at runtime (the loader only
ever serves status='active'). Composite molds (orchestrator, data-analyst)
reference their sub-agent molds via downstream_profile_ids, wired in a
second pass once every mold's profile_id is known.

Each mold gets its own versioned prompt lineage: an agent_prompts row keyed
on (agent_kind=TEMPLATE_AGENT_KIND, slug=<mold slug>) plus a v1 agent_prompt_versions
row, mirroring admin.seed_prompts. (agent_prompt_versions.prompt_id is a
real FK to agent_prompts — it is NOT the mold's own catalog slug.)

Tool/KB/MCP attachments stay empty for v1 EXCEPT mold:rag, which resolves
`search_documentation` to its `agent_tools.tool_id` by python_name. If that
tool row isn't present yet (e.g. sync_tools hasn't run), it degrades to an
empty tool_ids list plus a warning rather than failing the mold. Users wire
up everything else via the connect suite after forking a mold.

agent_profiles.model_main_id is NOT NULL + FK'd to agent_models. This
seeder runs AFTER seed_agent_models in the lifespan chain, so a default
staging model is already resolvable when a mold's insert needs one — even
on a completely fresh database's very first boot.

Best-effort throughout: a single mold failing to seed never blocks boot.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _template_agent_kind() -> str:
    """The agent kind the molds are stored under.

    A mold is not an agent of any kind: an agent made from one is re-kinded
    on fork (admin/services/templates.py), so the engine stores its molds
    under the generic `custom` kind. A deployment whose database already
    holds its molds under another kind names it through its plugin
    (`agent_catalog.template_agent_kind()`), so this insert-only seeder keeps
    finding them instead of seeding a second set.
    """
    from core.plugins import call_hook, load_hook

    for kind in call_hook(load_hook("agent_catalog"), "template_agent_kind"):
        if kind:
            return kind
    return "custom"


TEMPLATE_AGENT_KIND: str = _template_agent_kind()

# One prompt lineage per mold; short starter prompts. `downstream` lists the
# slugs of sub-agent molds a composite mold delegates to (wired in pass 2).
TEMPLATE_CATALOG: List[Dict[str, Any]] = [
    {"slug": "mold:blank", "display_name": "Blank Tool-Caller",
     "description": "Minimal baseline: a plain system prompt and one HTTP tool. Start here to build up.",
     "prompt": "You are a helpful assistant. Use your tools when they apply; otherwise answer directly.",
     "agent_kind": TEMPLATE_AGENT_KIND, "status": "template", "environment": "staging",
     "tool_names": [], "kb_names": [], "mcp_names": [], "downstream": []},
    {"slug": "mold:rag", "display_name": "RAG / Knowledge Agent",
     "description": "Retrieval-grounded: answers from an attached knowledge base via search_documentation.",
     "prompt": "You answer strictly from the attached knowledge base. Cite the source; if it isn't covered, say so.",
     "agent_kind": TEMPLATE_AGENT_KIND, "status": "template", "environment": "staging",
     "tool_names": ["search_documentation"], "kb_names": [], "mcp_names": [], "downstream": []},
    {"slug": "mold:worker-subagent", "display_name": "Worker Sub-agent",
     "description": "A leaf worker a parent can hand off to. Minimal tool set.",
     "prompt": "You are a focused worker sub-agent. Complete the delegated task and return a concise result.",
     "agent_kind": TEMPLATE_AGENT_KIND, "status": "template", "environment": "staging",
     "tool_names": [], "kb_names": [], "mcp_names": [], "downstream": []},
    {"slug": "mold:orchestrator", "display_name": "Orchestrator + Sub-agents",
     "description": "Routing / hand-off: a parent that delegates to one or more worker sub-agents.",
     "prompt": "You route each request to the right sub-agent and synthesize their results. Delegate; don't do the work yourself.",
     "agent_kind": TEMPLATE_AGENT_KIND, "status": "template", "environment": "staging",
     "tool_names": [], "kb_names": [], "mcp_names": [], "downstream": ["mold:worker-subagent"]},
    {"slug": "mold:chart-subagent", "display_name": "Chart Sub-agent",
     "description": "Renders a Vega-Lite chart from a data question. Used by the Data Analyst mold.",
     "prompt": "You turn a data question + rows into a single Vega-Lite spec. Return only the spec.",
     "agent_kind": TEMPLATE_AGENT_KIND, "status": "template", "environment": "staging",
     "tool_names": [], "kb_names": [], "mcp_names": [], "downstream": []},
    {"slug": "mold:data-analyst", "display_name": "Data Analyst + Charts",
     "description": "Analytics over business data (reporting/CSV tools) with a Vega-Lite chart sub-agent.",
     "prompt": "You answer analytical questions over the business's data. Pull the numbers with your tools, then hand charting to your chart sub-agent.",
     "agent_kind": TEMPLATE_AGENT_KIND, "status": "template", "environment": "staging",
     "tool_names": [], "kb_names": [], "mcp_names": [], "downstream": ["mold:chart-subagent"]},
    {"slug": "mold:mcp-connected", "display_name": "MCP-Connected Agent",
     "description": "Integration: tools resolved from an attached external MCP server.",
     "prompt": "You act through the tools exposed by the connected MCP server. Confirm inputs before write actions.",
     "agent_kind": TEMPLATE_AGENT_KIND, "status": "template", "environment": "staging",
     "tool_names": [], "kb_names": [], "mcp_names": [], "downstream": []},
]


def _existing(client) -> Dict[str, Dict[str, Any]]:
    """Currently-seeded mold rows, keyed by slug."""
    rows = (client.table("agent_profiles")
            .select("profile_id, slug, agent_kind, environment, downstream_profile_ids")
            .eq("agent_kind", TEMPLATE_AGENT_KIND).eq("environment", "staging")
            .eq("status", "template").execute().data or [])
    return {r["slug"]: r for r in rows}


def _default_model_id(client) -> Optional[str]:
    """The default model for freshly-minted (and repaired) mold rows.

    Ordered by ``created_at`` ASC — the OLDEST active staging model, i.e. the
    long-established fleet default.

    It used to order by ``name``, which is volatile in exactly the wrong way:
    the winner changes the moment somebody adds a model whose name happens to
    sort earlier. That is not theoretical. Ordering by name is how every mold
    came to point at "Claude Haiku 4.5" (an `anthropic_vertex` row that cannot
    build without GCP ADC), and then — once that row was deactivated and the
    repair pass ran — how they all jumped onto a newly-added
    "Azure GPT-5-mini …" row, which only works where AZURE_OPENAI_* is
    configured. A default that reshuffles whenever the catalog grows is a
    latent breakage generator.

    ``created_at`` is stable: adding a model never steals the default, and the
    established one keeps winning until it is explicitly deactivated.

    See module docstring: this can legitimately be empty on a brand-new
    database's first-ever boot; callers must tolerate None.
    """
    rows = (client.table("agent_models").select("model_id")
            .eq("environment", "staging").eq("is_active", True)
            .order("created_at", desc=False).limit(1).execute().data or [])
    return rows[0]["model_id"] if rows else None


def _repair_stale_model_refs(client, model_id: Optional[str]) -> int:
    """Repoint molds whose ``model_main_id`` references a missing or INACTIVE model.

    ``_default_model_id`` picks "first active staging model **ordered by name**",
    so each mold inherits whatever happened to sort first the day it was created —
    and that pointer then never updates, because this seeder is insert-only.

    When such a model is later deactivated the mold silently keeps referencing it,
    and an agent **forked** from that mold inherits the dead reference and fails at
    build time. That is not hypothetical: the three `anthropic_vertex` Claude rows
    were retired for the Azure migration (they mint a token via GCP Application
    Default Credentials, which does not exist off-GCP), and "Claude Haiku 4.5"
    sorted early enough that all seven molds were pointing at it.

    Templates are read-only through the admin API and MCP by design
    ("templates are read-only — fork one with templates(action='use')"), so this
    seeder is the only supported place to repair them.

    Idempotent: a no-op once every mold references an active model.
    """
    if not model_id:
        return 0
    try:
        molds = (client.table("agent_profiles")
                 .select("profile_id, slug, model_main_id")
                 .eq("agent_kind", TEMPLATE_AGENT_KIND).eq("environment", "staging")
                 .eq("status", "template").execute().data or [])
        if not molds:
            return 0
        active = {
            r["model_id"] for r in (
                client.table("agent_models").select("model_id")
                .eq("is_active", True).execute().data or []
            )
        }
    except Exception as exc:
        logger.warning("seed_templates: stale-model check failed (%s)", exc)
        return 0

    repaired = 0
    for m in molds:
        if m.get("model_main_id") in active:
            continue
        try:
            (client.table("agent_profiles").update({"model_main_id": model_id})
             .eq("profile_id", m["profile_id"]).execute())
            logger.info(
                "seed_templates: repointed mold %s off stale model %s -> %s",
                m.get("slug"), m.get("model_main_id"), model_id,
            )
            repaired += 1
        except Exception as exc:
            logger.warning("seed_templates: repair of %s failed (%s)", m.get("slug"), exc)
    return repaired


def _ensure_prompt(client, agent_kind: str, slug: str, description: str, content: str) -> str:
    """Insert-only an agent_prompts row (agent_kind, slug) + its v1 content;
    return the prompt_version_id. Mirrors admin.seed_prompts's
    _upsert_prompt / _prompt_has_any_version, but generates the primary
    keys client-side (rather than relying on the insert response echoing
    DB-generated defaults) so the write is unambiguous either way."""
    existing_prompt = (
        client.table("agent_prompts").select("prompt_id")
        .eq("agent_kind", agent_kind).eq("slug", slug)
        .limit(1).execute().data or []
    )
    if existing_prompt:
        prompt_id = existing_prompt[0]["prompt_id"]
    else:
        prompt_id = str(uuid.uuid4())
        client.table("agent_prompts").insert({
            "prompt_id": prompt_id, "agent_kind": agent_kind, "slug": slug,
            "description": description,
        }).execute()

    have = (
        client.table("agent_prompt_versions").select("prompt_version_id")
        .eq("prompt_id", prompt_id).limit(1).execute().data or []
    )
    if have:
        return have[0]["prompt_version_id"]

    prompt_version_id = str(uuid.uuid4())
    client.table("agent_prompt_versions").insert({
        "prompt_version_id": prompt_version_id, "prompt_id": prompt_id,
        "version_number": 1, "content": content,
        "notes": "template mold starter",
    }).execute()
    return prompt_version_id


def _tool_ids_for(client, mold: Dict[str, Any]) -> List[str]:
    """Resolve `tool_names` to agent_tools.tool_id by python_name. Missing
    tools degrade to being skipped (empty-ish result) + a warning, never a
    hard failure — v1 only wires mold:rag's search_documentation this way."""
    tool_ids: List[str] = []
    for name in mold.get("tool_names") or []:
        rows = (
            client.table("agent_tools").select("tool_id")
            .eq("python_name", name).eq("is_deprecated", False)
            .limit(1).execute().data or []
        )
        if rows:
            tool_ids.append(rows[0]["tool_id"])
        else:
            logger.warning(
                "seed_templates: tool %r not found for %s; leaving unwired", name, mold["slug"]
            )
    return tool_ids


def _repair_backfilled_attachments(client) -> int:
    """Put molds back to the tools and knowledge their catalog entry names.

    Until 2026-11-22 `admin/seed_agent_profiles.py` filled every empty array
    on every profile of a kind, molds included, so each mold picked up the
    kind's whole tool set and knowledge sources on the boot after it was
    seeded, and every agent made from it inherited them. That backfill now
    skips templates; this corrects the rows it already changed. Molds are
    read-only in the portal, so nothing an operator set is lost. Returns the
    number of molds corrected.
    """
    by_slug = {m["slug"]: m for m in TEMPLATE_CATALOG}
    try:
        molds = (client.table("agent_profiles")
                 .select("profile_id, slug, tool_ids, data_source_ids")
                 .eq("agent_kind", TEMPLATE_AGENT_KIND).eq("environment", "staging")
                 .eq("status", "template").execute().data or [])
    except Exception as exc:
        logger.warning("seed_templates: could not read molds to repair (%s)", exc)
        return 0
    fixed = 0
    for row in molds:
        mold = by_slug.get(row.get("slug"))
        if mold is None:
            continue
        patch: Dict[str, Any] = {}
        if not mold["kb_names"] and (row.get("data_source_ids") or []):
            patch["data_source_ids"] = []
        want_tools = _tool_ids_for(client, mold)
        if sorted(row.get("tool_ids") or []) != sorted(want_tools):
            patch["tool_ids"] = want_tools
        if not patch:
            continue
        try:
            client.table("agent_profiles").update(patch).eq("profile_id", row["profile_id"]).execute()
            fixed += 1
        except Exception as exc:
            logger.warning("seed_templates: could not repair %s (%s)", row.get("slug"), exc)
    if fixed:
        logger.info("seed_templates: reset attachments on %d mold(s) to their catalog entry", fixed)
    return fixed


def run() -> Dict[str, int]:
    from core.auth import get_service_client
    client = get_service_client()
    _repair_backfilled_attachments(client)
    existing = _existing(client)
    slug_to_id: Dict[str, str] = {s: r["profile_id"] for s, r in existing.items()}
    try:
        model_id = _default_model_id(client)
    except Exception as exc:
        logger.warning("seed_templates: failed to resolve default model_id: %s", exc)
        model_id = None
    created = 0

    # Repair existing molds pointing at a model that has since been deactivated.
    # Must run even when nothing new is inserted — this seeder is insert-only, so
    # without it a stale pointer is never corrected. See _repair_stale_model_refs.
    repaired = _repair_stale_model_refs(client, model_id)

    # Pass 1: create rows that don't exist yet (downstream wired in pass 2).
    for mold in TEMPLATE_CATALOG:
        slug = mold["slug"]
        if slug in slug_to_id:
            continue
        try:
            pv = _ensure_prompt(client, mold["agent_kind"], slug, mold["description"], mold["prompt"])
            tool_ids = _tool_ids_for(client, mold)
            pid = str(uuid.uuid4())
            row = {
                "profile_id": pid, "agent_kind": mold["agent_kind"], "slug": slug,
                "environment": mold["environment"], "status": mold["status"], "is_active": False,
                "display_name": mold["display_name"], "description": mold["description"],
                "model_main_id": model_id,
                "prompt_version_ids": {"system": pv},
                "tool_ids": tool_ids, "data_source_ids": [], "mcp_server_ids": [],
                "downstream_profile_ids": [],
            }
            client.table("agent_profiles").insert(row).execute()
            slug_to_id[slug] = pid
            created += 1
        except Exception as exc:
            logger.warning("seed_templates: %s failed (%s)", slug, exc)

    # Pass 2: wire composite downstream_profile_ids by slug -> id.
    wired = 0
    for mold in TEMPLATE_CATALOG:
        if not mold["downstream"]:
            continue
        pid = slug_to_id.get(mold["slug"])
        want = [slug_to_id[s] for s in mold["downstream"] if s in slug_to_id]
        cur = existing.get(mold["slug"], {}).get("downstream_profile_ids") or []
        if pid and want and want != cur:
            try:
                client.table("agent_profiles").update(
                    {"downstream_profile_ids": want}).eq("profile_id", pid).execute()
                wired += 1
            except Exception as exc:
                logger.warning("seed_templates: wire %s failed (%s)", mold["slug"], exc)

    logger.info(
        "seed_templates: %d created, %d composites wired, %d stale model refs repaired",
        created, wired, repaired,
    )
    return {"created": created, "wired": wired, "repaired": repaired}
