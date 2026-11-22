"""load_agents_context surfaces the active profile's MCP attachments + the
attachable MCPs (their exposable tool names) for the Phase 3b attachment
editor (see docs/superpowers/specs/2026-07-05-portal-mcp-tools-design.md).

Both additions are additive: the legacy ``mcp_servers`` / ``mapped.mcp_ids``
(``agent_profiles.mcp_server_ids`` array) path must keep working untouched.
"""
from __future__ import annotations

from typing import Any, Dict, List
from tests.scoped_build import use_orchard


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    """Minimal chainable postgrest-style query with REAL filtering, mirroring
    tests/runtime/test_profile_mcp_attachments.py's fake so both
    _attachable_mcps and _fetch_mcp_attachments (which uses .in_()) work
    against it."""

    def __init__(self, rows):
        self._rows = list(rows)

    def select(self, *_):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def in_(self, col, vals):
        allowed = set(vals)
        self._rows = [r for r in self._rows if r.get(col) in allowed]
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def execute(self):
        return _Result(self._rows)


class FakeSupabase:
    def __init__(self, tables: Dict[str, List[Dict[str, Any]]]):
        self._tables = tables

    def table(self, name):
        return _Query(self._tables.get(name, []))


def _tables() -> Dict[str, List[Dict[str, Any]]]:
    return {
        "agent_mcp_servers": [
            {"mcp_server_id": "u-bundle", "name": "Sales Docs", "source": "bundle",
             "is_builtin": False, "is_active": True},
            {"mcp_server_id": "u-ext", "name": "partner-reports", "source": "external",
             "is_builtin": False, "is_active": True, "tool_subset": ["fetch_report"]},
            {"mcp_server_id": "u-inactive", "name": "Old Bundle", "source": "bundle",
             "is_builtin": False, "is_active": False},
        ],
        "agent_mcp_tools": [
            {"mcp_server_id": "u-bundle", "tool_id": "t1", "position": 0},
            {"mcp_server_id": "u-bundle", "tool_id": "t2", "position": 1},
        ],
        "agent_tools": [
            {"tool_id": "t1", "python_name": "search_docs", "is_deprecated": False},
            {"tool_id": "t2", "python_name": "old_tool", "is_deprecated": True},
        ],
        "agent_profile_mcp_servers": [
            {"profile_id": "p1", "mcp_server_id": "u-bundle",
             "tool_subset": ["search_docs"], "position": 0},
        ],
    }


# --------------------------------------------------------------------------- #
# _attachable_mcps(client) — the focused, pure-ish helper
# --------------------------------------------------------------------------- #
def test_attachable_mcps_bundle_tool_names_from_membership_non_deprecated():
    from admin.screens.agents import _attachable_mcps

    result = _attachable_mcps(FakeSupabase(_tables()))
    by_id = {r["mcp_server_id"]: r for r in result}

    bundle = by_id["u-bundle"]
    assert bundle["source"] == "bundle"
    assert bundle["name"] == "Sales Docs"
    # deprecated old_tool (t2) is excluded; only search_docs (t1) surfaces.
    assert bundle["tool_names"] == ["search_docs"]


def test_attachable_mcps_external_tool_names_from_tool_subset():
    from admin.screens.agents import _attachable_mcps

    result = _attachable_mcps(FakeSupabase(_tables()))
    by_id = {r["mcp_server_id"]: r for r in result}

    ext = by_id["u-ext"]
    assert ext["source"] == "external"
    assert ext["tool_names"] == ["fetch_report"]


def test_attachable_mcps_external_without_tool_subset_is_empty_list():
    from admin.screens.agents import _attachable_mcps

    tables = _tables()
    tables["agent_mcp_servers"][1].pop("tool_subset")
    result = _attachable_mcps(FakeSupabase(tables))
    ext = next(r for r in result if r["mcp_server_id"] == "u-ext")
    assert ext["tool_names"] == []


def test_attachable_mcps_excludes_inactive_servers():
    from admin.screens.agents import _attachable_mcps

    result = _attachable_mcps(FakeSupabase(_tables()))
    ids = {r["mcp_server_id"] for r in result}
    assert "u-inactive" not in ids


# --------------------------------------------------------------------------- #
# load_agents_context wiring
# --------------------------------------------------------------------------- #
def test_context_has_attachments_and_attachable(monkeypatch):
    import admin.screens.agents as ag

    tables = _tables()
    tables["agent_profiles"] = [
        {"profile_id": "p1", "agent_kind": "business", "environment": "staging",
         "status": "active", "is_active": True},
    ]
    fake = FakeSupabase(tables)
    monkeypatch.setattr("admin.router.client_for", lambda env: fake)

    ctx = ag.load_agents_context(env="staging", selected_kind=None)

    assert ctx["active_profile"]["profile_id"] == "p1"

    # mcp_attachments: the p1 -> u-bundle join row, denormalized via
    # core.agent_profile._fetch_mcp_attachments.
    atts = ctx["mcp_attachments"]
    assert len(atts) == 1
    assert atts[0]["mcp_server"]["name"] == "Sales Docs"
    assert atts[0]["tool_subset"] == ["search_docs"]

    # attachable_mcps: every active server enriched with tool_names.
    names_by_id = {r["mcp_server_id"]: r["tool_names"] for r in ctx["attachable_mcps"]}
    assert names_by_id["u-bundle"] == ["search_docs"]
    assert names_by_id["u-ext"] == ["fetch_report"]
    assert "u-inactive" not in names_by_id

    # Legacy keys are untouched by this change.
    assert "mcp_servers" in ctx
    assert ctx["mapped"]["mcp_ids"] == set()


def test_context_mcp_keys_empty_when_no_active_profile(monkeypatch):
    import admin.screens.agents as ag

    fake = FakeSupabase({"agent_profiles": []})
    monkeypatch.setattr("admin.router.client_for", lambda env: fake)

    ctx = ag.load_agents_context(env="staging", selected_kind=None)

    assert ctx["active_profile"] is None
    assert ctx["mcp_attachments"] == []
    assert ctx["attachable_mcps"] == []


# --------------------------------------------------------------------------- #
# Review round 1: resilience + deployment-scope gate
# --------------------------------------------------------------------------- #
def test_attachable_mcps_degrades_to_empty_on_db_error():
    """A transient query failure must degrade to [] (logged), never 500 the
    Agents page — same posture as core.agent_profile._fetch_mcp_attachments,
    since _attachable_mcps runs unguarded on every /admin/agents load."""
    from admin.screens.agents import _attachable_mcps

    class _Exploding:
        def table(self, name):
            raise RuntimeError("boom")

    assert _attachable_mcps(_Exploding()) == []


def test_attachable_mcps_scope_filtered():
    """On a scoped build the picker is restricted to servers a visible
    (tenant) profile references — via the legacy mcp_server_ids array OR the
    new agent_profile_mcp_servers join table — so a full-build-only bundle a
    partner's operator never referenced is EXCLUDED."""
    from admin.screens.agents import _attachable_mcps

    tables = _tables()
    # A WAJ-only server no visible profile references.
    tables["agent_mcp_servers"].append({
        "mcp_server_id": "waj-only", "name": "WAJ Internal", "source": "external",
        "is_builtin": False, "is_active": True, "tool_subset": ["waj_tool"],
    })
    # Tenant's visible profile: references u-ext via the legacy array and
    # u-bundle via the join table (agent_profile_mcp_servers p1 -> u-bundle).
    visible = [{"profile_id": "p1", "mcp_server_ids": ["u-ext"]}]

    result = _attachable_mcps(FakeSupabase(tables), visible_profiles=visible)
    ids = {r["mcp_server_id"] for r in result}

    assert "waj-only" not in ids                # cross-tenant server hidden
    assert ids == {"u-ext", "u-bundle"}         # legacy-array + join-table refs kept


def test_context_attachable_mcps_scope_gated(monkeypatch):
    """End-to-end through load_agents_context on a scoped build: proves the
    scoped_build() gate is actually wired into the call."""
    use_orchard(monkeypatch)
    import admin.screens.agents as ag

    tables = _tables()
    tables["agent_mcp_servers"].append({
        "mcp_server_id": "waj-only", "name": "WAJ Internal", "source": "external",
        "is_builtin": False, "is_active": True, "tool_subset": ["waj_tool"],
    })
    tables["agent_profiles"] = [
        {"profile_id": "p1", "agent_kind": "orchard", "environment": "staging",
         "status": "active", "is_active": True, "mcp_server_ids": ["u-ext"]},
    ]
    fake = FakeSupabase(tables)
    monkeypatch.setattr("admin.router.client_for", lambda env: fake)

    ctx = ag.load_agents_context(env="staging", selected_kind=None)
    ids = {r["mcp_server_id"] for r in ctx["attachable_mcps"]}

    assert "waj-only" not in ids
    assert "u-ext" in ids and "u-bundle" in ids


def test_attachable_mcps_batches_the_tool_lookup_so_the_url_stays_short():
    """An `in.(…)` filter rides the URL. With a full engine's 200+ tools in one
    bundle, a single unbatched filter was a 414 behind the bundled gateway,
    which this function swallows into an EMPTY list — no MCP could be attached
    to any agent. Every batch must stay small, and nothing may be lost."""
    from admin.screens import agents as agents_screen

    n = 217
    tools = [{"tool_id": f"t-{i}", "python_name": f"tool_{i}", "is_deprecated": False} for i in range(n)]
    members = [{"mcp_server_id": "u-bundle", "tool_id": f"t-{i}", "position": i} for i in range(n)]
    batches: List[int] = []

    class _CountingQuery(_Query):
        def in_(self, col, vals):
            if col == "tool_id":
                batches.append(len(list(vals)))
            return super().in_(col, vals)

    class _Client(FakeSupabase):
        def table(self, name):
            return _CountingQuery(self._tables.get(name, []))

    client = _Client({
        "agent_mcp_servers": [{"mcp_server_id": "u-bundle", "name": "Big bundle", "source": "bundle",
                               "is_active": True, "is_builtin": True}],
        "agent_mcp_tools": members,
        "agent_tools": tools,
    })
    result = agents_screen._attachable_mcps(client)
    assert len(result) == 1
    assert result[0]["tool_names"] == [f"tool_{i}" for i in range(n)]  # all of them, in order
    assert batches and max(batches) <= 100
    assert sum(batches) == n
