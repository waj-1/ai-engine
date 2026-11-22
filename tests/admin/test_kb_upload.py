"""Tests for the KB upload pipeline.

Covers:
  * runtime.embeddings.chunk_text length/overlap behavior
  * runtime.embeddings.embed_texts surfaces a clear ConfigError without a key
  * POST /admin/kb/{source_id}/upload — non-pgvector rejected, missing
    columns rejected, happy path inserts pages + embeddings.

Uses the same scriptable supabase fake / FastAPI dependency-override pattern
as ``test_tool_routes.py`` so each test owns its DB state.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


# ---------------------------------------------------------------------------
# runtime.embeddings — chunk_text + embed_texts
# ---------------------------------------------------------------------------


def test_chunk_text_short_returns_single_chunk():
    from runtime.embeddings import chunk_text

    chunks = chunk_text("hello world", max_tokens=128, overlap_tokens=16)
    assert len(chunks) == 1
    assert chunks[0]["content"].strip() == "hello world"
    assert chunks[0]["token_count"] >= 1
    assert chunks[0]["slug"]  # derived from first line


def test_chunk_text_long_paginates_with_overlap():
    from runtime.embeddings import chunk_text

    # 2000 words: comfortably exceeds 128-token window so we expect >1 chunk.
    text = " ".join(f"word{i}" for i in range(2000))
    chunks = chunk_text(text, max_tokens=128, overlap_tokens=16)
    assert len(chunks) > 1
    # All but the last should be close to the cap.
    for c in chunks[:-1]:
        assert c["token_count"] <= 128
        assert c["token_count"] >= 64  # well above the overlap window
    # Heading + slug consistent across chunks.
    assert len({c["heading"] for c in chunks}) == 1
    assert len({c["slug"] for c in chunks}) == 1


def test_chunk_text_overlap_repeats_tail():
    """Consecutive chunks should share content (the overlap window)."""
    from runtime.embeddings import chunk_text

    text = " ".join(f"tok{i}" for i in range(800))
    chunks = chunk_text(text, max_tokens=100, overlap_tokens=20)
    assert len(chunks) >= 2
    # Heuristic overlap check: the last few words of chunk N should reappear
    # near the start of chunk N+1. Token boundaries may not align with word
    # boundaries when tiktoken is present, so we only assert that *some*
    # words from the tail of chunk 0 show up in chunk 1's prefix.
    tail = chunks[0]["content"].split()[-10:]
    head = " ".join(chunks[1]["content"].split()[:30])
    assert any(w in head for w in tail), "expected overlap between consecutive chunks"


def test_chunk_text_uses_heading_for_slug():
    from runtime.embeddings import chunk_text

    chunks = chunk_text("body text", max_tokens=64, overlap_tokens=8, heading="Quick Start Guide")
    assert chunks[0]["heading"] == "Quick Start Guide"
    assert chunks[0]["slug"] == "quick_start_guide"


def test_embed_texts_raises_when_api_key_missing(monkeypatch):
    from runtime.embeddings import ConfigError, embed_texts

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        embed_texts(["hello"])


# ---------------------------------------------------------------------------
# Supabase fake — same shape as test_tool_routes._FakeClient.
# ---------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self, rows: List[Dict[str, Any]], inserted_rows: Optional[List[Dict[str, Any]]] = None):
        self._rows = rows
        self._inserted_rows = inserted_rows or []
        self.last_filter: Dict[str, Any] = {}
        self.last_inserts: List[Any] = []
        self.last_update: Optional[Dict[str, Any]] = None

    def select(self, _cols):
        return self

    def eq(self, col, val):
        self.last_filter[col] = val
        return self

    def in_(self, col, vals):
        self.last_filter[f"in_{col}"] = list(vals)
        return self

    def limit(self, _n):
        return self

    def order(self, *_a, **_kw):
        return self

    def update(self, payload):
        self.last_update = payload
        return self

    def insert(self, payload):
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pgvector_source_row():
    return {
        "source_id": "src-pgv",
        "name": "Docs",
        "kind": "pgvector",
        "config": {
            # Shared KB (matches every live pgvector source): the pages table is
            # auto-derived via _DEFAULT_PAGES_TABLE_FOR[ai_kb_embeddings] =
            # ai_kb_pages, so pages_table is omitted here to exercise that fallback.
            "table": "ai_kb_embeddings",
        },
        "is_active": True,
    }


@pytest.fixture
def http_source_row():
    return {
        "source_id": "src-http",
        "name": "External API",
        "kind": "http",
        "config": {"url": "https://example.com"},
        "is_active": True,
    }


@pytest.fixture
def columns_full():
    return [
        {"column_name": "id", "data_type": "bigint"},
        {"column_name": "page_id", "data_type": "bigint"},
        {"column_name": "content", "data_type": "text"},
        {"column_name": "heading", "data_type": "text"},
        {"column_name": "slug", "data_type": "text"},
        {"column_name": "token_count", "data_type": "integer"},
        {"column_name": "embedding", "data_type": "USER-DEFINED"},
    ]


def _make_app(fake_client, *, embed_return=None):
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
    if embed_return is not None:
        patches.append(
            patch("runtime.embeddings.embed_texts", return_value=embed_return)
        )
    for p in patches:
        p.start()
    client = TestClient(app)

    def _teardown():
        for p in patches:
            p.stop()
        app.dependency_overrides.pop(auth.require_superuser, None)

    return client, _teardown


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------


def test_upload_rejects_non_pgvector_source(http_source_row):
    fake = _FakeClient(rows_by_table={"agent_data_sources": [http_source_row]})
    client, teardown = _make_app(fake)
    try:
        r = client.post(
            f"/admin/kb/{http_source_row['source_id']}/upload",
            files={"files": ("doc.md", b"hello world", "text/markdown")},
            data={"title": "t", "page_type": "user_guide", "language": "en"},
            follow_redirects=False,
        )
        assert r.status_code == 400, r.text[:500]
        assert "pgvector" in r.text
    finally:
        teardown()


def test_upload_rejects_when_required_columns_missing(pgvector_source_row):
    """Schema introspection returns columns lacking 'content' / 'embedding'."""
    fake = _FakeClient(
        rows_by_table={
            "agent_data_sources": [pgvector_source_row],
            # columns table = bare 'id' only -> missing content + embedding.
            "columns": [{"column_name": "id", "data_type": "bigint"}],
        }
    )
    client, teardown = _make_app(fake)
    try:
        r = client.post(
            f"/admin/kb/{pgvector_source_row['source_id']}/upload",
            files={"files": ("doc.md", b"# Title\n\nbody", "text/markdown")},
            data={"page_type": "user_guide", "language": "en"},
            follow_redirects=False,
        )
        assert r.status_code == 302, r.text[:500]
        loc = r.headers["location"]
        assert "error=" in loc
        assert "missing+required+columns" in loc or "missing%20required%20columns" in loc or "missing required columns" in loc.replace("%20", " ")
    finally:
        teardown()


# NOTE: the former `test_upload_happy_path_inserts_page_and_embeddings`
# asserted the old SYNCHRONOUS path (HTTP request inserts page +
# embeddings, redirects with ?uploaded=N). KB ingestion now runs on a
# background job, so that behavior moved to the worker. Coverage lives in
# tests/admin/test_kb_async.py:
#   - test_upload_enqueues_job_and_redirects_with_job_id (HTTP contract)
#   - test_worker_inserts_embedding_rows_with_right_shape (embedding shape)
# Kept this comment as a breadcrumb so the test isn't "lost", just moved.


def test_upload_rejects_oversized_file(pgvector_source_row, columns_full):
    fake = _FakeClient(
        rows_by_table={
            "agent_data_sources": [pgvector_source_row],
            "columns": columns_full,
        },
    )
    client, teardown = _make_app(fake)
    try:
        big = b"x" * (5 * 1024 * 1024 + 1)  # 5MB + 1 byte
        r = client.post(
            f"/admin/kb/{pgvector_source_row['source_id']}/upload",
            files={"files": ("big.txt", big, "text/plain")},
            data={"page_type": "user_guide", "language": "en"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "error=" in r.headers["location"]
        assert "5MB" in r.headers["location"] or "exceeds" in r.headers["location"]
    finally:
        teardown()
