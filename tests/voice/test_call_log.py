"""A call's spoken transcript, saved as a conversation (voice/live/call_log.py).

Before this, the only rows a call left were the ones a delegated turn wrote
(`ask_agent` -> `run_turn`): the model's wording of the question and the
brain's answer. A call the realtime model answered by itself left nothing, so
it never appeared under Conversations.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from common.chat_session import TurnIdentity
from voice.live import call_log as call_log_mod
from voice.live.call_log import CallLog


def _identity(session_id="6f1c6f0e-3a51-4f0e-9c69-0d1f3b7a2c11"):
    return TurnIdentity(agent_kind="custom", session_id=session_id, user_id=None,
                        business_id=None, session_type="custom", source="api",
                        session_metadata={"channel": "voice", "agent_slug": "helpdesk"})


def _log(**kw):
    ticks = iter(range(1, 10_000))
    return CallLog(_identity(), call_id="call-1", clock=lambda: float(next(ticks)), **kw)


def _said(log):
    return [(r["role"], r["content"]) for r in log.rows()]


def test_both_sides_of_a_call_in_the_order_spoken():
    log = _log()
    for part in ("Hello", "!", " How can I help?"):
        log.agent_text(part)
    log.response_ended()
    log.caller_committed()
    log.caller_text("Can you hear me?", final=True)
    log.agent_text("Yes,")
    log.agent_text(" I can.")
    log.response_ended()
    log.caller_committed()
    log.caller_text("Thanks, bye.", final=True)
    log.agent_text("Goodbye!")
    assert _said(log) == [
        ("assistant", "Hello! How can I help?"),
        ("user", "Can you hear me?"),
        ("assistant", "Yes, I can."),
        ("user", "Thanks, bye."),
        ("assistant", "Goodbye!"),
    ]
    stamps = [r["created_at"] for r in log.rows()]
    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)
    assert all(r["metadata"] == {"voice": {"call_id": "call-1", "spoken": True}}
               for r in log.rows())


def test_caller_text_that_arrives_after_the_reply_started_is_filed_before_it():
    """Azure commits the utterance, starts the reply, and only then delivers
    the caller's transcript."""
    log = _log()
    log.caller_committed()
    log.agent_text("Yes, I can hear you.")
    log.caller_text("Can you hear me?", final=True)
    assert _said(log) == [("user", "Can you hear me?"), ("assistant", "Yes, I can hear you.")]


def test_a_turn_the_brain_answered_is_not_saved_twice():
    """`run_turn` already saved the question and the answer of a delegated
    turn; the spoken copy of that turn is left out."""
    log = _log()
    log.agent_text("Hello!")
    log.response_ended()
    log.caller_committed()
    log.caller_text("What are your hours?", final=True)
    log.delegated(ok=True)
    log.agent_text("We are open nine to five.")
    log.response_ended()
    log.caller_committed()
    log.caller_text("Thanks.", final=True)
    log.agent_text("You're welcome.")
    assert _said(log) == [("assistant", "Hello!"), ("user", "Thanks."),
                          ("assistant", "You're welcome.")]


def test_a_failed_delegated_turn_keeps_what_the_agent_said():
    """`run_turn` saves the question of a failed turn, never an answer."""
    log = _log()
    log.caller_committed()
    log.caller_text("What are your hours?", final=True)
    log.delegated(ok=False)
    log.agent_text("Sorry, I could not look that up.")
    assert _said(log) == [("assistant", "Sorry, I could not look that up.")]


def test_a_provider_that_never_marks_caller_text_final():
    """Gemini Live: fragments on both sides, no commit event, no final."""
    log = _log()
    log.caller_text("What are ", final=False)
    log.caller_text("your hours?", final=False)
    log.agent_text("Nine ")
    log.agent_text("to five.")
    log.response_ended()
    log.caller_text("Thanks.", final=False)
    log.agent_text("Welcome.")
    assert _said(log) == [("user", "What are your hours?"), ("assistant", "Nine to five."),
                          ("user", "Thanks."), ("assistant", "Welcome.")]


def test_an_answer_in_two_responses_is_one_message_and_noise_adds_nothing():
    log = _log()
    log.caller_committed()
    log.caller_text("", final=True)          # a commit that was only noise
    log.caller_committed()
    log.caller_text("Hours?", final=True)
    log.agent_text("One moment.")
    log.response_ended()
    log.agent_text("Nine to five.")
    assert _said(log) == [("user", "Hours?"), ("assistant", "One moment.\nNine to five.")]


def test_nothing_said_saves_nothing(monkeypatch):
    wrote = []
    monkeypatch.setattr(call_log_mod, "_write", lambda *a: wrote.append(a))
    assert _log().save() is None
    assert wrote == []


def test_the_write_runs_on_another_thread_and_never_raises(monkeypatch):
    seen = {"calls": 0}

    def _write(identity, rows):
        seen["thread"] = threading.get_ident()
        seen["rows"] = rows
        seen["calls"] += 1
        raise RuntimeError("the database is away")

    monkeypatch.setattr(call_log_mod, "_write", _write)
    log = _log()
    log.agent_text("Hello!")
    log.save().result(timeout=10)      # the failure stays in the worker
    assert log.save() is None           # a second call writes nothing new
    assert seen["thread"] != threading.get_ident() and seen["calls"] == 1
    assert [r["content"] for r in seen["rows"]] == ["Hello!"]


class _Table:
    def __init__(self, db, name):
        self.db, self.name, self.op, self.filters = db, name, None, {}

    def select(self, *_a):
        self.op = ("select",)
        return self

    def insert(self, payload):
        self.op = ("insert", payload)
        return self

    def update(self, payload):
        self.op = ("update", payload)
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def is_(self, *_a):
        return self

    def maybe_single(self):
        return self

    def execute(self):
        rows = self.db.setdefault(self.name, [])
        if self.op[0] == "select":
            hit = [r for r in rows if all(str(r.get(k)) == str(v) for k, v in self.filters.items())]
            return SimpleNamespace(data=hit[0] if hit else None)
        if self.op[0] == "insert":
            payload = self.op[1]
            rows.extend(payload if isinstance(payload, list) else [payload])
        else:
            self.db.setdefault("updates", []).append((self.name, self.op[1], dict(self.filters)))
        return SimpleNamespace(data=[])


class _Client:
    def __init__(self):
        self.db = {}

    def table(self, name):
        return _Table(self.db, name)


def test_a_call_with_no_delegated_turn_creates_its_conversation(monkeypatch):
    client = _Client()
    monkeypatch.setattr(call_log_mod, "get_service_client", lambda: client)
    log = _log()
    log.agent_text("Hello!")
    log.caller_committed()
    log.caller_text("Just testing.", final=True)
    log.save().result(timeout=10)

    (session,) = client.db["ai_chat_sessions"]
    assert session["agent_kind"] == "custom"
    assert session["metadata"]["channel"] == "voice" and session["metadata"]["source"] == "api"
    assert session["metadata"]["agent_slug"] == "helpdesk"
    assert [(m["role"], m["content"]) for m in client.db["ai_chat_messages"]] == [
        ("assistant", "Hello!"), ("user", "Just testing.")]
    assert {m["session_id"] for m in client.db["ai_chat_messages"]} == {session["id"]}
    # The conversation sorts by its last words, like a chat turn's.
    assert client.db["updates"][-1][0] == "ai_chat_sessions"


def test_a_conversation_that_belongs_to_someone_else_is_left_alone(monkeypatch):
    client = _Client()
    sid = "6f1c6f0e-3a51-4f0e-9c69-0d1f3b7a2c11"
    client.db["ai_chat_sessions"] = [{"id": sid, "agent_kind": "business", "user_id": "u-9"}]
    monkeypatch.setattr(call_log_mod, "get_service_client", lambda: client)
    log = _log()
    log.agent_text("Hello!")
    log.save().result(timeout=10)
    assert "ai_chat_messages" not in client.db


@pytest.mark.parametrize("meta, spoken", [({"voice": {"spoken": True}}, True),
                                          ({"voice": {}}, False), ({}, False), (None, False)])
def test_is_spoken(meta, spoken):
    assert call_log_mod.is_spoken(meta) is spoken


def test_the_agents_history_leaves_the_spoken_rows_out():
    """What the brain reads as history is the turns it answered; the call's
    spoken transcript is a record for people."""
    from unittest.mock import MagicMock

    from common.chat_session import ChatSession

    log = []

    class _Query:
        def __getattr__(self, name):
            def _call(*args, **kwargs):
                log.append((name, args))
                return self
            return _call

        def execute(self):
            # Newest first, as the query orders them.
            return SimpleNamespace(data=[
                {"role": "assistant", "content": "Goodbye!", "voice": {"call_id": "c", "spoken": True}},
                {"role": "assistant", "content": "Nine to five.", "voice": None},
                {"role": "user", "content": "What are your hours?", "voice": None},
                {"role": "assistant", "content": "Hello!", "voice": {"call_id": "c", "spoken": True}},
            ])

    client = MagicMock()
    client.table.side_effect = lambda name: _Query()
    sess = ChatSession.__new__(ChatSession)
    sess._supabase, sess._session_id, sess._window = client, "s1", {"max_messages": 20}
    assert sess.load_history() == [
        {"role": "user", "content": "What are your hours?"},
        {"role": "assistant", "content": "Nine to five."},
    ]
    assert "voice:metadata->voice" in next(args[0] for name, args in log if name == "select")
