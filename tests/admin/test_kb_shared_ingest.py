"""Tests for source_id stamping when ingesting into shared ai_kb_* tables.

A pgvector source whose config.table == 'ai_kb_embeddings' is a "shared KB".
For shared KBs:
  - The upload handler must include source_id in every page row inserted into
    ai_kb_pages.
  - The background worker must include source_id in every embedding row
    inserted into ai_kb_embeddings.
  - The non-shared path (dedicated tables like ai_documentation_embeddings)
    must be completely unchanged.

These tests exercise _is_shared_kb() directly and drive _kb_ingest_worker
directly (same pattern as test_kb_async.py:test_worker_inserts_embedding_rows_*).
The HTTP upload path is verified by calling the endpoint via TestClient and
capturing the page insert payload (start_tracked_job is mocked so the worker
itself doesn't run on the request thread).
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
# Fake supabase client — same shape as test_kb_async._FakeClient
# ---------------------------------------------------------------------------


class _FakeQuery:
    def __init__(
        self,
        rows: List[Dict[str, Any]],
        inserted_rows: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self._rows = rows
        self._inserted_rows = inserted_rows or []
        self.last_filter: Dict[str, Any] = {}
        self.last_inserts: List[Any] = []
        self.last_update: Optional[Dict[str, Any]] = None

    def select(self, _cols: str = "*") -> "_FakeQuery":
        return self

    def eq(self, col: str, val: Any) -> "_FakeQuery":
        self.last_filter[col] = val
        return self

    def in_(self, col: str, vals: Any) -> "_FakeQuery":
        self.last_filter[f"in_{col}"] = list(vals)
        return self

    def limit(self, _n: int) -> "_FakeQuery":
        return self

    def order(self, *_a: Any, **_kw: Any) -> "_FakeQuery":
        return self

    def update(self, payload: Dict[str, Any]) -> "_FakeQuery":
        self.last_update = payload
        return self

    def insert(self, payload: Any) -> "_InsertResult":
        self.last_inserts.append(payload)
        return _InsertResult(payload, self._inserted_rows)

    def execute(self) -> Any:
        result = MagicMock()
        result.data = list(self._rows)
        return result


class _InsertResult:
    def __init__(self, payload: Any, override_rows: List[Dict[str, Any]]) -> None:
        self._payload = payload
        self._override = override_rows

    def execute(self) -> Any:
        result = MagicMock()
        if self._override:
            result.data = list(self._override)
        elif isinstance(self._payload, list):
            result.data = [{**r} for r in self._payload]
        else:
            result.data = [{**self._payload}]
        return result


class _FakeClient:
    def __init__(
        self,
        rows_by_table: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        inserts_by_table: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> None:
        self._rows_by_table = rows_by_table or {}
        self._inserts_by_table = inserts_by_table or {}
        self.queries: Dict[str, _FakeQuery] = {}

    def table(self, name: str) -> _FakeQuery:
        if name not in self.queries:
            self.queries[name] = _FakeQuery(
                self._rows_by_table.get(name, []),
                self._inserts_by_table.get(name),
            )
        return self.queries[name]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SHARED_SOURCE_ID = "src-shared-kb-1"

SHARED_KB_COLUMNS = [
    {"column_name": "id", "data_type": "bigint"},
    {"column_name": "source_id", "data_type": "uuid"},
    {"column_name": "page_id", "data_type": "bigint"},
    {"column_name": "content", "data_type": "text"},
    {"column_name": "heading", "data_type": "text"},
    {"column_name": "slug", "data_type": "text"},
    {"column_name": "token_count", "data_type": "integer"},
    {"column_name": "embedding", "data_type": "USER-DEFINED"},
]


@pytest.fixture
def shared_source_row() -> Dict[str, Any]:
    return {
        "source_id": SHARED_SOURCE_ID,
        "name": "Shared KB",
        "kind": "pgvector",
        "config": {
            "table": "ai_kb_embeddings",
            "pages_table": "ai_kb_pages",
        },
        "is_active": True,
    }


@pytest.fixture
def dedicated_source_row() -> Dict[str, Any]:
    """Non-shared source — dedicated embeddings table, no source_id column.

    A dedicated source names its own pages table explicitly; only the shared
    ai_kb_embeddings table has an auto-derived fallback.
    """
    return {
        "source_id": "src-docs",
        "name": "Docs",
        "kind": "pgvector",
        "config": {
            "table": "dedicated_docs_embeddings",
            "pages_table": "dedicated_docs_pages",
        },
        "is_active": True,
    }


# ---------------------------------------------------------------------------
# Helper: app factory (mirrors test_kb_async._make_app)
# ---------------------------------------------------------------------------


def _make_app(fake_client: _FakeClient):
    """Spin up a TestClient with require_superuser bypassed and every
    supabase entrypoint pinned to fake_client. start_tracked_job is also
    mocked so no real daemon thread spins up — the captured kwargs let us
    assert what the handler *would* pass to the worker."""
    from fastapi.testclient import TestClient
    from admin import auth, kb_handlers, _jobs
    from main import app

    def _fake_super() -> Dict[str, str]:
        return {"user_token": "tok", "user_id": "u-1", "email": "a@waj.test"}

    captured: Dict[str, Any] = {}

    def _fake_start(fn, **kwargs):
        captured["fn"] = fn
        captured["kwargs"] = kwargs
        return "job-shared-1"

    app.dependency_overrides[auth.require_superuser] = _fake_super
    patches = [
        patch.object(kb_handlers, "get_service_client", return_value=fake_client),
        patch.object(kb_handlers, "record_change"),
        patch.object(_jobs, "admin_service_client", return_value=fake_client),
        patch("admin._jobs.start_tracked_job", side_effect=_fake_start),
    ]
    for p in patches:
        p.start()
    client = TestClient(app)

    def teardown() -> None:
        for p in patches:
            p.stop()
        app.dependency_overrides.pop(auth.require_superuser, None)

    return client, captured, teardown


# ---------------------------------------------------------------------------
# Unit tests: _is_shared_kb
# ---------------------------------------------------------------------------


def test_is_shared_kb_true_for_shared_table():
    from admin.kb_handlers import _is_shared_kb

    assert _is_shared_kb({"table": "ai_kb_embeddings"}) is True


def test_is_shared_kb_false_for_dedicated_table():
    from admin.kb_handlers import _is_shared_kb

    assert _is_shared_kb({"table": "ai_documentation_embeddings"}) is False


def test_is_shared_kb_false_for_empty_config():
    from admin.kb_handlers import _is_shared_kb

    assert _is_shared_kb({}) is False
    assert _is_shared_kb(None) is False


# ---------------------------------------------------------------------------
# Unit test: _DEFAULT_PAGES_TABLE_FOR has the shared mapping
# ---------------------------------------------------------------------------


def test_default_pages_table_for_shared_kb():
    from admin.kb_handlers import _DEFAULT_PAGES_TABLE_FOR

    assert _DEFAULT_PAGES_TABLE_FOR.get("ai_kb_embeddings") == "ai_kb_pages"


# ---------------------------------------------------------------------------
# HTTP upload: page insert payload includes source_id for shared KB
# ---------------------------------------------------------------------------


def test_upload_page_insert_includes_source_id_for_shared_kb(shared_source_row):
    """Handler should add source_id to the ai_kb_pages insert payload."""
    fake = _FakeClient(
        rows_by_table={
            "agent_data_sources": [shared_source_row],
            "columns": SHARED_KB_COLUMNS,
        },
        inserts_by_table={"ai_kb_pages": [{"id": 77}]},
    )
    client, captured, teardown = _make_app(fake)
    try:
        r = client.post(
            f"/admin/kb/{SHARED_SOURCE_ID}/upload",
            files={"files": ("guide.md", b"# Title\n\nhello shared kb body text", "text/markdown")},
            data={"title": "Guide", "page_type": "user_guide", "language": "en"},
            follow_redirects=False,
        )
        assert r.status_code == 302, r.text[:500]
        assert "job_id=job-shared-1" in r.headers["location"]

        # The page row inserted into ai_kb_pages must carry source_id.
        pages_q = fake.queries.get("ai_kb_pages")
        assert pages_q is not None and pages_q.last_inserts, "expected ai_kb_pages insert"
        page_payload = pages_q.last_inserts[0]
        # payload may be a dict (single row) or a list; normalize.
        if isinstance(page_payload, list):
            page_payload = page_payload[0]
        assert "source_id" in page_payload, (
            f"source_id missing from ai_kb_pages insert; got keys: {list(page_payload.keys())}"
        )
        assert page_payload["source_id"] == SHARED_SOURCE_ID
    finally:
        teardown()


def test_upload_page_insert_no_source_id_for_dedicated_kb(dedicated_source_row):
    """Non-shared path: dedicated_docs_pages insert must NOT include source_id."""
    dedicated_columns = [
        {"column_name": "id", "data_type": "bigint"},
        {"column_name": "page_id", "data_type": "bigint"},
        {"column_name": "content", "data_type": "text"},
        {"column_name": "heading", "data_type": "text"},
        {"column_name": "slug", "data_type": "text"},
        {"column_name": "token_count", "data_type": "integer"},
        {"column_name": "embedding", "data_type": "USER-DEFINED"},
    ]
    fake = _FakeClient(
        rows_by_table={
            "agent_data_sources": [dedicated_source_row],
            "columns": dedicated_columns,
        },
        inserts_by_table={"dedicated_docs_pages": [{"id": 55}]},
    )
    client, captured, teardown = _make_app(fake)
    try:
        r = client.post(
            f"/admin/kb/{dedicated_source_row['source_id']}/upload",
            files={"files": ("doc.md", b"# Title\n\nbody content for docs", "text/markdown")},
            data={"title": "Doc", "page_type": "user_guide", "language": "en"},
            follow_redirects=False,
        )
        assert r.status_code == 302, r.text[:500]

        pages_q = fake.queries.get("dedicated_docs_pages")
        assert pages_q is not None and pages_q.last_inserts, "expected dedicated_docs_pages insert"
        page_payload = pages_q.last_inserts[0]
        if isinstance(page_payload, list):
            page_payload = page_payload[0]
        assert "source_id" not in page_payload, (
            "source_id must NOT be in dedicated dedicated_docs_pages insert"
        )
    finally:
        teardown()


# ---------------------------------------------------------------------------
# Worker: embedding rows include source_id for shared KB
# ---------------------------------------------------------------------------


def test_worker_stamps_source_id_in_shared_embedding_rows():
    """_kb_ingest_worker for a shared KB must include source_id in every
    embedding row dict inserted into ai_kb_embeddings."""
    from admin import kb_handlers

    fake = _FakeClient()
    fake_vec = [0.5] * 1536

    payload_text = " ".join(f"word{i}" for i in range(300))
    with patch.object(kb_handlers, "get_service_client", return_value=fake), \
         patch(
             "runtime.embeddings.embed_texts",
             side_effect=lambda texts, **_kw: [fake_vec] * len(texts),
         ):
        kb_handlers._kb_ingest_worker(
            source_id=SHARED_SOURCE_ID,
            table_name="ai_kb_embeddings",
            page_ids_by_filename={"guide.md": 77},
            file_contents_by_filename={"guide.md": payload_text},
            page_titles_by_filename={"guide.md": "Guide"},
            page_type="user_guide",
            language="en",
            max_tokens=128,
            overlap_tokens=16,
            embed_model="gemini-embedding-001",
            _job_id=None,
        )

    emb_q = fake.queries.get("ai_kb_embeddings")
    assert emb_q is not None and emb_q.last_inserts, "expected ai_kb_embeddings inserts"
    for batch in emb_q.last_inserts:
        assert isinstance(batch, list) and batch
        for row in batch:
            assert "source_id" in row, (
                f"source_id missing from embedding row; got keys: {list(row.keys())}"
            )
            assert row["source_id"] == SHARED_SOURCE_ID, (
                f"wrong source_id: expected {SHARED_SOURCE_ID}, got {row['source_id']}"
            )
            # Confirm other required fields are still present.
            assert row["page_id"] == 77
            assert row["content"]
            assert row["embedding"].startswith("[") and row["embedding"].endswith("]")


def test_worker_no_source_id_for_dedicated_embedding_rows():
    """Non-shared path: ai_documentation_embeddings rows must NOT get source_id."""
    from admin import kb_handlers

    fake = _FakeClient()
    fake_vec = [0.25] * 1536

    payload_text = " ".join(f"word{i}" for i in range(300))
    with patch.object(kb_handlers, "get_service_client", return_value=fake), \
         patch(
             "runtime.embeddings.embed_texts",
             side_effect=lambda texts, **_kw: [fake_vec] * len(texts),
         ):
        kb_handlers._kb_ingest_worker(
            source_id="src-docs",
            table_name="ai_documentation_embeddings",
            page_ids_by_filename={"doc.md": 55},
            file_contents_by_filename={"doc.md": payload_text},
            page_titles_by_filename={"doc.md": "Doc"},
            page_type="user_guide",
            language="en",
            max_tokens=128,
            overlap_tokens=16,
            embed_model="gemini-embedding-001",
            _job_id=None,
        )

    emb_q = fake.queries.get("ai_documentation_embeddings")
    assert emb_q is not None and emb_q.last_inserts
    for batch in emb_q.last_inserts:
        for row in batch:
            assert "source_id" not in row, (
                "source_id must NOT appear in dedicated embeddings table rows"
            )
