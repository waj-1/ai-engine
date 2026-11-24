import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.chat_session import TurnIdentity, run_turn


class _FakeAgent:
    def __init__(self, events):
        self._events = events
    async def run_stream(self, message, context):
        self.seen_context = context
        for ev in self._events:
            yield ev


def _profile():
    return SimpleNamespace(
        memory_config={"window": {"max_messages": 20, "max_message_chars": 2000,
                                  "suggestion_context_messages": 6}},
        model_main={"model_string": "gemini-3-flash", "model_id": "m1"},
        profile_id="p1",
    )


@pytest.fixture
def fake_session():
    sess = MagicMock()
    sess.resolve_or_create.return_value = ("s1", False, "T")
    sess.load_history.return_value = [{"role": "user", "content": "prev"}]
    return sess


@pytest.mark.asyncio
async def test_persists_turn_and_telemetry_before_done(fake_session):
    agent = _FakeAgent([
        {"type": "answer", "content": "Hello"},
        {"type": "usage", "input_tokens": 10, "output_tokens": 5, "model": "gemini-3-flash"},
        {"type": "suggestions", "questions": ["a", "b"]},
        {"type": "turn_meta", "metadata": {"iteration_count": 1,
            "cache_read_tokens": 2, "cache_creation_tokens": 0}},
    ])
    saved_before_done = {"value": None}
    tel = AsyncMock()
    with patch("common.chat_session.get_service_client", return_value=MagicMock()), \
         patch("common.chat_session.ChatSession.for_identity", return_value=fake_session), \
         patch("common.chat_session.record_turn_telemetry", tel):
        events = []
        async for ev in run_turn(agent=agent, message="hi",
                                 identity=TurnIdentity(agent_kind="sales", session_id="s1"),
                                 profile=_profile()):
            if ev.get("type") == "done":
                saved_before_done["value"] = fake_session.append_assistant.called
            events.append(ev)
    assert fake_session.append_user.call_args[0] == ("hi",)
    assert fake_session.append_assistant.call_args[0] == ("Hello",)
    assert saved_before_done["value"] is True          # persisted before done
    assert tel.await_count == 1                          # telemetry written
    assert events[-1]["type"] == "done"
    # agent never sees a done to emit; orchestrator owns it
    assert sum(1 for e in events if e["type"] == "done") == 1
    assert not any(e.get("type") == "turn_meta" for e in events)  # not forwarded
    assert tel.call_args.kwargs["iteration_count"] == 1
    assert tel.call_args.kwargs["status"] == "ok"        # completed turn


@pytest.mark.asyncio
async def test_midstream_error_persists_no_assistant_row(fake_session):
    class _Boom:
        async def run_stream(self, message, context):
            yield {"type": "answer", "content": "part"}
            raise RuntimeError("model died")
    tel = AsyncMock()
    with patch("common.chat_session.get_service_client", return_value=MagicMock()), \
         patch("common.chat_session.ChatSession.for_identity", return_value=fake_session), \
         patch("common.chat_session.record_turn_telemetry", tel):
        events = [ev async for ev in run_turn(agent=_Boom(), message="hi",
                  identity=TurnIdentity(agent_kind="sales", session_id="s1"),
                  profile=_profile())]
    assert fake_session.append_user.called is True       # user message survives
    assert fake_session.append_assistant.called is False  # no assistant row
    assert any(e["type"] == "error" for e in events)
    # A model that raised mid-stream is still a failed turn worth recording.
    assert tel.await_count == 1
    assert tel.call_args.kwargs["status"] == "error"


@pytest.mark.asyncio
async def test_agent_yielded_error_is_terminal_records_error_telemetry(fake_session):
    # Some agents (e.g. sales) catch their own exceptions and YIELD an error
    # event instead of raising. run_turn must treat that as terminal: forward
    # the error, persist no (partial) assistant row, emit no `done`. The user
    # row (persisted before the stream) still survives. It DOES now record a
    # telemetry row marked status="error" so failed turns are visible to the
    # dashboards (previously a failed turn left no agent_traces row at all).
    class _Errs:
        async def run_stream(self, message, context):
            yield {"type": "answer", "content": "partial"}
            yield {"type": "error", "message": "boom"}
    tel = AsyncMock()
    with patch("common.chat_session.get_service_client", return_value=MagicMock()), \
         patch("common.chat_session.ChatSession.for_identity", return_value=fake_session), \
         patch("common.chat_session.record_turn_telemetry", tel):
        events = [ev async for ev in run_turn(agent=_Errs(), message="hi",
                  identity=TurnIdentity(agent_kind="sales", session_id="s1"),
                  profile=_profile())]
    assert fake_session.append_user.called is True         # user survives
    assert fake_session.append_assistant.called is False    # no partial assistant row
    assert tel.await_count == 1                              # failed turn IS recorded
    assert tel.call_args.kwargs["status"] == "error"        # ...marked as an error
    assert events[-1]["type"] == "error"                    # error is terminal
    assert not any(e["type"] == "done" for e in events)     # no done after error


@pytest.mark.asyncio
async def test_history_passed_to_agent_context(fake_session):
    agent = _FakeAgent([{"type": "answer", "content": "x"}])
    with patch("common.chat_session.get_service_client", return_value=MagicMock()), \
         patch("common.chat_session.ChatSession.for_identity", return_value=fake_session), \
         patch("common.chat_session.record_turn_telemetry", AsyncMock()):
        async for _ in run_turn(agent=agent, message="hi",
                identity=TurnIdentity(agent_kind="sales", session_id="s1"),
                profile=_profile()):
            pass
    assert agent.seen_context["previous_messages"] == [{"role": "user", "content": "prev"}]
    assert agent.seen_context["is_first_message"] is False


@pytest.mark.asyncio
async def test_swallows_agent_emitted_done(fake_session):
    # Legacy/misbehaving agent emits its own `done` mid-stream followed by
    # more events. The orchestrator must break on the agent's `done` (not
    # forward it, not forward anything after it) and still emit exactly one
    # `done` of its own after persistence.
    agent = _FakeAgent([
        {"type": "answer", "content": "Hello"},
        {"type": "done"},
        {"type": "answer", "content": "After"},
        {"type": "suggestions", "questions": ["should-not-be-seen"]},
    ])
    with patch("common.chat_session.get_service_client", return_value=MagicMock()), \
         patch("common.chat_session.ChatSession.for_identity", return_value=fake_session), \
         patch("common.chat_session.record_turn_telemetry", AsyncMock()):
        events = [ev async for ev in run_turn(agent=agent, message="hi",
                  identity=TurnIdentity(agent_kind="sales", session_id="s1"),
                  profile=_profile())]
    assert sum(1 for e in events if e["type"] == "done") == 1
    assert events[-1]["type"] == "done"
    # events emitted by the agent after its own `done` were never forwarded
    assert not any(e.get("content") == "After" for e in events)
    assert not any(e["type"] == "suggestions" for e in events)
    # only the pre-`done` accumulated text was persisted
    assert fake_session.append_assistant.call_args[0] == ("Hello",)


@pytest.mark.asyncio
async def test_new_session_emits_session_event_and_empty_history():
    fake_session = MagicMock()
    fake_session.resolve_or_create.return_value = ("s1", True, "New Chat")
    fake_session.session_id = "s1"
    agent = _FakeAgent([{"type": "answer", "content": "Hi there"}])
    with patch("common.chat_session.get_service_client", return_value=MagicMock()), \
         patch("common.chat_session.ChatSession.for_identity", return_value=fake_session), \
         patch("common.chat_session.record_turn_telemetry", AsyncMock()):
        events = [ev async for ev in run_turn(agent=agent, message="hi",
                  identity=TurnIdentity(agent_kind="sales", session_id=None),
                  profile=_profile())]
    session_events = [e for e in events if e["type"] == "session"]
    assert len(session_events) == 1
    assert session_events[0]["id"] == "s1"
    assert session_events[0]["title"] == "New Chat"
    fake_session.load_history.assert_not_called()
    assert agent.seen_context["previous_messages"] == []
    assert agent.seen_context["is_first_message"] is True


@pytest.mark.asyncio
async def test_memory_provenance_forwarded_to_append_assistant(fake_session):
    """business owns its own memory subsystem and stamps
    turn_meta.metadata.memory = {retrieved_memory_ids, summary_used} — run_turn
    must lift that block onto ChatSession.append_assistant's `metadata` kwarg
    so the /admin/memory Inspector (which reads
    ai_chat_messages.metadata.memory.retrieved_memory_ids per assistant turn)
    keeps working after the run_turn migration."""
    agent = _FakeAgent([
        {"type": "answer", "content": "Hello"},
        {"type": "usage", "input_tokens": 10, "output_tokens": 5, "model": "gemini-3-flash"},
        {"type": "turn_meta", "metadata": {
            "iteration_count": 1, "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "memory": {"retrieved_memory_ids": ["m1", "m2"], "summary_used": True},
        }},
    ])
    with patch("common.chat_session.get_service_client", return_value=MagicMock()), \
         patch("common.chat_session.ChatSession.for_identity", return_value=fake_session), \
         patch("common.chat_session.record_turn_telemetry", AsyncMock()):
        async for _ in run_turn(agent=agent, message="hi",
                identity=TurnIdentity(agent_kind="business", session_id="s1"),
                profile=_profile()):
            pass
    assert fake_session.append_assistant.call_args.args == ("Hello",)
    assert fake_session.append_assistant.call_args.kwargs["metadata"] == {
        "memory": {"retrieved_memory_ids": ["m1", "m2"], "summary_used": True}
    }


@pytest.mark.asyncio
async def test_no_memory_in_turn_meta_is_backward_compatible(fake_session):
    """sales and the client demos don't own a memory subsystem and never set
    turn_meta.metadata.memory — append_assistant must receive metadata=None
    (ChatSession.append_assistant defaults None -> {}), i.e. byte-for-byte the
    same behavior as before this enhancement."""
    agent = _FakeAgent([
        {"type": "answer", "content": "Hi there"},
        {"type": "turn_meta", "metadata": {
            "iteration_count": 1, "cache_read_tokens": 0, "cache_creation_tokens": 0,
        }},
    ])
    with patch("common.chat_session.get_service_client", return_value=MagicMock()), \
         patch("common.chat_session.ChatSession.for_identity", return_value=fake_session), \
         patch("common.chat_session.record_turn_telemetry", AsyncMock()):
        async for _ in run_turn(agent=agent, message="hi",
                identity=TurnIdentity(agent_kind="sales", session_id="s1"),
                profile=_profile()):
            pass
    assert fake_session.append_assistant.call_args.kwargs["metadata"] is None


@pytest.mark.asyncio
async def test_no_turn_meta_at_all_is_backward_compatible(fake_session):
    """Agents that don't yield a turn_meta event at all (nothing captures it,
    turn_meta stays {}) must also get metadata=None — covers any agent that
    hasn't been touched by this PR."""
    agent = _FakeAgent([{"type": "answer", "content": "Hi"}])
    with patch("common.chat_session.get_service_client", return_value=MagicMock()), \
         patch("common.chat_session.ChatSession.for_identity", return_value=fake_session), \
         patch("common.chat_session.record_turn_telemetry", AsyncMock()):
        async for _ in run_turn(agent=agent, message="hi",
                identity=TurnIdentity(agent_kind="sales", session_id="s1"),
                profile=_profile()):
            pass
    assert fake_session.append_assistant.call_args.kwargs["metadata"] is None


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_chat_persistence_runs_off_the_event_loop():
    # supabase-py is synchronous: called directly from run_turn it stalls the
    # event loop for a full network round-trip — in the duplex-voice process
    # that freezes the audio pumps and the holding-line timer, not just this
    # turn. Every ChatSession round-trip must land on a worker thread.
    import threading
    loop_thread = threading.get_ident()
    seen: list[int] = []

    class _Recorder:
        session_id = "s1"

        def resolve_or_create(self):
            seen.append(threading.get_ident())
            return ("s1", False, "T")

        def load_history(self):
            seen.append(threading.get_ident())
            return []

        def append_user(self, content):
            seen.append(threading.get_ident())

        def append_assistant(self, content, metadata=None):
            seen.append(threading.get_ident())

        def update_title(self, title):
            seen.append(threading.get_ident())

        def update_session_suggestions(self, suggestions):
            seen.append(threading.get_ident())

    agent = _FakeAgent([
        {"type": "answer", "content": "Hello"},
        {"type": "suggestions", "questions": ["a"]},
    ])
    with patch("common.chat_session.get_service_client", return_value=MagicMock()), \
         patch("common.chat_session.ChatSession.for_identity", return_value=_Recorder()), \
         patch("common.chat_session.record_turn_telemetry", AsyncMock()):
        async for _ in run_turn(agent=agent, message="hi",
                identity=TurnIdentity(agent_kind="sales", session_id="s1"),
                profile=_profile()):
            pass
    # resolve, history, user, assistant, suggestions
    assert len(seen) >= 5
    assert all(t != loop_thread for t in seen), \
        "a sync supabase round-trip ran on the event loop thread"


async def _window_seen_by_for_identity(fake_session, *, extra_context, profile=None):
    """Run one turn and return the `window` dict run_turn hands ChatSession."""
    captured = {}

    def _capture(supabase, identity, *, window):
        captured["window"] = window
        return fake_session

    agent = _FakeAgent([{"type": "answer", "content": "x"}])
    with patch("common.chat_session.get_service_client", return_value=MagicMock()), \
         patch("common.chat_session.ChatSession.for_identity", side_effect=_capture), \
         patch("common.chat_session.record_turn_telemetry", AsyncMock()):
        async for _ in run_turn(agent=agent, message="hi",
                identity=TurnIdentity(agent_kind="sample_agent", session_id="s1"),
                profile=profile or _profile(), extra_context=extra_context):
            pass
    return captured["window"]


@pytest.mark.asyncio
async def test_voice_turn_clamps_history_window_to_voice_cap(fake_session):
    """A delegated voice turn (voice/live/delegate.py stamps voice_turn=True)
    must clamp the profile's history window to voice_history_msg_count (12):
    every history row is prompt tokens on ~2 sequential model calls per spoken
    answer, and the delegate has to stay under the realtime service's ~8s
    tool-drop ceiling. The profile here configures 20 — voice gets 12."""
    window = await _window_seen_by_for_identity(
        fake_session, extra_context={"voice_turn": True})
    assert window["max_messages"] == 12
    # Clamp only touches the count — sibling window keys survive untouched.
    assert window["max_message_chars"] == 2000


@pytest.mark.asyncio
async def test_text_turn_keeps_the_profile_window(fake_session):
    """No voice_turn -> the profile's own window (20) reaches ChatSession
    unchanged. This is the half that proves the clamp is voice-scoped."""
    window = await _window_seen_by_for_identity(fake_session, extra_context=None)
    assert window["max_messages"] == 20


@pytest.mark.asyncio
async def test_voice_cap_is_a_clamp_not_an_override(fake_session):
    """A profile window already tighter than the voice cap stays tighter —
    min(), never a blanket 12."""
    profile = SimpleNamespace(
        memory_config={"window": {"max_messages": 5}},
        model_main={"model_string": "gemini-3-flash", "model_id": "m1"},
        profile_id="p1",
    )
    window = await _window_seen_by_for_identity(
        fake_session, extra_context={"voice_turn": True}, profile=profile)
    assert window["max_messages"] == 5


async def test_telemetry_row_carries_lmnr_trace_id(fake_session):
    agent = _FakeAgent([{"type": "answer", "content": "hi"}])
    tel = AsyncMock()
    with patch("common.chat_session.get_service_client", return_value=MagicMock()), \
         patch("common.chat_session.ChatSession.for_identity", return_value=fake_session), \
         patch("common.chat_session.current_lmnr_trace_id", return_value="lmnr-xyz"), \
         patch("common.chat_session.record_turn_telemetry", tel):
        async for _ in run_turn(agent=agent, message="hi",
                identity=TurnIdentity(agent_kind="sales", session_id="s1"),
                profile=_profile()):
            pass
    assert tel.call_args.kwargs["lmnr_trace_id"] == "lmnr-xyz"


# --- Persistence off the answer's critical path ---------------------------
#
# A delegated voice turn runs against the realtime service's ~8s tool-drop
# ceiling, and prod telemetry put ~1.35s of a ~6.7s p50 sales delegate in
# Supabase bookkeeping the ANSWER does not depend on: the user-row write in
# front of the first model token, then the transcript write and the
# `agent_traces` insert serialized behind an answer that was already final.
# These two tests pin that those writes overlap the work they are independent
# of. Both are written so the SEQUENTIAL implementation FAILS them (the
# blocking stub waits on an event the serialized order cannot set in time and
# observes the timeout), not merely runs slower.


async def test_user_row_write_does_not_block_the_agent_stream(fake_session):
    """The user insert runs CONCURRENTLY with the agent, and is joined before
    the assistant row so transcript order is unchanged."""
    order: list[str] = []
    agent_started = threading.Event()

    def _append_user(content):
        order.append("user_write_start")
        # Set by the agent as soon as its stream is entered. Serialized code
        # never gets there while this is running, so it times out.
        agent_started.wait(timeout=2)
        order.append("user_write_end")

    def _append_assistant(content, metadata=None):
        order.append("assistant_write")

    fake_session.append_user.side_effect = _append_user
    fake_session.append_assistant.side_effect = _append_assistant

    class _SignallingAgent:
        async def run_stream(self, message, context):
            order.append("agent_start")
            agent_started.set()
            yield {"type": "answer", "content": "Hello"}

    with patch("common.chat_session.get_service_client", return_value=MagicMock()), \
         patch("common.chat_session.ChatSession.for_identity", return_value=fake_session), \
         patch("common.chat_session.record_turn_telemetry", AsyncMock()):
        events = [ev async for ev in run_turn(
            agent=_SignallingAgent(), message="hi",
            identity=TurnIdentity(agent_kind="sales", session_id="s1"),
            profile=_profile())]

    # The agent got going while the user insert was still in flight.
    assert order.index("agent_start") < order.index("user_write_end"), order
    # ...but the assistant row still lands after it — transcript order intact.
    assert order.index("user_write_end") < order.index("assistant_write"), order
    # ...and everything is still persisted before the terminal event.
    assert events[-1]["type"] == "done"


async def test_transcript_and_telemetry_writes_overlap(fake_session):
    """The `ai_chat_messages` write and the `agent_traces` insert are
    independent tables written after a final answer — they must not be
    serialized behind each other."""
    telemetry_running = threading.Event()
    observed: dict[str, bool] = {}

    def _append_assistant(content, metadata=None):
        # True only if the telemetry insert had already started while this
        # write was in flight. Serialized code leaves it False on timeout.
        observed["overlapped"] = telemetry_running.wait(timeout=2)

    fake_session.append_assistant.side_effect = _append_assistant

    async def _telemetry(**kwargs):
        telemetry_running.set()

    tel = AsyncMock(side_effect=_telemetry)
    agent = _FakeAgent([{"type": "answer", "content": "Hello"}])
    saw_both_before_done = {"value": None}

    with patch("common.chat_session.get_service_client", return_value=MagicMock()), \
         patch("common.chat_session.ChatSession.for_identity", return_value=fake_session), \
         patch("common.chat_session.record_turn_telemetry", tel):
        async for ev in run_turn(agent=agent, message="hi",
                                 identity=TurnIdentity(agent_kind="sales", session_id="s1"),
                                 profile=_profile()):
            if ev.get("type") == "done":
                saw_both_before_done["value"] = (
                    fake_session.append_assistant.called and tel.await_count == 1
                )

    assert observed["overlapped"] is True
    # Neither write escaped to after `done` — the break-on-done contract holds.
    assert saw_both_before_done["value"] is True


# ---------------------------------------------------------------------------
# Per-stage timing for delegated voice turns: voice/live/delegate.py threads
# a `stage_timings` dict through extra_context; run_turn owns the
# `history_ms` stage (session resolve + history load). Text chat passes no
# dict and must be byte-identical to before.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_stage_is_timed_into_the_delegates_dict(fake_session):
    import time as _time

    def _slow_history():
        _time.sleep(0.03)  # runs in to_thread, like the real supabase call
        return [{"role": "user", "content": "prev"}]

    fake_session.load_history.side_effect = _slow_history
    timings: dict = {}
    agent = _FakeAgent([{"type": "answer", "content": "x"}])
    with patch("common.chat_session.get_service_client", return_value=MagicMock()), \
         patch("common.chat_session.ChatSession.for_identity", return_value=fake_session), \
         patch("common.chat_session.record_turn_telemetry", AsyncMock()):
        async for _ in run_turn(agent=agent, message="hi",
                identity=TurnIdentity(agent_kind="sample_agent", session_id="s1"),
                profile=_profile(),
                extra_context={"voice_turn": True, "stage_timings": timings}):
            pass
    assert timings["history_ms"] >= 25


@pytest.mark.asyncio
async def test_a_raising_stage_timings_dict_never_breaks_the_turn(fake_session):
    """Timing is telemetry: a dict whose writes raise must not cost the turn
    anything — the answer still persists and `done` still arrives."""

    class _Poison(dict):
        def __setitem__(self, key, value):
            raise RuntimeError("poisoned")

    agent = _FakeAgent([{"type": "answer", "content": "Hello"}])
    with patch("common.chat_session.get_service_client", return_value=MagicMock()), \
         patch("common.chat_session.ChatSession.for_identity", return_value=fake_session), \
         patch("common.chat_session.record_turn_telemetry", AsyncMock()):
        events = [ev async for ev in run_turn(agent=agent, message="hi",
                  identity=TurnIdentity(agent_kind="sample_agent", session_id="s1"),
                  profile=_profile(),
                  extra_context={"voice_turn": True, "stage_timings": _Poison()})]
    assert events[-1]["type"] == "done"
    assert not any(e["type"] == "error" for e in events)
    assert fake_session.append_assistant.called is True


@pytest.mark.asyncio
async def test_no_stage_dict_records_nothing(fake_session):
    """A non-dict (or absent) stage_timings is ignored, not crashed on."""
    agent = _FakeAgent([{"type": "answer", "content": "x"}])
    with patch("common.chat_session.get_service_client", return_value=MagicMock()), \
         patch("common.chat_session.ChatSession.for_identity", return_value=fake_session), \
         patch("common.chat_session.record_turn_telemetry", AsyncMock()):
        events = [ev async for ev in run_turn(agent=agent, message="hi",
                  identity=TurnIdentity(agent_kind="sample_agent", session_id="s1"),
                  profile=_profile(),
                  extra_context={"stage_timings": "not-a-dict"})]
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_tool_names_are_saved_on_the_reply_and_absent_without_tools(fake_session):
    """The names an agent reports in `tool_activity` are saved on the
    assistant row (metadata.tools_used), so a transcript can show which
    actions ran on an install with no tracing store. A turn with no tools
    keeps writing no metadata at all."""
    async def _run(events):
        fake_session.append_assistant.reset_mock()
        with patch("common.chat_session.get_service_client", return_value=MagicMock()), \
             patch("common.chat_session.ChatSession.for_identity", return_value=fake_session), \
             patch("common.chat_session.record_turn_telemetry", AsyncMock()):
            out = [ev async for ev in run_turn(
                agent=_FakeAgent(events), message="hi",
                identity=TurnIdentity(agent_kind="custom", session_id="s1"), profile=_profile())]
        return out, fake_session.append_assistant.call_args.kwargs["metadata"]

    out, meta = await _run([
        {"type": "tool_activity", "tools": ["search_knowledge"]},
        {"type": "tool_activity", "tools": ["lookup_order", ""]},
        {"type": "answer", "content": "Hello"},
    ])
    assert meta == {"tools_used": ["search_knowledge", "lookup_order"]}
    assert [e for e in out if e.get("type") == "tool_activity"]  # still forwarded to the client
    _out, meta = await _run([{"type": "answer", "content": "Hello"}])
    assert meta is None


# --------------------------------------------------------------------------
# Caller-supplied history (endpoints/openai_compat.py)
# --------------------------------------------------------------------------

async def _run_with_history(fake_session, history, **profile_overrides):
    agent = _FakeAgent([{"type": "answer", "content": "Hello"}])
    profile = _profile()
    for key, value in profile_overrides.items():
        setattr(profile, key, value)
    with patch("common.chat_session.get_service_client", return_value=MagicMock()), \
         patch("common.chat_session.ChatSession.for_identity", return_value=fake_session), \
         patch("common.chat_session.record_turn_telemetry", AsyncMock()):
        events = [ev async for ev in run_turn(
            agent=agent, message="now", identity=TurnIdentity(agent_kind="custom", session_id="s1"),
            profile=profile, history=history)]
    return agent, events


@pytest.mark.asyncio
async def test_supplied_history_replaces_the_stored_rows(fake_session):
    """A client that resends the conversation is the source of the history:
    the session's stored rows are not loaded on top of it (they would double
    every earlier turn), and the turn is still recorded."""
    history = [{"role": "user", "content": "client says"},
               {"role": "assistant", "content": "client heard"}]
    agent, events = await _run_with_history(fake_session, history)
    assert agent.seen_context["previous_messages"] == history
    assert fake_session.load_history.called is False
    assert fake_session.append_user.call_args[0] == ("now",)
    assert fake_session.append_assistant.call_args[0] == ("Hello",)
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_an_empty_supplied_history_is_still_the_history(fake_session):
    """A regenerated first message: the client says there is nothing before
    it, whatever the session holds."""
    agent, _ = await _run_with_history(fake_session, [])
    assert agent.seen_context["previous_messages"] == []
    assert fake_session.load_history.called is False


@pytest.mark.asyncio
async def test_supplied_history_is_capped_to_the_agents_window(fake_session):
    history = [{"role": "user", "content": f"m{i}"} for i in range(50)]
    agent, _ = await _run_with_history(fake_session, history)
    seen = agent.seen_context["previous_messages"]
    assert len(seen) == 20 and seen[-1]["content"] == "m49" and seen[0]["content"] == "m30"


@pytest.mark.asyncio
async def test_without_supplied_history_the_stored_rows_are_used(fake_session):
    agent, _ = await _run_with_history(fake_session, None)
    assert agent.seen_context["previous_messages"] == [{"role": "user", "content": "prev"}]
