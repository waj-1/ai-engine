"""Tests for the pgvector shared-table defaulting logic in kb_create.

Task 7: When a pgvector KB is created without an explicit config.table, the
handler should default the config to:
  {"table": "ai_kb_embeddings", "pages_table": "ai_kb_pages"}

Rules:
  1. pgvector + empty/absent config  → gets shared-table defaults.
  2. pgvector + explicit config.table → left unchanged.
  3. Non-pgvector (e.g. gitbook)     → no shared-table defaults injected.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


# ---------------------------------------------------------------------------
# Minimal Supabase fake — mirrors the harness in test_kb_upload.py
# ---------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self, rows: List[Dict[str, Any]], inserted_rows: Optional[List[Dict[str, Any]]] = None):
        self._rows = rows
        self._inserted_rows = inserted_rows or []
        self.last_filter: Dict[str, Any] = {}
        self.last_inserts: List[Any] = []
        self.last_update: Optional[Dict[str, Any]] = None
        # Capture the most recent insert payload for assertions.
        self.last_insert_payload: Optional[Any] = None

    def select(self, _cols):
        return self

    def eq(self, col, val):
        self.last_filter[col] = val
        return self

    def limit(self, _n):
        return self

    def order(self, *_a, **_kw):
        return self

    def update(self, payload):
        self.last_update = payload
        return self

    def insert(self, payload):
        self.last_insert_payload = payload
        self.last_inserts.append(payload)
        return _InsertResult(payload, self._inserted_rows)

    def execute(self):
        result = MagicMock()
        result.data = list(self._rows)
        return result


class _InsertResult:
    def __init__(self, payload, override_rows):
        self._payload = payload
        self._override = override_rows

    def execute(self):
        result = MagicMock()
        if self._override:
            result.data = list(self._override)
        elif isinstance(self._payload, list):
            result.data = [{**r} for r in self._payload]
        else:
            result.data = [{**self._payload}]
        return result


class _FakeClient:
    """Scriptable Supabase client that records all insert payloads."""

    def __init__(self, rows_by_table=None, inserts_by_table=None):
        self._rows_by_table = rows_by_table or {}
        self._inserts_by_table = inserts_by_table or {}
        self.queries: Dict[str, _FakeQuery] = {}

    def table(self, name):
        if name not in self.queries:
            self.queries[name] = _FakeQuery(
                self._rows_by_table.get(name, []),
                self._inserts_by_table.get(name),
            )
        return self.queries[name]


def _make_app(fake_client):
    """Wire the FastAPI test client with auth + DB overrides."""
    from fastapi.testclient import TestClient
    from admin import auth, kb_handlers
    from main import app

    def _fake_super():
        return {"user_token": "tok", "user_id": "u-1", "email": "a@waj.test"}

    app.dependency_overrides[auth.require_superuser] = _fake_super
    patches = [
        patch.object(kb_handlers, "get_service_client", return_value=fake_client),
        patch.object(kb_handlers, "record_change"),
    ]
    for p in patches:
        p.start()
    client = TestClient(app)

    def _teardown():
        for p in patches:
            p.stop()
        app.dependency_overrides.pop(auth.require_superuser, None)

    return client, _teardown


# ---------------------------------------------------------------------------
# Helper: post to POST /admin/kb and return the captured insert payload
# ---------------------------------------------------------------------------

def _create_kb(fake_client, *, name="My KB", kind="pgvector", config_json="{}"):
    """POST /admin/kb (create) and return the raw insert payload dict."""
    client, teardown = _make_app(fake_client)
    try:
        # Insert returns a fake row with a source_id so the redirect resolves.
        fake_client._inserts_by_table["agent_data_sources"] = [
            {"source_id": "new-src-1", "name": name, "kind": kind, "config": {}}
        ]
        # Rebuild the query object so the override is picked up.
        fake_client.queries.clear()

        r = client.post(
            "/admin/kb",
            data={"name": name, "kind": kind, "config": config_json},
            follow_redirects=False,
        )
        assert r.status_code == 302, f"expected 302 redirect, got {r.status_code}: {r.text[:300]}"
        insert_payload = fake_client.queries["agent_data_sources"].last_insert_payload
        assert insert_payload is not None, "no insert was made to agent_data_sources"
        return insert_payload
    finally:
        teardown()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_pgvector_empty_config_gets_shared_table_defaults():
    """pgvector + empty JSON config → shared-table defaults injected."""
    fake = _FakeClient()
    payload = _create_kb(fake, kind="pgvector", config_json="{}")

    assert payload["config"] == {
        "table": "ai_kb_embeddings",
        "pages_table": "ai_kb_pages",
    }, f"unexpected config: {payload['config']}"


def test_pgvector_no_config_field_gets_shared_table_defaults():
    """pgvector with config field omitted entirely → shared-table defaults injected."""
    fake = _FakeClient()
    # Simulate config field being absent from the form by posting without it.
    client, teardown = _make_app(fake)
    try:
        fake._inserts_by_table["agent_data_sources"] = [
            {"source_id": "new-src-2", "name": "KB2", "kind": "pgvector", "config": {}}
        ]
        fake.queries.clear()

        r = client.post(
            "/admin/kb",
            data={"name": "KB2", "kind": "pgvector"},  # no config key at all
            follow_redirects=False,
        )
        assert r.status_code == 302, f"expected 302, got {r.status_code}: {r.text[:300]}"
        insert_payload = fake.queries["agent_data_sources"].last_insert_payload
        assert insert_payload is not None

        assert insert_payload["config"] == {
            "table": "ai_kb_embeddings",
            "pages_table": "ai_kb_pages",
        }, f"unexpected config: {insert_payload['config']}"
    finally:
        teardown()


def test_pgvector_explicit_table_is_not_overwritten():
    """pgvector with explicit config.table → config left unchanged."""
    fake = _FakeClient()
    explicit_cfg = '{"table": "ai_documentation_embeddings"}'
    payload = _create_kb(fake, kind="pgvector", config_json=explicit_cfg)

    assert payload["config"].get("table") == "ai_documentation_embeddings", (
        f"explicit table was overwritten: {payload['config']}"
    )
    # pages_table should not be injected either.
    assert "pages_table" not in payload["config"], (
        f"pages_table was unexpectedly added: {payload['config']}"
    )


def test_gitbook_source_does_not_get_shared_table_defaults():
    """Non-pgvector (gitbook) → config left untouched, no shared-table injection."""
    fake = _FakeClient()
    gitbook_cfg = '{"space_id": "abc123"}'
    payload = _create_kb(fake, kind="gitbook", config_json=gitbook_cfg)

    assert payload["config"].get("table") is None, (
        f"shared-table default wrongly injected into gitbook KB: {payload['config']}"
    )
    assert payload["config"].get("space_id") == "abc123"
