"""In-memory stand-ins for what first-run setup talks to: the engine's
database (`engine_setup`), the main database (`user`) and the auth service's
admin API. One `FakeStack` is all three, as on the bundled stack.

Filters are applied for real (eq, is_ null, lt, a `col->>key` JSON filter),
and every write is applied under a lock, so a conditional UPDATE is atomic the
way a single Postgres statement is: that is what the "only one admin" tests
rely on.
"""
from __future__ import annotations

import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List, Optional


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, stack: "FakeStack", table: str):
        self._stack, self._table = stack, table
        self._filters: List[tuple] = []
        self._op, self._payload, self._limit = "select", None, None

    def select(self, *_a, **_k):
        return self

    def limit(self, n, *_a, **_k):
        self._limit = n
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def is_(self, col, val):
        self._filters.append(("is", col, val))
        return self

    def lt(self, col, val):
        self._filters.append(("lt", col, val))
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def upsert(self, payload):
        self._op, self._payload = "upsert", payload
        return self

    def _value(self, row, col):
        if "->>" in col:
            base, key = col.split("->>", 1)
            inner = (row.get(base) or {}).get(key)
            return None if inner is None else str(inner).lower()
        return row.get(col)

    def _match(self, row) -> bool:
        for kind, col, val in self._filters:
            got = self._value(row, col)
            if kind == "eq" and str(got) != str(val):
                return False
            if kind == "is" and not (val == "null" and got is None):
                return False
            if kind == "lt" and not (got is not None and str(got) < str(val)):
                return False
        return True

    def execute(self):
        stack = self._stack
        with stack.lock:
            stack.queries.append((self._table, self._op))
            if stack.fail:
                raise RuntimeError("database unavailable")
            if self._table in stack.fail_tables:
                raise RuntimeError(f"{self._table} unavailable")
            rows = stack.tables.setdefault(self._table, [])
            if self._op == "insert":
                row = dict(self._payload)
                if self._table == "user":
                    row.setdefault("id", stack.next_user_id or str(uuid.uuid4()))
                rows.append(row)
                return _Result([dict(row)])
            if self._op == "upsert":
                hit = [r for r in rows if r.get("id") == self._payload.get("id")]
                if hit:
                    hit[0].update(self._payload)
                    return _Result([dict(hit[0])])
                row = {"failed_attempts": 0, "claimed_at": None, "completed_at": None,
                       "completed_by": None, "finished_at": None, **self._payload}
                rows.append(row)
                return _Result([dict(row)])
            hit = [r for r in rows if self._match(r)]
            if self._op == "update":
                for r in hit:
                    r.update(self._payload)
                return _Result([dict(r) for r in hit])
            if self._limit is not None:
                hit = hit[: self._limit]
            return _Result([dict(r) for r in hit])


class _AuthAdmin:
    def __init__(self, stack: "FakeStack"):
        self._stack = stack

    def create_user(self, attrs: Dict[str, Any]):
        stack = self._stack
        stack.create_calls.append(dict(attrs))
        if stack.create_delay:
            time.sleep(stack.create_delay)
        if stack.create_error:
            raise RuntimeError(stack.create_error)
        with stack.lock:
            if any(u.email.lower() == attrs["email"].lower() for u in stack.auth_users):
                raise RuntimeError("A user with this email address has already been registered")
            user = SimpleNamespace(id=str(uuid.uuid4()), email=attrs["email"],
                                   password=attrs["password"])
            stack.auth_users.append(user)
        return SimpleNamespace(user=user)

    def list_users(self):
        return list(self._stack.auth_users)

    def update_user_by_id(self, auth_id, attrs):
        for user in self._stack.auth_users:
            if user.id == auth_id:
                user.password = attrs.get("password", user.password)
        self._stack.password_resets.append(auth_id)

    def delete_user(self, auth_id):
        self._stack.auth_users[:] = [u for u in self._stack.auth_users if u.id != auth_id]
        self._stack.deleted.append(auth_id)


class FakeStack:
    def __init__(self):
        self.lock = threading.RLock()
        self.tables: Dict[str, List[Dict[str, Any]]] = {"user": [], "engine_setup": []}
        self.queries: List[tuple] = []
        self.fail = False
        self.fail_tables: set = set()
        self.auth_users: List[Any] = []
        self.create_calls: List[Dict[str, Any]] = []
        self.password_resets: List[str] = []
        self.deleted: List[str] = []
        self.create_error: Optional[str] = None
        self.create_delay = 0.0
        self.next_user_id: Optional[str] = None
        self.auth = SimpleNamespace(admin=_AuthAdmin(self))

    def table(self, name):
        return _Query(self, name)

    @property
    def setup_row(self) -> Dict[str, Any]:
        return self.tables["engine_setup"][0]

    @property
    def superusers(self) -> List[Dict[str, Any]]:
        return [r for r in self.tables["user"] if (r.get("permission") or {}).get("is_superuser")]

    def add_superuser(self, email="owner@corp.test") -> Dict[str, Any]:
        row = {"id": str(uuid.uuid4()), "auth_id": str(uuid.uuid4()), "email": email,
               "permission": {"is_superuser": True}, "role": "admin"}
        self.tables["user"].append(row)
        return row
