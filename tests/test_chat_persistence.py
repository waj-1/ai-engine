"""
Tests for chat_persistence module.
Verifies correct table targets, payload shapes, and that exceptions are swallowed.
"""

import asyncio
import time

import pytest
from unittest.mock import MagicMock, patch, call

from core.chat_persistence import coerce_session_id


def _make_mock_client():
    """Return a mock Supabase client with chainable table API."""
    client = MagicMock()
    chain = MagicMock()
    chain.upsert.return_value = chain
    chain.update.return_value = chain
    chain.insert.return_value = chain
    chain.eq.return_value = chain
    chain.execute.return_value = MagicMock(data=[{}], error=None)
    # session_owner_conflict's ownership lookup (select → eq → limit →
    # execute) sees NO existing row by default, so writes proceed. Tests that
    # override chain.select re-route that lookup through `chain` themselves.
    lookup = MagicMock()
    lookup.eq.return_value = lookup
    lookup.limit.return_value = lookup
    lookup.execute.return_value = MagicMock(data=[], error=None)
    chain.select.return_value = lookup
    client.table.return_value = chain
    client.schema.return_value = client
    return client, chain


@pytest.mark.asyncio
async def test_ensure_session_writes_agent_kind_when_provided():
    """agent_kind populates the discriminator column the live-chat eval
    sampler filters on; omitted → not written (legacy/NULL)."""
    client, chain = _make_mock_client()
    with patch("core.chat_persistence.get_service_client", return_value=client):
        from core import chat_persistence
        await chat_persistence.ensure_session(
            session_id="s", business_id="b", title="t",
            session_type="sales", source="waj", agent_kind="sales",
        )
    payload = chain.upsert.call_args[0][0]
    assert payload["agent_kind"] == "sales"


@pytest.mark.asyncio
async def test_ensure_session_omits_agent_kind_when_absent():
    client, chain = _make_mock_client()
    with patch("core.chat_persistence.get_service_client", return_value=client):
        from core import chat_persistence
        await chat_persistence.ensure_session(session_id="s", business_id="b", title="t")
    assert "agent_kind" not in chain.upsert.call_args[0][0]


@pytest.mark.asyncio
async def test_ensure_session_creates_row_on_first_message():
    client, chain = _make_mock_client()
    with patch("core.chat_persistence.get_service_client", return_value=client):
        from core import chat_persistence
        await chat_persistence.ensure_session(
            user_id="user-123",
            business_id="biz-456",
            session_id="sess-789",
            title="Test Session",
            extra_metadata={"record_id": "rec-1", "case_id": "case-1"},
        )

    # ownership lookup + upsert, both on ai_chat_sessions
    assert {c.args[0] for c in client.table.call_args_list} == {"ai_chat_sessions"}
    upsert_call = chain.upsert.call_args
    payload = upsert_call[0][0]
    assert payload["id"] == coerce_session_id("sess-789")  # non-UUID -> deterministic uuid
    assert payload["user_id"] == "user-123"
    assert payload["business_id"] == "biz-456"
    assert payload["title"] == "Test Session"
    assert payload["metadata"]["source"] == "waj"
    assert payload["metadata"]["session_type"] == "chat"
    assert payload["metadata"]["record_id"] == "rec-1"
    assert payload["metadata"]["case_id"] == "case-1"
    assert upsert_call[1]["on_conflict"] == "id"


@pytest.mark.asyncio
async def test_ensure_session_extra_metadata_skips_none_and_never_overrides_core_keys():
    client, chain = _make_mock_client()
    with patch("core.chat_persistence.get_service_client", return_value=client):
        from core import chat_persistence
        await chat_persistence.ensure_session(
            user_id="u",
            business_id="b",
            session_id="s",
            title="T",
            source="app",
            extra_metadata={"record_id": None, "source": "forged", "session_type": "x"},
        )

    payload = chain.upsert.call_args[0][0]
    assert "record_id" not in payload["metadata"]
    assert payload["metadata"]["source"] == "app"
    assert payload["metadata"]["session_type"] == "chat"


@pytest.mark.asyncio
async def test_ensure_session_swallows_exception():
    with patch("core.chat_persistence.get_service_client", side_effect=Exception("DB down")):
        from core import chat_persistence
        # Must not raise
        await chat_persistence.ensure_session(
            user_id="u", business_id="b", session_id="s", title="T"
        )


@pytest.mark.asyncio
async def test_update_session_title_calls_correct_table():
    client, chain = _make_mock_client()
    with patch("core.chat_persistence.get_service_client", return_value=client):
        from core import chat_persistence
        await chat_persistence.update_session_title(session_id="sess-1", title="New Title")

    client.table.assert_called_once_with("ai_chat_sessions")
    chain.update.assert_called_once_with({"title": "New Title"})
    chain.eq.assert_called_once_with("id", coerce_session_id("sess-1"))
    chain.execute.assert_called_once()


@pytest.mark.asyncio
async def test_update_session_title_swallows_exception():
    with patch("core.chat_persistence.get_service_client", side_effect=RuntimeError("timeout")):
        from core import chat_persistence
        await chat_persistence.update_session_title(session_id="s", title="T")


@pytest.mark.asyncio
async def test_save_turn_inserts_two_rows():
    client, chain = _make_mock_client()
    with patch("core.chat_persistence.get_service_client", return_value=client):
        from core import chat_persistence
        await chat_persistence.save_turn(
            session_id="sess-abc",
            user_message="Hello",
            assistant_content="Hi there",
            metadata={"tools_used": ["get_customer_info"], "proposal_count": 0},
        )

    # save_turn now also bumps ai_chat_sessions.updated_at (see
    # test_save_turn_bumps_session_updated_at below), so the messages insert
    # is one of two table() calls rather than the only one.
    client.table.assert_any_call("ai_chat_messages")
    rows = chain.insert.call_args[0][0]
    assert len(rows) == 2

    user_row = rows[0]
    assert user_row["session_id"] == coerce_session_id("sess-abc")
    assert user_row["role"] == "user"
    assert user_row["content"] == "Hello"
    assert user_row["metadata"] == {}

    assistant_row = rows[1]
    assert assistant_row["session_id"] == coerce_session_id("sess-abc")
    assert assistant_row["role"] == "assistant"
    assert assistant_row["content"] == "Hi there"
    assert assistant_row["metadata"]["tools_used"] == ["get_customer_info"]
    assert assistant_row["metadata"]["proposal_count"] == 0


@pytest.mark.asyncio
async def test_save_turn_bumps_session_updated_at():
    """History sorted by updated_at needs it bumped on every turn — before
    this fix nothing ever touched it after ensure_session's initial upsert,
    so History sorted by creation time instead of last activity."""
    client, chain = _make_mock_client()
    with patch("core.chat_persistence.get_service_client", return_value=client):
        from core import chat_persistence
        await chat_persistence.save_turn(
            session_id="sess-abc",
            user_message="Hello",
            assistant_content="Hi there",
            metadata={},
        )

    client.table.assert_any_call("ai_chat_sessions")
    update_payload = chain.update.call_args[0][0]
    assert "updated_at" in update_payload
    chain.eq.assert_any_call("id", coerce_session_id("sess-abc"))


@pytest.mark.asyncio
async def test_save_turn_swallows_updated_at_bump_failure():
    """A failure bumping updated_at must not be reported as save_turn
    failing — the messages were already persisted successfully."""
    client, chain = _make_mock_client()
    chain.update.side_effect = RuntimeError("boom")
    with patch("core.chat_persistence.get_service_client", return_value=client):
        from core import chat_persistence
        # Must not raise, even though the updated_at bump errors.
        await chat_persistence.save_turn(
            session_id="sess-abc",
            user_message="Hello",
            assistant_content="Hi there",
            metadata={},
        )
    # The messages insert still happened before the bump failed.
    rows = chain.insert.call_args[0][0]
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_save_turn_swallows_exception():
    with patch("core.chat_persistence.get_service_client", side_effect=Exception("network error")):
        from core import chat_persistence
        # Must not raise
        await chat_persistence.save_turn(
            session_id="s",
            user_message="msg",
            assistant_content="resp",
            metadata={},
        )


@pytest.mark.asyncio
async def test_save_turn_metadata_shape():
    """Verify all enriched metadata fields are accepted without error."""
    client, chain = _make_mock_client()
    full_metadata = {
        "thoughts": ["analyzing..."],
        "actions": [{"tool": "get_customer_info", "args": {}}],
        "observations": [{"tool": "get_customer_info", "result": "data"}],
        "suggestions": ["What are the open orders?"],
        "proposals": [{"type": "follow_up", "display": "Call back on Tuesday", "confidence": 0.92}],
        "usage": {"input_tokens": 100, "output_tokens": 200, "total_tokens": 300, "model": "gemini-2.0"},
        "tools_used": ["get_customer_info"],
        "proposal_types": ["follow_up"],
        "proposal_count": 1,
        "has_error": False,
        "model": "gemini-2.0",
        "response_time_ms": 1234,
        "ticket_id": "Ticket/t1",
        "record_id": "rec-1",
        "session_type": "support",
        "confidence_scores": [{"type": "follow_up", "score": 0.92}],
    }
    with patch("core.chat_persistence.get_service_client", return_value=client):
        from core import chat_persistence
        await chat_persistence.save_turn(
            session_id="s",
            user_message="msg",
            assistant_content="resp",
            metadata=full_metadata,
        )

    rows = chain.insert.call_args[0][0]
    assert rows[1]["metadata"] == full_metadata


@pytest.mark.asyncio
async def test_ensure_session_anonymous_without_user_id():
    """Test creating an anonymous session (e.g., sales chat) without user_id."""
    client, chain = _make_mock_client()
    with patch("core.chat_persistence.get_service_client", return_value=client):
        from core import chat_persistence
        await chat_persistence.ensure_session(
            session_id="anon-sess-123",
            business_id=None,
            title="Sales Chat",
            user_id=None,
            session_type="sales",
            source="waj",
        )

    payload = chain.upsert.call_args[0][0]
    assert payload["id"] == coerce_session_id("anon-sess-123")
    assert "user_id" not in payload  # user_id should be omitted when None
    assert payload["business_id"] is None
    assert payload["title"] == "Sales Chat"
    assert payload["metadata"]["session_type"] == "sales"
    assert payload["metadata"]["source"] == "waj"


@pytest.mark.asyncio
async def test_ensure_session_authenticated_business():
    """Test creating an authenticated business session."""
    client, chain = _make_mock_client()
    with patch("core.chat_persistence.get_service_client", return_value=client):
        from core import chat_persistence
        await chat_persistence.ensure_session(
            session_id="biz-sess-456",
            user_id="user-789",
            business_id="biz-999",
            title="Business Chat",
            session_type="business",
            source="waj",
        )

    payload = chain.upsert.call_args[0][0]
    assert payload["id"] == coerce_session_id("biz-sess-456")
    assert payload["user_id"] == "user-789"  # user_id included when provided
    assert payload["business_id"] == "biz-999"
    assert payload["metadata"]["session_type"] == "business"
    assert payload["metadata"]["source"] == "waj"


@pytest.mark.asyncio
async def test_ensure_session_with_record_linkage():
    """A session about one record carries that record in its metadata."""
    client, chain = _make_mock_client()
    with patch("core.chat_persistence.get_service_client", return_value=client):
        from core import chat_persistence
        await chat_persistence.ensure_session(
            session_id="records-sess-111",
            user_id="agent-222",
            business_id="org-333",
            title="Case review",
            extra_metadata={"record_id": "rec-444", "case_id": "case-555"},
            session_type="review",
            source="records_app",
        )

    payload = chain.upsert.call_args[0][0]
    assert payload["metadata"]["session_type"] == "review"
    assert payload["metadata"]["record_id"] == "rec-444"
    assert payload["metadata"]["case_id"] == "case-555"
    assert payload["metadata"]["source"] == "records_app"


@pytest.mark.asyncio
async def test_ensure_session_does_not_block_event_loop():
    """The blocking Supabase .execute() must run off the loop so other
    coroutines keep progressing while a chat write is in flight."""
    client, chain = _make_mock_client()

    def _slow_execute(*_a, **_k):
        time.sleep(0.2)  # simulate a blocking Supabase round-trip
        return MagicMock(data=[{}], error=None)

    chain.execute.side_effect = _slow_execute

    ticks = 0

    async def _ticker():
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.01)
            ticks += 1

    with patch("core.chat_persistence.get_service_client", return_value=client):
        from core import chat_persistence
        task = asyncio.create_task(_ticker())
        await chat_persistence.ensure_session(
            session_id="s", business_id="b", title="t"
        )
        await task

    # A blocking write would freeze the loop for ~0.2s and the ticker
    # (0.2s of 10ms sleeps) could not have advanced. Off-thread execution
    # lets it keep ticking concurrently.
    assert ticks >= 10


def test_chat_persistence_always_uses_service_client(monkeypatch):
    """ensure_session must write via the AI service client even when a user
    token is present (chat tables live in the AI project now)."""
    import core.chat_persistence as cp

    used = {"service": 0, "user": 0}

    class _Q:
        def schema(self, *_): return self
        def table(self, *_): return self
        def upsert(self, *_a, **_k): return self
        def execute(self): return None

    monkeypatch.setattr(cp, "get_service_client", lambda: (used.__setitem__("service", used["service"] + 1) or _Q()))
    monkeypatch.setattr(cp, "get_supabase_client", lambda _t: (used.__setitem__("user", used["user"] + 1) or _Q()), raising=False)

    import asyncio
    asyncio.run(cp.ensure_session("sess-1", user_token="tok", business_id="biz-1", title="t"))
    assert used["service"] == 1 and used["user"] == 0


@pytest.mark.asyncio
async def test_load_history_for_agent_returns_oldest_first_shaped():
    """load_history_for_agent reads by session_id (no user ownership check —
    anonymous sales sessions have no user_id), fetches newest-first + limited,
    and returns oldest-first {role, content} dicts the agent consumes."""
    client, chain = _make_mock_client()
    chain.select.return_value = chain
    chain.order.return_value = chain
    chain.limit.return_value = chain
    # DB returns newest-first (matches .order(desc=True)); loader reverses it.
    chain.execute.return_value = MagicMock(data=[
        {"role": "assistant", "content": "reply 2"},
        {"role": "user", "content": "msg 2"},
        {"role": "assistant", "content": "reply 1"},
        {"role": "user", "content": "msg 1"},
    ], error=None)
    with patch("core.chat_persistence.get_service_client", return_value=client):
        from core import chat_persistence
        out = await chat_persistence.load_history_for_agent("sess-1", limit=20)
    assert [m["content"] for m in out] == ["msg 1", "reply 1", "msg 2", "reply 2"]
    assert client.table.call_args[0][0] == "ai_chat_messages"
    assert chain.limit.call_args[0][0] == 20


@pytest.mark.asyncio
async def test_load_history_for_agent_swallows_errors():
    """A history-read failure degrades to [] so it never breaks the reply."""
    client, chain = _make_mock_client()
    chain.select.return_value = chain
    chain.order.return_value = chain
    chain.limit.return_value = chain
    chain.execute.side_effect = RuntimeError("db down")
    with patch("core.chat_persistence.get_service_client", return_value=client):
        from core import chat_persistence
        out = await chat_persistence.load_history_for_agent("sess-1")
    assert out == []


@pytest.mark.asyncio
async def test_save_turn_stamps_ordered_created_at():
    """save_turn inserts user+assistant in one batch; without explicit
    timestamps both would share created_at (DEFAULT now()) and every reader
    (orders by created_at) could return them reversed. Assert the user row is
    stamped strictly before the assistant row."""
    client, chain = _make_mock_client()
    with patch("core.chat_persistence.get_service_client", return_value=client):
        from core import chat_persistence
        await chat_persistence.save_turn(
            session_id="s", user_message="hi", assistant_content="hello",
        )
    rows = chain.insert.call_args[0][0]
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert "created_at" in rows[0] and "created_at" in rows[1]
    assert rows[0]["created_at"] < rows[1]["created_at"]  # user before assistant
