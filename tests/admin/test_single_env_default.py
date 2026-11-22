"""The admin portal must open on an environment that actually has data.

Reported as "<a partner demo box>/admin/live has no traces, no agents,
nothing showing". Nothing was missing: the box runs `ENVIRONMENT=staging` and
seeds every row as staging, but `current_env_live` defaulted to **prod** — an
environment a scoped build (core/deployment.py) does not have by construction. So a fresh session
filtered `agent_profiles.environment == "prod"` (0 rows) and scoped the trace
tiles to prod (0 traces) while 1,200+ real traces sat one query-param away.

The two-env defaults are correct on the real control-plane, which does serve
both branches — so these tests pin the distinction, not just the new value.
"""
from __future__ import annotations

import pytest

from tests.scoped_build import full_build, use_orchard


@pytest.fixture(autouse=True)
def _full_build(monkeypatch):
    full_build(monkeypatch)
    monkeypatch.delenv("ENVIRONMENT", raising=False)


def test_control_plane_keeps_its_two_env_defaults(monkeypatch):
    """No deployment scope → the full engine, which really does manage
    both branches. Config screens stay on staging, Live Monitor on prod."""
    from admin.router import current_env, current_env_live, single_env_deployment

    assert single_env_deployment() is None
    assert current_env(None, None) == "staging"
    assert current_env_live(None, None) == "prod"


def test_scoped_build_defaults_to_its_own_environment(monkeypatch):
    """The regression. A partner box is single-env staging: BOTH the config
    screens and the Live Monitor must land there, or the portal opens empty."""
    use_orchard(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    from admin.router import current_env, current_env_live, single_env_deployment

    assert single_env_deployment() == "staging"
    assert current_env(None, None) == "staging"
    assert current_env_live(None, None) == "staging", (
        "Live Monitor defaulting to prod on a staging-only box is exactly the "
        "'no agents, no traces' report"
    )


def test_scoped_build_on_prod_defaults_to_prod(monkeypatch):
    """The fix follows the deployment's own env — it does not hardcode staging."""
    use_orchard(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    from admin.router import current_env_live, single_env_deployment

    assert single_env_deployment() == "prod"
    assert current_env_live(None, None) == "prod"


def test_explicit_selection_still_wins_on_the_control_plane(monkeypatch):
    """`?env=` and the persisted cookie outrank the default on the real
    control-plane — an operator who deliberately switches must not be snapped
    back. That box serves both branches, so either value names something real.
    """
    from admin.router import current_env, current_env_live

    assert current_env_live("staging", None) == "staging"  # query param
    assert current_env_live(None, "staging") == "staging"  # persisted cookie
    assert current_env("prod", None) == "prod"
    assert current_env(None, "prod") == "prod"


def test_stale_cookie_cannot_blank_a_single_env_portal(monkeypatch):
    """The regression this file's fix missed the first time.

    `waj_admin_env` persists for 30 days, and on a scoped build the env
    switcher is HIDDEN (see the test below) — so once the cookie held `prod`
    there was no UI left to correct it. Every config screen rendered "No
    profiles seeded for the prod environment yet" forever. The deployment's
    own env must therefore outrank the cookie, not the other way round.
    """
    use_orchard(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    from admin.router import current_env, current_env_live

    assert current_env(None, "prod") == "staging"
    assert current_env_live(None, "prod") == "staging"


def test_query_param_cannot_wedge_a_single_env_portal(monkeypatch):
    """`?env=prod` must not stick either — the persist-env middleware writes
    whatever the query names into the cookie, so honouring it here is what
    loaded the footgun. On a one-env box the other tab is empty by
    construction; there is nothing to peek at and no switcher to come back on.
    """
    use_orchard(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    from admin.router import current_env, current_env_live

    assert current_env("prod", None) == "staging"
    assert current_env_live("prod", None) == "staging"
    # Belt and braces: both set, still pinned to the box's own env.
    assert current_env("prod", "prod") == "staging"


def test_middleware_resolves_single_env_and_refuses_the_cookie(monkeypatch):
    """`persist_admin_env` is the worse half of the bug: it never consulted
    `single_env_deployment()` at all, and the env it resolves drives the
    `_ADMIN_ENV` ContextVar behind `admin_service_client()` — so a stale
    cookie sent the actual DB reads to the wrong environment, not just the
    displayed label. It must also stop writing a cookie that contradicts the
    box, or the next request re-arms the same trap.
    """
    import asyncio

    use_orchard(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "staging")

    import main as main_module
    from core.auth import get_admin_env

    seen: dict = {}

    class _Resp:
        def __init__(self):
            self.cookies = {}

        def set_cookie(self, key, value, **kw):
            self.cookies[key] = value

    class _Req:
        url = type("U", (), {"path": "/admin/agents"})()
        query_params = {"env": "prod"}
        cookies = {"waj_admin_env": "prod"}

    async def _call_next(_req):
        seen["env"] = get_admin_env()
        return _Resp()

    resp = asyncio.run(main_module.persist_admin_env(_Req(), _call_next))

    assert seen["env"] == "staging", (
        "the ContextVar drives admin_service_client() — a prod value here reads "
        "the wrong environment's rows"
    )
    assert "waj_admin_env" not in resp.cookies, (
        "persisting prod on a staging-only box is what wedged the portal"
    )


def test_scoped_build_hides_the_env_switcher():
    """A switcher whose other tab is guaranteed-empty is a trap: clicking
    PRODUCTION on a partner box is what produced the original report."""
    from pathlib import Path

    base = Path("admin/templates/_base.html").read_text(encoding="utf-8")
    assert "single_env_deployment()" in base
    # Folded into hide_env_switcher so the per-screen opt-outs keep working.
    assert "{% if not hide_env_switcher %}" in base

    import admin.router as router

    assert router.templates.env.globals["single_env_deployment"] is (
        router.single_env_deployment
    ), "template global must be the callable, or it can't re-read env per render"


def test_every_admin_template_env_registers_the_global():
    """_base.html *calls* single_env_deployment(), so a Jinja env that renders
    it without the global raises UndefinedError — a 500, not a silent falsy.

    admin/ deliberately has more than one `Jinja2Templates` instance and they
    do NOT share globals (see the comment in eval_handlers.py). Registering on
    admin.router alone 500'd every /admin/evals/suites/* screen, so this walks
    the source for every env that exists rather than pinning today's two.
    """
    import importlib
    import re
    from pathlib import Path

    from core.deployment import single_env_deployment as canonical

    owners = [
        p for p in Path("admin").rglob("*.py")
        if re.search(r"^templates\s*=\s*Jinja2Templates\(", p.read_text(encoding="utf-8"), re.M)
    ]
    assert owners, "expected at least one admin Jinja2Templates owner"

    for path in owners:
        module = importlib.import_module(str(path.with_suffix("")).replace("/", "."))
        registered = module.templates.env.globals.get("single_env_deployment")
        assert registered is canonical, (
            f"{path} renders _base.html but never registered "
            f"single_env_deployment on its own Jinja env"
        )
