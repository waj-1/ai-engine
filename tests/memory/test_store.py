from core.memory.embeddings import to_vector_literal, EMBED_DIM


def test_to_vector_literal_formats_pgvector_text():
    assert to_vector_literal([0.0, 1.5, -2.0]) == "[0.0,1.5,-2.0]"


def test_embed_dim_is_768():
    assert EMBED_DIM == 768


import asyncio
from typing import Any, Dict, List


class _FakeQuery:
    def __init__(self, sink: Dict[str, Any], result: List[Dict[str, Any]] | None = None):
        self._sink = sink
        self._result = result if result is not None else []

    def insert(self, row):
        self._sink["inserted"] = row
        return self

    def update(self, patch):
        self._sink["updated"] = patch
        return self

    def eq(self, col, val):
        self._sink.setdefault("eq", {})[col] = val
        return self

    def select(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        return type("R", (), {"data": self._result})()


class _FakeClient:
    def __init__(self, result=None):
        self.sink: Dict[str, Any] = {}
        self._result = result or []

    def table(self, name):
        self.sink["table"] = name
        return _FakeQuery(self.sink, self._result)


def test_insert_memory_writes_expected_row():
    from core.memory.store import insert_memory

    client = _FakeClient()
    asyncio.run(insert_memory(
        client, user_id="u1", business_id="b1", agent_kind="business",
        content="prefers morning slots", embedding=[0.1] * 768, importance=0.8,
        source="auto", origin_session_id="s1", origin_message_id=None,
    ))
    row = client.sink["inserted"]
    assert client.sink["table"] == "ai_user_memory"
    assert row["content"] == "prefers morning slots"
    assert row["user_id"] == "u1"
    assert row["source"] == "auto"
    assert isinstance(row["embedding"], str) and row["embedding"].startswith("[")


def test_archive_memory_soft_deletes():
    from core.memory.store import archive_memory

    client = _FakeClient()
    asyncio.run(archive_memory(client, memory_id="m1"))
    assert client.sink["updated"]["status"] == "archived"
    assert client.sink["eq"]["id"] == "m1"


def test_summary_write_is_conditional_when_expected_count_given():
    from core.memory.store import set_session_summary

    client = _FakeClient(result=[])  # no row matched: another save wrote first
    written = asyncio.run(set_session_summary(
        client, session_id="s1", summary="S", message_count=12, expected_count=4,
    ))
    assert client.sink["eq"] == {"id": "s1", "summary_message_count": 4}
    assert written is False


def test_summary_write_reports_success():
    from core.memory.store import set_session_summary

    client = _FakeClient(result=[{"id": "s1"}])
    assert asyncio.run(set_session_summary(
        client, session_id="s1", summary="S", message_count=12, expected_count=4,
    )) is True


class _PagedTable:
    """ai_chat_messages with `n` rows, honouring PostgREST's per-request row
    cap the way the real API does: .range() past it returns at most `cap`."""

    def __init__(self, n, cap=1000):
        self.rows = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
                     for i in range(n)]
        self.cap = cap
        self.calls = []
        self._range = (0, None)
        self._count = None

    def select(self, *_cols, count=None, head=None):
        self._count = count
        return self

    def eq(self, *_a):
        return self

    def order(self, *_a, **_k):
        return self

    def range(self, lo, hi):
        self._range = (lo, hi)
        return self

    def execute(self):
        if self._count == "exact":
            return type("R", (), {"data": [], "count": len(self.rows)})()
        lo, hi = self._range
        self.calls.append((lo, hi))
        return type("R", (), {"data": self.rows[lo:min(hi + 1, lo + self.cap)], "count": None})()


class _PagedClient:
    def __init__(self, table):
        self._table = table

    def table(self, _name):
        return self._table


def test_count_messages_is_exact_past_one_page():
    from core.memory.store import count_messages

    assert asyncio.run(count_messages(_PagedClient(_PagedTable(2500)), session_id="s1")) == 2500


def test_history_reads_every_page():
    from core.memory.store import list_session_history

    table = _PagedTable(2500)
    rows = asyncio.run(list_session_history(_PagedClient(table), session_id="s1"))
    assert [r["content"] for r in rows] == [f"m{i}" for i in range(2500)]
    assert len(table.calls) == 3


def test_history_reads_only_the_requested_slice():
    from core.memory.store import list_session_history

    table = _PagedTable(2500)
    rows = asyncio.run(list_session_history(_PagedClient(table), session_id="s1",
                                            start=1200, end=1230))
    assert [r["content"] for r in rows] == [f"m{i}" for i in range(1200, 1230)]
    assert table.calls == [(1200, 1229)]
    