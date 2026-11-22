"""Knowledge + Connections layout fixes (portal revamp B4).

Each test pins one bug the Build-area audit found, by structure rather than
wording, so a copy edit does not break it but the bug coming back does.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List
from unittest.mock import patch

from tests.admin.conftest import client  # noqa: F401


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self._rows = list(rows)

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def in_(self, col, vals):
        self._rows = [r for r in self._rows if r.get(col) in vals]
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        return _Result(self._rows)


class _Fake:
    def __init__(self, tables: Dict[str, List[Dict[str, Any]]]):
        self._t = tables

    def table(self, name):
        return _Query(self._t.get(name, []))


KB_ID = "kb-1"


def _kb_tables(kind="pgvector"):
    return {
        "agent_data_sources": [{
            "source_id": KB_ID, "name": "product_docs", "kind": kind, "agent_kind": "orchard",
            "config": {"table": "ai_kb_embeddings", "pages_table": "ai_kb_pages"},
            "is_active": True, "last_refreshed_at": None, "refresh_cadence": "manual",
        }],
        "ai_kb_pages": [{"id": "p1", "source_id": KB_ID, "title": "Getting started",
                         "type": "user_guide", "language": "en", "source": "upload",
                         "created_at": "2026-10-01T00:00:00Z"}],
        "ai_kb_embeddings": [{"page_id": "p1", "source_id": KB_ID}],
    }


# ── Skills: every label wraps its own control ────────────────────────────


def test_skill_create_labels_wrap_their_own_inputs(client):  # noqa: F811
    """The old inline `.row` labels flowed after the inputs, so "Display
    name" sat beside the slug box — users typed into the wrong field."""
    html = client.get("/admin/skills").text
    for label, name in (("Name", "display_name"), ("Short code name", "slug"),
                        ("Agent", "agent_kind"), ("One-line description", "description"),
                        ("Instructions", "content")):
        pat = (r'<label class="field">\s*<span class="field-label">' + re.escape(label)
               + r'(?:\s*<span[^>]*>[^<]*</span>)?</span>\s*<(input|select|textarea)[^>]*name="'
               + name + '"')
        assert re.search(pat, html), f"{label!r} label does not wrap name={name!r}"


# ── Knowledge detail: documents first, danger zone last ──────────────────


def test_kb_detail_lists_documents_above_settings_and_danger_last(client):  # noqa: F811
    with patch("admin.kb_handlers.admin_service_client", return_value=_Fake(_kb_tables())):
        r = client.get(f"/admin/kb/{KB_ID}")
    assert r.status_code == 200
    html = r.text
    docs = html.index('id="documents"')
    upload = html.index(f'action="/admin/kb/{KB_ID}/upload"')
    listing = html.index("Getting started")
    settings = html.index('id="settings"')
    danger = html.index("Danger zone")
    assert docs < upload < listing < settings < danger
    # Danger zone is the last section of the main column.
    assert html.count('class="section') and html.rfind('<section class="section', 0, danger + 1) >= settings


def test_kb_detail_hides_never_refreshed_for_uploaded_files(client):  # noqa: F811
    with patch("admin.kb_handlers.admin_service_client", return_value=_Fake(_kb_tables())):
        html = client.get(f"/admin/kb/{KB_ID}").text
    assert "Never refreshed" not in html
    assert "Last synced" not in html


def test_kb_detail_explains_an_unsynced_source(client):  # noqa: F811
    with patch("admin.kb_handlers.admin_service_client", return_value=_Fake(_kb_tables("gitbook"))):
        html = client.get(f"/admin/kb/{KB_ID}").text
    assert "has not been synced yet" in html


def test_kb_detail_json_config_sits_under_advanced(client):  # noqa: F811
    with patch("admin.kb_handlers.admin_service_client", return_value=_Fake(_kb_tables())):
        html = client.get(f"/admin/kb/{KB_ID}").text
    adv = html.index('<details class="advanced">')
    assert html.index('name="config"') > adv


def test_kb_list_status_is_per_source_type(client):  # noqa: F811
    from admin.router import _attach_kb_document_stats

    rows = [
        {"source_id": KB_ID, "kind": "pgvector"},
        {"source_id": "kb-2", "kind": "pgvector"},
        {"source_id": "kb-3", "kind": "gitbook", "last_refreshed_at": None},
        {"source_id": "kb-4", "kind": "supabase_table"},
    ]
    _attach_kb_document_stats(_Fake(_kb_tables()), rows)
    assert [r["status"][1] for r in rows] == [
        "Ready", "No documents yet", "Never synced", "Live table"]
    assert rows[0]["doc_count"] == 1


def test_kb_update_accepts_checkbox_agents_and_clears_when_none(client):  # noqa: F811
    from admin.kb_handlers import _parse_form_payload
    from starlette.datastructures import FormData

    got = _parse_form_payload(FormData([("additional_agent_kinds", "sales"),
                                        ("additional_agent_kinds", "orchard"),
                                        ("_additional_kinds_form", "1")]))
    assert got["additional_agent_kinds"] == ["sales", "orchard"]
    cleared = _parse_form_payload(FormData([("_additional_kinds_form", "1")]))
    assert cleared["additional_agent_kinds"] == []
    legacy = _parse_form_payload(FormData([("additional_agent_kinds", "sales\nsupport_bot")]))
    assert legacy["additional_agent_kinds"] == ["sales", "support_bot"]


# ── Connect a server: discover before connect, plain auth help ───────────


def test_connect_puts_discover_before_connect_and_explains_token(client):  # noqa: F811
    html = client.get("/admin/mcp/connect").text
    assert html.index("data-mcp-discover") < html.index("Connect server</button>")
    assert "Access token" in html
    assert "never stored" in html
    # Raw JSON + transport sit under Advanced.
    adv = html.index('<details class="advanced">')
    assert html.index('name="context_fields"') > adv
    assert html.index('name="transport"') > adv


# ── Create a bundle: search + selected-first picker ──────────────────────


def test_create_bundle_has_search_and_selected_first_picker(client):  # noqa: F811
    tools = [{"tool_id": "t1", "python_name": "a_tool", "agent_kind": "orchard", "description": "x"},
             {"tool_id": "t2", "python_name": "b_tool", "agent_kind": "orchard", "description": "y"}]
    with patch("admin.mcp_manage_handlers._load_tools_for_picker", return_value=tools):
        html = client.post("/admin/mcp/create", data={
            "name": "__builtin__:nope", "member_tool_ids": "t2"}).text
    assert 'id="pick-search"' in html
    picked = html.index('id="picked"')
    pool = html.index('id="pool"')
    # The ticked tool renders in "Selected", ahead of the pool.
    assert picked < html.index('value="t2"') < pool < html.index('value="t1"')


# ── Tool detail: one Details block, back to its server ───────────────────


def test_tool_detail_uses_one_details_block(client):  # noqa: F811
    tables = {
        "agent_tools": [{"tool_id": "t1", "python_name": "get_sales_summary",
                         "display_name": "Sales summary", "agent_kind": "orchard",
                         "scope": "read", "kind": "python", "source_file": None}],
        "agent_mcp_tools": [{"mcp_server_id": "s1", "tool_id": "t1"}],
        "agent_mcp_servers": [{"mcp_server_id": "s1", "name": "__builtin__:orchard"}],
    }
    with patch("admin.tool_handlers.admin_service_client", return_value=_Fake(tables)):
        html = client.get("/admin/tools/t1").text
    assert html.count('<dl class="kv">') == 1
    assert "Tool ID</div>" not in html  # the old single-value rail cards
    assert 'class="back-link" href="/admin/agent-mcp-servers/s1"' in html
