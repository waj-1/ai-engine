"""Every admin MCP tool parameter carries a description on the wire.

Hosts show the JSON schema to the calling model; a bare `kind` / `source` /
`environment` is ambiguous across ~50 tools. Descriptions come from
`Annotated[..., Field(description=...)]` or, for the older signatures, from
`admin_mcp/param_docs.py` (applied at build time). A new parameter with
neither fails here, naming the tool and parameter.
"""
from __future__ import annotations

import pytest

from tests.admin_mcp.conftest import rpc


@pytest.fixture(scope="module")
def tools(mcp_client):
    resp = rpc(mcp_client, "tools/list")
    assert resp.status_code == 200
    return resp.json()["result"]["tools"]


def test_every_parameter_has_a_description(tools):
    missing = [f"{t['name']}.{p}"
               for t in tools
               for p, spec in (t["inputSchema"].get("properties") or {}).items()
               if not (spec.get("description") or "").strip()]
    assert not missing, f"parameters without a description: {missing}"


def test_env_means_the_config_branch_everywhere(tools):
    """One meaning per name: every `env` parameter is the config branch
    enum, never a Laminar tag or a row column."""
    for t in tools:
        spec = (t["inputSchema"].get("properties") or {}).get("env")
        if spec is None:
            continue
        assert set(spec.get("enum") or []) == {"staging", "prod"}, t["name"]
        assert "Config branch" in spec["description"], t["name"]


def test_renamed_env_args_keep_their_old_names(tools):
    by_name = {t["name"]: t["inputSchema"]["properties"] for t in tools}
    for tool, new in (("update_profile", "row_environment"),
                      ("deployment_targets", "row_environment"),
                      ("env_vars", "row_environment"),
                      ("live_analytics", "trace_env"),
                      ("caching_analytics", "trace_env")):
        assert new in by_name[tool], f"{tool} lacks {new}"
        assert "environment" in by_name[tool], f"{tool} dropped its old name"
        assert "DEPRECATED" in by_name[tool]["environment"]["description"]


def test_action_descriptions_list_the_choices(tools):
    by_name = {t["name"]: t["inputSchema"]["properties"] for t in tools}
    desc = by_name["prompts"]["action"]["description"]
    for action in ("get", "list", "diff", "publish_from_repo"):
        assert action in desc
