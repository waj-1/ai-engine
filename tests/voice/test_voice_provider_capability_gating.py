"""Defences attach to DECLARED provider capabilities, not to every session.

The recovery machinery in `endpoints/voice_live.py` grew across ~20 fixes in
three days, every one of them measured against `azure_voice_live`. When the
client demo moved to Gemini Live, the defences that were tuned against Azure
protocol facts came along — and the one with no capability gate came along
without its brake.

That defence is the answer watchdog. Its safety margin is not its threshold:
delivery -> first audio on an idle floor is 0.21s / 0.33s (live-probed
2026-08-05), so nothing about `answer_watchdog_sec` separates "wedged" from
"still on its way" — only `session.response_in_flight` does. The base property
returns False unconditionally, which the watchdog reads as "the floor is idle",
so on a provider that cannot report the wire the deferral silently never
engages and the watchdog degrades to exactly the shape that spoke one answer
twice in a live client call.

`reports_response_in_flight` makes that difference declared rather than
emergent, and these tests pin both directions of it: Azure keeps the full
rescue, a provider that cannot answer the question does not get an unbraked
one.
"""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import endpoints.voice_live as live_ep
from voice.live.base import AudioFormat, RealtimeEvent, RealtimeSession
from voice.live.config import LiveVoiceConfig
from voice.live.delegate import DelegateResult
from voice.live.testing import FakeRealtimeSession


@pytest.fixture
def harness(monkeypatch):
    """Same wiring as tests/voice/test_voice_live_endpoint.py's fixture.

    Duplicated rather than imported for the reason the turn-invariants file
    states: that fixture clears a module-level connection limiter shared
    across ITS file, and reaching across test modules for it would couple two
    files' rate budgets together.
    """
    state = {}
    live_ep._connect_limiter._store.clear()

    def _cfg(_conn, _kind):
        return state.get(
            "cfg", LiveVoiceConfig(enabled=True, model_id="m-1", provider="fake")
        )

    monkeypatch.setattr(live_ep, "_live_config_for", _cfg)
    monkeypatch.setattr(live_ep, "resolve_channel", lambda conn: None)
    monkeypatch.setattr(live_ep, "enforce_channel_kind", lambda conn, kind: None)
    monkeypatch.setattr(live_ep, "authenticate_optional", lambda authz: ("tok", "user-1"))
    monkeypatch.setattr(live_ep, "reserve_audio_seconds", lambda *a, **kw: 0)
    monkeypatch.setattr(
        live_ep, "build_agent_for_kind", lambda kind, params: (object(), object())
    )
    monkeypatch.setattr(
        live_ep, "resolve_persona_prompt", lambda cfg, kind, **kw: "You are a voice agent."
    )

    session = FakeRealtimeSession(audio_format=AudioFormat(16000, 24000))
    monkeypatch.setattr(live_ep, "_session_for", lambda cfg, **kw: session)
    state["session"] = session

    app = FastAPI()
    app.include_router(live_ep.router)
    return TestClient(app), state


def _read_until(ws, kind, limit=8):
    for _ in range(limit):
        frame = ws.receive_json()
        if frame.get("type") == kind:
            return frame
    raise AssertionError(f"no {kind!r} frame in {limit} frames")


def _redelivered(session) -> bool:
    """Did the watchdog re-issue the answer as a say-exactly text turn?

    Matched on the ANSWER, not on the instruction wording: the instruction is
    operator-configurable per language now, so a test that asserted its English
    prefix would break the moment a tenant translated it — while the thing that
    actually matters (the caller hearing the same sentence a second time) is
    the answer text appearing in a delivery turn.
    """
    return any("Nine hundred." in t for t in session.text_turns)


def _drive_one_delegated_answer(client, session, monkeypatch):
    async def _delegate(**kwargs):
        return DelegateResult(spoken="Nine hundred.", display="AED 900", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(
            type="tool_call", call_id="c1", tool_name="ask_agent",
            arguments={"query": "net sales?"},
        ))
        _read_until(ws, "display")
        # Three full windows of provable silence — well past the watchdog's
        # own ceiling (`delivered_at + window * 3`), so a watchdog that was
        # going to fire at all has fired by now.
        time.sleep(1.0)
        return list(session.text_turns)


def test_the_watchdog_is_armed_on_a_provider_that_reports_the_floor(
    harness, monkeypatch
):
    """The rescue itself, unchanged, on an Azure-shaped session.

    This is the discriminating half: it fails if the capability gate is wired
    to the wrong sense, or if `reports_response_in_flight` stops reaching the
    endpoint at all. `FakeRealtimeSession` declares the flag exactly as
    `azure_voice_live` does.
    """
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0, answer_watchdog_sec=0.25,
    )
    session = state["session"]
    assert session.reports_response_in_flight is True

    _drive_one_delegated_answer(client, session, monkeypatch)
    assert _redelivered(session), session.text_turns


def test_the_watchdog_is_not_armed_on_a_provider_that_cannot_see_the_floor(
    harness, monkeypatch
):
    """The Gemini shape: `response_in_flight` is the base default.

    Reading that False as "the floor is idle" is what makes the watchdog an
    unbraked duplicate generator — it cannot tell an answer on its way from a
    mute one, and there is no threshold that can. The endpoint must not arm it.

    Note what is NOT asserted here: that the answer was spoken. It was —
    through the ordinary tool-result delivery — and this test deliberately
    only pins that the SECOND, watchdog-issued copy never happens.
    """
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0, answer_watchdog_sec=0.25,
    )
    session = state["session"]
    # Instance shadow of the class attr — the same technique
    # tests/voice/test_stall_reprompt.py uses to model a provider that does
    # not declare a capability.
    session.reports_response_in_flight = False

    _drive_one_delegated_answer(client, session, monkeypatch)
    assert not _redelivered(session), session.text_turns


def test_the_two_flags_default_off_on_the_contract():
    """A provider that has not thought about either question gets neither
    defence, rather than one that cannot brake itself.

    Both flags are documented as opt-in for exactly this reason, and the
    default is what a provider written after this change inherits by silence.
    """
    assert RealtimeSession.reports_response_in_flight is False
    assert RealtimeSession.finalizes_user_transcripts is False


def test_the_shipped_providers_declare_what_they_actually_do():
    """Pins the declarations against the reality each provider documents.

    Azure creates the answer's response itself (so it has a wire to read and a
    mute race to lose) and runs an azure-speech recognizer that finalizes every
    utterance. Gemini hands the tool response to the Live API and lets the
    service schedule the speech, and never finalizes input transcription — the
    same fact behind `supports_barge_in_confirmation` staying False there.
    """
    from voice.live.providers.azure_voice_live import AzureVoiceLiveSession
    from voice.live.providers.gemini_live import GeminiLiveSession

    assert AzureVoiceLiveSession.reports_response_in_flight is True
    assert AzureVoiceLiveSession.finalizes_user_transcripts is True

    assert GeminiLiveSession.reports_response_in_flight is False
    assert GeminiLiveSession.finalizes_user_transcripts is True


async def test_declaring_the_flag_without_implementing_it_fails_the_conformance_suite():
    """The flag must not be decoration.

    A provider that declares `reports_response_in_flight` while inheriting the
    base property hands the watchdog a floor it believes is permanently idle —
    which is worse than not declaring it at all, because it looks configured.
    `assert_realtime_contract` is where that gets caught, the same way
    `supports_reprompt` is.
    """
    from voice.live.testing import assert_realtime_contract

    class Liar(FakeRealtimeSession):
        provider = "liar"
        # Restore the BASE property, undoing the fake's drivable class attr:
        # this models the provider that declares the flag and then leaves
        # `response_in_flight` to the contract's default.
        response_in_flight = RealtimeSession.response_in_flight

    assert Liar.reports_response_in_flight is True

    with pytest.raises(AssertionError, match="reports_response_in_flight"):
        await assert_realtime_contract(lambda: Liar())


async def test_confirmation_cannot_be_declared_without_the_final_it_reads():
    """`supports_barge_in_confirmation` is the strictly stronger claim.

    Confirmation IS a verdict read off the final user transcript. A provider
    declaring it while declaring no finals describes a contract it cannot
    deliver: the verdict the endpoint holds its playback flush for would never
    arrive and every commit would ride the valve's expiry instead — a silent
    2s tax on every turn, which is precisely the kind of thing that only shows
    up as "it feels laggy".
    """
    from voice.live.testing import assert_realtime_contract

    class Inconsistent(FakeRealtimeSession):
        provider = "inconsistent"
        supports_barge_in_confirmation = True
        finalizes_user_transcripts = False

    with pytest.raises(AssertionError, match="finalizes_user_transcripts"):
        await assert_realtime_contract(lambda: Inconsistent())
