"""dispatch_deploy()/dispatch_rollback(): validation, GitHub dispatch, audit.

Route-level coverage (payload shape, redirect, 400 on bad ref/revision)
stays in test_deploy_routes.py; this file exercises the service functions
directly, the way an MCP ship tool would call them without going through
FastAPI at all.
"""
import pytest

from admin.services import deploys
from admin.services.deploys import DeployError, dispatch_deploy


async def test_missing_token_is_400(monkeypatch):
    monkeypatch.delenv("GITHUB_DEPLOY_TOKEN", raising=False)
    with pytest.raises(DeployError) as exc:
        await dispatch_deploy(environment="prod", git_ref="main",
                              actor_user_id="u1")
    assert exc.value.status_code == 400


async def test_bad_environment_is_400(monkeypatch):
    monkeypatch.setenv("GITHUB_DEPLOY_TOKEN", "t")
    with pytest.raises(DeployError):
        await dispatch_deploy(environment="production", git_ref="main",
                              actor_user_id="u1")


async def test_staging_is_no_longer_deployable(monkeypatch):
    """Single-DB main-only topology: `staging` is an environment column value,
    not a deployable service (ai-engine-staging was retired) — only `prod`
    passes the guard now."""
    monkeypatch.setenv("GITHUB_DEPLOY_TOKEN", "t")
    with pytest.raises(DeployError) as exc:
        await dispatch_deploy(environment="staging", git_ref="main",
                              actor_user_id="u1")
    assert exc.value.status_code == 400
    assert "prod" in str(exc.value.detail)
    assert "staging" in str(exc.value.detail)


async def test_dispatch_success(monkeypatch):
    monkeypatch.setenv("GITHUB_DEPLOY_TOKEN", "t")

    class FakeResp:
        status_code = 204
        text = ""

    class FakeHTTP:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr(deploys.httpx, "AsyncClient", lambda **k: FakeHTTP())
    audits = []
    monkeypatch.setattr(deploys, "record_change", lambda **kw: audits.append(kw))
    out = await dispatch_deploy(environment="prod", git_ref="main",
                                actor_user_id="u1")
    assert out["dispatched"] is True
    assert audits[0]["action"] == "DEPLOY_TRIGGERED"
