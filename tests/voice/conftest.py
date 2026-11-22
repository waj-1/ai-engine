"""Shared helpers for the voice test package.

Nothing here is a pytest fixture — it is imported explicitly
(`from tests.voice.conftest import iter_route_paths`) because the route
enumerator is also useful from a one-off shell check.
"""


def iter_route_paths(routes, prefix: str = ""):
    """Every registered path, across FastAPI 0.136 and 0.139+.

    0.136 gives a flat list of routes carrying `.path`. 0.139 wraps each
    `include_router` call in an `_IncludedRouter` that has no `.path` at all —
    the child routes hang off `.original_router` and the mount prefix off
    `.include_context.prefix`. A test that reads `.path` directly sees NOTHING
    under an included router on 0.139, so any "route X is absent" assertion
    written that way passes vacuously.
    """
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            context = getattr(route, "include_context", None)
            yield from iter_route_paths(
                original.routes, prefix + (getattr(context, "prefix", "") or "")
            )
            continue
        path = getattr(route, "path", None)
        if path:
            yield prefix + path
            continue
        nested = getattr(route, "routes", None)
        if nested:
            yield from iter_route_paths(nested, prefix)
