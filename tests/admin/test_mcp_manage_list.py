"""Manage-list partitions MCPs into bundle/external/builtin with counts."""
from unittest.mock import MagicMock


def _fake_client(servers, members, attachments, profiles=None):
    client = MagicMock()
    def _table(name):
        t = MagicMock()
        data = {"agent_mcp_servers": servers, "agent_mcp_tools": members,
                "agent_profile_mcp_servers": attachments,
                "agent_profiles": profiles or []}[name]
        # .select(...).order(...).execute() and .select().eq().execute()
        q = MagicMock()
        q.execute.return_value = MagicMock(data=list(data))
        q.select.return_value = q
        q.order.return_value = q
        def _eq(col, val):
            eq_q = MagicMock()
            eq_q.execute.return_value = MagicMock(
                data=[r for r in data if r.get(col) == val])
            eq_q.select.return_value = eq_q
            return eq_q
        q.eq.side_effect = _eq
        t.select.return_value = q
        return t
    client.table.side_effect = _table
    return client


def test_cards_partition_and_count(monkeypatch):
    import admin.router as r
    servers = [
        {"mcp_server_id": "b1", "name": "__builtin__:business", "source": "bundle", "is_builtin": True},
        {"mcp_server_id": "u1", "name": "Sales Docs", "source": "bundle", "is_builtin": False},
        {"mcp_server_id": "e1", "name": "support-mock", "source": "external", "is_builtin": False,
         "tool_subset": ["get-sales-summary", "login"]},
    ]
    members = [{"mcp_server_id": "u1", "tool_id": "t1"}, {"mcp_server_id": "u1", "tool_id": "t2"}]
    attachments = [{"mcp_server_id": "e1", "profile_id": "p1"}]
    monkeypatch.setattr(r, "get_service_client", lambda: _fake_client(servers, members, attachments))
    monkeypatch.setattr(r, "client_for", lambda env: _fake_client(servers, members, attachments))

    ctx = r.build_integrations_context(None)
    assert [c["name"] for c in ctx["mcp_builtin"]] == ["__builtin__:business"]
    assert [c["name"] for c in ctx["mcp_bundles"]] == ["Sales Docs"]
    assert [c["name"] for c in ctx["mcp_external"]] == ["support-mock"]
    bundle = ctx["mcp_bundles"][0]
    assert bundle["tool_count"] == 2
    ext = ctx["mcp_external"][0]
    assert ext["attached_count"] == 1
    assert ext["tool_count"] == 2  # len(tool_subset)
    # Friendly names: built-in code names never become the title.
    assert ctx["mcp_builtin"][0]["display_name"].startswith("Tools for ")
    assert ext["display_name"] == "Support mock"


def test_cards_name_the_agents_using_each_server(monkeypatch):
    import admin.router as r
    servers = [{"mcp_server_id": "e1", "name": "support-mock", "source": "external",
                "is_builtin": False}]
    attachments = [{"mcp_server_id": "e1", "profile_id": "p1"},
                   {"mcp_server_id": "e1", "profile_id": "p2"}]
    profiles = [
        {"profile_id": "p1", "display_name": "Support Assistant", "agent_kind": "agents_support", "status": "active"},
        # Template ("mold") profiles are blueprints, not agents — never listed.
        {"profile_id": "p2", "display_name": "RAG mold", "agent_kind": "business", "status": "template"},
    ]
    fake = lambda *_a: _fake_client(servers, [], attachments, profiles)  # noqa: E731
    monkeypatch.setattr(r, "get_service_client", fake)
    monkeypatch.setattr(r, "client_for", fake)
    ctx = r.build_integrations_context(None)
    assert ctx["mcp_external"][0]["used_by"] == ["Support Assistant"]
