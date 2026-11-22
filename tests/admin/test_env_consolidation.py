"""Tests for the consolidated Environments screen.

Covers the env-vars fold into /admin/env and the single-DB main-only deploy
model (prod is the only deployable Cloud Run service; `staging` is an
`environment` column value, not a service — dev is local-only and never was
a deploy target):

  POST /admin/env/prod/deploy     — prod dispatches target_env=prod
                                    to the GHA workflow.
  POST /admin/env/staging/deploy  — staging now 400 ('staging' is a column,
                                    not a deployable service).
  POST /admin/env/dev/deploy      — dev now 400 (prod-only control plane).
  POST /admin/env/prod/rollback   — prod dispatches the rollback workflow
                                    with target_env=prod.
  GET  /admin/env-vars            — 307-redirects to /admin/env?tab=variables.
  env.html Variables tab          — renders stubbed agent_env_vars rows
                                    with secret values masked.

Uses the same TestClient + auth-override + fake-supabase pattern as
test_channels.py / test_tool_routes.py.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


# ---------------------------------------------------------------------------
# Minimal scriptable supabase fake (subset of the test_channels.py shape)
# ---------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows

    def select(self, *_a, **_k):
        return self

    def eq(self, col=None, value=None, **_k):
        # Honour real equality filters (e.g. the per-env fetch filters on
        # environment) so single-client tests keep meaningful buckets.
        if col is not None:
            self._rows = [r for r in self._rows if r.get(col) == value]
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        result = MagicMock()
        result.data = list(self._rows)
        return result


class _FakeClient:
    def __init__(self, rows_by_table=None):
        self._rows_by_table = rows_by_table or {}

    def table(self, name):
        return _FakeQuery(self._rows_by_table.get(name, []))


# ---------------------------------------------------------------------------
# App boot helper
# ---------------------------------------------------------------------------


def _make_client(fake_db=None, *, github_token="fake-token"):
    """Boot the app with require_superuser bypassed. Patches the supabase
    client in router (for env_screen) + env_vars_handlers, and sets the
    GITHUB_DEPLOY_TOKEN so deploy/rollback get past the token gate."""
    from fastapi.testclient import TestClient
    from admin import auth, router, env_vars_handlers
    from admin.services import deploys as deploy_service
    from main import app

    fake_db = fake_db or _FakeClient()

    def _fake_superuser():
        return {"user_token": "jwt", "user_id": "u-1", "email": "t@waj.local"}

    prior = app.dependency_overrides.get(auth.require_superuser)
    app.dependency_overrides[auth.require_superuser] = _fake_superuser

    patches = [
        patch.object(router, "get_service_client", return_value=fake_db),
        patch.object(router, "client_for", return_value=fake_db),
        patch.object(env_vars_handlers, "admin_service_client", return_value=fake_db),
        patch.object(env_vars_handlers, "client_for", return_value=fake_db),
        patch.object(deploy_service, "record_change"),
        patch.dict("os.environ", {"GITHUB_DEPLOY_TOKEN": github_token} if github_token else {}, clear=False),
    ]
    if not github_token:
        # Ensure the var is actually absent for the "no token" path.
        patches[-1] = patch.dict("os.environ", {}, clear=False)
    for p in patches:
        p.start()
    if not github_token:
        import os
        os.environ.pop("GITHUB_DEPLOY_TOKEN", None)

    client = TestClient(app)

    def _teardown():
        for p in patches:
            p.stop()
        if prior is not None:
            app.dependency_overrides[auth.require_superuser] = prior
        else:
            app.dependency_overrides.pop(auth.require_superuser, None)

    return client, _teardown


def _mock_httpx_204():
    """Return a patcher for httpx.AsyncClient whose .post() resolves to a
    204 response (GitHub dispatch success) and captures the call."""
    captured: Dict[str, Any] = {}

    resp = MagicMock()
    resp.status_code = 204
    resp.text = ""

    async def _post(url, headers=None, json=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return resp

    fake_async = MagicMock()
    fake_async.post = AsyncMock(side_effect=_post)
    fake_async.__aenter__ = AsyncMock(return_value=fake_async)
    fake_async.__aexit__ = AsyncMock(return_value=False)

    return patch("admin.deploy_handlers.httpx.AsyncClient", return_value=fake_async), captured


# ---------------------------------------------------------------------------
# Deploy: single-DB main-only control plane (prod is the only deploy target)
# ---------------------------------------------------------------------------


def test_deploy_prod_dispatches_target_env_prod():
    client, teardown = _make_client()
    patcher, captured = _mock_httpx_204()
    try:
        with patcher:
            r = client.post(
                "/admin/env/prod/deploy",
                data={"git_ref": "main"},
                follow_redirects=False,
            )
        assert r.status_code == 302, r.text[:400]
        assert captured["json"]["inputs"]["target_env"] == "prod"
        assert captured["json"]["ref"] == "main"
    finally:
        teardown()


def test_deploy_staging_now_rejected():
    """`staging` is an environment column value now, not a deployable
    service (the staging service was retired) — only prod is deployable."""
    client, teardown = _make_client()
    try:
        r = client.post(
            "/admin/env/staging/deploy", data={"git_ref": "main"}, follow_redirects=False
        )
        assert r.status_code == 400
        assert "prod" in r.text.lower()
    finally:
        teardown()


def test_deploy_service_name_mapping_single_db(monkeypatch):
    from admin.deploy_handlers import _service_name
    monkeypatch.setenv("ACA_APP_NAME", "engine-app")
    assert _service_name("prod") == "engine-app"
    # staging is a column value now, not a service — maps to the one app
    # like any other non-prod value.
    assert _service_name("staging") == "engine-app"
    # dev is local-only now — no Container App, maps to the same app.
    assert _service_name("dev") == "engine-app"


def test_deploy_dev_now_rejected():
    """dev is no longer a deploy target — the single-DB main-only control
    plane accepts prod only."""
    client, teardown = _make_client()
    try:
        r = client.post(
            "/admin/env/dev/deploy", data={"git_ref": "main"}, follow_redirects=False
        )
        assert r.status_code == 400
        assert "prod" in r.text.lower()
    finally:
        teardown()


def test_deploy_unknown_env_still_rejected():
    client, teardown = _make_client()
    try:
        r = client.post(
            "/admin/env/qa/deploy", data={"git_ref": "main"}, follow_redirects=False
        )
        assert r.status_code == 400
        assert "prod" in r.text.lower()
    finally:
        teardown()


# ---------------------------------------------------------------------------
# Rollback: prod only
# ---------------------------------------------------------------------------


def test_rollback_prod_accepted():
    client, teardown = _make_client()
    patcher, captured = _mock_httpx_204()
    try:
        with patcher:
            r = client.post(
                "/admin/env/prod/rollback",
                data={"target_revision": "engine-app-00001-abc"},
                follow_redirects=False,
            )
        assert r.status_code == 302, r.text[:400]
        assert captured["json"]["inputs"]["target_env"] == "prod"
        assert captured["json"]["inputs"]["target_revision"] == "engine-app-00001-abc"
    finally:
        teardown()


def test_rollback_staging_now_rejected():
    """`staging` is an environment column value now, not a deployable
    service — only prod can be rolled back."""
    client, teardown = _make_client()
    try:
        r = client.post(
            "/admin/env/staging/rollback",
            data={"target_revision": "engine-app-00001-abc"},
            follow_redirects=False,
        )
        assert r.status_code == 400
        assert "prod" in r.text.lower()
    finally:
        teardown()


def test_rollback_dev_now_rejected():
    client, teardown = _make_client()
    try:
        r = client.post(
            "/admin/env/dev/rollback",
            data={"target_revision": "ai-engine-dev-00001-abc"},
            follow_redirects=False,
        )
        assert r.status_code == 400
        assert "prod" in r.text.lower()
    finally:
        teardown()


# ---------------------------------------------------------------------------
# /admin/env-vars → /admin/env?tab=variables redirect
# ---------------------------------------------------------------------------


def test_env_vars_list_redirects_to_env():
    client, teardown = _make_client()
    try:
        r = client.get("/admin/env-vars", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/admin/env?tab=variables"
    finally:
        teardown()


def test_env_vars_list_redirect_carries_query():
    from urllib.parse import parse_qs, urlsplit

    client, teardown = _make_client()
    try:
        r = client.get("/admin/env-vars?env=prod", follow_redirects=False)
        assert r.status_code == 307
        parts = urlsplit(r.headers["location"])
        assert parts.path == "/admin/env"
        assert parse_qs(parts.query) == {"tab": ["variables"], "env": ["prod"]}
    finally:
        teardown()


# ---------------------------------------------------------------------------
# env.html Variables tab renders the env-vars registry with masked rows
# ---------------------------------------------------------------------------


def test_env_screen_renders_env_vars_section_masked():
    rows = [
        {
            "var_id": "v-secret",
            "environment": "staging",
            "name": "OPENAI_API_KEY",
            "value": "sk-super-secret-123",
            "is_secret": True,
            "source": "db",
            "description": "OpenAI key",
            "updated_at": "2026-05-20T10:00:00Z",
        },
        {
            "var_id": "v-plain",
            "environment": "prod",
            "name": "LOG_LEVEL",
            "value": "debug",
            "is_secret": False,
            "source": "db",
            "description": "log verbosity",
            "updated_at": "2026-05-20T11:00:00Z",
        },
    ]
    fake = _FakeClient(rows_by_table={"agent_env_vars": rows, "agent_profiles": []})
    client, teardown = _make_client(fake)
    try:
        # env_screen builds env_vars via fetch_env_vars_by_env(client); call
        # the real helper against our fake client and assert the template
        # renders the grouped, masked structure it produces.
        from admin.env_vars_handlers import fetch_env_vars_by_env

        env_vars = fetch_env_vars_by_env(fake)
        # Secret masked, plain visible, in the grouped structure.
        assert env_vars["staging"][0]["value"] == "••••"
        assert env_vars["staging"][0].get("_masked") is True
        assert env_vars["prod"][0]["value"] == "debug"

        # Render the template directly with the grouped context to prove the
        # Variables tab handles masked rows.
        from admin.router import templates
        html = templates.get_template("env.html").render(
            ctx={"email": "t@waj.local"},
            active="env",
            current_env="dev",
            tab="variables",
            env_vars=env_vars,
        )
        assert "Environment variables" in html
        assert "OPENAI_API_KEY" in html
        assert "LOG_LEVEL" in html
        assert "sk-super-secret-123" not in html  # secret never leaks
        assert "••••" in html
        assert "debug" in html
    finally:
        teardown()


def test_env_variables_tab_renders_empty_state():
    """GET /admin/env?tab=variables renders the registry with the empty-state
    rows when no env vars exist, and without the promote cards."""
    fake = _FakeClient(rows_by_table={"agent_profiles": [], "agent_env_vars": []})
    client, teardown = _make_client(fake)
    try:
        r = client.get("/admin/env?tab=variables")
        assert r.status_code == 200
        assert "Environment variables" in r.text
        assert "No env vars configured" in r.text
        assert "How promote works" not in r.text
    finally:
        teardown()


def test_env_promote_tab_has_no_env_vars_registry():
    """The default (Promote) tab renders the release overview (one grid +
    the promote dialog) and does not render the env-vars registry."""
    fake = _FakeClient(rows_by_table={"agent_profiles": [], "agent_env_vars": []})
    client, teardown = _make_client(fake)
    try:
        r = client.get("/admin/env")
        assert r.status_code == 200
        assert 'id="promote-dialog"' in r.text
        assert "No agents yet" in r.text
        # No two-column STAGING / PROD stack any more.
        assert ">STAGING<" not in r.text and ">PROD<" not in r.text
        assert "No env vars configured" not in r.text
    finally:
        teardown()
