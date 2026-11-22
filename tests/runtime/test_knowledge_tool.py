"""runtime/tools/knowledge.py — the generic agent's knowledge search."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from runtime.tools import knowledge


class _Rpc:
    def __init__(self, rows, seen):
        self.rows, self.seen = rows, seen

    def rpc(self, name, params):
        self.seen.update(name=name, params=params)
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=self.rows))


@pytest.fixture
def backend(monkeypatch):
    seen, state = {}, {"rows": []}
    monkeypatch.setattr("runtime.embeddings.embed_texts", lambda texts: [[0.1, 0.2]])
    monkeypatch.setattr("core.auth.get_service_client", lambda: _Rpc(state["rows"], seen))
    return seen, state


def test_retrieve_searches_every_attached_source_in_any_language(backend):
    seen, state = backend
    state["rows"] = [
        {"page_title": "Refunds", "heading": "Refunds", "content": " 14 days. "},
        {"page_title": "Billing", "heading": "Late fees", "content": "None."},
        {"page_title": "", "heading": "", "content": "Bare chunk."},
    ]
    out = knowledge.retrieve("كم مدة الاسترجاع؟", source_ids=["kb-1", "kb-2"], match_count=3)
    assert out == "### Refunds\n14 days.\n\n### Billing — Late fees\nNone.\n\nBare chunk."
    assert seen["name"] == "match_kb_embeddings"
    assert seen["params"]["p_source_ids"] == ["kb-1", "kb-2"]
    assert seen["params"]["match_count"] == 3
    # No language filter: an Arabic question must be able to find an English article.
    assert seen["params"]["filter_language"] is None


def test_retrieve_returns_none_without_sources_query_or_matches(backend):
    seen, _ = backend
    assert knowledge.retrieve("refunds?", source_ids=[]) is None
    assert knowledge.retrieve("  ", source_ids=["kb-1"]) is None
    assert seen == {}  # neither reached the embedder or the database
    assert knowledge.retrieve("refunds?", source_ids=["kb-1"]) is None


def test_tool_distinguishes_nothing_found_from_search_failed(backend, monkeypatch):
    _, state = backend
    tool = knowledge.make_knowledge_tool(["kb-1"])
    assert tool.name == "search_knowledge"
    assert json.loads(tool.invoke({"query": "refunds?"})) == {
        "ok": True, "found": False,
        "detail": "Nothing in the knowledge base covers this. Say so plainly; never invent "
                  "a policy, price or timeline."}
    state["rows"] = [{"page_title": "Refunds", "heading": "", "content": "14 days."}]
    found = json.loads(tool.invoke({"query": "refunds?"}))
    assert found["found"] is True and "14 days." in found["articles"]

    def _boom(texts):
        raise RuntimeError("embed down")

    monkeypatch.setattr("runtime.embeddings.embed_texts", _boom)
    failed = json.loads(tool.invoke({"query": "refunds?"}))
    assert failed["ok"] is False and failed["error"] == "knowledge_unavailable"


def test_tools_for_profile():
    assert knowledge.knowledge_tools_for(SimpleNamespace(data_source_ids=[])) == []
    assert knowledge.knowledge_tools_for(SimpleNamespace()) == []
    tools = knowledge.knowledge_tools_for(SimpleNamespace(data_source_ids=["kb-1"]))
    assert [t.name for t in tools] == ["search_knowledge"]
