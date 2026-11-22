"""Unit tests for the duplex-voice stage-latency collector.

Pure state-machine tests with a fake clock and the span-emission seam
patched — no Laminar, no network. The event sequences mirror what the Azure
provider actually emits (see test_azure_voice_live_provider.py for the wire
side of the same contract).
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

import voice.live.metrics as metrics_mod
from voice.live.metrics import SESSION_SPAN, TOOL_SPAN, TURN_SPAN, VoiceLiveMetrics


class FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def emitted(monkeypatch) -> List[Tuple[str, Dict[str, Any]]]:
    spans: List[Tuple[str, Dict[str, Any]]] = []
    monkeypatch.setattr(
        metrics_mod, "_emit_span", lambda name, attrs: spans.append((name, attrs))
    )
    return spans


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _collector(clock: FakeClock, **kw) -> VoiceLiveMetrics:
    return VoiceLiveMetrics(
        provider="azure_voice_live",
        agent_kind="sample_voice",
        base_attrs={"waj.env": "prod", "waj.agent_kind": "sample_voice"},
        clock=clock,
        **kw,
    )


def _spans_named(emitted, name):
    return [attrs for n, attrs in emitted if n == name]


def test_direct_turn_measures_eot_to_first_audio(clock, emitted):
    m = _collector(clock)
    m.on_event("user_turn_end")
    clock.advance(0.8)
    m.on_event("audio")
    clock.advance(2.0)
    m.on_event("turn_end")

    turns = _spans_named(emitted, TURN_SPAN)
    assert len(turns) == 1
    attrs = turns[0]
    assert attrs["waj.voice.live.eot_to_first_audio_ms"] == 800.0
    assert attrs["waj.voice.live.turn_ms"] == 2800.0
    assert attrs["waj.voice.live.op"] == "turn"
    assert attrs["waj.voice.live.provider"] == "azure_voice_live"
    assert attrs["waj.voice.live.interrupted"] == 0
    assert attrs["waj.voice.live.had_tool"] == 0
    # Base attributes ride along so ClickHouse rows stay env/agent-scoped.
    assert attrs["waj.env"] == "prod"


def test_turn_without_eot_omits_the_metric(clock, emitted):
    """The greeting (send_text_turn) has no user EOT — no fabricated number."""
    m = _collector(clock)
    m.on_event("audio")
    clock.advance(1.0)
    m.on_event("turn_end")

    attrs = _spans_named(emitted, TURN_SPAN)[0]
    assert "waj.voice.live.eot_to_first_audio_ms" not in attrs
    assert attrs["waj.voice.live.turn_ms"] == 1000.0


def test_eot_is_consumed_by_one_turn_only(clock, emitted):
    """A tool-result follow-up response must not reuse the question's EOT."""
    m = _collector(clock)
    m.on_event("user_turn_end")
    clock.advance(0.5)
    m.on_event("audio")          # announce turn consumes the EOT
    m.on_event("turn_end")
    clock.advance(5.0)
    m.on_event("audio")          # tool-result turn — no EOT left
    m.on_event("turn_end")

    turns = _spans_named(emitted, TURN_SPAN)
    assert "waj.voice.live.eot_to_first_audio_ms" in turns[0]
    assert "waj.voice.live.eot_to_first_audio_ms" not in turns[1]
    assert turns[0]["waj.voice.live.turn_index"] == 0
    assert turns[1]["waj.voice.live.turn_index"] == 1


def test_interrupt_mid_turn_is_a_barge_in_and_idle_is_not(clock, emitted):
    """The Azure provider emits `interrupted` on EVERY committed utterance as
    a playback-flush signal; only one landing while a turn is open is a real
    barge-in, and at most once per turn."""
    m = _collector(clock)
    m.on_event("audio")
    m.on_event("interrupted")   # mid-turn: barge-in
    m.on_event("interrupted")   # same turn: not double-counted
    m.on_event("turn_end")
    m.on_event("interrupted")   # idle: flush signal, not a barge-in

    attrs = _spans_named(emitted, TURN_SPAN)[0]
    assert attrs["waj.voice.live.interrupted"] == 1
    m.close()
    session = _spans_named(emitted, SESSION_SPAN)[0]
    assert session["waj.voice.live.barge_ins"] == 1


def test_assistant_transcript_opens_a_turn(clock, emitted):
    """A response that leads with text (transcript delta before audio) still
    gets a turn record; user transcripts never open one."""
    m = _collector(clock)
    m.on_event("transcript", role="user")
    m.on_event("transcript", role="assistant")
    clock.advance(0.3)
    m.on_event("turn_end")

    turns = _spans_named(emitted, TURN_SPAN)
    assert len(turns) == 1
    assert turns[0]["waj.voice.live.turn_ms"] == 300.0


def test_turn_end_without_any_output_emits_nothing(clock, emitted):
    """A cancelled response can fire response.done with no audio/transcript —
    there is no turn to report."""
    m = _collector(clock)
    m.on_event("turn_end")
    assert _spans_named(emitted, TURN_SPAN) == []


def test_tool_timer_measures_dispatch_eot_and_delegate(clock, emitted):
    m = _collector(clock)
    m.on_event("user_turn_end")           # the question's EOT
    clock.advance(1.0)
    m.on_event("audio")                   # announce
    timer = m.tool_started("call-1")      # dispatched 1s after EOT
    clock.advance(9.0)
    timer.delegate_ms = 8800.0
    timer.mark_first_speech()
    timer.finish(mode="interrupt", error=False, streamed_sentences=0)

    tools = _spans_named(emitted, TOOL_SPAN)
    assert len(tools) == 1
    attrs = tools[0]
    assert attrs["waj.voice.live.delegate_ms"] == 8800.0
    assert attrs["waj.voice.live.dispatch_to_result_ms"] == 9000.0
    assert attrs["waj.voice.live.eot_to_result_ms"] == 10000.0
    assert attrs["waj.voice.live.mode"] == "interrupt"
    assert attrs["waj.voice.live.error"] == 0


def test_tool_timer_reports_a_recovered_false_interrupt(clock, emitted):
    """An answer spoken because its interrupt was never confirmed (noise, not
    a barge-in) must be countable — this attribute is how false-barge-in rate
    gets tuned from the dashboard instead of from caller complaints."""
    m = _collector(clock)
    timer = m.tool_started("c1")
    timer.finish(mode="interrupt", error=False, streamed_sentences=0, recovered=True)
    plain = m.tool_started("c2")
    plain.finish(mode="interrupt", error=False, streamed_sentences=0)

    tools = _spans_named(emitted, TOOL_SPAN)
    assert tools[0]["waj.voice.live.recovered_false_interrupt"] == 1
    assert tools[1]["waj.voice.live.recovered_false_interrupt"] == 0


def test_tool_timer_first_speech_is_idempotent(clock, emitted):
    """Streaming delivers several sentences; only the FIRST is the caller's
    answer moment."""
    m = _collector(clock)
    timer = m.tool_started("call-1")
    clock.advance(3.0)
    timer.mark_first_speech()
    clock.advance(4.0)
    timer.mark_first_speech()             # later sentence — ignored
    timer.finish(mode="when_idle", error=False, streamed_sentences=2)

    attrs = _spans_named(emitted, TOOL_SPAN)[0]
    assert attrs["waj.voice.live.dispatch_to_result_ms"] == 3000.0
    assert attrs["waj.voice.live.streamed_sentences"] == 2


def test_tool_timer_finish_is_idempotent_and_error_flagged(clock, emitted):
    m = _collector(clock)
    timer = m.tool_started("call-1")
    timer.finish(mode="silent", error=True, streamed_sentences=0)
    timer.finish(mode="silent", error=True, streamed_sentences=0)

    tools = _spans_named(emitted, TOOL_SPAN)
    assert len(tools) == 1
    assert tools[0]["waj.voice.live.error"] == 1
    # Never spoken: no dispatch_to_result / eot_to_result fabricated.
    assert "waj.voice.live.dispatch_to_result_ms" not in tools[0]


def test_session_summary_counts_everything(clock, emitted):
    m = _collector(clock)
    m.on_event("user_turn_end")
    m.on_event("audio")
    m.on_event("tool_call")
    m.on_event("interrupted")
    m.on_event("turn_end")
    m.on_event("speech_started_idle")
    clock.advance(42.0)
    m.close()
    m.close()  # idempotent

    sessions = _spans_named(emitted, SESSION_SPAN)
    assert len(sessions) == 1
    attrs = sessions[0]
    assert attrs["waj.voice.live.session_sec"] == 42
    assert attrs["waj.voice.live.assistant_turns"] == 1
    assert attrs["waj.voice.live.user_turns"] == 1
    assert attrs["waj.voice.live.tool_calls"] == 1
    assert attrs["waj.voice.live.barge_ins"] == 1
    assert attrs["waj.voice.live.speech_started_idle"] == 1


def test_idle_speech_started_is_counted_separately_from_barge_ins(clock, emitted):
    """The diagnostic pair: `speech_started_idle` proves the service hears
    the caller start to speak, `barge_ins` proves it happened while the
    assistant held the floor. Live sessions showed barge_ins=0 against
    callers who said they had interrupted; 0/0 and N/0 mean different
    things, so the idle count must never be folded into barge_ins — and it
    must not open, close, or flag a turn either."""
    m = _collector(clock)
    m.on_event("speech_started_idle")
    m.on_event("speech_started_idle")
    assert _spans_named(emitted, TURN_SPAN) == []   # opens no turn

    m.on_event("audio")
    m.on_event("interrupted")                        # the real thing
    m.on_event("turn_end")
    turn = _spans_named(emitted, TURN_SPAN)[0]
    assert turn["waj.voice.live.interrupted"] == 1

    m.close()
    attrs = _spans_named(emitted, SESSION_SPAN)[0]
    assert attrs["waj.voice.live.speech_started_idle"] == 2
    assert attrs["waj.voice.live.barge_ins"] == 1


def test_tool_call_flags_the_open_turn(clock, emitted):
    m = _collector(clock)
    m.on_event("audio")
    m.on_event("tool_call")
    m.on_event("turn_end")
    attrs = _spans_named(emitted, TURN_SPAN)[0]
    assert attrs["waj.voice.live.had_tool"] == 1


def test_collector_never_raises_out_of_on_event(clock, emitted, monkeypatch):
    """A metrics bug must not take down the pump loop."""
    m = _collector(clock)
    monkeypatch.setattr(
        metrics_mod, "_emit_span",
        lambda name, attrs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    m.on_event("audio")
    m.on_event("turn_end")  # emission explodes internally; must not propagate
    m.close()


def test_unknown_event_types_are_ignored(clock, emitted):
    m = _collector(clock)
    m.on_event("__contract_probe__")
    m.on_event("error")
    assert emitted == []


def test_relayed_turns_counted_in_session_close(caplog):
    """Relay-mode beats are visible in the session summary — the live-debug
    line is how flow custody is audited after a call."""
    import logging
    from voice.live.metrics import VoiceLiveMetrics
    m = VoiceLiveMetrics(agent_kind="sample_voice", provider="fake")
    m.relayed_turn()
    m.relayed_turn()
    with caplog.at_level(logging.INFO, logger="voice.live.metrics"):
        m.close()
    assert "relayed=2" in caplog.text


def test_false_barge_in_suppressed_is_counted_on_the_session_span(emitted):
    """A suppressed false barge-in (transcript verdict empty/backchannel) is
    the direct measure of what the confirmed-barge-in machinery is absorbing.
    The endpoint emits this event when a held playback flush is dropped."""
    m = _collector(FakeClock())
    m.on_event("false_barge_in_suppressed")
    m.on_event("false_barge_in_suppressed")
    m.close()
    session_span = _spans_named(emitted, SESSION_SPAN)[0]
    assert session_span["waj.voice.live.false_barge_ins_suppressed"] == 2
    assert session_span["waj.voice.live.barge_ins"] == 0


def test_greeting_interrupt_suppressed_is_counted_on_the_session_span(emitted):
    """The greeting-echo guard (Fix 1): an `interrupted` reported against the
    endpoint's own dispatched greeting is suppressed rather than treated as a
    barge-in. Distinct from `false_barge_ins_suppressed` (a held FLUSH that
    was dropped on a noise verdict) — this counts a barge-in ACTION that was
    never taken at all, on a turn the caller could not possibly have
    interrupted (she has said nothing yet)."""
    m = _collector(FakeClock())
    m.on_event("greeting_interrupt_suppressed")
    m.close()
    session_span = _spans_named(emitted, SESSION_SPAN)[0]
    assert session_span["waj.voice.live.greeting_interrupts_suppressed"] == 1
    assert session_span["waj.voice.live.barge_ins"] == 0


def test_greeting_interrupt_suppressed_appears_in_the_session_log_line(caplog):
    import logging

    m = VoiceLiveMetrics(agent_kind="sample_voice", provider="fake")
    m.on_event("greeting_interrupt_suppressed")
    with caplog.at_level(logging.INFO, logger="voice.live.metrics"):
        m.close()
    assert "greeting_interrupts_suppressed=1" in caplog.text


def test_tool_span_carries_prefetch_and_render_attributes(clock, emitted):
    """The numbers spec §6 grades the change on: how often the prefetch hit,
    and which tool a deterministically-rendered turn skipped a synthesis
    call for."""
    m = _collector(clock)
    timer = m.tool_started("call_1")
    timer.delegate_ms = 1200.0
    timer.prefetch_hit = True
    timer.rendered_tool = "get_iban"
    timer.finish(mode="interrupt", error=False, streamed_sentences=0, recovered=False)

    attrs = _spans_named(emitted, TOOL_SPAN)[0]
    assert attrs["waj.voice.live.prefetch_hit"] == 1
    assert attrs["waj.voice.live.rendered_tool"] == "get_iban"
    # Defaults to 0 (a SUCCESSFUL render) when the caller never set it.
    assert attrs["waj.voice.live.rendered_failure"] == 0


def test_tool_span_marks_a_rendered_failure(clock, emitted):
    """A failed side-effecting tool call that ended on a truthful decline
    (an agent's deterministic failure render) must be
    distinguishable from a rendered SUCCESS on the same rendered_tool
    attribute — an operator watching for the #137-class defect needs to
    tell the two apart."""
    m = _collector(clock)
    timer = m.tool_started("call_1")
    timer.rendered_tool = "create_ticket"
    timer.rendered_failure = True
    timer.finish(mode="answer", error=False, streamed_sentences=0, recovered=False)

    attrs = _spans_named(emitted, TOOL_SPAN)[0]
    assert attrs["waj.voice.live.rendered_tool"] == "create_ticket"
    assert attrs["waj.voice.live.rendered_failure"] == 1


def test_rendered_tool_attribute_is_omitted_when_unset(clock, emitted):
    """No empty-string attribute -- a ClickHouse groupBy on this column must
    not carry a fake "" bucket for every turn that rendered nothing."""
    m = _collector(clock)
    timer = m.tool_started("call_1")
    timer.finish(mode="interrupt", error=False, streamed_sentences=0, recovered=False)

    attrs = _spans_named(emitted, TOOL_SPAN)[0]
    assert "waj.voice.live.rendered_tool" not in attrs
    # Nested under the same guard as rendered_tool -- no fake "0" bucket for
    # every turn that never entered the deterministic-render path at all.
    assert "waj.voice.live.rendered_failure" not in attrs
    assert attrs["waj.voice.live.prefetch_hit"] == 0


def test_outstanding_endpoint_requests_reach_the_session_summary(clock, emitted, caplog):
    """Gemini's `reply` verdict counts the endpoint's spoken requests down one
    per response; any left at close means the service coalesced some (each
    one stamped a later genuine reply `false`). Visible, not silent."""
    import logging
    from voice.live.metrics import SESSION_SPAN
    m = _collector(clock)
    with caplog.at_level(logging.INFO, logger="voice.live.metrics"):
        m.close(endpoint_requests_outstanding=2)
    attrs = [a for n, a in emitted if n == SESSION_SPAN][0]
    assert attrs["waj.voice.live.endpoint_requests_outstanding"] == 2
    assert "endpoint_requests_outstanding=2" in caplog.text


def test_providers_without_the_counter_add_no_attribute(clock, emitted):
    from voice.live.metrics import SESSION_SPAN
    _collector(clock).close()
    attrs = [a for n, a in emitted if n == SESSION_SPAN][0]
    assert "waj.voice.live.endpoint_requests_outstanding" not in attrs


def test_gemini_session_exposes_its_outstanding_requests():
    from voice.live.config import LiveVoiceConfig
    from voice.live.providers.gemini_live import GeminiLiveSession
    session = GeminiLiveSession(LiveVoiceConfig(enabled=True), persona="p")
    session._turn_state.endpoint_requests = 3
    assert session.endpoint_requests_outstanding == 3
