"""Azure Voice Live — the second RealtimeSession implementation.

Speaks the Azure OpenAI Realtime wire protocol (JSON events over WebSocket)
directly via aiohttp; Voice Live is protocol-compatible with additive Azure
extensions (azure_semantic_vad, azure-speech transcription, azure-standard
voices). Everything Azure-specific is confined to this file.

Cascaded mode: Azure speech-to-text (locale-pinned, phrase-list-boosted)
feeds a text chat model, Azure neural TTS speaks the reply. The model gets
exactly one tool (ask_agent) and does not answer data questions itself.

Design spec: docs/superpowers/specs/2026-07-28-azure-voice-live-provider-design.md
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import ssl
import urllib.parse
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import aiohttp

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
from voice.live.config import (
    LiveVoiceConfig,
    resolve_progress_spoken_variants,
    resolve_session_voice,
)
from voice.live.factory import register_realtime_provider

logger = logging.getLogger(__name__)

# Voice Live's rates (both directions negotiable 16k/24k; we run 24k/24k).
# Nothing outside this module may assume them — they reach the browser
# through the `ready` frame.
_RATE = 24000

_DEFAULT_API_VERSION = "2026-04-10"
# Last-resort fallback only — the operator names the real env var on the
# agent_models row (extra_config.auth_env_var).
_DEFAULT_AUTH_ENV = "AZURE_VOICE_LIVE_API_KEY"

# Name/argument come from the shared cross-provider contract
# (ASK_AGENT_TOOL_SPEC) — built FROM it so the two can't drift. The
# description and schema are this provider's own wording (OpenAI Realtime
# function-tool format, flat — not the chat-completions nested format).
# `{subject}` is `ask_agent_subject()`: who handles the request, a
# deployment's own words (ask_agent_declaration()).
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
    "BEFORE calling — except a pure relay of something the caller just "
    "gave you, which needs no announcement at all — then read the "
    "returned `spoken` text back exactly as written — never recompute, "
    "re-round or re-order a figure."
)

ASK_AGENT_DECLARATION: Dict[str, Any] = {
    "type": "function",
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


# BCP-47 -> a name to put in a prompt. Only the tags this stack pins are
# spelled out; anything else degrades to the bare tag, which the model still
# reads as a language instruction ("in ar-EG") rather than guessing.
_LANG_NAMES = {
    "ar-sa": "Arabic, Saudi dialect",
    "ar": "Arabic",
    "en-us": "English",
    "en": "English",
}


def _lang_name(cfg: LiveVoiceConfig) -> str:
    """Human language name for an out-of-band line's instructions.

    Out-of-band responses are created with `conversation: "none"` and their own
    `instructions`, which REPLACE the session's — so they inherit neither the
    persona nor the pinned language and will otherwise be read in whatever
    language the model settles on.
    """
    tag = (getattr(cfg, "language", None) or "").strip().lower()
    if not tag:
        # Never leave it unstated: unpinned is exactly the condition that
        # produced a French reading of an English line.
        return "the same language as the text"
    return _LANG_NAMES.get(tag) or _LANG_NAMES.get(tag.split("-")[0]) or tag


def _session_payload(cfg: LiveVoiceConfig, persona: str) -> Dict[str, Any]:
    """The one session.update sent at start(). Pure function — unit-tested
    directly. Operator overrides for turn detection merge over our defaults
    via extra_config.turn_detection."""
    # Turn detection, measured against the live service — not guessed. A/B on
    # the native model (gpt-realtime), interrupting mid-reply:
    #
    #   config                    real speech        white noise
    #   defaults                  detected 0.26s     not detected
    #   threshold .7 / 300ms      detected 0.36s     not detected
    #   *_assistant_speaking      NOT DETECTED       not detected
    #
    # So a native model already separates speech from noise, and the
    # `*_assistant_speaking` guards (require_vowel / distinct_ci_phones /
    # window_size) make a HUMAN unable to interrupt at all — they were added
    # here for the CASCADED model, where ambient noise really did cut the
    # reply off, and they belong on that row's extra_config, not in the
    # default. Being talked over is worse than the noise they prevent: a
    # caller answering early is the normal way people use a phone.
    #
    # threshold/speech_duration are nudged above the bare defaults for a small
    # margin (0.36s still reads as instant) while keeping barge-in real.
    turn_detection: Dict[str, Any] = {
        "type": "azure_semantic_vad",
        "interrupt_response": True,
        "auto_truncate": True,
        "threshold": 0.7,
        "speech_duration_ms": 300,
        # 650ms, measured — not guessed. This window is paid on EVERY turn
        # before any work starts, and a client's row carried 950 into the
        # 2026-08-05 client call. A/B against the live service (same
        # synthesized utterance, 3 runs each, end-of-speech -> committed):
        #
        #   silence_duration_ms   end-of-turn       one 400/600ms mid-
        #                         detected          sentence pause
        #   650                   0.87/0.88/0.88s   held (1 turn)
        #   950                   1.17/1.19/1.16s   held (1 turn)
        #
        # ~295ms off every turn, and the pause tolerance that the longer
        # window is supposed to buy did not differ: both settings held a
        # 400ms and a 600ms mid-sentence pause, and both split an 800ms one.
        # Note the ~230ms floor under the window itself — 650 does not mean
        # 650. Do not take it to the service's 500 default: the margin over
        # it is what is left of the think-pause tolerance.
        "silence_duration_ms": 650,
        "remove_filler_words": True,
    }
    override = cfg.extra_config.get("turn_detection") or {}
    if isinstance(override, dict):
        turn_detection.update(override)
        # A null in the override REMOVES the default. Without this an operator
        # can only add or change keys, never drop one — which is exactly how a
        # row that switched off the barge-in guards silently kept them: the
        # merge preserved the baked-in defaults and the caller still could not
        # interrupt. Config that cannot express "unset" is config you have to
        # redeploy to fix.
        turn_detection = {k: v for k, v in turn_detection.items() if v is not None}
    # Which recognizer writes the transcript. `azure-speech` is the right
    # answer for BOTH model kinds and is the default when a row says nothing.
    #
    # This comment used to claim a native audio model "transcribes for display
    # with an OpenAI recognizer instead" -- that is not a service constraint,
    # and believing it cost us the demo's transcript pane. Live-probed
    # 2026-07-30: gpt-realtime accepts `azure-speech`, phrase list included.
    # It matters because the gpt-4o-transcribe family is Whisper-family, where
    # `language` only BIASES decoding: on silence or room noise it emits
    # memorised subtitle boilerplate, so an ar-SA-pinned Saudi session showed
    # Bulgarian, German, Danish and Japanese lines the caller never said.
    # azure-speech is LOCKED to the locale and cannot do that.
    #
    # Still operator-set per row (a native model DOES hear the audio itself, so
    # a future row may have a reason to prefer its own recognizer), but the
    # default no longer sends a caller's transcript through a model that
    # invents language.
    stt_model = str(cfg.extra_config.get("transcription_model") or "azure-speech")
    transcription: Dict[str, Any] = {"model": stt_model}
    if cfg.language:
        # azure-speech wants BCP-47 ("ar-SA"); whisper-1 / gpt-4o-transcribe
        # want ISO-639-1 ("ar") and IGNORE anything else — falling back to
        # auto-detect, which on a Saudi call transcribed Korean and Latin
        # word-salad and fed it to the agent as if the merchant had said it.
        transcription["language"] = (
            cfg.language if stt_model == "azure-speech"
            else cfg.language.split("-")[0].strip().lower()
        )
    # Several caller languages: the Azure recognizer auto-detects among them
    # ("ar-JO,en-US", at most 10 — Voice Live language support doc). Only
    # azure-speech takes a list; a Whisper-family model keeps the single tag.
    langs = getattr(cfg, "caller_languages", None)
    if stt_model == "azure-speech" and isinstance(langs, (list, tuple)):
        tags = list(dict.fromkeys(str(t).strip() for t in langs if str(t).strip()))[:10]
        if len(tags) > 1:
            transcription["language"] = ",".join(tags)
    # Phrase lists only exist on the Azure recognizer — whisper-1 and the
    # gpt-4o-transcribe family reject them, so sending one to a native model
    # would fail the whole session.update over an optional nicety.
    if cfg.phrase_list and stt_model == "azure-speech":
        # Field name live-verified 2026-07-28 (session.updated accepted it).
        # Type-guarded because the value is raw operator JSON: a bare string
        # here would list()-iterate into single CHARACTERS and silently
        # degrade recognition rather than erroring anywhere.
        if isinstance(cfg.phrase_list, (list, tuple)):
            transcription["phrase_list"] = [str(p) for p in cfg.phrase_list]
        else:
            logger.warning(
                "voice_config.phrase_list must be a list of strings, "
                "got %s — ignoring it", type(cfg.phrase_list).__name__,
            )
    session: Dict[str, Any] = {
        "instructions": persona,
        # A native-audio model accepts exactly ["text"] or ["audio"] per
        # response (probed constraint documented at the out-of-band block);
        # session-level ["text"] is the whole point of text_only mode — no
        # TTS stage on cascaded rows, no audio tokens on native ones.
        "modalities": (["text"] if cfg.text_only else ["text", "audio"]),
        "input_audio_sampling_rate": _RATE,
        "turn_detection": turn_detection,
        "input_audio_noise_reduction": {"type": "azure_deep_noise_suppression"},
        "input_audio_transcription": transcription,
        "voice": _voice_payload(cfg),
        # Pre-fetched-context mode declares the call's direct tools INSTEAD
        # of ask_agent (see LiveVoiceConfig.prefetched_context).
        "tools": (
            [] if cfg.tools_disabled
            else [{"type": "function", **d} for d in cfg.direct_tools]
            if cfg.direct_tools else [ask_agent_declaration()]
        ),
        "tool_choice": "none" if cfg.tools_disabled else "auto",
    }
    # Server-side echo cancellation exists to stop the model hearing its OWN
    # speech through the caller's speaker. A text-only session emits no audio,
    # so there is no echo to cancel — and the service REJECTS the whole
    # session.update for asking: "Server side echo cancellation is not
    # supported when modalities is text-only" (live, staging, 2026-08-10).
    # A rejected session.update takes the entire session down, so this is not
    # a tidy-up: it is what makes text_only usable on this provider at all.
    if not cfg.text_only:
        session["input_audio_echo_cancellation"] = {"type": "server_echo_cancellation"}
    # interim_response exists solely to fill AUDIBLE dead air — with no audio
    # there is nothing to fill, and the block references voices.
    if not cfg.text_only:
        interim = _interim_response_payload(cfg)
        if interim is not None:
            session["interim_response"] = interim
    return {"type": "session.update", "session": session}


def _interim_response_payload(cfg: LiveVoiceConfig) -> Optional[Dict[str, Any]]:
    """The server-side tool-call filler block, or None when not opted in.

    Azure Voice Live's `interim_response` (api-version >= 2026-01-01-preview,
    GA in the 2026-04-10 default) fills tool-call/latency silence ITSELF —
    server-timed, bounded static text — which retires the endpoint's whole
    out-of-band holding-line machinery for this session (the endpoint checks
    `handles_interim_response` before scheduling one). Opt-in per model row
    (`extra_config.interim_response`), never a default: an api-version too
    old for the key would reject the whole session.update, so an operator
    should live-probe once per deployment (manual_tests/
    probe_azure_voice_live.py) before flipping it on.

    The operator states the MECHANISM; the WORDS default in from the same
    progress_spoken(_by_language) knobs the endpoint holding line uses, so
    per-language wording is configured once. Cascaded only — "Realtime audio
    models don't support interim responses" (documented), so a native row's
    block is dropped rather than risking a rejected session.update.
    """
    interim = cfg.extra_config.get("interim_response")
    if not isinstance(interim, dict):
        return None
    if str(cfg.extra_config.get("voice_type") or "azure-standard") == "native":
        return None
    block = dict(interim)
    block.setdefault("type", "static_interim_response")
    if block["type"] == "static_interim_response" and not block.get("texts"):
        texts = resolve_progress_spoken_variants(cfg)
        if texts:
            block["texts"] = texts
    if "latency_threshold_ms" not in block and cfg.progress_after_sec:
        block["latency_threshold_ms"] = int(cfg.progress_after_sec * 1000)
    return block


# The voices a native gpt-realtime model accepts (the service's own
# session.update error lists them). Anything else — an Azure neural name from
# `voice_by_language`, the config default "Kore" (a Gemini voice) — is
# rejected and the call closes with 4502 before it starts.
NATIVE_VOICES = frozenset(
    {"alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar"}
)
_NATIVE_FALLBACK_VOICE = "marin"


def _native_voice_name(cfg: LiveVoiceConfig, name: str) -> str:
    """A voice a native row will accept, whatever the profile carries.

    The seeders never write neural names onto a native row, but only at seed
    time: pointing `voice_config.model_id` at a native row LATER (the portal's
    model switch) left `voice_by_language` neural names and the "Kore"
    default in place, and every call failed at session.update (retest
    2026-09-28). Resolved here, where the row's `voice_type` is known:
    a native name passes; otherwise `voice_id` if native; otherwise the row's
    `extra_config.native_voice`, else "marin".
    """
    if (name or "").lower() in NATIVE_VOICES:
        return name.lower()
    fallback = (cfg.voice_id or "").lower()
    if fallback not in NATIVE_VOICES:
        fallback = str(cfg.extra_config.get("native_voice") or _NATIVE_FALLBACK_VOICE).lower()
    logger.warning(
        "azure voice live: voice %r is not a native voice; using %r "
        "(voice_config carries a non-native voice for a native model row)",
        name, fallback,
    )
    return fallback


def _voice_payload(cfg: LiveVoiceConfig) -> Any:
    """The `voice` field, whose SHAPE depends on the kind of voice.

    Azure TTS voices are an object ({name, type: azure-standard}); a native
    audio model's own voice is a bare string ("alloy", "marin", …). Operator
    picks via extra_config.voice_type because it is a property of the model
    row, not the tenant: pointing voice_config.live.model_id at a native row
    and keeping an Azure voice object would be rejected at session.update.

    Why it matters beyond plumbing: Azure's ar-SA voices are MSA-trained, so
    they read colloquial spellings wrong however the text is written. A native
    model generates the dialect itself, which is the only way to actually
    sound Saudi.

    `cfg.speaking_rate` (an Azure prosody rate string, e.g. "+10%" / "-20%" /
    "0.9") is added as a `rate` key ONLY on the azure-standard object form.
    The native (bare-string) voice has no object to hang a key off, and a
    native realtime model rejects an unrecognised shape outright -- there is
    no analogous per-utterance rate knob on that side of the API, so an
    operator-set rate is silently inert for a native-voice row rather than
    breaking session.update. Unset (None) leaves the payload byte-identical
    to before this field existed.
    """
    name = resolve_session_voice(cfg)
    if str(cfg.extra_config.get("voice_type") or "azure-standard") == "native":
        return _native_voice_name(cfg, name)
    voice: Dict[str, Any] = {"name": name, "type": "azure-standard"}
    if cfg.speaking_rate:
        voice["rate"] = cfg.speaking_rate
    # HD-voice expressiveness knobs (temperature 0-1, style, pitch, volume —
    # documented SSML-prosody-shaped values) ride the same object, straight
    # from the model row so an operator can tune voice character without a
    # deploy. Type-guarded raw operator JSON, same posture as phrase_list;
    # merged LAST so an explicit param wins over the derived rate above.
    params = cfg.extra_config.get("voice_params")
    if isinstance(params, dict):
        voice.update({k: v for k, v in params.items() if v is not None})
    return voice


def _ws_url(cfg: LiveVoiceConfig) -> str:
    endpoint = (cfg.extra_config.get("endpoint") or "").rstrip("/")
    if not endpoint:
        raise RealtimeProviderError(
            "azure_voice_live model row has no extra_config.endpoint"
        )
    if not endpoint.startswith("https://"):
        # An http:// or bare-hostname endpoint would silently become a
        # non-wss URL and fail with an opaque connect error — reject the
        # misconfiguration by name instead.
        raise RealtimeProviderError(
            "azure_voice_live extra_config.endpoint must start with "
            f"https:// (got {endpoint!r})"
        )
    host = endpoint.replace("https://", "wss://", 1)
    api_version = cfg.extra_config.get("api_version") or _DEFAULT_API_VERSION
    # quote(), not a bare f-string interpolation: api_version and
    # model_string both come from an operator-edited agent_models row
    # (extra_config.api_version / model_string), not a source constant. An
    # unescaped typo (a stray space, `&`, `#`, non-ASCII) would silently
    # corrupt or truncate the query string instead of breaking loudly.
    # safe="" -- nothing in either value is a URL structural character worth
    # preserving unescaped.
    return (
        f"{host}/voice-live/realtime"
        f"?api-version={urllib.parse.quote(api_version, safe='')}"
        f"&model={urllib.parse.quote(cfg.model_string, safe='')}"
    )


class _TransportError(RealtimeProviderError):
    """Internal marker: the websocket ended on a WSMsgType.ERROR frame -- a
    genuine mid-call transport failure (a dropped connection, a malformed
    frame aiohttp itself rejected), not a clean close. `_recv_json` raises
    this instead of returning None for that frame type, specifically so it
    is NOT conflated with CLOSE/CLOSING/CLOSED (a normal disconnect, where
    returning None and quietly ending events() is correct). Already a
    RealtimeProviderError, so start()'s ack-wait loop needs no extra
    handling to let it surface as one; events()'s existing `except
    Exception` -> `yield RealtimeEvent(type="error", ...)` handling already
    turns it into a visible error event before the generator ends, instead
    of silently going quiet on a network failure mid-call."""


async def _open_transport(cfg: LiveVoiceConfig) -> Tuple[Any, Any]:
    """Open (http_session, websocket). Module-level so tests patch it — the
    same seam pattern as gemini_live._connect."""
    env_name = cfg.extra_config.get("auth_env_var") or _DEFAULT_AUTH_ENV
    key = os.getenv(env_name)
    if not key:
        raise RealtimeProviderError(
            f"azure_voice_live: auth env var {env_name} is not set"
        )
    # aiohttp verifies TLS against the ssl module's default CA paths, which on
    # a python.org macOS install are EMPTY (certificate verify failed: unable
    # to get local issuer certificate) — unlike httpx, which wires certifi in
    # by itself and is why every other outbound call in this engine never hits
    # this. Pin the same certifi bundle here so the provider behaves like the
    # rest of the codebase on every platform. Fall back to library defaults if
    # certifi ever goes missing (Linux images have real system CAs).
    try:
        import certifi
        ssl_ctx: Any = ssl.create_default_context(cafile=certifi.where())
    except ImportError:  # pragma: no cover - certifi is a transitive dep everywhere
        ssl_ctx = None
    http = aiohttp.ClientSession()
    try:
        ws = await http.ws_connect(
            _ws_url(cfg), headers={"api-key": key}, heartbeat=30.0,
            max_msg_size=16 * 1024 * 1024,
            ssl=ssl_ctx,
        )
    except Exception:
        await http.close()
        raise
    return http, ws


class AzureVoiceLiveSession(RealtimeSession):
    provider = "azure_voice_live"
    # One function_call_output per call_id in this protocol — per-sentence
    # streamed delivery needs several, so the endpoint must not stream to us.
    supports_streamed_tool_results = False
    # An out-of-band response (`conversation: "none"`) is spoken without being
    # added to the conversation and without touching the pending call id —
    # live-verified against the service 2026-07-29. This is what lets the
    # holding line exist here despite one-result-per-call-id.
    supports_out_of_band_speech = True
    # This provider delivers the full confirmation contract (final user
    # transcripts via azure-speech + playback_flush on commit), so the
    # endpoint may defer barge-in handling to the transcript verdict.
    supports_barge_in_confirmation = True
    # Every committed caller utterance ends in a final user transcript here
    # (`conversation.item.input_audio_transcription.completed`), which is what
    # relay custody dispatches its replacement turn from, what
    # `supports_barge_in_confirmation` above is built on, and what populates
    # the endpoint's `last_commit`.
    finalizes_user_transcripts = True
    # A bare `response.create` hands the model the floor again — see
    # `reprompt()`.
    supports_reprompt = True
    # `response_in_flight` below is a real reading of this session's wire
    # (acknowledged response + our own unacknowledged create + a create
    # already committed to on the next `response.done`), so the endpoint may
    # arm the answer watchdog here: the mute race it rescues is THIS
    # protocol's — an answer's `response.create` rejected by a racing
    # response, leaving the function_call_output in the conversation with
    # nothing speaking it.
    reports_response_in_flight = True
    # Derived from the REAL declaration handed to the service — see
    # GeminiLiveSession for why this must never be hand-copied.
    tool_spec = ToolSpec(
        name=ASK_AGENT_DECLARATION["name"],
        query_arg=next(iter(ASK_AGENT_DECLARATION["parameters"]["properties"])),
    )

    def __init__(self, cfg: LiveVoiceConfig, *, persona: str) -> None:
        self._cfg = cfg
        self._persona = persona
        self.audio_format = AudioFormat(
            input_sample_rate=_RATE, output_sample_rate=_RATE
        )
        # Tracks exactly whether the session payload will carry the
        # server-side interim_response block — the endpoint skips its own
        # holding line iff the upstream fills the wait itself. Gated on
        # text_only too: _session_payload omits the block entirely in text
        # mode (no audio to fill dead air with), so this must agree or the
        # endpoint would wrongly believe Azure has it covered and skip its
        # own filler too — silently, no filler at all.
        self.handles_interim_response = (
            not cfg.text_only and _interim_response_payload(cfg) is not None
        )
        self._http: Optional[Any] = None
        self._ws: Optional[Any] = None
        self._closed = False
        self._send_lock = asyncio.Lock()
        self._response_active = False
        # WHEN_IDLE results held until response.done; flushed in _translate.
        self._idle_queue: List[Tuple[str, Dict[str, Any]]] = []
        # call_ids we already delivered a result for (one per call in this
        # protocol) and tool calls already emitted (dedupe when the server
        # reports the same call via more than one event type).
        self._delivered_calls: set = set()
        self._emitted_calls: set = set()
        # Events that arrived while start() waited for session.updated.
        self._early_events: List[RealtimeEvent] = []
        # Item ids whose user transcript streamed as deltas — their
        # `completed` must close the bubble without repeating the text.
        self._user_tx_streamed: set = set()
        # Ordered out-of-band speech queue. One OOB response at a time:
        # `speak_out_of_band` enqueues, `_pump_oob` sends the next line only
        # when no response is active AND none of ours is in flight, and
        # events() drains on every response.done. Two lines sent while a
        # response was active is exactly what produced "Conversation already
        # has an active response" in front of a merchant — the queue is what
        # lets per-sentence streaming ride out-of-band speech safely.
        self._oob_queue: List[str] = []
        # Text of the line currently in flight, so `response.created` can
        # report WHAT was actually committed to speech rather than the
        # endpoint guessing at enqueue time.
        self._oob_inflight_text: Optional[str] = None
        self._oob_inflight = False
        # True while the ACTIVE response is one of our out-of-band creates —
        # its audio must stay out of the truncate ledger below (an OOB item
        # is not in the conversation; there is nothing to truncate).
        self._response_is_oob = False
        self._pending_oob_create = False
        # Playback-truncate ledger: every forwarded audio delta advances
        # `_audio_ms_total` (24kHz mono pcm16 -> bytes/48 ms) and audio is
        # grouped into contiguous segments [(item_id|None-for-OOB, start_ms)].
        # The browser reports how many ms it actually PLAYED before a flush
        # (protocol.py's `played` frame); report_playback() maps that offset
        # onto the segment ledger and truncates the conversation item the
        # caller was actually hearing. Without this the model's context keeps
        # whole sentences the flush threw away — the service streams far
        # faster than realtime, so its own auto_truncate estimate (based on
        # what it SENT) systematically overshoots what was HEARD.
        self._audio_ms_total = 0.0
        self._audio_segments: List[Tuple[Optional[str], float]] = []
        self._truncated_items: set = set()
        # The conversation item the ACTIVE response is still generating audio
        # into, and a truncate held back because of it.
        #
        # Truncating an item MID-GENERATION does not merely repair context:
        # the service stops generating that response, mid-word, and still
        # reports it `status: "completed"`. A/B live-probed against
        # gpt-realtime on 2026-08-06, same session shape, same
        # turn_detection as a live client row:
        #
        #   control (no report_playback)   30150ms audio, counted 1..20,
        #                                  status=completed
        #   one report_playback at 1.0s     1750ms audio, transcript dies at
        #   (200ms behind the stream)       "اثنان", status=completed
        #
        # That is the whole 2026-08-06 04:13 "she stops mid-word and nothing
        # is in the logs" report: the flush that correctly cut the greeting
        # made the browser report its playback offset, and by then the reply
        # to the caller's own question had already started streaming (the
        # service runs ~4x realtime), so the offset landed inside the NEW
        # item and killed it. One confirmed cut, two casualties.
        #
        # So a truncate for the item still being generated is DEFERRED to
        # that response's response.done, where it is pure context repair and
        # cannot stop anything.
        self._active_item_id: Optional[str] = None
        self._deferred_truncate: Optional[Tuple[str, int]] = None
        # Answer-create retry bookkeeping. A tool result's response.create can
        # be rejected ("Conversation already has an active response") when a
        # response races ours — the result ITEM is already in the conversation
        # but nothing would ever speak it: dead air with no error, live-heard
        # as "the filler, then silence forever". `_answer_create_seq` records
        # the commit counter at the moment we send an answer create;
        # `_retry_answer_create` re-issues one bare response.create on the
        # next response.done, but ONLY if no utterance committed in between —
        # if the racing response was the model answering the caller's NEW
        # question, re-creating for the old answer would speak a non-sequitur
        # behind it.
        self._commit_seq = 0
        self._answer_create_seq: Optional[int] = None
        self._retry_answer_create = False
        # Confirmed-barge-in bookkeeping. `_speech_started_seq` is the commit
        # counter at the caller's most recent speech ONSET; `_response_created_seq`
        # stamps the active response with the counter at its creation. A
        # response stamped AT OR BEFORE the onset is the one the caller talked
        # over (cancellable on a confirmed barge-in); one stamped after it is
        # the service's auto-created reply to their own utterance (suppressible
        # when the utterance turns out to be noise, otherwise untouchable).
        self._speech_started_seq: Optional[int] = None
        self._response_created_seq: Optional[int] = None
        # turn_end `reply` bookkeeping (see `response.created`): whether the
        # active response is the FIRST one to answer the caller's latest
        # speech onset, and which onset was last answered that way.
        self._response_is_reply = False
        self._reply_verdict_pending = False
        self._replied_speech_seq: Optional[int] = None
        # The service's id for the response currently in flight, from
        # `response.created`. Load-bearing, not diagnostics: a bare
        # `response.cancel` names no response, and the service then applies it
        # to the DEFAULT conversation's response — an out-of-band line
        # (`conversation: "none"`) is not in that conversation and is simply
        # not cancelled. Live-probed 2026-08-05 against gpt-realtime, same
        # session, mid-line:
        #
        #   response.cancel                       2.34s MORE audio, then
        #                                         response.done status=completed
        #   response.cancel + response_id         33ms, status=cancelled,
        #                                         status_details client_cancelled
        #
        # Every cancel in this file therefore carries the id when it has one
        # (see `_cancel_frame`).
        self._active_response_id: Optional[str] = None
        # Event mirror of "no response active AND none of ours in flight" —
        # what `_await_idle` waits on. A named cancel completes in ~33ms
        # (live-probed 2026-08-05, see `_cancel_frame`), so a poll loop's
        # wake-up granularity was a real chunk of interrupt-mode delivery
        # overhead: the cancelled response's `response.done` now sets this
        # event and the waiter resolves immediately, while the 1.5s ceiling
        # in `_await_idle` stays as the fallback for a cancel the service
        # ignored (a no-op cancel produces no `response.done`, so nothing
        # sets the event and the waiter times out — exactly as before).
        # Kept in lockstep with the two flags via `_sync_idle_event()` at
        # every mutation site; starts set because the session starts idle.
        self._idle_event = asyncio.Event()
        self._idle_event.set()

    def _sync_idle_event(self) -> None:
        """Mirror the idle condition into `_idle_event`.

        Call after EVERY mutation of `_response_active` / `_oob_inflight`.
        Sync on purpose: `_translate_nosend` never awaits, and
        `asyncio.Event.set()` wakes waiters without yielding.
        """
        if self._response_active or self._oob_inflight:
            self._idle_event.clear()
        else:
            self._idle_event.set()

    async def start(self) -> None:
        try:
            self._http, self._ws = await _open_transport(self._cfg)
        except RealtimeProviderError:
            raise
        except Exception as exc:
            logger.error("azure voice live: connect failed", exc_info=True)
            raise RealtimeProviderError("realtime provider connect failed") from exc
        # Everything past this point can fail with the transport already
        # open (send failure, a rejected session.update, the 10s ack
        # timeout, or the socket closing mid-setup). The endpoint calls
        # session.start() BEFORE the try/finally that calls session.close()
        # (see endpoints/voice_live.py), so a start() that raises is never
        # cleaned up by the caller -- without this wrapper every one of
        # those paths would leak the open websocket and its
        # aiohttp.ClientSession. Mirror _open_transport's own
        # connect-failure cleanup for this later stage: close what we
        # opened, then re-raise the real error untouched.
        try:
            await self._send(_session_payload(self._cfg, self._persona))
            # Await the config ack so a rejected session surfaces here, loudly,
            # not as a mystery mid-call. Buffer anything else that arrives first.
            try:
                async with asyncio.timeout(10):
                    while True:
                        evt = await self._recv_json()
                        if evt is None:
                            raise RealtimeProviderError(
                                "azure voice live: socket closed during session setup"
                            )
                        if evt.get("type") == "session.updated":
                            return
                        if evt.get("type") == "error":
                            raise RealtimeProviderError(
                                f"azure voice live: session.update rejected: "
                                f"{(evt.get('error') or {}).get('message', evt)}"
                            )
                        self._early_events.extend(self._translate_nosend(evt))
            except TimeoutError as exc:
                raise RealtimeProviderError(
                    "azure voice live: no session.updated within 10s"
                ) from exc
        except BaseException:
            # BaseException (not just Exception): a start() cancelled mid-wait
            # must still release the transport, not just a rejection/timeout.
            # close() is idempotent and already does exactly "ws then http",
            # so reuse it rather than duplicating the cleanup here.
            await self.close()
            raise

    async def _recv_json(self) -> Optional[Dict[str, Any]]:
        msg = await self._ws.receive()
        if msg.type == aiohttp.WSMsgType.TEXT:
            try:
                return json.loads(msg.data)
            except (TypeError, ValueError):
                logger.warning("azure voice live: non-JSON text frame dropped")
                return {}
        if msg.type == aiohttp.WSMsgType.ERROR:
            # A mid-call transport failure, not a clean close -- must not be
            # conflated with CLOSE/CLOSING/CLOSED below (returning None
            # there ends events() silently, which is correct for a normal
            # disconnect but would hide a real network failure here). aiohttp
            # sets `.data` to the underlying exception for this frame type.
            raise _TransportError(str(msg.data))
        if msg.type in (
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSING,
            aiohttp.WSMsgType.CLOSED,
        ):
            # Returning None ends events() quietly, which is right -- but the
            # code/reason the service closed WITH is the only evidence of why,
            # and dropping it on the floor left "did Azure hang up mid-call?"
            # unanswerable from the logs. `close_code` is None until aiohttp
            # has processed the frame, so fall back to the frame's own data.
            logger.info(
                "azure voice live: upstream closed (type=%s code=%s reason=%r)",
                getattr(msg.type, "name", msg.type),
                getattr(self._ws, "close_code", None) or msg.data,
                getattr(msg, "extra", None),
            )
            return None
        return {}  # binary/ping/pong — nothing for us

    def _require_ws(self):
        if self._ws is None:
            raise RealtimeProviderError("session not started")
        return self._ws

    async def _send(self, payload: Dict[str, Any], *, quiet_if_closed: bool = False) -> None:
        # aiohttp does not document concurrent sends on one WS as safe, and
        # tool results are sent from a different task than the greeting/audio
        # path. Serialize them.
        async with self._send_lock:
            # Re-check _closed AFTER acquiring the lock, not just rely on a
            # caller's own check (see send_tool_result's) made before it got
            # here: close() can land in exactly that gap -- the same hang-up
            # race gemini_live handles via a narrow ConnectionClosed catch.
            # quiet_if_closed scopes the "drop at debug, don't raise"
            # treatment to tool-result sends (an already-hung-up caller is
            # expected, not a fault); a send from start()/send_audio()/etc.
            # racing a close is still a real failure and must still raise.
            if self._closed and quiet_if_closed:
                logger.debug("azure voice live: dropping send, session closed")
                return
            ws = self._ws
            if ws is None:
                if quiet_if_closed:
                    logger.debug("azure voice live: dropping send, session closed")
                    return
                raise RealtimeProviderError("session not started")
            try:
                await ws.send_str(json.dumps(payload))
            except Exception as exc:
                if self._closed and quiet_if_closed:
                    logger.debug(
                        "azure voice live: send raced a session close", exc_info=True
                    )
                    return
                raise RealtimeProviderError("realtime provider send failed") from exc

    async def send_audio(self, pcm: bytes) -> None:
        await self._send({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm).decode("ascii"),
        })

    async def send_text_turn(self, text: str) -> None:
        # A delivered text turn (relay custody's say-exactly answer) ends the
        # wait any queued holding line was filling — same rule as a delivered
        # tool result below. Left queued, the line drains on the ANSWER's own
        # response.done and is spoken right after it.
        self._oob_queue.clear()
        await self._send({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        })
        # This create supersedes any armed answer-create marker: if IT
        # collides ("already has an active response"), retrying a bare
        # response.create would re-read the LAST answer — heard live as the
        # same OTP announcement spoken twice in a relay-heavy session.
        self._answer_create_seq = None
        await self._send({"type": "response.create"})

    async def send_tool_result(
        self, call_id: str, payload: Dict[str, Any], *, mode: ToolResultMode
    ) -> None:
        if not isinstance(mode, ToolResultMode):
            raise TypeError(f"mode must be a ToolResultMode, got {type(mode).__name__}")
        if self._closed:
            # Caller hung up while the delegated turn ran — expected race,
            # same as gemini_live. Debug, not error.
            logger.debug(
                "azure voice live: dropping tool result for %s, session closed", call_id
            )
            return
        if call_id in self._delivered_calls:
            raise RealtimeProviderError(
                f"azure_voice_live accepts one tool result per call "
                f"(got a second for {call_id!r}) — per-sentence streaming is "
                "unsupported here; supports_streamed_tool_results is False"
            )
        self._delivered_calls.add(call_id)
        # A delivered result ends the wait it was holding for: any holding
        # line still queued would otherwise drain at the NEXT response.done —
        # i.e. be spoken right after (or instead of) the answer. Both live
        # symptoms of "it keeps filling the silence even when the answer is
        # here" trace to this queue surviving the delivery: the INTERRUPT
        # path's own cancel opens an idle window (the cancelled response's
        # response.done) that _pump_oob immediately claims, and the answer's
        # create then collides with the stale line's. Cleared for every mode:
        # a SILENT delivery means the caller moved on, where a held "still
        # working on that" is an even worse non-sequitur.
        self._oob_queue.clear()
        # `_oob_inflight` counts as an active response in BOTH mode checks
        # below: a holding line's response.create that is still unacknowledged
        # (one RTT) would otherwise collide with the answer's — the service
        # rejects the ANSWER's create ("Conversation already has an active
        # response"), and the caller hears the filler and then nothing.
        if mode is ToolResultMode.WHEN_IDLE and (
            self._response_active or self._oob_inflight
        ):
            self._idle_queue.append((call_id, payload))
            return
        # quiet_if_closed=True on every send from here down: the check above
        # only catches a hang-up that already happened BEFORE this call --
        # one that lands mid-flight (between here and the network write) must
        # drop the same way, not surface as a RealtimeProviderError the
        # endpoint logs a full traceback for (see _send).
        if mode is ToolResultMode.INTERRUPT and (
            self._response_active or self._oob_inflight
        ):
            # The websocket is ordered, so this cancel always arrives AFTER
            # any unacknowledged create it is racing — the service applies it
            # to that response. If the response already ended, the service
            # answers "Cancellation failed: no active response", absorbed as
            # bookkeeping in _translate_nosend.
            #
            # One wrinkle the id makes visible: when OUR create is still in
            # flight (`_oob_inflight` and no `response.created` yet) there is
            # no id to name, so the cancel would be the useless bare frame
            # again. The id is one RTT away (measured 60-100ms) — wait for it,
            # briefly, and cancel the line the caller is about to hear instead
            # of racing it.
            if self._oob_inflight and not self._active_response_id:
                await self._await_response_id(timeout=0.5)
            await self._send(self._cancel_frame(), quiet_if_closed=True)
            # Cancel is async — wait for it to land before creating the next
            # response, or the service rejects the create outright.
            await self._await_idle()
        await self._send_result_item(call_id, payload)
        if mode is not ToolResultMode.SILENT:
            self._answer_create_seq = self._commit_seq
            await self._send({"type": "response.create"}, quiet_if_closed=True)

    def _cancel_frame(self) -> Dict[str, Any]:
        """A `response.cancel` that names the response it means to stop.

        Without `response_id` the service cancels the DEFAULT conversation's
        in-progress response — which an out-of-band holding line
        (`conversation: "none"`) is not. Live-probed 2026-08-05 (gpt-realtime,
        same line, cancelled 0.6s in):

            bare                2.34s more audio, response.done COMPLETED
            + response_id       0.03s, response.done CANCELLED/client_cancelled

        and the same frame cancels an in-conversation response just as well
        (probed separately: 34ms, cancelled, and the next `response.create` is
        accepted immediately). That single missing field is what made every
        answer delivered during a holding line wait out the whole line: the
        cancel did nothing, `_await_idle` burned its full 1.5s (measured live:
        1318ms / 1519ms), the answer's `response.create` came back
        "Conversation already has an active response", and the answer only
        spoke on `_retry_answer_create` once the line finished by itself.

        Falls back to the bare frame when no id is known yet (our create is
        still in flight): the service ignores it for an OOB response, but it
        is the pre-2026-08-05 behaviour and never worse than sending nothing.
        """
        frame: Dict[str, Any] = {"type": "response.cancel"}
        if self._active_response_id:
            frame["response_id"] = self._active_response_id
        return frame

    @property
    def response_in_flight(self) -> bool:
        """See `RealtimeSession.response_in_flight`.

        `_oob_inflight` and `_retry_answer_create` count as in flight
        alongside `_response_active`: the first is a create of ours the
        service has not acknowledged yet, the second an answer create this
        provider has already promised to issue on the next `response.done`.
        Both mean audio is coming, which is exactly what the endpoint's answer
        watchdog must not mistake for a mute answer.
        """
        return bool(
            self._response_active or self._oob_inflight or self._retry_answer_create
        )

    async def _await_idle(self, timeout: float = 1.5) -> bool:
        """Wait (briefly) for the current response to finish.

        `response.cancel` is asynchronous — creating the next response before
        the service has finished cancelling gets "Conversation already has an
        active response", which surfaced as an error bubble in a live call.

        Event-driven, not polled: a named cancel's `response.done` lands in
        ~33ms (live-probed 2026-08-05, see `_cancel_frame`), and until
        2026-08-06 this loop slept in 50ms ticks on top of that — pure
        wake-up latency inside every interrupt-mode delivery. The pump's
        `_translate_nosend` sets `_idle_event` the moment `response.done`
        clears the flags, so the waiter resolves immediately. The `timeout`
        ceiling is strictly the fallback for a cancel the service IGNORED
        (bare/no-op cancel → no `response.done` → nothing sets the event):
        that path must still wait it out here rather than proceed into a
        rejected create — do not shorten it.

        The loop re-checks the flags after every wake: `Event.wait()`
        resolves waiters at `set()` time, so a set immediately followed by a
        clear (response.done → a new response.created) can wake us into a
        busy floor — loop and wait again on the remaining budget.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._response_active or self._oob_inflight:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(self._idle_event.wait(), remaining)
            except asyncio.TimeoutError:
                break
        return not (self._response_active or self._oob_inflight)

    async def _await_response_id(self, timeout: float = 0.5) -> bool:
        """Wait (briefly) for `response.created` to name the response we sent.

        Only ever called on the create->created window of one of OUR creates,
        where the alternative is a cancel that names nothing and is therefore
        ignored (see `_cancel_frame`). Bounded low: the window measured
        60-100ms live, and a cancel is worth at most a fraction of the wait it
        is trying to save.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while (
            self._active_response_id is None
            and not self._closed
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.02)
        return self._active_response_id is not None

    def _first_caption(self, event: RealtimeEvent) -> RealtimeEvent:
        """Stamp the response's reply verdict on its first assistant caption."""
        if self._reply_verdict_pending:
            self._reply_verdict_pending = False
            event.turn_reply = self._response_is_reply
        return event

    async def cancel_active_response(self) -> None:
        # Same cancel-then-settle dance send_tool_result's INTERRUPT mode
        # does: response.cancel is asynchronous, and acting before the
        # service finishes cancelling gets "Conversation already has an
        # active response" on the next create.
        if self._response_active:
            await self._send(self._cancel_frame(), quiet_if_closed=True)
            await self._await_idle()

    async def cancel_barged_in_response(self) -> None:
        # Guarded cancel: only the response the caller talked over. See the
        # bookkeeping comment in __init__ — a stamp AFTER the speech onset
        # means the active response is the service's reply to the caller's
        # own utterance, which must never be killed by a late confirmation.
        if not self._response_active or self._speech_started_seq is None:
            return
        if (
            self._response_created_seq is not None
            and self._response_created_seq > self._speech_started_seq
        ):
            return
        # Send and RETURN — deliberately no `_await_idle()` settle here.
        #
        # The settle exists for one reason: a `response.create` of OURS that
        # would collide with a still-cancelling response ("Conversation
        # already has an active response"). Nothing follows this cancel — the
        # endpoint calls it purely to stop her talking — so there is nothing
        # to protect. The websocket is ordered, so the cancel still arrives
        # after any create it races, and if the response already ended the
        # service's "Cancellation failed: no active response" is absorbed as
        # bookkeeping in _translate_nosend.
        #
        # Settling here is actively HARMFUL: the endpoint awaits this from
        # inside its `async for event in session.events()` body, and
        # `_response_active` is only ever cleared by `_translate_nosend`,
        # which runs when the pump pulls the NEXT event. Awaiting the flag to
        # change while holding the pump can never succeed — it would burn the
        # full 1.5s timeout every single time, delaying the barge-in flush
        # frame and stalling all audio/transcript forwarding behind it.
        #
        # The id is what makes the cancel bite (see `_cancel_frame`) — and
        # this path is the one the CALLER feels: without it, a caller talking
        # over a holding line could not stop it at all.
        await self._send(self._cancel_frame(), quiet_if_closed=True)

    async def suppress_spurious_reply(self) -> None:
        # The mirror guard: only a reply created after the (noise) utterance's
        # speech onset. Her own in-flight response — the answer the held
        # flush is protecting — has an older stamp and survives.
        if not self._response_active or self._speech_started_seq is None:
            return
        if self._response_is_oob:
            # An out-of-band response (holding line) is never "the reply to
            # a caller turn" — it has conversation:"none" and may legally be
            # created after the noise's onset (the endpoint's holding-line
            # timer fires on its own clock). Killing it here would silence
            # the one thing the caller should hear during a delegated wait.
            return
        if (
            self._answer_create_seq is not None
            and self._answer_create_seq == self._commit_seq
        ):
            # A delegated answer's response.create is in flight at this
            # commit level — it is stamped identically to the service's
            # auto-reply, and cancelling it here would eat the answer the
            # caller is waiting on. The verdict racing an answer is rare
            # (the answer usually takes ~10s); losing one suppression is
            # cheaper than losing one answer.
            return
        if (
            self._response_created_seq is None
            or self._response_created_seq <= self._speech_started_seq
        ):
            return
        # A cancelled spurious reply answers nothing: its turn_end must not
        # claim `reply` (the client would open a late-transcript window on it
        # and detach the real answer from its question). `_replied_speech_seq`
        # stays at the noise onset, so the delegated answer that follows for
        # the ORIGINAL question stays a continuation (reply=False) and merges
        # into that question's turn.
        self._response_is_reply = False
        # No `_await_idle()` settle, for exactly the reasons spelled out in
        # cancel_barged_in_response above: nothing of ours follows this
        # cancel, and this is awaited from inside the endpoint's pump body —
        # where `_response_active` cannot change, so a settle would always
        # burn its whole 1.5s timeout and freeze the event loop's only reader.
        await self._send(self._cancel_frame(), quiet_if_closed=True)

    async def reprompt(self) -> bool:
        """Hand the model the floor again: a bare `response.create`.

        No `response` body at all, deliberately. Per-response overrides were
        live-probed against gpt-realtime on 2026-08-05 and this service
        IGNORES them: a create carrying `tool_choice: "required"` (and one
        naming `ask_agent` outright) came back `response.created` with
        `tool_choice: null` and produced a plain message — no function_call.
        The bare create, on the same stalled conversation, produced the owed
        `ask_agent` call in 0.37s with the caller's question already in its
        `query`. So the model does not need TELLING what it owes; it needs a
        turn in which to owe it. Anything we add to this frame is at best
        inert and at worst (an `instructions` override) replaces the persona
        for that response — the 2026-08-05 French-holding-line failure.

        Nothing is added to the conversation, so the model sees exactly the
        history it stalled on. Refused when the floor is busy: a create that
        collides gets "Conversation already has an active response" and, on
        the stall path, the endpoint's recovery is the correct fallback.
        """
        if self._closed or self._response_active or self._oob_inflight:
            return False
        # NOT stamped as an answer create (`_answer_create_seq`): this is the
        # model's own turn, not a delivery of ours, and stamping it would make
        # suppress_spurious_reply spare it as "the answer in flight".
        await self._send({"type": "response.create"}, quiet_if_closed=True)
        return True

    async def speak_out_of_band(self, text: str) -> None:
        """Speak `text` via an out-of-band response, in order.

        `conversation: "none"` keeps it out of the conversation, so it can be
        said WHILE a tool call is pending without consuming that call id and
        without teaching the model that it already answered.

        Used ONLY for short standalone lines (the holding line) — never
        answer sentences, which are delivered as the closing in-conversation
        tool result so the model's history contains what it said (see
        endpoints/voice_live.py, 2026-07-31). Lines are QUEUED, not dropped
        (the earlier skip-when-active behaviour silently swallowed a line
        that arrived while she was talking). One line is in flight at a time;
        events() drains the next on each response.done, so sentences are
        spoken strictly in the order they arrived. A barge-in or a committed
        caller utterance clears the queue — see _translate_nosend.
        """
        self._oob_queue.append(text)
        await self._pump_oob()

    async def _pump_oob(self) -> None:
        """Send the next queued out-of-band line iff the line is clear.

        `_oob_inflight` covers the window between our response.create and the
        server's response.created — without it two rapid sentences would both
        see `_response_active == False` and collide. quiet_if_closed: a
        sentence racing a hang-up is not a fault.
        """
        if self._response_active or self._oob_inflight or not self._oob_queue:
            return
        text = self._oob_queue.pop(0)
        self._oob_inflight_text = text
        self._oob_inflight = True
        self._sync_idle_event()
        self._pending_oob_create = True
        try:
            await self._send(
                {
                    "type": "response.create",
                    "response": {
                        "conversation": "none",
                        # NOT context-free (`input: []` was tried on 2026-07-31
                        # and reverted the same day): this service does not
                        # treat per-response `instructions` as a clean
                        # override, so an empty input made every out-of-band
                        # line look like the FIRST reply of a fresh call and
                        # the persona re-greeted the caller instead of saying
                        # the line. Out-of-band speech is now used ONLY for
                        # short standalone lines (the holding line), never for
                        # answer sentences — the answer travels as the closing
                        # in-conversation tool result (see voice_live.py).
                        # AUDIO ONLY, deliberately. A native-audio model rejects
                        # ["text","audio"] on a response ("Supported combinations
                        # are: ['text'] and ['audio']") and the whole holding line
                        # then errors mid-turn; a spoken filler needs no transcript
                        # anyway, and audio-only is accepted by both model kinds.
                        "modalities": ["audio"],
                        # LANGUAGE ANCHOR. Per-response `instructions`
                        # REPLACE the session instructions, and
                        # `conversation: "none"` strips the context, so
                        # this response carries no persona and no language
                        # unless it is stated right here. Without it the
                        # model picks one: a live English call on
                        # 2026-08-05 had its holding line read out in
                        # FRENCH. The text is already in the right
                        # language; this stops the reading drifting.
                        "instructions": (
                            f"Say exactly this, in {_lang_name(self._cfg)}, "
                            f"and nothing else: {text}"
                        ),
                    },
                },
                quiet_if_closed=True,
            )
        except BaseException:
            # A failed send must not wedge the queue closed forever.
            self._oob_inflight = False
            self._sync_idle_event()
            self._pending_oob_create = False
            raise

    async def report_playback(self, played_total_ms: float) -> None:
        """Truncate context to what the caller actually HEARD.

        `played_total_ms` is the offset into the forwarded audio stream that
        the browser's playback had reached when it flushed (protocol.py's
        `played` frame) — earlier-flushed audio stays counted on both sides,
        which is what keeps the offset aligned with `_audio_ms_total` here. The service streams a reply far
        faster than realtime, so a flush can throw away whole sentences the
        model believes it said. Map the played offset onto the audio-segment
        ledger and `conversation.item.truncate` the item the caller was
        hearing at that moment — items the flush skipped entirely are left
        alone (recorded limitation; barge-in across an item boundary
        mid-buffer is rare).
        """
        if played_total_ms is None or played_total_ms < 0:
            return
        segments = self._audio_segments
        if not segments or played_total_ms >= self._audio_ms_total - 1.0:
            # Heard everything (a flush after playback caught up) — nothing
            # to repair.
            return
        # The segment containing the played offset: last one starting at or
        # before it.
        item_id: Optional[str] = None
        seg_start = 0.0
        seg_end = self._audio_ms_total
        for i, (seg_item, start_ms) in enumerate(segments):
            if start_ms <= played_total_ms:
                item_id = seg_item
                seg_start = start_ms
                seg_end = (
                    segments[i + 1][1] if i + 1 < len(segments) else self._audio_ms_total
                )
            else:
                break
        if item_id is None:
            # Offset falls in out-of-band audio (holding line / streamed
            # sentence) — not a conversation item, nothing to truncate.
            return
        if item_id in self._truncated_items:
            return
        audio_end_ms = int(played_total_ms - seg_start)
        if audio_end_ms >= int(seg_end - seg_start) - 1:
            return  # heard this item fully
        if self._response_active and item_id == self._active_item_id:
            # This item is STILL BEING GENERATED. Truncating it now stops the
            # service mid-word (see __init__'s ledger comment for the live
            # A/B) — and the caller is waiting on exactly this reply, because
            # a flush lands on the in-flight item only when the reply to
            # their own utterance has already started streaming. Hold the
            # repair until response.done; a later report for the same item
            # refines the offset upward (more heard) until then.
            prev = self._deferred_truncate
            if prev is None or prev[0] != item_id or audio_end_ms > prev[1]:
                self._deferred_truncate = (item_id, audio_end_ms)
                logger.info(
                    "azure voice live: playback truncate deferred, item %s "
                    "is still generating (audio_end_ms=%d)",
                    item_id, audio_end_ms,
                )
            return
        await self._send_truncate(item_id, audio_end_ms)

    async def _send_truncate(self, item_id: str, audio_end_ms: int) -> None:
        self._truncated_items.add(item_id)
        await self._send(
            {
                "type": "conversation.item.truncate",
                "item_id": item_id,
                "content_index": 0,
                "audio_end_ms": audio_end_ms,
            },
            quiet_if_closed=True,
        )

    async def _send_result_item(self, call_id: str, payload: Dict[str, Any]) -> None:
        # Always quiet_if_closed: the only two callers are send_tool_result
        # (above) and the WHEN_IDLE flush in events() below -- both are
        # tool-result delivery, where a hang-up mid-flight is an expected
        # race, not a fault.
        await self._send({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(payload, ensure_ascii=False),
            },
        }, quiet_if_closed=True)

    async def events(self) -> AsyncIterator[RealtimeEvent]:
        self._require_ws()
        for event in self._early_events:
            yield event
        self._early_events = []
        try:
            while not self._closed:
                evt = await self._recv_json()
                if evt is None:
                    return
                outs = self._translate_nosend(evt)
                # Flushing WHEN_IDLE results happens outside translation so
                # translation stays pure/sync and testable — and BEFORE the
                # yields below: a yield suspends this generator until the
                # consumer asks again, and the next queued send must not wait
                # on the consumer's pace.
                if evt.get("type") == "response.done":
                    # The item that just finished can now be truncated safely:
                    # the repair `report_playback` held back (it would have
                    # stopped this very response mid-word) costs nothing here.
                    # Sent BEFORE any response.create below so the model's
                    # context is already trimmed to what the caller heard when
                    # it takes its next turn.
                    deferred, self._deferred_truncate = self._deferred_truncate, None
                    if deferred is not None and deferred[0] not in self._truncated_items:
                        await self._send_truncate(deferred[0], deferred[1])
                    if self._idle_queue:
                        queued, self._idle_queue = self._idle_queue, []
                        for call_id, payload in queued:
                            await self._send_result_item(call_id, payload)
                        # quiet_if_closed: same tool-result-delivery reasoning as
                        # send_tool_result above -- this is that same flow,
                        # resumed here once the model goes idle.
                        self._answer_create_seq = self._commit_seq
                        self._retry_answer_create = False
                        await self._send({"type": "response.create"}, quiet_if_closed=True)
                    elif self._retry_answer_create:
                        # An answer's create was rejected by a racing response
                        # (see the retry bookkeeping in __init__) — the racing
                        # response just finished, so re-issue the create now.
                        # The result item is already in the conversation; a
                        # bare create is all that's missing for it to be
                        # spoken.
                        self._retry_answer_create = False
                        self._answer_create_seq = self._commit_seq
                        await self._send({"type": "response.create"}, quiet_if_closed=True)
                    else:
                        # This response completing is also the end of any
                        # answer-create the marker was tracking — a stale
                        # marker must not vouch for some LATER collision.
                        self._answer_create_seq = None
                        # The line is clear — speak the next queued out-of-band
                        # sentence, if any. Skipped when the idle flush above
                        # just created a response (the queues never co-occur on
                        # this provider today, but ordering must not depend on
                        # that staying true).
                        await self._pump_oob()
                for out in outs:
                    yield out
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._closed:
                return
            logger.error("azure voice live: receive failed", exc_info=True)
            yield RealtimeEvent(type="error", message=str(exc))

    def _translate_nosend(self, evt: Dict[str, Any]) -> List[RealtimeEvent]:
        """Map one server event onto zero or more provider-neutral events.
        Pure state-machine translation; never awaits."""
        out: List[RealtimeEvent] = []
        etype = evt.get("type") or ""

        if etype == "response.created":
            self._response_active = True
            self._sync_idle_event()
            self._response_created_seq = self._commit_seq
            self._active_response_id = (evt.get("response") or {}).get("id") or None
            # Label the response so its audio can be kept out of the truncate
            # ledger: our out-of-band creates set _pending_oob_create right
            # before sending. (A server-created response racing into that
            # window would be mislabelled — accepted: the window is one RTT
            # and the failure mode is a skipped truncate, not a wrong one.)
            self._response_is_oob = self._pending_oob_create
            self._pending_oob_create = False
            # The turn_end `reply` flag for this response: the SAME test the
            # audio deltas' `reply` uses (created after the caller's latest
            # speech onset, and not out-of-band), narrowed to the FIRST such
            # response per onset. The answer created after an announce line's
            # tool call, a retried answer-create and a stall recovery all
            # postdate that same onset too, but they continue a turn the first
            # response already answered — reporting them as replies would make
            # the client open a second late-transcript window and glue the
            # caller's NEXT utterance onto the continuation.
            #
            # Known race (accepted, one RTT wide): if the caller's NEXT onset
            # is stamped before `response.created` for the previous commit
            # arrives, that reply is stamped `created_seq == speech_seq` and
            # reports False, so the client merges it into the previous turn.
            self._response_is_reply = (
                not self._response_is_oob
                and self._speech_started_seq is not None
                and self._response_created_seq > self._speech_started_seq
                and self._speech_started_seq != self._replied_speech_seq
            )
            if self._response_is_reply:
                self._replied_speech_seq = self._speech_started_seq
            # The verdict rides this response's FIRST assistant caption too.
            self._reply_verdict_pending = not self._response_is_oob
            if self._response_is_oob and self._oob_inflight_text:
                # The service ACCEPTED this line, so it will be spoken.
                # Until now the endpoint emitted its transcript at enqueue
                # time, which lies whenever the queue is cleared first
                # (barge-in, a committed utterance, a playback flush) --
                # the caller saw a line they never heard.
                out.append(RealtimeEvent(
                    type='out_of_band_started',
                    text=self._oob_inflight_text,
                ))
                self._oob_inflight_text = None
        elif etype == "response.done":
            self._response_active = False
            self._oob_inflight = False
            # The wake `_await_idle` is blocked on: this is what lets a
            # cancelled response's `response.done` (~33ms after a named
            # cancel) release the tool-result delivery immediately instead
            # of it sleeping out a poll tick.
            self._sync_idle_event()
            self._response_is_oob = False
            self._active_response_id = None
            self._active_item_id = None
            # A response that did NOT end normally is the one thing the caller
            # feels and our logs never carried: the 2026-08-06 04:13 call
            # reported two replies stopping mid-word with no flush, no
            # truncate, no cancel and no barge-in anywhere in the engine log,
            # because `status` was thrown away right here.
            #
            # It does NOT name the defect that call turned out to have — a
            # self-inflicted in-flight truncate still reports
            # `status: "completed"` (live-probed 2026-08-06; see the ledger
            # comment in __init__). It rules the OTHER family out in one line
            # instead of a day: a content-filtered, token-capped or genuinely
            # cancelled response says so here and nowhere else.
            status = str((evt.get("response") or {}).get("status") or "")
            if status and status != "completed":
                logger.info(
                    "azure voice live: response ended status=%s details=%s",
                    status, (evt.get("response") or {}).get("status_details"),
                )
            out.append(RealtimeEvent(type="turn_end", turn_reply=self._response_is_reply))
            self._response_is_reply = False
        elif etype == "response.audio.delta":
            data = evt.get("delta") or ""
            try:
                pcm = base64.b64decode(data)
            except (TypeError, ValueError):
                pcm = b""
            if pcm:
                # Ledger for report_playback: EVERY forwarded delta advances
                # the total (the browser plays one undifferentiated stream),
                # grouped into segments keyed by conversation item — None for
                # out-of-band audio, which has no item to truncate.
                seg_item = None if self._response_is_oob else (evt.get("item_id") or None)
                # The item this response is still generating into — the one
                # `report_playback` must never truncate while it streams.
                self._active_item_id = seg_item
                if not self._audio_segments or self._audio_segments[-1][0] != seg_item:
                    self._audio_segments.append((seg_item, self._audio_ms_total))
                self._audio_ms_total += len(pcm) / 48.0  # 24kHz mono pcm16
                out.append(RealtimeEvent(
                    type="audio", audio=pcm,
                    # Reply audio = the active response postdates the caller's
                    # speech onset. OOB audio is never a reply.
                    reply=(
                        not self._response_is_oob
                        and self._speech_started_seq is not None
                        and self._response_created_seq is not None
                        and self._response_created_seq > self._speech_started_seq
                    ),
                ))
        elif etype == "response.audio_transcript.delta":
            # Out-of-band speech is logged by the endpoint's own explicit
            # frame (it knows the exact sentence it asked for); forwarding
            # the service's transcript of that audio as well interleaves a
            # second delta stream into whatever bubble the client has open —
            # seen live as one garbled answer with fragments of another
            # spliced mid-sentence.
            if not self._response_is_oob:
                text = evt.get("delta") or ""
                if text:
                    out.append(self._first_caption(RealtimeEvent(
                        type="transcript", role="assistant", text=text, final=False)))
        elif etype == "response.audio_transcript.done":
            # Deltas already carried the text — final=True with empty text
            # closes the client's streaming bubble without doubling it
            # (same convention as gemini_live's finished-but-textless case).
            # Suppressed for out-of-band audio like the deltas above: there
            # is no bubble of ours to close, only someone else's to corrupt.
            if not self._response_is_oob:
                out.append(RealtimeEvent(type="transcript", role="assistant", text="", final=True))
        elif etype == "response.text.delta":
            # TEXT-modality responses (text_only sessions) stream here
            # instead of response.audio_transcript.delta. Same out-of-band
            # suppression: an OOB line's text is not a bubble of ours.
            if not self._response_is_oob:
                text = evt.get("delta") or ""
                if text:
                    out.append(self._first_caption(RealtimeEvent(
                        type="transcript", role="assistant", text=text, final=False)))
        elif etype == "response.text.done":
            # Same bubble-closing convention as response.audio_transcript.done
            # above: deltas already carried the text.
            if not self._response_is_oob:
                out.append(RealtimeEvent(type="transcript", role="assistant", text="", final=True))
        elif etype == "conversation.item.input_audio_transcription.delta":
            item_id = evt.get("item_id") or ""
            self._user_tx_streamed.add(item_id)
            text = evt.get("delta") or ""
            if text:
                out.append(RealtimeEvent(type="transcript", role="user", text=text, final=False))
        elif etype == "conversation.item.input_audio_transcription.completed":
            item_id = evt.get("item_id") or ""
            text = evt.get("transcript") or ""
            if item_id in self._user_tx_streamed:
                self._user_tx_streamed.discard(item_id)
                out.append(RealtimeEvent(type="transcript", role="user", text="", final=True))
            else:
                out.append(RealtimeEvent(type="transcript", role="user", text=text, final=True))
        elif etype == "conversation.item.input_audio_transcription.failed":
            # The caller's transcript for this item will never come. Without
            # a closing signal a client holding a late-transcript window for
            # it (turn_end `reply`) would swallow the NEXT utterance's words.
            # A dedicated event, not an empty user `final`: the endpoint reads
            # an empty final as a noise verdict (drops the held flush, may
            # cancel the reply), which a transcription outage must not do.
            item_id = evt.get("item_id") or ""
            self._user_tx_streamed.discard(item_id)
            logger.info(
                "azure voice live: input transcription failed item=%s error=%s",
                item_id, evt.get("error"),
            )
            out.append(RealtimeEvent(type="user_transcript_failed"))
        elif etype == "input_audio_buffer.speech_started":
            # Stamp the onset FIRST, unconditionally -- both arms below
            # (barge-in detected, or idle) mark the caller having started to
            # speak, and the confirmed-barge-in guards (cancel_barged_in_
            # response / suppress_spurious_reply) key off this stamp
            # regardless of which arm fired.
            self._speech_started_seq = self._commit_seq
            # Only while SHE is speaking. Firing on every detection made her
            # cut herself off: her own voice through the caller's speaker (and
            # any room blip) trips speech_started, the client flushes
            # playback, and she stops mid-sentence — reported as "it keeps
            # interrupting itself".
            if self._response_active:
                # Queued out-of-band sentences belong to the answer the
                # caller just talked over — speaking the tail of an abandoned
                # answer after their NEW question is a spoken non-sequitur.
                # The full answer still reaches the model's context via the
                # endpoint's closing SILENT tool result.
                self._oob_queue.clear()
                out.append(RealtimeEvent(type="interrupted"))
            else:
                # Detected while the line was idle: still not a barge-in, and
                # deliberately still no behaviour — but no longer invisible.
                # Live sessions reported barge_ins=0 while callers insisted
                # they had talked over her, and with this branch silent we
                # could not tell "the service never detects the caller's
                # speech on real hardware" from "detection works, it just
                # never lands mid-response". This event is a pure counter for
                # voice/live/metrics.py; the endpoint forwards nothing for it.
                out.append(RealtimeEvent(type="speech_started_idle"))
        elif etype == "input_audio_buffer.speech_stopped":
            # The caller STOPPED talking (VAD's own end, before the commit's
            # silence window closes). A pure boundary for the endpoint's
            # sustained-speech cut (`barge_in_sustain_sec`): an onset that
            # stops before the sustain window was a blip, not a talk-over.
            out.append(RealtimeEvent(type="speech_stopped"))
        elif etype == "input_audio_buffer.committed":
            # Advance the commit counter FIRST: the answer-create retry (see
            # __init__) must never fire across a committed utterance — the
            # caller moved on, and their new turn owns the line.
            self._commit_seq += 1
            # The caller's end-of-turn, stamped BEFORE the flush event below so
            # the metrics collector reads EOT ahead of any interrupt handling.
            # This is the provider's real EOT signal (semantic VAD closed the
            # turn), which is what eot_to_first_audio_ms measures from.
            out.append(RealtimeEvent(type="user_turn_end"))
            # A COMMITTED utterance is the caller definitely having spoken, not
            # a blip, so playback must flush: the service streams a reply far
            # faster than realtime, response.done can fire while the browser is
            # still playing, and without this the tail of the previous answer
            # plays over the caller's next question.
            #
            # But it is NOT a barge-in, and reporting it as one cost an answer
            # per call. The endpoint bumps its barge-in epoch on `interrupted`,
            # and a bumped epoch downgrades the in-flight delegated answer to
            # SILENT — so any sound the caller made during the ~10s turn (an
            # "mhm", a room noise loud enough to commit) meant the answer they
            # had just asked for was never spoken. `playback_flush` keeps the
            # flush and drops the false intent; see voice/live/base.py.
            #
            # Queued out-of-band sentences are cleared here too — semantic VAD
            # will auto-create the reply response for this committed
            # utterance, and a stale queued sentence draining right after it
            # would either collide with that response or get spoken as a
            # non-sequitur behind it. (Unlike `interrupted`, this does NOT
            # downgrade the in-flight delegated answer — its closing tool
            # result still lands normally.)
            self._oob_queue.clear()
            out.append(RealtimeEvent(type="playback_flush"))
        elif etype == "response.function_call_arguments.done":
            out.extend(self._tool_call_event(
                evt.get("call_id") or "", evt.get("name") or "", evt.get("arguments")
            ))
        elif etype == "response.output_item.done":
            item = evt.get("item") or {}
            if item.get("type") == "function_call":
                out.extend(self._tool_call_event(
                    item.get("call_id") or "", item.get("name") or "", item.get("arguments")
                ))
        elif etype == "error":
            message = str((evt.get("error") or {}).get("message") or "provider error")
            # A conversation.item.truncate that races the service's own
            # auto_truncate (kept ON as a fallback for clients that never
            # report playback) can be rejected — that rejection is expected
            # bookkeeping, not a call-visible fault. Absorb ONLY
            # truncate-shaped errors; everything else still surfaces.
            lowered = message.lower()
            if (
                "truncat" in lowered
                or "audio_end_ms" in lowered
                # Same rejection, third wording — "Audio content of 1450ms is
                # already shorter than 2309ms" reached a live caller because
                # it names neither "truncate" nor "audio_end_ms".
                or ("audio content of" in lowered and "shorter than" in lowered)
            ):
                logger.debug("azure voice live: truncate rejected: %s", message)
            elif "already has an active response" in lowered:
                # The documented two-response.create race (see send_tool_result
                # above) — previously "riskier to absorb than the bug until a
                # probe captures the exact error code". A live call captured
                # it verbatim on 2026-07-31, rendered to the caller as a raw
                # "Conversation already has an active response" line. The
                # rejected create is bookkeeping — but a rejected ANSWER
                # create means the caller never hears the answer, so if the
                # marker vouches for one (and no utterance committed since it
                # was sent), schedule a re-issue for the racing response's
                # response.done (see events()).
                if (
                    self._answer_create_seq is not None
                    and self._answer_create_seq == self._commit_seq
                ):
                    self._retry_answer_create = True
                logger.info("azure voice live: duplicate response.create rejected: %s", message)
            elif "cancellation failed" in lowered:
                # INTERRUPT sends response.cancel whenever a response MAY be
                # in flight; if it already ended, the service answers
                # "Cancellation failed: no active response". Microsoft's own
                # function-calling sample swallows exactly this error --
                # bookkeeping, never a caller-visible fault.
                logger.debug("azure voice live: cancel raced idle: %s", message)
            elif "tool call id" in lowered and "not found" in lowered:
                # The closing tool result was posted after the service had
                # already dropped that call's function_call item (a committed
                # utterance superseded the response mid-call). Bookkeeping —
                # a live call surfaced it as three raw API-error bubbles.
                #
                # This USED to reason "the caller already heard the streamed
                # answer; only the model's context copy is lost". That holds
                # only when the answer actually streamed. On 2026-08-05 a live
                # call hit it with `streamed=0` three times over: nothing had
                # reached the caller, the service had discarded the call, and
                # the answer watchdog then re-issued the text as a say-exactly
                # turn against a conversation with no matching function_call —
                # so the model improvised around it and replayed a mangled
                # splice of the previous answer and the holding line.
                #
                # So report it rather than swallowing it. The endpoint stands
                # its watchdog down for this call: an answer the service has
                # dropped cannot be rescued by saying it again, and the stall
                # tiers are the path that can still recover the turn.
                logger.info("azure voice live: tool result dropped: %s", message)
                dropped_call = re.search(r"'([^']+)'", message)
                if dropped_call:
                    out.append(RealtimeEvent(
                        type="tool_result_dropped",
                        call_id=dropped_call.group(1),
                    ))
            else:
                out.append(RealtimeEvent(type="error", message=message))
        return out

    def _tool_call_event(self, call_id: str, name: str, raw_args: Any) -> List[RealtimeEvent]:
        # The server may report one call through both
        # response.function_call_arguments.done and response.output_item.done
        # — emit it once.
        if not call_id or call_id in self._emitted_calls:
            return []
        self._emitted_calls.add(call_id)
        args: Dict[str, Any] = {}
        if isinstance(raw_args, dict):
            args = raw_args
        elif isinstance(raw_args, str) and raw_args:
            try:
                parsed = json.loads(raw_args)
                if isinstance(parsed, dict):
                    args = parsed
            except (TypeError, ValueError):
                logger.warning(
                    "azure voice live: malformed tool arguments for %s", call_id
                )
        return [RealtimeEvent(
            type="tool_call", call_id=call_id, tool_name=name, arguments=args
        )]

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                logger.debug("azure voice live: ws close raised", exc_info=True)
        if self._http is not None:
            try:
                await self._http.close()
            except Exception:
                logger.debug("azure voice live: http close raised", exc_info=True)
        self._ws = None
        self._http = None


def build_azure_voice_live(cfg: LiveVoiceConfig, persona: str) -> RealtimeSession:
    return AzureVoiceLiveSession(cfg, persona=persona)


register_realtime_provider("azure_voice_live", build_azure_voice_live)
