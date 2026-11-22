"""Tests for the Task 13 ship-domain tools: promote (eval-gate aware),
deploy/rollback, list_deploy_runs, deployment_targets, env_vars. Same
FakeClient pattern as test_tools_configure.py / test_tools_test.py."""
from __future__ import annotations

import admin_mcp.tools_ship as ts
from tests.admin_mcp.conftest import FakeClient

TARGET = {"target_id": "t1", "environment": "staging", "label": "primary",
          "base_url": "https://staging.example.com", "auth_env_var": None,
          "description": None, "is_default": True, "is_active": True}

VAR_PLAIN = {"var_id": "v1", "environment": "staging", "name": "FEATURE_X",
             "value": "on", "source": "db", "description": None,
             "is_secret": False}

VAR_SECRET = {"var_id": "v2", "environment": "staging", "name": "API_KEY",
              "value": "s3cret", "source": "db", "description": None,
              "is_secret": True}


def _fake(monkeypatch, tables):
    client = FakeClient(tables)
    monkeypatch.setattr(ts, "client_for_env", lambda env: client)
    return client


def _noaudit(monkeypatch):
    monkeypatch.setattr(ts, "actor_audit", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# promote_profile
# ---------------------------------------------------------------------------


def test_promote_profile_surfaces_eval_gate_dict(monkeypatch):
    import admin.services.promotion as promotion

    gate_detail = {"error": "eval_gate_failed", "score": 0.4, "threshold": 0.7,
                   "suite_name": "Business smoke", "hint": "re-run evals"}

    def fake_promote(**kw):
        raise promotion.EvalGateFailed(422, gate_detail)

    monkeypatch.setattr(promotion, "promote", fake_promote)

    out = ts.promote_profile(profile_id="p1", target_env="prod", confirm=True)

    assert out == gate_detail


def test_promote_profile_not_found(monkeypatch):
    import admin.services.promotion as promotion

    def fake_promote(**kw):
        raise promotion.ProfileNotFound(404, "profile not found")

    monkeypatch.setattr(promotion, "promote", fake_promote)

    out = ts.promote_profile(profile_id="does-not-exist", target_env="prod",
                             confirm=True)
    assert "error" in out
    assert "not found" in out["error"]


def test_promote_profile_other_promotion_error(monkeypatch):
    import admin.services.promotion as promotion

    def fake_promote(**kw):
        raise promotion.SnapshotWriteFailed(502, "snapshot writer blew up")

    monkeypatch.setattr(promotion, "promote", fake_promote)

    out = ts.promote_profile(profile_id="p1", target_env="prod", confirm=True)
    assert out["error"] == "snapshot writer blew up"
    assert out["code"] == 502


def test_promote_profile_force_without_confirm(monkeypatch):
    monkeypatch.setattr(ts, "promote_preview", lambda *a, **k: {
        "display_name": "Biz", "agent_kind": "business", "slug": "main",
        "from_env": "staging", "changes": ["x"], "blockers": []})
    out = ts.promote_profile(profile_id="p1", target_env="prod", force=True)
    assert out["confirmed"] is False
    assert out["would"].startswith("force-promote")


def test_promote_profile_invalid_target_env():
    out = ts.promote_profile(profile_id="p", target_env="production")
    assert "error" in out
    assert "staging" in out["error"]
    assert "prod" in out["error"]


def test_promote_profile_happy_path(monkeypatch):
    import admin.services.promotion as promotion

    seen = {}

    def fake_promote(**kw):
        seen.update(kw)
        return {"profile_id": "p1", "source_profile_id": "p1",
                "target_env": "prod", "forced": False, "cross_env": True,
                "eval_gate": {"status": "pass"}}

    monkeypatch.setattr(promotion, "promote", fake_promote)

    out = ts.promote_profile(profile_id="p1", target_env="prod", env="staging",
                             confirm=True)

    assert out["promoted"] is True
    assert out["target_env"] == "prod"
    assert seen["profile_id"] == "p1"
    assert seen["target_env"] == "prod"
    assert seen["force"] is False


# ---------------------------------------------------------------------------
# deploy / rollback
# ---------------------------------------------------------------------------


async def test_deploy_without_confirm():
    out = await ts.deploy(environment="staging", git_ref="main")
    assert out["confirmed"] is False


async def test_deploy_happy_path(monkeypatch):
    import admin.services.deploys as deploys

    seen = {}

    async def fake_dispatch_deploy(**kw):
        seen.update(kw)
        return {"dispatched": True, "environment": kw["environment"],
                "git_ref": kw["git_ref"], "workflow": "deploy-agent-from-portal.yaml",
                "repo": "example-org/ai-engine"}

    monkeypatch.setattr(deploys, "dispatch_deploy", fake_dispatch_deploy)

    out = await ts.deploy(environment="staging", git_ref="main", confirm=True)

    assert out["dispatched"] is True
    assert seen["environment"] == "staging"
    assert seen["git_ref"] == "main"


async def test_deploy_dispatch_error(monkeypatch):
    import admin.services.deploys as deploys

    async def fake_dispatch_deploy(**kw):
        raise deploys.DeployError(400, "GITHUB_DEPLOY_TOKEN is not configured.")

    monkeypatch.setattr(deploys, "dispatch_deploy", fake_dispatch_deploy)

    out = await ts.deploy(environment="staging", git_ref="main", confirm=True)
    assert out["error"] == "GITHUB_DEPLOY_TOKEN is not configured."
    assert out["code"] == 400


async def test_rollback_without_confirm():
    out = await ts.rollback(environment="prod", target_revision="ai-engine-00042-abc")
    assert out["confirmed"] is False


async def test_rollback_happy_path(monkeypatch):
    import admin.services.deploys as deploys

    async def fake_dispatch_rollback(**kw):
        return {"dispatched": True, "environment": kw["environment"],
                "target_revision": kw["target_revision"]}

    monkeypatch.setattr(deploys, "dispatch_rollback", fake_dispatch_rollback)

    out = await ts.rollback(environment="prod", target_revision="ai-engine-00042-abc",
                            confirm=True)
    assert out["dispatched"] is True
    assert out["target_revision"] == "ai-engine-00042-abc"


# ---------------------------------------------------------------------------
# list_deploy_runs
# ---------------------------------------------------------------------------


async def test_list_deploy_runs_filters_by_target_env(monkeypatch):
    import admin.deploy_handlers as dh

    runs = [
        {"run_id": 1, "target_env": "staging", "status": "completed"},
        {"run_id": 2, "target_env": "prod", "status": "completed"},
        {"run_id": 3, "target_env": None, "status": "in_progress"},
    ]

    async def fake_fetch(*, limit):
        return runs

    monkeypatch.setattr(dh, "_fetch_recent_deploy_runs", fake_fetch)

    out = await ts.list_deploy_runs(environment="staging")

    run_ids = {r["run_id"] for r in out["runs"]}
    # Matches the target env exactly, plus ambiguous (None) runs.
    assert run_ids == {1, 3}


async def test_list_deploy_runs_no_filter_returns_all(monkeypatch):
    import admin.deploy_handlers as dh

    runs = [{"run_id": 1, "target_env": "staging"}, {"run_id": 2, "target_env": "prod"}]

    async def fake_fetch(*, limit):
        return runs

    monkeypatch.setattr(dh, "_fetch_recent_deploy_runs", fake_fetch)

    out = await ts.list_deploy_runs()
    assert len(out["runs"]) == 2


# ---------------------------------------------------------------------------
# deployment_targets
# ---------------------------------------------------------------------------


def test_deployment_targets_list(monkeypatch):
    _fake(monkeypatch, {"agent_deployment_targets": [dict(TARGET)]})
    out = ts.deployment_targets(action="list")
    assert out["targets"][0]["target_id"] == "t1"


def test_deployment_targets_create_requires_label_and_base_url(monkeypatch):
    _fake(monkeypatch, {"agent_deployment_targets": []})
    out = ts.deployment_targets(action="create", environment="staging", label="",
                                base_url="")
    assert "error" in out


def test_deployment_targets_create_rejects_duplicate(monkeypatch):
    _fake(monkeypatch, {"agent_deployment_targets": [dict(TARGET)]})
    out = ts.deployment_targets(action="create", environment="staging",
                                label="primary", base_url="https://x.example.com")
    assert "error" in out
    assert "already exists" in out["error"]


def test_deployment_targets_create_success_clears_sibling_default(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_deployment_targets": [dict(TARGET)]})
    out = ts.deployment_targets(action="create", environment="staging",
                                label="secondary", base_url="https://y.example.com",
                                is_default=True)
    assert out["created"] is True
    rows = client.table("agent_deployment_targets")._data
    primary = next(r for r in rows if r["label"] == "primary")
    secondary = next(r for r in rows if r["label"] == "secondary")
    assert primary["is_default"] is False
    assert secondary["is_default"] is True


def test_deployment_targets_delete_without_confirm(monkeypatch):
    _fake(monkeypatch, {"agent_deployment_targets": [dict(TARGET)]})
    out = ts.deployment_targets(action="delete", target_id="t1")
    assert out["confirmed"] is False


def test_deployment_targets_delete_with_confirm(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_deployment_targets": [dict(TARGET)]})
    out = ts.deployment_targets(action="delete", target_id="t1", confirm=True)
    assert out["deleted"] is True
    assert client.table("agent_deployment_targets")._data == []


def test_deployment_targets_delete_not_found(monkeypatch):
    _fake(monkeypatch, {"agent_deployment_targets": []})
    out = ts.deployment_targets(action="delete", target_id="nope", confirm=True)
    assert "error" in out


def test_deployment_targets_update_no_fields(monkeypatch):
    _fake(monkeypatch, {"agent_deployment_targets": [dict(TARGET)]})
    out = ts.deployment_targets(action="update", target_id="t1")
    assert "error" in out


def test_deployment_targets_unknown_action(monkeypatch):
    _fake(monkeypatch, {})
    out = ts.deployment_targets(action="bogus")
    assert "error" in out


# ---------------------------------------------------------------------------
# env_vars
# ---------------------------------------------------------------------------


def test_env_vars_list_masks_secrets(monkeypatch):
    _fake(monkeypatch, {"agent_env_vars": [dict(VAR_PLAIN), dict(VAR_SECRET)]})
    out = ts.env_vars(action="list")
    by_id = {v["var_id"]: v for v in out["vars"]}
    assert by_id["v1"]["value"] == "on"
    assert by_id["v2"]["value"] == "••••"
    assert by_id["v2"]["_masked"] is True


def test_env_vars_get_masks_by_default(monkeypatch):
    _fake(monkeypatch, {"agent_env_vars": [dict(VAR_SECRET)]})
    out = ts.env_vars(action="get", var_id="v2")
    assert out["var"]["value"] == "••••"


def test_env_vars_get_reveal_returns_real_value_and_audits(monkeypatch):
    audits = []
    monkeypatch.setattr(ts, "actor_audit",
                        lambda *a, **k: audits.append((a, k)))
    _fake(monkeypatch, {"agent_env_vars": [dict(VAR_SECRET)]})

    out = ts.env_vars(action="get", var_id="v2", reveal=True)

    assert out["var"]["value"] == "s3cret"
    assert len(audits) == 1
    args, kwargs = audits[0]
    assert args[:3] == ("agent_env_vars", "v2", "reveal")
    assert kwargs["after"] == {"name": "API_KEY"}


def test_env_vars_get_not_found(monkeypatch):
    _fake(monkeypatch, {"agent_env_vars": []})
    out = ts.env_vars(action="get", var_id="nope")
    assert "error" in out


def test_env_vars_create_success(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_env_vars": []})
    out = ts.env_vars(action="create", environment="staging", name="NEW_FLAG",
                      value="1", is_secret=False)
    assert out["created"] is True
    inserted = client.table("agent_env_vars").inserted[0]
    assert inserted["name"] == "NEW_FLAG"
    assert inserted["source"] == "db"


def test_env_vars_create_masks_secret_in_audit(monkeypatch):
    audits = []
    monkeypatch.setattr(ts, "actor_audit",
                        lambda *a, **k: audits.append((a, k)))
    _fake(monkeypatch, {"agent_env_vars": []})

    ts.env_vars(action="create", environment="staging", name="SECRET_KEY",
               value="topsecret", is_secret=True)

    args, kwargs = audits[0]
    assert kwargs["after"]["value"] == "••••"


def test_env_vars_create_requires_name(monkeypatch):
    _fake(monkeypatch, {"agent_env_vars": []})
    out = ts.env_vars(action="create", environment="staging", name="")
    assert "error" in out


def test_env_vars_update_can_clear_value(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_env_vars": [dict(VAR_PLAIN)]})
    out = ts.env_vars(action="update", var_id="v1", value="")
    assert out["updated"] is True
    assert client.table("agent_env_vars")._data[0]["value"] is None


def test_env_vars_update_unmark_secret_masks_audit(monkeypatch):
    """Regression for the critical leak: updating a secret row with
    is_secret=False and no new value must NOT let the carried-over
    plaintext value reach the audit log."""
    audits = []
    monkeypatch.setattr(ts, "actor_audit",
                        lambda *a, **k: audits.append((a, k)))
    _fake(monkeypatch, {"agent_env_vars": [dict(VAR_SECRET)]})

    out = ts.env_vars(action="update", var_id="v2", is_secret=False)

    assert out["updated"] is True
    args, kwargs = audits[0]
    before = kwargs["before"]
    after = kwargs["after"]
    assert "s3cret" not in repr(before)
    assert "s3cret" not in repr(after)
    assert before["value"] == "••••"
    assert after["value"] == "••••"


def test_env_vars_create_rejects_bad_environment(monkeypatch):
    _fake(monkeypatch, {"agent_env_vars": []})
    out = ts.env_vars(action="create", environment="qa", name="X")
    assert "error" in out
    assert "staging" in out["error"]
    assert "prod" in out["error"]


def test_env_vars_update_rejects_bad_environment(monkeypatch):
    _fake(monkeypatch, {"agent_env_vars": [dict(VAR_PLAIN)]})
    out = ts.env_vars(action="update", var_id="v1", environment="qa")
    assert "error" in out
    assert "staging" in out["error"]
    assert "prod" in out["error"]


def test_deployment_targets_create_rejects_bad_environment(monkeypatch):
    _fake(monkeypatch, {"agent_deployment_targets": []})
    out = ts.deployment_targets(action="create", environment="qa", label="x",
                                base_url="https://x.example.com")
    assert "error" in out
    assert "staging" in out["error"]
    assert "prod" in out["error"]


def test_deployment_targets_update_rejects_bad_environment(monkeypatch):
    _fake(monkeypatch, {"agent_deployment_targets": [dict(TARGET)]})
    out = ts.deployment_targets(action="update", target_id="t1", environment="qa")
    assert "error" in out
    assert "staging" in out["error"]
    assert "prod" in out["error"]


def test_env_vars_delete_without_confirm(monkeypatch):
    _fake(monkeypatch, {"agent_env_vars": [dict(VAR_PLAIN)]})
    out = ts.env_vars(action="delete", var_id="v1")
    assert out["confirmed"] is False


def test_env_vars_delete_with_confirm(monkeypatch):
    _noaudit(monkeypatch)
    client = _fake(monkeypatch, {"agent_env_vars": [dict(VAR_PLAIN)]})
    out = ts.env_vars(action="delete", var_id="v1", confirm=True)
    assert out["deleted"] is True
    assert client.table("agent_env_vars")._data == []


def test_env_vars_unknown_action(monkeypatch):
    _fake(monkeypatch, {})
    out = ts.env_vars(action="bogus")
    assert "error" in out
