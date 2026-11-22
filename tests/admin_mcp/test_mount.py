from tests.admin_mcp.conftest import MCP_HEADERS, rpc


def test_missing_auth_rejected_before_protocol(mcp_client):
    resp = mcp_client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={k: v for k, v in MCP_HEADERS.items() if k != "Authorization"},
    )
    assert resp.status_code == 401
    assert "Bearer" in resp.json()["error"]


def test_initialize_succeeds(mcp_client):
    resp = rpc(mcp_client, "initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"},
    })
    assert resp.status_code == 200
    assert resp.json()["result"]["serverInfo"]["name"] == "waj-ai-engine"


def test_tools_list_returns_json(mcp_client):
    resp = rpc(mcp_client, "tools/list")
    assert resp.status_code == 200
    assert "tools" in resp.json()["result"]


def test_post_mcp_no_trailing_slash_redirect(mcp_client):
    """Route (not Mount) must match "/mcp" exactly — no 307 to "/mcp/".

    A Mount-based redirect rebuilds the Location URL from scope["scheme"],
    which is plaintext "http" behind Cloud Run's TLS-terminating LB (uvicorn
    runs without --proxy-headers), so a client replaying its Authorization
    header against the redirect would leak the bearer token over http.
    """
    resp = mcp_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers=MCP_HEADERS,
        follow_redirects=False,
    )
    assert resp.status_code != 307
    assert resp.status_code == 200
