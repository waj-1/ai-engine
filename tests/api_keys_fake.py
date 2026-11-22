"""An in-memory stand-in for the `api_keys` table, shared by the API-key tests.

`FakeKeyStore` quacks like the supabase-py client for the calls the key code
makes (select / eq / in_ / order / limit / insert / update), filters for real,
and records every query so a test can assert what was (not) sent to the
database. `install(monkeypatch)` points both key modules at one store and
captures audit rows instead of writing them.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, store: "FakeKeyStore", table: str):
        self._store, self._table = store, table
        self._filters: List[tuple] = []
        self._insert = self._update = None
        self._columns = "*"
        self._limit = None

    def select(self, columns="*", *_a, **_k):
        self._columns = columns
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def in_(self, col, vals):
        self._filters.append(("in", col, list(vals)))
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, n, *_a, **_k):
        self._limit = n
        return self

    def insert(self, payload):
        self._insert = payload
        return self

    def update(self, payload):
        self._update = payload
        return self

    def _match(self, row):
        for kind, col, val in self._filters:
            if kind == "eq" and str(row.get(col)) != str(val):
                return False
            if kind == "in" and str(row.get(col)) not in [str(v) for v in val]:
                return False
        return True

    def execute(self):
        store = self._store
        store.queries.append({"table": self._table, "filters": list(self._filters),
                              "columns": self._columns,
                              "op": "insert" if self._insert is not None
                              else "update" if self._update is not None else "select"})
        if store.fail:
            raise store.fail
        rows = store.tables.setdefault(self._table, [])
        if self._insert is not None:
            row = {"id": str(uuid.uuid4()), "created_at": datetime.now(timezone.utc).isoformat(),
                   "last_used_at": None, "revoked_at": None, "revoked_by": None, **self._insert}
            rows.append(row)
            return _Result([dict(row)])
        if self._update is not None:
            hit = [r for r in rows if self._match(r)]
            for r in hit:
                r.update(self._update)
            return _Result([dict(r) for r in hit])
        hit = [r for r in rows if self._match(r)]
        if self._table == "api_keys":
            hit = sorted(hit, key=lambda r: r.get("created_at") or "", reverse=True)
        if self._limit is not None:
            hit = hit[: self._limit]
        if self._columns != "*":
            wanted = [c.strip() for c in self._columns.split(",")]
            return _Result([{c: r.get(c) for c in wanted} for r in hit])
        return _Result([dict(r) for r in hit])


class FakeKeyStore:
    def __init__(self):
        self.tables: Dict[str, List[Dict[str, Any]]] = {"api_keys": []}
        self.queries: List[Dict[str, Any]] = []
        self.audit: List[Dict[str, Any]] = []
        self.fail: Exception | None = None

    def table(self, name):
        return _Query(self, name)

    @property
    def rows(self) -> List[Dict[str, Any]]:
        return self.tables["api_keys"]

    def row(self, key_id: str) -> Dict[str, Any]:
        return next(r for r in self.rows if r["id"] == key_id)


def install(monkeypatch) -> FakeKeyStore:
    """Route the key modules to a fresh store; capture audit rows on it."""
    import admin.services.api_keys as service
    import core.api_keys as keys

    store = FakeKeyStore()
    monkeypatch.setattr(keys, "_client", lambda: store)
    monkeypatch.setattr(service, "_client", lambda: store)

    def _record(entity_type, entity_id, action, actor_user_id, before=None, after=None):
        store.audit.append({"entity_type": entity_type, "entity_id": entity_id, "action": action,
                            "actor_user_id": actor_user_id, "before": before, "after": after})

    monkeypatch.setattr(service, "record_change", _record)
    keys.reset_last_used_throttle()
    return store
