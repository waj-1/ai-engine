"""Walk each agent's tool factory and upsert agent_tools rows.

Idempotent: tools missing from the latest walk are marked deprecated.
Called from main.py lifespan via asyncio.to_thread.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from core.tool_registry import ToolSpec, register_tool, sync_to_db
from core.deployment import active_scope, all_scope_kinds, scope_kinds

logger = logging.getLogger(__name__)

# Union of every deployment scope's kinds. On a full build NONE of these may
# be synced into the shared DB; on a scoped build ONLY the active scope's
# kinds are.
_ALL_SCOPE_KINDS = all_scope_kinds()

def _plugin_factories() -> List[tuple]:
    """Every plugin's tool factories: (agent_kind, source_file, module name,
    factory name, kwargs), in plugin order."""
    from core.plugins import call_hook, load_hook

    return [f for found in call_hook(load_hook("tool_factories"), "tool_factories") for f in found]


def _plugin_write_tool_names() -> set:
    """Tool names a plugin marks as changing data (scope 'write')."""
    from core.plugins import call_hook, load_hook

    return {n for found in call_hook(load_hook("tool_factories"), "write_tool_names") for n in found}


# Per-agent: (agent_kind, source_file, module name, factory name, kwargs).
# Each agent's tools come from its own factory, so the engine ships none: the
# demo agents seed their own (admin/seed_agents_demo.py) and a generic agent's
# tools come from the MCP servers it attaches.
_FACTORIES: List[tuple] = _plugin_factories()

# Catalog metadata only — nothing at runtime gates on it.
WRITE_TOOL_NAMES: set = _plugin_write_tool_names()


def run() -> Dict[str, int]:
    """Register every tool from every agent then sync to DB.

    Gated via the deployment scopes (core/deployment.py): a scoped build
    syncs ONLY the active scope's kinds; a full build syncs everything EXCEPT
    the union of ALL scopes' kinds (no scope's tool rows may land in the
    shared DB or surface in the full build's admin tools list).
    """
    try:
        active = active_scope()
    except ValueError as exc:
        # Unknown scope configuration: boot validation elsewhere refuses to
        # start; a seeder must not crash — fail safe to the full-build gate
        # (which skips every scope's kind).
        logger.warning("sync_tools: %s — treating as normal WAJ build", exc)
        active = None
    allowed_kinds = scope_kinds(active) if active is not None else None
    scanned_kinds: set = set()
    for agent_kind, source_file, module_name, factory_name, kwargs in _FACTORIES:
        if allowed_kinds is not None:
            # Scoped build: ONLY the active scope's kinds.
            if agent_kind not in allowed_kinds:
                continue
        elif agent_kind in _ALL_SCOPE_KINDS:
            # Full build: no deployment scope's kinds, ever.
            continue
        try:
            module = __import__(module_name, fromlist=[factory_name])
            factory = getattr(module, factory_name)
            tools = _call_factory(factory, kwargs)
        except Exception as exc:
            logger.warning("sync_tools: %s/%s failed (%s)", agent_kind, factory_name, exc)
            continue
        for tool in tools:
            spec = _to_spec(tool, agent_kind, source_file)
            register_tool(spec)
        scanned_kinds.add(agent_kind)
    # Only kinds whose factory ACTUALLY REGISTERED are "scanned" -- populated
    # in the loop above on success. A factory that raised leaves its kind out,
    # so a transient import/build failure cannot mass-deprecate that kind's
    # tools (absence from the registry would mean "the factory broke", not
    # "the tools were deleted from source").
    try:
        return sync_to_db(scope_kinds=scanned_kinds)
    except Exception as exc:
        logger.warning("sync_tools: db sync failed (%s)", exc)
        return {"inserted": 0, "updated": 0, "deprecated": 0}


def _call_factory(factory, kwargs: Dict[str, Any]) -> List[Any]:
    result = factory(**kwargs)
    return list(result) if result else []


_FACTORY_BY_KIND = {f[0]: f for f in _FACTORIES}


def pool_for_kind(agent_kind: str) -> List[Any]:
    """Build an agent kind's factory tool pool (best-effort). Returns [] for
    kinds with no factory or on any factory error — callers treat [] as
    'no python tools'.

    Unlike ``run()`` this deliberately skips the scoped-build ``allowed_kinds``
    gating: the flag-parity diagnostic must be able to build ANY kind's pool
    regardless of the build's deployment scope.
    """
    entry = _FACTORY_BY_KIND.get(agent_kind)
    if entry is None:
        return []
    _, source_file, module_name, factory_name, kwargs = entry
    try:
        module = __import__(module_name, fromlist=[factory_name])
        return _call_factory(getattr(module, factory_name), kwargs)
    except Exception as exc:  # noqa: BLE001 — best-effort pool build
        logger.warning("pool_for_kind: %s/%s failed (%s)", agent_kind, factory_name, exc)
        return []


def _to_spec(tool: Any, agent_kind: str, source_file: str) -> ToolSpec:
    name = getattr(tool, "name", str(tool))
    description = getattr(tool, "description", "") or ""
    arg_schema: Dict[str, Any] = {}
    args_schema = getattr(tool, "args_schema", None)
    if args_schema is not None and hasattr(args_schema, "model_json_schema"):
        try:
            arg_schema = args_schema.model_json_schema()
        except Exception:
            arg_schema = {}
    scope = "write" if name in WRITE_TOOL_NAMES else "read"
    return ToolSpec(
        python_name=name,
        display_name=name.replace("_", " ").title(),
        description=description[:500],
        category=agent_kind,
        scope=scope,
        source_file=source_file,
        argument_schema=arg_schema,
        agent_kind=agent_kind,
    )
