"""Admin portal HTML must not be heuristically cached by the browser.

The portal is server-rendered operator state — the profile rail, the env-scoped
config cards, live trace tiles — and it shipped with no `Cache-Control` and no
validator. With neither, browsers fall back to heuristic freshness and may serve
a stored copy, which is how an operator ends up looking at a profile list
captured before a row existed and concludes the row is missing.

Static assets are excluded on purpose: they carry their own `?v=` cache-busting
and SHOULD stay cacheable (unbumped, cached portal CSS is its own past
incident — a stale chart stylesheet once outlived its fix).
"""
from __future__ import annotations

from tests.admin.conftest import client  # noqa: F401 — session-scoped superuser TestClient


def test_admin_pages_are_no_store(client):  # noqa: F811
    r = client.get("/admin/agents")

    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-store"


def test_admin_static_keeps_its_own_caching(client):  # noqa: F811
    """portal.css is versioned via `?v=` in the template; forcing no-store here
    would re-fetch the stylesheet on every navigation for no benefit."""
    r = client.get("/admin/static/portal.css")

    assert r.status_code == 200
    assert r.headers.get("cache-control") != "no-store"


def test_non_admin_routes_are_untouched(client):  # noqa: F811
    """The directive is scoped to /admin — the chat/voice API is unaffected."""
    r = client.get("/health")

    assert r.headers.get("cache-control") != "no-store"
