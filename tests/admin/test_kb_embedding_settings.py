"""The Knowledge screens and the embedding model.

Three things an operator must be told, in words, before anything fails:

  * no embedding model is set up (an install with no cloud key and no local
    endpoint): what to set, on the list, on the detail page, and as the answer
    to an upload, which used to stage a page and then fail in the background;
  * which model is in force, when one is;
  * this knowledge base was embedded by a different model than the one in
    force: vectors from two models are not comparable and the search does not
    error, so the stamp written at ingest is the only signal there is.
"""
from __future__ import annotations

from typing import Any, Dict
from unittest.mock import patch
from urllib.parse import unquote

import pytest

from tests.admin.test_kb_async import _FakeClient

_EMBED_VARS = ("EMBED_PROVIDER", "EMBED_BASE_URL", "EMBED_MODEL", "EMBED_API_KEY_ENV",
               "EMBED_API_KEY", "OLLAMA_BASE_URL", "GOOGLE_API_KEY",
               "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT")


@pytest.fixture
def keyless(monkeypatch):
    """An install with no cloud key and no embedding endpoint."""
    for name in _EMBED_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def local_model(keyless):
    keyless.setenv("EMBED_BASE_URL", "http://127.0.0.1:11434/v1")
    keyless.setenv("EMBED_MODEL", "nomic-embed-text")
    return keyless


def _source(**config: Any) -> Dict[str, Any]:
    return {"source_id": "src-1", "name": "Handbook", "kind": "pgvector", "is_active": True,
            "agent_kind": None, "config": {"table": "ai_kb_embeddings", **config}}


def _portal(fake: _FakeClient):
    from fastapi.testclient import TestClient
    from admin import _jobs, auth, kb_handlers, router as admin_router
    from main import app

    captured: Dict[str, Any] = {}

    def _fake_start(fn, **kwargs):
        captured["kwargs"] = kwargs
        return "job-1"

    app.dependency_overrides[auth.require_superuser] = lambda: {
        "user_token": "tok", "user_id": "u-1", "email": "admin@example.test"}
    patches = [
        patch.object(kb_handlers, "admin_service_client", return_value=fake),
        patch.object(admin_router, "client_for", return_value=fake),
        patch.object(kb_handlers, "record_change"),
        patch.object(_jobs, "admin_service_client", return_value=fake),
        patch("admin._jobs.start_tracked_job", side_effect=_fake_start),
    ]
    for p in patches:
        p.start()

    def teardown() -> None:
        for p in patches:
            p.stop()
        app.dependency_overrides.pop(auth.require_superuser, None)

    return TestClient(app), captured, teardown


def _upload(client, source_id="src-1"):
    return client.post(
        f"/admin/kb/{source_id}/upload",
        files={"files": ("returns.md", b"# Returns\n\nItems can be returned within 45 days.", "text/markdown")},
        data={"page_type": "faq", "language": "en"},
        follow_redirects=False,
    )


# --------------------------------------------------------------------------
# Nothing set up
# --------------------------------------------------------------------------


def test_upload_on_a_keyless_install_says_what_to_set_and_stages_nothing(keyless):
    fake = _FakeClient(rows_by_table={"agent_data_sources": [_source()]})
    client, captured, teardown = _portal(fake)
    try:
        r = _upload(client)
    finally:
        teardown()

    assert r.status_code == 302
    message = unquote(r.headers["location"].split("error=", 1)[1])
    assert "No embedding model is set up" in message
    for name in ("GOOGLE_API_KEY", "EMBED_BASE_URL", "EMBED_MODEL"):
        assert name in message
    # No page row, no background job: nothing half-done to clean up.
    assert "ai_kb_pages" not in fake.queries
    assert captured == {}


def test_the_screens_say_what_to_set_instead_of_waiting_for_an_upload(keyless):
    fake = _FakeClient(rows_by_table={"agent_data_sources": [_source()]})
    client, _captured, teardown = _portal(fake)
    try:
        listing = client.get("/admin/kb")
        detail = client.get("/admin/kb/src-1")
    finally:
        teardown()

    for page in (listing, detail):
        assert page.status_code == 200, page.text[:400]
        assert "EMBED_BASE_URL" in page.text and "EMBED_MODEL" in page.text
        assert "GOOGLE_API_KEY" in page.text
    assert "Knowledge bases cannot be searched yet" in listing.text
    assert "Documents cannot be added or searched yet" in detail.text
    # The upload button is not offered as if it would work.
    assert 'disabled title="Set up an embedding model first"' in detail.text


# --------------------------------------------------------------------------
# A local model in force
# --------------------------------------------------------------------------


def test_the_screens_show_the_model_in_force(local_model):
    fake = _FakeClient(rows_by_table={"agent_data_sources": [_source()]})
    client, _captured, teardown = _portal(fake)
    try:
        listing = client.get("/admin/kb")
        detail = client.get("/admin/kb/src-1")
    finally:
        teardown()

    for page in (listing, detail):
        assert page.status_code == 200, page.text[:400]
        assert "nomic-embed-text" in page.text
        assert "OpenAI-compatible server" in page.text
        assert "http://127.0.0.1:11434/v1" in page.text
        assert "cannot be searched yet" not in page.text
    assert "disabled title=" not in detail.text


def test_upload_with_a_local_model_is_queued(local_model):
    fake = _FakeClient(
        rows_by_table={"agent_data_sources": [_source()]},
        inserts_by_table={"ai_kb_pages": [{"id": "page-1"}]},
    )
    client, captured, teardown = _portal(fake)
    try:
        r = _upload(client)
    finally:
        teardown()

    assert r.status_code == 302
    assert r.headers["location"].endswith("/admin/kb/src-1?job_id=job-1")
    assert captured["kwargs"]["page_ids_by_filename"] == {"returns.md": "page-1"}


def test_ingest_embeds_through_the_local_model_and_stamps_the_source(local_model):
    """The worker end to end against a fake embeddings server: 768-wide
    vectors land as 1536-wide literals, and the source records who wrote them."""
    import json

    import httpx

    from admin import kb_handlers
    from core import embedding_provider as provider

    def _server(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={"data": [
            {"index": i, "embedding": [0.5] * 768} for i, _t in enumerate(body["input"])]})

    local_model.setattr(provider, "_oc_transport", httpx.MockTransport(_server))
    local_model.setattr(provider, "_oc_clients", {})
    local_model.setattr(provider, "_oc_send_dimensions", {})
    source = _source()
    fake = _FakeClient(rows_by_table={"agent_data_sources": [source]})

    with patch.object(kb_handlers, "admin_service_client", return_value=fake):
        kb_handlers._kb_ingest_worker(
            source_id="src-1", table_name="ai_kb_embeddings",
            page_ids_by_filename={"returns.md": "page-1"},
            file_contents_by_filename={"returns.md": "Items can be returned within 45 days."},
            page_titles_by_filename={"returns.md": "Returns"},
            page_type="faq", language="en",
        )

    inserted = fake.queries["ai_kb_embeddings"].last_inserts[0]
    assert inserted[0]["source_id"] == "src-1"
    assert inserted[0]["embedding"].count(",") == 1535
    assert source["config"]["embedded_with"] == "openai_compatible:nomic-embed-text"


# --------------------------------------------------------------------------
# A corpus embedded by something else
# --------------------------------------------------------------------------


def _with_documents(source: Dict[str, Any]) -> _FakeClient:
    return _FakeClient(rows_by_table={
        "agent_data_sources": [source],
        "ai_kb_pages": [{"id": "page-1", "source_id": "src-1", "title": "Returns",
                         "created_at": "2026-11-20T10:00:00Z"}],
        "ai_kb_embeddings": [{"id": "e-1", "page_id": "page-1", "source_id": "src-1"}],
    })


def test_a_corpus_embedded_by_another_model_is_flagged(local_model):
    fake = _with_documents(_source(embedded_with="google:gemini-embedding-001"))
    client, _captured, teardown = _portal(fake)
    try:
        listing = client.get("/admin/kb")
        detail = client.get("/admin/kb/src-1")
    finally:
        teardown()

    assert "Needs re-embedding" in listing.text
    assert "This knowledge base needs re-embedding" in detail.text
    assert "google:gemini-embedding-001" in detail.text
    assert "openai_compatible:nomic-embed-text" in detail.text
    assert "python -m scripts.reembed --target kb --apply" in detail.text


def test_a_corpus_embedded_by_the_model_in_force_is_not_flagged(local_model):
    fake = _with_documents(_source(embedded_with="openai_compatible:nomic-embed-text"))
    client, _captured, teardown = _portal(fake)
    try:
        listing = client.get("/admin/kb")
        detail = client.get("/admin/kb/src-1")
    finally:
        teardown()

    assert "Needs re-embedding" not in listing.text
    assert "needs re-embedding" not in detail.text
    assert "Built with" in detail.text


def test_a_source_with_no_stamp_is_not_flagged(local_model):
    """Sources from before the stamp existed: unknown is not a warning."""
    fake = _with_documents(_source())
    client, _captured, teardown = _portal(fake)
    try:
        detail = client.get("/admin/kb/src-1")
    finally:
        teardown()

    assert "needs re-embedding" not in detail.text


def test_upload_into_a_corpus_from_another_model_is_refused(local_model):
    """Adding passages from a second model would mix two spaces in one search."""
    fake = _with_documents(_source(embedded_with="google:gemini-embedding-001"))
    client, captured, teardown = _portal(fake)
    try:
        r = _upload(client)
    finally:
        teardown()

    message = unquote(r.headers["location"].split("error=", 1)[1])
    assert "google:gemini-embedding-001" in message
    assert "openai_compatible:nomic-embed-text" in message
    assert "scripts.reembed" in message
    assert captured == {}


def test_an_empty_knowledge_base_can_change_model_freely(local_model):
    """A stale stamp with no passages left behind blocks nothing."""
    source = _source(embedded_with="google:gemini-embedding-001")
    fake = _FakeClient(
        rows_by_table={"agent_data_sources": [source]},
        inserts_by_table={"ai_kb_pages": [{"id": "page-1"}]},
    )
    client, captured, teardown = _portal(fake)
    try:
        r = _upload(client)
    finally:
        teardown()

    assert "job_id=job-1" in r.headers["location"]
    assert captured["kwargs"]["source_id"] == "src-1"


# --------------------------------------------------------------------------
# The same answers over MCP
# --------------------------------------------------------------------------


def test_mcp_upload_on_a_keyless_install_is_refused_with_what_to_set(keyless):
    from admin_mcp import tools_configure

    fake = _FakeClient(rows_by_table={"agent_data_sources": [_source()]})
    with patch.object(tools_configure, "client_for_env", return_value=fake):
        listed = tools_configure.kb_sources(action="list")
        out = tools_configure.kb_sources(
            action="upload_document", source_id="src-1",
            filename="returns.md", content="Items can be returned within 45 days.")

    assert listed["embedding"]["ready"] is False
    assert "EMBED_MODEL" in out["error"] and "EMBED_BASE_URL" in out["error"]
    assert "ai_kb_pages" not in fake.queries
