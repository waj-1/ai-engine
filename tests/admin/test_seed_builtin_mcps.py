"""Fake-Supabase tests for admin/seed_builtin_mcps.py."""
import pytest


class _Result:
    def __init__(self, data):
        self.data = data or []


class _Query:
    """Minimal chainable fake supporting the calls the seeder makes."""
    def __init__(self, table, store):
        self._table = table
        self._store = store
        self._filters = {}
        self._contains = None
        self._pending_insert = None
        self._pending_update = None

    def select(self, *_a, **_k):
        return self

    def limit(self, _n):
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def in_(self, col, vals):
        self._in = (col, list(vals))
        return self

    def contains(self, col, val):
        self._contains = (col, val)
        return self

    def insert(self, payload):
        self._pending_insert = payload
        return self

    def update(self, payload):
        self._pending_update = payload
        return self

    def delete(self):
        self._pending_delete = True
        return self

    def _match(self, row):
        for c, v in self._filters.items():
            if row.get(c) != v:
                return False
        if getattr(self, "_in", None):
            col, vals = self._in
            if row.get(col) not in vals:
                return False
        if self._contains:
            col, vals = self._contains
            if not set(vals).issubset(set(row.get(col) or [])):
                return False
        return True

    def execute(self):
        rows = self._store.setdefault(self._table, [])
        if self._pending_insert is not None:
            payload = self._pending_insert
            items = payload if isinstance(payload, list) else [payload]
            stored = []
            for it in items:
                row = dict(it)
                # Simulate the DB default gen_random_uuid() PK that Supabase
                # returns on insert — the seeder reads mcp_server_id back.
                if self._table == "agent_mcp_servers" and "mcp_server_id" not in row:
                    row["mcp_server_id"] = f"mcp-{len(rows) + len(stored) + 1}"
                rows.append(row)
                stored.append(row)
            return _Result(stored)
        if self._pending_update is not None:
            updated = []
            for row in rows:
                if self._match(row):
                    row.update(self._pending_update)
                    updated.append(row)
            return _Result(updated)
        if getattr(self, "_pending_delete", False):
            keep = [r for r in rows if not self._match(r)]
            self._store[self._table] = keep
            return _Result([])
        return _Result([r for r in rows if self._match(r)])


class FakeClient:
    def __init__(self, store):
        self._store = store

    def table(self, name):
        return _Query(name, self._store)


@pytest.fixture()
def store():
    # One business profile + two business tools (one python in-pool, one http not-in-pool).
    return {
        "agent_tools": [
            {"tool_id": "t-perf", "python_name": "get_business_performance",
             "kind": "python", "agent_kind": "business", "additional_agent_kinds": [],
             "is_deprecated": False},
            {"tool_id": "t-http", "python_name": "crm_http",
             "kind": "http", "agent_kind": "business", "additional_agent_kinds": [],
             "is_deprecated": False},
        ],
        "agent_profiles": [
            {"profile_id": "p-biz", "agent_kind": "business", "slug": "business",
             "tool_ids": ["t-perf", "t-http"]},
        ],
        "agent_mcp_servers": [],
        "agent_mcp_tools": [],
        "agent_profile_mcp_servers": [],
    }


def _patch_pool(monkeypatch, names_by_kind):
    import admin.seed_builtin_mcps as mod
    monkeypatch.setattr(mod, "_pool_names_for_kind",
                        lambda kind: names_by_kind.get(kind, []))
    # Restrict to just 'business' so the test doesn't need every factory.
    monkeypatch.setattr(mod, "_seedable_kinds", lambda: ["business"])


def test_seeds_bundle_membership_and_attachment(monkeypatch, store):
    import admin.seed_builtin_mcps as mod
    monkeypatch.setattr(mod, "get_service_client", lambda: FakeClient(store))
    _patch_pool(monkeypatch, {"business": ["get_business_performance"]})

    counts = mod.run()

    # One built-in bundle created, is_builtin + source=bundle
    servers = store["agent_mcp_servers"]
    assert len(servers) == 1
    assert servers[0]["is_builtin"] is True
    assert servers[0]["source"] == "bundle"
    assert servers[0]["name"] == "__builtin__:business"

    # Membership: python pool tool first (pos 0), http appended (pos 1)
    members = sorted(store["agent_mcp_tools"], key=lambda m: m["position"])
    assert [m["tool_id"] for m in members] == ["t-perf", "t-http"]

    # Attachment: profile linked with tool_subset = its tool_python_names (DB order)
    atts = store["agent_profile_mcp_servers"]
    assert len(atts) == 1
    assert atts[0]["profile_id"] == "p-biz"
    assert atts[0]["tool_subset"] == ["get_business_performance", "crm_http"]
    assert counts["bundles"] == 1 and counts["attachments"] == 1


def test_idempotent_second_run_no_duplicates(monkeypatch, store):
    import admin.seed_builtin_mcps as mod
    monkeypatch.setattr(mod, "get_service_client", lambda: FakeClient(store))
    _patch_pool(monkeypatch, {"business": ["get_business_performance"]})

    mod.run()
    mod.run()

    assert len(store["agent_mcp_servers"]) == 1
    assert len(store["agent_profile_mcp_servers"]) == 1
    # membership reconciled, not duplicated
    assert len(store["agent_mcp_tools"]) == 2


def test_existing_attachment_is_left_untouched(monkeypatch, store):
    import admin.seed_builtin_mcps as mod
    # Pre-seed a REAL (tool-providing) attachment with a custom subset (an admin
    # edit we must not clobber). It needs a backing server row so the seeder can
    # see it's not endpoint-only.
    store["agent_mcp_servers"].append(
        {"mcp_server_id": "pre", "name": "admin-choice", "source": "external",
         "endpoint_only": False, "is_active": True})
    store["agent_profile_mcp_servers"].append(
        {"profile_id": "p-biz", "mcp_server_id": "pre", "tool_subset": ["only_this"], "position": 0})
    monkeypatch.setattr(mod, "get_service_client", lambda: FakeClient(store))
    _patch_pool(monkeypatch, {"business": ["get_business_performance"]})

    mod.run()

    atts = store["agent_profile_mcp_servers"]
    assert len(atts) == 1  # not replaced
    assert atts[0]["tool_subset"] == ["only_this"]


def test_attach_profiles_attaches_bundle_over_endpoint_only(monkeypatch):
    """A profile whose ONLY existing attachment is to an endpoint_only=True
    external server still gets the curated bundle attached — an endpoint-only
    attachment doesn't count as a real/admin tool choice (the orchard case)."""
    import admin.seed_builtin_mcps as mod
    store = {
        "agent_tools": [
            {"tool_id": "t-a", "python_name": "get_a", "kind": "python",
             "agent_kind": "orchard", "additional_agent_kinds": [], "is_deprecated": False},
        ],
        "agent_profiles": [
            {"profile_id": "p-orchard", "agent_kind": "orchard", "slug": "orchard_default",
             "tool_ids": ["t-a"]},
        ],
        "agent_mcp_servers": [
            {"mcp_server_id": "ext-orchard", "name": "orchard-mock", "source": "external",
             "endpoint_only": True, "is_active": True},
        ],
        "agent_mcp_tools": [],
        "agent_profile_mcp_servers": [
            {"profile_id": "p-orchard", "mcp_server_id": "ext-orchard",
             "tool_subset": None, "position": 0},
        ],
    }
    monkeypatch.setattr(mod, "get_service_client", lambda: FakeClient(store))
    monkeypatch.setattr(mod, "_seedable_kinds", lambda: ["orchard"])
    monkeypatch.setattr(mod, "_pool_names_for_kind", lambda kind: ["get_a"])

    counts = mod.run()

    atts = store["agent_profile_mcp_servers"]
    # endpoint-only attachment preserved AND bundle attachment added
    assert len(atts) == 2
    bundle = store["agent_mcp_servers"][-1]  # newly created built-in bundle
    assert bundle["name"] == "__builtin__:orchard"
    assert any(a["mcp_server_id"] == bundle["mcp_server_id"] for a in atts)
    assert counts["attachments"] == 1


def test_attach_profiles_respects_real_attachment(monkeypatch):
    """A profile with a real (non-endpoint-only) attachment is left untouched —
    the bundle is NOT attached (admin/prior tool choice wins)."""
    import admin.seed_builtin_mcps as mod
    store = {
        "agent_tools": [
            {"tool_id": "t-a", "python_name": "get_a", "kind": "python",
             "agent_kind": "orchard", "additional_agent_kinds": [], "is_deprecated": False},
        ],
        "agent_profiles": [
            {"profile_id": "p-orchard", "agent_kind": "orchard", "slug": "orchard_default",
             "tool_ids": ["t-a"]},
        ],
        "agent_mcp_servers": [
            {"mcp_server_id": "real-srv", "name": "admin-choice", "source": "external",
             "endpoint_only": False, "is_active": True},
        ],
        "agent_mcp_tools": [],
        "agent_profile_mcp_servers": [
            {"profile_id": "p-orchard", "mcp_server_id": "real-srv",
             "tool_subset": None, "position": 0},
        ],
    }
    monkeypatch.setattr(mod, "get_service_client", lambda: FakeClient(store))
    monkeypatch.setattr(mod, "_seedable_kinds", lambda: ["orchard"])
    monkeypatch.setattr(mod, "_pool_names_for_kind", lambda kind: ["get_a"])

    counts = mod.run()

    atts = store["agent_profile_mcp_servers"]
    assert len(atts) == 1  # bundle NOT attached
    assert atts[0]["mcp_server_id"] == "real-srv"
    assert counts["attachments"] == 0


def test_new_tool_auto_joins_bundle_on_next_run(monkeypatch):
    """When a tool is added to the database (agent_tools), the next seeder run
    reconciles membership to include it (auto-join)."""
    import admin.seed_builtin_mcps as mod
    store = {
        "agent_tools": [
            {"tool_id": "t-a", "python_name": "get_a", "kind": "python",
             "agent_kind": "business", "additional_agent_kinds": [], "is_deprecated": False},
        ],
        "agent_profiles": [
            {"profile_id": "p-biz", "agent_kind": "business", "slug": "business",
             "tool_ids": ["t-a"]},
        ],
        "agent_mcp_servers": [], "agent_mcp_tools": [], "agent_profile_mcp_servers": [],
    }
    monkeypatch.setattr(mod, "get_service_client", lambda: FakeClient(store))
    monkeypatch.setattr(mod, "_seedable_kinds", lambda: ["business"])
    monkeypatch.setattr(mod, "_pool_names_for_kind", lambda kind: ["get_a", "get_b"])

    # First run: only get_a in the database
    mod.run()
    assert len(store["agent_mcp_tools"]) == 1
    assert store["agent_mcp_tools"][0]["tool_id"] == "t-a"

    # Add a new tool to the database
    store["agent_tools"].append(
        {"tool_id": "t-b", "python_name": "get_b", "kind": "python",
         "agent_kind": "business", "additional_agent_kinds": [], "is_deprecated": False})

    # Second run: new tool auto-joins the bundle
    mod.run()

    # Membership reconciled: both tools now in bundle
    members = sorted(store["agent_mcp_tools"], key=lambda m: m["position"])
    assert len(members) == 2
    assert [m["tool_id"] for m in members] == ["t-a", "t-b"]


def test_tool_borrowing_kind_attaches_to_the_owners_bundle(monkeypatch):
    """A kind that borrows another kind's tools (_TOOL_KIND_ALIAS) attaches
    to that kind's built-in bundle. The two kinds here are test data."""
    import admin.seed_agent_profiles as profiles_mod
    import admin.seed_builtin_mcps as mod
    store = {
        "agent_tools": [
            {"tool_id": "t-rec", "python_name": "search_records", "kind": "python",
             "agent_kind": "records", "additional_agent_kinds": [], "is_deprecated": False},
        ],
        "agent_profiles": [
            {"profile_id": "p-rec", "agent_kind": "records", "slug": "records",
             "tool_ids": ["t-rec"]},
            {"profile_id": "p-review", "agent_kind": "records_review", "slug": "records_review",
             "tool_ids": ["t-rec"]},
        ],
        "agent_mcp_servers": [], "agent_mcp_tools": [], "agent_profile_mcp_servers": [],
    }
    monkeypatch.setattr(profiles_mod, "_TOOL_KIND_ALIAS", {"records_review": "records", "notes": None})
    monkeypatch.setattr(mod, "get_service_client", lambda: FakeClient(store))
    monkeypatch.setattr(mod, "_seedable_kinds", lambda: ["records"])
    monkeypatch.setattr(mod, "_pool_names_for_kind", lambda kind: ["search_records"])

    mod.run()

    atts = store["agent_profile_mcp_servers"]
    # both records AND records_review profiles attach to the SAME built-in bundle
    assert {a["profile_id"] for a in atts} == {"p-rec", "p-review"}
    assert len({a["mcp_server_id"] for a in atts}) == 1


def test_tool_subset_follows_profile_tool_ids_order_not_membership(monkeypatch):
    import admin.seed_builtin_mcps as mod
    store = {
        "agent_tools": [
            {"tool_id": "t-a", "python_name": "get_a", "kind": "python",
             "agent_kind": "business", "additional_agent_kinds": [], "is_deprecated": False},
            {"tool_id": "t-b", "python_name": "get_b", "kind": "python",
             "agent_kind": "business", "additional_agent_kinds": [], "is_deprecated": False},
        ],
        "agent_profiles": [
            # tool_ids deliberately REVERSED vs the factory pool order (a, b)
            {"profile_id": "p-biz", "agent_kind": "business", "slug": "business",
             "tool_ids": ["t-b", "t-a"]},
        ],
        "agent_mcp_servers": [], "agent_mcp_tools": [], "agent_profile_mcp_servers": [],
    }
    monkeypatch.setattr(mod, "get_service_client", lambda: FakeClient(store))
    monkeypatch.setattr(mod, "_seedable_kinds", lambda: ["business"])
    monkeypatch.setattr(mod, "_pool_names_for_kind", lambda kind: ["get_a", "get_b"])  # pool order a,b

    mod.run()

    att = store["agent_profile_mcp_servers"][0]
    # tool_subset follows the profile's tool_ids order (b, a), NOT pool/membership order
    assert att["tool_subset"] == ["get_b", "get_a"]
    # membership position still follows pool order (a before b)
    by_id = {m["tool_id"]: m["position"] for m in store["agent_mcp_tools"]}
    assert by_id["t-a"] < by_id["t-b"]
