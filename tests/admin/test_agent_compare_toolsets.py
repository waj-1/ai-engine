"""Agent compare shows toolsets + chosen actions and skills (not only the
legacy flat tool lists): admin/profile_fork_handlers.toolsets_diff /
compute_profile_diff, the agents_diff.html page, summarize_profile_diff."""
from __future__ import annotations

from unittest.mock import patch

from admin import profile_fork_handlers as pf
from admin.services.profile_versions import summarize_profile_diff
from tests.admin_mcp.conftest import FakeClient


def _tool(name, display=None):
    return {"tool": {"tool_id": f"t-{name}", "python_name": name, "display_name": display}}


def _att(sid, name, members, subset=None, source="bundle"):
    return {"mcp_server": {"mcp_server_id": sid, "name": name, "source": source},
            "tool_subset": subset, "member_tools": [_tool(*m) if isinstance(m, tuple) else _tool(m)
                                                     for m in members]}


DATA = ["get_sales", ("get_transactions", "Get transactions"), "get_refunds"]


def test_toolsets_diff_classifies_toolsets_and_actions():
    a = [_att("s-data", "Order data", DATA),
         _att("s-tickets", "Tickets", ["open_ticket"]),
         _att("s-kb", "Help docs", ["search_docs"])]
    b = [_att("s-data", "Order data", DATA, subset=["get_sales", "get_transactions"]),
         _att("s-kb", "Help docs", ["search_docs"]),
         _att("s-pay", "Payments", ["send_link"])]
    d = pf.toolsets_diff(a, b)
    by = {x["name"]: x for x in d["items"]}
    assert by["Payments"]["state"] == "added"
    assert by["Tickets"]["state"] == "removed"
    assert by["Help docs"]["state"] == "same"
    data = by["Order data"]
    assert data["state"] == "changed"
    assert data["summary_a"] == "All 3 actions" and data["summary_b"] == "2 of 3 actions"
    states = {x["name"]: x["state"] for x in data["actions"]}
    assert states == {"get_sales": "same", "get_transactions": "same", "get_refunds": "removed"}
    assert next(x for x in data["actions"] if x["name"] == "get_transactions")["label"] == "Get transactions"
    assert d["counts"] == {"added": 1, "removed": 1, "changed": 1, "same": 1}
    assert d["n_changed"] == 3
    # Differences sort first.
    assert [x["state"] for x in d["items"]] == ["added", "removed", "changed", "same"]


def test_external_server_actions_all_vs_subset():
    a = [_att("s-ext", "Orchard MCP", [], source="external")]
    b = [_att("s-ext", "Orchard MCP", [], subset=["get_iban"], source="external")]
    d = pf.toolsets_diff(a, b)
    (item,) = d["items"]
    assert item["state"] == "changed"
    assert item["summary_a"] == "All actions" and item["summary_b"] == "1 action"


def test_unknown_side_is_flagged():
    assert pf.toolsets_diff(None, [])["unknown"] is True
    assert pf.toolsets_diff([], [])["unknown"] is False


def _profile(pid, **kw):
    base = {"profile_id": pid, "agent_kind": "orchard", "display_name": pid, "environment": "staging",
            "status": "active", "tool_ids": [], "data_source_ids": [], "mcp_server_ids": [],
            "downstream_profile_ids": []}
    base.update(kw)
    return base


def _tables():
    return {
        "agent_profile_mcp_servers": [
            {"profile_id": "pa", "mcp_server_id": "s-data", "tool_subset": None, "position": 0},
            {"profile_id": "pb", "mcp_server_id": "s-data", "tool_subset": ["get_sales"], "position": 0},
            {"profile_id": "pb", "mcp_server_id": "s-pay", "tool_subset": None, "position": 1},
        ],
        "agent_mcp_servers": [
            {"mcp_server_id": "s-data", "name": "Order data", "source": "bundle", "is_active": True},
            {"mcp_server_id": "s-pay", "name": "Payments", "source": "bundle", "is_active": True},
        ],
        "agent_mcp_tools": [
            {"mcp_server_id": "s-data", "tool_id": "t1", "position": 0},
            {"mcp_server_id": "s-data", "tool_id": "t2", "position": 1},
            {"mcp_server_id": "s-pay", "tool_id": "t3", "position": 0},
        ],
        "agent_tools": [
            {"tool_id": "t1", "python_name": "get_sales", "display_name": "Get sales", "is_deprecated": False},
            {"tool_id": "t2", "python_name": "get_refunds", "display_name": None, "is_deprecated": False},
            {"tool_id": "t3", "python_name": "send_link", "display_name": "Send payment link", "is_deprecated": False},
        ],
        "agent_profile_skills": [
            {"profile_id": "pa", "skill_id": "k1", "position": 0},
            {"profile_id": "pb", "skill_id": "k1", "position": 0},
            {"profile_id": "pb", "skill_id": "k2", "position": 1},
        ],
        "agent_skills": [{"skill_id": "k1", "display_name": "Refund policy"},
                         {"skill_id": "k2", "display_name": "Collections script"}],
        "agent_prompts": [],
    }


def test_compute_profile_diff_reads_live_attachments_and_skills():
    client = FakeClient(_tables())
    diff = pf.compute_profile_diff(client, _profile("pa"), _profile("pb"))
    by = {x["name"]: x for x in diff["toolsets"]["items"]}
    assert by["Payments"]["state"] == "added"
    assert by["Order data"]["state"] == "changed"
    removed = [a["label"] for a in by["Order data"]["actions"] if a["state"] == "removed"]
    assert removed == ["Get refunds"]
    assert [x["label"] for x in diff["skills"]["added_in_b"]] == ["Collections script"]
    assert [x["label"] for x in diff["skills"]["shared"]] == ["Refund policy"]
    lines = summarize_profile_diff(diff)
    assert "toolsets: +Payments" in lines
    assert "toolset Order data actions: -Get refunds" in lines
    assert "skills: +Collections script" in lines


def test_snapshot_side_uses_its_frozen_attachments_not_a_live_read():
    client = FakeClient(_tables())
    frozen = [_att("s-data", "Order data", ["get_sales", "get_refunds"], subset=["get_sales"])]
    a = _profile("pb", _snapshot=True, mcp_attachments=frozen, skill_attachments=[])
    diff = pf.compute_profile_diff(client, a, _profile("pb"))
    by = {x["name"]: x for x in diff["toolsets"]["items"]}
    assert by["Order data"]["state"] == "same"      # live pb pins get_sales too
    assert by["Payments"]["state"] == "added"
    old = _profile("pb", _snapshot=True)                # version predates freezing
    diff2 = pf.compute_profile_diff(client, old, _profile("pb"))
    assert diff2["toolsets"]["unknown"] and diff2["skills"]["unknown"]


def test_agents_diff_page_shows_toolsets_and_actions(client):
    tables = _tables()
    tables["agent_profiles"] = [_profile("pa", display_name="Orchard assistant"),
                                _profile("pb", display_name="Orchard copy", status="draft")]
    fake = FakeClient(tables)
    with patch.object(pf, "admin_service_client", return_value=fake):
        r = client.get("/admin/agents/diff?a=pa&b=pb")
    assert r.status_code == 200, r.text[:300]
    t = r.text
    assert "Toolsets and actions" in t
    assert "Added" in t and "Actions differ" in t and "Removed" not in t.split("Toolsets and actions")[1].split("Skills")[0]
    assert "− Get refunds" in t
    assert "All 2 actions" in t and "1 of 2 actions" in t
    assert "Collections script" in t
    # Legacy lists are kept, behind a disclosure.
    assert "Legacy lists (not used when the agent runs)" in t
