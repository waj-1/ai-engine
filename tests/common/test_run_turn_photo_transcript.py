"""The photo transcript (2026-11, fix round 1): an agent
hands it to run_turn through turn_meta.user_message_metadata; run_turn stamps
it on the persisted USER row's metadata, never on the message text or the
stream; ChatSession.load_history hands it back for later turns."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from common.chat_session import ChatSession, TurnIdentity, run_turn

TRANSCRIPT = {"photo_transcript": {"kind": "price_list", "count": 1, "text": "Hair > Cut — 80"}}


class _Agent:
    def __init__(self, events):
        self._events = events

    async def run_stream(self, message, context):
        for ev in self._events:
            yield ev


def _profile():
    return SimpleNamespace(memory_config={}, model_main={"model_string": "m"}, profile_id="p1")


async def _drive(agent, sess, message="[1 photo attached]"):
    events = []
    with patch("common.chat_session.get_service_client", return_value=MagicMock()), \
         patch("common.chat_session.ChatSession.for_identity", return_value=sess), \
         patch("common.chat_session.record_turn_telemetry", AsyncMock()):
        async for ev in run_turn(agent=agent, message=message,
                                 identity=TurnIdentity(agent_kind="business", session_id="s1"),
                                 profile=_profile()):
            events.append(ev)
    return events


def _session():
    sess = MagicMock()
    sess.resolve_or_create.return_value = ("s1", False, "T")
    sess.load_history.return_value = []
    sess.append_user.return_value = "msg-1"
    return sess


async def test_transcript_stamped_on_user_row_not_text_or_stream():
    sess = _session()
    events = await _drive(_Agent([
        {"type": "answer", "content": "Cards are ready."},
        {"type": "turn_meta", "metadata": {"iteration_count": 1, "user_message_metadata": TRANSCRIPT}},
    ]), sess)
    sess.update_message_metadata.assert_called_once_with("msg-1", TRANSCRIPT)
    assert sess.append_user.call_args[0] == ("[1 photo attached]",)        # text unchanged
    assert "Hair > Cut" not in repr(sess.append_assistant.call_args)
    assert "Hair > Cut" not in repr(events)                                 # never streamed


async def test_no_stamp_without_metadata_or_row_id():
    sess = _session()
    await _drive(_Agent([{"type": "answer", "content": "hi"},
                         {"type": "turn_meta", "metadata": {"iteration_count": 1}}]), sess)
    sess.update_message_metadata.assert_not_called()
    sess = _session()
    sess.append_user.return_value = None
    await _drive(_Agent([{"type": "answer", "content": "hi"},
                         {"type": "turn_meta", "metadata": {"user_message_metadata": TRANSCRIPT}}]), sess)
    sess.update_message_metadata.assert_not_called()


async def test_stamp_failure_does_not_break_the_turn():
    sess = _session()
    sess.update_message_metadata.side_effect = RuntimeError("db down")
    events = await _drive(_Agent([{"type": "answer", "content": "ok"},
                                  {"type": "turn_meta", "metadata": {"user_message_metadata": TRANSCRIPT}}]), sess)
    assert events[-1]["type"] == "done"
    assert sess.append_assistant.called


class _Query:
    """Minimal supabase-py query chain recording calls."""

    def __init__(self, log, data):
        self.log, self.data = log, data

    def __getattr__(self, name):
        def _call(*args, **kwargs):
            self.log.append((name, args))
            return self
        return _call

    def execute(self):
        return SimpleNamespace(data=self.data)


def _session_with(data):
    log = []
    client = MagicMock()
    client.table.side_effect = lambda name: _Query(log, data)
    sess = ChatSession.__new__(ChatSession)
    sess._supabase = client
    sess._session_id = "s1"
    sess._window = {"max_messages": 20}
    return sess, log


def test_load_history_returns_the_transcript_only_where_stored():
    sess, log = _session_with([
        {"role": "assistant", "content": "Cards are ready.", "photo_transcript": None},
        {"role": "user", "content": "[1 photo attached]", "photo_transcript": TRANSCRIPT["photo_transcript"]},
    ])
    rows = sess.load_history()
    assert rows == [
        {"role": "user", "content": "[1 photo attached]", "photo_transcript": TRANSCRIPT["photo_transcript"]},
        {"role": "assistant", "content": "Cards are ready."},
    ]
    select = next(args[0] for name, args in log if name == "select")
    assert "photo_transcript:metadata->photo_transcript" in select


def test_append_user_returns_the_row_id():
    sess, _ = _session_with([{"id": "msg-9"}])
    assert sess.append_user("hi") == "msg-9"
    sess, _ = _session_with([])
    assert sess.append_user("hi") is None


def test_update_message_metadata_merges():
    sess, log = _session_with([{"metadata": {"a": 1}}])
    sess.update_message_metadata("msg-1", TRANSCRIPT)
    update = next(args[0] for name, args in log if name == "update")
    assert update == {"metadata": {"a": 1, **TRANSCRIPT}}
    assert ("eq", ("session_id", "s1")) in log
