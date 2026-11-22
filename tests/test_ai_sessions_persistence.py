"""Tests for the AI-session read/manage query functions in chat_persistence.

These back the engine endpoints that the Flutter app uses to read chat
history. The chat tables live in the standalone AI project, so every query
goes through the service client and is scoped explicitly by user_id /
business_id (there is no per-user RLS on the AI project).
"""

import pytest
from unittest.mock import MagicMock, patch

from core import chat_persistence

_CHAIN_METHODS = (
    "select", "eq", "is_", "in_", "or_", "order", "limit", "update", "upsert", "insert",
)


def _make_client(table_data):
    """Build a mock Supabase client with a distinct chain per table name.

    table_data maps table name -> list of rows returned by .execute().data.
    Returns (client, chains_by_table) so tests can assert on filter calls.
    """
    client = MagicMock()
    chains = {}

    def _table(name):
        if name not in chains:
            chain = MagicMock(name=f"chain[{name}]")
            for m in _CHAIN_METHODS:
                getattr(chain, m).return_value = chain
            chain.execute.return_value = MagicMock(data=table_data.get(name, []), error=None)
            chains[name] = chain
        return chains[name]

    client.schema.return_value = client
    client.table.side_effect = _table
    return client, chains


@pytest.mark.asyncio
async def test_list_sessions_scopes_by_user_business_and_active():
    client, chains = _make_client({
        "ai_chat_sessions": [
            {"id": "s1", "user_id": "u1", "business_id": "b1", "title": "T",
             "created_at": "2026-06-10T00:00:00Z", "updated_at": "2026-06-10T00:00:00Z"},
        ],
        "ai_chat_messages": [],
    })
    with patch("core.chat_persistence.get_service_client", return_value=client):
        rows = await chat_persistence.list_sessions("u1", "b1")

    assert [r["id"] for r in rows] == ["s1"]
    s = chains["ai_chat_sessions"]
    s.eq.assert_any_call("user_id", "u1")
    s.eq.assert_any_call("business_id", "b1")
    s.is_.assert_any_call("deleted_at", "null")
    s.order.assert_any_call("updated_at", desc=True)


@pytest.mark.asyncio
async def test_list_sessions_without_a_business_is_the_users_sessions_with_no_business():
    """business_id=None must add `business_id IS NULL`, never drop the filter:
    dropping it would list a user's sessions across every business."""
    client, chains = _make_client({
        "ai_chat_sessions": [{"id": "s1", "user_id": "u1", "business_id": None, "title": "T",
                              "created_at": "2026-06-10T00:00:00Z", "updated_at": "2026-06-10T00:00:00Z"}],
        "ai_chat_messages": [],
    })
    with patch("core.chat_persistence.get_service_client", return_value=client):
        rows = await chat_persistence.list_sessions("u1", None)

    assert [r["id"] for r in rows] == ["s1"]
    s = chains["ai_chat_sessions"]
    s.eq.assert_any_call("user_id", "u1")
    s.is_.assert_any_call("business_id", "null")
    s.is_.assert_any_call("deleted_at", "null")
    assert all(call.args[0] != "business_id" for call in s.eq.call_args_list)


@pytest.mark.asyncio
async def test_list_sessions_aggregates_last_message_and_download_links():
    client, _ = _make_client({
        "ai_chat_sessions": [
            {"id": "s1", "user_id": "u1", "business_id": "b1", "title": "T",
             "created_at": "2026-06-10T00:00:00Z", "updated_at": "2026-06-10T00:00:00Z"},
        ],
        # ordered ascending by created_at; last entry is the latest turn
        "ai_chat_messages": [
            {"session_id": "s1", "created_at": "2026-06-10T10:00:00Z",
             "metadata": {"download_links": {
                 "message": "Here are your files",
                 "files": [{"name": "a.csv", "url": "http://x/a.csv"}]}}},
            {"session_id": "s1", "created_at": "2026-06-10T11:00:00Z",
             "metadata": {"foo": "bar"}},
        ],
    })
    with patch("core.chat_persistence.get_service_client", return_value=client):
        rows = await chat_persistence.list_sessions("u1", "b1")

    s = rows[0]
    assert s["last_message_at"] == "2026-06-10T11:00:00Z"
    meta = s["last_message_metadata"]
    assert meta["foo"] == "bar"  # base = latest message metadata
    assert meta["download_links"]["message"] == "Here are your files"
    assert meta["download_links"]["files"] == [{"name": "a.csv", "url": "http://x/a.csv"}]
    assert meta["download_links_count"] == 1


@pytest.mark.asyncio
async def test_list_sessions_computes_message_count_from_fetched_messages():
    """`ai_chat_sessions.message_count` has had no writer since waj#1424
    (2026-06-17) moved message writes to the engine, so it reads 0 for every
    row — the app renders that value verbatim as "0 messages" in History.
    The count must come from the messages this function already fetches."""
    client, _ = _make_client({
        "ai_chat_sessions": [
            {"id": "s1", "user_id": "u1", "business_id": "b1", "title": "T",
             "created_at": "2026-06-10T00:00:00Z", "updated_at": "2026-06-10T00:00:00Z",
             "message_count": 0},
        ],
        "ai_chat_messages": [
            {"session_id": "s1", "created_at": "2026-06-10T10:00:00Z", "metadata": {}},
            {"session_id": "s1", "created_at": "2026-06-10T10:01:00Z", "metadata": {}},
            {"session_id": "s1", "created_at": "2026-06-10T10:02:00Z", "metadata": {}},
        ],
    })
    with patch("core.chat_persistence.get_service_client", return_value=client):
        rows = await chat_persistence.list_sessions("u1", "b1")
    assert rows[0]["message_count"] == 3


@pytest.mark.asyncio
async def test_list_sessions_reports_zero_for_a_session_with_no_messages():
    client, _ = _make_client({
        "ai_chat_sessions": [
            {"id": "s1", "user_id": "u1", "business_id": "b1", "title": "T",
             "created_at": "2026-06-10T00:00:00Z", "updated_at": "2026-06-10T00:00:00Z",
             "message_count": 0},
        ],
        "ai_chat_messages": [],
    })
    with patch("core.chat_persistence.get_service_client", return_value=client):
        rows = await chat_persistence.list_sessions("u1", "b1")
    assert rows[0]["message_count"] == 0


@pytest.mark.asyncio
async def test_list_sessions_sorts_by_last_message_activity_not_stale_updated_at():
    """`updated_at` was never bumped on new turns before this fix, so a DB-side
    `order("updated_at", desc=True)` alone sorts by creation time. The final
    Python-side sort must re-derive activity order from `last_message_at`
    (falling back to updated_at/created_at for a session with no messages) so
    stale rows saved before the updated_at fix still land in the right place."""
    client, _ = _make_client({
        # DB order (by stale updated_at) puts s1 first, s2 second — the wrong
        # order once actual last-activity is considered.
        "ai_chat_sessions": [
            {"id": "s1", "user_id": "u1", "business_id": "b1", "title": "Old",
             "created_at": "2026-06-01T00:00:00Z", "updated_at": "2026-06-01T00:00:00Z"},
            {"id": "s2", "user_id": "u1", "business_id": "b1", "title": "New",
             "created_at": "2026-06-02T00:00:00Z", "updated_at": "2026-06-02T00:00:00Z"},
        ],
        "ai_chat_messages": [
            {"session_id": "s1", "created_at": "2026-06-10T00:00:00Z", "metadata": {}},
            {"session_id": "s2", "created_at": "2026-06-20T00:00:00Z", "metadata": {}},
        ],
    })
    with patch("core.chat_persistence.get_service_client", return_value=client):
        rows = await chat_persistence.list_sessions("u1", "b1")
    assert [r["id"] for r in rows] == ["s2", "s1"]


@pytest.mark.asyncio
async def test_list_sessions_empty_skips_message_query():
    client, chains = _make_client({"ai_chat_sessions": []})
    with patch("core.chat_persistence.get_service_client", return_value=client):
        rows = await chat_persistence.list_sessions("u1", "b1")
    assert rows == []
    assert "ai_chat_messages" not in chains  # never queried


@pytest.mark.asyncio
async def test_get_session_returns_row_scoped_to_user():
    client, chains = _make_client({
        "ai_chat_sessions": [{"id": "s1", "user_id": "u1", "title": "T"}],
    })
    with patch("core.chat_persistence.get_service_client", return_value=client):
        row = await chat_persistence.get_session("s1", "u1")
    assert row["id"] == "s1"
    chains["ai_chat_sessions"].eq.assert_any_call("user_id", "u1")


@pytest.mark.asyncio
async def test_get_session_computes_message_count_from_fetched_messages():
    """Mirrors list_sessions: the single-session GET must not return the
    stale stored `message_count` column either."""
    client, _ = _make_client({
        "ai_chat_sessions": [
            {"id": "s1", "user_id": "u1", "title": "T", "message_count": 0},
        ],
        "ai_chat_messages": [
            {"session_id": "s1", "created_at": "2026-06-10T10:00:00Z", "metadata": {}},
            {"session_id": "s1", "created_at": "2026-06-10T10:01:00Z", "metadata": {}},
            {"session_id": "s1", "created_at": "2026-06-10T10:02:00Z", "metadata": {}},
        ],
    })
    with patch("core.chat_persistence.get_service_client", return_value=client):
        row = await chat_persistence.get_session("s1", "u1")
    assert row["message_count"] == 3
    assert row["last_message_at"] == "2026-06-10T10:02:00Z"


@pytest.mark.asyncio
async def test_get_session_returns_none_when_missing():
    client, _ = _make_client({"ai_chat_sessions": []})
    with patch("core.chat_persistence.get_service_client", return_value=client):
        row = await chat_persistence.get_session("s1", "u1")
    assert row is None


@pytest.mark.asyncio
async def test_list_session_messages_returns_none_when_not_owned():
    # session ownership check returns no row -> caller gets None (404)
    client, _ = _make_client({"ai_chat_sessions": [], "ai_chat_messages": [{"id": "m1"}]})
    with patch("core.chat_persistence.get_service_client", return_value=client):
        msgs = await chat_persistence.list_session_messages("s1", "u1")
    assert msgs is None


@pytest.mark.asyncio
async def test_list_session_messages_returns_ordered_messages_when_owned():
    client, chains = _make_client({
        "ai_chat_sessions": [{"id": "s1", "user_id": "u1"}],
        "ai_chat_messages": [
            {"id": "m1", "session_id": "s1", "role": "user", "content": "hi",
             "created_at": "2026-06-10T10:00:00Z"},
        ],
    })
    with patch("core.chat_persistence.get_service_client", return_value=client):
        msgs = await chat_persistence.list_session_messages("s1", "u1")
    assert [m["id"] for m in msgs] == ["m1"]
    chains["ai_chat_messages"].order.assert_any_call("created_at", desc=False)


@pytest.mark.asyncio
async def test_rename_session_updates_title_scoped_to_user():
    client, chains = _make_client({
        "ai_chat_sessions": [{"id": "s1", "user_id": "u1", "title": "New"}],
    })
    with patch("core.chat_persistence.get_service_client", return_value=client):
        row = await chat_persistence.rename_session("s1", "u1", "New")
    assert row["title"] == "New"
    s = chains["ai_chat_sessions"]
    assert s.update.call_args[0][0]["title"] == "New"
    s.eq.assert_any_call("user_id", "u1")


@pytest.mark.asyncio
async def test_rename_session_returns_none_when_not_owned():
    client, _ = _make_client({"ai_chat_sessions": []})
    with patch("core.chat_persistence.get_service_client", return_value=client):
        row = await chat_persistence.rename_session("s1", "u1", "New")
    assert row is None


@pytest.mark.asyncio
async def test_soft_delete_session_sets_deleted_at_and_reports_success():
    client, chains = _make_client({
        "ai_chat_sessions": [{"id": "s1"}],  # one row affected
    })
    with patch("core.chat_persistence.get_service_client", return_value=client):
        ok = await chat_persistence.soft_delete_session("s1", "u1")
    assert ok is True
    s = chains["ai_chat_sessions"]
    assert "deleted_at" in s.update.call_args[0][0]
    s.eq.assert_any_call("user_id", "u1")


@pytest.mark.asyncio
async def test_soft_delete_session_returns_false_when_not_owned():
    client, _ = _make_client({"ai_chat_sessions": []})
    with patch("core.chat_persistence.get_service_client", return_value=client):
        ok = await chat_persistence.soft_delete_session("s1", "u1")
    assert ok is False


@pytest.mark.asyncio
async def test_list_sessions_filters_by_source_when_provided():
    client, chains = _make_client({
        "ai_chat_sessions": [
            {"id": "s1", "user_id": "u1", "business_id": "b1", "title": "T",
             "created_at": "2026-06-10T00:00:00Z", "updated_at": "2026-06-10T00:00:00Z"},
        ],
        "ai_chat_messages": [],
    })
    with patch("core.chat_persistence.get_service_client", return_value=client):
        rows = await chat_persistence.list_sessions("u1", "b1", source="pulse")
    assert [r["id"] for r in rows] == ["s1"]
    chains["ai_chat_sessions"].eq.assert_any_call("metadata->>source", "pulse")


@pytest.mark.asyncio
async def test_list_sessions_omits_source_filter_when_none():
    client, chains = _make_client({"ai_chat_sessions": []})
    with patch("core.chat_persistence.get_service_client", return_value=client):
        await chat_persistence.list_sessions("u1", "b1")
    for call in chains["ai_chat_sessions"].eq.call_args_list:
        assert call.args[0] != "metadata->>source"


# ---------------------------------------------------------------------------
# list_client_sessions — backs a tenant site's own History drawer.
#
# Two of its behaviours are load-bearing and neither is guessable from the
# schema, so they are pinned rather than assumed:
#   * `ai_chat_sessions.message_count` is NOT maintained (it reads 0 for every
#     row on a live client demo box, including a session with 222 messages), so
#     the count must come from the messages actually fetched.
#   * titles are written asynchronously by the title agent, so a just-finished
#     conversation is very often still the "New Chat" placeholder.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_client_sessions_filters_by_client_tag():
    """The demo merchant's identity is shared with the eval runner; the
    metadata tag is the only thing that separates a human's history from a
    scenario run."""
    client, chains = _make_client({
        "ai_chat_sessions": [
            {"id": "s1", "title": "Sales", "updated_at": "2026-07-31T13:00:00Z"},
        ],
        "ai_chat_messages": [
            {"session_id": "s1", "role": "user", "content": "hi"},
        ],
    })
    with patch("core.chat_persistence.get_service_client", return_value=client):
        rows = await chat_persistence.list_client_sessions(
            "u1", "b1", client_tag="portal",
        )

    assert [r["id"] for r in rows] == ["s1"]
    s = chains["ai_chat_sessions"]
    s.eq.assert_any_call("user_id", "u1")
    s.eq.assert_any_call("business_id", "b1")
    s.eq.assert_any_call("metadata->>client", "portal")
    s.is_.assert_any_call("deleted_at", "null")


@pytest.mark.asyncio
async def test_list_client_sessions_counts_messages_not_the_stale_column():
    """message_count on the row says 0; the real count is 3."""
    client, _ = _make_client({
        "ai_chat_sessions": [
            {"id": "s1", "title": "T", "updated_at": "2026-07-31T13:00:00Z",
             "message_count": 0},
        ],
        "ai_chat_messages": [
            {"session_id": "s1", "role": "user", "content": "a"},
            {"session_id": "s1", "role": "assistant", "content": "b"},
            {"session_id": "s1", "role": "user", "content": "c"},
        ],
    })
    with patch("core.chat_persistence.get_service_client", return_value=client):
        rows = await chat_persistence.list_client_sessions(
            "u1", "b1", client_tag="portal",
        )
    assert rows[0]["message_count"] == 3


@pytest.mark.asyncio
async def test_list_client_sessions_titles_placeholder_from_first_user_message():
    """"New Chat" is a placeholder, not a title — fall back to what was asked."""
    client, _ = _make_client({
        "ai_chat_sessions": [
            {"id": "s1", "title": "New Chat", "updated_at": "2026-07-31T13:00:00Z"},
        ],
        "ai_chat_messages": [
            {"session_id": "s1", "role": "assistant", "content": "Hello!"},
            {"session_id": "s1", "role": "user", "content": "What were my sales?"},
        ],
    })
    with patch("core.chat_persistence.get_service_client", return_value=client):
        rows = await chat_persistence.list_client_sessions(
            "u1", "b1", client_tag="portal",
        )
    assert rows[0]["title"] == "What were my sales?"


@pytest.mark.asyncio
async def test_list_client_sessions_drops_empty_sessions():
    """A page-load creates a session row before anyone types; an empty shell
    must not show up in the drawer as a blank conversation."""
    client, _ = _make_client({
        "ai_chat_sessions": [
            {"id": "empty", "title": "New Chat", "updated_at": "2026-07-31T13:00:00Z"},
            {"id": "real", "title": "Real", "updated_at": "2026-07-31T12:00:00Z"},
        ],
        "ai_chat_messages": [
            {"session_id": "real", "role": "user", "content": "hi"},
        ],
    })
    with patch("core.chat_persistence.get_service_client", return_value=client):
        rows = await chat_persistence.list_client_sessions(
            "u1", "b1", client_tag="portal",
        )
    assert [r["id"] for r in rows] == ["real"]


@pytest.mark.asyncio
async def test_list_client_sessions_empty_skips_message_query():
    client, chains = _make_client({"ai_chat_sessions": []})
    with patch("core.chat_persistence.get_service_client", return_value=client):
        rows = await chat_persistence.list_client_sessions(
            "u1", "b1", client_tag="portal",
        )
    assert rows == []
    assert "ai_chat_messages" not in chains


@pytest.mark.asyncio
async def test_list_client_sessions_excludes_agent_kinds_null_safely():
    client, chains = _make_client({
        "ai_chat_sessions": [{"id": "s1", "title": "T", "updated_at": "2026-07-31T13:00:00Z"}],
        "ai_chat_messages": [{"session_id": "s1", "role": "user", "content": "hi"}],
    })
    with patch("core.chat_persistence.get_service_client", return_value=client):
        await chat_persistence.list_client_sessions(
            "u1", "b1", client_tag="portal", exclude_agent_kinds=("orchard_outbound",),
        )
    chains["ai_chat_sessions"].or_.assert_any_call(
        "agent_kind.is.null,agent_kind.neq.orchard_outbound")


@pytest.mark.asyncio
async def test_list_session_messages_hides_excluded_agent_kind():
    client, _ = _make_client({
        "ai_chat_sessions": [{"id": "s1", "agent_kind": "orchard_outbound"}],
        "ai_chat_messages": [{"session_id": "s1", "role": "user", "content": "x"}],
    })
    with patch("core.chat_persistence.get_service_client", return_value=client):
        assert await chat_persistence.list_session_messages(
            "s1", "u1", exclude_agent_kinds=("orchard_outbound",)) is None
        assert await chat_persistence.list_session_messages("s1", "u1")
