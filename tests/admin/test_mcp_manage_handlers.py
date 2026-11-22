"""POST /admin/mcp/connect inserts an external agent_mcp_servers row."""
from starlette.testclient import TestClient
from fastapi import FastAPI


class _Q:
    def __init__(self, store, table):
        self.store, self.table, self._ins = store, table, None
    def select(self, *a): return self
    def eq(self, *a): return self
    def limit(self, *a): return self
    def order(self, *a): return self
    def insert(self, p): self._ins = p; return self
    def execute(self):
        rows = self.store.setdefault(self.table, [])
        if self._ins is not None:
            r = dict(self._ins); r.setdefault("mcp_server_id", f"srv-{len(rows)+1}")
            rows.append(r); return type("R", (), {"data": [r]})()
        return type("R", (), {"data": list(rows)})()


class _Client:
    def __init__(self, store): self.store = store
    def table(self, name): return _Q(self.store, name)


def _app(monkeypatch, store):
    import admin.mcp_manage_handlers as h
    from admin.router import router
    from admin import auth
    monkeypatch.setattr(h, "admin_service_client", lambda: _Client(store))
    monkeypatch.setattr(h, "record_change", lambda **k: None)
    app = FastAPI()
    app.dependency_overrides[auth.require_superuser] = lambda: {"user_id": "u", "email": "e", "user_token": "t"}
    app.include_router(router)
    return TestClient(app)


def test_connect_inserts_external_row(monkeypatch):
    store = {}
    client = _app(monkeypatch, store)
    r = client.post("/admin/mcp/connect", data={
        "name": "Orchard Prod", "transport": "http", "endpoint": "https://mcp.partner.example/mcp",
        "auth_env_var": "ORCHARD_MCP_TOKEN", "tool_subset": "get-sales-summary, login",
        "context_fields": '{"token": "$ctx.user_token"}'}, follow_redirects=False)
    assert r.status_code in (302, 303)
    row = store["agent_mcp_servers"][0]
    assert row["source"] == "external" and row["is_builtin"] is False
    assert row["endpoint"] == "https://mcp.partner.example/mcp"
    assert row["tool_subset"] == ["get-sales-summary", "login"]
    assert row["context_fields"] == {"token": "$ctx.user_token"}


def test_connect_rejects_bad_context_fields(monkeypatch):
    store = {}
    client = _app(monkeypatch, store)
    r = client.post("/admin/mcp/connect", data={
        "name": "X", "transport": "http", "endpoint": "https://x.test/mcp",
        "context_fields": "not json"}, follow_redirects=False)
    assert r.status_code == 200  # re-render with error, not a redirect
    assert "context_fields" in r.text.lower()
    assert store.get("agent_mcp_servers", []) == []  # nothing inserted


class _RaisingQ(_Q):
    """Like _Q, but the agent_mcp_servers insert raises a PostgREST-style
    unique-violation instead of storing the row."""
    def execute(self):
        if self._ins is not None and self.table == "agent_mcp_servers":
            raise Exception(
                'duplicate key value violates unique constraint '
                '"agent_mcp_servers_name_key"')
        return super().execute()


class _RaisingClient:
    def __init__(self, store): self.store = store
    def table(self, name): return _RaisingQ(self.store, name)


def test_connect_duplicate_name_rerenders_not_500(monkeypatch):
    import admin.mcp_manage_handlers as h
    from admin.router import router
    from admin import auth
    store = {}
    monkeypatch.setattr(h, "admin_service_client", lambda: _RaisingClient(store))
    monkeypatch.setattr(h, "record_change", lambda **k: None)
    app = FastAPI()
    app.dependency_overrides[auth.require_superuser] = lambda: {"user_id": "u", "email": "e", "user_token": "t"}
    app.include_router(router)
    client = TestClient(app)
    r = client.post("/admin/mcp/connect", data={
        "name": "Orchard Prod", "transport": "http", "endpoint": "https://x.test/mcp"},
        follow_redirects=False)
    assert r.status_code == 200  # re-render, not a 500
    assert "already exists" in r.text.lower() or "orchard prod" in r.text.lower()
    assert store.get("agent_mcp_servers", []) == []  # nothing stored


def test_connect_rejects_builtin_prefix(monkeypatch):
    store = {}
    client = _app(monkeypatch, store)
    r = client.post("/admin/mcp/connect", data={
        "name": "__builtin__:x", "transport": "http", "endpoint": "https://x.test/mcp"},
        follow_redirects=False)
    assert r.status_code == 200  # re-render with error, not a redirect
    assert "reserved" in r.text.lower() or "built-in" in r.text.lower() or "builtin" in r.text.lower()
    assert store.get("agent_mcp_servers", []) == []  # nothing inserted


# ---------------------------------------------------------------------------
# POST /admin/mcp/create — bundle MCP + agent_mcp_tools membership
# ---------------------------------------------------------------------------


def test_create_bundle_inserts_server_and_membership(monkeypatch):
    store = {"agent_tools": [
        {"tool_id": "t1", "python_name": "get_clients", "kind": "python", "is_deprecated": False},
        {"tool_id": "t2", "python_name": "crm_http", "kind": "http", "is_deprecated": False}]}
    client = _app(monkeypatch, store)
    r = client.post("/admin/mcp/create", data={
        "name": "Sales Tools", "usage_guidance": "Use for sales data.",
        "context_fields": "{}", "member_tool_ids": ["t1", "t2"]}, follow_redirects=False)
    assert r.status_code in (302, 303)
    srv = store["agent_mcp_servers"][0]
    assert srv["source"] == "bundle" and srv["is_builtin"] is False
    members = sorted(store["agent_mcp_tools"], key=lambda m: m["position"])
    assert [m["tool_id"] for m in members] == ["t1", "t2"]
    assert members[0]["position"] == 0 and members[1]["position"] == 1
    assert r.headers["location"] == f"/admin/agent-mcp-servers/{srv['mcp_server_id']}"


def test_create_get_renders_member_picker(monkeypatch):
    store = {"agent_tools": [
        {"tool_id": "t1", "python_name": "get_clients", "kind": "python",
         "agent_kind": "business", "is_deprecated": False}]}
    client = _app(monkeypatch, store)
    r = client.get("/admin/mcp/create")
    assert r.status_code == 200
    assert "get_clients" in r.text
    assert "member_tool_ids" in r.text


def test_create_rejects_empty_name(monkeypatch):
    store = {"agent_tools": []}
    client = _app(monkeypatch, store)
    r = client.post("/admin/mcp/create", data={
        "name": "", "context_fields": "{}"}, follow_redirects=False)
    assert r.status_code == 200  # re-render with error, not a redirect
    assert "name is required" in r.text.lower()
    assert store.get("agent_mcp_servers", []) == []
    assert store.get("agent_mcp_tools", []) == []


def test_create_rejects_builtin_prefix(monkeypatch):
    store = {"agent_tools": []}
    client = _app(monkeypatch, store)
    r = client.post("/admin/mcp/create", data={
        "name": "__builtin__:sneaky", "context_fields": "{}"}, follow_redirects=False)
    assert r.status_code == 200
    assert "reserved" in r.text.lower() or "built-in" in r.text.lower() or "builtin" in r.text.lower()
    assert store.get("agent_mcp_servers", []) == []


def test_create_bundle_rejects_unknown_member_id(monkeypatch):
    # Only t1 is a valid, non-deprecated tool; "forged-id" is not in the picker.
    store = {"agent_tools": [
        {"tool_id": "t1", "python_name": "get_clients", "kind": "python", "is_deprecated": False}]}
    client = _app(monkeypatch, store)
    r = client.post("/admin/mcp/create", data={
        "name": "Sales Tools", "context_fields": "{}",
        "member_tool_ids": ["t1", "forged-id"]}, follow_redirects=False)
    assert r.status_code == 200  # re-render with error, not a redirect
    body = r.text.lower()
    assert "unavailable" in body or "deprecated" in body
    # Validation happens before any write — nothing persisted in either table.
    assert store.get("agent_mcp_servers", []) == []
    assert store.get("agent_mcp_tools", []) == []


def test_create_rejects_bad_context_fields(monkeypatch):
    store = {"agent_tools": []}
    client = _app(monkeypatch, store)
    r = client.post("/admin/mcp/create", data={
        "name": "Sales Tools", "context_fields": "not json",
        "member_tool_ids": ["t1"]}, follow_redirects=False)
    assert r.status_code == 200  # re-render with error, not a redirect
    assert "context_fields" in r.text.lower()
    assert store.get("agent_mcp_servers", []) == []
    assert store.get("agent_mcp_tools", []) == []


class _CreateRaisingQ(_Q):
    """Like _Q, but the agent_mcp_servers insert raises a PostgREST-style
    unique-violation instead of storing the row."""
    def execute(self):
        if self._ins is not None and self.table == "agent_mcp_servers":
            raise Exception(
                'duplicate key value violates unique constraint '
                '"agent_mcp_servers_name_key"')
        return super().execute()


def test_create_duplicate_name_rerenders_not_500(monkeypatch):
    import admin.mcp_manage_handlers as h
    from admin.router import router
    from admin import auth
    # t1 must be a valid member so member-id validation passes and we actually
    # reach the (raising) agent_mcp_servers insert this test is about.
    store = {"agent_tools": [
        {"tool_id": "t1", "python_name": "get_clients", "kind": "python", "is_deprecated": False}]}

    class _RaisingClient:
        def __init__(self, store): self.store = store
        def table(self, name): return _CreateRaisingQ(self.store, name)

    monkeypatch.setattr(h, "admin_service_client", lambda: _RaisingClient(store))
    monkeypatch.setattr(h, "record_change", lambda **k: None)
    app = FastAPI()
    app.dependency_overrides[auth.require_superuser] = lambda: {"user_id": "u", "email": "e", "user_token": "t"}
    app.include_router(router)
    client = TestClient(app)
    r = client.post("/admin/mcp/create", data={
        "name": "Sales Tools", "context_fields": "{}", "member_tool_ids": ["t1"]},
        follow_redirects=False)
    assert r.status_code == 200  # re-render, not a 500
    assert "already exists" in r.text.lower() or "sales tools" in r.text.lower()
    assert store.get("agent_mcp_servers", []) == []  # nothing stored
    assert store.get("agent_mcp_tools", []) == []  # membership never written


class _EmptyInsertQ(_Q):
    """Like _Q, but the agent_mcp_servers insert 'succeeds' with no returned
    row (simulates PostgREST returning no data), to exercise the no-id guard."""
    def execute(self):
        if self._ins is not None and self.table == "agent_mcp_servers":
            return type("R", (), {"data": []})()
        return super().execute()


def test_create_empty_insert_rerenders_not_none_redirect(monkeypatch):
    import admin.mcp_manage_handlers as h
    from admin.router import router
    from admin import auth
    # t1 must be a valid member so member-id validation passes and we reach the
    # empty-insert (no-id) guard this test is about.
    store = {"agent_tools": [
        {"tool_id": "t1", "python_name": "get_clients", "kind": "python", "is_deprecated": False}]}

    class _EmptyInsertClient:
        def __init__(self, store): self.store = store
        def table(self, name): return _EmptyInsertQ(self.store, name)

    monkeypatch.setattr(h, "admin_service_client", lambda: _EmptyInsertClient(store))
    monkeypatch.setattr(h, "record_change", lambda **k: None)
    app = FastAPI()
    app.dependency_overrides[auth.require_superuser] = lambda: {"user_id": "u", "email": "e", "user_token": "t"}
    app.include_router(router)
    client = TestClient(app)
    r = client.post("/admin/mcp/create", data={
        "name": "Sales Tools", "context_fields": "{}", "member_tool_ids": ["t1"]},
        follow_redirects=False)
    assert r.status_code == 200  # re-render, never a redirect to /None
    assert store.get("agent_mcp_tools", []) == []  # no orphan membership rows written
