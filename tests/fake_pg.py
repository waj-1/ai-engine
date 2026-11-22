"""An in-memory stand-in for the supabase-py query builder, for tests that
read and write a few tables: select / insert / update / delete with eq, in_,
is_, gte, ilike, or_ (created_at/updated_at windows), order and limit."""
from __future__ import annotations

import uuid
from typing import Any, Dict, List

_PK = {"agent_models": "model_id", "agent_profiles": "profile_id", "agent_prompts": "prompt_id",
       "agent_prompt_versions": "prompt_version_id", "agent_env_vars": "var_id"}


class _Res:
    def __init__(self, data):
        self.data = data
        self.count = len(data)


class Query:
    def __init__(self, db, table):
        self.db, self.table, self.filters, self.op, self.payload = db, table, [], "select", None
        self._limit, self._order = None, None

    def select(self, *_a, **_k):
        self.op = "select"
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def in_(self, col, vals):
        vals = {str(v) for v in vals}
        self.filters.append(lambda r: str(r.get(col)) in vals)
        return self

    def is_(self, col, val):
        assert val == "null"
        self.filters.append(lambda r: r.get(col) is None)
        return self

    def gte(self, col, val):
        self.filters.append(lambda r: str(r.get(col) or "") >= str(val))
        return self

    def ilike(self, col, pattern):
        needle = pattern.strip("%").replace("\\%", "%").replace("\\_", "_").replace("\\\\", "\\").lower()
        self.filters.append(lambda r: needle in str(r.get(col) or "").lower())
        return self

    def or_(self, expr):
        parts = [p.split(".", 2) for p in expr.split(",")]
        assert all(op == "gte" for _c, op, _v in parts), expr
        self.filters.append(lambda r: any(str(r.get(c) or "") >= v for c, _op, v in parts))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def order(self, col, desc=False):
        self._order = (col, desc)
        return self

    def execute(self):
        rows = self.db.setdefault(self.table, [])
        self.db.setdefault("_calls", []).append((self.table, self.op))
        if self.op == "insert":
            out = []
            for item in (self.payload if isinstance(self.payload, list) else [self.payload]):
                row = dict(item)
                pk = _PK.get(self.table)
                if pk and pk not in row:
                    row[pk] = str(uuid.uuid4())
                rows.append(row)
                out.append(row)
            return _Res(out)
        hits: List[Dict[str, Any]] = [r for r in rows if all(f(r) for f in self.filters)]
        if self.op == "update":
            for r in hits:
                r.update(self.payload)
            return _Res(hits)
        if self.op == "delete":
            for r in hits:
                rows.remove(r)
            return _Res(hits)
        if self._order:
            col, desc = self._order
            hits = sorted(hits, key=lambda r: str(r.get(col) or ""), reverse=desc)
        return _Res(hits[: self._limit] if self._limit else hits)


class Client:
    def __init__(self, db):
        self.db = db

    def schema(self, _name):
        return self

    def table(self, name):
        return Query(self.db, name)
