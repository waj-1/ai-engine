"""seed_agent_profiles must bridge the dev→runtime-env gap locally.

The migration seeds agent_profiles in the ``dev`` environment, but the runtime
collapses local ``ENVIRONMENT=development`` to ``staging`` and looks up rows by
that env — so locally there were no matching rows and every chat (incl. the
admin "Test agent" widget) 500'd with MissingProfileError. The clone step makes
the resolved env serveable; deployed staging/prod must be left untouched.
"""
from __future__ import annotations

import copy


class _Builder:
    def __init__(self, rows, sink):
        self._rows = rows
        self._sink = sink
        self._filters = {}

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def insert(self, payload):
        self._pending_insert = payload
        return self

    def execute(self):
        if getattr(self, "_pending_insert", None) is not None:
            row = dict(self._pending_insert)
            self._sink.append(row)
            self._rows.append(row)
            return type("Res", (), {"data": [row]})()
        out = [
            r for r in self._rows
            if all(r.get(c) == v for c, v in self._filters.items())
        ]
        return type("Res", (), {"data": copy.deepcopy(out)})()


class _FakeClient:
    def __init__(self, rows):
        self.rows = rows
        self.inserted = []

    def table(self, name):
        assert name == "agent_profiles"
        return _Builder(self.rows, self.inserted)


def _dev_rows():
    return [
        {"agent_kind": "business", "slug": "default", "environment": "dev",
         "is_active": True, "status": "active", "profile_id": "p1",
         "tool_ids": ["t1"], "created_at": "x", "updated_at": "y"},
        {"agent_kind": "sales", "slug": "default", "environment": "dev",
         "is_active": True, "status": "active", "profile_id": "p2",
         "tool_ids": [], "created_at": "x", "updated_at": "y"},
    ]


def test_clone_copies_dev_into_staging_locally(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    from admin import seed_agent_profiles as s

    client = _FakeClient(_dev_rows())
    cloned = s._clone_dev_profiles_into_runtime_env(client)

    assert cloned == 2
    staging = [r for r in client.rows if r["environment"] == "staging"]
    assert {r["agent_kind"] for r in staging} == {"business", "sales"}
    # FK arrays are carried verbatim; volatile/PK columns are dropped.
    biz = next(r for r in staging if r["agent_kind"] == "business")
    assert biz["tool_ids"] == ["t1"]
    assert "profile_id" not in biz and "created_at" not in biz


def test_clone_is_idempotent(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    from admin import seed_agent_profiles as s

    client = _FakeClient(_dev_rows())
    assert s._clone_dev_profiles_into_runtime_env(client) == 2
    # Second pass: staging rows already exist → nothing new.
    assert s._clone_dev_profiles_into_runtime_env(client) == 0


def test_clone_skips_when_not_local(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "staging")
    from admin import seed_agent_profiles as s

    assert s._is_local() is False
