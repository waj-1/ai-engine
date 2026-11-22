"""Tests for `core.tool_registry.sync_to_db`.

Two behaviours we want to lock in:
  1. The pre-sync SELECT pulls both `python_name` AND `kind` so HTTP
     rows can be excluded from deprecation.
  2. Rows with `kind='http'` are never deprecated by a Python-only sync,
     even when they're missing from the in-memory spec list.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


# ---------------------------------------------------------------------------
# Recording fake supabase client
# ---------------------------------------------------------------------------


class _Query:
    """A query builder that records every chain call so the test can
    introspect what was queried and what payloads landed where."""

    def __init__(self, client, table_name):
        self._client = client
        self._table_name = table_name
        # Per-table-state on the parent client
        self._selected_columns: List[str] = []
        self._action: str = ""
        self._action_payload: Any = None
        self._filters: List[tuple] = []

    def select(self, cols):
        self._client.select_calls.append((self._table_name, cols))
        self._action = "select"
        return self

    def insert(self, payload):
        self._client.insert_calls.append((self._table_name, payload))
        self._action = "insert"
        return self

    def update(self, payload):
        self._action = "update"
        self._action_payload = payload
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def in_(self, col, vals):
        vals = list(vals)
        if self._action == "update":
            # Record the update→in_ chain — this is the deprecation call.
            self._client.deprecation_updates.append({
                "table": self._table_name,
                "column": col,
                "values": vals,
                "payload": self._action_payload,
            })
        self._filters.append(("in_", col, vals))
        return self

    def execute(self):
        result = MagicMock()
        if self._action == "select" and self._table_name == "agent_tools":
            result.data = self._client.select_rows
        else:
            result.data = []
        return result


class _FakeClient:
    def __init__(self, select_rows=None):
        self.select_rows = select_rows or []
        self.select_calls: List[tuple] = []
        self.insert_calls: List[tuple] = []
        self.deprecation_updates: List[Dict[str, Any]] = []

    def table(self, name):
        return _Query(self, name)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_sync_to_db_selects_python_name_and_kind(monkeypatch):
    """Lock in the column list — if we drop `kind` here the HTTP-tool
    protection silently regresses."""
    from core import tool_registry

    fake = _FakeClient(select_rows=[])
    monkeypatch.setattr(tool_registry, "get_service_client", lambda: fake)
    # Empty registry → only the initial select runs.
    monkeypatch.setattr(tool_registry, "_REGISTRY", {})

    tool_registry.sync_to_db()

    # First SELECT on agent_tools should request both columns.
    selects_on_tools = [call for call in fake.select_calls if call[0] == "agent_tools"]
    assert selects_on_tools, "expected at least one SELECT on agent_tools"
    cols = selects_on_tools[0][1]
    # Accept either "python_name,kind" or "python_name, kind" — both are
    # valid PostgREST column lists.
    normalised = cols.replace(" ", "")
    assert "python_name" in normalised
    assert "kind" in normalised


def test_sync_to_db_only_deprecates_python_rows(monkeypatch):
    """An HTTP-kind row that's not in the registry must NOT be flagged
    deprecated. Only Python rows missing from the spec list should be."""
    from core import tool_registry
    from core.tool_registry import ToolSpec

    # DB has 3 rows pre-existing — 1 Python (present in registry), 1
    # Python (NOT in registry, should be deprecated), 1 HTTP (not in the
    # registry but must be left alone).
    fake = _FakeClient(select_rows=[
        {"python_name": "keep_py", "kind": "python"},
        {"python_name": "stale_py", "kind": "python"},
        {"python_name": "http_only", "kind": "http"},
    ])
    monkeypatch.setattr(tool_registry, "get_service_client", lambda: fake)

    # Registry contains just the "keep_py" Python tool.
    spec = ToolSpec(
        python_name="keep_py",
        display_name="Keep",
        description="kept",
        category="business",
        scope="read",
        source_file="runtime/tools/business.py",
        argument_schema={},
        agent_kind="business",
    )
    monkeypatch.setattr(tool_registry, "_REGISTRY", {"keep_py": spec})

    counts = tool_registry.sync_to_db()

    # Exactly one deprecation update should have run, targeting only stale_py.
    assert len(fake.deprecation_updates) == 1
    dep = fake.deprecation_updates[0]
    assert dep["table"] == "agent_tools"
    assert dep["column"] == "python_name"
    assert dep["values"] == ["stale_py"]
    assert dep["payload"] == {"is_deprecated": True}
    # And "http_only" must NOT appear in the deprecation set.
    assert "http_only" not in dep["values"]
    assert counts["deprecated"] == 1


def test_sync_to_db_no_deprecation_when_all_python_present(monkeypatch):
    """When every Python row in the DB matches a spec, no deprecation
    update should run at all (avoids a no-op write each boot)."""
    from core import tool_registry
    from core.tool_registry import ToolSpec

    fake = _FakeClient(select_rows=[
        {"python_name": "keep_py", "kind": "python"},
        {"python_name": "http_only", "kind": "http"},
    ])
    monkeypatch.setattr(tool_registry, "get_service_client", lambda: fake)

    spec = ToolSpec(
        python_name="keep_py",
        display_name="Keep",
        description="kept",
        category="business",
        scope="read",
        source_file="runtime/tools/business.py",
        argument_schema={},
        agent_kind="business",
    )
    monkeypatch.setattr(tool_registry, "_REGISTRY", {"keep_py": spec})

    counts = tool_registry.sync_to_db()
    assert fake.deprecation_updates == []
    assert counts["deprecated"] == 0
