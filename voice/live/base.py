"""The provider-agnostic realtime-voice contract.

The engine is migrating GCP -> Azure, so this interface is deliberately
described in OUR terms, never a vendor's. Anything a specific API happens to
call this (Gemini's `FunctionResponseScheduling`, Azure's
`conversation.item.create`) is that provider's implementation detail and must
not appear above `voice/live/providers/`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Dict, Optional


class RealtimeProviderError(RuntimeError):
    """A provider-side failure. The WS handler turns this into a close frame."""


@dataclass(frozen=True)
class ToolSpec:
    """The one tool a `RealtimeSession` provider must expose to its model.

    `name` and `query_arg` here, NOT a provider-local constant. This closes a
    seam that leaked out of the cloud-agnostic boundary: `endpoints/
    voice_live.py`'s `_handle_tool_call` reads
    `(event.arguments or {}).get(ASK_AGENT_TOOL_SPEC.query_arg)` with no
    fallback, and the persona prompt -- a `agent_prompts` row shared across
    every provider (see `runtime/prompts/voice.py`) -- names `ask_agent`
    explicitly and instructs the model to call it. Before this, nothing in
    `base.py` or the conformance suite said a provider must declare a tool by
    this name/argument, so an Azure provider declaring e.g.
    `ask_assistant(question=...)` would have passed
    `assert_realtime_contract` cleanly and then silently delivered an empty
    query to the agent on every call.

    A provider is free to word its own SDK-specific function-declaration
    `description`/schema however it likes (see `gemini_live.py`'s
    `ASK_AGENT_DECLARATION`) -- only the name and the single argument name
    are fixed across providers. Each concrete `RealtimeSession` sets its
    `tool_spec` class attribute, ideally DERIVED from the real declaration it
    hands to its SDK (not hand-copied) so the two can't drift independently,
    and `assert_realtime_contract` asserts it equals `ASK_AGENT_TOOL_SPEC`.
    """

    name: str
    query_arg: str


ASK_AGENT_TOOL_SPEC = ToolSpec(name="ask_agent", query_arg="query")

DEFAULT_ASK_AGENT_SUBJECT = "the agent"


def ask_agent_subject() -> str:
    """Who each provider's ask_agent description says handles the caller's
    request ("Ask <subject> to handle the caller's request. …").

    The realtime model reads that description on every call, and rewording
    tool text has changed call behaviour before, so a deployment keeps the
    exact words its calls were tuned on: a plugin's `voice_builders` hook may
    define `ask_agent_subject()`, the last plugin's answer wins. A build with
    no plugin says "the agent". Read per session, not at import.
    """
    from core.plugins import call_hook, load_hook

    found = [s for s in call_hook(load_hook("voice_builders"), "ask_agent_subject") if s]
    return found[-1] if found else DEFAULT_ASK_AGENT_SUBJECT


class ToolResultMode(str, Enum):
    """How a tool result should reach the caller's ears.

    INTERRUPT  — cut off whatever is being said and deliver this now.
    WHEN_IDLE  — hold until the model stops speaking, then deliver.
    SILENT     — add to context without speaking it (used when the caller
                 barged in while the tool was still running).
    """

    INTERRUPT = "interrupt"
    WHEN_IDLE = "when_idle"
    SILENT = "silent"


@dataclass(frozen=True)
class AudioFormat:
    """Declared by the provider, sent to the browser in the `ready` frame.

    Gemini Live is 16 kHz in / 24 kHz out; Azure Realtime is 24 kHz both ways.
    Nothing above the provider layer may assume either.
    """

    input_sample_rate: int
    output_sample_rate: int
    encoding: str = "pcm16"


@dataclass
class RealtimeEvent:
    """One thing that happened in the session.

    `type` is one of: audio, transcript, tool_call, interrupted,
    playback_flush, speech_started_idle, speech_stopped, turn_end,
    user_turn_end, user_transcript_failed, error.

    `speech_stopped` is OPTIONAL (azure_voice_live emits it on VAD's
    end-of-speech): the endpoint only uses it to stand down a pending
    sustained-speech cut (`barge_in_sustain_sec`), forwards nothing.

    Three of these can arise from the same upstream signal (Azure's
    `input_audio_buffer.committed` emits `user_turn_end` then
    `playback_flush`), so keep their meanings straight — conflating the last
    two cost us an answer per call:

      interrupted    — the provider DETECTED the caller starting to speak
                       while the assistant held the floor. Detection only:
                       background noise trips it too, so the endpoint no
                       longer flushes playback or bumps its barge-in epoch
                       on this alone — it waits for the utterance to be
                       CONFIRMED substantive (transcript content that is
                       non-empty and non-backchannel, or a tool_call).
                       With interrupt_confirm_window_sec = 0 the endpoint
                       reverts to treating it as an immediate barge-in.
      playback_flush — the caller's utterance committed; the browser's
                       buffered audio *may* need to stop. Carries NO intent:
                       the endpoint HOLDS this flush until the transcript
                       verdict (substantive → flush; empty/backchannel →
                       dropped, she keeps talking), with a safety valve so
                       a transcription outage degrades to flushing anyway.
      speech_started_idle
                     — OPTIONAL, and purely a measurement point too: the
                       provider detected the caller starting to speak while
                       NO response was active. Not a barge-in (nothing was
                       being said to cut into) and it changes no behaviour;
                       it exists because sessions kept reporting
                       `barge_ins=0` while callers said they had interrupted,
                       and a zero that means "never detected" needs telling
                       apart from a zero that means "never mid-response".
                       Counted by `voice/live/metrics.py`, forwarded to the
                       client by nothing.
      tool_result_dropped
                     — OPTIONAL. The provider posted a tool result and its
                       upstream REFUSED it because the matching function_call
                       no longer exists in the conversation (a committed
                       utterance superseded the response mid-call). Carries
                       the `call_id`. The answer for that call will never be
                       spoken from the tool result, and re-sending the same
                       text as a plain turn does NOT rescue it — the model has
                       no call to attach it to and improvises instead. The
                       endpoint uses this to stand the answer watchdog down
                       for that call and let the stall tiers recover the turn.
                       A provider whose upstream cannot refuse a tool result
                       simply never emits it.
      user_transcript_failed
                     — OPTIONAL. The caller's utterance was committed but its
                       transcription failed, so no user `final` will follow.
                       The endpoint forwards it to the client as an empty
                       user `final` (closing any late-transcript window) and
                       deliberately does NOT run the noise verdict on it: a
                       transcription outage is not evidence of noise.
      user_turn_end  — OPTIONAL, and purely a measurement point. A provider
                       emits it when its upstream has a real end-of-user-turn
                       signal; Gemini Live has none today and simply never
                       emits it. `voice/live/metrics.py` measures
                       end-of-turn -> first audio from it. The endpoint
                       forwards nothing to the client for it, and the
                       conformance suite deliberately does not require it.

    Azure originally reported a committed utterance as `interrupted`, which
    meant any sound the caller made during a ~10s delegated turn silenced the
    answer permanently. Providers must emit `interrupted` only for a genuine
    barge-in over the assistant's own speech.
    """

    type: str
    audio: Optional[bytes] = None
    role: Optional[str] = None          # transcript: "user" | "assistant"
    text: Optional[str] = None
    final: bool = False
    call_id: Optional[str] = None       # tool_call
    tool_name: Optional[str] = None
    arguments: Dict[str, Any] = field(default_factory=dict)
    message: Optional[str] = None       # error
    # audio only: True when this delta belongs to a response that REPLIES to
    # the caller's latest utterance (created after their speech onset). The
    # endpoint uses it to release a held playback flush BEFORE forwarding
    # reply audio, so the flush can never eat the start of the answer it was
    # protecting. Default False: providers without response identity simply
    # never set it, and the endpoint then relies on the transcript verdict
    # and the safety valve alone.
    reply: bool = False
    # transcript only: True when this assistant transcript is OUT-OF-BAND
    # speech (a holding line spoken via `speak_out_of_band`), not the answer
    # to the caller's utterance. Providers whose out-of-band lines surface as
    # ordinary transcript events (the simulated provider) set it so the
    # endpoint can mark the WS frame; providers that report
    # `out_of_band_started` separately (azure_voice_live) never need to —
    # the endpoint already knows at that send site. Default False: absent
    # means a normal in-conversation transcript.
    out_of_band: bool = False
    # turn_end only: does the provider response that just ended ANSWER a new
    # caller utterance (True), or is it a greeting / continuation (the answer
    # after an announce line) / holding line / recovery response (False)?
    # Forwarded as the additive `reply` key of the client's `turn_end` frame
    # (see protocol.turn_end_frame) so a client on completed-only STT can
    # attach a caller transcript that arrives after the turn_end to the turn
    # it belongs to. Deliberately tri-state and separate from `reply` above:
    # None = the provider cannot tell (Gemini Live), and the frame then omits
    # the key rather than asserting a False it does not know.
    turn_reply: Optional[bool] = None


class RealtimeSession(ABC):
    provider: str = "base"
    audio_format: AudioFormat
    # The tool this provider actually declares to its model -- see
    # ASK_AGENT_TOOL_SPEC's docstring for why this exists. Deliberately NO
    # default: a subclass MUST set this from its own real SDK declaration
    # (see GeminiLiveSession), never inherit it. A default here would only
    # catch a provider that volunteers a WRONG spec -- the likelier mistake
    # is a provider that simply forgets to override it at all, which would
    # then silently inherit the canonical contract, pass
    # assert_realtime_contract, and deliver an empty query to the agent on
    # every real call forever. Omitting the default turns that into a loud
    # `tool_spec must be set` failure instead (see assert_realtime_contract
    # in voice/live/testing.py).
    tool_spec: ToolSpec
    # Whether this provider can accept SEVERAL tool results for the SAME
    # call id (one per sentence, mid-turn) rather than exactly one at the
    # end. Per-sentence delivery (endpoints/voice_live.py's streaming path)
    # depends on that: a provider must DECLARE its position so the endpoint
    # can refuse to stream on one that can't, instead of duplicated or
    # undelivered sentences at runtime. Same no-default rationale as
    # `tool_spec` above -- a provider that forgets to set this must fail
    # assert_realtime_contract loudly (see voice/live/testing.py), not
    # silently pass with the endpoint guessing wrong.
    supports_streamed_tool_results: bool
    # Can this provider speak a line that is NOT a tool result — i.e. without
    # consuming the pending call id? Declared because the holding line ("still
    # working on that") is the one thing a caller must hear DURING a delegated
    # turn, and on a provider that accepts only one result per call id
    # (supports_streamed_tool_results=False) delivering it as a tool result
    # eats the real answer. A provider that declares True must implement
    # `speak_out_of_band`; one that declares False leaves the wait to the
    # persona's announce-then-call line. No default, same
    # forgot-to-override rationale as the two attributes above.
    supports_out_of_band_speech: bool
    # Accepting several results per call id is not the same as TRANSCRIBING
    # them, and this flag is only about the SCREEN. It answers: for an answer
    # delivered sentence by sentence, can this provider's output transcription
    # be used as the on-screen text? When False the endpoint sends its own
    # copy of each sentence instead (`answer_echo` in endpoints/voice_live.py)
    # — streaming itself is NOT disabled, because the transcript is a display
    # concern and turning streaming off to fix it costs up to 3.7s of first
    # speech on a long answer.
    #
    # Gemini Live is False. Live-probed 2026-08-19 against
    # `gemini-3.1-flash-live-preview` with the answer from that morning's
    # client call, delivered as the four sentences voice/chunker.py cuts it
    # into: the transcript came back 'أبشر،' / 'ولا تشيل' / 'هل معك' /
    # 'ولا أفتحها' — the leading fragment of each response and nothing after
    # it. The transcription lags the generated audio by ~1s, and each
    # response's outstanding remainder is abandoned when generation ends;
    # a short response never gets it out. The SAME answer as ONE tool result
    # transcribed completely and BYTE-IDENTICALLY, 5 runs out of 5 — which is
    # why the non-streamed delivery path is left reading the provider.
    #
    # Defaults False — the conservative direction, same posture as
    # `reports_response_in_flight` below: a provider nobody has probed shows
    # server-authored text it definitely asked for, where the other default
    # shows a truncation of it. Declare True only after watching a streamed
    # answer's transcript come back whole.
    transcribes_streamed_tool_results: bool = False
    # True when THIS session's upstream fills tool-call/latency silence
    # itself (Azure's server-side `interim_response`, configured per model
    # row) — the endpoint then must NOT layer its own holding line on top,
    # or the same silence gets filled twice. Unlike the two capability
    # flags above this defaults to False: it describes optional per-session
    # config, not a structural property every provider must take a position
    # on, and False reproduces today's behaviour everywhere.
    handles_interim_response: bool = False
    # Whether the endpoint may defer barge-in handling to a transcript
    # verdict on this session (the confirmed-barge-in machinery in
    # endpoints/voice_live.py). Requires the provider to actually DELIVER
    # that verdict: streamed user transcription with a FINAL fragment per
    # utterance, plus `playback_flush` on commit. azure_voice_live does;
    # Gemini Live never finalizes its input transcription (see the
    # user_turn_end note above), so it inherits False and keeps the legacy
    # cut-on-detection path byte-identically.
    supports_barge_in_confirmation: bool = False
    # Does this provider close every caller utterance with a FINAL fragment on
    # its USER transcript stream? Declared separately from the flag above
    # because that one demands MORE (playback_flush + non-settling cancel
    # overrides): the simulated provider finalizes transcripts and still
    # declares supports_barge_in_confirmation = False, so it cannot stand in
    # for this.
    #
    # Relay custody (endpoints/voice_live.py) is what needs it, and needs it
    # structurally: custody SUPPRESSES the model's own ask_agent — closing it
    # unspoken — while its only DISPATCH trigger is that final. A provider
    # that never finalizes satisfies the first half and can never reach the
    # second, so custody becomes a black hole: the caller reads back their
    # verification code, the model relays it as an ask_agent, the endpoint
    # closes that call unspoken, and no delegated turn ever runs. Live on the
    # client demo 2026-08-19, one 51s Gemini Live call: `relay mode OPEN (flow
    # signals: ['send_otp'])`, two calls closed unspoken, `relayed=0`, and a
    # caller whose OTP reached nobody. The endpoint therefore refuses to OPEN
    # custody unless the session declares this — see `_relay_can_dispatch`.
    #
    # azure_voice_live does (both shapes: streamed deltas closed by an empty
    # final, and a single final carrying the text). Gemini Live does not, and
    # says so explicitly rather than inheriting the default.
    # Beyond custody it is also the DECLARED answer for the whole
    # commit-keyed family (`last_commit`, the stall tiers, the blank-call
    # recovery): a provider that never finalizes simply never populates
    # `last_commit`, so those are already inert there. The admin voice
    # screen reads this to tell an operator that `stall_recovery_topic_terms`
    # and the rest of that family cannot take effect on this profile's
    # provider, instead of rendering a box whose contents are ignored.
    finalizes_user_transcripts: bool = False
    # Can the endpoint hand the model the floor again, mid-conversation,
    # without inventing a caller turn? See `reprompt()` below for why that is
    # the difference between a 1s recovery and a 10s one. Defaults False:
    # a provider whose upstream keeps thinking on its own has nothing to
    # implement, and False reproduces today's behaviour everywhere.
    supports_reprompt: bool = False
    # Is `response_in_flight` below a real ANSWER on this provider, or just
    # the base default? The property returns a bool either way, and that is
    # precisely the problem: `False` from a provider that tracks its wire
    # means "the floor is idle", while `False` from one that does not means
    # "I cannot say" — and the endpoint's answer watchdog reads the second as
    # the first, which is the 2026-08-05 duplicate-answer condition verbatim.
    #
    # The watchdog re-delivers an answer that has produced no audio for
    # `answer_watchdog_sec`. Delivery -> first audio on an idle floor is
    # 0.21s / 0.33s (live-probed against gpt-realtime), so no threshold
    # separates "wedged" from "still on its way" — only the floor's state
    # does. A provider that cannot report the floor's state therefore cannot
    # run this defence safely, and the endpoint does not arm it there.
    #
    # This is a capability, not a second opinion about the property: declare
    # True only when `response_in_flight` is overridden with a real reading of
    # the wire (assert_realtime_contract checks exactly that). Defaults False,
    # so a provider that has not thought about it gets no watchdog rather than
    # an unbraked one.
    reports_response_in_flight: bool = False
    # Does this provider close each committed caller utterance with a FINAL
    # user transcript? Everything the endpoint keys off `last_commit` needs
    # one: the three stall-recovery tiers, the blank-`ask_agent` recovery, the
    # caller-intent relay custody trigger, and the legacy substantive-commit
    # epoch bump all populate or read that record from the `final` fragment of
    # a user transcript.
    #
    # Declared separately from `supports_barge_in_confirmation` even though
    # that flag REQUIRES this one (the conformance suite asserts the
    # implication): confirmation additionally needs `playback_flush` on
    # commit, so it is the strictly stronger claim, and a provider that
    # delivers finals but no flush is a real shape the endpoint handles.
    #

    @property
    def response_in_flight(self) -> bool:
        """Is a response on its way upstream RIGHT NOW?

        Not a capability flag — live state, in the same "the provider reports
        reality, the endpoint reacts" family as `tool_result_dropped` and
        `out_of_band_started`. True covers a response the service has
        acknowledged, a `response.create` of ours it has not acknowledged yet,
        and a create the provider has already committed to issuing on the next
        `response.done`. All three mean the same thing to a caller: audio is
        coming.

        The endpoint's answer watchdog is what needs it. It fires when a
        delivered answer has produced no audio for `answer_watchdog_sec` and
        re-delivers it as a say-exactly text turn — a real rescue for the mute
        race it was built for (a rejected answer-create leaves the result item
        in the conversation with nothing speaking it), but a DUPLICATE
        GENERATOR when the answer was merely still on its way: live
        2026-08-05 17:46, the watchdog fired 4.0s after delivery while the
        answer's response was in flight behind a holding line, and the caller
        got the same sentence, verbatim, twice. A response in flight is not a
        mute answer; it is an answer about to speak.

        Default False so a provider that cannot answer the question keeps the
        pre-2026-08-05 behaviour exactly (the watchdog then judges on audio
        alone, as it always did).
        """
        return False

    @abstractmethod
    async def start(self) -> None:
        """Open the upstream session. Raises RealtimeProviderError on failure."""

    @abstractmethod
    async def send_audio(self, pcm: bytes) -> None:
        """Push one chunk of caller audio, in `audio_format.input_sample_rate`."""

    async def cancel_active_response(self) -> None:
        """Best-effort: stop the model's in-flight response, if any.

        Relay mode (endpoints/voice_live.py) calls this right after a caller
        utterance commits mid-protected-flow, so the model's own reply to
        that utterance never plays — the endpoint substitutes a delegated
        turn's answer instead. The base default is a no-op: a provider
        without a cancel mechanism degrades to the model's reply racing the
        relayed one, which relay mode tolerates (the delegated answer still
        arrives and flow state still advances server-side).
        """
        return None

    async def cancel_barged_in_response(self) -> None:
        """Cancel the response the caller talked over — and ONLY that one.

        Called by the endpoint when a barge-in is CONFIRMED (substantive
        transcript / tool_call). A provider that can tell responses apart
        (azure_voice_live commit-stamps them) must never cancel the reply
        the service auto-created for the caller's own utterance. The base
        default falls back to the coarse cancel_active_response(): correct
        whenever the provider has no finer identity to offer.
        """
        await self.cancel_active_response()

    async def suppress_spurious_reply(self) -> None:
        """Cancel the auto-created reply to an utterance that turned out to
        be noise (empty/backchannel transcript) — and ONLY that reply.

        Best-effort by design: fired at verdict time, so a reply that has
        not been created yet is missed (accepted race — the residual is one
        odd model reply, not a lost answer). Base default no-op: without
        response identity, guessing risks cancelling a real answer.
        """
        return None

    @abstractmethod
    async def send_text_turn(self, text: str) -> None:
        """Inject a caller-side TEXT turn and let the model respond to it.

        Exists because a realtime session is silent until spoken to, so
        nothing makes the assistant greet first. The endpoint uses this to
        deliver `voice_config.live.greeting_trigger` right after the socket
        opens.

        Deliberately generic rather than a `greet()`: "inject a text turn"
        is a primitive both backends already have (Gemini
        `send_client_content`; Azure Realtime `conversation.item.create` with
        an `input_text` part followed by `response.create`), whereas a
        greeting is policy that belongs in the operator's prompt.
        """

    @abstractmethod
    async def send_tool_result(
        self, call_id: str, payload: Dict[str, Any], *, mode: ToolResultMode
    ) -> None:
        """Return a tool result. `mode` decides whether it is spoken now,
        queued, or added silently."""

    async def speak_out_of_band(self, text: str) -> None:
        """Say `text` now, without it being a tool result or a caller turn.

        NOT abstract: only providers that declare
        `supports_out_of_band_speech` need it, and the endpoint checks that
        flag before calling. Deliberately worded as "say this" rather than
        naming any provider mechanism (Azure Realtime models it as an
        out-of-band `response.create` with `conversation: "none"`; another
        backend may do something else entirely).
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support out-of-band speech"
        )

    async def reprompt(self) -> bool:
        """Give the model the floor back, adding nothing to the conversation.

        Returns True when the request actually went upstream (False when the
        provider can't, or the floor was busy — the caller then falls back to
        whatever recovery it already had).

        Why this is a first-class primitive rather than an Azure quirk: a
        realtime model's turn is a REQUEST-scoped unit of work. When a
        response ends, the model does not think again until something asks it
        to — so a turn that ended after "one moment, let me check" with no
        function call in it is not late, it is FINISHED, and the caller waits
        forever (live 2026-08-04: three sessions in a row; live-probed
        2026-08-05 against gpt-realtime: no call within 12s of the stall,
        ever). The one thing that gets the owed call out is a new response:
        the same probe measured the function_call landing 0.37s after a bare
        re-prompt, carrying the caller's own question as its query.

        Deliberately NOT `retry_tool_call()`: nothing here says "tool". The
        endpoint decides WHEN the model owes something (its stall tripwire);
        this only says "your turn again", which is a primitive every realtime
        backend either has or doesn't.

        NOT abstract, default False: a provider that declares
        `supports_reprompt = False` (the default) is never asked.
        """
        return False

    async def report_playback(self, played_total_ms: float) -> None:
        """Playback reached `played_total_ms` ms into the forwarded stream.

        Sent by the endpoint whenever the client flushes playback
        (protocol.py's `played` frame). The value is an OFFSET into the
        audio stream as forwarded — audio an earlier flush discarded stays
        counted — so it maps directly onto a provider's sent-audio ledger. A provider that can repair its
        conversation to match what was heard uses it (Azure sends
        `conversation.item.truncate` — the service streams audio far faster
        than realtime, so a flush throws away speech the model believes it
        delivered); a provider with no such mechanism ignores it.

        NOT abstract, default no-op: this is a best-effort refinement, and
        requiring every provider (and every test fake) to stub it would buy
        nothing. The conformance suite deliberately does not require it.
        """

    @abstractmethod
    def events(self) -> AsyncIterator[RealtimeEvent]:
        """Yield events until the session closes."""

    @abstractmethod
    async def close(self) -> None:
        """Tear down. Must be safe to call twice."""
