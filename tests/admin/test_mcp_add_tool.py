"""Authoring an HTTP tool INSIDE an internal MCP bundle:
  GET/POST /admin/agent-mcp-servers/{id}/add-tool

Creates an agent_tools (kind=http) row AND its agent_mcp_tools membership in one
step; refuses built-in / external MCPs; re-renders on validation error.
"""
import pytest
from starlette.testclient import TestClient
from fastapi import FastAPI


class _Q:
    def __init__(self, store, table):
        self.store, self.table, self._ins = store, table, None

    def select(self, *a):
        return self

    def eq(self, *a):
        return self

    def limit(self, *a):
        return self

    def order(self, *a):
        return self

    def insert(self, p):
        self._ins = p
        return self

    def execute(self):
        rows = self.store.setdefault(self.table, [])
        if self._ins is not None:
            r = dict(self._ins)
            if self.table == "agent_tools":
                r.setdefault("tool_id", f"tool-{len(rows) + 1}")
            elif self.table == "agent_mcp_servers":
                r.setdefault("mcp_server_id", f"srv-{len(rows) + 1}")
            rows.append(r)
            return type("R", (), {"data": [r]})()
        return type("R", (), {"data": list(rows)})()


class _Client:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        return _Q(self.store, name)


def _app(monkeypatch, store):
    import admin.mcp_manage_handlers as h
    from admin.router import router
    from admin import auth

    monkeypatch.setattr(h, "admin_service_client", lambda: _Client(store))
    monkeypatch.setattr(h, "record_change", lambda **k: None)
    monkeypatch.setattr("core.agent_profile.AgentProfileLoader.invalidate", lambda *a, **k: None)
    app = FastAPI()
    app.dependency_overrides[auth.require_superuser] = lambda: {"user_id": "u", "email": "e", "user_token": "t"}
    app.include_router(router)
    return TestClient(app)


def _bundle_store(**over):
    row = {"mcp_server_id": "b1", "name": "My Bundle", "source": "bundle",
           "is_builtin": False, "is_active": True}
    row.update(over)
    return {"agent_mcp_servers": [row]}


def _valid_tool_form():
    return {
        "python_name": "lookup_x", "display_name": "Lookup X",
        "description": "does a lookup", "endpoint_url": "https://api.example.com/x",
        "http_method": "POST", "scope": "read", "agent_kind": "sample_agent",
        "category": "http", "timeout_seconds": "30",
        "argument_schema": '{"type":"object","properties":{"q":{"type":"string"}},"required":["q"]}',
    }


def test_add_tool_get_renders_form_for_internal_bundle(monkeypatch):
    client = _app(monkeypatch, _bundle_store())
    r = client.get("/admin/agent-mcp-servers/b1/add-tool")
    assert r.status_code == 200
    assert 'name="endpoint_url"' in r.text
    assert "/admin/agent-mcp-servers/b1/add-tool" in r.text  # form posts back here
    assert "My Bundle" in r.text


def test_add_tool_post_creates_tool_and_membership(monkeypatch):
    store = _bundle_store()
    client = _app(monkeypatch, store)
    r = client.post("/admin/agent-mcp-servers/b1/add-tool",
                    data=_valid_tool_form(), follow_redirects=False)
    assert r.status_code == 303, r.text[:400]
    assert r.headers["location"] == "/admin/agent-mcp-servers/b1"
    tool = store["agent_tools"][0]
    assert tool["kind"] == "http" and tool["source_file"] == "__portal__"
    assert tool["python_name"] == "lookup_x"
    member = store["agent_mcp_tools"][0]
    assert member["mcp_server_id"] == "b1"
    assert member["tool_id"] == tool["tool_id"]
    assert member["position"] == 0


def test_add_tool_post_validation_error_rerenders_no_insert(monkeypatch):
    store = _bundle_store()
    client = _app(monkeypatch, store)
    form = _valid_tool_form()
    form["endpoint_url"] = "not-a-url"
    r = client.post("/admin/agent-mcp-servers/b1/add-tool",
                    data=form, follow_redirects=False)
    assert r.status_code == 400
    assert "endpoint_url" in r.text
    assert store.get("agent_tools", []) == []
    assert store.get("agent_mcp_tools", []) == []


def test_add_tool_refuses_builtin(monkeypatch):
    store = _bundle_store(mcp_server_id="bi", is_builtin=True)
    client = _app(monkeypatch, store)
    r = client.post("/admin/agent-mcp-servers/bi/add-tool",
                    data=_valid_tool_form(), follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/agent-mcp-servers/bi"
    assert store.get("agent_tools", []) == []


def test_add_tool_refuses_external(monkeypatch):
    store = {"agent_mcp_servers": [{"mcp_server_id": "e1", "name": "Ext",
                                    "source": "external", "is_builtin": False}]}
    client = _app(monkeypatch, store)
    r = client.get("/admin/agent-mcp-servers/e1/add-tool", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/agent-mcp-servers/e1"


def test_add_tool_post_refuses_external(monkeypatch):
    store = {"agent_mcp_servers": [{"mcp_server_id": "e1", "name": "Ext",
                                    "source": "external", "is_builtin": False}]}
    client = _app(monkeypatch, store)
    r = client.post("/admin/agent-mcp-servers/e1/add-tool",
                    data=_valid_tool_form(), follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/agent-mcp-servers/e1"
    assert store.get("agent_tools", []) == []
    assert store.get("agent_mcp_tools", []) == []


@pytest.fixture(autouse=True)
def _sample_kinds(product_build):
    """Generic logic, fictional agent kinds (tests/conftest.py::product_build)."""
