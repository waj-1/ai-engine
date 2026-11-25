"""Resolve the effective LIVE (duplex) voice configuration for a request.

Since the 2026-08-17 unification, `agent_profiles.voice_config` IS the
duplex settings block — read flat, not nested under a `live` key. It used
to be one of three layers (env defaults -> profile `voice_config.live` ->
channel `config.voice.live`); the channel layer is gone, because nothing
ever wrote it, no admin screen labelled it, and a live audit found zero
rows using it on any stack. `_voice_block()` still accepts a row that
carries the old nested `{"live": {...}}` shape for one release — logging a
warning, not silently — so an unmigrated database keeps working while it
catches up. That shim and the now-inert `channel` parameter on
`resolve_live_config` are two halves of the same rollback window: remove
them together, once every environment has run the `unify_voice_config`
migration.

Nothing here is hardcoded config: the realtime model is an `agent_models` row
and the persona prompt is an `agent_prompts` slug, both resolved at runtime.
"""
from __future__ import annotations

import logging

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from voice.config import apply_overrides, coerce_bool

logger = logging.getLogger(__name__)

_VOICE_FIELDS = (
    "enabled",
    "model_id",
    "voice_id",
    "language",
    "persona_prompt_slug",
    "greeting_trigger",
    "greeting_barge_in_guard_sec",
    "greeting_input_mute_sec",
    "delegate_timeout_sec",
    "progress_after_sec",
    "progress_on_dispatch",
    "progress_spoken",
    "progress_spoken_by_language",
    "failure_spoken",
    "failure_spoken_by_language",
    "on_screen_spoken",
    "on_screen_spoken_by_language",
    "say_exactly_instruction",
    "say_exactly_instruction_by_language",
    "stream_sentences",
    "kb_prefetch_enabled",
    "kb_prefetch_match_count",
    "deterministic_render_enabled",
    "phrase_list",
    "caller_languages",
    "voice_by_language",
    "agent_profile_slug",
    "speaking_rate",
    "relay_mode",
    "relay_intent_terms",
    "server_side_iban_capture",
    "interrupt_confirm_window_sec",
    "barge_in_enabled",
    "barge_in_sustain_sec",
    "answer_watchdog_sec",
    "stall_recovery_sec",
    "stall_recovery_unannounced_sec",
    "stall_recovery_silent_sec",
    "stall_recovery_topic_terms",
    "max_session_sec",
    "handshake_charge_sec",
    "budget_charge_interval_sec",
    "max_concurrent_sessions",
    "max_concurrent_per_ip",
    "relay_max_turns",
    "relay_max_sec",
    "relay_quiet_grace_turns",
    "backchannel_tokens",
    "text_only",
    "persona_claim_recovery_enabled",
    "prefetched_context",
    "local_barge_in_sec",
    "local_barge_in_dbfs",
)


@dataclass
class LiveVoiceConfig:
    # Operator-set, from voice_config.live.
    enabled: bool = False
    # Ambient-scribe mode: the realtime model responds in TEXT modality only —
    # no synthesized speech ever reaches the client. Assistant text still flows
    # (as transcript/display frames), user-side ASR is unaffected. Set on a
    # dictation (ambient scribe) profile; default False so phone kinds are
    # untouched.
    text_only: bool = False
    model_id: Optional[str] = None
    voice_id: str = "Kore"
    language: Optional[str] = None
    persona_prompt_slug: Optional[str] = None
    # Spoken first, before the caller says anything. A realtime session is
    # silent until spoken to, so without this the assistant just waits and the
    # caller thinks it is broken. Operator-editable prompt content, not a
    # source constant -- blank disables the greeting entirely.
    greeting_trigger: str = ""
    # The greeting is spoken by US, the instant the call connects, before the
    # caller has said a single word — so an `interrupted` event reported
    # against it cannot be a real barge-in on anything the caller said. A
    # real call recording on the demo VM (2026-08-19, 100ms RMS, caller
    # channel vs assistant channel) shows why that matters:
    #
    #   0.70s  176      802   <- greeting audio starts
    #   0.90s  116     9481
    #   1.00s 8286    10078   <- caller's SPEAKERPHONE echoes it back into
    #   1.20s 13504    9466      the mic at roughly EQUAL amplitude
    #   1.40s 2516        0   <- greeting cut; then 4.4s of silence; hangup
    #
    # Gemini's VAD reads that echo as speech and reports `interrupted`, and
    # because GeminiLiveSession.supports_barge_in_confirmation is False the
    # endpoint's legacy path treats it as an immediate barge-in — bumping
    # `interrupt_epoch` and flushing the browser's queued audio mid-word.
    # Every session's turn 0 logged `interrupted=True` with turn_ms 650-707:
    # this fired on EVERY call, not an edge case.
    #
    # While this guard is active (from the moment the greeting is dispatched
    # until its `turn_end`, capped here) an `interrupted` event is recorded
    # (the utterance clock still ticks — see the comment at the top of the
    # `interrupted` handler) but takes none of the barge-in actions: no epoch
    # bump, no `interrupted_frame`, no playback flush. It never reaches past
    # the greeting's own turn — cleared the moment that turn's `turn_end`
    # arrives, so a caller who genuinely interrupts turn 1 is never affected.
    # The cap exists so a greeting that never gets a `turn_end` (a wedged
    # provider) cannot hold the floor forever. 0 disables the guard entirely
    # — the rollback lever, restoring the exact pre-fix legacy behaviour on
    # the greeting the way `interrupt_confirm_window_sec=0` does for every
    # later turn. DEFAULT ON at 8.0 (not 0) because the bug it fixes was
    # breaking every single call, not an opt-in improvement.
    greeting_barge_in_guard_sec: float = 8.0
    # The guard above only stops the ENDPOINT acting on an `interrupted`;
    # on Gemini Live the provider's own activity detection has already
    # cancelled the greeting by then. Echo or room noise ~0.8s into the
    # opening then leaves the agent silent mid-sentence and the caller
    # waiting for it to finish — nothing ever ends the deadlock (agents demo,
    # 10-08..10-13: ~40% of collection calls, every one with no caller turn).
    # While > 0, caller audio is forwarded to the provider as SILENCE of the
    # same length from the greeting's dispatch until its `turn_end` has come
    # AND its queued audio has played out, capped at this many seconds — the
    # provider cannot be cut by what it never hears. The recording keeps the
    # real audio. Costs a caller who talks over the opening their words. 0 =
    # off (default); the agents-demo seeds set it.
    greeting_input_mute_sec: float = 0.0
    # A delegated turn is a FULL agent turn (LLM -> MCP tools -> LLM), so the
    # caller sits in silence for most of it. Measured on a client demo VM
    # 2026-08-19 over 174 delegated calls it is ~2.9s p50 / ~4.6s p90 (it read
    # ~10s when this block was written, on a slower brain and before the
    # voice-turn round-trip trims); the tail is what these three exist for,
    # and they are operator-tunable rather than baked in:
    #   delegate_timeout_sec -- hard cap on one delegated turn.
    #   progress_after_sec   -- speak a holding line once the turn exceeds
    #                           this. 0 disables it.
    #   progress_spoken      -- what to say. Operator text; it asserts no
    #                           facts, so it stays inside the verbatim-safety
    #                           contract the same way FAILURE_SPOKEN does.
    # 30s, not 15: a multi-tool turn (branch performance over a period) blew
    # the 15s cap and the caller was told "there was a system problem" for
    # what was really impatience on our side. The holding line makes the wait
    # audible, so a longer ceiling costs nothing and saves the answer.
    delegate_timeout_sec: float = 30.0
    # 4s. Two failure modes bracket this number and both have been live.
    #
    # TOO EARLY: the persona already says "let me look that up" the moment it
    # calls the tool, so a holding line landing on top of it is two
    # near-identical waits back to back. Worse, once it fires within a beat of
    # the answer the caller hears a fragment of the line and then the answer
    # cutting it off — the first streamed sentence is delivered INTERRUPT and
    # flushes the browser's queue. A client demo carried 2.5s into a stack
    # whose answers arrive at a p50 of 2.68s: measured over 77 delegated calls
    # on the VM 2026-08-19, that fired on 56% of turns, i.e. the median firing
    # turn got its holding line ~0.2s before the answer it was masking.
    #
    # TOO LATE: a holding line that never fires is not a holding line. The old
    # 8.0 was set against ~10s turns; at today's distribution it fires on 1.3%.
    #
    # dispatch -> first spoken sentence, same 77 calls (google_genai, gpt-5-mini
    # brain): p10 1.20s, p50 2.68s, p90 4.41s. Firing rate by threshold —
    # 2.0s: 79%, 2.5s: 56%, 3.0s: 42%, 4.0s: 17%, 5.0s: 2.6%. 4.0s is the
    # value that speaks only into the genuinely slow tail (roughly the slowest
    # sixth) while still leaving ~1s of holding line before a p90 answer.
    # Operator-tunable per profile; 0 disables it.
    progress_after_sec: float = 4.0
    # The product ask this exists for: when a lookup is going to take a
    # while, say "understood, one moment" right after the caller finishes —
    # not `progress_after_sec` seconds into silence. The endpoint cannot
    # predict a tool's duration (the realtime model just calls `ask_agent`
    # with a query), so this is a MODE, not a smarter timer: True speaks the
    # holding line at DISPATCH instead of waiting out `progress_after_sec`.
    # `progress_after_sec` still gates whether a holding line exists AT ALL
    # (0 disables it, same as today) — this only changes WHEN a line that
    # would have fired gets spoken.
    #
    # NOT A DEFECT, BUT KNOW WHAT YOU ARE CHOOSING: since the endpoint
    # genuinely cannot predict a tool's duration up front, this also queues
    # the line on a SUB-SECOND turn that today would say nothing at all —
    # "one moment" immediately before an answer that was already fast. An
    # operator turning this on is trading that (a caller occasionally hears
    # a brief, harmless "one moment" ahead of an instant answer) for never
    # sitting in silence on a genuinely slow one; there is no per-turn
    # signal that lets the endpoint have it both ways.
    #
    # Works unchanged on the Gemini WHEN_IDLE path (`supports_out_of_band_
    # speech = False`): the earlier `send_tool_result(..., mode=WHEN_IDLE)`
    # is still deferred by the PROVIDER until it goes idle, so speaking
    # sooner does not cut across whatever filler the model is already
    # saying — it just means the line is already queued instead of being
    # registered four seconds late, which is the dead-air gap this closes.
    # DEFAULT OFF: with this False the sleep below is unconditional, exactly
    # today's byte-identical timer behaviour — this is opt-in per tenant.
    progress_on_dispatch: bool = False
    # A string OR a list of variant strings — see
    # resolve_progress_spoken_variants for why a caller should not hear the
    # identical holding line on every lookup.
    progress_spoken: Any = "Still working on that, one moment."
    # Per-language holding lines, keyed on the PRIMARY subtag exactly like
    # voice_by_language. Without this the fixed `progress_spoken` above is
    # spoken verbatim in whatever language it was written in — an English
    # "still working on that" landed mid-call in an Arabic session on the
    # demo VM. Falls back to `progress_spoken` for an unmapped language.
    # Values take the same string-or-list-of-variants shape as
    # `progress_spoken`.
    progress_spoken_by_language: Dict[str, Any] = field(default_factory=dict)
    # Spoken when a delegated turn errors, times out, or produces nothing, and
    # when an answer's substance is visual. Both are operator text asserting no
    # facts, so they sit inside the same verbatim-safety contract as
    # `progress_spoken`. Blank falls back to the built-in per-language wording
    # in delegate.py / speakable.py rather than to silence.
    failure_spoken: str = ""
    failure_spoken_by_language: Dict[str, str] = field(default_factory=dict)
    on_screen_spoken: str = ""
    on_screen_spoken_by_language: Dict[str, str] = field(default_factory=dict)
    # The instruction wrapped around an answer the ENDPOINT delivers itself
    # (relay custody, stall/blank-call recovery, the answer watchdog's
    # re-delivery) instead of returning it as a tool result. It reaches the
    # model as a caller-side text turn, so it is a PROMPT — the model obeys it
    # or improvises around it, and how well it obeys depends on the language it
    # is written in. It was an English literal at three sites in
    # endpoints/voice_live.py; on an Arabic call that handed the model an
    # English command wrapping Arabic text, which is the shape that produced a
    # mangled improvisation on 2026-08-05.
    #
    # Must contain `{spoken}` — the answer is substituted there, not appended,
    # because Arabic reads right-to-left and the instruction does not always
    # belong in front of it. A value with no `{spoken}` (or a broken format
    # spec) falls back to the built-in for the session's language rather than
    # dropping the answer: see resolve_say_exactly_instruction.
    #
    # Blank falls back to the built-in per-language wording in
    # _SAY_EXACTLY_INSTRUCTION below, exactly as `failure_spoken` falls back to
    # delegate.py's. There is deliberately no "" = disabled here: a bare answer
    # with no say-exactly instruction is the model free to paraphrase a figure,
    # which the whole verbatim-safety contract exists to prevent.
    say_exactly_instruction: str = ""
    say_exactly_instruction_by_language: Dict[str, str] = field(default_factory=dict)
    # Speak the agent's answer sentence-by-sentence as it streams, instead of
    # waiting for the whole turn.
    #
    # WHAT IT IS ACTUALLY WORTH, measured — because the number this comment
    # used to carry ("~10s down to ~3s") is off by two orders of magnitude and
    # sent people optimising the chunker. Over 85 streamed calls on a client
    # demo VM 2026-08-19, first-sentence delivery ran a median of just 68ms
    # ahead of whole-answer delivery, and that lead was FLAT at ~65ms across
    # 1-, 2- and 3-sentence answers (85% of calls) instead of growing with
    # sentence count.
    #
    # The reason is upstream of this flag: the brain does not dribble the
    # answer out, it emits it in a burst. Probed directly against the same
    # gpt-5-mini row (reasoning_effort=minimal, uaenorth) the same day —
    # time-to-first-content-token 1.0-1.6s, then the ENTIRE answer in
    # 154-539ms (a 3-sentence Arabic answer: 64 chunks over 154ms). So the
    # whole prize per-sentence streaming can ever win is the length of that
    # burst, and it is already winning most of it. Boundary rules, clause
    # cuts and time-boxed first flushes all divide up those ~150ms; the
    # 1.0-1.6s in front of them is the only part worth attacking.
    #
    # IT IS NOT FREE, though the cost is not latency. This comment said it
    # "costs nothing" until 2026-08-19, when a client's Live tab started
    # rendering the caller's spoken transcript and the real price appeared:
    # splitting one answer into N back-to-back tool results makes the provider
    # generate N short responses, and Gemini Live's output transcription does
    # not survive that — the caller HEARS the whole answer and READS the first
    # two words of each sentence. The fix is NOT to turn this off (that would
    # cost up to 3.7s of first speech on a long answer, the other thing the
    # same caller complained about): the provider declares
    # `transcribes_streamed_tool_results = False` and the endpoint sends the
    # screen its OWN copy of each sentence. Keep the two concerns apart when
    # tuning this — it is a LATENCY knob, and the screen is handled elsewhere.
    #
    # It is still worth real time on the long answers (a 10-sentence answer
    # led by 3.7s) — but do not expect a knob here to move a median call.
    #
    # DEFAULT ON. It relies on the provider accepting several tool results
    # for one call id and speaking them in order; a provider that can't
    # (supports_streamed_tool_results=False, e.g. azure_voice_live) is gated
    # off structurally in endpoints/voice_live.py regardless of this value.
    # Delivery order still needs a live listen per provider — an operator can
    # flip it back per tenant (voice_config.live / channel) with an explicit
    # false if sentences repeat or land out of order.
    stream_sentences: bool = True
    # --- Round-trip reduction (2026-08-19) --------------------------------
    # Measured on a client demo VM: 89% of a delegated tool turn is two
    # sequential brain LLM calls (1244ms + 1098ms); the tools themselves are
    # 10-122ms. These two levers each remove the SECOND call for a class of
    # turn. Both are silent when off — see scripts/check_profile_drift.py.
    #
    # KB questions are the slowest class (3568ms p50) because
    # the docs-search tool hands the brain 12,815 chars p50 that land in call 2.
    # Prefetching retrieves BEFORE call 1 so the answer costs one call.
    kb_prefetch_enabled: bool = True
    # Chat keeps the tool's own match_count of 6. Voice answers are 2-3 spoken
    # sentences, so 6 chunks is oversized; 3 is the starting point and the A/B
    # against 6 is an acceptance criterion, not an assumption.
    kb_prefetch_match_count: int = 3
    # OFF reopens the fabrication surface on the identifiers PR #137 guards —
    # a renderer emits digits from the tool result or nothing, so it is
    # structurally incapable of inventing an IBAN. Treat as an incident lever.
    deterministic_render_enabled: bool = True
    # Recognition-boosted domain terms. Consumed by providers with a real ASR stage
    # (Azure). Gemini ignores it.
    phrase_list: List[str] = field(default_factory=list)
    # Languages the CALLER may speak (BCP-47, e.g. ["ar-JO", "en-US"]). Set,
    # the Azure recognizer auto-detects among them instead of being locked
    # to `language` — which the app's UI locale overrides, so an English-UI
    # merchant speaking Arabic was transcribed as English (2026-10-02).
    caller_languages: List[str] = field(default_factory=list)
    # Primary-language-subtag → voice name. Falls back to `voice_id`.
    voice_by_language: Dict[str, str] = field(default_factory=dict)
    # Serve voice calls with a DIFFERENT profile of the same agent_kind
    # (resolved by slug via AgentProfileLoader.load_delegate). Unset — or a
    # slug that doesn't resolve — falls back to the kind's active profile,
    # so this is a one-field rollback lever, same posture as model_id.
    agent_profile_slug: Optional[str] = None
    # TTS pace, provider-interpreted (Azure prosody rate strings like
    # "+10%" / "-20%" / "0.9"). None means "don't send a rate at all" —
    # a provider that never receives the key can't reject it.
    speaking_rate: Optional[str] = None
    # Server-side custody of protected multi-beat flows (the IBAN change).
    # When a delegated turn's tools show an OTP flow opened, the ENDPOINT
    # routes every committed caller utterance straight into a delegated turn
    # until the flow closes — the realtime model keeps ears, mouth and
    # persona but loses mid-flow discretion. Three live failure modes on one
    # day (2026-07-31) forced this: a fabricated IBAN written on the update
    # beat, a verification code requested that was never sent, and a
    # dead-air stall holding digits it had nowhere to put. Default ON;
    # per-tenant/channel off-switch is the rollback lever.
    relay_mode: bool = True
    # Custody can also open on the CALLER'S OWN WORDS, before any tool runs:
    # live acceptance showed the model sometimes refuses to delegate beat
    # ONE of the change flow at all (or delegates it with an invented code
    # bundled in) — and tool-signal custody can only start after a delegated
    # turn exists. A caller utterance containing any of these terms opens
    # custody speculatively and relays that very utterance; a false positive
    # costs one-two delegated turns (which answer fine) before the grace
    # releases. Substring match, case-insensitive, operator-editable.
    # Includes the ASR garbles the prompts already enumerate ("E band",
    # "eye ban"): a garbled opening beat that misses this match never opens
    # custody, and the realtime model then runs the protected flow itself —
    # observed live 2026-07-31 (asked for the new IBAN, no OTP). Matching in
    # the endpoint is normalized (case/space/hyphen-insensitive), so "iban"
    # also covers "i ban"/"I-ban" and "e band" covers "e-band"/"eband".
    relay_intent_terms: List[str] = field(default_factory=lambda: [
        "iban", "e ban", "e band", "eye ban",
        "آيبان", "ايبان", "آيبن", "اي بان",
    ])
    # Moves IBAN digit capture out of the realtime model's head and into the
    # endpoint. Today the persona forbids calling ask_agent between digit
    # groups during an IBAN change ("that tally is where the digits LIVE
    # between turns"), so the accumulating number exists only in the
    # model's own context — re-generated, and therefore re-riskable, on
    # every pause. Three failure modes were observed live on the same demo
    # stack: the caller-said/model-spoke/on-screen numbers diverged; a
    # collection turn produces no tool call, so a miscount is invisible
    # server-side (measured 8.1-16.4s per turn); and an ASR hallucination
    # ("I'm improv. I do miss improv." mid-Arabic-sentence) can silently
    # corrupt a buffer that lives only in the model's head.
    #
    # ON: opens on the endpoint's OWN signal — a delegated turn's
    # flow_signals reporting "verify_otp_ok" — not on intent-term matching,
    # and from then on every committed caller utterance is folded into a
    # server-held digit buffer (a plugin-supplied collector, voice/live/dictation.py) instead of being
    # left to the model to tally. Deterministic renders on the same stack
    # ran 1.8-2.0s, an order of magnitude faster than a model-generated
    # tally, so this should make the flow faster, not slower.
    #
    # DEFAULT OFF: the model-side path (this persona's own tally-in-context
    # instructions) stays intact and is exactly what runs while this is
    # False — byte-identical to today. Requires the same
    # finalizes_user_transcripts capability relay custody does (the digit
    # buffer advances from the FINAL fragment of the caller's transcript);
    # on a provider that never finalizes one, the endpoint refuses to open
    # it and logs once, the same fail-safe posture as relay_mode.
    server_side_iban_capture: bool = False
    # DUAL ROLE — one knob, two places barge-in confirmation is awaited.
    #
    # (1) Delegated-answer confirm window: how long an `interrupted` may stay
    # UNCONFIRMED before it is treated as a false alarm and the pending
    # delegated answer is spoken after all. Background noise trips
    # speech-start detection mid-response; downgrading the answer to a silent
    # delivery on that signal alone meant a caller who asked, waited through
    # the filler, and then heard nothing. A REAL barge-in confirms itself
    # within a beat — the caller's overriding utterance turns into real
    # transcribed words or a new question (tool_call) — so an interrupt that
    # produces neither within this window was noise.
    #
    # (2) Held-flush safety valve (endpoints/voice_live.py): on a provider
    # that declares supports_barge_in_confirmation, a commit's
    # `playback_flush` is HELD until the caller's transcript says whether
    # those were words or noise — she keeps talking through a noise commit
    # instead of being cut mid-sentence. If no verdict lands within this
    # window (transcription dark/slow) the valve releases the flush anyway:
    # failing toward today's behaviour, never toward stale audio playing over
    # a real reply.
    #
    # 0 is the single rollback lever for BOTH: every `interrupted` flushes
    # and silences immediately, nothing is ever held, and the endpoint runs
    # the exact pre-2026-08 legacy path end-to-end (the same path a provider
    # without supports_barge_in_confirmation — Gemini Live — always takes).
    # One knowing deviation from byte-identical legacy at window 0: the flush
    # frame now routes through the endpoint's shared `_send_flush` helper, so
    # a send failure on that frame is absorbed (logged at debug) instead of
    # propagating out of the pump as it used to.
    # Master switch for interrupting her mid-sentence. ON preserves every
    # existing behaviour. OFF makes a committed utterance WAIT: she finishes
    # the sentence she is speaking, then answers the next thing.
    #
    # Why this exists: six mechanisms in this endpoint can cancel, suppress or
    # re-issue a response (confirmed barge-in, supersede-on-commit, the
    # playback-flush valve, the answer watchdog, relay custody, blank-call
    # recovery). Individually each is correct; together, on a live mic whose
    # recognizer invents words out of room noise, they starve her — a live
    # client call answered a question, then three utterances in five seconds
    # cancelled three replies in a row and the caller heard nothing at all.
    # A scripted demo does not need barge-in, and turning it off removes the
    # mid-sentence cut, the silently-dropped answer and the rapid-utterance
    # stall in one flag, because all three ride the same cancel.
    barge_in_enabled: bool = True
    interrupt_confirm_window_sec: float = 2.0
    # Cut her on SUSTAINED caller speech over audible playback, without
    # waiting for the transcript. On azure_voice_live with azure-speech the
    # caller's words arrive as ONE delta right after the COMMIT, i.e. after
    # they have stopped talking plus `silence_duration_ms`; and a talk-over
    # while she is still audible is almost always `speech_started_idle` (the
    # service finished generating seconds earlier), which carried no
    # pending-interrupt. So the confirmed cut landed ~1s AFTER the caller
    # finished — they talked over her for the whole sentence (live
    # 2026-09-29, one voice tenant: speech_started_idle=6 barge_ins=0
    # confirmed_cuts=6, "interrupting doesn't work at all"). With this > 0, an
    # onset heard over audible playback that is STILL speech this many seconds
    # later (no speech_stopped / commit yet) flushes the client and cancels
    # the response. It does not bump the supersede epoch — the transcript
    # verdict still decides what happens to answers in flight. 0 = off
    # (default: a tenant keeps transcript-confirmed cuts only unless its
    # profile sets this).
    barge_in_sustain_sec: float = 0.0
    # Endpoint-side barge-in from the caller's own audio level
    # (voice/live/onset.py): a run of caller speech at least this long while
    # her audio is still QUEUED at the caller flushes playback. Exists for
    # providers whose `interrupted` stops once generation finishes (Gemini
    # Live generates faster than real time, so most of every sentence plays
    # from the browser buffer, uncuttable). 0 = off (default). The level
    # threshold is dBFS on the echo-cancelled, noise-suppressed mic.
    local_barge_in_sec: float = 0.0
    local_barge_in_dbfs: float = -35.0
    # Post-delivery answer watchdog: a delivered INTERRUPT tool result whose
    # response.create was rejected by a racing response can leave the result
    # item in the conversation with NOTHING speaking it — the provider's own
    # retry declines whenever any commit interleaved, and a NOISE commit
    # (empty transcript) declines it too. Live shape: delivery ok, then 74s
    # of dead air until the caller hangs up (tid_shorthand ~1/3,
    # 2026-08-03). If no assistant audio starts within this window and the
    # caller has not superseded the answer (interrupt_epoch unchanged), the
    # endpoint re-delivers it ONCE as a say-exactly text turn — the same
    # cancel-then-text-turn discipline every relay answer already ships
    # with. 4s, not 2: a slow first audio token after a tool result is ~1s;
    # doubling twice keeps false fires (a benign cancel + re-say of an
    # answer that was just starting) rare. 0 disables — the rollback lever.
    #
    # 4.0 STAYS after the 2026-08-05 duplicate-answer incident, deliberately:
    # the number was never the bug. Delivery -> first audio on an idle floor
    # is 0.21s / 0.33s (live-probed against gpt-realtime the same day), so
    # 4s was already ~12x the median; what fired the watchdog was an answer
    # queued BEHIND a holding line the cancel had failed to stop, which no
    # threshold distinguishes from silence. The window is now counted only
    # while the provider reports nothing in flight (`response_in_flight`),
    # which makes 4s mean "4 seconds of provable silence with an idle floor"
    # — and raising it instead would only have delayed the genuine mute-race
    # rescue this exists for.
    answer_watchdog_sec: float = 4.0
    # Announce-then-stall recovery: the native model sometimes completes its
    # reply right after the looking-it-up line ("one moment", «ثانية وحدة»)
    # WITHOUT emitting the ask_agent call — clean wire, no cancels, and in
    # server-VAD mode nothing ever prompts it again, so the caller sits in
    # silence forever (three live client sessions in a row, 2026-08-04; a
    # persona rule making the announce and the call one reply did not hold).
    # An announce phrase in a completed reply IS the tripwire — no
    # legitimate reply declares a lookup and ends with no call behind it.
    # When that happens while the caller's committed question is still
    # unanswered, the endpoint waits this grace (letting a tool call that
    # is merely in transit land and stand the timer down), then dispatches
    # the question itself via the recovery path. 0 disables — the rollback
    # lever.
    stall_recovery_sec: float = 2.0
    # ── Residual tier: a stall with NO recognised announce phrase ──────────
    # The announce list plus a prompt pinned to it (see _ANNOUNCE_TOKENS)
    # covers the model that declares a lookup. It cannot cover the model that
    # simply stops — the persona is allowed to "just answer", and a reply that
    # ends a turn with the caller's question unconsumed is indistinguishable,
    # by its own text, from a goodbye or a piece of chitchat. Arming on all of
    # them was measured and is wrong: it recovers "no, that's all thanks" and
    # speaks over the persona's farewell on every call that ends politely.
    #
    # So this tier keys on the CALLER instead of on the model. If the caller's
    # committed utterance mentions something only the agent can answer — a
    # figure, a device, a branch, an account — then a turn that ended without
    # a tool call did not answer it, whatever the model said. A goodbye and
    # "how are you" contain no such term and never arm.
    #
    # Tenant-configured, like relay_intent_terms / backchannel_tokens /
    # phrase_list — the engine is unbranded and cannot guess a tenant's
    # vocabulary. EMPTY BY DEFAULT, so this tier is strictly opt-in and no
    # existing deployment changes behaviour by upgrading.
    stall_recovery_topic_terms: List[str] = field(default_factory=list)
    # Grace for that tier. Longer than the announced one: without a
    # declaration of intent there is less certainty, so leave room for a slow
    # tool call and for a model that is legitimately still working. A reply
    # ending in a question never arms at all — that is the persona handing the
    # turn back (a clarifying question, "want the breakdown?"), not a stall.
    # 0 disables this tier alone; the announced tier is unaffected.
    stall_recovery_unannounced_sec: float = 6.0
    # ── Silent tier: the model answers the caller with NOTHING ─────────────
    # Both tiers above hang off `turn_end` and read the reply text. That
    # cannot catch a model which never speaks and never ends a turn: measured
    # 2026-08-05 on a cold session — `sec=85 turns=1 user_turns=1
    # tool_calls=0`, the caller asked for last month's revenue and got 85s of
    # silence, with no recovery armed because there was no reply to inspect.
    #
    # So this tier hangs off the CALLER's commit instead of any model event,
    # which is the only trigger that cannot be blinded by the model doing
    # nothing. It stands down when: the commit gets consumed (a tool call
    # landed), the epoch moves (the caller said something newer), one of the
    # turn_end tiers already armed for it, or the model produced ANY audio
    # after the commit (it replied — that is chitchat, a clarifying question
    # or a closure, all of which the turn_end tiers judge properly and this
    # tier must not second-guess).
    #
    # Gated on `stall_recovery_topic_terms` like the unannounced tier, so it
    # is opt-in and a goodbye never trips it. Generous by default: it is the
    # last line, it must never preempt a legitimately slow first token, and
    # nothing else will catch the caller if it fires late. 0 disables.
    stall_recovery_silent_sec: float = 12.0
    # Tier 1b (user voice test 2026-10-01): a persona turn with NO ask_agent
    # that claims a result («الحين تشوف … قدامك», "here are your invoices",
    # "refresh the page"), or that answers a caller saying the screen is
    # empty («مش شايف إشي على الشاشة»), is a stall. OPT-IN per tenant: its
    # phrases are merchant-app phrases, and on a tenant whose persona talks
    # about terminals and receipts they would read device guidance as a
    # claim. A tenant's profile turns it on. Farewells never arm it.
    persona_claim_recovery_enabled: bool = False
    # ── Session/abuse ceilings, previously endpoint constants ──────────────
    # All operator-tunable per tenant (voice_config.live) / channel, because a
    # kiosk deployment and a call-center deployment want very different
    # ceilings — and "edit a Python constant and redeploy" is exactly the
    # workflow the admin portal exists to remove.
    #
    # Hard ceiling on one session. Realtime providers cap session length
    # anyway (Azure at ~30 min); this bounds OUR exposure to a socket nobody
    # is listening to.
    max_session_sec: float = 900.0
    # Charged at handshake so an abandoned socket still costs something; the
    # periodic charge below and the teardown charge then book the rest of
    # the session's real duration on top of this advance.
    handshake_charge_sec: float = 30.0
    # How often a RUNNING session charges its elapsed-but-uncharged seconds
    # to the persisted daily tally. The handshake advance and the teardown
    # charge alone left an in-flight session contributing only its 30s
    # advance however long it had actually run: with max_session_sec=900 and
    # 8 concurrent per IP, ~2 hours of live audio could accrue against a
    # 1-hour daily cap before any of it was recorded — and a replica killed
    # mid-session (deploy/OOM) booked 30s for a 15-minute call, silently.
    # Charging the delta each interval keeps the tally within one interval
    # of the truth, so the NEXT handshake's check_audio_budget actually
    # binds. <= 0 disables the periodic charge — the rollback lever back to
    # the legacy two-charge (handshake + teardown) scheme. Floored at the
    # use site (endpoints/voice_live.py) so a tiny operator value cannot
    # hammer the DB.
    budget_charge_interval_sec: float = 60.0
    # Per-identity concurrency cap (verified sub, else peer IP). Best-effort
    # and per-replica — see voice/live/budget.py for why the daily budget is
    # persisted and this deliberately isn't.
    max_concurrent_sessions: int = 3
    # The ceiling that actually bounds abuse: only a VERIFIED bearer narrows
    # the identity key, so an anonymous caller could otherwise mint fresh
    # identities per connection. Looser than the per-identity cap so a store's
    # staff sharing one NAT are not punished for it.
    max_concurrent_per_ip: int = 8
    # Relay-custody ceilings so custody can never wedge a call: a flow real
    # callers finish in 4-5 beats gets 8 turns, and 180s covers the slowest
    # OTP fumbling observed live with margin. `relay_quiet_grace_turns` is how
    # many consecutive flow-quiet delegated turns close custody (the read-back
    # beat legitimately calls no tool, so one quiet turn is normal).
    relay_max_turns: int = 8
    relay_max_sec: float = 180.0
    relay_quiet_grace_turns: int = 2
    # EXTRA acknowledgement tokens merged into the built-in backchannel set
    # (endpoints/voice_live.py's _BACKCHANNEL_TOKENS): whole-utterance nods
    # that must NOT supersede an in-flight delegated answer. Per-tenant
    # because the built-in set is EN+AR — a tenant serving another language
    # needs its nods listed here, not a code change. Same normalisation as
    # the built-ins (case/space/tashkeel-insensitive).
    backchannel_tokens: List[str] = field(default_factory=list)
    # Pre-fetched-context mode (outbound simulations): the agent fetched the
    # whole case BEFORE dialling and it lives in the persona prompt, so the
    # realtime model answers on its own and calls instant local ACTION tools
    # directly — no `ask_agent`, no brain round trip. Operator-set flag; the
    # endpoint then fills `direct_tools` from the agent and stands down every
    # ask_agent-shaped recovery (see endpoints/voice_live._apply_direct_overrides).
    prefetched_context: bool = False
    # Per-CALL, never from config: function declarations ({name, description,
    # parameters} — plain JSON schema) the provider declares INSTEAD of
    # ask_agent. Empty = the normal delegated session.
    direct_tools: List[Dict[str, Any]] = field(default_factory=list)
    # Per-CALL, never from config: a pre-fetched-context agent whose
    # `direct_voice_tools()` is EMPTY (the prompt-only inbound demo agents)
    # declares NO tools at all. Without this flag an empty `direct_tools`
    # would read as "the normal delegated session" and declare ask_agent.
    tools_disabled: bool = False
    # Filled by hydrate_live_model() from the agent_models row.
    provider: str = ""
    model_string: str = ""
    extra_config: Dict[str, Any] = field(default_factory=dict)


# Numeric live fields and their dataclass-default fallbacks, coerced after
# the overrides land. `voice_config.live` is RAW OPERATOR JSON — the portal
# form float()s its inputs, but `update_profile(voice_config=...)` over the
# admin MCP (and a direct DB edit) does not, so a string like "eight" or a
# null can reach these. Several are read on the hot path — the handshake's
# concurrency check and the relay ceilings inside the provider pump loop —
# where a TypeError/ValueError would kill the whole CALL, not just one turn.
# Coerce once here, at the config boundary, and fall back to the default on
# anything unparseable.
#
# The third element is a LOWER BOUND, applied after the cast, only where a
# zero/negative value has no legitimate meaning and the use site reads the
# value bare:
#   max_concurrent_sessions / max_concurrent_per_ip — the handshake check is
#     a bare `active >= cap` (endpoints/voice_live.py), so a typo'd -1 (or 0)
#     makes the FIRST connection already over-limit and every call for the
#     tenant dies CLOSE_RATE_LIMITED. Zero is not the voice off-switch
#     (`enabled` is), so a floor of 1 cannot change a legitimate config.
#   relay_max_turns / relay_max_sec — the custody ceilings are bare `>=`/`>`
#     checks, so <= 0 closes custody the moment it opens and the protected
#     flow runs in the realtime model — exactly the 2026-07-31 incident mode,
#     silently. `relay_mode: false` is the documented custody off-switch; the
#     ceilings are not one. Floored at 1 turn / 1 second.
# The "0 disables" timer levers (progress_after_sec, answer_watchdog_sec,
# the stall tiers, interrupt_confirm_window_sec, greeting_barge_in_guard_sec)
# and the fields already floored at their use site (handshake_charge_sec,
# max_session_sec, relay_quiet_grace_turns) deliberately get no floor here.
_NUMERIC_LIVE_FIELDS = (
    ("delegate_timeout_sec", float, None), ("progress_after_sec", float, None),
    # No floor: 0 is the documented rollback lever back to the pre-fix
    # legacy behaviour on the greeting turn — see the field's own comment.
    ("greeting_barge_in_guard_sec", float, None),
    # No floor: 0 is off.
    ("greeting_input_mute_sec", float, None),
    ("interrupt_confirm_window_sec", float, None),
    # No floor: 0 is off (transcript-confirmed cuts only).
    ("barge_in_sustain_sec", float, None),
    ("local_barge_in_sec", float, None),
    ("local_barge_in_dbfs", float, None),
    ("answer_watchdog_sec", float, None),
    ("stall_recovery_sec", float, None),
    ("stall_recovery_unannounced_sec", float, None),
    ("stall_recovery_silent_sec", float, None),
    ("max_session_sec", float, None),
    ("handshake_charge_sec", float, None),
    # No floor here on purpose: `<= 0` is the documented rollback lever back
    # to the legacy handshake+teardown scheme, and a positive value is floored
    # at the use site (endpoints/voice_live.py) so it cannot hammer the DB.
    ("budget_charge_interval_sec", float, None),
    ("max_concurrent_sessions", int, 1),
    ("max_concurrent_per_ip", int, 1), ("relay_max_turns", int, 1),
    ("relay_max_sec", float, 1.0), ("relay_quiet_grace_turns", int, None),
    # Floor 1: a 0 here would retrieve nothing while leaving the feature
    # reporting as "on", which is exactly the silent-when-wrong shape.
    ("kb_prefetch_match_count", int, 1),
)

# Boolean live fields, coerced at the same boundary and for the same reason:
# a stringy "false" written over the admin MCP (or a direct DB edit) is a
# NON-EMPTY STRING — truthy — so every `if not cfg.<flag>` gate in
# endpoints/voice_live.py would read the operator's explicit OFF as ON. That
# silently defeats exactly the flags that exist as rollback levers:
# `barge_in_enabled` (the live-incident master switch, #38), `relay_mode`
# (protected-flow custody) and `enabled` itself. coerce_bool maps the usual
# string spellings to their meaning and falls back to the field's default on
# garbage, same posture as the numeric pass above.
_BOOL_LIVE_FIELDS = (
    "enabled", "stream_sentences", "relay_mode",
    "barge_in_enabled", "text_only",
    "kb_prefetch_enabled", "deterministic_render_enabled",
    "progress_on_dispatch", "server_side_iban_capture",
    "persona_claim_recovery_enabled", "prefetched_context",
)


def _coerce_numeric_fields(cfg: LiveVoiceConfig) -> None:
    defaults = LiveVoiceConfig()
    for name, caster, floor in _NUMERIC_LIVE_FIELDS:
        try:
            # float() first for the int fields too: an operator writing 8.0
            # (perfectly good JSON) must not fall back to the default.
            value = caster(float(getattr(cfg, name)))
        except (TypeError, ValueError):
            value = getattr(defaults, name)
        if floor is not None and value < floor:
            value = floor
        setattr(cfg, name, value)


def _coerce_bool_fields(cfg: LiveVoiceConfig) -> None:
    defaults = LiveVoiceConfig()
    for name in _BOOL_LIVE_FIELDS:
        setattr(cfg, name, coerce_bool(getattr(cfg, name), getattr(defaults, name)))


def _voice_block(profile_voice: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The flat voice settings block.

    Post-unification (2026-08-17) `agent_profiles.voice_config` IS the settings
    block. A row still carrying the old `{"live": {...}}` shape — an unmigrated
    branch DB, a hand edit, a snapshot restored from before the migration —
    resolves from the nested block ALONE and warns.

    Resolving from the nested block alone, rather than merging the top level in,
    is deliberate: the top-level keys were the TURN-BASED config and this
    resolver never read them. Merging would newly apply the turn-based
    `voice_id` (an Azure neural voice name) and its `enabled: true` default to a
    duplex session, changing behaviour on exactly the rows the shim exists to
    leave alone.

    Remove this shim one release after the unify_voice_config migration.
    """
    block = profile_voice or {}
    nested = block.get("live")
    if isinstance(nested, dict):
        logger.warning(
            "voice: voice_config still carries a legacy 'live' sub-block — "
            "resolving from it and ignoring top-level turn-based keys. Apply "
            "the unify_voice_config migration to this database."
        )
        return nested
    return block


# The pre-retirement turn-based key set — `voice/config.py::_FIELDS` as it stood
# before the `/voice/*` routes were retired, and the exact list
# supabase/migrations/20260817120000_unify_voice_config.sql archives in
# statements 2b / 4b. Four of the nine COLLIDE with LiveVoiceConfig field names
# (enabled, voice_id, language, speaking_rate); the other five exist nowhere in
# `_VOICE_FIELDS`, so their presence is proof a `live`-less block is turn-based.
_RETIRED_TURN_BASED_FIELDS = frozenset((
    "enabled", "stt_provider", "stt_model", "tts_provider", "tts_model",
    "voice_id", "language", "sample_rate", "speaking_rate",
))
_TURN_BASED_ONLY_FIELDS = frozenset((
    "stt_provider", "stt_model", "tts_provider", "tts_model", "sample_rate",
))


def _is_retired_turn_based_block(block: Dict[str, Any]) -> bool:
    """True for a flat block that is provably the PRE-DUPLEX turn-based config.

    Mirrors statement 2b's gate exactly: at least one turn-based-ONLY key (the
    proof), and no key outside the retired nine (any other key — `model_id`,
    `persona_prompt_slug`, the duplex-only `filler_enabled`, an already-written
    `_retired_turn_based` — proves the block is duplex and must be left alone).

    A block holding ONLY the four colliding names is NOT matched: it has no
    discriminator either way, and it is also what a promoted `{"live": {...}}`
    row with no top-level keys legitimately becomes. See the migration's 2b
    comment for the full argument.
    """
    keys = set(block)
    return (
        bool(keys & _TURN_BASED_ONLY_FIELDS)
        and keys <= _RETIRED_TURN_BASED_FIELDS
    )


def promote_legacy_voice_config(
    profile_voice: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """The WRITER-side counterpart of `_voice_block`: a flat block, always.

    `_voice_block` resolves a legacy `{"live": {...}}` row without changing it
    — right for a read. A WRITER cannot leave it alone: flat keys written
    beside a surviving `live` key are invisible, because `_voice_block`
    resolves the nested block ALONE. So every writer promotes first and the
    save repairs the row instead of compounding it.

    Promotion mirrors supabase/migrations/20260817120000_unify_voice_config.sql
    exactly: the duplex keys BECOME the block, `filler_enabled` is dropped
    (declared, coerced, and read by nothing), and the pre-unification top-level
    keys are stashed under `_retired_turn_based` rather than merged up —
    merging would newly apply a turn-based `voice_id` (an Azure neural voice
    name) and its `enabled: true` to a duplex session, the exact behaviour
    change `_voice_block` refuses to make.

    A row that is already flat comes back as a plain copy MINUS
    `filler_enabled` — statements 2 and 4 of that migration strip the dead key
    from already-flat rows too, so returning it made the "mirrors the migration
    exactly" claim above false and let a writer re-persist a key the migration
    deletes. A NESTED row keeps a top-level `filler_enabled` inside
    `_retired_turn_based`, because statement 1 stashes `voice_config - 'live'`
    whole — the archived turn-based block is not de-duplicated, and mirroring
    means not "improving" that either. Idempotent under repeated calls (the flat
    result has no `live` and no `filler_enabled`), so calling this on every write
    is safe. Remove it with the read shim.

    A block with NO `live` key that is provably the pre-duplex turn-based config
    is archived WHOLE (`_is_retired_turn_based_block`), mirroring statements
    2b / 4b. Without that, `admin/db_snapshot.py restore` — which re-imports rows
    the migration has already run past — would land such a row flat, and the
    reader would resolve `enabled: true` with no `model_id`: duplex-on, then
    LookupError, then CLOSE_FORBIDDEN on every connect.
    """
    block = dict(profile_voice or {})
    nested = block.get("live")
    if not isinstance(nested, dict):
        # Checked BEFORE the `filler_enabled` pop, because the migration's gate
        # sees the raw row: a block carrying that duplex-only key is duplex, and
        # statement 2 (not 2b) is what strips the dead key from it.
        if _is_retired_turn_based_block(block):
            return {"_retired_turn_based": block}
        block.pop("filler_enabled", None)
        return block
    flat = {k: v for k, v in nested.items() if k != "filler_enabled"}
    retired = {k: v for k, v in block.items() if k != "live"}
    if retired:
        flat["_retired_turn_based"] = retired
    return flat


def resolve_live_config(
    *,
    profile_voice: Optional[Dict[str, Any]],
    channel: Optional[Dict[str, Any]] = None,
) -> LiveVoiceConfig:
    """Resolve duplex voice settings from the profile's flat voice_config.

    `channel` is accepted and IGNORED. It used to carry
    `agent_channels.config.voice.live` as the HIGHEST-precedence layer — which
    no seeder wrote, no screen labelled, and (verified 2026-08-17 against the
    local, WAJ-prod and both client-demo VM databases: 6 channels each, zero
    matches) no row anywhere used. The parameter stays so the ~20 call sites and
    tests that pass it keep working; drop it with the `live` shim.
    """
    cfg = LiveVoiceConfig()
    apply_overrides(cfg, _voice_block(profile_voice), _VOICE_FIELDS)
    _coerce_numeric_fields(cfg)
    _coerce_bool_fields(cfg)
    return cfg


def hydrate_live_model(
    cfg: LiveVoiceConfig,
    *,
    client: Any,
    environment: Optional[str] = None,
    model_row: Optional[Dict[str, Any]] = None,
) -> LiveVoiceConfig:
    """Fill `provider` / `model_string` / `extra_config` from the model row.

    `model_row`, when given, is the row FROZEN INTO THE PROFILE SNAPSHOT at
    promote (`runtime/compiler.py`'s `snapshot["voice_model"]`, mirroring the
    existing `snapshot["model_main"]`). Preferring it is what brings the
    realtime model under promote/rollback: `model_id` was already frozen, but
    the row it named was read live, so `endpoint`, `voice_type`,
    `turn_detection`, `api_version` and `auth_env_var` — the settings deciding
    whether a call sounds Saudi, whether barge-in works, and which region the
    audio crosses — bypassed governance entirely.

    Falls back to a live read in dev/staging, where the profile is the live row
    and there is no snapshot. Raises LookupError when no model is configured or
    the row is gone — a tenant with duplex enabled but no realtime model is a
    misconfiguration we surface loudly, not silently default.
    """
    from core.agent_profile import AgentProfileLoader

    if model_row:
        row: Optional[Dict[str, Any]] = model_row
    else:
        if not cfg.model_id:
            raise LookupError(
                "voice_config.model_id is not set — duplex voice needs an "
                "agent_models row naming the realtime model."
            )
        row = AgentProfileLoader.fetch_model(client, cfg.model_id, environment)
    if not row:
        raise LookupError(f"agent_models row '{cfg.model_id}' not found")
    cfg.provider = (row.get("provider") or "").lower()
    cfg.model_string = row.get("model_string") or ""
    cfg.extra_config = row.get("extra_config") or {}
    return cfg


# Built-in holding lines, per language. `progress_spoken`'s dataclass default
# is the English one, so a tenant that never touched the field used to hear an
# English "still working on that" mid-Arabic call — the exact failure the
# per-language map above was added to prevent, still reachable because the
# FLAT field had no per-language fallback of its own the way `failure_spoken`
# (delegate.py) and `on_screen_spoken` (speakable.py) both do.
#
# Deliberately worded to avoid every phrase in `common.announce.ANNOUNCE_TOKENS`:
# a holding line that reads as a lookup announcement can arm the
# announce-then-stall tripwire against the endpoint's own filler.
_PROGRESS_SPOKEN = {
    "ar": "ما زلت أشتغل على طلبك.",
}


def resolve_progress_spoken_variants(cfg: LiveVoiceConfig) -> List[str]:
    """ALL holding-line variants for this session's language, as a list.

    `progress_spoken` and each `progress_spoken_by_language` value accept a
    single string OR a list of strings: a caller who hears the identical
    holding line on every lookup clocks it as fake, so operators can store
    several and the session varies them. Same primary-subtag keying and same
    raw-operator-JSON tolerance as resolve_session_voice; blank/empty values
    resolve to [] (holding line disabled).

    Precedence, and the middle rung is the one that needs stating:

      1. `progress_spoken_by_language[primary]` — the operator was explicit
         about this language.
      2. `progress_spoken`, IF the operator set one. Their single custom line
         must win over any built-in: a tenant who wrote one line and no map
         gets that line, in every language, which is what they asked for.
      3. the built-in for this language (`_PROGRESS_SPOKEN`), when
         `progress_spoken` is still exactly the shipped default — i.e. nobody
         has expressed an opinion, so an English default must not be spoken
         into an Arabic call.
      4. the shipped English default.
    """
    primary = (cfg.language or "").split("-")[0].strip().lower()
    value: Any = None
    if primary:
        mapping = cfg.progress_spoken_by_language
        value = mapping.get(primary) if isinstance(mapping, dict) else None
    if not value:
        value = cfg.progress_spoken
        if primary and value == LiveVoiceConfig.progress_spoken:
            value = _PROGRESS_SPOKEN.get(primary, value)
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    text = str(value or "")
    return [text] if text.strip() else []


def resolve_progress_spoken(cfg: LiveVoiceConfig) -> str:
    """ONE holding line for this session's language, varied across calls.

    random.choice over the resolved variants — per-call variety needs no
    persistent rotation state, and a single-entry list (the common case)
    always returns that entry, exactly the old behaviour.
    """
    import random

    variants = resolve_progress_spoken_variants(cfg)
    if not variants:
        return ""
    return random.choice(variants)


def _by_language(cfg: LiveVoiceConfig, mapping_attr: str, fallback_attr: str) -> str:
    """Operator wording for this session's language, else the flat value.

    Same primary-subtag keying and same raw-operator-JSON tolerance as
    `resolve_progress_spoken`, which this generalises.
    """
    primary = (cfg.language or "").split("-")[0].strip().lower()
    if primary:
        mapping = getattr(cfg, mapping_attr, None)
        mapped = mapping.get(primary) if isinstance(mapping, dict) else None
        if mapped:
            return str(mapped)
    return getattr(cfg, fallback_attr, "") or ""


def resolve_failure_spoken(cfg: LiveVoiceConfig) -> str:
    """Operator's failure line for this session, or "" to use the built-in."""
    return _by_language(cfg, "failure_spoken_by_language", "failure_spoken")


def resolve_on_screen_spoken(cfg: LiveVoiceConfig) -> str:
    """Operator's on-screen pointer for this session, or "" for the built-in."""
    return _by_language(cfg, "on_screen_spoken_by_language", "on_screen_spoken")


# The built-in say-exactly instruction, per language. Same shape and same
# reason as delegate.py's `_FAILURE_SPOKEN` and speakable.py's `_ON_SCREEN`:
# this is spoken-adjacent text the voice layer authors itself, so it needs a
# sane default in every language the tenants actually run, not an English
# literal at the use site.
_SAY_EXACTLY_INSTRUCTION = {
    "en": "Say exactly this to the caller, and nothing else: {spoken}",
    "ar": "قل هذا للمتصل حرفياً، ولا تقل شيئاً غيره: {spoken}",
}


def _format_or_none(template: str, **values: Any) -> Optional[str]:
    """`template.format(**values)`, or None if the template can't take it.

    Every one of these templates is RAW OPERATOR JSON reaching a `.format()`
    call on the answer-delivery path. A stray brace ("say this {now}") raises
    KeyError, a lone "{" raises ValueError, and either one would take down the
    delivery of an answer the agent had already computed — the caller hears
    nothing at all, for a typo in a text box. Returning None lets the caller
    fall back to the built-in instead.
    """
    try:
        out = template.format(**values)
    except (KeyError, IndexError, ValueError, TypeError):
        return None
    # A template that never mentioned the placeholder formats cleanly and
    # silently DROPS the answer, which is the same dead air by a quieter
    # route. Require the substitution to have actually happened.
    return out if any(str(v) in out for v in values.values() if str(v)) else None


def resolve_say_exactly_instruction(cfg: LiveVoiceConfig, spoken: str) -> str:
    """The endpoint's answer-delivery turn, with `spoken` substituted in.

    Never blank and never lossy: an operator template that cannot take the
    answer (no `{spoken}`, an unbalanced brace, an unknown field) falls back
    to the built-in for this session's language, and an unmapped language
    falls back to English — the same primary-subtag keying as every other
    resolver here. The one thing this must never do is return a string that
    does not contain the answer.
    """
    template = _by_language(
        cfg, "say_exactly_instruction_by_language", "say_exactly_instruction"
    )
    if template:
        out = _format_or_none(template, spoken=spoken)
        if out:
            return out
        logger.warning(
            "voice: say_exactly_instruction could not take the answer (no "
            "{spoken} placeholder, or a bad format spec) — falling back to "
            "the built-in wording"
        )
    primary = (cfg.language or "").split("-")[0].strip().lower()
    builtin = _SAY_EXACTLY_INSTRUCTION.get(
        primary, _SAY_EXACTLY_INSTRUCTION["en"]
    )
    return builtin.format(spoken=spoken)



def resolve_session_voice(cfg: LiveVoiceConfig) -> str:
    """Voice for this session: the language-mapped voice if the session's
    resolved language has one, else `voice_id`. A full tag ("ar-EG") is
    looked up first, then the PRIMARY subtag ("ar-SA" -> "ar"), so one entry
    covers every regional variant and a regional entry can name an accent."""
    tag = (cfg.language or "").strip().replace("_", "-")
    primary = tag.split("-")[0].strip().lower()
    if primary:
        # Raw operator JSON: anything non-dict (a string, a list) falls back
        # to voice_id instead of raising AttributeError at session build.
        mapping = cfg.voice_by_language
        mapped = None
        if isinstance(mapping, dict):
            parts = tag.split("-")
            full = "-".join([parts[0].lower()] + [p.upper() for p in parts[1:]]) if len(parts) > 1 else ""
            mapped = (mapping.get(full) if full else None) or mapping.get(primary)
        if mapped:
            return mapped
    return cfg.voice_id
