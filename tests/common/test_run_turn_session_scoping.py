"""Session-ownership access control for the run_turn orchestrator.

The AI project has no per-user RLS, so `ChatSession.resolve_or_create` (the
identity/`run_turn` path) is itself the access-control gate when *resuming* a
session: an authenticated caller may only resume a session that is theirs AND
belongs to this agent_kind. Without this, a caller who learns another user's
session id — or reuses one agent's session id against another agent's endpoint —
would have that transcript loaded into the model via `load_history`.

Anonymous callers (identity.user_id is None — the public sales and demo agents) keep the
session-id-as-secret model, and a session with no stored owner stays resumable
so an anon→authenticated upgrade on the same browser session id isn't locked
out.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.chat_session import ChatSession, TurnIdentity, run_turn

_SID = "11111111-1111-1111-1111-111111111111"
_USER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_USER_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    """Fluent stand-in for a supabase-py query builder."""

    def __init__(self, client, mode):
        self._client = client
        self._mode = mode  # "select" | "insert"

    # select chain — every step returns self
    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def is_(self, *a, **k):
        return self

    def maybe_single(self):
        return self

    def execute(self):
        if self._mode == "insert":
            return _Resp([{"id": _SID}])
        return _Resp(self._client.existing)


class _Table:
    def __init__(self, client):
        self._client = client

    def select(self, *a, **k):
        return _Query(self._client, "select")

    def insert(self, row):
        self._client.inserted.append(row)
        return _Query(self._client, "insert")


class _FakeClient:
    """Returns `existing` for the ownership SELECT and records inserted rows."""

    def __init__(self, existing=None):
        self.existing = existing  # dict or None
        self.inserted: list[dict] = []

    def table(self, name):
        assert name == "ai_chat_sessions"
        return _Table(self)


def _session(existing, *, user_id, agent_kind="business"):
    identity = TurnIdentity(agent_kind=agent_kind, session_id=_SID, user_id=user_id)
    return ChatSession.for_identity(_FakeClient(existing), identity, window={})


# --------------------------------------------------------------------------
# resolve_or_create ownership + agent_kind gate
# --------------------------------------------------------------------------

def test_authenticated_caller_denied_foreign_session():
    """A session owned by user B must not be resumable by user A."""
    existing = {"id": _SID, "title": "B's chat", "user_id": _USER_B, "agent_kind": "business"}
    cs = _session(existing, user_id=_USER_A)
    with pytest.raises(LookupError):
        cs.resolve_or_create()


def test_authenticated_caller_resumes_own_session():
    existing = {"id": _SID, "title": "My chat", "user_id": _USER_A, "agent_kind": "business"}
    cs = _session(existing, user_id=_USER_A)
    sid, is_new, title = cs.resolve_or_create()
    assert (sid, is_new, title) == (_SID, False, "My chat")


def test_cross_agent_kind_session_denied():
    """A business session id sent to another agent must be rejected so a
    business transcript can't be loaded into that agent's turn."""
    existing = {"id": _SID, "title": "biz", "user_id": _USER_A, "agent_kind": "business"}
    cs = _session(existing, user_id=_USER_A, agent_kind="support_bot")
    with pytest.raises(LookupError):
        cs.resolve_or_create()


def test_anonymous_session_resumable_by_session_id():
    """Anonymous flows (user_id None, owner None) keep the session-id-secret
    model — the id alone resumes the row."""
    existing = {"id": _SID, "title": "anon", "user_id": None, "agent_kind": "sales"}
    cs = _session(existing, user_id=None, agent_kind="sales")
    sid, is_new, title = cs.resolve_or_create()
    assert (sid, is_new, title) == (_SID, False, "anon")


def test_null_owner_session_resumable_by_authenticated_caller():
    """An owner-less session (created anonymously) can be claimed when the same
    browser session id later arrives authenticated — no cross-user leak because
    a foreign *authenticated* session would have a non-null, mismatched owner."""
    existing = {"id": _SID, "title": "was anon", "user_id": None, "agent_kind": "business"}
    cs = _session(existing, user_id=_USER_A)
    sid, is_new, title = cs.resolve_or_create()
    assert (sid, is_new, title) == (_SID, False, "was anon")


def test_new_session_inserts_with_owner():
    """No existing row → insert a fresh session stamped with the caller's id."""
    cs = _session(None, user_id=_USER_A)
    client = cs._supabase  # the _FakeClient
    sid, is_new, title = cs.resolve_or_create()
    assert is_new is True
    assert len(client.inserted) == 1
    assert client.inserted[0]["user_id"] == _USER_A
    assert client.inserted[0]["agent_kind"] == "business"


def test_legacy_null_agent_kind_row_not_blocked():
    """Rows predating agent_kind stamping (agent_kind None) must not be blocked
    by the kind check — only a present, mismatched kind denies."""
    existing = {"id": _SID, "title": "legacy", "user_id": _USER_A, "agent_kind": None}
    cs = _session(existing, user_id=_USER_A, agent_kind="business")
    sid, is_new, title = cs.resolve_or_create()
    assert (sid, is_new, title) == (_SID, False, "legacy")


# --------------------------------------------------------------------------
# run_turn end-to-end: a denied session surfaces as "Session not found" and
# never runs the agent, persists, or writes telemetry.
# --------------------------------------------------------------------------

def _profile():
    return SimpleNamespace(
        memory_config={},
        model_main={"model_string": "gemini-3-flash", "model_id": "m1"},
        profile_id="p1",
    )


class _AgentThatMustNotRun:
    async def run_stream(self, message, context):  # pragma: no cover - must not run
        raise AssertionError("agent must not run for a denied session")
        yield {}  # unreachable; keeps this an async generator


@pytest.mark.asyncio
async def test_run_turn_denies_foreign_session_before_agent():
    existing = {"id": _SID, "title": "B's chat", "user_id": _USER_B, "agent_kind": "business"}
    tel = AsyncMock()
    with patch("common.chat_session.get_service_client", return_value=_FakeClient(existing)), \
         patch("common.chat_session.record_turn_telemetry", tel):
        identity = TurnIdentity(agent_kind="business", session_id=_SID, user_id=_USER_A)
        events = [ev async for ev in run_turn(
            agent=_AgentThatMustNotRun(), message="steal history",
            identity=identity, profile=_profile())]
    assert events == [{"type": "error", "message": "Session not found"}]
    assert tel.await_count == 0  # denied before any model call — nothing to record
