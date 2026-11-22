"""Duplex voice mounting under a deployment scope's prefix.

The HTTP deployment gate (`main.deployment_gate`) never sees a WebSocket
handshake: it is registered via `@app.middleware("http")`, and Starlette's
`BaseHTTPMiddleware` skips every non-"http" ASGI scope before the gate's own
body runs. So the duplex route is protected differently: only the ACTIVE
scope's prefix is mounted in a given process (`main._mount_prefix()`), and
another scope's `<prefix>/voice/live` is a genuine routing 404. The plugin's
own prefix list is pinned in
`tests/voice/test_duplex_mount_survives_turn_based_retirement.py`.
"""
import main as main_mod

from tests.scoped_build import make_scope, use_scope
from tests.voice.conftest import iter_route_paths


def test_duplex_route_is_mounted_under_the_active_portal_prefix():
    # NOT `{route.path for route in app.routes}`: on FastAPI 0.139 every
    # `include_router` call becomes an `_IncludedRouter` with no `.path` at
    # all, so the naive comprehension raises there and yields nothing under
    # an included router -- see tests/voice/conftest.py::iter_route_paths.
    paths = set(iter_route_paths(main_mod.app.routes))
    live = {p for p in paths if p.endswith("/voice/live")}
    assert live, "the duplex voice route must be registered"
    assert f"{main_mod._mount_prefix()}/voice/live" in live


def test_mount_prefix_follows_the_active_scope(monkeypatch):
    """Direct unit test of `_mount_prefix()`'s branch logic.

    The suite runs as a full build, so the shared `main.app` is mounted with
    an empty prefix (baked into the route table at import). `_mount_prefix()`
    is its own function so THIS test can exercise the scoped branch.
    """
    orchard, other = make_scope("orchard"), make_scope("other")

    use_scope(monkeypatch, orchard, others=[other])
    assert main_mod._mount_prefix() == "/orchard"

    use_scope(monkeypatch, other, others=[orchard])
    assert main_mod._mount_prefix() == "/other"

    use_scope(monkeypatch, None, others=[orchard, other])
    assert main_mod._mount_prefix() == ""
