"""Pure ordering logic for built-in bundle MCP membership.

Isolated + I/O-free so the seeder (admin/seed_builtin_mcps.py) and the
MCP-resolution harness (tests/runtime/test_mcp_parity.py) share ONE definition
of "what order do a kind's tools go in." Tool order depends on member
`position` following the factory-pool order, because the MCP resolver
(runtime/tools/_mcp_resolver._bundle_tools) binds tools in member `position`
order.
"""
from __future__ import annotations

from typing import Any, Dict, List

_BUILTIN_PREFIX = "__builtin__:"


def builtin_mcp_name(kind: str) -> str:
    """Stable, kind-scoped name the seeder upserts the built-in bundle by."""
    return f"{_BUILTIN_PREFIX}{kind}"


def ordered_membership(
    pool_names: List[str], tool_rows: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Ordered member descriptors for a kind's built-in bundle.

    Python tools (present in the factory `pool_names`) come first, in
    factory-pool order; HTTP/other tools not in the pool are appended in
    `python_name`-sorted order (deterministic). Positions are 0..N-1.
    Rows missing python_name or tool_id are skipped.
    """
    pool_index: Dict[str, int] = {}  # first occurrence wins
    for i, name in enumerate(pool_names):
        pool_index.setdefault(name, i)
    in_pool: List[Dict[str, Any]] = []
    appended: List[Dict[str, Any]] = []
    seen: set = set()
    for row in tool_rows:
        name = row.get("python_name")
        tool_id = row.get("tool_id")
        if not name or not tool_id or name in seen:
            continue
        seen.add(name)
        descriptor = {"python_name": name, "tool_id": tool_id, "kind": row.get("kind") or "python"}
        if name in pool_index:
            in_pool.append(descriptor)
        else:
            appended.append(descriptor)
    in_pool.sort(key=lambda d: pool_index[d["python_name"]])
    appended.sort(key=lambda d: d["python_name"])
    out: List[Dict[str, Any]] = []
    for pos, d in enumerate([*in_pool, *appended]):
        out.append({**d, "position": pos})
    return out
