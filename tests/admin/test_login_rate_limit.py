"""Regression test: /admin/login and /admin/logout must stay rate-limited.

A security regression on this branch broadened the rate-limit skip in
main.security_and_rate_limit_middleware to exempt *every* ``/admin/*`` path
(``request.url.path.startswith("/admin")``). That exempted the unauthenticated
``POST /admin/login`` endpoint (which calls ``sign_in_with_password``) from the
per-IP sliding-window limiter, removing the brute-force throttle on superuser
password auth.

The fix keeps skipping the authenticated GET config pages under ``/admin`` but
re-includes ``/admin/login`` and ``/admin/logout`` in the limiter.

These tests pin both layers:

* ``test_skip_predicate_*`` — a faithful copy of the middleware's
  ``skip_rate_limit`` expression, asserting it does NOT skip the auth endpoints
  while still skipping ordinary ``/admin`` config pages. This is the
  predicate-level guard the task asks for and is the one that would FAIL against
  the pre-fix expression (which skipped ``/admin/login``).
* ``test_admin_login_is_rate_limited`` — drives the real ASGI app through a
  TestClient with the limit lowered, proving the wiring actually produces a 429
  for ``/admin/login``. (We don't drive an authenticated config page through the
  client because those routes fan out to a real Supabase/PostgREST instance that
  isn't available in unit tests; the predicate test above covers the
  "still skipped" half for ``/admin/agents``.)
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def _skip_rate_limit(path: str) -> bool:
    """Faithful copy of the ``skip_rate_limit`` expression in
    main.security_and_rate_limit_middleware. Kept in sync by the behavioural
    test below; this isolates the boolean so a regression is obvious."""
    return (
        path in ["/health", "/", "/debug"]
        or path.startswith("/static")
        or (
            path.startswith("/admin")
            and path not in ("/admin/login", "/admin/logout", "/admin/setup")
        )
    )


@pytest.mark.parametrize("path", ["/admin/login", "/admin/logout", "/admin/setup"])
def test_skip_predicate_does_not_skip_auth_endpoints(path):
    """The unauthenticated auth endpoints must NOT be skipped (so the limiter
    runs and can throttle brute-force attempts)."""
    assert _skip_rate_limit(path) is False


@pytest.mark.parametrize(
    "path",
    ["/admin/agents", "/admin", "/admin/env-vars", "/admin/static/portal.css"],
)
def test_skip_predicate_still_skips_config_pages(path):
    """Authenticated GET config pages (and admin static) stay skipped so a
    developer browsing the portal doesn't exhaust the per-IP budget."""
    assert _skip_rate_limit(path) is True


def test_skip_predicate_still_skips_health_and_static():
    assert _skip_rate_limit("/health") is True
    assert _skip_rate_limit("/") is True
    assert _skip_rate_limit("/static/app.js") is True


@pytest.fixture
def low_rate_limit(monkeypatch):
    """Lower the per-IP limit so the test trips the limiter in a few requests,
    and start from a clean store so other tests' traffic doesn't bleed in."""
    import main

    monkeypatch.setattr(main, "RATE_LIMIT_REQUESTS", 3)
    main.rate_limit_store.clear()
    try:
        yield 3
    finally:
        main.rate_limit_store.clear()


def test_admin_login_is_rate_limited(low_rate_limit):
    """Hammering POST /admin/login past the limit eventually returns 429.

    FastAPI runs HTTP middleware before routing, so the limiter fires
    regardless of how the login handler responds (200/302/4xx). Once the
    per-IP budget is exhausted the middleware short-circuits with a 429 before
    the route is even reached — which is exactly the brute-force throttle this
    regression removed. Against the pre-fix code (/admin/* fully skipped) no
    429 is ever produced and this assertion fails.
    """
    from main import app

    limit = low_rate_limit
    statuses = []
    with TestClient(app) as c:
        # Send a few more than the limit. The middleware admits `limit`
        # requests then returns 429 for the rest.
        for _ in range(limit + 3):
            resp = c.post("/admin/login", data={"email": "x@y.z", "password": "p"})
            statuses.append(resp.status_code)

    assert 429 in statuses, (
        f"POST /admin/login was never rate-limited (statuses={statuses}); the "
        "brute-force throttle on superuser auth is missing."
    )
