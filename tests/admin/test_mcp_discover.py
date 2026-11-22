"""POST /admin/mcp/discover returns a tools/list envelope (HTTP 200 always)."""
import pytest
from starlette.testclient import TestClient
from fastapi import FastAPI


def _app(monkeypatch, list_tools_result=None, raise_exc=None):
    import admin.mcp_manage_handlers as h
    from admin.router import router
    from admin import auth

    app = FastAPI()
    app.dependency_overrides[auth.require_superuser] = lambda: {"user_id": "u", "email": "e", "user_token": "t"}
    app.include_router(router)

    def _fake_list_tools_cached(endpoint, auth_token=None, **kw):
        if raise_exc:
            raise raise_exc
        return list_tools_result or []
    monkeypatch.setattr(h, "list_tools_cached", _fake_list_tools_cached)
    # public endpoint (not loopback)
    monkeypatch.setattr(h, "_classify_endpoint", lambda url: "public")
    return TestClient(app)


def test_discover_returns_tools(monkeypatch):
    client = _app(monkeypatch, list_tools_result=[
        {"name": "get-sales-summary", "description": "sales", "inputSchema": {"type": "object"}}])
    r = client.post("/admin/mcp/discover", json={"endpoint": "https://x.test/mcp", "transport": "http"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["tools"][0]["name"] == "get-sales-summary"


def test_discover_blocked_loopback(monkeypatch):
    import admin.mcp_manage_handlers as h
    client = _app(monkeypatch)
    monkeypatch.setattr(h, "_classify_endpoint", lambda url: "blocked")
    r = client.post("/admin/mcp/discover", json={"endpoint": "http://127.0.0.1/mcp", "transport": "http"})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "loopback" in r.json()["error"].lower() or "blocked" in r.json()["error"].lower()


def test_discover_error_degrades_to_envelope(monkeypatch):
    from runtime.tools._mcp_adapter import MCPError
    client = _app(monkeypatch, raise_exc=MCPError("boom"))
    r = client.post("/admin/mcp/discover", json={"endpoint": "https://x.test/mcp", "transport": "http"})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "boom" in r.json()["error"]


@pytest.mark.parametrize("bad_endpoint", [
    "http://[::1",          # malformed IPv6 → urlparse().hostname raises ValueError
    12345,                  # non-str truthy → bypasses `or ""`, `.strip()` would AttributeError
    ["http://x.test"],      # list → same AttributeError path
])
def test_discover_malformed_input_returns_200_envelope(monkeypatch, bad_endpoint):
    """The never-500 contract must hold even for hostile/malformed superuser
    inputs: a bad IPv6 bracket, a non-string endpoint, etc. must degrade to the
    {ok:false} envelope, not crash into a 500."""
    from admin.router import router
    from admin import auth

    # Build the app WITHOUT monkeypatching _classify_endpoint — we want the
    # REAL classifier to run so the malformed-IPv6 case is exercised end to
    # end. raise_server_exceptions=False so a regression surfaces as a 500
    # status code instead of a raised exception escaping the test.
    app = FastAPI()
    app.dependency_overrides[auth.require_superuser] = lambda: {"user_id": "u", "email": "e", "user_token": "t"}
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    r = client.post("/admin/mcp/discover", json={"endpoint": bad_endpoint, "transport": "http"})
    assert r.status_code == 200, f"expected never-500 envelope, got {r.status_code}"
    assert r.json()["ok"] is False
