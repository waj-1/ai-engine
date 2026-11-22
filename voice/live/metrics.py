"""Stage-latency metrics for the duplex voice edge.

The reference numbers for "does this call feel responsive" are per-stage
latencies (user end-of-turn -> first audio, question -> spoken answer,
delegated-turn wall time), and until this module existed the duplex edge
measured none of them — `_tag_live_span` attributes spend, not time, and a
WebSocket handler has no ambient request span for it to land on anyway.

Everything here is server-side: stamps are taken as provider events are
dequeued in the endpoint's pump loop, so they include provider->engine
network time but NOT browser playback. That is the part we control and can
regress-test; true voice-to-voice needs a waveform measurement on the client
and is recorded as out of scope in the design spec
(docs/superpowers/specs/2026-07-30-voice-live-latency-metrics-design.md).

Reporting rides the existing `waj.ttft_ms` pattern (common/sse.py): numbers
are NUMERIC span attributes, aggregated in ClickHouse with quantileIf by the
admin Dashboard's voice card (admin/screens/observability.py::
load_voice_live_insights). Spans are emitted as instantaneous points at each
completion moment — the measured durations live in `waj.voice.live.*`
attributes, never in span length, and `load_live_insights` drops
`voice.live.*` top spans from its generic trace-duration tile so the points
can't skew it.

Metrics must never break a live call: every emission is best-effort and the
collector itself never raises out of `on_event`.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

TURN_SPAN = "voice.live.turn"
TOOL_SPAN = "voice.live.tool"
SESSION_SPAN = "voice.live.session"


def _emit_span(name: str, attributes: Dict[str, Any]) -> None:
    """Open-and-close a point span carrying `attributes`. Module-level so
    tests patch it — the seam pattern of `_open_transport` in the Azure
    provider. `root_span` already swallows a missing/disabled Laminar; the
    extra guard is for anything else, because a metrics failure mid-call
    must never surface to the caller."""
    try:
        from core.observability import root_span

        with root_span(name, attributes):
            pass
    except Exception:
        logger.debug("voice.live metrics: span emission failed", exc_info=True)


def _ms(later: float, earlier: float) -> float:
    return round((later - earlier) * 1000.0, 1)


@dataclass
class _TurnRecord:
    """One assistant response (provider `turn_end` == response.done)."""

    index: int
    eot_ts: Optional[float] = None          # the user EOT this turn answers
    first_event_ts: Optional[float] = None  # first audio/assistant transcript
    first_audio_ts: Optional[float] = None
    interrupted: bool = False
    had_tool: bool = False


class ToolCallTimer:
    """Timing for one `ask_agent` call. Created at dispatch, finished once.

    `mark_first_speech()` is idempotent on purpose: the streaming path may
    deliver several sentences and the whole-answer fallback also marks after
    its send — only the FIRST spoken delivery is the caller-perceived answer
    moment. The holding line deliberately never marks it (it asserts no
    facts; it is not the answer).
    """

    def __init__(self, metrics: "VoiceLiveMetrics", call_id: str) -> None:
        self._metrics = metrics
        self.call_id = call_id
        self.dispatch_ts = metrics.now()
        # The EOT of the user question that triggered this call — captured at
        # dispatch because by delivery time the caller may have spoken again.
        self.eot_ts = metrics.last_eot_ts()
        self.first_speech_ts: Optional[float] = None
        self.delegate_ms: Optional[float] = None
        # Stamped by the endpoint from `DelegateResult.prefetch_hit` /
        # `.rendered_tool` (Task 8/9) -- the numbers spec §6 grades this
        # change on: how often the KB prefetch actually hit, and how often
        # the turn ended without a synthesis call.
        self.prefetch_hit: bool = False
        self.rendered_tool: Optional[str] = None
        # Stamped from `DelegateResult.rendered_failure` — distinguishes a
        # rendered SUCCESS from a rendered truthful FAILURE decline on the
        # same `rendered_tool` attribute (a product agent's deterministic
        # failure decline sets it).
        self.rendered_failure: bool = False
        self._finished = False

    def mark_first_speech(self) -> None:
        if self.first_speech_ts is None:
            self.first_speech_ts = self._metrics.now()

    def finish(
        self, *, mode: str, error: bool, streamed_sentences: int,
        recovered: bool = False,
    ) -> None:
        """Emit the `voice.live.tool` span. Safe to call twice (second no-ops).

        `recovered` marks an answer spoken because its interrupt was never
        confirmed (noise tripped speech-start detection, no utterance ever
        committed) — the per-tenant rate of these is the false-barge-in
        signal VAD tuning needs.
        """
        if self._finished:
            return
        self._finished = True
        attrs = self._metrics.base_attributes("tool")
        attrs["waj.voice.live.mode"] = mode
        attrs["waj.voice.live.error"] = int(error)
        attrs["waj.voice.live.streamed_sentences"] = int(streamed_sentences)
        attrs["waj.voice.live.recovered_false_interrupt"] = int(recovered)
        attrs["waj.voice.live.prefetch_hit"] = int(self.prefetch_hit)
        if self.rendered_tool:
            attrs["waj.voice.live.rendered_tool"] = self.rendered_tool
            attrs["waj.voice.live.rendered_failure"] = int(self.rendered_failure)
        if self.delegate_ms is not None:
            attrs["waj.voice.live.delegate_ms"] = round(self.delegate_ms, 1)
        if self.first_speech_ts is not None:
            attrs["waj.voice.live.dispatch_to_result_ms"] = _ms(
                self.first_speech_ts, self.dispatch_ts
            )
            if self.eot_ts is not None:
                attrs["waj.voice.live.eot_to_result_ms"] = _ms(
                    self.first_speech_ts, self.eot_ts
                )
        _emit_span(TOOL_SPAN, attrs)
        logger.info(
            "voice.live metrics: tool call_id=%s delegate_ms=%s "
            "dispatch_to_result_ms=%s eot_to_result_ms=%s mode=%s error=%s "
            "streamed=%d prefetch_hit=%s rendered_tool=%s rendered_failure=%s",
            self.call_id,
            attrs.get("waj.voice.live.delegate_ms"),
            attrs.get("waj.voice.live.dispatch_to_result_ms"),
            attrs.get("waj.voice.live.eot_to_result_ms"),
            mode, error, streamed_sentences,
            int(self.prefetch_hit), self.rendered_tool, int(self.rendered_failure),
        )


class VoiceLiveMetrics:
    """Provider-neutral state machine fed with `RealtimeEvent.type`s.

    The endpoint calls `on_event(type, role=...)` for every dequeued provider
    event, `tool_started(call_id)` per ask_agent dispatch, and `close()` from
    its teardown `finally`. Nothing here awaits and nothing raises.

    Turn model: a turn OPENS on the first assistant output (audio, or an
    assistant transcript delta for a response that leads with text) and
    CLOSES on `turn_end`. `user_turn_end` (emitted by providers with a real
    end-of-turn signal — Azure's `input_audio_buffer.committed`; Gemini has
    none today) stamps the pending EOT the next turn consumes, so
    `eot_to_first_audio_ms` is only reported for turns that genuinely answer
    a user utterance — the greeting, tool-result follow-ups and the
    out-of-band holding line carry no EOT and simply omit the attribute.

    Barge-in counting: an `interrupted` event only counts while a turn is
    OPEN (the caller cut the assistant off mid-response), at most once per
    turn. A committed user utterance is NOT one: the Azure provider reports
    that as `playback_flush` (a pure client playback-flush signal) precisely
    so it carries no barge-in intent — it used to be reported as
    `interrupted`, which silenced the answer the caller had just asked for.
    Those events land while no turn is open, so the count here is unchanged
    either way.

    `speech_started_idle` is the companion counter to that: the provider
    detected the caller starting to speak with no response in flight. It is
    NOT a barge-in and stays out of `barge_ins` — it is here so a session
    that reports `barge_ins=0` can be read. Live calls came back 0 while the
    caller insisted they had talked over the assistant, and with idle
    detections silently dropped there was no way to tell a service that
    never hears the caller's interruption on real hardware from one that
    hears it but never mid-response.
    """

    def __init__(
        self,
        *,
        provider: str,
        agent_kind: str = "",
        base_attrs: Optional[Dict[str, Any]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._provider = provider
        self._agent_kind = agent_kind
        self._base = dict(base_attrs or {})
        self._started_ts = clock()
        self._pending_eot: Optional[float] = None
        self._last_eot: Optional[float] = None
        self._turn: Optional[_TurnRecord] = None
        self._turns_completed = 0
        self._user_turns = 0
        self._tool_calls = 0
        self._barge_ins = 0
        self._false_barge_ins_suppressed = 0
        # `interrupted` events suppressed by the greeting-echo guard (Fix 1):
        # a caller's speakerphone echoing OUR OWN dispatched greeting back at
        # near-equal amplitude, reported by Gemini's VAD as speech and, on the
        # pre-fix legacy path, treated as an immediate barge-in on every
        # single call (turn_ms 650-707, `interrupted=True` on turn 0 of every
        # session — see LiveVoiceConfig.greeting_barge_in_guard_sec). Distinct
        # from `false_barge_ins_suppressed`: that one is a held FLUSH dropped
        # on a noise transcript verdict; this is a barge-in ACTION never taken
        # at all, on a turn nothing the caller says could legitimately
        # interrupt.
        self._greeting_interrupts_suppressed = 0
        # `interrupted` events absorbed as the provider reporting OUR OWN
        # floor cut (an INTERRUPT-mode delivery displacing the holding line)
        # -- see endpoints/voice_live.py's _SELF_CUT_ABSORB_SEC. Sits beside
        # the two suppression counters above: a non-zero count on a session
        # with streamed answers is the expected, healthy shape. NOTE: the raw
        # `interrupted` was already fed to on_event at the pump-loop top, so
        # an absorbed event may ALSO have counted one `barge_ins` on an open
        # turn -- same accounting as greeting_interrupts_suppressed; read the
        # pair together.
        self._self_interrupts_absorbed = 0
        # Flushes withheld because the caller still had unplayed answer audio
        # queued (endpoints/voice_live.py's playback cursor). The direct
        # measure of the 2026-08-04 mid-answer-truncation fix: every increment
        # is an answer that used to stop mid-sentence.
        self._playback_protected = 0
        self._answers_dropped_upstream = 0
        # Cancels issued by the endpoint's own barge-in CONFIRMATION — the
        # thing that actually stops her mid-sentence. `barge_ins` counts the
        # provider's `interrupted` EVENT and nothing else, so every session
        # where she was cut by a confirmed (or hallucinated) transcript read
        # `barge_ins=0`: healthy-looking logs through a live call that was not.
        #
        # 2026-08-05: narrowed to AUDIBLE cuts (the endpoint sent a playback
        # flush). It used to count every confirmation, i.e. every substantive
        # caller turn, and read as a catastrophe that wasn't: a 185s call
        # reported confirmed_cuts=8 across 9 user turns. `barge_in_confirmations`
        # below is the old, wider number; the pair is what makes the session
        # line readable — confirmations ≈ user turns is normal, cuts should be
        # roughly the number of times the caller actually talked over her.
        self._confirmed_cuts = 0
        # Cuts made on SUSTAINED caller speech over audible playback before
        # any transcript (endpoints/voice_live.py `barge_in_sustain_sec`) —
        # the talk-overs the transcript-confirmed path would only have cut
        # after the caller stopped. Not also counted in `confirmed_cuts`.
        self._sustained_cuts = 0
        self._sustained_cuts_recovered = 0
        self._barge_in_confirmations = 0
        self._speech_started_idle = 0
        # Stall re-prompts issued by the endpoint's announce-then-stall
        # tripwire — every one is a turn that would have sat out the full
        # recovery grace before this existed.
        self._reprompts = 0
        # Turns the ENDPOINT answered by direct delegation because the model's
        # own ask_agent never arrived (stall tiers 1/2, the silent tier, and
        # the unusable-call recovery all land in _handle_recovered_turn).
        # Counted because it was invisible: live 2026-08-27 a long call
        # produced ELEVEN consecutive recoveries — the model had stopped
        # calling tools entirely — and the session summary read clean
        # (reprompts counts only the bare response.create nudges). A non-zero
        # burst here is the context-bloat / tool-discipline collapse signal.
        self._recovered_turns = 0
        # Relayed turns — protected-flow beats the ENDPOINT routed to the
        # delegate directly (relay mode), bypassing the model's discretion.
        self._relayed_turns = 0
        self._closed = False

    # -- helpers ----------------------------------------------------------

    def now(self) -> float:
        return self._clock()

    def last_eot_ts(self) -> Optional[float]:
        return self._last_eot

    def base_attributes(self, op: str) -> Dict[str, Any]:
        attrs = dict(self._base)
        attrs["waj.voice.live.op"] = op
        attrs["waj.voice.live.provider"] = self._provider
        return attrs

    # -- event feed --------------------------------------------------------

    def on_event(self, event_type: str, *, role: Optional[str] = None) -> None:
        try:
            self._on_event(event_type, role)
        except Exception:  # metrics must never take down the pump loop
            logger.debug("voice.live metrics: on_event failed", exc_info=True)

    def _on_event(self, event_type: str, role: Optional[str]) -> None:
        now = self._clock()
        if event_type == "user_turn_end":
            self._pending_eot = now
            self._last_eot = now
            self._user_turns += 1
        elif event_type == "audio":
            turn = self._ensure_turn(now)
            if turn.first_audio_ts is None:
                turn.first_audio_ts = now
        elif event_type == "transcript" and role == "assistant":
            self._ensure_turn(now)
        elif event_type == "tool_call":
            self._tool_calls += 1
            if self._turn is not None:
                self._turn.had_tool = True
        elif event_type == "interrupted":
            if self._turn is not None and not self._turn.interrupted:
                self._turn.interrupted = True
                self._barge_ins += 1
        elif event_type == "speech_started_idle":
            # Session-level only: it never opens or touches a turn (there is
            # no response for it to be an interruption OF), it just proves
            # the service is hearing the caller start to speak at all.
            self._speech_started_idle += 1
        elif event_type == "false_barge_in_suppressed":
            # Synthetic, endpoint-emitted: a commit's held playback flush was
            # dropped because the transcript verdict came back empty/backchannel
            # — noise the confirmed-barge-in machinery absorbed. The direct
            # measure of what the 2026-08-02 change is doing.
            # NOTE: counts only when a flush is actually held (endpoint gated);
            # slow-transcription cases where the safety valve already fired are
            # NOT counted — the counter systematically undercounts those.
            self._false_barge_ins_suppressed += 1
        elif event_type == "greeting_interrupt_suppressed":
            # Synthetic, endpoint-emitted: an `interrupted` reported while the
            # greeting-echo guard is active (see LiveVoiceConfig.
            # greeting_barge_in_guard_sec) took none of the barge-in actions —
            # no epoch bump, no interrupted_frame, no playback flush.
            self._greeting_interrupts_suppressed += 1
        elif event_type == "self_interrupt_absorbed":
            # Synthetic, endpoint-emitted: an `interrupted` that arrived
            # inside the self-cut absorb window took none of the barge-in
            # actions -- no epoch bump, no interrupted_frame, no flush.
            self._self_interrupts_absorbed += 1
        elif event_type == "confirmed_cut":
            # Synthetic, endpoint-emitted from _confirm_barge_in — only on the
            # branches that actually send a playback flush, i.e. the caller was
            # hearing her when she started speaking.
            self._confirmed_cuts += 1
        elif event_type == "sustained_cut":
            self._sustained_cuts += 1
        elif event_type == "sustained_cut_recovered":
            # A sustained cut the transcript then judged noise/backchannel;
            # the unheard tail was re-said. Read against sustained_cuts.
            self._sustained_cuts_recovered += 1
        elif event_type == "barge_in_confirmation":
            # Synthetic, endpoint-emitted from _confirm_barge_in, once per
            # substantive caller utterance. Not an interruption on its own.
            self._barge_in_confirmations += 1
        elif event_type == "playback_protected":
            # Synthetic, endpoint-emitted: an unconfirmed commit's held flush
            # was abandoned because the browser was still playing out an
            # answer the provider already considers delivered.
            self._playback_protected += 1
        elif event_type == "reprompted":
            # Synthetic, endpoint-emitted: a model turn ended owing an
            # ask_agent call and the endpoint handed the floor back rather
            # than sitting out the recovery grace. Read against `tool_calls`
            # and the recovery lines: N reprompts with no
            # `recovering the committed question` warning behind them is N
            # stalls that cost ~1s instead of ~10s.
            self._reprompts += 1
        elif event_type == "recovered_turn":
            # Synthetic, endpoint-emitted from _handle_recovered_turn: the
            # endpoint delegated the caller's committed question itself
            # because the model's ask_agent never arrived or arrived
            # unusable. Read against `tool_calls`: a session where this
            # rivals tool_calls is a model that stopped calling tools.
            self._recovered_turns += 1
        elif event_type == "answer_dropped_upstream":
            # Synthetic, endpoint-emitted: the provider's upstream refused a
            # tool result because its function_call was already gone, so that
            # answer can never be spoken. Distinct from a delegate error --
            # the answer was computed fine, it just had nowhere to land.
            self._answers_dropped_upstream += 1
        elif event_type == "turn_end":
            if self._turn is not None:
                self._close_turn(now)

    def _ensure_turn(self, now: float) -> _TurnRecord:
        if self._turn is None:
            self._turn = _TurnRecord(
                index=self._turns_completed,
                eot_ts=self._pending_eot,
                first_event_ts=now,
            )
            self._pending_eot = None
        return self._turn

    def _close_turn(self, now: float) -> None:
        turn, self._turn = self._turn, None
        assert turn is not None  # guarded by the caller
        self._turns_completed += 1
        attrs = self.base_attributes("turn")
        attrs["waj.voice.live.turn_index"] = turn.index
        attrs["waj.voice.live.had_tool"] = int(turn.had_tool)
        attrs["waj.voice.live.interrupted"] = int(turn.interrupted)
        if turn.eot_ts is not None and turn.first_audio_ts is not None:
            attrs["waj.voice.live.eot_to_first_audio_ms"] = _ms(
                turn.first_audio_ts, turn.eot_ts
            )
        start = turn.eot_ts if turn.eot_ts is not None else turn.first_event_ts
        if start is not None:
            attrs["waj.voice.live.turn_ms"] = _ms(now, start)
        _emit_span(TURN_SPAN, attrs)
        logger.info(
            "voice.live metrics: turn %d eot_to_first_audio_ms=%s turn_ms=%s "
            "had_tool=%s interrupted=%s",
            turn.index,
            attrs.get("waj.voice.live.eot_to_first_audio_ms"),
            attrs.get("waj.voice.live.turn_ms"),
            turn.had_tool, turn.interrupted,
        )

    # -- tool + session ----------------------------------------------------

    def relayed_turn(self) -> None:
        """Count one relay-mode beat (see endpoints/voice_live.py)."""
        self._relayed_turns += 1

    def tool_started(self, call_id: str) -> ToolCallTimer:
        return ToolCallTimer(self, call_id)

    def close(self, *, endpoint_requests_outstanding: Optional[int] = None) -> None:
        """Emit the per-session summary span. Idempotent.

        `endpoint_requests_outstanding` (Gemini only; None elsewhere): spoken
        requests the endpoint made (greeting, holding line, answer, streamed
        sentences) that never got a response of their own by the end of the
        call. The `reply` verdict counts them down one per response; a
        non-zero value means the service coalesced several into one response
        (unprobed), and each unit stamped one later genuine reply `false`.
        """
        if self._closed:
            return
        self._closed = True
        try:
            now = self._clock()
            attrs = self.base_attributes("session")
            attrs["waj.voice.live.session_sec"] = int(now - self._started_ts)
            attrs["waj.voice.live.assistant_turns"] = self._turns_completed
            attrs["waj.voice.live.user_turns"] = self._user_turns
            attrs["waj.voice.live.tool_calls"] = self._tool_calls
            attrs["waj.voice.live.relayed_turns"] = self._relayed_turns
            attrs["waj.voice.live.speech_started_idle"] = self._speech_started_idle
            attrs["waj.voice.live.barge_ins"] = self._barge_ins
            attrs["waj.voice.live.false_barge_ins_suppressed"] = (
                self._false_barge_ins_suppressed
            )
            attrs["waj.voice.live.greeting_interrupts_suppressed"] = (
                self._greeting_interrupts_suppressed
            )
            attrs["waj.voice.live.self_interrupts_absorbed"] = (
                self._self_interrupts_absorbed
            )
            attrs["waj.voice.live.playback_protected"] = self._playback_protected
            attrs["waj.voice.live.confirmed_cuts"] = self._confirmed_cuts
            attrs["waj.voice.live.sustained_cuts"] = self._sustained_cuts
            attrs["waj.voice.live.sustained_cuts_recovered"] = self._sustained_cuts_recovered
            attrs["waj.voice.live.barge_in_confirmations"] = (
                self._barge_in_confirmations
            )
            attrs["waj.voice.live.answers_dropped_upstream"] = (
                self._answers_dropped_upstream
            )
            attrs["waj.voice.live.reprompts"] = self._reprompts
            attrs["waj.voice.live.recovered_turns"] = self._recovered_turns
            if endpoint_requests_outstanding is not None:
                attrs["waj.voice.live.endpoint_requests_outstanding"] = int(
                    endpoint_requests_outstanding
                )
            _emit_span(SESSION_SPAN, attrs)
            # `speech_started_idle` sits immediately before `barge_ins` on
            # purpose: the pair is how the next live test is read — 0/0 means
            # the service never heard the caller interrupt at all, N/0 means
            # it heard them but never while the assistant held the floor.
            # `false_barge_ins_suppressed` sits adjacent to `barge_ins`: the pair
            # reads together — showing what the 2026-08-02 confirmed-barge-in
            # machinery is absorbing. `greeting_interrupts_suppressed` sits
            # right after it for the same reason: it is what the 2026-08-19
            # greeting-echo guard is absorbing, and a non-zero count on a
            # session with a greeting configured is the expected, healthy
            # shape — not a symptom.
            logger.info(
                "voice.live metrics: session agent_kind=%s provider=%s sec=%s "
                "turns=%d user_turns=%d tool_calls=%d relayed=%d speech_started_idle=%d barge_ins=%d false_barge_ins_suppressed=%d greeting_interrupts_suppressed=%d self_interrupts_absorbed=%d playback_protected=%d confirmed_cuts=%d sustained_cuts=%d sustained_cuts_recovered=%d confirmations=%d answers_dropped_upstream=%d reprompts=%d recovered_turns=%d",
                self._agent_kind, self._provider,
                attrs["waj.voice.live.session_sec"],
                self._turns_completed, self._user_turns,
                self._tool_calls, self._relayed_turns,
                self._speech_started_idle, self._barge_ins,
                self._false_barge_ins_suppressed,
                self._greeting_interrupts_suppressed,
                self._self_interrupts_absorbed,
                self._playback_protected,
                self._confirmed_cuts,
                self._sustained_cuts,
                self._sustained_cuts_recovered,
                self._barge_in_confirmations,
                self._answers_dropped_upstream,
                self._reprompts,
                self._recovered_turns,
            )
            if endpoint_requests_outstanding:
                logger.info(
                    "voice.live metrics: session endpoint_requests_outstanding=%d "
                    "(responses the service never gave the endpoint's spoken requests)",
                    endpoint_requests_outstanding,
                )
        except Exception:
            logger.debug("voice.live metrics: close failed", exc_info=True)
