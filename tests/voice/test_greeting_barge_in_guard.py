"""Fix 1: the greeting's own echo must not be treated as a barge-in.

Evidence, measured from a real call recording on the demo VM (100ms RMS,
caller channel vs. assistant channel):

    0.70s   176      802   <- assistant greeting audio starts
    0.90s   116     9481
    1.00s  8286    10078   <- caller's speakerphone echoes the greeting back
    1.20s 13504     9466      into the mic at roughly EQUAL amplitude
    1.40s  2516        0   <- greeting cut; then 4.4s of silence; hangup

Every session's turn 0 logged `interrupted=True` with turn_ms 650-707 --
this fired on every call, not an edge case. Gemini's VAD reads the echo as
speech and reports `interrupted`; because `GeminiLiveSession.
supports_barge_in_confirmation` is False, the endpoint's legacy
`elif not confirm_mode:` path used to treat it as an immediate barge-in --
bumping `interrupt_epoch` and flushing the browser's queued audio.

The greeting is dispatched by us the instant the call connects, before the
caller has said anything, so an "interruption" of it is echo or room noise
by construction. `LiveVoiceConfig.greeting_barge_in_guard_sec` (default 8.0)
guards exactly that window.
"""
import asyncio as _a
import time as _t

import endpoints.voice_live as live_ep
from voice.live.base import RealtimeEvent, ToolResultMode
from voice.live.config import LiveVoiceConfig
from voice.live.delegate import DelegateResult

from tests.voice.test_voice_live_endpoint import _read_until, harness  # noqa: F401


def _connect_gemini_like(client, state, *, guard_sec=None, greeting="Welcome, how can I help?"):
    """A session shaped like Gemini Live: no barge-in confirmation, so an
    `interrupted` takes the legacy immediate-cut path once the guard lifts."""
    overrides = dict(
        enabled=True, model_id="m-1", provider="fake",
        greeting_trigger=greeting,
    )
    if guard_sec is not None:
        overrides["greeting_barge_in_guard_sec"] = guard_sec
    state["cfg"] = LiveVoiceConfig(**overrides)
    session = state["session"]
    session.supports_barge_in_confirmation = False
    return session


def test_greeting_echo_is_not_treated_as_a_barge_in(harness):  # noqa: F811
    """The core fix: an `interrupted` reported while the greeting is still
    playing must not flush playback -- the caller must be able to hear the
    rest of the greeting instead of a fragment followed by silence."""
    client, state = harness
    session = _connect_gemini_like(client, state)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        assert session.text_turns == ["Welcome, how can I help?"]
        session.push(RealtimeEvent(type="interrupted"))
        session.push(RealtimeEvent(type="turn_end"))
        frame = ws.receive_json()
    # No `interrupted` frame was ever sent -- the very next thing the client
    # hears about is the greeting's own turn_end, not a flush. (Marked as the
    # greeting — additive keys, see protocol.turn_end_frame.)
    assert frame == {"type": "turn_end", "reply": False, "greeting": True}


def test_guard_disabled_at_zero_restores_legacy_behaviour(harness):  # noqa: F811
    """0 is the documented rollback lever -- the exact pre-fix path."""
    client, state = harness
    session = _connect_gemini_like(client, state, guard_sec=0)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="interrupted"))
        frame = ws.receive_json()
    assert frame == {"type": "interrupted", "reason": "barge_in"}


def test_no_greeting_trigger_means_no_guard_ever_arms(harness):  # noqa: F811
    """Nothing was dispatched for the guard to protect, so a session with no
    greeting configured must behave exactly as it did before Fix 1."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(enabled=True, model_id="m-1", provider="fake")
    session = state["session"]
    session.supports_barge_in_confirmation = False
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        assert session.text_turns == []
        session.push(RealtimeEvent(type="interrupted"))
        frame = ws.receive_json()
    assert frame == {"type": "interrupted", "reason": "barge_in"}


def test_guard_ends_at_the_greeting_turn_end_not_a_fixed_delay(harness):  # noqa: F811
    """Once the greeting's own turn completes, barge-in must behave exactly
    as today on every later turn -- the guard must never reach past it."""
    client, state = harness
    session = _connect_gemini_like(client, state)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        # The greeting's own (suppressed) interrupt, then its turn_end --
        # this closes the guard.
        session.push(RealtimeEvent(type="interrupted"))
        session.push(RealtimeEvent(type="turn_end"))
        assert ws.receive_json() == {"type": "turn_end", "reply": False, "greeting": True}
        # A LATER interrupted event, on a later turn, must flush normally.
        session.push(RealtimeEvent(type="interrupted"))
        frame = ws.receive_json()
    assert frame == {"type": "interrupted", "reason": "barge_in"}


def test_guard_expires_at_its_cap_even_if_turn_end_never_arrives(harness):  # noqa: F811
    """A wedged provider that never emits the greeting's turn_end must not
    hold the floor forever -- the cap is the second half of the guard."""
    client, state = harness
    session = _connect_gemini_like(client, state, guard_sec=0.05)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        _t.sleep(0.15)  # outlast the 0.05s cap; no turn_end ever pushed
        session.push(RealtimeEvent(type="interrupted"))
        frame = ws.receive_json()
    assert frame == {"type": "interrupted", "reason": "barge_in"}


def test_guard_does_not_block_a_transcript_confirmed_real_interrupt(harness):  # noqa: F811
    """Review finding: the guard must be scoped to the legacy
    `elif not confirm_mode:` arm only. A confirm-mode session (Azure Voice
    Live shape -- `supports_barge_in_confirmation` stays True, the fake's
    default) never bumps the epoch / sends a frame / flushes playback from
    bare `interrupted` detection alone -- it only arms `pending_interrupt`
    for a LATER, transcript-confirmed cut (`_confirm_barge_in`). Suppressing
    that detection during the greeting window would cost Azure the ability
    to honour a genuine caller barge-in for the whole guard duration, for a
    bug that only exists on a provider with no confirmation at all. So a
    real, transcript-confirmed interrupt during the greeting must still cut
    through here."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        greeting_trigger="Welcome, how can I help?",
        interrupt_confirm_window_sec=5.0,
    )
    session = state["session"]
    # supports_barge_in_confirmation stays True (the fake's default) --
    # exactly the shape the narrowed guard must leave alone.
    assert session.supports_barge_in_confirmation is True
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        assert session.text_turns == ["Welcome, how can I help?"]
        session.push(RealtimeEvent(type="audio", audio=b"GREETING-AUDIO"))
        assert ws.receive_bytes() == b"GREETING-AUDIO"
        # Provider-detected speech-start over the greeting, immediately
        # followed by real, substantive caller words -- a genuine barge-in,
        # not an echo.
        session.push(RealtimeEvent(type="interrupted"))
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="actually, wait", final=False))
        frame = _read_until(ws, "interrupted")
    assert frame == {"type": "interrupted", "reason": "barge_in"}


def test_utterance_clock_ticks_correctly_while_the_greeting_guard_is_armed(
    harness, monkeypatch  # noqa: F811
):
    """The MUST-preserve requirement: `utterance_clock["n"]` records the
    caller's speech, not the endpoint's policy, so an `interrupted` event
    must tick it whatever the guard decides to do about the rest of the
    event -- see the comment at the top of the `interrupted` handler.

    Investigated where that tick actually has a downstream consequence: the
    ONLY reader of a dispatch's stamped `"utterance"` value is the
    belongs-vs-supersedes carry-forward (`mine`, see
    test_voice_live_turn_invariants.py), and that block is gated on
    `confirm_mode` -- it never runs on the `not confirm_mode` arm the guard's
    suppression branch lives inside. So there is currently no black-box way
    to observe a MISSING tick on the exact suppressed path (the tick line
    sits, unconditionally, above all three `interrupted` sub-branches, which
    is what makes it structurally safe today). This test instead pins the
    tick's correctness through the ONE path where it has a real effect,
    with the greeting guard ARMED throughout (a greeting is configured and
    no turn_end is ever pushed) -- proving arming the guard changes nothing
    about a confirm-mode session's normal, clock-driven behaviour, and
    guarding the shared tick line itself: moving it to depend on
    `confirm_mode`, or widening the guard back over the whole handler (the
    exact regression Finding 1 fixed), would make dispatch A below wrongly
    look like it belongs to the second utterance and fail this test.
    """
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        greeting_trigger="Welcome, how can I help?",
        interrupt_confirm_window_sec=5.0,
        progress_after_sec=0, answer_watchdog_sec=0, relay_mode=False,
        stall_recovery_sec=0, stall_recovery_unannounced_sec=0,
        stall_recovery_silent_sec=0,
    )
    session = state["session"]
    assert session.supports_barge_in_confirmation is True  # guard is armed but inert here

    async def _slow(**kwargs):
        await _a.sleep(0.3)
        return DelegateResult(spoken="OLD ANSWER.", display="d", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        assert session.text_turns == ["Welcome, how can I help?"]  # guard armed
        # Dispatch A: an answer to an old question, still in flight, stamped
        # with the clock's reading at THIS moment (0).
        session.push(RealtimeEvent(type="tool_call", call_id="c1",
                                   tool_name="ask_agent",
                                   arguments={"query": "old question"}))
        # A genuine speech onset -- must tick the clock to 1 whatever the
        # guard's bookkeeping is doing elsewhere.
        session.push(RealtimeEvent(type="interrupted"))
        # A substantive final for a DIFFERENT question. If the clock ticked,
        # dispatch A's stamped utterance (0) no longer matches the current
        # reading (1), so it is NOT carried forward and must go SILENT.
        session.push(RealtimeEvent(type="transcript", role="user",
                                   text="a totally different question",
                                   final=True))
        _t.sleep(0.5)  # outlast the 0.3s slow delegate

    modes = [(p["spoken"], m) for _cid, p, m in session.tool_results]
    assert ("OLD ANSWER.", ToolResultMode.SILENT) in modes, modes


# ── `greeting: true` on the greeting's own transcript frames ─────────────────
# The app has to know WHILE the greeting streams (not only at its turn_end)
# that these captions are the greeting, to keep them out of the chat history.
def _captions_until_turn_end(ws):
    frames = []
    while True:
        frame = ws.receive_json()
        frames.append(frame)
        if frame["type"] == "turn_end":
            return frames


def test_greeting_captions_are_marked_while_they_stream(harness):  # noqa: F811
    client, state = harness
    session = _connect_gemini_like(client, state)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="transcript", role="assistant", text="Hi, WAJ AI", final=False))
        session.push(RealtimeEvent(type="interrupted"))          # its own echo, suppressed
        session.push(RealtimeEvent(type="transcript", role="assistant", text=" here.", final=True))
        session.push(RealtimeEvent(type="turn_end"))
        greeting = _captions_until_turn_end(ws)
        # The next response is an ordinary one: no key at all.
        session.push(RealtimeEvent(type="transcript", role="assistant", text="Sure.", final=False))
        session.push(RealtimeEvent(type="turn_end"))
        later = _captions_until_turn_end(ws)
    captions = [f for f in greeting if f["type"] == "transcript"]
    assert captions and all(f.get("greeting") is True for f in captions), greeting
    assert greeting[-1]["greeting"] is True
    assert all("greeting" not in f for f in later), later


def test_greeting_stays_marked_even_if_the_caller_talks_during_it(harness):  # noqa: F811
    client, state = harness
    state["cfg"] = LiveVoiceConfig(enabled=True, model_id="m-1", provider="fake",
                                   greeting_trigger="Welcome")
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ready = ws.receive_json()
        session.push(RealtimeEvent(type="transcript", role="assistant", text="Hi, WAJ", final=False))
        session.push(RealtimeEvent(type="speech_started_idle"))     # the caller talks over it
        session.push(RealtimeEvent(type="transcript", role="assistant", text=" AI here.", final=False))
        session.push(RealtimeEvent(type="turn_end"))
        frames = _captions_until_turn_end(ws)
    assert ready["greeting"] is True
    captions = [f for f in frames if f["type"] == "transcript"]
    assert len(captions) == 2 and all(f.get("greeting") is True for f in captions), frames


def test_ready_says_when_there_is_no_greeting(harness):  # noqa: F811
    client, state = harness
    state["cfg"] = LiveVoiceConfig(enabled=True, model_id="m-1", provider="fake", greeting_trigger="")
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ready = ws.receive_json()
    assert ready["type"] == "ready" and ready["greeting"] is False


def test_first_caption_carries_the_reply_verdict_once(harness):  # noqa: F811
    client, state = harness
    state["cfg"] = LiveVoiceConfig(enabled=True, model_id="m-1", provider="fake", greeting_trigger="")
    session = state["session"]
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="transcript", role="assistant", text="Sure", final=False,
                                   turn_reply=True))
        session.push(RealtimeEvent(type="transcript", role="assistant", text=", one sec.", final=False))
        first, second = ws.receive_json(), ws.receive_json()
    assert first["reply"] is True and "reply" not in second, (first, second)


# ---------------------------------------------------------------------------
# greeting_input_mute_sec — the provider must not HEAR the opening's echo.
#
# The guard above only stops the endpoint acting on `interrupted`; Gemini's
# own activity detection has already cancelled the greeting by then. Agents
# demo 10-08..10-13: the greeting cut ~0.8s in on ~40% of collection calls,
# the agent silent mid-sentence and the caller waiting (user_turns=0 on every
# one). The mute forwards the caller as same-length silence until the
# greeting has been generated and played out.
# ---------------------------------------------------------------------------
def _connect_muted(client, state, *, mute_sec=12.0):
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        greeting_trigger="Welcome, how can I help?", greeting_input_mute_sec=mute_sec,
    )
    session = state["session"]
    session.supports_barge_in_confirmation = False
    return session


def test_caller_audio_reaches_the_provider_as_silence_during_the_greeting(harness):  # noqa: F811
    client, state = harness
    session = _connect_muted(client, state)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        ws.send_bytes(b"\x01\x02\x03\x04")
        ws.send_json({"type": "mute"})  # round-trip to let the pump run
        _t.sleep(0.05)
    assert b"\x00\x00\x00\x00" in session.sent_audio  # same length, silent
    assert b"\x01\x02\x03\x04" not in session.sent_audio


def test_mute_lifts_once_the_greeting_is_generated_and_played_out(harness):  # noqa: F811
    client, state = harness
    session = _connect_muted(client, state)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="turn_end"))  # nothing queued: played out
        assert ws.receive_json()["type"] == "turn_end"
        ws.send_bytes(b"\x01\x02")
        ws.send_json({"type": "mute"})
        _t.sleep(0.05)
    assert b"\x01\x02" in session.sent_audio


def test_mute_holds_while_the_generated_greeting_is_still_playing(harness):  # noqa: F811
    """turn_end means GENERATED, not heard: Gemini outruns real time, and
    the tail still playing at the caller echoes just the same."""
    client, state = harness
    session = _connect_muted(client, state)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="audio", audio=b"\x00\x01" * 48000))  # ~4s queued
        ws.receive_bytes()
        session.push(RealtimeEvent(type="turn_end"))
        assert ws.receive_json()["type"] == "turn_end"
        ws.send_bytes(b"\x01\x02")
        ws.send_json({"type": "mute"})
        _t.sleep(0.05)
    assert b"\x01\x02" not in session.sent_audio
    assert b"\x00\x00" in session.sent_audio


def test_mute_releases_at_its_cap_without_a_turn_end(harness):  # noqa: F811
    client, state = harness
    session = _connect_muted(client, state, mute_sec=0.05)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        _t.sleep(0.15)
        ws.send_bytes(b"\x01\x02")
        ws.send_json({"type": "mute"})
        _t.sleep(0.05)
    assert b"\x01\x02" in session.sent_audio


def test_mute_is_off_by_default(harness):  # noqa: F811
    client, state = harness
    _connect_gemini_like(client, state)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        ws.send_bytes(b"\x01\x02")
        ws.send_json({"type": "mute"})
        _t.sleep(0.05)
    assert b"\x01\x02" in state["session"].sent_audio
