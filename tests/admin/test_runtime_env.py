"""Tests for the runtime-environment indicator on the Environments screen.

The portal shares a process with the agent, so collect_runtime_env()
reports what the agent actually reads. The contract that matters: it must
identify which Supabase + environment is live, and it must NEVER leak a
secret value (keys collapse to set/unset, URLs are host-only)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


def test_resolved_environment_maps_raw_values(monkeypatch):
    from admin import _runtime_env

    for raw, expected in [
        ("production", "prod"), ("prod", "prod"),
        ("staging", "staging"), ("stage", "staging"),
        ("development", "staging"), ("", "staging"), ("anything", "staging"),
    ]:
        monkeypatch.setenv("ENVIRONMENT", raw)
        assert _runtime_env._resolved_environment() == expected


def test_host_only_strips_credentials_and_path():
    from admin._runtime_env import _host_only

    assert _host_only("postgresql://user:pass@db:5432/postgres") == "postgresql://db:5432"
    assert _host_only("https://ref.supabase.co/rest/v1") == "https://ref.supabase.co"
    assert _host_only("") == ""
    assert _host_only(None) == ""


def test_collect_masks_secret_values(monkeypatch):
    from admin import _runtime_env

    monkeypatch.setenv("SUPABASE_URL", "https://ref.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "TOPSECRET-service")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "TOPSECRET-anon")
    monkeypatch.setenv("LMNR_PROJECT_API_KEY", "TOPSECRET-lmnr")
    monkeypatch.setenv("GITHUB_DEPLOY_TOKEN", "TOPSECRET-gh")

    out = _runtime_env.collect_runtime_env()
    # presence flags, not values
    assert out["supabase"]["service_key_set"] is True
    assert out["supabase"]["anon_key_set"] is True
    assert out["observability"]["lmnr_api_key_set"] is True
    assert out["deploy"]["github_token_set"] is True
    # the host is shown, the secret never is
    assert out["supabase"]["url"] == "https://ref.supabase.co"
    blob = json.dumps(out)
    assert "TOPSECRET" not in blob


def test_collect_reports_cross_env_targets(monkeypatch):
    from admin import _runtime_env

    monkeypatch.setenv("STAGING_SUPABASE_URL", "https://staging.supabase.co")
    monkeypatch.setenv("STAGING_SUPABASE_SERVICE_KEY", "k")
    monkeypatch.delenv("PROD_SUPABASE_URL", raising=False)

    out = _runtime_env.collect_runtime_env()
    targets = {t["environment"]: t for t in out["cross_env_targets"]}
    assert "staging" in targets
    assert targets["staging"]["url"] == "https://staging.supabase.co"
    assert targets["staging"]["service_key_set"] is True
    assert "prod" not in targets  # unset → omitted


def test_collect_unset_supabase_is_labelled(monkeypatch):
    from admin import _runtime_env

    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    out = _runtime_env.collect_runtime_env()
    assert out["supabase"]["url"] == "(unset)"
    assert out["supabase"]["service_key_set"] is False
