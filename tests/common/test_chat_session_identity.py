from unittest.mock import MagicMock

from core.agent_profile import _default_environment
from common.chat_session import ChatSession, TurnIdentity


def _client(existing=None, insert_id="11111111-1111-1111-1111-111111111111", select_data=None):
    """Build a fake chainable supabase client.

    `select_data` controls what the `.select(...).../.maybe_single().execute()`
    chain returns (the "does a row already exist" lookup used by the
    anonymous resolve-or-create path). `existing` controls what a bare
    `.execute()` returns for the authenticated insert/verify paths (kept for
    the pre-existing tests below). The two are distinct MagicMocks so a
    single test can make "select finds nothing" and "insert succeeds" behave
    differently.
    """
    client = MagicMock()
    chain = MagicMock()
    for m in ("select", "eq", "is_", "order", "limit", "insert", "upsert", "update"):
        getattr(chain, m).return_value = chain

    select_result = MagicMock(data=select_data)
    chain.maybe_single.return_value = MagicMock(execute=MagicMock(return_value=select_result))

    chain.execute.return_value = MagicMock(
        data=(existing if existing else [{"id": insert_id, "title": "New Chat"}])
    )
    client.table.return_value = chain
    return client, chain


WINDOW = {"max_messages": 20, "max_message_chars": 2000, "suggestion_context_messages": 6}


def test_anonymous_new_session_inserts_with_valid_uuid():
    # arbitrary (non-uuid) client session id → deterministic uuid, no
    # existing row found → insert path (not upsert)
    client, chain = _client(select_data=None)
    ident = TurnIdentity(agent_kind="sales", session_id="sales_abc",
                         session_type="sales", source="waj")
    cs = ChatSession.for_identity(client, ident, window=WINDOW)
    sid, is_new, _ = cs.resolve_or_create()
    assert is_new is True
    assert chain.insert.called
    # coerced to a uuid string
    import uuid
    uuid.UUID(sid)  # does not raise


def test_anonymous_returning_session_is_not_new_and_keeps_title():
    sid = "22222222-2222-2222-2222-222222222222"
    client, chain = _client(select_data={"id": sid, "title": "Kept Title"})
    ident = TurnIdentity(agent_kind="sales", session_id=sid,
                         session_type="sales", source="waj")
    cs = ChatSession.for_identity(client, ident, window=WINDOW)
    result = cs.resolve_or_create()
    assert result == (sid, False, "Kept Title")
    assert not chain.insert.called


def test_anonymous_without_session_id_generates_uuid():
    client, chain = _client(select_data=None)
    ident = TurnIdentity(agent_kind="sales", session_id=None,
                         session_type="sales", source="waj")
    cs = ChatSession.for_identity(client, ident, window=WINDOW)
    sid, is_new, _ = cs.resolve_or_create()
    import uuid
    uuid.UUID(sid)  # does not raise
    assert is_new is True


def test_authenticated_new_session_with_client_id_creates_and_sets_user_id():
    # Authenticated agents (business and the client demos) send a client-generated
    # session_id on turn 1. The row won't exist yet → must CREATE it (a
    # verify-only path would 404). The insert must carry user_id/business_id.
    sid = "33333333-3333-3333-3333-333333333333"
    client, chain = _client(select_data=None)
    ident = TurnIdentity(agent_kind="business", session_id=sid, user_id="u1",
                         business_id="b1", session_type="business")
    cs = ChatSession.for_identity(client, ident, window=WINDOW)
    _, is_new, _ = cs.resolve_or_create()
    assert is_new is True
    assert chain.insert.called
    row = chain.insert.call_args[0][0]
    assert row["user_id"] == "u1"
    assert row["business_id"] == "b1"
    assert row["id"] == sid


def test_authenticated_returning_session_is_not_new():
    sid = "44444444-4444-4444-4444-444444444444"
    client, chain = _client(select_data={"id": sid, "title": "Prior"})
    ident = TurnIdentity(agent_kind="business", session_id=sid, user_id="u1",
                         business_id="b1", session_type="business")
    cs = ChatSession.for_identity(client, ident, window=WINDOW)
    result = cs.resolve_or_create()
    assert result == (sid, False, "Prior")
    assert not chain.insert.called


def test_append_assistant_defaults_to_empty_metadata():
    """No metadata argument -> {} written, exactly like before the
    memory-provenance enhancement (backward compat for sales and the client demos, none of which pass a metadata kwarg)."""
    client, chain = _client()
    ident = TurnIdentity(agent_kind="sales", session_id="s1")
    cs = ChatSession.for_identity(client, ident, window=WINDOW)
    cs._session_id = "s1"
    cs.append_assistant("hello")
    row = chain.insert.call_args[0][0]
    assert row["metadata"] == {}


def test_append_assistant_writes_provided_memory_metadata():
    """business passes metadata={"memory": {...}} (via run_turn, which lifts
    it off the agent's turn_meta) — the row actually persisted must carry it,
    completing the round trip the /admin/memory Inspector reads."""
    client, chain = _client()
    ident = TurnIdentity(agent_kind="business", session_id="s1",
                         user_id="u1", business_id="b1")
    cs = ChatSession.for_identity(client, ident, window=WINDOW)
    cs._session_id = "s1"
    memory_meta = {"memory": {"retrieved_memory_ids": ["m1"], "summary_used": True}}
    cs.append_assistant("hello", metadata=memory_meta)
    row = chain.insert.call_args[0][0]
    assert row["metadata"] == memory_meta


def test_new_session_merges_identity_session_metadata():
    """An agent that sets identity.session_metadata has it merged into a NEW
    session's metadata, alongside source/session_type (audit linkage: the
    record a conversation is about)."""
    sid = "55555555-5555-5555-5555-555555555555"
    client, chain = _client(select_data=None)
    ident = TurnIdentity(agent_kind="records", session_id=sid, user_id="u1",
                         business_id="b1", session_type="records", source="waj",
                         session_metadata={"record_id": "R1", "case_id": "C1"})
    cs = ChatSession.for_identity(client, ident, window=WINDOW)
    _, is_new, _ = cs.resolve_or_create()
    assert is_new is True
    row = chain.insert.call_args[0][0]
    assert row["metadata"] == {
        "source": "waj", "session_type": "records",
        "record_id": "R1", "case_id": "C1",
        "environment": _default_environment(),
    }


def test_new_session_without_session_metadata_is_backward_compatible():
    """None (the default, and what the 5 already-shipped agents pass) must
    not change the pre-existing metadata shape — just {source, session_type}."""
    sid = "66666666-6666-6666-6666-666666666666"
    client, chain = _client(select_data=None)
    ident = TurnIdentity(agent_kind="business", session_id=sid, user_id="u1",
                         business_id="b1", session_type="business", source="waj")
    cs = ChatSession.for_identity(client, ident, window=WINDOW)
    cs.resolve_or_create()
    row = chain.insert.call_args[0][0]
    assert row["metadata"] == {"source": "waj", "session_type": "business",
                               "environment": _default_environment()}


def test_new_session_stamps_the_writer_environment(monkeypatch):
    """ai_chat_sessions has no environment column: the writer's env rides in
    metadata so conversation insights can be scoped per environment."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    client, chain = _client(select_data=None)
    ident = TurnIdentity(agent_kind="custom", session_id="77777777-7777-7777-7777-777777777777",
                         session_type="custom", source="waj")
    ChatSession.for_identity(client, ident, window=WINDOW).resolve_or_create()
    assert chain.insert.call_args[0][0]["metadata"]["environment"] == "prod"


def test_load_history_pushes_the_cap_into_sql_and_returns_oldest_first():
    # The window cap must reach the DB as ORDER BY created_at DESC LIMIT n —
    # selecting every message and trimming in Python re-reads the whole
    # conversation on every turn. Newest-first from the DB, oldest-first out.
    client, chain = _client(existing=[
        {"role": "assistant", "content": "newest", "created_at": "t3"},
        {"role": "user", "content": "middle", "created_at": "t2"},
        {"role": "user", "content": "oldest", "created_at": "t1"},
    ])
    ident = TurnIdentity(agent_kind="sales", session_id="s1")
    cs = ChatSession.for_identity(client, ident, window=WINDOW)
    cs._session_id = "s1"  # resolved
    out = cs.load_history()
    chain.order.assert_called_with("created_at", desc=True)
    chain.limit.assert_called_with(WINDOW["max_messages"])
    assert [r["content"] for r in out] == ["oldest", "middle", "newest"]


def test_load_history_caps_to_window_max_messages():
    rows = [{"role": "user", "content": f"m{i}", "created_at": f"t{i:03d}"} for i in range(30)]
    client, chain = _client(existing=rows)
    ident = TurnIdentity(agent_kind="sales", session_id="s1")
    cs = ChatSession.for_identity(client, ident, window={"max_messages": 5,
                                  "max_message_chars": 2000, "suggestion_context_messages": 6})
    cs._session_id = "s1"  # resolved
    hist = cs.load_history()
    assert len(hist) == 5
    assert [h["role"] for h in hist] == ["user"] * 5
