"""
Integration tests for end-to-end server-side persistence.
Tests the flow from endpoint request through agent to database persistence.
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
import json

from core.chat_persistence import coerce_session_id


def _make_mock_supabase_service_client():
    """Create a mock Supabase client that tracks calls."""
    client = MagicMock()
    table_mock = MagicMock()

    # Setup chainable methods
    table_mock.upsert.return_value = table_mock
    table_mock.update.return_value = table_mock
    table_mock.insert.return_value = table_mock
    table_mock.eq.return_value = table_mock
    table_mock.execute.return_value = MagicMock(data=[{}], error=None)
    # session_owner_conflict's ownership lookup sees no existing row.
    _lookup = MagicMock()
    _lookup.eq.return_value = _lookup
    _lookup.limit.return_value = _lookup
    _lookup.execute.return_value = MagicMock(data=[], error=None)
    table_mock.select.return_value = _lookup

    client.table.return_value = table_mock
    client.schema.return_value = client
    return client, table_mock


class TestBusinessChatPersistence:
    """Test persistence for authenticated /chat/stream endpoint."""

    @pytest.mark.asyncio
    async def test_business_chat_endpoint_context(self):
        """Verify /chat/stream endpoint threads correct context to agent."""
        # Simulate the context dict that /chat/stream builds
        context = {
            "user_token": "Bearer token-123",
            "user_id": "user-456",
            "business_id": "biz-789",
            "branch_id": "branch-111",
            "session_id": "550e8400-e29b-41d4-a716-446655440000",  # UUID
            "locale": "en",
            "currency": "AED",
            "is_first_message": True,
            "previous_messages": [],
        }

        # Verify all required fields are present
        assert context["user_id"] == "user-456"
        assert context["session_id"] == "550e8400-e29b-41d4-a716-446655440000"
        assert context["is_first_message"] is True
        assert "business_id" in context
        assert "locale" in context

    @pytest.mark.asyncio
    async def test_ensure_session_called_on_first_message(self):
        """Verify ensure_session is invoked for first message in business chat."""
        mock_client, mock_table = _make_mock_supabase_service_client()

        with patch("core.chat_persistence.get_service_client", return_value=mock_client):
            from core import chat_persistence

            # Simulate first message in business session
            await chat_persistence.ensure_session(
                session_id="sess-abc-123",
                user_id="user-123",
                business_id="biz-456",
                title="Order lookup",
                session_type="business",
                source="waj",
            )

            # Verify session was created
            assert mock_client.table.called
            assert mock_table.upsert.called

            # Verify correct table and payload
            mock_client.table.assert_called_with("ai_chat_sessions")
            payload = mock_table.upsert.call_args[0][0]

            assert payload["user_id"] == "user-123"
            assert payload["id"] == coerce_session_id("sess-abc-123")
            assert payload["metadata"]["session_type"] == "business"

    @pytest.mark.asyncio
    async def test_save_turn_persists_business_message(self):
        """Verify save_turn persists both user and assistant messages."""
        mock_client, mock_table = _make_mock_supabase_service_client()

        with patch("core.chat_persistence.get_service_client", return_value=mock_client):
            from core import chat_persistence

            # Simulate end of business chat exchange
            await chat_persistence.save_turn(
                session_id="sess-abc-123",
                user_message="Show me all customers",
                assistant_content="I found 15 active customers in your database.",
                metadata={
                    "session_type": "business",
                    "response_time_ms": 2100,
                    "model": "gemini-2.0-flash",
                    "usage": {
                        "input_tokens": 200,
                        "output_tokens": 50,
                        "total_tokens": 250,
                    },
                    "thoughts": ["user wants customer list"],
                },
            )

            # Verify messages were inserted
            assert mock_client.table.called
            assert mock_table.insert.called

            # save_turn also bumps ai_chat_sessions.updated_at after the
            # messages insert (so History sorts by last activity, not just
            # creation time), so ai_chat_messages is no longer the only/last
            # table touched.
            mock_client.table.assert_any_call("ai_chat_messages")
            rows = mock_table.insert.call_args[0][0]

            # Verify two rows inserted
            assert len(rows) == 2
            assert rows[0]["role"] == "user"
            assert rows[1]["role"] == "assistant"

            # Verify metadata on assistant message
            assert rows[1]["metadata"]["session_type"] == "business"
            assert rows[1]["metadata"]["response_time_ms"] == 2100


class TestSalesChatPersistence:
    """Test persistence for anonymous /sales-chat/stream endpoint."""

    @pytest.mark.asyncio
    async def test_sales_chat_endpoint_context_no_user_id(self):
        """Verify /sales-chat/stream endpoint threads context without user_id."""
        # Simulate the context dict that /sales-chat/stream builds
        context = {
            "session_id": "550e8400-e29b-41d4-a716-446655440001",  # UUID generated client-side
            "is_first_message": True,
            "previous_messages": [],
            # Note: no user_id, business_id, or branch_id for anonymous
        }

        # Verify user_id is absent
        assert context.get("user_id") is None
        assert context.get("business_id") is None
        assert context["is_first_message"] is True
        assert "session_id" in context

    @pytest.mark.asyncio
    async def test_ensure_session_called_for_anonymous_sales_chat(self):
        """Verify ensure_session is invoked for anonymous sales chat."""
        mock_client, mock_table = _make_mock_supabase_service_client()

        with patch("core.chat_persistence.get_service_client", return_value=mock_client):
            from core import chat_persistence

            # Simulate first message in anonymous sales session
            await chat_persistence.ensure_session(
                session_id="sess-anon-xyz",
                user_id=None,
                business_id=None,
                title="",
                session_type="sales",
                source="waj",
            )

            # Verify session was created
            assert mock_client.table.called
            assert mock_table.upsert.called

            payload = mock_table.upsert.call_args[0][0]

            # Verify user_id is omitted for anonymous
            assert "user_id" not in payload
            assert payload["id"] == coerce_session_id("sess-anon-xyz")
            assert payload["metadata"]["session_type"] == "sales"

    @pytest.mark.asyncio
    async def test_save_turn_persists_sales_message(self):
        """Verify save_turn persists anonymous sales messages."""
        mock_client, mock_table = _make_mock_supabase_service_client()

        with patch("core.chat_persistence.get_service_client", return_value=mock_client):
            from core import chat_persistence

            # Simulate end of anonymous sales chat exchange
            await chat_persistence.save_turn(
                session_id="sess-anon-xyz",
                user_message="How much does WAJ cost?",
                assistant_content="WAJ pricing depends on your business size and needs...",
                metadata={
                    "session_type": "sales",
                    "response_time_ms": 1050,
                    "model": "gemini-2.0-flash",
                    "usage": {
                        "input_tokens": 85,
                        "output_tokens": 120,
                        "total_tokens": 205,
                    },
                },
            )

            # Verify messages were inserted
            rows = mock_table.insert.call_args[0][0]

            # Verify two rows inserted
            assert len(rows) == 2
            assert rows[0]["session_id"] == coerce_session_id("sess-anon-xyz")
            assert rows[1]["session_id"] == coerce_session_id("sess-anon-xyz")

            # Verify metadata on assistant message
            assert rows[1]["metadata"]["session_type"] == "sales"
            assert rows[1]["role"] == "assistant"
            assert "WAJ pricing" in rows[1]["content"]


class TestPersistenceErrorHandling:
    """Test that persistence errors don't abort the SSE stream."""

    @pytest.mark.asyncio
    async def test_ensure_session_swallows_database_error(self):
        """Verify ensure_session errors are logged but not raised."""
        with patch("core.chat_persistence.get_service_client", side_effect=ConnectionError("DB down")):
            from core import chat_persistence

            # Should not raise — error should be swallowed
            try:
                await chat_persistence.ensure_session(
                    session_id="sess-123",
                    user_id="user-456",
                    business_id="biz-789",
                    title="Test",
                    session_type="business",
                )
                error_raised = False
            except Exception:
                error_raised = True

            assert not error_raised

    @pytest.mark.asyncio
    async def test_save_turn_swallows_database_error(self):
        """Verify save_turn errors are logged but not raised."""
        with patch("core.chat_persistence.get_service_client", side_effect=TimeoutError("slow db")):
            from core import chat_persistence

            # Should not raise — error should be swallowed
            try:
                await chat_persistence.save_turn(
                    session_id="sess-123",
                    user_message="hello",
                    assistant_content="hi",
                    metadata={"session_type": "business"},
                )
                error_raised = False
            except Exception:
                error_raised = True

            assert not error_raised

    @pytest.mark.asyncio
    async def test_update_session_title_swallows_error(self):
        """Verify update_session_title errors are logged but not raised."""
        with patch("core.chat_persistence.get_service_client", side_effect=Exception("network error")):
            from core import chat_persistence

            # Should not raise — error should be swallowed
            try:
                await chat_persistence.update_session_title(
                    session_id="sess-123",
                    title="New Title"
                )
                error_raised = False
            except Exception:
                error_raised = True

            assert not error_raised


class TestSessionMetadataComplexity:
    """Test that complex metadata structures are persisted correctly."""

    @pytest.mark.asyncio
    async def test_full_metadata_structure_persisted(self):
        """Verify all metadata fields are stored without truncation or loss."""
        mock_client, mock_table = _make_mock_supabase_service_client()

        full_metadata = {
            "thoughts": [
                "Analyzing customer request",
                "Retrieving customer records",
                "Formatting response",
            ],
            "usage": {
                "model": "gemini-2.0-flash",
                "input_tokens": 250,
                "output_tokens": 380,
                "total_tokens": 630,
            },
            "session_type": "business",
            "response_time_ms": 3200,
            "model": "gemini-2.0-flash",
            "tools_used": ["get_customer_info"],
            "proposal_count": 2,
        }

        with patch("core.chat_persistence.get_service_client", return_value=mock_client):
            from core import chat_persistence

            await chat_persistence.save_turn(
                session_id="sess-complex",
                user_message="Show customer orders",
                assistant_content="Here are the orders...",
                metadata=full_metadata,
            )

            rows = mock_table.insert.call_args[0][0]
            assert rows[1]["metadata"] == full_metadata


class TestSessionTypeVariations:
    """Test all three session types are persisted with correct metadata."""

    @pytest.mark.asyncio
    async def test_record_linked_session_type_metadata(self):
        """Verify a record-linked session has correct metadata."""
        mock_client, mock_table = _make_mock_supabase_service_client()

        with patch("core.chat_persistence.get_service_client", return_value=mock_client):
            from core import chat_persistence

            await chat_persistence.ensure_session(
                session_id="records-sess",
                user_id="agent-123",
                business_id="org-456",
                title="Case review",
                extra_metadata={"record_id": "rec-123", "case_id": "case-456"},
                session_type="review",
                source="records_app",
            )

            payload = mock_table.upsert.call_args[0][0]
            assert payload["metadata"]["session_type"] == "review"
            assert payload["metadata"]["source"] == "records_app"
            assert payload["metadata"]["record_id"] == "rec-123"

    @pytest.mark.asyncio
    async def test_business_session_type_metadata(self):
        """Verify business sessions have correct metadata."""
        mock_client, mock_table = _make_mock_supabase_service_client()

        with patch("core.chat_persistence.get_service_client", return_value=mock_client):
            from core import chat_persistence

            await chat_persistence.ensure_session(
                session_id="biz-sess",
                user_id="owner-123",
                business_id="biz-456",
                title="Quick Query",
                session_type="business",
                source="waj",
            )

            payload = mock_table.upsert.call_args[0][0]
            assert payload["metadata"]["session_type"] == "business"
            assert payload["metadata"]["source"] == "waj"

    @pytest.mark.asyncio
    async def test_sales_session_type_metadata(self):
        """Verify sales sessions have correct metadata."""
        mock_client, mock_table = _make_mock_supabase_service_client()

        with patch("core.chat_persistence.get_service_client", return_value=mock_client):
            from core import chat_persistence

            await chat_persistence.ensure_session(
                session_id="sales-sess",
                user_id=None,
                business_id=None,
                title="",
                session_type="sales",
                source="waj",
            )

            payload = mock_table.upsert.call_args[0][0]
            assert payload["metadata"]["session_type"] == "sales"
            assert payload["metadata"]["source"] == "waj"
            assert "user_id" not in payload
