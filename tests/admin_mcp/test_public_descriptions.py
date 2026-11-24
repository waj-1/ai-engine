"""What any MCP client of the engine reads must describe the engine, not one
deployment's infrastructure: no private database, cloud, CI or tracing
vendor names, no internal ticket numbers. And the tools that only work
against a GitHub deploy workflow are not offered where none is configured.
"""
from __future__ import annotations

import re

import pytest
from mcp.server.fastmcp import FastMCP

from tests.admin_mcp.conftest import rpc

# Each was in a tool description or parameter description before 2026-11-23.
_PRIVATE = re.compile(
    r"Cloud Run|GitHub Actions|Laminar|ClickHouse|Container App|"
    r"WAJ\sDB|merchant id|CLAUDE\.md|#\d{3,5}\b",
    re.I,
)


@pytest.fixture(scope="module")
def listing(mcp_client):
    resp = rpc(mcp_client, "tools/list")
    assert resp.status_code == 200
    return resp.json()["result"]["tools"]


def _texts(tool):
    yield "description", tool.get("description") or ""
    for name, spec in ((tool.get("inputSchema") or {}).get("properties") or {}).items():
        yield f"param {name}", spec.get("description") or ""


def test_no_tool_or_parameter_description_names_private_infrastructure(listing):
    found = [(t["name"], where, m.group(0))
             for t in listing for where, text in _texts(t)
             for m in _PRIVATE.finditer(text)]
    assert found == []


def test_the_server_instructions_name_no_tracing_vendor(mcp_client):
    resp = rpc(mcp_client, "initialize", {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"}})
    instructions = resp.json()["result"].get("instructions") or ""
    assert instructions and not _PRIVATE.search(instructions)


def _server_with(*names):
    server = FastMCP("t")
    for name in names:
        server.add_tool(lambda: None, name=name)
    return server


def test_deploy_workflow_tools_are_not_offered_without_a_deploy_repository(monkeypatch):
    from admin_mcp import tools_ship

    monkeypatch.delenv("GITHUB_DEPLOY_REPO", raising=False)
    monkeypatch.setattr("engine_plugins.PLUGINS", ())  # no plugin supplies one
    server = _server_with("deploy", "rollback", "list_deploy_runs", "deployment_targets")
    removed = tools_ship.drop_unconfigured_deploy_tools(server)
    assert sorted(removed) == ["deploy", "list_deploy_runs", "rollback"]
    assert [t.name for t in server._tool_manager.list_tools()] == ["deployment_targets"]


def test_deploy_workflow_tools_stay_when_a_repository_is_configured(monkeypatch):
    from admin_mcp import tools_ship

    monkeypatch.setenv("GITHUB_DEPLOY_REPO", "example-org/engine")
    server = _server_with("deploy", "rollback", "list_deploy_runs")
    assert tools_ship.drop_unconfigured_deploy_tools(server) == []
    assert len(server._tool_manager.list_tools()) == 3


def test_the_server_build_drops_them(monkeypatch):
    """build_mcp_asgi_app is where the drop happens (a server built without
    it would still list the tools)."""
    import inspect

    from admin_mcp import server

    assert "drop_unconfigured_deploy_tools(mcp)" in inspect.getsource(server.build_mcp_asgi_app)
