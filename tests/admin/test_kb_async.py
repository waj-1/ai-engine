"""Tests for the background-queued KB ingestion pipeline.

The KB upload endpoint no longer chunks/embeds/inserts synchronously on
the request thread. Instead the handler:

  1. Reads + decodes the uploaded bytes
  2. Inserts the parent page row(s)
  3. Hands off a payload to ``admin._jobs.start_tracked_job`` which
     spawns a daemon thread running ``_kb_ingest_worker``
  4. Returns a 302 to ``/admin/kb/{source_id}?job_id=<id>``

These tests pin that contract without spinning up a real Supabase
instance — the supabase client + the embeddings call + the tracker
itself are all mocked. The worker is also exercised directly with a
mocked ``embed_texts`` so we can assert insert shape + cancellation
semantics without racing a real thread.

See ``test_kb_upload.py`` for the pre-async tests that have been marked
skip — kept around as documentation but deliberately not executed
because the request thread no longer does the work they assert on.
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
# Reused fakes — same shape as test_kb_upload._FakeClient, kept local so
# this file can evolve independently as the async pipeline grows.
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
        # Mutate any matching row so subsequent selects see the change.
        for row in self._rows:
            if all(row.get(k) == v for k, v in self.last_filter.items()):
                row.update(payload)
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


@pytest.fixture
def pgvector_row() -> Dict[str, Any]:
    return {
        "source_id": "src-pgv",
        "name": "Docs",
        "kind": "pgvector",
        # Dedicated (non-shared) KB exercising the non-ai_kb_embeddings path: a
        # dedicated source names its own pages table explicitly (only the shared
        # ai_kb_embeddings table gets a fallback in _DEFAULT_PAGES_TABLE_FOR).
        "config": {"table": "dedicated_docs_embeddings", "pages_table": "dedicated_docs_pages"},
        "is_active": True,
    }


@pytest.fixture
def columns_full() -> List[Dict[str, str]]:
    return [
        {"column_name": "id", "data_type": "bigint"},
        {"column_name": "page_id", "data_type": "bigint"},
        {"column_name": "content", "data_type": "text"},
        {"column_name": "heading", "data_type": "text"},
        {"column_name": "slug", "data_type": "text"},
        {"column_name": "token_count", "data_type": "integer"},
        {"column_name": "embedding", "data_type": "USER-DEFINED"},
    ]


def _make_app(fake_client: _FakeClient):
    """Spin up a TestClient with require_superuser bypassed and every
    supabase entrypoint the upload path touches pinned to ``fake_client``.

    Critically we also patch ``admin._jobs.start_tracked_job`` so the
    test doesn't race a real daemon thread — it returns a deterministic
    job id and captures the worker kwargs the handler would have used.
    """
    from fastapi.testclient import TestClient
    from admin import auth, kb_handlers, _jobs
    from main import app

    def _fake_super() -> Dict[str, str]:
        return {"user_token": "tok", "user_id": "u-1", "email": "a@waj.test"}

    captured: Dict[str, Any] = {}

    def _fake_start(fn, **kwargs):
        captured["fn"] = fn
        captured["kwargs"] = kwargs
        return "job-fake-1"

    app.dependency_overrides[auth.require_superuser] = _fake_super
    patches = [
        patch.object(kb_handlers, "get_service_client", return_value=fake_client),
        patch.object(kb_handlers, "record_change"),
        patch.object(_jobs, "admin_service_client", return_value=fake_client),
        # The handler imports start_tracked_job lazily inside the
        # function body, so patching the source module is what gets
        # picked up at call time.
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
# Endpoint: POST /admin/kb/{source_id}/upload now enqueues + 302s with job_id
# ---------------------------------------------------------------------------


def test_upload_enqueues_job_and_redirects_with_job_id(pgvector_row, columns_full):
    """The handler should insert the page row, call start_tracked_job
    with the right payload, and redirect carrying ?job_id=..."""
    fake = _FakeClient(
        rows_by_table={
            "agent_data_sources": [pgvector_row],
            "columns": columns_full,
        },
        inserts_by_table={"dedicated_docs_pages": [{"id": 99}]},
    )
    client, captured, teardown = _make_app(fake)
    try:
        r = client.post(
            f"/admin/kb/{pgvector_row['source_id']}/upload",
            files={"files": ("guide.md", b"# Title\n\nhello world body text", "text/markdown")},
            data={"title": "Guide", "page_type": "user_guide", "language": "en"},
            follow_redirects=False,
        )
        assert r.status_code == 302, r.text[:500]
        loc = r.headers["location"]
        assert loc.startswith(f"/admin/kb/{pgvector_row['source_id']}")
        assert "job_id=job-fake-1" in loc

        # The start_tracked_job kwargs should carry the ingest payload
        # the worker needs to run independently of the request.
        kwargs = captured["kwargs"]
        assert kwargs["kind"] == "kb_upload"
        assert kwargs["actor_user_id"] == "u-1"
        assert kwargs["related_entity_type"] == "agent_data_sources"
        assert kwargs["related_entity_id"] == pgvector_row["source_id"]
        assert kwargs["page_ids_by_filename"] == {"guide.md": 99}
        assert "guide.md" in kwargs["file_contents_by_filename"]
        assert kwargs["metadata"]["file_count"] == 1
        assert kwargs["metadata"]["total_bytes"] > 0

        # Page row was inserted but no embedding insert happened yet —
        # that's the worker's job.
        pages_q = fake.queries.get("dedicated_docs_pages")
        assert pages_q is not None and pages_q.last_inserts
        assert fake.queries.get("dedicated_docs_embeddings") is None
    finally:
        teardown()


def test_upload_rejects_non_pgvector_source():
    http_source = {
        "source_id": "src-http",
        "kind": "http",
        "config": {"url": "https://example.com"},
        "is_active": True,
    }
    fake = _FakeClient(rows_by_table={"agent_data_sources": [http_source]})
    client, _captured, teardown = _make_app(fake)
    try:
        r = client.post(
            f"/admin/kb/{http_source['source_id']}/upload",
            files={"files": ("doc.md", b"hello", "text/markdown")},
            data={"page_type": "user_guide", "language": "en"},
            follow_redirects=False,
        )
        assert r.status_code == 400
    finally:
        teardown()


def test_upload_rejects_when_required_columns_missing(pgvector_row):
    fake = _FakeClient(
        rows_by_table={
            "agent_data_sources": [pgvector_row],
            # Only 'id' present → missing content + embedding.
            "columns": [{"column_name": "id", "data_type": "bigint"}],
        }
    )
    client, _captured, teardown = _make_app(fake)
    try:
        r = client.post(
            f"/admin/kb/{pgvector_row['source_id']}/upload",
            files={"files": ("doc.md", b"# Title\n\nbody", "text/markdown")},
            data={"page_type": "user_guide", "language": "en"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "error=" in r.headers["location"]
    finally:
        teardown()


def test_upload_rejects_oversized_file(pgvector_row, columns_full):
    fake = _FakeClient(
        rows_by_table={
            "agent_data_sources": [pgvector_row],
            "columns": columns_full,
        },
    )
    client, _captured, teardown = _make_app(fake)
    try:
        big = b"x" * (5 * 1024 * 1024 + 1)
        r = client.post(
            f"/admin/kb/{pgvector_row['source_id']}/upload",
            files={"files": ("big.txt", big, "text/plain")},
            data={"page_type": "user_guide", "language": "en"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "error=" in r.headers["location"]
    finally:
        teardown()


# ---------------------------------------------------------------------------
# Worker: _kb_ingest_worker
# ---------------------------------------------------------------------------


def test_worker_inserts_embedding_rows_with_right_shape():
    """Worker called directly with a mocked client + embed_texts should
    chunk → embed → insert into the embeddings table."""
    from admin import kb_handlers

    fake = _FakeClient()
    fake_vec = [0.25] * 1536

    payload_text = " ".join(f"word{i}" for i in range(400))
    with patch.object(kb_handlers, "get_service_client", return_value=fake), \
         patch(
             "runtime.embeddings.embed_texts",
             side_effect=lambda texts, **_kw: [fake_vec] * len(texts),
         ):
        kb_handlers._kb_ingest_worker(
            source_id="src-pgv",
            table_name="ai_documentation_embeddings",
            page_ids_by_filename={"guide.md": 42},
            file_contents_by_filename={"guide.md": payload_text},
            page_titles_by_filename={"guide.md": "Guide"},
            page_type="user_guide",
            language="en",
            max_tokens=128,
            overlap_tokens=16,
            embed_model="gemini-embedding-001",
            _job_id=None,
        )

    emb_q = fake.queries.get("ai_documentation_embeddings")
    assert emb_q is not None and emb_q.last_inserts, "expected embeddings to be inserted"
    batch = emb_q.last_inserts[0]
    assert isinstance(batch, list) and batch
    first = batch[0]
    assert first["page_id"] == 42
    assert first["content"]
    # pgvector literal: bracketed, comma-separated, 1536 floats → 1535 commas.
    assert first["embedding"].startswith("[") and first["embedding"].endswith("]")
    assert first["embedding"].count(",") == 1535


def test_worker_cancellation_breaks_loop_between_files():
    """Setting is_cancelled True before the second file should stop the
    loop so only the first file's embeddings are inserted."""
    from admin import kb_handlers

    fake = _FakeClient()
    fake_vec = [0.1] * 8  # tiny vectors — count, not dims, is what matters here.

    files = {
        "a.md": "alpha " * 80,
        "b.md": "bravo " * 80,
        "c.md": "charlie " * 80,
    }
    page_ids = {"a.md": 1, "b.md": 2, "c.md": 3}
    titles = {k: k for k in files}

    # is_cancelled returns False on the first check (file=a.md), then True
    # for every subsequent call. The worker should ingest a.md and bail
    # before touching b.md or c.md.
    cancel_calls = {"n": 0}

    def _fake_is_cancelled(_jid: str) -> bool:
        cancel_calls["n"] += 1
        return cancel_calls["n"] > 1

    with patch.object(kb_handlers, "get_service_client", return_value=fake), \
         patch(
             "runtime.embeddings.embed_texts",
             side_effect=lambda texts, **_kw: [fake_vec] * len(texts),
         ), \
         patch("admin._jobs.is_cancelled", side_effect=_fake_is_cancelled), \
         patch("admin._jobs.merge_job_metadata"):
        kb_handlers._kb_ingest_worker(
            source_id="src-pgv",
            table_name="ai_documentation_embeddings",
            page_ids_by_filename=page_ids,
            file_contents_by_filename=files,
            page_titles_by_filename=titles,
            page_type="user_guide",
            language="en",
            max_tokens=32,
            overlap_tokens=4,
            embed_model="gemini-embedding-001",
            _job_id="job-cancel-1",
        )

    emb_q = fake.queries.get("ai_documentation_embeddings")
    assert emb_q is not None and emb_q.last_inserts
    inserted_page_ids = {row["page_id"] for batch in emb_q.last_inserts for row in batch}
    assert inserted_page_ids == {1}, (
        f"only a.md (page_id=1) should have been ingested before cancel; "
        f"got page_ids={inserted_page_ids}"
    )


def test_worker_continues_when_a_single_file_fails():
    """A bad embed call on file 1 should not poison file 0's commits."""
    from admin import kb_handlers

    fake = _FakeClient()
    fake_vec = [0.5] * 4

    files = {"good.md": "alpha " * 60, "bad.md": "bravo " * 60}
    page_ids = {"good.md": 10, "bad.md": 20}
    titles = {k: k for k in files}

    call_count = {"n": 0}

    def _flaky_embed(texts: List[str], **_kw):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated embed outage")
        return [fake_vec] * len(texts)

    with patch.object(kb_handlers, "get_service_client", return_value=fake), \
         patch("runtime.embeddings.embed_texts", side_effect=_flaky_embed), \
         patch("admin._jobs.merge_job_metadata"):
        kb_handlers._kb_ingest_worker(
            source_id="src-pgv",
            table_name="ai_documentation_embeddings",
            page_ids_by_filename=page_ids,
            file_contents_by_filename=files,
            page_titles_by_filename=titles,
            page_type="user_guide",
            language="en",
            max_tokens=32,
            overlap_tokens=4,
            embed_model="gemini-embedding-001",
            _job_id=None,
        )

    emb_q = fake.queries.get("ai_documentation_embeddings")
    assert emb_q is not None
    inserted_page_ids = {row["page_id"] for batch in emb_q.last_inserts for row in batch}
    assert 10 in inserted_page_ids
    assert 20 not in inserted_page_ids


# ---------------------------------------------------------------------------
# JSON polling endpoint: GET /admin/jobs/{job_id}.json
# ---------------------------------------------------------------------------


def test_jobs_json_endpoint_returns_row_shape():
    from fastapi.testclient import TestClient
    from admin import auth, _jobs
    from main import app

    def _fake_super() -> Dict[str, str]:
        return {"user_token": "tok", "user_id": "u-1", "email": "a@waj.test"}

    sample_row = {
        "job_id": "job-1",
        "kind": "kb_upload",
        "label": "kb:docs",
        "status": "running",
        "metadata": {"chunk_count": 12, "files_done": 1, "file_count": 3},
        "started_at": "2026-05-20T10:00:00+00:00",
        "created_at": "2026-05-20T10:00:00+00:00",
    }

    app.dependency_overrides[auth.require_superuser] = _fake_super
    # jobs_handlers does `from admin._jobs import get_job`, which binds a
    # separate name in that module's namespace — patching admin._jobs.get_job
    # wouldn't reach it. Patch the name the handler actually calls.
    from admin import jobs_handlers
    with patch.object(jobs_handlers, "get_job", return_value=sample_row):
        try:
            client = TestClient(app)
            r = client.get("/admin/jobs/job-1.json")
            assert r.status_code == 200
            body = r.json()
            assert body["job_id"] == "job-1"
            assert body["status"] == "running"
            assert body["metadata"]["chunk_count"] == 12
        finally:
            app.dependency_overrides.pop(auth.require_superuser, None)


def test_jobs_json_endpoint_404s_on_missing_row():
    from fastapi.testclient import TestClient
    from admin import auth, _jobs
    from main import app

    def _fake_super() -> Dict[str, str]:
        return {"user_token": "tok", "user_id": "u-1", "email": "a@waj.test"}

    app.dependency_overrides[auth.require_superuser] = _fake_super
    with patch.object(_jobs, "get_job", return_value=None):
        try:
            client = TestClient(app)
            r = client.get("/admin/jobs/does-not-exist.json")
            assert r.status_code == 404
        finally:
            app.dependency_overrides.pop(auth.require_superuser, None)
