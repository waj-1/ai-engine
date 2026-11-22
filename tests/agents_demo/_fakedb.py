"""A tiny in-memory stand-in for the supabase-py query builder — enough of
select / insert / update / delete / eq / in_ / order / limit for the demo
seeder and the inbound prompt-only rollout script."""
from __future__ import annotations

import uuid
from typing import Any, Dict, List

_PK = {"agent_tools": "tool_id", "agent_prompts": "prompt_id", "agent_prompt_versions": "prompt_version_id",
       "agent_profiles": "profile_id", "agent_data_sources": "source_id", "agent_mcp_servers": "mcp_server_id"}


class _Res:
    def __init__(self, data):
        self.data = data


class _Q:
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
        vals = set(vals)
        self.filters.append(lambda r: r.get(col) in vals)
        return self

    def contains(self, col, vals):
        self.filters.append(lambda r: set(vals) <= set(r.get(col) or []))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def order(self, col, desc=False):
        self._order = (col, desc)
        return self

    def _match(self) -> List[Dict[str, Any]]:
        return [r for r in self.db.setdefault(self.table, []) if all(f(r) for f in self.filters)]

    def execute(self):
        rows = self.db.setdefault(self.table, [])
        if self.op == "insert":
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            out = []
            for item in items:
                row = dict(item)
                pk = _PK.get(self.table)
                if pk and pk not in row:
                    row[pk] = str(uuid.uuid4())
                rows.append(row)
                out.append(row)
            self.db.setdefault("_inserts", []).append(self.table)
            return _Res(out)
        hits = self._match()
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
            hits = sorted(hits, key=lambda r: r.get(col) or 0, reverse=desc)
        return _Res(hits[: self._limit] if self._limit else hits)


class _Client:
    def __init__(self, db):
        self.db = db

    def table(self, name):
        return _Q(self.db, name)
