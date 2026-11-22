"""
Tool registry — single index of every Python function that can be exposed
to an LLM via an agent profile.

Inspired by n8n's filesystem node-discovery pattern (packages/cli/src/
load-nodes-and-credentials.ts) but with a DB-backed mirror so the admin
UI can paginate/filter without scanning code.

Flow:
1. At import time, agent modules call `register_tool(...)` for each tool
   they expose (decorator-style).
2. The registry holds a process-local dict {python_name: ToolSpec}.
3. The admin portal calls `sync_to_db()` to upsert the registry rows into
   `agent_tools`, marking missing rows `is_deprecated = true`.
4. At agent run time, tools are resolved by
   `runtime.tools._mcp_resolver.resolve_mcp_tools_for_profile`: the agent's
   factory output is a pool and the profile's MCP attachments decide what
   binds. If the profile is fallback (no DB row), the full pool is served.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Set

from core.agent_profile import Profile
from core.auth import get_service_client

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolSpec:
    python_name: str
    display_name: str
    description: str
    category: str
    scope: str  # "read" | "write"
    source_file: str
    argument_schema: Dict[str, Any] = field(default_factory=dict)
    agent_kind: Optional[str] = None
    # Other agent kinds that also expose this same tool. A tool exposed by
    # several agents (e.g. search_documentation, built by two agents'
    # factories) is assigned to every one of them — see
    # seed_agent_profiles._collect_tool_ids, which matches this column too.
    additional_agent_kinds: tuple = ()


_REGISTRY: Dict[str, ToolSpec] = {}


def register_tool(spec: ToolSpec) -> None:
    """Add a tool spec to the in-memory registry.

    Idempotent: re-registering the same name overrides the previous spec,
    which makes hot-reload during development behave sensibly.

    When the same tool name is registered under a different ``agent_kind``
    (two agents' factories both build a ``search_documentation`` tool), the
    kinds are accumulated rather than clobbered: the latest
    registration stays primary and the earlier kind is folded into
    ``additional_agent_kinds`` so the tool is assigned to every agent that
    exposes it.
    """
    existing = _REGISTRY.get(spec.python_name)
    if existing is not None and existing != spec:
        logger.debug("tool_registry: overriding %s", spec.python_name)
        # Union every kind this tool has been registered under (including any
        # already accumulated), keeping the latest registration's agent_kind
        # primary. This survives an idempotent re-register under the same kind.
        prior_kinds = (existing.agent_kind, *existing.additional_agent_kinds)
        if spec.agent_kind and any(k and k != spec.agent_kind for k in prior_kinds):
            merged = list(spec.additional_agent_kinds)
            for kind in prior_kinds:
                if kind and kind != spec.agent_kind and kind not in merged:
                    merged.append(kind)
            spec = replace(spec, additional_agent_kinds=tuple(merged))
    _REGISTRY[spec.python_name] = spec


def get_spec(python_name: str) -> Optional[ToolSpec]:
    return _REGISTRY.get(python_name)


def all_specs() -> List[ToolSpec]:
    return list(_REGISTRY.values())


def sync_to_db(scope_kinds: Optional[Set[str]] = None) -> Dict[str, int]:
    """Push the in-memory registry into `agent_tools`.

    Returns a small counts dict so the admin UI can show "synced N tools,
    M deprecated". Uses the service client (superuser ops only).

    `scope_kinds` is the set of agent kinds THIS build actually scanned. When
    given, deprecation is restricted to those kinds so a build never flags
    another build's tools (see the deprecation block below). `None` keeps the
    legacy behaviour of deprecating anything missing, for callers that really
    do have the whole registry.
    """
    client = get_service_client()
    specs = all_specs()
    inserted = 0
    updated = 0

    # `kind` column was added in migration 20260520140000. Old rows
    # default to 'python'. HTTP tools (kind='http') are authored from
    # the admin UI — they have no Python source so they MUST NOT be
    # flagged deprecated every boot.
    existing_rows = (
        client.table("agent_tools")
        .select("python_name, kind, agent_kind, additional_agent_kinds")
        .execute().data or []
    )
    existing_names = {row["python_name"] for row in existing_rows}
    python_existing_names = {
        row["python_name"] for row in existing_rows
        if (row.get("kind") or "python") == "python"
    }

    for spec in specs:
        payload = {
            "python_name": spec.python_name,
            "display_name": spec.display_name,
            "description": spec.description,
            "category": spec.category,
            "scope": spec.scope,
            "source_file": spec.source_file,
            "argument_schema": spec.argument_schema,
            "agent_kind": spec.agent_kind,
            "additional_agent_kinds": list(spec.additional_agent_kinds),
            "is_deprecated": False,
            # Explicit so legacy rows promoted from before migration
            # 20260520140000 land with a sensible discriminator and
            # don't get treated as HTTP tools by mistake.
            "kind": "python",
        }
        if spec.python_name in existing_names:
            client.table("agent_tools").update(payload).eq("python_name", spec.python_name).execute()
            updated += 1
        else:
            client.table("agent_tools").insert(payload).execute()
            inserted += 1

    # Only Python-kind rows can be deprecated by this sync — HTTP tools
    # live entirely in the DB and would otherwise be flagged every boot.
    seen = {spec.python_name for spec in specs}
    deprecated = python_existing_names - seen
    if deprecated and scope_kinds is not None:
        # Never deprecate a tool belonging to an agent kind THIS build does not
        # serve. `sync_tools.run()` only registers the active deployment
        # scope's kinds (or, on a full build, every kind no scope owns), so
        # everything else is absent from the registry for a reason that has
        # nothing to do with the tool still existing in source.
        #
        # Without this, any boot deprecates every other build's tools in a
        # shared AI project: a full-build boot wipes a client demo's eight
        # tools, that agent then resolves to a tool set missing exactly those, and the
        # model calls tools that aren't bound. It presents as the agent
        # "forgetting" how to answer -- the recurring Tool-not-found class of
        # incident in this repo. Reproduced locally: running the test suite
        # (conftest forces a full build) between two questions in one
        # voice session was enough to break the second.
        # A row is safe to deprecate only when EVERY kind it serves is in
        # scope -- primary AND additional. A tool built by two builds' own
        # factories (e.g. a chart tool two client demos share) carries the other build's
        # kind in additional_agent_kinds; judging by primary kind alone lets
        # whichever build booted last deprecate it out from under the other.
        # Conservative by design: when in doubt, leave the row alone -- an
        # undead tool row is inert, a wrongly-deprecated one is the
        # Tool-not-found incident.
        in_scope = {
            row["python_name"] for row in existing_rows
            if (row.get("kind") or "python") == "python"
            and (row.get("agent_kind") or "") in scope_kinds
            and all(k in scope_kinds for k in (row.get("additional_agent_kinds") or []))
        }
        skipped = deprecated - in_scope
        if skipped:
            logger.info(
                "tool_registry: leaving %d out-of-scope tool(s) alone: %s",
                len(skipped), sorted(skipped),
            )
        deprecated &= in_scope
    if deprecated:
        client.table("agent_tools").update({"is_deprecated": True}).in_("python_name", list(deprecated)).execute()

    return {"inserted": inserted, "updated": updated, "deprecated": len(deprecated)}
