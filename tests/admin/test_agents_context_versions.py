"""load_agents_context surfaces the active profile's version history
(agent_profile_versions, via admin.services.profile_versions.list_versions)
and which version is currently pinned/served
(agent_profiles.served_version_id) — the data behind the Agents editor's
version bar (Phase P1).
"""
from __future__ import annotations

from typing import Any, Dict, List


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    """Minimal chainable postgrest-style query with real .eq() filtering —
    same shape as tests/admin/test_agents_context_mcp_attachments.py's fake."""

    def __init__(self, rows):
        self._rows = list(rows)

    def select(self, *_):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) == val]
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
        "agent_profiles": [
            {"profile_id": "p1", "agent_kind": "sales", "environment": "staging",
             "status": "active", "is_active": True, "served_version_id": "v2"},
        ],
    }


def test_context_carries_profile_versions_and_served_pin(monkeypatch):
    import admin.screens.agents as ag

    fake = FakeSupabase(_tables())
    monkeypatch.setattr("admin.router.client_for", lambda env: fake)
    monkeypatch.setattr(
        "admin.screens.agents.pv.list_versions",
        lambda profile_id, environment: [
            {"version_id": "v2", "version_number": 2, "is_served": True},
            {"version_id": "v1", "version_number": 1, "is_served": False},
        ],
    )

    ctx = ag.load_agents_context(env="staging", selected_kind="sales")

    assert ctx["active_profile"]["profile_id"] == "p1"
    assert ctx["profile_versions"][0]["version_id"] == "v2"
    assert ctx["served_version_id"] == "v2"


def test_context_versions_empty_and_served_none_when_no_active_profile(monkeypatch):
    import admin.screens.agents as ag

    fake = FakeSupabase({"agent_profiles": []})
    monkeypatch.setattr("admin.router.client_for", lambda env: fake)

    calls = []
    monkeypatch.setattr(
        "admin.screens.agents.pv.list_versions",
        lambda profile_id, environment: calls.append((profile_id, environment)) or [],
    )

    ctx = ag.load_agents_context(env="staging", selected_kind=None)

    assert ctx["active_profile"] is None
    assert ctx["profile_versions"] == []
    assert ctx["served_version_id"] is None
    # No active profile -> list_versions must not be called at all.
    assert calls == []
