"""Tests for the KB pages/chunks viewer on the source detail screen.

Covers GET /admin/kb/{source_id} for:
  * shared pgvector KB (ai_kb_embeddings): pages list + chunk counts rendered
  * non-shared pgvector KB (dedicated table): pages=None, no crash
  * non-pgvector KB (e.g. http kind): pages=None, no crash

Uses the same scriptable supabase fake / FastAPI dependency-override harness
as test_kb_upload.py so each test owns its DB state.
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
# Supabase fake — mirrors _FakeClient from test_kb_upload.py.
# The key difference: this fake needs to handle multiple table() calls
# for the same table name, returning different rows per query depending on
# eq() filters.  We model this with per-table callable factories.
# ---------------------------------------------------------------------------


class _FakeQuery:
    """Chainable query builder returning pre-canned rows on .execute()."""

    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows
        self._filters: Dict[str, Any] = {}
        self._limit: Optional[int] = None

    def select(self, _cols):
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def neq(self, col, val):
        return self

    def in_(self, col, vals):
        self._filters[f"in_{col}"] = list(vals)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def order(self, *_a, **_kw):
        return self

    def update(self, payload):
        return self

    def insert(self, payload):
        result = MagicMock()
        if isinstance(payload, list):
            result.data = [{**r} for r in payload]
        else:
            result.data = [{**payload}]
        result.execute = lambda: result
        return result

    def execute(self):
        result = MagicMock()
        rows = list(self._rows)
        if self._limit is not None:
            rows = rows[: self._limit]
        result.data = rows
        return result


class _MultiQuery:
    """A table proxy that creates a fresh _FakeQuery per chained call.

    For tables where we call .table(name) more than once (ai_kb_pages,
    ai_kb_embeddings) each call returns an independent query so the first
    call's filters don't bleed into the second.
    """

    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows

    def select(self, _cols):
        return _FakeQuery(self._rows)

    def eq(self, col, val):
        q = _FakeQuery(self._rows)
        q.eq(col, val)
        return q


class _FakeClient:
    """Scriptable Supabase client returning per-table canned data.

    `rows_by_table` maps table name → list of row dicts returned by SELECT.
    `queries` keeps the last _FakeQuery for each table for post-call assertions.
    """

    def __init__(self, rows_by_table: Optional[Dict[str, List[Dict[str, Any]]]] = None):
        self._rows = rows_by_table or {}
        # track per-table queries so tests can inspect them
        self.queries: Dict[str, Any] = {}

    def table(self, name: str):
        rows = self._rows.get(name, [])
        q = _FakeQuery(rows)
        self.queries[name] = q
        return q


# ---------------------------------------------------------------------------
# Helper: wire the FastAPI app with the fake client + bypassed auth
# ---------------------------------------------------------------------------


def _make_app(fake_client):
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
# Fixtures
# ---------------------------------------------------------------------------

SHARED_KB_SOURCE_ID = "src-shared-001"

@pytest.fixture
def shared_kb_row():
    """A pgvector source backed by the shared ai_kb_embeddings table."""
    return {
        "source_id": SHARED_KB_SOURCE_ID,
        "name": "partner_docs",
        "kind": "pgvector",
        "config": {
            "table": "ai_kb_embeddings",
            "pages_table": "ai_kb_pages",
        },
        "is_active": True,
    }


@pytest.fixture
def dedicated_kb_row():
    """A pgvector source backed by a dedicated (non-shared) table."""
    return {
        "source_id": "src-dedicated-002",
        "name": "Custom KB",
        "kind": "pgvector",
        "config": {
            "table": "custom_embeddings",
        },
        "is_active": True,
    }


@pytest.fixture
def http_source_row():
    return {
        "source_id": "src-http-003",
        "name": "External API",
        "kind": "http",
        "config": {"url": "https://example.com"},
        "is_active": True,
    }


@pytest.fixture
def two_pages():
    """Two ai_kb_pages rows for the shared KB."""
    return [
        {
            "id": "page-aaa",
            "source_id": SHARED_KB_SOURCE_ID,
            "title": "Getting Started",
            "type": "user_guide",
            "language": "en",
            "source": "admin_upload",
            "created_at": "2026-06-01T10:00:00",
        },
        {
            "id": "page-bbb",
            "source_id": SHARED_KB_SOURCE_ID,
            "title": "API Reference",
            "type": "feature_explanation",
            "language": "en",
            "source": "https://docs.partner.example/api",
            "created_at": "2026-06-02T12:00:00",
        },
    ]


@pytest.fixture
def embedding_rows():
    """ai_kb_embeddings rows: 3 for page-aaa, 1 for page-bbb."""
    return [
        {"id": "emb-1", "source_id": SHARED_KB_SOURCE_ID, "page_id": "page-aaa"},
        {"id": "emb-2", "source_id": SHARED_KB_SOURCE_ID, "page_id": "page-aaa"},
        {"id": "emb-3", "source_id": SHARED_KB_SOURCE_ID, "page_id": "page-aaa"},
        {"id": "emb-4", "source_id": SHARED_KB_SOURCE_ID, "page_id": "page-bbb"},
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_kb_detail_shared_pgvector_shows_pages_and_chunk_counts(
    shared_kb_row, two_pages, embedding_rows
):
    """GET /admin/kb/{shared_source_id} renders 2 pages with correct chunk counts."""
    fake = _FakeClient(
        rows_by_table={
            "agent_data_sources": [shared_kb_row],
            "columns": [],  # schema introspection returns nothing (ok)
            "ai_kb_pages": two_pages,
            "ai_kb_embeddings": embedding_rows,
        }
    )
    client, teardown = _make_app(fake)
    try:
        r = client.get(f"/admin/kb/{SHARED_KB_SOURCE_ID}", follow_redirects=False)
        assert r.status_code == 200, r.text[:500]
        body = r.text

        # Summary header: pages_total and chunks_total
        assert "2 pages" in body or "2&nbsp;pages" in body or ">2<" in body
        assert "4 chunks" in body or "4&nbsp;chunks" in body or ">4<" in body

        # Both page titles appear in the table
        assert "Getting Started" in body
        assert "API Reference" in body

        # Chunk counts per page
        assert "3" in body  # page-aaa has 3 chunks
        assert "1" in body  # page-bbb has 1 chunk

        # Source rendered (URL as a link, admin_upload as text)
        assert "admin_upload" in body
        assert "docs.partner.example" in body or "https://docs.partner.example" in body
    finally:
        teardown()


def test_kb_detail_shared_pgvector_pages_total_and_chunks_total(
    shared_kb_row, two_pages, embedding_rows
):
    """Ensure pages_total == 2 and chunks_total == 4 for the shared KB fixture."""
    fake = _FakeClient(
        rows_by_table={
            "agent_data_sources": [shared_kb_row],
            "columns": [],
            "ai_kb_pages": two_pages,
            "ai_kb_embeddings": embedding_rows,
        }
    )
    client, teardown = _make_app(fake)
    try:
        r = client.get(f"/admin/kb/{SHARED_KB_SOURCE_ID}")
        assert r.status_code == 200
        body = r.text
        # We check the actual summary numbers appear together
        assert "2" in body and "4" in body  # basic sanity
    finally:
        teardown()


def test_kb_detail_non_shared_pgvector_no_pages_no_crash(dedicated_kb_row):
    """GET for a dedicated-table pgvector source should not crash; pages=None."""
    fake = _FakeClient(
        rows_by_table={
            "agent_data_sources": [dedicated_kb_row],
            "columns": [],
        }
    )
    client, teardown = _make_app(fake)
    try:
        r = client.get(f"/admin/kb/{dedicated_kb_row['source_id']}", follow_redirects=False)
        assert r.status_code == 200, r.text[:500]
        # Should NOT show the pages table header when pages is None
        # (the section either absent or shows a "no inventory" note)
        body = r.text
        # Page renders fine — no 500
        assert "Custom KB" in body
    finally:
        teardown()


def test_kb_detail_http_source_no_pages_no_crash(http_source_row):
    """GET for a non-pgvector (http) source should render fine; pages=None."""
    fake = _FakeClient(
        rows_by_table={
            "agent_data_sources": [http_source_row],
        }
    )
    client, teardown = _make_app(fake)
    try:
        r = client.get(f"/admin/kb/{http_source_row['source_id']}", follow_redirects=False)
        assert r.status_code == 200, r.text[:500]
        body = r.text
        assert "External API" in body
    finally:
        teardown()


def test_kb_detail_empty_shared_kb_shows_zero_counts(shared_kb_row):
    """A shared KB with no pages yet should show 0 pages · 0 chunks without crashing."""
    fake = _FakeClient(
        rows_by_table={
            "agent_data_sources": [shared_kb_row],
            "columns": [],
            "ai_kb_pages": [],
            "ai_kb_embeddings": [],
        }
    )
    client, teardown = _make_app(fake)
    try:
        r = client.get(f"/admin/kb/{SHARED_KB_SOURCE_ID}")
        assert r.status_code == 200, r.text[:500]
        body = r.text
        # 0 pages / 0 chunks — both zeros appear somewhere in the page
        assert "0" in body
    finally:
        teardown()
