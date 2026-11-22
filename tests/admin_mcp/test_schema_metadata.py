"""Schema-level metadata MCP hosts key permission UX off: server
instructions (initialize), read-only / destructive tool annotations
(tools/list), and enum'd closed-set parameters (Literal → JSON-schema enum).

These are wire-level assertions through the mounted app — the same schemas
a real client sees — so a regression that drops an annotation or widens an
enum back to a bare string fails here, not in a host's permission dialog.
"""
from __future__ import annotations

import pytest

from tests.admin_mcp.conftest import rpc

# Every action these tools multiplex is a read — safe for hosts to
# auto-approve. Keep in sync with the annotations in admin_mcp/tools_*.py.
READ_ONLY_TOOLS = {
    "list_profiles", "get_profile", "diff_profiles", "mentionables",
    "eval_runs", "list_deploy_runs",
    "live_analytics", "caching_analytics", "traces", "search",
    "promotion_status", "voice_insights", "conversation_insights", "list_users",
}

# promote_profile ships config to prod: destructive even though it now
# dry-runs by default (the annotation is about what confirm=true does).
DESTRUCTIVE_TOOLS = {"deploy", "rollback", "promote_profile"}

# Action-multiplexed tools mixing reads with writes must NOT claim
# read-only — hosts would auto-approve their destructive actions too.
MIXED_TOOLS = {"models", "tools", "mcp_servers", "kb_sources", "env_vars",
               "test_fixtures", "channels", "memory", "eval_suites",
               "eval_schedules", "deployment_targets", "jobs", "audit",
               "templates", "prompts", "profile_mcp_attachments",
               "users", "profile_attachments", "skills", "profile_versions",
               "chat", "prompt_optimize", "api_keys", "agent_types"}


def test_users_is_not_annotated_destructive(tools_by_name):
    """`users` multiplexes a read (list) with a write (set_superuser): a
    destructiveHint made every plain listing trip the host's destructive-
    action prompt. It is unannotated (mixed) now; list_users is the
    read-only twin."""
    ann = tools_by_name["users"].get("annotations") or {}
    assert not ann.get("destructiveHint")
    assert (tools_by_name["list_users"].get("annotations") or {}).get("readOnlyHint")


@pytest.fixture(scope="module")
def tools_by_name(mcp_client):
    resp = rpc(mcp_client, "tools/list")
    assert resp.status_code == 200
    return {t["name"]: t for t in resp.json()["result"]["tools"]}


def test_initialize_carries_instructions(mcp_client):
    resp = rpc(mcp_client, "initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"},
    })
    instructions = resp.json()["result"].get("instructions") or ""
    # The cross-tool conventions the tools' own descriptions rely on.
    assert "confirm=true" in instructions
    assert 'env="prod"' in instructions
    assert "jobs action=get" in instructions


def test_read_only_tools_are_annotated(tools_by_name):
    missing = {name for name in READ_ONLY_TOOLS
               if not (tools_by_name[name].get("annotations") or {}).get("readOnlyHint")}
    assert not missing, f"read-only tools missing readOnlyHint: {sorted(missing)}"


def test_destructive_tools_are_annotated(tools_by_name):
    for name in DESTRUCTIVE_TOOLS:
        ann = tools_by_name[name].get("annotations") or {}
        assert ann.get("destructiveHint") is True, f"{name} missing destructiveHint"
        assert not ann.get("readOnlyHint"), f"{name} must not claim read-only"


def test_mixed_tools_never_claim_read_only(tools_by_name):
    liars = {name for name in MIXED_TOOLS
             if (tools_by_name[name].get("annotations") or {}).get("readOnlyHint")}
    assert not liars, f"mixed read/write tools claiming read-only: {sorted(liars)}"


@pytest.mark.parametrize("tool_name, expected_actions", [
    ("traces", {"search", "get", "span", "transcript", "overview",
                "session", "audio"}),
    ("audit", {"list", "get", "probe_write", "verify"}),
    ("api_keys", {"list", "create", "revoke"}),
    ("profile_attachments", {"list", "add", "remove"}),
    ("models", {"list", "get", "create", "update", "promote"}),
    ("env_vars", {"list", "get", "create", "update", "delete"}),
    ("test_fixtures", {"list", "create", "update", "delete", "export"}),
    ("mcp_servers", {"list", "get", "create", "update", "delete",
                     "members", "discover"}),
])
def test_action_params_are_enums(tools_by_name, tool_name, expected_actions):
    props = tools_by_name[tool_name]["inputSchema"]["properties"]
    assert set(props["action"].get("enum") or []) == expected_actions


def test_env_param_is_enum(tools_by_name):
    props = tools_by_name["list_profiles"]["inputSchema"]["properties"]
    assert set(props["env"].get("enum") or []) == {"staging", "prod"}


def test_deploy_environment_is_prod_only(tools_by_name):
    spec = tools_by_name["deploy"]["inputSchema"]["properties"]["environment"]
    # A single-value Literal serializes as const OR a one-element enum
    # depending on the pydantic version — accept either.
    allowed = set(spec.get("enum") or ([spec["const"]] if "const" in spec else []))
    assert allowed == {"prod"}
