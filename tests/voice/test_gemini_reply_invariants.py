"""Gemini turn_end `reply` across every order one call can physically produce.

Same method as test_voice_live_turn_invariants.py: rather than script the one
interleaving that broke, enumerate the orders the service can emit for one
exchange and assert what must hold in all of them.

The exchange (the 2026-09-28 E2E block-time turn, generalised):

    greeting   endpoint text turn → G opens … G closes
    question   caller transcript  → R1 opens (tool call) … R1 closes
    holding    endpoint tool result (sent while R1 is open) → H opens … H closes
    answer     endpoint tool result, sent while H is open or after it closed
               — or INTERRUPT-delivered, which interrupts H → A opens … A closes
    follow-up  caller transcript → R2 opens … R2 closes

Noise injected at every possible position:
  * E — an echo / backchannel / recognizer-invented fragment while a response
    is OPEN (no interruption follows);
  * L — a late fragment of the question, anywhere after R1 opened and before
    the follow-up.

Must hold in every order: G, H, A are never `true` (ours); R1 and R2 are `true`
(the caller's); an interrupted H carries no verdict.
"""
from __future__ import annotations

import itertools
from types import SimpleNamespace

import pytest

from voice.live.providers.gemini_live import UserTurnState, _translate


def _msg(*, user=None, audio=False, tool=False, turn_complete=False, interrupted=False):
    if tool:
        return SimpleNamespace(server_content=None, tool_call=SimpleNamespace(
            function_calls=[SimpleNamespace(id="c1", name="ask_agent", args={"query": "q"})]))
    parts = [SimpleNamespace(inline_data=SimpleNamespace(data=b"\x00\x01"), text=None)] if audio else []
    return SimpleNamespace(
        server_content=SimpleNamespace(
            interrupted=interrupted,
            input_transcription=SimpleNamespace(text=user, finished=False) if user else None,
            output_transcription=None,
            model_turn=SimpleNamespace(parts=parts) if parts else None,
            turn_complete=turn_complete, generation_complete=False),
        tool_call=None)


# Script steps. ("req",) = the endpoint sends something spoken (WHEN_IDLE);
# ("req_int",) = spoken with INTERRUPT scheduling; ("open", name) = first
# audio of a response; ("close", name) = its turn_complete; ("interrupt",) =
# the service's `interrupted`.
MODES = ("during_H", "after_H", "self_interrupt_close", "self_interrupt_noclose",
         "self_interrupt_lateclose", "caller_barge_close", "caller_barge_noclose")


def _script(mode):
    s = [("req",), ("open", "G"), ("close", "G"),
         ("user", "add a lunch break for Sara"), ("open", "R1"), ("tool",), ("req",),
         ("close", "R1"), ("open", "H")]
    if mode == "during_H":
        s += [("req",), ("close", "H"), ("open", "A"), ("close", "A")]
    elif mode == "after_H":
        s += [("close", "H"), ("req",), ("open", "A"), ("close", "A")]
    elif mode == "self_interrupt_close":
        # 2026-09-28 retest: the answer (INTERRUPT) cuts our holding line and
        # the service still completes the holding line's turn.
        s += [("req_int",), ("interrupt",), ("close", "H"), ("open", "A"), ("close", "A")]
    elif mode == "self_interrupt_noclose":
        s += [("req_int",), ("interrupt",), ("open", "A"), ("close", "A")]
    elif mode == "self_interrupt_lateclose":
        # Re-review round 3: the cut holding line's turn_complete arrives
        # AFTER the answer's first audio.
        s += [("req_int",), ("interrupt",), ("open", "A"), ("late_close", "H"), ("close", "A")]
    else:
        # The CALLER talks over the holding line; her question supersedes the
        # answer (delivered SILENT: no spoken request), the model answers her.
        s += [("user", "wait, make it Friday"), ("interrupt",)]
        if mode == "caller_barge_close":
            s += [("close", "H")]
        s += [("open", "Rb"), ("close", "Rb")]
    s += [("user", "and tomorrow?"), ("open", "R2"), ("close", "R2")]
    return s


def _open_slots(script):
    """Indices after which a response is open (for E)."""
    open_ = []
    out = []
    for i, step in enumerate(script):
        if step[0] == "open":
            open_ = [step[1]]
        elif step[0] in ("close", "interrupt"):
            open_ = []
        elif step[0] == "late_close":
            open_ = ["A"]
        if open_:
            out.append(i)
    return out


def _late_slots(script):
    """Indices after R1 opened and before the follow-up question (for L)."""
    start = script.index(("open", "R1"))
    end = script.index(("user", "and tomorrow?"))
    return list(range(start, end))


def _play(script, e_at, l_at):
    state = UserTurnState()
    verdicts = {}
    current = []
    cut = set()

    def feed(message):
        for ev in _translate(message, state):
            if ev.type == "turn_end":
                verdicts[current.pop(0)] = ev.turn_reply

    for i, step in enumerate(script):
        kind = step[0]
        if kind == "req":
            state.endpoint_requests += 1  # what send_text_turn / send_tool_result do
        elif kind == "req_int":
            state.endpoint_requests += 1
            state.self_interrupt_pending = True
        elif kind == "user":
            feed(_msg(user=step[1]))
        elif kind == "open":
            # A cut response that never completes is simply gone.
            current[:] = [c for c in current if c not in cut]
            current.append(step[1])
            feed(_msg(audio=True))
        elif kind == "tool":
            feed(_msg(tool=True))
        elif kind == "close":
            if step[1] in current:
                feed(_msg(turn_complete=True))
        elif kind == "late_close":
            current.insert(0, step[1])  # the stray turn_complete is ITS frame
            feed(_msg(turn_complete=True))
        elif kind == "interrupt":
            cut.update(current)
            feed(_msg(interrupted=True))
        if i == e_at:
            feed(_msg(user="mm-hm"))
        if i == l_at:
            feed(_msg(user="from one to two"))
    return verdicts


def _cases():
    for mode in MODES:
        script = _script(mode)
        for e_at in [None] + _open_slots(script):
            for l_at in [None] + _late_slots(script):
                yield mode, e_at, l_at


@pytest.mark.parametrize("mode,e_at,l_at", list(_cases()))
def test_reply_verdict_holds_in_every_order(mode, e_at, l_at):
    verdicts = _play(_script(mode), e_at, l_at)
    assert verdicts["G"] is False, verdicts
    assert verdicts["R1"] is True, verdicts
    if mode in ("during_H", "after_H", "self_interrupt_close", "self_interrupt_lateclose"):
        # Ours, even when our own INTERRUPT delivery cut it.
        assert verdicts["H"] is False, verdicts
    elif mode == "caller_barge_close":
        assert verdicts["H"] is None, verdicts  # the caller cut it: no guess
    else:
        assert "H" not in verdicts, verdicts  # never completed, no frame
    if mode.startswith("caller_barge"):
        assert verdicts["Rb"] is True, verdicts
    else:
        assert verdicts["A"] is False, verdicts
    assert verdicts["R2"] is True, verdicts


def test_the_enumeration_is_not_trivially_small():
    assert len(list(_cases())) > 100


def test_session_counts_its_own_spoken_requests():
    """The provider increments `endpoint_requests` itself for spoken sends
    (not for SILENT context repair, which produces no speech)."""
    import asyncio

    from voice.live.base import ToolResultMode
    from voice.live.config import LiveVoiceConfig
    from voice.live.providers.gemini_live import GeminiLiveSession

    session = GeminiLiveSession(LiveVoiceConfig(enabled=True), persona="p")

    class _Up:
        async def send_tool_response(self, **kw):
            pass

        async def send_client_content(self, **kw):
            pass

    session._upstream = _Up()

    async def _go():
        await session.send_text_turn("greet")
        await session.send_tool_result("c1", {"spoken": "x"}, mode=ToolResultMode.WHEN_IDLE)
        await session.send_tool_result("c1", {"spoken": "y"}, mode=ToolResultMode.INTERRUPT)
        await session.send_tool_result("c1", {"spoken": "z"}, mode=ToolResultMode.SILENT)

    asyncio.run(_go())
    assert session._turn_state.endpoint_requests == 3
    # The INTERRUPT delivery marks the next `interrupted` as the endpoint's own.
    assert session._turn_state.self_interrupt_pending is True


def test_speech_in_a_gap_is_consumed_by_our_next_response():
    """The caller speaks while our answer is queued: the client puts her words
    in OUR response's turn (they precede its audio), so the model's own answer
    after it must be `false` and merge — a `true` there would open a turn with
    no user text, whose late window swallows her NEXT utterance."""
    state = UserTurnState()
    ends = []

    def feed(m):
        ends.extend(e.turn_reply for e in _translate(m, state) if e.type == "turn_end")

    state.endpoint_requests += 1               # answer queued
    feed(_msg(user="actually, tomorrow?"))     # she speaks in the gap
    feed(_msg(audio=True))                     # our answer takes the floor
    feed(_msg(turn_complete=True))
    feed(_msg(audio=True))                     # the model answers her
    feed(_msg(turn_complete=True))
    assert ends == [False, False]
