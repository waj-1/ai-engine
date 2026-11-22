"""Single-dashboard fixes: cross-env refs, env screen, channels seed, switcher.

One control-plane dashboard manages BOTH environments. These tests pin the
fixes for what that surfaced: promoted rows referencing source-env profile
ids (sub-agents invisible, downstream boxes unticked), the Environments
screen reading only one branch DB ("no profiles in staging"), the runtime
card checking the wrong cross-env var names, and the empty channel registry.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from admin.screens.agents import resolve_cross_env_refs


def _profile(pid, kind, env="prod", status="active", parent=None, ds=None):
    return {
        "profile_id": pid, "agent_kind": kind, "environment": env,
        "status": status, "parent_profile_id": parent,
        "downstream_profile_ids": ds or [],
    }


def _client_with_kinds(kind_by_id):
    client = MagicMock()
    chain = client.table.return_value.select.return_value.in_.return_value
    chain.execute.return_value.data = [
        {"profile_id": k, "agent_kind": v} for k, v in kind_by_id.items()
    ]
    return client


def test_cross_env_parent_and_downstream_resolve_to_env_siblings():
    profiles = [
        _profile("p-biz", "business", ds=["dev-title", "dev-sugg"]),
        _profile("p-title", "business_title", parent="dev-biz"),
        _profile("p-sugg", "business_suggestions", parent="dev-biz"),
    ]
    client = _client_with_kinds({
        "dev-biz": "business", "dev-title": "business_title", "dev-sugg": "business_suggestions",
    })
    resolve_cross_env_refs(profiles, client)
    # Sub-agents now hang off the prod parent → they render in the rail.
    assert profiles[1]["parent_profile_id"] == "p-biz"
    assert profiles[2]["parent_profile_id"] == "p-biz"
    # Downstream ids now match the env's candidate rows → checkboxes tick.
    assert profiles[0]["downstream_profile_ids"] == ["p-title", "p-sugg"]


def test_unresolvable_refs_are_left_untouched():
    profiles = [_profile("p1", "business", ds=["ghost-id"])]
    client = _client_with_kinds({})  # referenced row no longer exists
    resolve_cross_env_refs(profiles, client)
    assert profiles[0]["downstream_profile_ids"] == ["ghost-id"]


def test_same_env_refs_need_no_lookup():
    profiles = [
        _profile("p1", "business", ds=["p2"]),
        _profile("p2", "business_title", parent="p1"),
    ]
    client = MagicMock()
    resolve_cross_env_refs(profiles, client)
    client.table.assert_not_called()
    assert profiles[0]["downstream_profile_ids"] == ["p2"]


def test_runtime_env_recognises_ai_prefixed_cross_env_vars():
    from admin._runtime_env import collect_runtime_env

    with patch.dict(os.environ, {
        "STAGING_AI_SUPABASE_URL": "https://stg.supabase.co",
        "STAGING_AI_SUPABASE_SERVICE_KEY": "k1",
        "PROD_AI_SUPABASE_URL": "https://prd.supabase.co",
        "PROD_AI_SUPABASE_SERVICE_KEY": "k2",
    }):
        re_ = collect_runtime_env()
    envs = {t["environment"]: t for t in re_["cross_env_targets"]}
    assert set(envs) == {"staging", "prod"}
    assert all(t["service_key_set"] for t in envs.values())


def test_seed_channels_is_insert_only():
    from admin import seed_channels

    defaults = [
        {"slug": "web", "name": "Web", "kind": "embed", "allowed_agent_kinds": []},
        {"slug": "api", "name": "API", "kind": "public_api", "allowed_agent_kinds": []},
    ]
    client = MagicMock()
    client.table.return_value.select.return_value.execute.return_value.data = [
        {"slug": "web"},  # operator-touched row must not be re-written
    ]
    inserted_payloads = []
    client.table.return_value.insert.side_effect = lambda row: (
        inserted_payloads.append(row), MagicMock())[1]
    with patch.object(seed_channels, "get_service_client", return_value=client), \
            patch.object(seed_channels, "default_channels", return_value=defaults):
        out = seed_channels.run()
    assert out == {"inserted": 1, "skipped": 1}
    assert [r["slug"] for r in inserted_payloads] == ["api"]


def test_seed_channels_without_defaults_touches_nothing():
    from admin import seed_channels

    with patch.object(seed_channels, "get_service_client") as get_client, \
            patch.object(seed_channels, "default_channels", return_value=[]):
        assert seed_channels.run() == {"inserted": 0, "skipped": 0}
    get_client.assert_not_called()


def test_global_screens_hide_the_env_switcher():
    from pathlib import Path
    base = Path("admin/templates/_base.html").read_text()
    assert "{% if not hide_env_switcher %}" in base
    router_src = Path("admin/router.py").read_text()
    for screen in ('"active": "docs"', '"active": "users"', '"active": "audit"',
                   '"active": "deployments"', '"active": "env"'):
        chunks = router_src.split(screen)[1:]
        assert chunks, f"no context dicts found for {screen}"
        for chunk in chunks:
            assert "hide_env_switcher" in chunk[:220], (screen, chunk[:120])
