"""Tests for admin.mcp_membership — the crash-safe reconcile helpers.

Core invariant: desired rows are UPSERTED before any stale row is DELETED,
so the join is never emptied mid-write (the #1032 / #1380 tool-loss class).
Also covers ordering, reorder-in-place, detach-stale, clear-all, and the
returned prior membership.
"""
from __future__ import annotations

from admin.mcp_membership import (reconcile_bundle_members,
                                  reconcile_profile_mcp_attachments)


class _Result:
    def __init__(self, data):
        self.data = data


class _OrderRecordingClient:
    """Stateful fake recording the (op, table) sequence so a test can assert
    upsert precedes delete, plus the resulting store."""

    def __init__(self, tables):
        self.store = {k: [dict(r) for r in v] for k, v in tables.items()}
        self.ops: list = []
        self._reset()

    def _reset(self):
        self._t = None
        self._eq: dict = {}
        self._in = None
        self._upsert = None
        self._on_conflict = None
        self._delete = False

    def table(self, name):
        self._reset()
        self._t = name
        return self

    def select(self, *a, **k):
        return self

    def eq(self, c, v):
        self._eq[c] = v
        return self

    def in_(self, c, v):
        self._in = (c, list(v))
        return self

    def upsert(self, payload, on_conflict=None):
        self._upsert = payload if isinstance(payload, list) else [payload]
        self._on_conflict = on_conflict
        return self

    def delete(self):
        self._delete = True
        return self

    def _match(self, r):
        for c, v in self._eq.items():
            if r.get(c) != v:
                return False
        if self._in is not None:
            col, vals = self._in
            if r.get(col) not in set(vals):
                return False
        return True

    def execute(self):
        rows = self.store.setdefault(self._t, [])
        if self._upsert is not None:
            cols = [c.strip() for c in (self._on_conflict or "").split(",") if c.strip()]
            for it in self._upsert:
                existing = next(
                    (r for r in rows if all(r.get(c) == it.get(c) for c in cols)), None
                ) if cols else None
                if existing is not None:
                    existing.update(it)
                else:
                    rows.append(dict(it))
            self.ops.append(("upsert", self._t))
            return _Result(list(self._upsert))
        if self._delete:
            removed = [r for r in rows if self._match(r)]
            self.store[self._t] = [r for r in rows if not self._match(r)]
            self.ops.append(("delete", self._t))
            return _Result(removed)
        self.ops.append(("select", self._t))
        return _Result([r for r in rows if self._match(r)])


def _first(ops, name):
    return next((i for i, (op, _) in enumerate(ops) if op == name), None)


# --- profile attachments ----------------------------------------------------


def test_attachments_upsert_before_delete_never_empties():
    """Replacing [m2] with [m1]: the new row is upserted BEFORE the stale row
    is deleted, so the join is never empty at any point."""
    client = _OrderRecordingClient({
        "agent_profile_mcp_servers": [
            {"profile_id": "p1", "mcp_server_id": "m2", "tool_subset": None, "position": 0},
        ],
    })
    before = reconcile_profile_mcp_attachments(client, "p1", [("m1", ["ta"])])

    assert before == ["m2"]
    assert _first(client.ops, "upsert") < _first(client.ops, "delete")
    final = client.store["agent_profile_mcp_servers"]
    assert final == [{"profile_id": "p1", "mcp_server_id": "m1",
                      "tool_subset": ["ta"], "position": 0}]


def test_attachments_reorder_in_place_no_delete():
    """Reordering the same set rewrites position via upsert and deletes
    nothing — no row is ever removed."""
    client = _OrderRecordingClient({
        "agent_profile_mcp_servers": [
            {"profile_id": "p1", "mcp_server_id": "a", "tool_subset": None, "position": 0},
            {"profile_id": "p1", "mcp_server_id": "b", "tool_subset": None, "position": 1},
        ],
    })
    before = reconcile_profile_mcp_attachments(client, "p1", [("b", None), ("a", None)])

    assert before == ["a", "b"]
    assert _first(client.ops, "delete") is None  # nothing detached
    by_id = {r["mcp_server_id"]: r["position"]
             for r in client.store["agent_profile_mcp_servers"]}
    assert by_id == {"b": 0, "a": 1}


def test_attachments_clear_all_deletes_everything():
    client = _OrderRecordingClient({
        "agent_profile_mcp_servers": [
            {"profile_id": "p1", "mcp_server_id": "m1", "tool_subset": None, "position": 0},
        ],
    })
    before = reconcile_profile_mcp_attachments(client, "p1", [])
    assert before == ["m1"]
    assert client.store["agent_profile_mcp_servers"] == []


# --- bundle members ---------------------------------------------------------


def test_members_upsert_before_delete_never_empties():
    client = _OrderRecordingClient({
        "agent_mcp_tools": [
            {"mcp_server_id": "b1", "tool_id": "old", "position": 0},
        ],
    })
    before = reconcile_bundle_members(client, "b1", ["new"])

    assert before == ["old"]
    assert _first(client.ops, "upsert") < _first(client.ops, "delete")
    final = [(r["tool_id"], r["position"]) for r in client.store["agent_mcp_tools"]]
    assert final == [("new", 0)]


def test_members_preserves_order_and_returns_prior():
    client = _OrderRecordingClient({
        "agent_mcp_tools": [
            {"mcp_server_id": "b1", "tool_id": "ta", "position": 0},
            {"mcp_server_id": "b1", "tool_id": "tb", "position": 1},
        ],
    })
    before = reconcile_bundle_members(client, "b1", ["tb", "ta"])
    assert before == ["ta", "tb"]
    assert _first(client.ops, "delete") is None
    by_id = {r["tool_id"]: r["position"] for r in client.store["agent_mcp_tools"]}
    assert by_id == {"tb": 0, "ta": 1}


# ── Pinning tool names on an external server that exposes all its tools ──────

class TestSubsetNamesFor:
    """An external MCP server registered without a tool subset exposes every
    tool it lists. Pinning a few of them on an agent used to be refused as
    "unknown tool(s)", because the only list checked was the stored one, which
    is empty for such a server."""

    def _server(self, **over):
        row = {"mcp_server_id": "s1", "name": "Company systems", "tool_names": [],
               "exposes_all_tools": True, "endpoint": "https://tools.example/mcp",
               "auth_env_var": None}
        row.update(over)
        return row

    def test_stored_list_is_used_without_calling_the_server(self, monkeypatch):
        from admin import mcp_membership
        import runtime.tools._mcp_adapter as adapter

        def boom(*a, **k):
            raise AssertionError("must not probe a server that has a stored list")
        monkeypatch.setattr(adapter, "list_tools_cached", boom)
        names, why = mcp_membership.subset_names_for(
            self._server(tool_names=["a", "b"], exposes_all_tools=False))
        assert (names, why) == ({"a", "b"}, None)

    def test_a_bundle_with_no_members_has_no_valid_names(self, monkeypatch):
        from admin import mcp_membership
        names, why = mcp_membership.subset_names_for(
            {"name": "Empty bundle", "tool_names": [], "exposes_all_tools": False})
        assert (names, why) == (set(), None)

    def test_all_tools_server_is_checked_against_its_live_list(self, monkeypatch):
        from admin import mcp_membership
        import runtime.tools._mcp_adapter as adapter
        monkeypatch.setattr(adapter, "list_tools_cached",
                            lambda ep, tok: [{"name": "lookup_customer"}, {"name": "open_ticket"}])
        names, why = mcp_membership.subset_names_for(self._server())
        assert names == {"lookup_customer", "open_ticket"} and why is None

    def test_unreachable_server_is_refused_with_the_reason(self, monkeypatch):
        from admin import mcp_membership
        import runtime.tools._mcp_adapter as adapter

        def down(ep, tok):
            raise RuntimeError("connection refused")
        monkeypatch.setattr(adapter, "list_tools_cached", down)
        names, why = mcp_membership.subset_names_for(self._server())
        assert names is None
        assert "Company systems" in why and "connection refused" in why

    def test_local_address_is_refused_unless_allowed(self, monkeypatch):
        from admin import mcp_membership
        monkeypatch.delenv("MCP_ALLOW_LOCAL_ENDPOINTS", raising=False)
        names, why = mcp_membership.subset_names_for(
            self._server(endpoint="http://127.0.0.1:9100/mcp"))
        assert names is None and "blocked" in why

    def test_secret_is_not_sent_to_a_non_public_address(self, monkeypatch):
        from admin import mcp_membership
        import runtime.tools._mcp_adapter as adapter
        seen = {}
        monkeypatch.setenv("MCP_ALLOW_LOCAL_ENDPOINTS", "1")
        monkeypatch.setenv("TOOLS_TOKEN", "s3cret")
        monkeypatch.setattr(adapter, "list_tools_cached",
                            lambda ep, tok: seen.update(tok=tok) or [{"name": "x"}])
        mcp_membership.subset_names_for(
            self._server(endpoint="http://127.0.0.1:9100/mcp", auth_env_var="TOOLS_TOKEN"))
        assert seen["tok"] is None

    def test_apply_accepts_a_pinned_name_the_live_server_serves(self, monkeypatch):
        from admin import mcp_membership
        from admin_mcp import tools_configure as tc
        import runtime.tools._mcp_adapter as adapter
        monkeypatch.setattr(adapter, "list_tools_cached",
                            lambda ep, tok: [{"name": "lookup_customer"}])
        monkeypatch.setattr(mcp_membership, "reconcile_profile_mcp_attachments",
                            lambda *a, **k: [])
        monkeypatch.setattr(tc, "actor_audit", lambda *a, **k: None)
        attachable = {"s1": self._server()}
        bad = tc._apply_mcp_attachments(None, {"profile_id": "p1"},
                                        {"s1": ["nope"]}, attachable)
        assert "unknown tool(s) nope" in bad["error"]
        ok = tc._apply_mcp_attachments(None, {"profile_id": "p1"},
                                       {"s1": ["lookup_customer"]}, attachable)
        assert not ok.get("error")
