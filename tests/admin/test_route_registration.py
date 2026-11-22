"""Route-table hygiene: no duplicate (method, path) registrations in admin.router.

Why this exists
----------------
Starlette dispatches first-match-wins, so registering the same (method, path)
twice silently dead-codes the later registration. The 2026-08-26 portal audit
found exactly this had been live for months: back-compat redirects for
``GET /admin/agent-data-sources`` and ``GET /admin/agent-mcp-servers`` were
registered BEFORE the ``_make_simple_crud`` factory registered list views on
the same paths — the factory list views were unreachable, while their sibling
POST creates survived WITHOUT the validation the real create paths carry
(reserved ``__builtin__:`` prefix, absolute-http endpoint, context_fields
JSON). The factory is gone now; this test pins the bug *class* so a duplicate
registration is a build failure, not a silent shadow.

Scope: every route registered on the shared admin ``router`` — router.py's own
body, its bottom-of-file sidecar imports, and the handler modules main.py
additionally imports (same list as tests/admin_mcp/test_surface_parity.py's
discovery, for the same reason: importing main itself would drag in
Laminar/lifespan wiring this check doesn't need).

Deliberately NOT asserted here: shadowing across *different* path templates
(e.g. ``/observability/{trace_id}`` swallowing a later static
``/observability/data``). That needs a template-aware matcher, and the one
known instance was deleted rather than guarded — see admin/router.py near the
datasets redirects.
"""
from __future__ import annotations

from collections import Counter

from fastapi.routing import APIRoute


def _all_admin_route_pairs():
    """Every (method, path) pair registered on the shared admin router."""
    from admin.router import router
    import admin.prompt_handlers  # noqa: F401
    import admin.channel_handlers  # noqa: F401
    import admin.observability_explorer_handlers  # noqa: F401
    import admin.jobs_handlers  # noqa: F401
    import admin.eval_runs_handlers  # noqa: F401
    import admin.memory_handlers  # noqa: F401

    pairs = []
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            pairs.append((method, route.path))
    return pairs


def test_no_duplicate_method_path_registrations():
    pairs = _all_admin_route_pairs()
    assert pairs, "route discovery found nothing — the import wiring is broken"

    duplicates = {pair: n for pair, n in Counter(pairs).items() if n > 1}
    assert not duplicates, (
        "Duplicate (method, path) registrations on the admin router — the "
        "later registration is unreachable dead code (Starlette dispatch is "
        "first-match-wins):\n"
        + "\n".join(f"  {n}x {m} {p}" for (m, p), n in sorted(duplicates.items()))
        + "\nRemove the duplicate (or the route it shadows) instead of "
        "shipping a handler that can never run."
    )
