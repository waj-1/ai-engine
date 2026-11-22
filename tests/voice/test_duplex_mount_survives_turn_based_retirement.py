"""The duplex WS must stay reachable after the turn-based routes are retired.

Both routers declared prefix="/voice" (endpoints/voice.py and
endpoints/voice_live.py). "/voice/" was once a blocked prefix on scoped builds
to 404 the turn-based routes; leaving it there once the duplex edge is the
only thing mounted under /voice would 404 every scoped build's calls.

The deployment gate is @app.middleware("http") and Starlette's
BaseHTTPMiddleware skips non-http scopes, so a WS handshake never reaches the
gate on any prefix. This test pins the HTTP-side prefix list, which is the
thing a careless edit breaks. (The plugin's own list is pinned in its tests.)
"""
import main
from core.deployment import blocked_path_prefixes

from tests.scoped_build import ORCHARD, use_scope
from tests.voice.conftest import iter_route_paths


def test_voice_prefix_is_not_blocked_on_a_scoped_build(monkeypatch):
    tenant = ORCHARD
    use_scope(monkeypatch, tenant)
    assert not any(p.startswith("/voice") for p in blocked_path_prefixes(tenant)), (
        "the duplex WS mounts under /voice — blocking the prefix 404s it on "
        "every scoped build"
    )


def test_a_duplex_voice_live_route_is_registered():
    paths = set(iter_route_paths(main.app.routes))
    assert any(p.endswith("/voice/live") for p in paths), sorted(
        p for p in paths if "voice" in p
    )


def test_no_turn_based_voice_routes_remain():
    paths = set(iter_route_paths(main.app.routes))
    # Guard against a vacuous pass: if the enumerator returns nothing under an
    # included router (the FastAPI 0.139 trap), every "is absent" assertion
    # below would pass with the routes still mounted.
    assert any(p.endswith("/voice/live") for p in paths), (
        "route enumeration is broken — the assertions below would pass vacuously"
    )
    for gone in ("/voice/speak", "/voice/transcribe", "/voice/chat/stream"):
        assert not any(p.endswith(gone) for p in paths), f"{gone} still mounted"
