"""Gemini Live — the first RealtimeSession implementation.

Everything Google-specific is confined to this file. `Behavior.NON_BLOCKING`
and `FunctionResponseScheduling` are used here as an ENHANCEMENT: the portable
filler mechanism is announce-then-call in the persona prompt, so an
implementation without non-blocking calls (Azure) behaves the same.

The model gets exactly one tool. It does not answer data questions itself.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

try:
    # Transitive dep of google-genai's Live transport (websockets>=13,<17) --
    # not declared in our requirements on purpose (no-new-deps rule). If
    # google-genai ever swaps transports this import must not take down
    # provider registration at boot: fall back to a sentinel that matches
    # nothing, so the narrow hang-up-race catch below simply never fires and
    # such failures surface through the generic handler instead.
    from websockets.exceptions import ConnectionClosed
except ImportError:  # pragma: no cover - environment-dependent
    class ConnectionClosed(Exception):
        pass

from google import genai
from google.genai import types as genai_types

from voice.live.base import (
    ASK_AGENT_TOOL_SPEC,
    DEFAULT_ASK_AGENT_SUBJECT,
    AudioFormat,
    RealtimeEvent,
    RealtimeProviderError,
    RealtimeSession,
    ToolResultMode,
    ToolSpec,
    ask_agent_subject,
)
from voice.live.config import LiveVoiceConfig
from voice.live.factory import register_realtime_provider

logger = logging.getLogger(__name__)

# Gemini Live's native rates. Nothing outside this module may assume them —
# they reach the browser through the `ready` frame.
_INPUT_RATE = 16000
_OUTPUT_RATE = 24000

# The name/argument are the shared cross-provider contract from
# voice/live/base.py (ASK_AGENT_TOOL_SPEC) -- built FROM it, not
# independently hardcoded, so the two can't drift apart. Everything else
# here (description, JSON-schema shape) is Gemini-specific SDK plumbing and
# stays local to this file. `{subject}` is `ask_agent_subject()`: who
# handles the request, a deployment's own words (ask_agent_declaration()).
_ASK_AGENT_DESCRIPTION = (
    "Ask {subject} to handle the caller's request. Use this for "
    "ANY question about data, products, bookings, policies or figures — "
    "AND for any account action (an IBAN change, a verification code, a "
    "maintenance ticket): call it the INSTANT such a request lands, "
    "before asking the caller for anything yourself. The agent drives "
    "every step — it sends and checks verification codes, collects the "
    "new details, performs the update; you only relay. Relay each thing "
    "the caller then gives you (a code they read, digits they dictate, a "
    "yes) as its own call, verbatim. Announce that you are looking it up "
    "BEFORE calling, then read the returned `spoken` text back exactly "
    "as written — never recompute, re-round or re-order a figure."
)

ASK_AGENT_DECLARATION: Dict[str, Any] = {
    "name": ASK_AGENT_TOOL_SPEC.name,
    "description": _ASK_AGENT_DESCRIPTION.format(subject=DEFAULT_ASK_AGENT_SUBJECT),
    "parameters": {
        "type": "object",
        "properties": {
            ASK_AGENT_TOOL_SPEC.query_arg: {
                "type": "string",
                "description": "The caller's request or reply, in their own words.",
            }
        },
        "required": [ASK_AGENT_TOOL_SPEC.query_arg],
    },
}


def ask_agent_declaration() -> Dict[str, Any]:
    """The declaration as sent: ASK_AGENT_DECLARATION in this deployment's
    wording (voice/live/base.py ask_agent_subject())."""
    return dict(ASK_AGENT_DECLARATION,
                description=_ASK_AGENT_DESCRIPTION.format(subject=ask_agent_subject()))


_SCHEDULING = {
    ToolResultMode.INTERRUPT: genai_types.FunctionResponseScheduling.INTERRUPT,
    ToolResultMode.WHEN_IDLE: genai_types.FunctionResponseScheduling.WHEN_IDLE,
    ToolResultMode.SILENT: genai_types.FunctionResponseScheduling.SILENT,
}


def _scheduling_for(mode: ToolResultMode):
    # ToolResultMode subclasses str, so a plain string like "interrupt" is
    # both `==` and same-hash as ToolResultMode.INTERRUPT -- a bare
    # `_SCHEDULING.get(mode)` would silently accept it via dict lookup
    # without ever checking it's a real enum member. Require the real type.
    if not isinstance(mode, ToolResultMode):
        raise TypeError(f"mode must be a ToolResultMode, got {type(mode).__name__}")
    scheduling = _SCHEDULING.get(mode)
    if scheduling is None:
        raise TypeError(f"unsupported ToolResultMode: {mode!r}")
    return scheduling


# --- End-of-speech detection ------------------------------------------------
#
# Measured, not guessed. Live A/B against `gemini-3.1-flash-live-preview` from
# the demo VM (manual_tests/probe_gemini_turn_detection.py, 2026-08-19): one
# synthesized Arabic utterance, its trailing TTS silence TRIMMED so that "end
# of speech" is the real last sample (leaving it in adds ~900ms to every
# reading and hides the whole effect), streamed in real time at 20ms/frame,
# then real-time silence until the model answers. 5 runs per value,
# end-of-speech -> first audio frame back:
#
#   silence_duration_ms   eos -> first audio       one 500ms mid-   one 800ms mid-
#                         median (range)           sentence pause   sentence pause
#   400                   1085ms (1038..1162)      SPLIT (1/2)      SPLIT (2/2)
#   600                   1114ms (1084..1272)      held  (2/2)      SPLIT (2/2)
#   700                   1264ms (1230..1315)      --               SPLIT (3/3)
#   800                   1315ms (1301..1373)      --               SPLIT (3/3)
#   900                   1467ms (1436..1470)      --               held  (3/3)
#   provider default      1392ms (1380..1421)      held  (2/2)      held  (2/2)
#
# Three things follow, and the third is why there is a default here at all
# rather than "leave it to the provider":
#
#  1. The provider default behaves like ~850-900ms and is ~280ms slower than
#     600 on EVERY turn, paid before any work starts. Same order as the
#     950 -> 650 Azure finding.
#  2. 600 is the KNEE, not the fastest number. Below it the curve flattens
#     (400 buys a further 29ms — the ~1.08s floor is the model's own
#     time-to-first-audio, not the window) while a 500ms think-pause starts
#     splitting. Above it, 700 and 800 pay 150-200ms more for exactly the
#     same 800ms-pause failure as 600 — they are strictly dominated; only
#     900+/default buys the 800ms pause back, at ~280-350ms per turn.
#  3. Unlike Azure — where a longer window bought NO extra pause tolerance
#     (see azure_voice_live._session_payload) — on Gemini it genuinely does.
#     So this is a real trade and the number is a choice, which is exactly
#     what makes it config rather than a constant.
#
# Everything is operator-overridable per model row via
# `extra_config.turn_detection` -- deliberately the SAME key the Azure
# provider reads, so an operator learns one name. The services take different
# FIELDS though, so `_activity_detection` maps only the ones Gemini honours
# and drops the rest: handing Voice Live's block (`type`, `threshold`,
# `speech_duration_ms`, ...) to this SDK raises on the unknown kwarg and takes
# the whole call down, which is not an acceptable failure for a copy-pasted
# config row.
_DEFAULT_TURN_DETECTION: Dict[str, Any] = {
    "silence_duration_ms": 600,
}

# Fields of `types.AutomaticActivityDetection` we accept from operator config.
_AAD_INT_FIELDS = ("silence_duration_ms", "prefix_padding_ms")
_AAD_BOOL_FIELDS = ("disabled",)
_SENSITIVITY_FIELDS = {
    "start_of_speech_sensitivity": genai_types.StartSensitivity,
    "end_of_speech_sensitivity": genai_types.EndSensitivity,
}
# Siblings inside the same operator block, but they live one level up on
# RealtimeInputConfig rather than on AutomaticActivityDetection.
_REALTIME_INPUT_FIELDS = {
    "activity_handling": genai_types.ActivityHandling,
    "turn_coverage": genai_types.TurnCoverage,
}


def _coerce_enum(enum_cls: Any, value: Any, key: str) -> Optional[Any]:
    """Accept a real enum member, a bare word ("high"), or a full name.

    Operator config is raw JSON out of the admin portal, so "high" and
    "END_SENSITIVITY_HIGH" both have to work; anything unrecognised is
    dropped with a warning rather than raised, because a typo in one tuning
    key must not be able to take a live call down.
    """
    if isinstance(value, enum_cls):
        return value
    name = str(value).strip().upper()
    for member in enum_cls:
        if name in (member.name, member.value, member.name.rsplit("_", 1)[-1]):
            return member
    logger.warning(
        "gemini live: turn_detection.%s=%r is not a valid %s — ignoring it",
        key, value, enum_cls.__name__,
    )
    return None


def _activity_detection(cfg: LiveVoiceConfig) -> Optional[Any]:
    """Build `RealtimeInputConfig` from `extra_config.turn_detection`.

    Override semantics match the Azure provider's, including the one that is
    easy to miss: an explicit `null` REMOVES a baked-in default (so an
    operator can hand end-of-speech detection back to the provider without a
    redeploy), whereas an absent key keeps it.
    """
    override = cfg.extra_config.get("turn_detection")
    merged: Dict[str, Any] = dict(_DEFAULT_TURN_DETECTION)
    if isinstance(override, dict):
        merged.update(override)
        merged = {k: v for k, v in merged.items() if v is not None}
    elif override is not None:
        logger.warning(
            "gemini live: extra_config.turn_detection must be an object, got "
            "%s — using the built-in defaults", type(override).__name__,
        )

    aad: Dict[str, Any] = {}
    realtime: Dict[str, Any] = {}
    ignored: List[str] = []
    for key, value in merged.items():
        if key in _AAD_INT_FIELDS:
            try:
                aad[key] = int(value)
            except (TypeError, ValueError):
                logger.warning(
                    "gemini live: turn_detection.%s=%r is not a number — "
                    "ignoring it", key, value,
                )
        elif key in _AAD_BOOL_FIELDS:
            aad[key] = bool(value)
        elif key in _SENSITIVITY_FIELDS:
            coerced = _coerce_enum(_SENSITIVITY_FIELDS[key], value, key)
            if coerced is not None:
                aad[key] = coerced
        elif key in _REALTIME_INPUT_FIELDS:
            coerced = _coerce_enum(_REALTIME_INPUT_FIELDS[key], value, key)
            if coerced is not None:
                realtime[key] = coerced
        else:
            ignored.append(key)
    if ignored:
        # INFO, not WARNING: the common cause is an Azure row's block copied
        # onto a Gemini row, which is a reasonable thing for an operator to
        # do and is handled correctly — it just needs to be visible that
        # `threshold` and friends did nothing here.
        logger.info(
            "gemini live: turn_detection keys not supported by this provider, "
            "ignored: %s", ", ".join(sorted(ignored)),
        )
    if not aad and not realtime:
        return None
    if aad:
        realtime["automatic_activity_detection"] = (
            genai_types.AutomaticActivityDetection(**aad)
        )
    return genai_types.RealtimeInputConfig(**realtime)


# Context-window compression -- ON by default, and load-bearing for any call
# longer than a few minutes. Without it a session accumulates every audio
# token until the model's context ceiling, and the failure is a one-two the
# operator experiences as two separate bugs: first the model's tool-calling
# discipline collapses as the context bloats (live 2026-08-27, 08:05-08:12:
# ELEVEN consecutive announce-then-stall recoveries -- the model had stopped
# calling ask_agent entirely and every answer arrived via the tripwire's
# direct delegation), then the service kills the connection outright
# (`receive failed`, provider failed mid-session, 08:12:58). The sliding
# window keeps the live context bounded so neither cliff is reached; the
# system_instruction (persona) and tool declarations survive compression by
# API contract. Values are Google's documented flash-live guidance; operator
# override via `extra_config.context_window_compression`
# ({"trigger_tokens": N, "target_tokens": M}), explicit null disables --
# the same semantics as `turn_detection` above.
_DEFAULT_CONTEXT_COMPRESSION: Dict[str, Any] = {
    "trigger_tokens": 25600,
    "target_tokens": 12800,
}


def _context_compression(cfg: LiveVoiceConfig) -> Optional[Any]:
    """Build `ContextWindowCompressionConfig` from operator config."""
    override = cfg.extra_config.get("context_window_compression", "ABSENT")
    if override is None:
        return None  # explicit null: operator handed limits back to the provider
    merged: Dict[str, Any] = dict(_DEFAULT_CONTEXT_COMPRESSION)
    if isinstance(override, dict):
        merged.update({k: v for k, v in override.items() if v is not None})
    elif override != "ABSENT":
        logger.warning(
            "gemini live: extra_config.context_window_compression must be an "
            "object or null, got %s — using the built-in defaults",
            type(override).__name__,
        )
    try:
        return genai_types.ContextWindowCompressionConfig(
            trigger_tokens=int(merged["trigger_tokens"]),
            sliding_window=genai_types.SlidingWindow(
                target_tokens=int(merged["target_tokens"])
            ),
        )
    except (KeyError, TypeError, ValueError):
        logger.warning(
            "gemini live: bad context_window_compression values %r — "
            "using the built-in defaults", merged,
        )
        return genai_types.ContextWindowCompressionConfig(
            trigger_tokens=_DEFAULT_CONTEXT_COMPRESSION["trigger_tokens"],
            sliding_window=genai_types.SlidingWindow(
                target_tokens=_DEFAULT_CONTEXT_COMPRESSION["target_tokens"]
            ),
        )


def _function_declarations(cfg: LiveVoiceConfig) -> List[Dict[str, Any]]:
    """ask_agent, or — in pre-fetched-context mode — the call's direct tools
    INSTEAD of it. Direct tools are instant local actions, so they stay
    BLOCKING: the model waits the few ms for the result and then speaks on
    it, rather than talking past an action it has not seen succeed. A
    prompt-only call (`tools_disabled`) declares nothing at all."""
    if cfg.tools_disabled:
        return []
    if cfg.direct_tools:
        return [dict(d) for d in cfg.direct_tools]
    return [dict(ask_agent_declaration(), behavior="NON_BLOCKING")]


def _connect(cfg: LiveVoiceConfig, persona: str, *, omit_language: bool = False):
    """Return the SDK's async-context-manager connection. Patched in tests.

    `omit_language=True` builds the same config with NO
    `speech_config.language_code` — the retry path for models that reject an
    explicit code (see GeminiLiveSession.start): the native-audio line
    auto-detects the spoken language and refuses every explicit Arabic code
    with a 1007 `Unsupported language code`, while the 3.1 live line accepts
    them. Only the SpeechConfig field is dropped; `cfg.language` itself
    stays authoritative everywhere else (holding-line language, speakable
    rendering)."""
    # A key stored on Settings > Model keys, else the SDK reads the variable.
    from core.provider_keys import stored_key

    client = genai.Client(api_key=cfg.extra_config.get("api_key")
                          or stored_key("google_genai") or None)
    # Ambient-scribe (text_only) sessions never produce audio output, so there
    # is nothing to voice and nothing to transcribe on the output side --
    # `speech_config`/`output_audio_transcription` are audio-output-only
    # blocks and are omitted entirely rather than sent as None/empty, because
    # a voice config alongside TEXT modality is the kind of thing a preview
    # API rejects whole-request. `input_audio_transcription` and
    # `session_resumption` stay unconditional in both modes.
    #
    # Consequence for an ambient scribe (text_only, 2026-10-07 audit): on this
    # provider the INPUT language cannot be pinned. `language_code` lives on
    # the output-only SpeechConfig, and the input-side
    # `AudioTranscriptionConfig(language_codes=[...])` is Vertex-only — the
    # google-genai SDK (1.73) raises "language_codes parameter is not
    # supported in Gemini API" client-side for an api-key client like this
    # one, which would take the whole connect down. So a text_only Gemini
    # session relies on the persona's {language} line plus model
    # auto-detection; a deployment that needs the recognizer LOCKED to a
    # language (and a kind's phrase list) must run that profile on an
    # `azure_voice_live` model row, which pins input_audio_transcription.
    speech_config = None
    output_audio_transcription = None
    if not cfg.text_only:
        speech_config = genai_types.SpeechConfig(
            voice_config=genai_types.VoiceConfig(
                prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(voice_name=cfg.voice_id)
            ),
            # voice_config.live.language (BCP-47, e.g. "ar") -- documented in
            # 02-configure.md/data-model.md as a working knob since it was
            # added, but nothing read it until now: an operator setting
            # language="ar" on a channel got silence, not an error, because
            # SpeechConfig defaulted language_code to the provider's own
            # choice regardless. `None`/blank means "let the provider decide"
            # (its existing behaviour), so this is additive, not a behaviour
            # change for tenants that never set it.
            language_code=(None if omit_language else (cfg.language or None)),
        )
        output_audio_transcription = genai_types.AudioTranscriptionConfig()
    # Thinking control for the native-audio line
    # (gemini-2.5-flash-native-audio-*): those models THINK by default,
    # which delays first audio and streams thought summaries as TEXT parts
    # in AUDIO mode (see the part.thought skip in _translate). The row opts
    # in via extra_config.thinking_budget — 0 disables thinking outright.
    # Absent → nothing is sent, so the gemini-3.1-flash-live-preview rows
    # (which reject/ignore the block) are byte-identical.
    thinking_config = None
    raw_budget = cfg.extra_config.get("thinking_budget")
    if raw_budget is not None:
        try:
            thinking_config = genai_types.ThinkingConfig(
                thinking_budget=int(raw_budget)
            )
        except (TypeError, ValueError):
            logger.warning(
                "gemini live: bad extra_config.thinking_budget %r — ignored",
                raw_budget,
            )
    config = genai_types.LiveConnectConfig(
        response_modalities=(["TEXT"] if cfg.text_only else ["AUDIO"]),
        system_instruction=persona,
        speech_config=speech_config,
        input_audio_transcription=genai_types.AudioTranscriptionConfig(),
        output_audio_transcription=output_audio_transcription,
        session_resumption=genai_types.SessionResumptionConfig(),
        # Bounded live context (see _DEFAULT_CONTEXT_COMPRESSION above) --
        # unconditional in both modes: an ambient-scribe session runs even
        # longer than a phone call.
        context_window_compression=_context_compression(cfg),
        # End-of-speech detection. Set in BOTH modes on purpose: it governs
        # the INPUT side, and an ambient-scribe session commits caller turns
        # exactly like a phone one.
        realtime_input_config=_activity_detection(cfg),
        # A prompt-only call has no tools: omit the block rather than send an
        # empty declaration list.
        tools=([{"function_declarations": decls}] if (decls := _function_declarations(cfg)) else None),
        thinking_config=thinking_config,
    )
    return client.aio.live.connect(model=cfg.model_string, config=config)


class GeminiLiveSession(RealtimeSession):
    provider = "google_genai"
    # Derived from the REAL declaration this provider hands to the SDK
    # (ASK_AGENT_DECLARATION, above) -- not hand-copied from
    # ASK_AGENT_TOOL_SPEC -- so the two can only agree if the actual wiring
    # agrees. If ASK_AGENT_DECLARATION's name or its single argument key ever
    # changes without updating base.py's contract, this attribute changes
    # with it and assert_realtime_contract (voice/live/testing.py) catches
    # the mismatch instead of the drift going unnoticed.
    tool_spec = ToolSpec(
        name=ASK_AGENT_DECLARATION["name"],
        query_arg=next(iter(ASK_AGENT_DECLARATION["parameters"]["properties"])),
    )
    # Verified against the Live API's FunctionResponseScheduling: multiple
    # FunctionResponse messages for the same call id are delivered in order
    # (that's exactly how send_tool_result above sends each streamed
    # sentence), so per-sentence streaming is safe on this provider.
    supports_streamed_tool_results = True
    # …but it does NOT transcribe them, which is a separate question and the
    # one that decides where the SCREEN's copy of a streamed answer comes
    # from. Streaming stays on here; the endpoint sends its own sentence text
    # instead of this provider's transcription of it.
    #
    # Live-probed 2026-08-19 against `gemini-3.1-flash-live-preview`, replaying
    # the answer from that morning's client call as the four sentences
    # voice/chunker.py cuts it into (`streamed=4` in the VM's own metrics for
    # that turn). `output_audio_transcription` returned the leading fragment of
    # each response and nothing after it —
    #
    #   delivered   أبشر، / ولا تشيل هم. / هل معك رقم الطرفية للمشكلة، / ولا أفتحها على أي فرع؟
    #   transcribed أبشر، / ولا تشيل     / هل معك                      / ولا أفتحها
    #
    # — while the audio spoke every word, which is exactly what the caller
    # reported ("it speaks more than it writes"). The mechanism is a ~1s lag
    # between the generated audio and its transcription: each response's
    # outstanding remainder is dropped when that response's generation ends,
    # and a 1-2s response never gets it out. Reproduced identically through a
    # raw continuous `_receive()` loop, so it is not the SDK's per-turn
    # generator break. The SAME answer as ONE tool result transcribed
    # completely and byte-identically, 5 runs out of 5 — so this is specific
    # to the streamed shape, and the whole-answer delivery path still reads
    # its transcription.
    transcribes_streamed_tool_results = False
    # Not needed here: this provider takes several tool results per call id,
    # so the holding line rides the normal WHEN_IDLE path.
    supports_out_of_band_speech = False
    # Declared explicitly rather than inherited, because each was REASONED
    # about for this provider rather than merely not implemented.
    #
    # TRUE, but not because the service finalizes anything: it does not. The
    # Live API streams `input_transcription` and never sets
    # `Transcription.finished` (probed 2026-08-19 across three full
    # utterances — every fragment `finished=None`, the last one included).
    # `UserTurnState` below SYNTHESIZES the boundary from the event the
    # service does send, the model taking the floor for a new response, which
    # the same probe showed arriving in the same message as the caller's
    # transcript to the millisecond. That is what the endpoint's whole
    # commit-keyed layer needs: `last_commit`, the three stall tiers,
    # `_arm_silent_recovery`, and relay custody's dispatch. Until it existed
    # every one of them had never run here, and a caller left in silence by a
    # stalled turn had nothing to rescue her.
    #
    finalizes_user_transcripts = True
    # ...and this one stays False, which is NOT an oversight now that the line
    # above is True. Confirmation reads its verdict off a final that arrives
    # WHILE the caller is talking over the assistant, and the synthesized
    # final deliberately does not fire while a response is in flight (see
    # UserTurnState: firing then would finalize her utterance with the next
    # audio frame of the speech she is interrupting, cutting it in half). So
    # the verdict never arrives mid-barge-in, and the endpoint keeps the
    # legacy cut-on-detection path here. Consequence to know: an unconfirmed
    # barge-in bumps the supersede epoch, so an answer still in flight is
    # dropped SILENT — observed live 2026-08-19 (`mode=silent`,
    # `dispatch_to_result_ms=None`, 1.2s from the caller's ear). Fixing that
    # needs a verdict source that survives an in-flight response; it is not
    # fixed by this flag.
    supports_barge_in_confirmation = False
    # There is no wire state to report: `send_tool_response` hands the
    # FunctionResponse to the Live API with its scheduling and the service
    # decides when to speak it — there is no `response.create` of ours for a
    # racing response to reject, so the mute race the answer watchdog rescues
    # has no analogue here, and the watchdog's only possible contribution
    # would be the duplicate it produces when it cannot see an answer already
    # on its way.
    reports_response_in_flight = False

    def __init__(self, cfg: LiveVoiceConfig, *, persona: str) -> None:
        self._cfg = cfg
        self._persona = persona
        self.audio_format = AudioFormat(
            input_sample_rate=_INPUT_RATE, output_sample_rate=_OUTPUT_RATE
        )
        self._ctx: Optional[Any] = None
        self._upstream: Optional[Any] = None
        self._closed = False
        # One per session: the caller's utterance and the turn_end `reply`
        # verdict both span messages, and `send_*` below must reach it too.
        self._turn_state = UserTurnState()
        # call_id -> function name, for tools other than ask_agent: Gemini
        # matches a FunctionResponse to its call by id AND name.
        self._call_names: Dict[str, str] = {}

    async def start(self) -> None:
        try:
            self._ctx = _connect(self._cfg, self._persona)
            self._upstream = await self._ctx.__aenter__()
        except Exception as exc:
            # A model that auto-detects the spoken language refuses an
            # explicit `speech_config.language_code` outright (1007
            # "Unsupported language code 'ar-SA' for model ...gemini-2.5-
            # flash-native-audio..." — live on a staging voice tenant
            # 2026-09-02, where it killed every Arabic-page call at connect
            # while en-US worked). The code is a hint, not a requirement:
            # the persona/greeting pin the spoken language anyway, so
            # retrying once WITHOUT the code beats failing the whole call —
            # and models that accepted the code never reach this branch.
            if self._cfg.language and "unsupported language code" in str(exc).lower():
                logger.warning(
                    "gemini live: model %s rejected language_code=%r — "
                    "retrying the connect without it (the model picks the "
                    "language itself; persona still pins it)",
                    self._cfg.model_string, self._cfg.language,
                )
                try:
                    self._ctx = _connect(self._cfg, self._persona, omit_language=True)
                    self._upstream = await self._ctx.__aenter__()
                    return
                except Exception:
                    logger.error(
                        "gemini live: retry without language_code failed too",
                        exc_info=True,
                    )
                    raise RealtimeProviderError(
                        "realtime provider connect failed"
                    ) from exc
            logger.error("gemini live: connect failed", exc_info=True)
            raise RealtimeProviderError("realtime provider connect failed") from exc

    @property
    def endpoint_requests_outstanding(self) -> int:
        """Spoken endpoint requests no response has consumed (session summary)."""
        return self._turn_state.endpoint_requests

    def _require_upstream(self):
        if self._upstream is None:
            raise RealtimeProviderError("session not started")
        return self._upstream

    async def send_audio(self, pcm: bytes) -> None:
        upstream = self._require_upstream()
        try:
            await upstream.send_realtime_input(
                audio=genai_types.Blob(data=pcm, mime_type=f"audio/pcm;rate={_INPUT_RATE}")
            )
        except Exception as exc:
            raise RealtimeProviderError("realtime provider send failed") from exc

    async def send_text_turn(self, text: str) -> None:
        upstream = self._require_upstream()
        # The greeting (and any other text turn) is the ENDPOINT asking for a
        # response: whatever answers it is not a reply to the caller.
        self._turn_state.endpoint_requests += 1
        try:
            await upstream.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]},
                turn_complete=True,
            )
        except Exception as exc:
            raise RealtimeProviderError("realtime provider text-turn failed") from exc

    async def send_tool_result(
        self, call_id: str, payload: Dict[str, Any], *, mode: ToolResultMode
    ) -> None:
        scheduling = _scheduling_for(mode)
        if mode is not ToolResultMode.SILENT and not self._closed:
            # A spoken tool result (holding line, answer, streamed sentence)
            # makes the service produce a response the ENDPOINT caused.
            self._turn_state.endpoint_requests += 1
            if mode is ToolResultMode.INTERRUPT:
                self._turn_state.self_interrupt_pending = True
        if self._closed:
            # A delegated turn takes ~10s; a caller who hangs up during one
            # leaves a task that still has a result to deliver. Sending into a
            # torn-down session is expected in that race, not a fault -- raise
            # nothing and log nothing above debug, so a genuine
            # tool-response failure stays visible in the logs instead of being
            # buried in hang-up noise.
            logger.debug(
                "gemini live: dropping tool result for %s, session already closed", call_id
            )
            return
        upstream = self._require_upstream()
        try:
            await upstream.send_tool_response(
                function_responses=[
                    genai_types.FunctionResponse(
                        id=call_id,
                        name=self._call_names.pop(call_id, self.tool_spec.name),
                        response=payload,
                        scheduling=scheduling,
                    )
                ]
            )
        except ConnectionClosed:
            # Same race, lost narrowly: the socket closed between the check
            # above and the send.
            logger.debug(
                "gemini live: tool result for %s raced a session close", call_id
            )
        except Exception as exc:
            raise RealtimeProviderError("realtime provider tool-response failed") from exc

    async def events(self) -> AsyncIterator[RealtimeEvent]:
        # The SDK's `receive()` is documented to yield exactly one model
        # turn and then end its own generator (it breaks right after a
        # `turn_complete` message) -- it is NOT a single stream for the
        # whole session. A single `async for ... in upstream.receive()`
        # would therefore go silent after the caller's first exchange.
        # Looping here calls it again for every subsequent turn, until
        # `close()` flips `_closed`. `asyncio.sleep(0)` between attempts
        # cedes control back to the event loop so a transport that ends a
        # call immediately (idle, or a stub with nothing queued) can't spin
        # this coroutine and starve the loop's own timers.
        upstream = self._require_upstream()
        # One instance for the whole session: a caller's transcript can span
        # several messages, so the open/closed state of her utterance cannot
        # live inside a single _translate call.
        user_turn = self._turn_state
        try:
            while not self._closed:
                async for message in upstream.receive():
                    for event in _translate(message, user_turn):
                        if event.type == "tool_call" and event.tool_name:
                            self._call_names[event.call_id or ""] = event.tool_name
                        yield event
                if self._closed:
                    return
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("gemini live: receive failed", exc_info=True)
            yield RealtimeEvent(type="error", message=str(exc))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._ctx is not None:
            try:
                await self._ctx.__aexit__(None, None, None)
            except Exception:
                logger.debug("gemini live: close raised", exc_info=True)
        self._ctx = None
        self._upstream = None


class UserTurnState:
    """Per-session state for synthesizing the user-transcript `final`.

    Gemini Live NEVER sets `Transcription.finished` on the INPUT side. Probed
    directly (2026-08-19, `gemini-3.1-flash-live-preview`, three utterances,
    full traces): every `input_transcription` message came back
    `finished=None`, including the last one. `output_transcription` behaves
    the same way.

    That one missing flag switched off, silently, every recovery mechanism the
    duplex edge has. In `endpoints/voice_live.py` the caller's committed
    utterance is handled entirely inside `if event.final:` for a `user`
    transcript — `last_commit` (the blank-`ask_agent` recovery), the three
    stall-recovery tiers, `_arm_silent_recovery`, the supersede epoch bump,
    intent-triggered relay custody, and the closing of the client's interim
    transcript bubble. On Gemini none of it had ever run. A caller left in
    silence by a stalled turn had nothing to rescue her.

    So derive the boundary from what the service DOES send. The probe shows
    the input transcript arriving in the SAME message as the model's first
    audio, to the millisecond (`+1089 in_tx` / `+1089 audio`): the service
    transcribes the caller's turn at the moment it commits it and hands the
    floor to the model. "The model has taken the floor for a NEW response" is
    therefore an accurate end-of-utterance marker, not an approximation.

    Two things this deliberately does NOT do:

    * It does not emit `user_turn_end`. That event feeds
      `eot_to_first_audio_ms`, and a marker derived FROM the first audio would
      report ~0ms forever — a metric that looks healthy and measures nothing.
      Gemini still has no true end-of-turn signal; the honest reading is the
      absent attribute it already produces.
    * It does not fire while a response is already in flight. A caller
      speaking OVER the assistant would otherwise be "finalized" by the next
      audio frame of the speech she is interrupting, cutting her utterance in
      half. The model's own `interrupted` / `generation_complete` /
      `turn_complete` close the in-flight response first, and only the next
      floor-taking event finalizes.
    """

    __slots__ = ("user_open", "response_active", "user_spoke", "user_spoke_in_response",
                 "reply_of_open", "endpoint_requests", "self_interrupt_pending",
                 "self_interrupted", "late_close_expected", "carried_verdict")

    def __init__(self) -> None:
        self.user_open = False
        self.response_active = False
        # turn_end `reply` bookkeeping — see `_stamp_turn_reply`.
        self.user_spoke = False
        self.user_spoke_in_response = False
        self.reply_of_open: Optional[bool] = None
        self.endpoint_requests = 0
        # An INTERRUPT-scheduled tool result was sent: the service's next
        # `interrupted` is OURS (the answer displacing our holding line), not
        # the caller's — see `_stamp_turn_reply`.
        self.self_interrupt_pending = False
        self.self_interrupted = False
        # A response we cut and closed silently (its turn_complete had not
        # come) may still get one LATE — after the next response's first
        # audio. That stray turn_end must not leave the next response's own
        # turn_end key-less; see `_stamp_turn_reply`.
        self.late_close_expected = False
        self.carried_verdict: Optional[bool] = None


# Events that mean the model has the floor. `audio` and an assistant
# transcript both count: a text-modality (ambient scribe) session produces no
# audio at all, and would otherwise never finalize a caller's utterance.
_MODEL_FLOOR_EVENTS = ("audio", "tool_call")


def _is_model_floor(event: RealtimeEvent) -> bool:
    if event.type in _MODEL_FLOOR_EVENTS:
        return True
    return event.type == "transcript" and event.role == "assistant"


def _stamp_turn_reply(event: RealtimeEvent, state: UserTurnState) -> RealtimeEvent:
    """Carry each response's `reply` verdict from its START to its turn_end.

    The client merges a `reply: false` turn into the previous one (the answer
    after an announce line, a holding line, each streamed answer sentence) and
    opens a late-transcript window on `reply: true`. Without the key every
    Gemini response became its own chat bubble (2026-09-28 E2E).

    The rule: a response is a REPLY only if the caller took the floor since
    the previous response closed. Concretely —

    * Caller transcript that arrives while NO response is open counts.
    * Caller transcript that arrives while a response IS open is parked: an
      echo of her own voice, a backchannel, room noise the recognizer turned
      into words, or the late tail of the utterance just answered. It counts
      only if that response then ends `interrupted` — she actually took the
      floor. A normal turn_end discards it.
    * A response the ENDPOINT asked for (the greeting's text turn, a holding
      line, an answer or a streamed sentence delivered as a tool result;
      `endpoint_requests`, incremented by `send_*`) is never a reply, and it
      consumes any caller speech since the last close — the client attached
      that speech to this turn, so the model's answer after it merges in.
      Erring toward `false` is safe for the client: it never merges a turn
      that carries user text, and releases the previous turn as soon as the
      caller speaks (verified in ai_voice_cubit.dart `_endTurn`/`_onUser`),
      whereas a `true` with no user text opens a late-transcript window.

    The verdict is taken when the response takes the floor (its first audio
    or tool call), not at turn_end: turn_complete is paced to the AUDIO
    timeline (probed 7s after generation), by which time the next utterance
    may already be in. Only audio and tool calls open a response — an
    assistant transcript can lag its audio, and a text-only session keeps the
    legacy frame. A response the CALLER interrupts gets no verdict (no key);
    one cut by the endpoint's own INTERRUPT delivery keeps its verdict.
    """
    if event.type == "transcript" and event.role == "user" and event.text:
        if state.reply_of_open is None:
            state.user_spoke = True
        else:
            state.user_spoke_in_response = True
    elif event.type in ("audio", "tool_call"):
        if state.self_interrupted:
            # The response WE interrupted never got its turn_complete: this
            # floor event is the next response. Close ours silently — and
            # expect its turn_complete may still arrive late.
            state.self_interrupted = False
            state.reply_of_open = None
            state.late_close_expected = True
        elif state.reply_of_open is None:
            state.late_close_expected = False
            state.carried_verdict = None
        if state.reply_of_open is None:
            state.self_interrupt_pending = False
            if state.endpoint_requests:
                # Ours, so `false`. Caller speech since the last close is
                # CONSUMED here too: the client has already put her words in
                # this turn (they arrived before its audio), so the model's
                # own answer that follows must merge into it (`false`), not
                # open a turn with no user text and a late-transcript window
                # that would swallow her NEXT utterance (fix round 2).
                state.endpoint_requests -= 1
                state.reply_of_open = False
                state.user_spoke = False
            else:
                state.reply_of_open = state.user_spoke
                state.user_spoke = False
            state.user_spoke_in_response = False
    elif event.type == "interrupted":
        if state.self_interrupt_pending:
            # Our own INTERRUPT delivery cut the response (the answer
            # displacing the holding line, 2026-09-28 retest): its verdict
            # stands — the service still sends that response's turn_complete,
            # and a key-less frame there split the answer into its own bubble.
            # Caller speech parked during it is not a barge-in either.
            state.self_interrupt_pending = False
            state.self_interrupted = state.reply_of_open is not None
        else:
            state.reply_of_open = None
            if state.user_spoke_in_response:
                state.user_spoke = True
        state.user_spoke_in_response = False
    elif event.type == "turn_end":
        verdict, state.reply_of_open = state.reply_of_open, None
        if verdict is not None and state.late_close_expected:
            # Possibly the cut response's LATE turn_complete arriving inside
            # the next response: stamp it with that response's verdict and
            # carry it, so the response's own turn_end (arriving with nothing
            # open) gets it too instead of going out key-less (the V8 split,
            # in this ordering). Re-review round 3.
            state.late_close_expected = False
            state.carried_verdict = verdict
        elif verdict is None and state.carried_verdict is not None:
            verdict, state.carried_verdict = state.carried_verdict, None
        state.user_spoke_in_response = False
        state.self_interrupted = False
        if verdict is not None and event.turn_reply is None:
            return RealtimeEvent(type="turn_end", turn_reply=verdict)
    return event


def _synthesize_user_final(
    events: list[RealtimeEvent], state: UserTurnState, *, generation_complete: bool
) -> list[RealtimeEvent]:
    """Insert the `final=True` user transcript Gemini never sends. See UserTurnState."""
    out: list[RealtimeEvent] = []
    for event in events:
        event = _stamp_turn_reply(event, state)
        if event.type == "transcript" and event.role == "user":
            # A real final (a future SDK/API that starts setting the flag)
            # closes the utterance itself — never synthesize a second one.
            state.user_open = not event.final
            out.append(event)
            continue
        if event.type == "interrupted" or event.type == "turn_end":
            state.response_active = False
            out.append(event)
            continue
        if _is_model_floor(event):
            if state.user_open and not state.response_active:
                # Empty text on purpose: the endpoint accumulates the deltas
                # itself (`relay_user_text`) and reads the buffer on the
                # final, which is the same shape Azure's streamed transcripts
                # end in. Carrying text here would double every word.
                out.append(
                    RealtimeEvent(type="transcript", role="user", text="", final=True)
                )
                state.user_open = False
            state.response_active = True
        out.append(event)
    if generation_complete:
        # The model stopped producing. `turn_complete` is NOT a substitute
        # here: probed on one turn it arrived 7.0s after generation finished
        # (+10904ms vs +3881ms), because the service paces it to the audio
        # timeline rather than to generation. Waiting for it would leave a
        # response "active" for seconds after it ended and swallow the
        # finalize for the caller's next utterance.
        state.response_active = False
    return out


def _translate(message, state: Optional[UserTurnState] = None) -> list[RealtimeEvent]:
    """Map one SDK message onto zero or more provider-neutral events."""
    out: list[RealtimeEvent] = []

    server_content = getattr(message, "server_content", None)
    if server_content is not None:
        if getattr(server_content, "interrupted", False):
            out.append(RealtimeEvent(type="interrupted"))
        # BLOCKER 5 fix: the Live API streams input/output transcription as
        # incremental fragments of ONE utterance, not one message per
        # utterance -- `Transcription.finished` (verified present on the
        # pinned SDK's Transcription type: `finished: Optional[bool]`,
        # "indicates the end of the transcription") is the only signal for
        # which fragment is the last one. Hardcoding final=True on every
        # fragment (the previous behaviour) made the client render a new
        # user bubble per fragment -- one spoken question rendered as
        # several bubbles. `final` now reflects the real flag; `text` stays
        # the raw per-message delta (not accumulated here) so the client's
        # existing "acc += delta, re-render in place" streaming pattern
        # (see dispatch()'s "answer" case) is the one place that owns
        # reassembly, instead of duplicating that logic on both ends. Also
        # emit on a finished-but-textless chunk (text defaults to "") so a
        # final message with no additional delta still closes out the
        # client's interim bubble rather than leaving it open forever.
        input_tx = getattr(server_content, "input_transcription", None)
        if input_tx is not None:
            in_text = getattr(input_tx, "text", None) or ""
            in_finished = bool(getattr(input_tx, "finished", False))
            if in_text or in_finished:
                out.append(
                    RealtimeEvent(type="transcript", role="user", text=in_text, final=in_finished)
                )
        output_tx = getattr(server_content, "output_transcription", None)
        if output_tx is not None:
            out_text = getattr(output_tx, "text", None) or ""
            out_finished = bool(getattr(output_tx, "finished", False))
            if out_text or out_finished:
                out.append(
                    RealtimeEvent(type="transcript", role="assistant", text=out_text, final=out_finished)
                )
        turn = getattr(server_content, "model_turn", None)
        for part in getattr(turn, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None)
            if data:
                out.append(RealtimeEvent(type="audio", audio=data))
            text = getattr(part, "text", None)
            if text and not getattr(part, "thought", False):
                # TEXT-modality sessions deliver the model's words as parts,
                # not as output_transcription -- surface them as assistant
                # transcript deltas so the client streams them like captions.
                #
                # `part.thought` parts are EXCLUDED: "AUDIO-mode messages
                # carry inline_data, not text parts" stopped being true the
                # moment a THINKING model answered — the native-audio line
                # (gemini-2.5-flash-native-audio-*) streams its thought
                # summaries as text parts alongside the audio, and forwarding
                # them painted the model's internal monologue ("**Crafting
                # the Opening Greeting** I've crafted...") on the caller's
                # screen as the assistant transcript (live on a staging voice
                # tenant, 2026-08-31, 4/4 probe calls). Thoughts are
                # never spoken, so dropping them loses no caption — and the
                # assistant-reply buffer that feeds the announce-then-stall
                # tripwire must not read them either.
                out.append(
                    RealtimeEvent(type="transcript", role="assistant", text=text, final=False)
                )
        if getattr(server_content, "turn_complete", False):
            out.append(RealtimeEvent(type="turn_end"))

    tool_call = getattr(message, "tool_call", None)
    for call in getattr(tool_call, "function_calls", None) or []:
        out.append(
            RealtimeEvent(
                type="tool_call",
                call_id=getattr(call, "id", "") or "",
                tool_name=getattr(call, "name", "") or "",
                arguments=dict(getattr(call, "args", None) or {}),
            )
        )

    # `state` is optional so the many direct `_translate(message)` unit tests
    # (and any future caller that only wants the mapping) keep working: a
    # throwaway state still finalizes correctly for the observed common case,
    # where the caller's transcript and the model's first audio arrive in the
    # SAME message. `events()` owns the real per-session instance.
    return _synthesize_user_final(
        out,
        state if state is not None else UserTurnState(),
        generation_complete=bool(
            getattr(server_content, "generation_complete", False)
        ) if server_content is not None else False,
    )


def build_gemini_live(cfg: LiveVoiceConfig, persona: str) -> RealtimeSession:
    return GeminiLiveSession(cfg, persona=persona)


register_realtime_provider("google_genai", build_gemini_live)
