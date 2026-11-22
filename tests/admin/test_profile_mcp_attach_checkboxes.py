"""The checkbox wire variant of POST /admin/profiles/{id}/mcp-attachments.

The Agents screen's attachment editor grew per-server member-tool checkboxes
(`tool_subset_mode__{mid}=list` + `tool_subset_all__{mid}` +
`tool_subset_cb__{mid}`) in place of the comma-separated text field for bundle
MCPs. The handler contract: both variants parse to the SAME desired subset and
reconcile identically — same validation, same join rows — and the text field
stays the fallback for external MCPs and scripted POSTs.

Reuses the stateful fake Supabase client from test_profile_mcp_attach.py.
"""
from __future__ import annotations

from tests.admin.test_profile_mcp_attach import _base_store, _make_app


def _post(tc, data):
    return tc.post("/admin/profiles/p1/mcp-attachments", data=data,
                   follow_redirects=False)


def test_checkbox_subset_writes_same_row_as_comma_string(monkeypatch):
    """The core equivalence: a checkbox POST pinning `search_docs` must land
    the exact join row the comma-string POST lands.

    Discriminates: without the checkbox parsing, `tool_subset__u1` is absent
    → the old parser reads a blank field → subset None ("all tools"), and the
    equality against the text-variant row fails.
    """
    store_cb = _base_store()
    tc, _client_obj, teardown = _make_app(store_cb, monkeypatch)
    try:
        r = _post(tc, {
            "attach_mcp_ids": ["u1"],
            "tool_subset_mode__u1": "list",
            "tool_subset_cb__u1": ["search_docs"],
        })
        assert r.status_code == 302
        assert "error=" not in r.headers["location"]
    finally:
        teardown()

    store_txt = _base_store()
    tc, _client_obj, teardown = _make_app(store_txt, monkeypatch)
    try:
        r = _post(tc, {"attach_mcp_ids": ["u1"], "tool_subset__u1": "search_docs"})
        assert r.status_code == 302
    finally:
        teardown()

    assert store_cb["agent_profile_mcp_servers"] == store_txt["agent_profile_mcp_servers"]
    assert store_cb["agent_profile_mcp_servers"][0]["tool_subset"] == ["search_docs"]


def test_all_tools_master_persists_null_even_with_boxes_checked(monkeypatch):
    """The real form posts every pre-checked per-tool box alongside the "All
    tools" master; the master wins and NULL is stored (follow the bundle)."""
    store = _base_store()
    tc, _client_obj, teardown = _make_app(store, monkeypatch)
    try:
        r = _post(tc, {
            "attach_mcp_ids": ["u1"],
            "tool_subset_mode__u1": "list",
            "tool_subset_all__u1": "1",
            "tool_subset_cb__u1": ["search_docs"],
        })
        assert r.status_code == 302
        assert store["agent_profile_mcp_servers"][0]["tool_subset"] is None
    finally:
        teardown()


def test_no_boxes_checked_means_all_tools_like_blank_text_field(monkeypatch):
    store = _base_store()
    tc, _client_obj, teardown = _make_app(store, monkeypatch)
    try:
        r = _post(tc, {
            "attach_mcp_ids": ["u1"],
            "tool_subset_mode__u1": "list",
        })
        assert r.status_code == 302
        assert store["agent_profile_mcp_servers"][0]["tool_subset"] is None
    finally:
        teardown()


def test_forged_checkbox_value_refused_before_any_write(monkeypatch):
    """Guard 2 runs on the PARSED subset, so a forged checkbox name is refused
    exactly like a forged comma-string token — and nothing is written."""
    store = _base_store()
    tc, client_obj, teardown = _make_app(store, monkeypatch)
    try:
        r = _post(tc, {
            "attach_mcp_ids": ["u1"],
            "tool_subset_mode__u1": "list",
            "tool_subset_cb__u1": ["search_docs", "forged_tool"],
        })
        assert r.status_code == 302
        assert "error=" in r.headers["location"]
        assert store["agent_profile_mcp_servers"] == []
        assert "inserts" not in client_obj.log and "deletes" not in client_obj.log
    finally:
        teardown()


def test_text_field_fallback_untouched_when_mode_marker_absent(monkeypatch):
    """A scripted POST that never heard of the checkbox variant keeps the
    original comma-string contract byte-for-byte."""
    store = _base_store()
    tc, _client_obj, teardown = _make_app(store, monkeypatch)
    try:
        r = _post(tc, {
            "attach_mcp_ids": ["u1"],
            "tool_subset__u1": " search_docs , ",
            # A stray cb field WITHOUT the mode marker must be ignored — the
            # marker is what proves the checkbox controls were rendered.
            "tool_subset_cb__u1": ["forged_tool"],
        })
        assert r.status_code == 302
        assert "error=" not in r.headers["location"]
        assert store["agent_profile_mcp_servers"][0]["tool_subset"] == ["search_docs"]
    finally:
        teardown()


def test_checkbox_order_and_duplicates_normalised(monkeypatch):
    """Duplicated checkbox values dedupe first-occurrence, matching the text
    parser's behaviour of never writing a duplicate name."""
    store = _base_store()
    store["agent_mcp_tools"].append({"mcp_server_id": "u1", "tool_id": "t2", "position": 1})
    store["agent_tools"].append(
        {"tool_id": "t2", "python_name": "export_csv", "is_deprecated": False})
    tc, _client_obj, teardown = _make_app(store, monkeypatch)
    try:
        r = _post(tc, {
            "attach_mcp_ids": ["u1"],
            "tool_subset_mode__u1": "list",
            "tool_subset_cb__u1": ["export_csv", "search_docs", "export_csv"],
        })
        assert r.status_code == 302
        assert store["agent_profile_mcp_servers"][0]["tool_subset"] == [
            "export_csv", "search_docs"]
    finally:
        teardown()
