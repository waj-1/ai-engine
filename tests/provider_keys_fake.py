"""An in-memory stand-in for the Supabase client, for provider-key tests.

Supports what `core/provider_keys.py` and `admin/services/provider_keys.py`
use: select / eq / insert / update / delete / execute on named tables.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db: "FakeDB", table: str):
        self._db, self._table = db, table
        self._filters: List[tuple] = []
        self._op, self._payload = "select", None

    def select(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def _match(self, row):
        return all(row.get(c) == v for c, v in self._filters)

    def execute(self):
        self._db.calls.append((self._table, self._op))
        if self._db.fail:
            raise RuntimeError("database unavailable")
        rows = self._db.tables.setdefault(self._table, [])
        if self._op == "insert":
            row = dict(self._payload)
            row.setdefault("key_id", str(uuid.uuid4()))
            rows.append(row)
            return _Result([dict(row)])
        hits = [r for r in rows if self._match(r)]
        if self._op == "update":
            for r in hits:
                r.update(self._payload)
        elif self._op == "delete":
            self._db.tables[self._table] = [r for r in rows if not self._match(r)]
        return _Result([dict(r) for r in hits])


class FakeDB:
    def __init__(self, tables: Dict[str, List[Dict[str, Any]]] | None = None):
        self.tables: Dict[str, List[Dict[str, Any]]] = tables or {}
        self.calls: List[tuple] = []
        self.fail = False

    def table(self, name: str) -> _Query:
        return _Query(self, name)

    def reads(self, table: str = "provider_keys") -> int:
        return sum(1 for t, op in self.calls if t == table and op == "select")
