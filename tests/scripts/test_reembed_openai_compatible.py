"""`python -m scripts.reembed` with the OpenAI-compatible provider.

Moving a corpus to a local model is the documented path: run the script with
the new settings in the environment, then leave them set. Pinned here against
a fake embeddings server and a fake database:

  * every stored row is rewritten at its COLUMN's width (knowledge 1536,
    memory 768) from a 768-wide model;
  * the knowledge bases are marked with the model that now holds them, which
    is what clears the portal's warning;
  * a model that does not fit stops the run before anything is written;
  * an install with no embedding model does not start an --apply run.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import httpx
import pytest

from core import embedding_provider as provider
from scripts import reembed


class _Query:
    def __init__(self, db: "_Db", table: str):
        self.db, self.table = db, table
        self._filters: Dict[str, Any] = {}
        self._range = None
        self._update = None

    def select(self, _cols="*"):
        return self

    def order(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def update(self, payload):
        self._update = payload
        return self

    def execute(self):
        rows = [r for r in self.db.tables.get(self.table, [])
                if all(r.get(k) == v for k, v in self._filters.items())]
        if self._update is not None:
            for r in rows:
                r.update(self._update)
            self.db.writes.append((self.table, dict(self._filters), self._update))
        elif self._range:
            rows = rows[self._range[0]:self._range[1] + 1]

        class _R:
            data = rows
        return _R()


class _Db:
    def __init__(self, tables: Dict[str, List[Dict[str, Any]]]):
        self.tables = tables
        self.writes: List[Any] = []

    def table(self, name):
        return _Query(self, name)


@pytest.fixture
def local_model(monkeypatch):
    for name in ("EMBED_PROVIDER", "EMBED_API_KEY_ENV", "EMBED_API_KEY", "OLLAMA_BASE_URL",
                 "GOOGLE_API_KEY", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EMBED_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("EMBED_MODEL", "nomic-embed-text")
    monkeypatch.setattr(provider, "_oc_clients", {})
    monkeypatch.setattr(provider, "_oc_send_dimensions", {})
    state = {"width": 768, "inputs": []}

    def _server(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        state["inputs"].extend(body["input"])
        return httpx.Response(200, json={"data": [
            {"index": i, "embedding": [0.5] * state["width"]} for i, _t in enumerate(body["input"])]})

    monkeypatch.setattr(provider, "_oc_transport", httpx.MockTransport(_server))
    return state


def _db() -> _Db:
    return _Db({
        "ai_kb_embeddings": [
            {"id": "e-1", "content": "Returns are accepted for 45 days.", "embedding": "old"},
            {"id": "e-2", "content": "   ", "embedding": "old"},
            {"id": "e-3", "content": "Shipping takes three days.", "embedding": "old"},
        ],
        "ai_user_memory": [{"id": "m-1", "content": "Prefers email.", "embedding": "old"}],
        "agent_data_sources": [
            {"source_id": "s-1", "kind": "pgvector",
             "config": {"table": "ai_kb_embeddings", "embedded_with": "google:gemini-embedding-001"}},
            {"source_id": "s-2", "kind": "pgvector", "config": {"table": "some_other_table"}},
            {"source_id": "s-3", "kind": "http", "config": {}},
        ],
    })


def _width(literal: str) -> int:
    return literal.count(",") + 1


def test_kb_rows_are_rewritten_at_the_columns_width_and_sources_are_marked(local_model, monkeypatch):
    db = _db()
    monkeypatch.setattr(reembed, "get_service_client", lambda: db)

    assert reembed.reembed("kb", apply=True) == 2

    rows = {r["id"]: r for r in db.tables["ai_kb_embeddings"]}
    assert _width(rows["e-1"]["embedding"]) == 1536
    assert _width(rows["e-3"]["embedding"]) == 1536
    assert rows["e-2"]["embedding"] == "old"            # blank text is never sent
    assert local_model["inputs"] == ["Returns are accepted for 45 days.", "Shipping takes three days."]
    sources = {s["source_id"]: s["config"] for s in db.tables["agent_data_sources"]}
    assert sources["s-1"]["embedded_with"] == "openai_compatible:nomic-embed-text"
    assert "embedded_with" not in sources["s-2"]        # its own table was not touched
    assert "embedded_with" not in sources["s-3"]


def test_memory_rows_are_rewritten_at_768(local_model, monkeypatch):
    db = _db()
    monkeypatch.setattr(reembed, "get_service_client", lambda: db)

    assert reembed.reembed("memory", apply=True) == 1

    assert _width(db.tables["ai_user_memory"][0]["embedding"]) == 768
    # Memory has no knowledge bases to mark.
    assert db.tables["agent_data_sources"][0]["config"]["embedded_with"] == "google:gemini-embedding-001"


def test_a_dry_run_writes_nothing_and_calls_no_server(local_model, monkeypatch):
    db = _db()
    monkeypatch.setattr(reembed, "get_service_client", lambda: db)

    assert reembed.reembed("kb", apply=False) == 2

    assert db.writes == [] and local_model["inputs"] == []


def test_a_model_that_does_not_fit_stops_before_any_write(local_model, monkeypatch):
    local_model["width"] = 2048
    db = _db()
    monkeypatch.setattr(reembed, "get_service_client", lambda: db)

    with pytest.raises(provider.EmbeddingWidthError) as exc:
        reembed.reembed("kb", apply=True)

    assert "nomic-embed-text" in str(exc.value) and "2048" in str(exc.value) and "1536" in str(exc.value)
    assert db.writes == []
    assert db.tables["agent_data_sources"][0]["config"]["embedded_with"] == "google:gemini-embedding-001"


def test_apply_does_not_start_when_no_embedding_model_is_set_up(local_model, monkeypatch, capsys):
    monkeypatch.delenv("EMBED_MODEL")
    monkeypatch.delenv("EMBED_BASE_URL")
    monkeypatch.setattr(reembed, "get_service_client", lambda: pytest.fail("must not touch the database"))
    monkeypatch.setattr("sys.argv", ["reembed", "--target", "kb", "--apply"])

    assert reembed.main() == 2
    assert "EMBED_MODEL" in capsys.readouterr().out
