"""WS /voice/live — the duplex voice edge.

A realtime model owns ears and mouth; the existing agent owns the brain. The
model's only tool is `ask_agent`, which runs the real `run_turn` and comes
back with a `spoken` string the model reads verbatim (see
`voice/live/delegate.py`).

Kept out of `endpoints/voice.py` on purpose: that router is turn-based and
already 300+ lines, and a duplex state machine does not belong in it.

WebSocket-specific gotcha: `enforce_channel_kind` and the sliding-window
limiter raise `HTTPException`, which means nothing on a socket. Both are
caught here and translated into application close codes.

A second WebSocket-specific gotcha, found in review: `websocket.close()`
called BEFORE `websocket.accept()` is translated by ASGI into a bare HTTP 403
handshake rejection (confirmed against all three uvicorn WS implementations)
-- whatever close code/reason we pass is silently discarded and the browser
just sees a refused connection / 1006, not the intended
CLOSE_FORBIDDEN/CLOSE_RATE_LIMITED. So every rejection in this handler,
without exception, happens AFTER `accept()`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import unicodedata
import uuid
from urllib.parse import urlsplit
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from common.chat_session import TurnIdentity
from core.auth import authenticate_optional
from core.chat_persistence import PORTAL_CLIENT_TAG
from core.prompt_resolver import PromptResolver
from runtime.agents.voice_registry import (
    VoiceCredentialRefused,
    voice_agent_requires_auth,
    voice_agent_slug_addressed,
    voice_client_context_keys,
    voice_config_defaults,
    voice_persona_fallback,
    voice_credential,
    voice_tenant_access,
    voice_identity_source,
    voice_session_metadata_keys,
)
from core.channel_resolver import (
    any_channel_has_origin_allowlist,
    enforce_channel_kind,
    resolve_channel,
    root_span_attrs,
)
from core.deployment import active_scope, scope_kinds
from voice.live.budget import BudgetExhausted, check_audio_budget, reserve_audio_seconds
from voice.live.base import ASK_AGENT_TOOL_SPEC, RealtimeProviderError, ToolResultMode
from voice.live.config import (
    LiveVoiceConfig,
    hydrate_live_model,
    resolve_failure_spoken,
    resolve_live_config,
    resolve_on_screen_spoken,
    resolve_progress_spoken,
    resolve_say_exactly_instruction,
)
from voice.live.delegate import failure_spoken_for, run_delegated_turn
from voice.live.factory import build_realtime_session
from voice.live.dictation import (
    CLAIMING_ACTIONS,
    ClosedCaptureState,
    dictation_capture,
)
from voice.live.metrics import VoiceLiveMetrics
from voice.live.onset import SpeechOnsetDetector
from voice.live.readiness import config_problem
from core.agent_profile import MissingProfileError, MissingSnapshotError
from voice.live.recorder import VoiceCallRecorder, recording_enabled
from core.sensitive_content import enter_redacted_scope, is_sensitive_kind
from voice.live.call_log import CallLog
from voice.live.speech_hygiene import ToolSyntaxScrubber
from voice.live.protocol import (
    CLOSE_FORBIDDEN,
    CLOSE_PROVIDER_ERROR,
    CLOSE_RATE_LIMITED,
    agent_status_frame,
    chart_frame,
    display_frame,
    links_frame,
    proposal_frame,
    error_frame,
    interrupted_frame,
    ready_frame,
    transcript_frame,
    turn_end_frame,
)
from voice.rate_limit import SlidingWindowLimiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice", tags=["voice"])

# One socket per caller per minute is plenty; reconnect storms are the abuse
# case this catches. Concurrency is capped separately, per replica.
_connect_limiter = SlidingWindowLimiter(max_requests=10, window_sec=60)

# Per-replica concurrency bookkeeping. The CAPS live in LiveVoiceConfig
# (max_concurrent_sessions / max_concurrent_per_ip — operator-tunable per
# tenant/channel); only the counter dict is module state. Deliberately NOT
# shared across replicas — see voice/live/budget.py for why the daily budget
# is persisted and this isn't.
_active: Dict[str, int] = {}

# Fixed namespace for deriving a synthetic budget key for anonymous callers
# (see _budget_key below). Any stable constant works; this one is arbitrary.
_ANON_BUDGET_NAMESPACE = uuid.NAMESPACE_URL

# Floor for cfg.budget_charge_interval_sec. `voice_config.live` is raw
# operator JSON, so a tiny positive value (0.5, "1") would otherwise turn the
# periodic budget charge into a per-second DB write per live session. A
# module constant (not inlined) so tests can shrink it and exercise the
# periodic path in real time.
_BUDGET_CHARGE_INTERVAL_FLOOR_SEC = 5.0

# First-frame auth (kinds registered `requires_auth`). A browser WebSocket
# cannot set an Authorization header, an app on another domain has no
# engine-domain portal cookie, and a mint-a-ticket endpoint would need
# cross-replica state — so the FIRST client frame carries the credentials.
# 5s is generous for a frame the client sends immediately on open. A module
# constant (not inlined) so tests can shrink it and exercise the timeout.
_AUTH_FRAME_TIMEOUT_SEC = 5.0

# Turn-context keys a client may set (auth frame `context` / later `context`
# frames) are per kind (`client_context_keys` on its voice registration).
# Everything else is dropped: this dict is merged into the delegated turn's
# context, which reaches the agent's prompt injection.


def _whitelist_context(raw: Any, keys: Tuple[str, ...]) -> Dict[str, Any]:
    """The subset of a client-supplied context dict the engine accepts."""
    if not isinstance(raw, dict):
        return {}
    out = {k: raw[k] for k in keys if k in raw}
    dropped = set(raw) - set(out)
    if dropped:
        logger.debug(
            "voice.live: dropped non-whitelisted context keys: %s", sorted(dropped)
        )
    return out


_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _apply_client_context(
    delegate_ctx: Dict[str, Any], raw: Any, *, pinned: Dict[str, Any],
    keys: Tuple[str, ...] = ("screen_context",),
) -> None:
    """Replace the client-owned turn context, then re-apply server-pinned keys.

    Client `context` frames REPLACE context wholesale (stale keys from the
    previous screen would describe the wrong record), but server-owned keys
    (business caller identity/branch/capabilities) must survive — dropping
    them mid-call would silently stop proposals from binding.
    """
    delegate_ctx.clear()
    delegate_ctx.update(_whitelist_context(raw, keys))
    delegate_ctx.update(pinned)


async def _receive_auth_frame(websocket: WebSocket) -> Optional[Dict[str, Any]]:
    """The first client frame, iff it is a well-formed auth frame.

    None for a timeout, a binary frame, unparseable JSON, or any frame that
    isn't `{"type": "auth", ...}` — the caller closes CLOSE_FORBIDDEN on
    None. Never raises: this runs inside the handshake's happy path where an
    unguarded exception would abort the ASGI task with no close frame.
    """
    try:
        raw = await asyncio.wait_for(
            websocket.receive_text(), timeout=_AUTH_FRAME_TIMEOUT_SEC
        )
        frame = json.loads(raw)
    except Exception:
        return None
    if not isinstance(frame, dict) or frame.get("type") != "auth":
        return None
    return frame



# Announce/normalisation helpers live in common.announce so a product
# agent's announce-without-tool-call guard shares the exact detector (see
# that module's docstring); the aliases keep this file's call sites and the
# agreement test's imports stable.
from common.announce import (
    ANNOUNCE_TOKENS as _ANNOUNCE_TOKENS,
    AR_NORMALISE as _AR_NORMALISE,
    AR_TASHKEEL as _AR_TASHKEEL,
    QUESTION_MARKS as _QUESTION_MARKS,
    QUESTION_TRAILING as _QUESTION_TRAILING,
    normalize_intent_text as _normalize_intent_text,
    reply_ends_in_question as _reply_ends_in_question,
)
from common.screen_claims import caller_sees_nothing, is_farewell, persona_claims_a_result


# Pure acknowledgement tokens — "I'm still here", not "stop, I meant something
# else". A caller who says one of these while she is fetching their answer must
# NOT lose that answer, which is exactly what superseding on every committed
# utterance would do. Deliberately NARROW: negatives ("no", "لا") are absent on
# purpose — "no, the best branch instead" is a cancellation, not a nod — and
# anything outside this set counts as substantive.
_BACKCHANNEL_TOKENS = frozenset({
    "yes", "yeah", "yep", "yup", "ok", "okay", "k", "sure", "right",
    "mmhm", "mhm", "mmm", "uhhuh", "aha", "hmm", "gotit",
    # fragments left by the strip regex: "mm-hm" -> "mm" "hm", "uh-huh" -> ...
    "mm", "hm", "uh", "huh", "m",
    "أيوه", "ايوه", "أيوا", "ايوا", "نعم", "طيب", "تمام", "أوكي", "اوكي",
    "اها", "آها", "ممم", "زين", "إيه", "ايه", "اي", "أي",
    # Availability probes — the caller checking the line during a long tool
    # wait ("هلو؟", "سامعني؟", "hello?"). Same contract as a nod: they carry
    # no new intent, and superseding on one drops the very answer the caller
    # is waiting for (live 2026-08-03: mode=silent delivery after a 14.6s
    # delegate, then the same question re-asked across three redials). "يا"
    # is here so a vocative "يا <assistant name>" reads as a probe once the
    # tenant adds its assistant's name via backchannel_tokens.
    "هلو", "هالو", "الو", "سامعني", "سامعتني", "تسمعني", "معي", "وينك",
    "يا", "hello", "hey",
})
_BACKCHANNEL_STRIP = re.compile(r"[\s,.!?؟،…-]+")
# _ANNOUNCE_TOKENS moved to common.announce (imported above).
# _AR_NORMALISE / _AR_TASHKEEL moved to common.announce (imported above).


# _reply_ends_in_question moved to common.announce (imported above).


def _refetch_request(utterance: str, previous: str) -> str:
    """The request to delegate for a caller who says they see nothing.

    «مش شايف إشي» alone tells the brain nothing; the request it refers to is
    the caller's previous one. Anything else is returned unchanged."""
    if not caller_sees_nothing(utterance) or not (previous or "").strip():
        return utterance
    return (f"{previous.strip()}\n\n(The caller then said «{utterance.strip()}»: nothing they asked "
            f"for is on their screen. Fetch it again with the tools and answer.)")


def _mentions_a_topic_term(utterance: str, terms) -> bool:
    """Does the CALLER's utterance name something only the agent can answer?

    The announce tier reads the model's reply. This reads the caller's, and
    that inversion is the whole point: a reply that ends a turn with the
    question unconsumed is textually indistinguishable from a goodbye or a
    piece of chitchat, but the caller's own words are not. "What were my
    sales last month" names a figure; "no, that's all thanks" names nothing.

    Same normalisation as every other matcher here, so ASR spacing and Arabic
    orthography variants are covered without enumerating them.
    """
    if not terms:
        return False
    norm = _normalize_intent_text(utterance)
    return any(_normalize_intent_text(t) in norm for t in terms if t)


def _normalise_token(tok: str) -> str:
    return _AR_TASHKEEL.sub("", tok).translate(_AR_NORMALISE)


# \w is Unicode-aware for str patterns, so Arabic letters are word chars.
_QUERY_TOKEN_SPLIT = re.compile(r"\W+")


def _query_tokens(text: str) -> frozenset:
    folded = _AR_TASHKEEL.sub("", (text or "").lower()).translate(_AR_NORMALISE)
    return frozenset(t for t in _QUERY_TOKEN_SPLIT.split(folded) if t)


def _same_recovered_question(query: str, recovered: str) -> bool:
    """Is the model's late ask_agent asking the question the stall recovery
    already answered?

    The exact-epoch check beside this is right but brittle: any unconfirmed
    `interrupted` between the caller's commit and the model's (late) call —
    echo, room noise — moves the epoch and used to defeat the duplicate
    suppression entirely. Live 2026-08-31 13:44 on the landing sales tenant:
    the recovery answered the caller's booking details, the model's own call
    landed 1.2s later past a bumped epoch, and the caller got their phone
    number read back TWICE — and because both turns ran the brain,
    `book_demo_call` fired twice (two demo-request emails for one lead, the
    first sent before the caller had even confirmed the digits).

    The model paraphrases rather than quoting (observed live: re-cased,
    re-punctuated), so equality is wrong and token containment is right:
    normalized (case, tashkeel, Arabic letter folds) token overlap over the
    SMALLER side, so a condensed paraphrase still matches its source. 0.75
    keeps that paraphrase in while keeping out a different question that
    merely shares stopwords ("how does the whatsapp integration work" vs
    "how does the online booking work" overlap at 0.67). A genuinely
    repeated caller question is unaffected either way — its own commit
    clears `last_commit["recovered"]` before the model's call arrives.
    """
    qa, qb = _query_tokens(query), _query_tokens(recovered)
    if not qa or not qb:
        return False
    return len(qa & qb) / min(len(qa), len(qb)) >= 0.75


_BACKCHANNEL_NORMALISED = frozenset(_normalise_token(t) for t in _BACKCHANNEL_TOKENS)


def _is_backchannel(utterance: str, extra: frozenset = frozenset()) -> bool:
    """True when the whole utterance is just acknowledgement noise.

    Whole-utterance only: "yes" is a nod, but "yes, and what about Jeddah" is a
    real turn. Requiring EVERY token to be an acknowledgement keeps a two-word
    nod ("ok ok") on the safe side without swallowing a short real question.

    Both sides are Arabic-normalised so one spelling covers the family — see
    _AR_NORMALISE for why enumerating them is not enough.

    `extra` is the tenant's own normalised token set
    (voice_config.live.backchannel_tokens via _extra_backchannel_tokens) —
    the built-in set is EN+AR, and a tenant serving another language adds its
    nods there rather than editing this module.
    """
    cleaned = _BACKCHANNEL_STRIP.sub(" ", (utterance or "").lower()).strip()
    if not cleaned:
        return True
    return all(
        _normalise_token(t) in _BACKCHANNEL_NORMALISED
        or _normalise_token(t) in extra
        for t in cleaned.split()
    )


# How long the floor must have been silent before a backchannel is treated as
# the caller checking whether anyone is there. Comfortably longer than the gap
# between an assistant's own sentences, short enough that a caller does not
# repeat themselves three times first.
_BACKCHANNEL_WAKE_AFTER_SEC = 1.5


# WHY EVERY BLOCKING CALL IN THIS FILE GOES THROUGH asyncio.to_thread
# (`_HANDSHAKE_OFF_LOOP`, referenced from the call sites).
#
# supabase-py is SYNCHRONOUS. Called straight from an async handler it does
# not merely make that handler slow — it parks the whole event loop for the
# round trip, so every other connection on the replica stops: other callers'
# handshakes, and the audio pumps of calls already in progress.
#
# The voice handshake makes five of them back to back (active profile +
# realtime model row, budget ceiling, daily usage, the voice specialist
# profile, the persona's prompt version), then the handshake charge. On an
# idle prod replica that is 2.4-4.1s end to end. On 2026-09-03 a caller on
# company.example got "the live voice service can't be reached" because the same
# replica was also serving a burst of `/recommendations` traffic: the
# handshake's own reads finished at ~14s — each one queued behind other
# requests' blocking calls — and the browser had given up at 12s. Nothing
# was broken; the loop was simply never free.
#
# `common/chat_session.py` already establishes the rule for the delegated
# turn ("every call goes through to_thread so the event loop -- and, in the
# voice WS process, the audio pumps -- keeps running"). This file is the
# other half of it.
#
# Order and semantics are deliberately unchanged: each call still runs one
# at a time, in the same sequence, so a budget refusal still short-circuits
# before the agent is built. The win is that the loop stays free WHILE they
# run, so concurrent connections and live audio no longer wait on them.


# How long after WE deliberately cut the floor (an INTERRUPT-mode tool result
# displacing our own holding line, or the answer watchdog's cancel-and-resay)
# a provider `interrupted` event is read as the service reporting OUR cut back
# to us rather than the caller speaking. The echo arrives well inside one
# network round-trip of the interrupting send (observed same-batch as our own
# answer_ready flush frame, live 2026-08-31); a real barge-in mistaken for it
# still supersedes on its own substantive transcript commit, so the cost of a
# too-generous window is ~1s of the answer talking over the caller, never a
# lost interruption.
_SELF_CUT_ABSORB_SEC = 1.0


class _StreamHalted(Exception):
    """Raised by the per-sentence streamer to DECLINE a sentence without
    aborting the delegate turn.

    voice/live/delegate.py's on_sentence contract is exactly what makes this
    work: a raising callback is logged and the turn continues, and the
    sentence is never recorded in `result.streamed` — so the final delivery
    still owns the unspoken tail and can resolve it through
    `_resolve_barge_in_mode` instead of the answer dying sentence-silenced.
    """


def _utterance_should_arm_recovery(
    *,
    is_backchannel: bool,
    response_in_flight: bool,
    since_last_audio_sec: float,
    idle_after_sec: float = _BACKCHANNEL_WAKE_AFTER_SEC,
) -> bool:
    """Does this committed utterance arm stall recovery?

    A backchannel ("mm-hm", "ايوه", "ألو") is noise WHILE the assistant is
    talking — dropping it is why a listening caller does not cut the answer
    off. But the drop also skips `last_commit`, and `last_commit` is what arms
    stall recovery and the silent-turn watchdog. On a SILENT floor that
    inverts the meaning: the same word is the caller asking "are you still
    there?", and dropping it guarantees the one thing they can do to break a
    stall changes nothing.

    That is not hypothetical. This demo lists "ألو" in `backchannel_tokens`,
    and on a live call (2026-08-18) the assistant announced "خلني أشوف لك"
    and never returned; the caller said "ألو... ألو ألو ألو" and the call
    stayed dead, because every one of those was discarded before it could arm
    anything.

    So: substantive utterances always arm. A backchannel arms too, but only
    when nothing is on its way to the caller — no response in flight, and no
    assistant audio for `idle_after_sec`.
    """
    if not is_backchannel:
        return True
    if response_in_flight:
        return False
    return since_last_audio_sec >= idle_after_sec


def _extra_backchannel_tokens(cfg) -> frozenset:
    """cfg.backchannel_tokens, normalised the same way as the built-ins."""
    return frozenset(
        _normalise_token(_BACKCHANNEL_STRIP.sub("", str(t).lower()))
        for t in (getattr(cfg, "backchannel_tokens", None) or [])
        if str(t).strip()
    )


def _tag_live_span(
    *, op: str, provider: str, agent_kind: str, text_only: bool = False
) -> None:
    """Attach waj.voice.live.* to the active Laminar span.

    Duplex audio bills per audio token in BOTH directions, well above text, so
    the spend has to be attributable per tenant in /admin. Best-effort:
    Laminar being unavailable must never break a call (mirrors
    endpoints/voice.py::_tag_voice_op). `text_only` keeps ambient-scribe
    sessions (never billed for output audio) separable from spoken ones in
    the latency series.
    """
    try:
        from lmnr import Laminar

        Laminar.set_span_attributes(
            {
                "waj.voice.live.op": op,
                "waj.voice.live.provider": provider,
                "waj.voice.live.agent_kind": agent_kind,
                "waj.voice.live.text_only": text_only,
            }
        )
    except Exception:
        pass


def _apply_kind_voice_defaults(cfg: "LiveVoiceConfig", agent_kind: str) -> None:
    """Code defaults a kind registers for its duplex sessions.

    Today that is one thing: the recognizer phrase list used when the
    profile's voice_config sets none (a kind may register one — see its
    registration for why). An operator-set list always wins and is never
    merged. Only the Azure `azure-speech` recognizer consumes it
    (azure_voice_live._session_payload); Gemini Live has no phrase-list
    support and ignores the field.
    """
    # Values the kind uses where the profile sets none (a generic agent's
    # greeting). A text-only session has no greeting to default.
    for _key, _value in voice_config_defaults(agent_kind).items():
        if _key == "greeting_trigger" and cfg.text_only:
            continue
        if not getattr(cfg, _key, None):
            setattr(cfg, _key, _value)
    if cfg.phrase_list:
        return
    from runtime.agents.voice_registry import voice_default_phrase_list

    defaults = voice_default_phrase_list(agent_kind)
    if defaults:
        cfg.phrase_list = defaults


def _apply_text_only_overrides(cfg: "LiveVoiceConfig") -> None:
    """Stand down every subsystem that manages AUDIBLE dead air.

    In text_only mode nothing is ever spoken, so the answer watchdog and
    stall tiers — which stand down only on observed reply audio — would
    fire on every turn and re-deliver duplicates; holding lines and
    per-sentence streaming are speech delivery by definition.
    """
    if not cfg.text_only:
        return
    cfg.progress_after_sec = 0.0
    cfg.answer_watchdog_sec = 0.0
    # The STALL tiers stay armed. They rescue a turn the model ended without
    # calling ask_agent, which is not an audio problem at all — it is the
    # difference between the clinician seeing a note and seeing nothing. An
    # earlier version of this helper zeroed them along with the audio
    # machinery and produced exactly that: sessions that listened and showed
    # no proposals, no diagnosis, no note (reported from staging 2026-08-10).
    # Their stand-down signal (`last_reply_audio`) is fed by assistant TEXT in
    # this mode — see where it is stamped in the event loop — so tier 3 can
    # still tell a replying model from a dead one.
    cfg.stream_sentences = False
    # A scribe listens; it does not greet.
    cfg.greeting_trigger = ""


def _apply_direct_overrides(cfg: "LiveVoiceConfig") -> None:
    """Stand down every subsystem that exists to manage `ask_agent`.

    In pre-fetched-context mode the model has no `ask_agent` to call: it
    answers from the case file in its persona and its only tools are instant
    local actions. Every recovery tier keys on "the model should have called
    ask_agent and did not" and would DELEGATE the caller's words to the brain
    — exactly the round trip this mode removes — and the holding line, answer
    watchdog and relay custody all manage a delegated answer that never
    exists here.
    """
    cfg.progress_after_sec = 0.0
    cfg.progress_on_dispatch = False
    cfg.answer_watchdog_sec = 0.0
    cfg.stall_recovery_sec = 0.0
    cfg.stall_recovery_unannounced_sec = 0.0
    cfg.stall_recovery_silent_sec = 0.0
    cfg.stall_recovery_topic_terms = []
    cfg.persona_claim_recovery_enabled = False
    cfg.relay_mode = False
    cfg.server_side_iban_capture = False
    cfg.kb_prefetch_enabled = False


def _direct_tools_for(cfg: "LiveVoiceConfig", agent: Any) -> Dict[str, Any]:
    """{tool name: handler} for a pre-fetched-context call, {} otherwise.

    Fills `cfg.direct_tools` (the declarations the provider sends INSTEAD of
    ask_agent) and applies `_apply_direct_overrides`. A kind that has no
    `direct_voice_tools()` keeps the delegated session, loudly: the flag is
    operator config and must not be able to take a call down.
    """
    if not cfg.prefetched_context:
        return {}
    factory = getattr(agent, "direct_voice_tools", None)
    if not callable(factory):
        logger.warning(
            "voice.live: prefetched_context is set but %s has no direct_voice_tools() "
            "— serving the delegated (ask_agent) session",
            type(agent).__name__,
        )
        return {}
    pairs = list(factory())
    cfg.direct_tools = [dict(decl) for decl, _handler in pairs]
    # An agent that answers from its prompt alone returns NO pairs: the
    # session then declares nothing (not ask_agent), and every recovery that
    # assumes a delegate stands down exactly as in the action-tool case.
    cfg.tools_disabled = not pairs
    _apply_direct_overrides(cfg)
    return {decl["name"]: handler for decl, handler in pairs}


def _suppress_extraction_display(cfg: "LiveVoiceConfig", result) -> bool:
    """True when a delegated turn's display frame must NOT be sent.

    text_only is the ambient-scribe mode: the user is dictating a live
    session, and an extraction turn's payload is the proposal cards it
    queued — the delegate's prose is narration of work already visible on
    screen. Posting that narration after every meaningful sentence is the
    scribe chatter users complained about (2026-08: a reply to every
    utterance while they were busy with the person in front of them). A turn
    that produced NO proposals is a genuine question/answer, and its display
    frame IS the answer's delivery in this mode — keep it, or real answers
    go silent. Audio modes are untouched: the phone agents' screens must
    stay byte-identical.
    """
    return bool(cfg.text_only and (getattr(result, "proposals", None) or []))


def _active_profile_for(agent_kind: str):
    """The `status='active'` profile for `agent_kind` — the PARENT profile.

    Split out of `_live_config_for` so the handshake can hand the SAME object
    to `resolve_persona_prompt` without widening `_live_config_for`'s
    `(conn, agent_kind)` signature, which half a dozen test modules
    monkeypatch as a two-positional seam. `AgentProfileLoader` caches by
    (kind, environment) for `AGENT_PROFILE_CACHE_TTL_SEC`, so the second call
    per connection is a dict lookup, not a second round trip.

    Tolerant like `_live_config_for`'s own load: a DB blip returns None and
    the persona then falls back to its own last resort (which warns) rather
    than failing the whole connect.

    That tolerance is deliberate — a transient DB error must not take voice
    down — but it used to be SILENT (`logger.debug`, off in every deployed
    stack), so a tenant whose profile never loaded degraded to built-in
    defaults with nothing above debug to say so: the config an operator reads
    in /admin is not the config the call ran on, and the logs agree with the
    portal. Both branches now WARN, the level this module already uses for
    "serving something other than what was configured" (see
    `resolve_persona_prompt`'s two fallbacks). Not `error`: this path stays
    fail-open by design and fires per connect, and the transport case is
    already logged at error inside `load_active` — a second error-level record
    per WS connect would page on a steady state that is not an outage. WARNING
    is above every deployed handler's threshold, so it is greppable, which is
    the whole requirement.

    The two branches are kept apart because they need different operator
    responses: a missing profile/snapshot is a CONFIG state to fix in /admin
    (seed or promote the profile), while anything else is the DB or the
    service key and carries a traceback.
    """
    from core.agent_profile import (
        AgentProfileLoader,
        MissingProfileError,
        MissingSnapshotError,
    )

    try:
        return AgentProfileLoader.load_active(agent_kind)
    except (MissingProfileError, MissingSnapshotError) as exc:
        logger.warning(
            "voice.live: no active profile for agent_kind=%s (%s) — this call is "
            "running on BUILT-IN voice defaults, not this tenant's voice_config: "
            "barge-in, the answer watchdog, the persona pin, the realtime model row "
            "and the greeting all fall back. Seed or promote an active profile for "
            "this kind/environment.",
            agent_kind, exc,
        )
    except Exception:
        logger.warning(
            "voice.live: loading the active profile for agent_kind=%s FAILED — this "
            "call is running on BUILT-IN voice defaults, not this tenant's "
            "voice_config. The profile row may be fine; check the AI Supabase "
            "connection and service key first.",
            agent_kind, exc_info=True,
        )
    return None


@dataclass
class VoiceRoute:
    """A slug-addressed way into `voice_live` (`WS /v1/agents/{slug}/voice`).

    The plain socket picks its agent by KIND, which is enough while a kind
    has one active profile. The generic `custom` kind has many, told apart by
    slug, so its route parks one of these on `websocket.state` and calls the
    same handler. It is the whole seam: the handler reads the owning profile,
    the caller's identity and the budget bucket through it, and everything
    else (the protocol, the turn machinery, the providers) is shared.

    Never built from a query parameter: only a route that has already matched
    a slug creates one, so a caller of the plain socket cannot supply it.

    `authenticate(authorization_header)` is the route's own caller check (the
    same function its HTTP chat uses). It returns the caller or raises
    `HTTPException`; it is called with the handshake's `Authorization`
    header, or with the token of the first `auth` frame as a bearer header.
    `profile_pin` is a profile id an operator asked to test (a draft copy of
    the addressed agent); `pin` is that id once the handler has checked the
    caller may pin and that it names a copy of this very agent.

    Three settings let a route that owns its agent's voice serve a call
    without a voice-enabled profile row (the public demo's builder
    assistant, admin/demo_handlers.py). All default to the agent row's own:
    `voice_config` is used in place of the row's `voice_config`;
    `build_brain(params)` builds the agent that answers `ask_agent`, in place
    of the kind's registered builder, with the same params; `own_page` says
    the route has checked the call comes from this engine's own portal page,
    so no channel applies to it.
    """

    agent_slug: str
    authenticate: Callable[[Optional[str]], Awaitable[Dict[str, Any]]]
    profile_pin: Optional[str] = None
    pin: Optional[str] = None
    voice_config: Optional[Dict[str, Any]] = None
    build_brain: Optional[Callable[[Dict[str, Any]], Any]] = None
    own_page: bool = False
    #: Set only by a route that owns the call's context (demo_mode/showcase.py):
    #: `brief` is handed to the brain for this call, `greeting` replaces the
    #: agent's `greeting_trigger` for this call. None leaves both as configured.
    brief: Optional[str] = None
    greeting: Optional[str] = None


def _voice_route(conn: Any) -> Optional[VoiceRoute]:
    route = getattr(getattr(conn, "state", None), "voice_route", None)
    return route if isinstance(route, VoiceRoute) else None


def _demo_may_record(conn: Any) -> bool:
    """In the public demo only some calls may be recorded
    (`demo_mode.voice.may_record`); everywhere else this changes nothing."""
    from demo_mode import settings as demo_settings

    if not demo_settings.enabled():
        return True
    from demo_mode import voice as demo_voice

    route = _voice_route(conn)
    return demo_voice.may_record(route.agent_slug if route else None)


async def serve_voice_route(
    websocket: WebSocket, route: VoiceRoute, *, agent_kind: str,
    session_id: Optional[str] = None, locale: Optional[str] = None,
    provider: Optional[str] = None, sim_run: Optional[str] = None,
) -> None:
    """Serve one call addressed by slug through the shared handler."""
    websocket.state.voice_route = route
    await voice_live(
        websocket, agentKind=agent_kind, businessId=None, sessionId=session_id,
        locale=locale, provider=provider, simRun=sim_run, callCase=None,
    )


async def _route_caller(route: VoiceRoute, authorization: Optional[str]) -> Optional[Dict[str, Any]]:
    """The caller `route.authenticate` admits, or None. Never raises."""
    try:
        caller = await route.authenticate(authorization)
    except HTTPException:
        return None
    except Exception:
        logger.warning("voice.live: the route's caller check failed", exc_info=True)
        return None
    return caller if isinstance(caller, dict) else None


def _caller_principal(caller: Dict[str, Any]) -> str:
    """A stable id for an admitted caller. An API key has no user id."""
    for key in ("user_id", "api_key_id", "key_id", "id"):
        if caller.get(key):
            return str(caller[key])
    return "caller"


def _is_operator_token(token: str) -> bool:
    """True for a signature-verified superuser token (who may pin a profile)."""
    try:
        from admin.auth import resolve_superuser

        resolve_superuser(token)
        return True
    except Exception:
        return False


def agent_budget_id(agent_kind: str, agent_slug: str) -> str:
    """The id a slug-addressed agent's calls are metered and recorded under.

    The daily audio budget and the call recording key off a business id, and
    a generic agent has none: it is called with an admin token or an API key,
    not on behalf of a tenant. Its calls are charged to ONE bucket per agent
    instead. Not per caller: the realtime audio is the agent owner's cost
    whoever dials, an API key carries no user id, and a per-caller bucket
    would multiply the daily cap by the number of keys. The id is shown on
    the agent's Voice tab, where an operator can give it its own allowance.
    """
    return str(uuid.uuid5(_ANON_BUDGET_NAMESPACE, f"voice-live-agent:{agent_kind}:{agent_slug}"))


def _route_profile(route: VoiceRoute, agent_kind: str):
    """The profile a slug-addressed call is configured by. FAILS CLOSED:
    an unknown, inactive or unpublished slug raises, it never resolves to
    another agent of the kind."""
    from core.agent_profile import AgentProfileLoader, _default_environment

    if route.pin:
        pinned = AgentProfileLoader.load_pinned(route.pin, agent_kind)
        if pinned is not None and getattr(pinned, "slug", None) == route.agent_slug:
            return pinned
        logger.warning(
            "voice.live: profile pin %s is not a copy of agent %r; serving the "
            "published agent", route.pin, route.agent_slug,
        )
        route.pin = None
    return AgentProfileLoader.load_delegate(agent_kind, route.agent_slug, _default_environment())


def _owner_profile_for(conn: Any, agent_kind: str):
    """The profile that OWNS this call's voice settings: the kind's active
    profile, or the one a slug-addressed route names."""
    route = _voice_route(conn)
    if route is None:
        return _active_profile_for(agent_kind)
    return _route_profile(route, agent_kind)


def _live_config_for(conn: WebSocket, agent_kind: str):
    """Resolve voice_config.live for this connection, model row included."""
    from core.auth import get_service_client

    profile = _owner_profile_for(conn, agent_kind)
    route = _voice_route(conn)
    own_voice = route is not None and route.voice_config is not None
    profile_voice = route.voice_config if own_voice else getattr(profile, "voice_config", None)

    channel = getattr(conn.state, "channel", None)
    cfg = resolve_live_config(profile_voice=profile_voice, channel=channel)
    if cfg.enabled:
        hydrate_live_model(
            cfg,
            client=get_service_client(),
            # Matches the brain path (core/agent_profile.py passes environment
            # to fetch_model), so a per-env realtime sibling row is honoured
            # for the ears/mouth too instead of silently ignored.
            environment=getattr(profile, "environment", None),
            model_row=None if own_voice else getattr(profile, "voice_model", None),
        )
    return cfg


# Portal locale -> (BCP-47 speech tag, human name bound into the persona).
# The realtime model handles BOTH transcription and speech, so leaving the
# language unpinned makes it guess from the audio: callers reported it drifting
# into Indian- and French-accented readings mid-session. A bare prefix pins
# the per-language default region; a full `xx-YY` tag is the caller's explicit
# ACCENT choice (the company.example voice demo's accent picker) and is passed through
# for any region, so a new accent never needs a deploy here.
_LOCALE_SPEECH = {
    "ar": ("ar-SA", "Arabic, Saudi dialect"),
    "en": ("en-US", "English"),
}
_DEFAULT_LOCALE = "en"

# Human names for the accents the demo offers today. An unlisted region still
# passes through with a generic name — see language_name's fallback contract.
_ACCENT_NAMES = {
    "ar-SA": "Arabic, Saudi dialect",
    "ar-AE": "Arabic, Emirati dialect",
    "ar-KW": "Arabic, Kuwaiti dialect",
    "ar-QA": "Arabic, Qatari dialect",
    "ar-BH": "Arabic, Bahraini dialect",
    "ar-OM": "Arabic, Omani dialect",
    "ar-EG": "Arabic, Egyptian dialect",
    "ar-JO": "Arabic, Levantine (Jordanian) dialect",
    "ar-LB": "Arabic, Lebanese dialect",
    "ar-SY": "Arabic, Syrian dialect",
    "ar-IQ": "Arabic, Iraqi dialect",
    "ar-MA": "Arabic, Moroccan dialect",
    "en-US": "English",
    "en-GB": "English, British accent",
    "en-AU": "English, Australian accent",
    "en-IN": "English, Indian accent",
    "en-IE": "English, Irish accent",
}
_LANGUAGE_BASE_NAMES = {"ar": "Arabic", "en": "English"}


def session_language(locale: Optional[str], configured: Optional[str], mapped: str) -> str:
    """The session's speech tag for a KNOWN portal locale.

    A bare portal locale ("ar") only names the LANGUAGE; the region it maps to
    (ar-SA) is this file's default, not the caller's choice. When the
    operator's `voice_config.language` is a regional tag of that same
    language (a deployment's "ar-JO": Levantine, 2026-10-01), the
    operator's accent wins. A full tag ("ar-EG", the demo's accent picker)
    and a different language ("en" on an ar-JO row) still override it."""
    bare = len((locale or "").strip().replace("_", "-").split("-")) == 1
    conf = (configured or "").strip()
    if bare and "-" in conf and conf.split("-")[0].lower() == mapped.split("-")[0].lower():
        return conf
    return mapped


# An example line quoted in a greeting trigger («أهلين، معك وَج …»), and the
# parenthetical that carries it.
_QUOTED_EXAMPLE = re.compile(r"\s*\([^()]*«[^»]*»[^()]*\)|\s*«[^»]*»")


def greeting_for_language(trigger: str, language: Optional[str]) -> str:
    """The greeting trigger as sent for THIS session's language.

    A deployment's trigger quotes an Arabic example greeting, and the
    model recites a quoted script over an instruction about it (CLAUDE.md "A
    script in the prompt beats an instruction about the script"): an English
    app opened every call in Arabic (user voice test 2026-10-03). The trigger
    is operator config, so the language is pinned here: a non-Arabic session
    drops the Arabic example, and every session is told which language to
    greet in. A trigger with no quoted example is sent as it is."""
    text = (trigger or "").strip()
    if not text or not language or not _QUOTED_EXAMPLE.search(text):
        return trigger
    if not language.lower().startswith("ar"):
        text = _QUOTED_EXAMPLE.sub("", text).strip()
    return f"{text}\nGreet in {language_name(language)} only."


def speech_locale(locale: Optional[str]) -> Optional[Tuple[str, str]]:
    """(bcp47, human name) for a KNOWN portal locale, else None.

    Returning None (rather than silently defaulting to English) is what lets
    the caller distinguish "the portal chose a language" from "no/unknown
    locale" -- only the former may override an operator-configured
    `voice_config.live.language`. The map itself is a fallback table for when
    the operator set nothing, per the config-lives-in-DB rule; it is NOT the
    authority over the DB knob.

    A full `xx-YY` tag with a known language prefix is honoured with its
    region intact — that is the accent picker's contract. The region is NOT
    validated against a list: Azure treats an odd region as best-effort, and
    a picker option should never 404 here because this map lagged it.
    """
    parts = (locale or "").strip().replace("_", "-").split("-")
    key = parts[0].lower()
    if key not in _LOCALE_SPEECH:
        return None
    if len(parts) >= 2 and len(parts[1]) == 2 and parts[1].isalpha():
        tag = f"{key}-{parts[1].upper()}"
        return (tag, language_name(tag))
    return _LOCALE_SPEECH[key]


def language_name(bcp47: str) -> str:
    """Human name bound into the persona's {language} placeholder.

    Accent names come first; an unlisted region of a known language still
    reads coherently ("Arabic (ar-TN dialect)"); anything else falls back to
    the raw tag (an operator pinning an exotic value still gets an
    unambiguous "Speak ar-XX, and only ar-XX" instruction).
    """
    parts = (bcp47 or "").strip().replace("_", "-").split("-")
    tag = "-".join([parts[0].lower()] + [p.upper() for p in parts[1:]])
    named = _ACCENT_NAMES.get(tag)
    if named:
        return named
    for known, name in _LOCALE_SPEECH.values():
        if known.lower() == tag.lower():
            return name
    base = _LANGUAGE_BASE_NAMES.get(parts[0].lower())
    if base and len(parts) >= 2:
        return f"{base} ({tag} dialect)"
    return bcp47


def _render(content: str, variables: Optional[Dict[str, Any]]) -> str:
    """Substitute persona placeholders in a promoted snapshot.

    The snapshot branch returns stored content directly rather than going
    through PromptResolver, so it needs the same tolerant substitution --
    missing keys stay literal instead of raising.
    """
    if not variables:
        return content
    try:
        from core.prompt_resolver import _SafeDict

        return content.format_map(_SafeDict(variables))
    except Exception:
        return content


def resolve_persona_prompt(
    cfg, agent_kind: str, profile: Any = None, *,
    specialist_profile: Any = None,
    language: Optional[str] = None,
    extra_variables: Optional[Dict[str, Any]] = None,
) -> str:
    """Fetch the voice persona prompt named by voice_config.

    Config-DB only: there is deliberately no hardcoded persona string here.
    A tenant with duplex enabled but no persona slug is a misconfiguration.

    `profile` is the **PARENT** (`status='active'`) profile for `agent_kind` —
    the same row `_live_config_for` reads `voice_config` off. The persona
    belongs to the parent because the parent already NAMES it
    (`voice_config.persona_prompt_slug`) and owns every other edge setting
    (`voice_id`, `language`, `greeting_trigger`, `barge_in_enabled`), while the
    specialist supplies only the brain (model, tools, KB, memory). Pinning it
    here is also what makes ONE promote of the parent freeze edge config +
    realtime model row + persona version together.

    It is load-bearing that this is the parent and not the specialist: the
    portal's persona editor posts its version repoint to the profile being
    viewed, and every doc tells operators to edit the parent. Resolving the pin
    off the specialist made a parent-side save a silent no-op — the portal
    showed the new persona, the caller heard the old one, and no warning fired
    because the specialist's stale pin satisfied the governed branch.

    Precedence — the same promote/rollback governance every other prompt obeys,
    rather than always serving whatever `agent_prompt_versions` row happens to
    have the highest `version_number`:
      1. `profile.resolved_prompts[slug]` — the parent's frozen
         compile-on-promote snapshot content (prod). Without this, saving a
         draft persona version in the portal would ship to every prod voice
         caller on the very next connect, with no promote step.
      2. `profile.prompt_version_ids[slug]` — the operator's explicit pin on
         the parent (dev/staging). Without this, rolling the parent back to an
         older persona version would silently do nothing for voice.
      3. `specialist_profile`'s snapshot/pin — a MISPLACED pin, honoured but
         warned. See the branch below.
      4. Slug -> latest `agent_prompt_versions` row (`resolve_by_slug`), which
         also warns. Only reached with no profile at all, or a parent that
         doesn't (yet) carry an entry for this slug — e.g. a freshly-seeded
         tenant before anything has been promoted or pinned.
    """
    if not cfg.persona_prompt_slug:
        raise LookupError(
            "voice_config.persona_prompt_slug is not set — duplex voice "
            "needs an agent_prompts row for the voice persona."
        )
    slug = cfg.persona_prompt_slug

    # Bound into the persona's `{language}` placeholder. PromptResolver uses
    # format_map with a _SafeDict, so a persona row that predates the
    # placeholder renders unchanged instead of raising -- the wording stays
    # operator-editable config, only the VALUE is request-bound.
    variables: Optional[Dict[str, Any]] = {"language": language} if language else None
    # Agent-supplied, request-bound values (e.g. the outbound collection
    # call's {callee_name}). Same _SafeDict rendering, so a persona without
    # the placeholder renders unchanged.
    if extra_variables:
        variables = {**(variables or {}), **extra_variables}

    if profile is not None:
        resolved = (getattr(profile, "resolved_prompts", None) or {}).get(slug)
        if resolved:
            return _render(resolved, variables)
        pinned_version_id = (getattr(profile, "prompt_version_ids", None) or {}).get(slug)
        if pinned_version_id:
            return PromptResolver.resolve(
                pinned_version_id, variables=variables, agent_kind=agent_kind, prompt_key=slug
            )

    # MISPLACED-PIN FALLBACK: the pin is on the SPECIALIST, not the parent.
    #
    # Reachable, not hypothetical: the Agents screen's persona editor posts the
    # profile being VIEWED as its `profile_id` (agents.html's hidden
    # `active_profile.profile_id`), so an operator who opens the specialist and
    # saves the persona pins it there — as does an `update_profile` MCP call
    # naming the specialist. It was also this code's own reading before
    # 2026-08-17. Honouring it beats silently regressing that tenant to
    # global-latest, which is what ignoring it would do.
    #
    # But the PARENT wins whenever it has an entry, and this branch WARNS. The
    # bug being fixed here was preferring the specialist SILENTLY: a persona
    # save on the parent then served the specialist's older version with nothing
    # in the logs. The warning names both profiles so an operator can see which
    # row holds the pin; the fix is repointing it on the parent (a persona save
    # made while viewing the parent does exactly that), not editing the
    # specialist further.
    if specialist_profile is not None and (
        profile is None
        or getattr(specialist_profile, "profile_id", None)
        != getattr(profile, "profile_id", None)
    ):
        misplaced_resolved = (getattr(specialist_profile, "resolved_prompts", None) or {}).get(slug)
        misplaced_pin = (getattr(specialist_profile, "prompt_version_ids", None) or {}).get(slug)
        if misplaced_resolved or misplaced_pin:
            logger.warning(
                "voice.live: persona %r is pinned on the SPECIALIST profile %r but not on "
                "the parent %r — serving the specialist's pin, which is the wrong owner. "
                "The parent owns the persona pin (it names the slug, and a persona save "
                "made while viewing the PARENT repoints it there); move it, or a persona "
                "edit on the parent will keep losing to this pin.",
                slug,
                getattr(specialist_profile, "slug", None) or "<none>",
                getattr(profile, "slug", None) or "<none>",
            )
            if misplaced_resolved:
                return _render(misplaced_resolved, variables)
            return PromptResolver.resolve(
                misplaced_pin, variables=variables, agent_kind=agent_kind, prompt_key=slug
            )

    # `persona_prompt_slug` is an `agent_prompts.slug` value (e.g.
    # "<kind>_voice_persona"), NOT a `prompt_version_id` -- PromptResolver.resolve()
    # takes the latter. resolve_by_slug() does the slug -> agent_prompts ->
    # latest agent_prompt_versions lookup and only then calls resolve(); no
    # fallback is supplied on purpose (config-DB only), so a slug with no
    # matching row still raises LookupError here, same "misconfigured
    # tenant" bucket the caller already handles. `prompt_key` is left to
    # default to `slug` (not a separate literal) so an eval run's
    # `X-WAJ-Prompt-Override: {"<agent_kind>/<slug>": vN}` can actually match
    # this prompt — a mismatched key here would silently defeat pinning.

    # Reached when NEITHER the parent nor the specialist carries a pin for this
    # slug. The persona then resolves to the globally-latest version, which
    # means promote/rollback do NOT govern what a caller hears — unlike the
    # brain's system prompt, which is pinned via prompt_version_ids and frozen
    # into the snapshot by runtime/compiler.py. Legitimate for a tenant that has
    # never been pinned; invisible until now, which is why it is logged.
    logger.warning(
        "voice.live: persona %r is not pinned on the parent profile %r — serving "
        "the globally-latest version, so promote/rollback do not govern it. Run "
        "the pin_voice_persona_versions backfill or set prompt_version_ids[%r] "
        "on the parent.",
        slug, getattr(profile, "slug", None) or "<none>", slug,
    )
    return PromptResolver.resolve_by_slug(slug, variables=variables, agent_kind=agent_kind)


def build_agent_for_kind(agent_kind: str, params: Dict[str, Any]) -> Tuple[Any, Any]:
    """Seam (mocked in tests): build the real agent for `agent_kind`.

    Construction lives in `runtime/agents/voice_registry.py` — one registered
    builder per agent kind, and nothing in THIS module is kind-specific.
    Giving a new agent kind duplex voice is one `register_voice_agent_builder`
    call there; a new tenant/profile of an already-registered kind is pure
    admin-portal config (profile, model row, persona prompt, channel), no
    code at all.

    Callers MUST gate `agent_kind` against the active deployment scope
    (see `_agent_kind_allowed`) before reaching this — this function has no
    idea which deployment it's running in and will happily build whichever
    kind it's asked for.

    `params["profile_slug"]` carries the already-resolved
    `LiveVoiceConfig.agent_profile_slug` (the handshake resolves `cfg` before
    it gets here). It rides the params dict rather than a separate `cfg`
    argument on purpose: this function is a monkeypatch seam with a
    two-positional-argument contract across several test modules, so widening
    the SIGNATURE would break callers we don't own, while widening the dict
    that already carries every other per-connection value cannot.
    """
    from runtime.agents.voice_registry import build_voice_agent

    try:
        agent = build_voice_agent(agent_kind, params)
    except LookupError as exc:
        # Same wire behaviour as the pre-registry inline branch: unknown kind
        # -> HTTPException(400) -> the handshake's CLOSE_FORBIDDEN path.
        raise HTTPException(status_code=400, detail=str(exc))
    return agent, agent.profile


def _session_for(cfg, *, persona: str):
    """Seam (mocked in tests): build the provider session."""
    return build_realtime_session(cfg, persona=persona)


_FORWARDING_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded")


def _sim_override_allowed(websocket: WebSocket) -> bool:
    """May this connection swap in the eval-only simulated provider?

    Three independent gates, all required: the deploy opted in via
    VOICE_LIVE_SIM_ENABLED (set on the control-plane app only — never on
    public prod chat), the peer is loopback (eval runs connect to their
    own replica), and the handshake carries no forwarding header.

    That third check exists because "loopback" here is NOT trustworthy on
    its own: this app runs behind uvicorn with `--proxy-headers
    --forwarded-allow-ips="*"` (see Dockerfile) so that Cloud Run/ACA's
    real edge proxy can report the true client IP. But `--forwarded-allow-ips="*"`
    means uvicorn's ProxyHeadersMiddleware trusts X-Forwarded-For from ANY
    peer — including on WebSocket scopes — and rewrites `scope["client"]`
    to the LEFTMOST entry, which is client-supplied. A remote caller who
    simply sends `X-Forwarded-For: 127.0.0.1` would otherwise sail through
    the loopback check on any deployment with the sim flag on. The eval
    driver (admin/voice_eval_driver.py) connects directly to
    `ws://127.0.0.1:$PORT` and never sends any forwarding header, so a
    genuine loopback eval connection is unaffected; anything that arrived
    via a proxy (real or spoofed) is denied.

    Denial is silent by design: the caller falls through to the profile's
    real provider, so the param can't be used to probe what this deploy
    supports.
    """
    if os.getenv("VOICE_LIVE_SIM_ENABLED", "").strip().lower() not in {
        "1", "true", "yes", "on"
    }:
        return False
    return _trusted_loopback(websocket)


def _trusted_loopback(websocket: WebSocket) -> bool:
    """A genuinely local peer: loopback address AND no forwarding header (see
    `_sim_override_allowed`'s docstring for why the header check is load-
    bearing behind `--forwarded-allow-ips="*"`). Shared by the sim-provider
    override and the `?simRun=1` session tag — both are claims only the
    in-process harnesses (eval driver, scenario runner) should be able to
    make; accepting them from the open internet would let a hostile caller
    self-badge as harness traffic and get dismissed in triage."""
    client = getattr(websocket, "client", None)
    host = getattr(client, "host", None) or ""
    if host not in {"127.0.0.1", "::1"}:
        return False
    headers = getattr(websocket, "headers", None) or {}
    return not any(h in headers for h in _FORWARDING_HEADERS)


def _same_origin(websocket: WebSocket, origin: Optional[str]) -> bool:
    """True when the handshake's Origin names the very host serving it.

    A browser always sets both headers itself, so a cross-site page cannot
    make them agree; a non-browser caller can forge either, but it can just
    as well omit Origin, which the gate already lets through. Compares host
    and port only: behind TLS-terminating ingress the page is https while
    the hop to uvicorn is not.
    """
    if not origin:
        return False
    try:
        netloc = urlsplit(origin).netloc.lower()
    except ValueError:
        return False
    host = ((getattr(websocket, "headers", None) or {}).get("host") or "").lower()
    return bool(netloc) and netloc == host


def _flag_on(value: Optional[str]) -> bool:
    """Parse a query-param boolean: '0'/'false'/'no'/'off'/'' are OFF.
    `bool("0")` is True in Python — a caller sending ?simRun=0 means no."""
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _uuid_or_none(value: Optional[str]) -> Optional[str]:
    """`value` when it is a real UUID, else None. `ai_voice_recordings.
    business_id` is a uuid column, but the resolved business id can be a
    tenant's merchant id in any format (`_budget_key` makes the same
    point) — inserting a non-UUID would 22P02 the whole recording away."""
    try:
        return str(uuid.UUID(str(value)))
    except Exception:  # noqa: BLE001
        return None


def _agent_kind_allowed(agent_kind: str) -> bool:
    """False iff a deployment scope is active and `agent_kind` isn't its own.

    `build_agent_for_kind` branches purely on the `agentKind` query param
    with no awareness of the deployment it's running in, and
    `enforce_channel_kind` no-ops whenever there's no `Origin` header (any
    non-browser client can just omit it). Without this, one scope's build
    would happily build another scope's agent for `?agentKind=<theirs>` --
    exactly the cross-scope bleed the HTTP deployment gate exists to prevent, just on
    a surface the gate can't see (WebSocket scopes bypass it entirely; see
    the module mounting this router in main.py).
    """
    scope = active_scope()
    if scope is None:
        return True
    return agent_kind in scope_kinds(scope)


def _resolve_portal_session(agent_kind: str, conn: WebSocket) -> Optional[Dict[str, Any]]:
    """Read the portal's signed session cookie for this agent kind, if any.

    A browser's `WebSocket` constructor cannot set custom headers, so the
    a partner portal client's identity can only reach this handshake via the
    same signed httpOnly cookie `main.py`'s HTTP chat reads through
    `read_session(http_request)` -- cookies ARE sent on a same-origin WS
    handshake, and `read_session` only touches `HTTPConnection.cookies`,
    which `WebSocket` provides too (a kind's registered session reader takes
    an `HTTPConnection` for exactly this reason).

    Returns None for any agent_kind with no portal-session concept, a
    missing cookie, a tampered/expired one (both already handled inside
    `read_session` itself), or an unexpected error reading it -- every one
    of those must degrade to the existing anonymous connection, never an
    error. This runs after `accept()` with no wrapping try/except at the
    call site, so the guard has to live in here.
    """
    try:
        from runtime.agents.voice_registry import voice_session_reader

        reader = voice_session_reader(agent_kind)
        if reader is not None:
            return reader(conn)
    except Exception:
        logger.warning(
            "voice.live: portal session lookup failed for agent_kind=%s",
            agent_kind,
            exc_info=True,
        )
    return None


def _budget_identity(
    *,
    business_id_param: Optional[str],
    portal_session: Optional[Dict[str, Any]],
    verified_user_id: Optional[str],
) -> Optional[str]:
    """Which business id the daily budget is charged to — server-derived first.

    `/voice/live` requires no authentication (`authenticate_optional` never
    raises), so `businessId` is just a string the caller typed. Trusting it
    unconditionally, as this did, meant a client could rotate it to mint a
    fresh daily budget per connection — the persisted cap became decorative —
    and could equally aim someone ELSE's `ai_voice_usage_daily` row and burn
    their day.

    Precedence, strongest evidence first:
      1. the portal session's merchant id — a signed httpOnly cookie this
         server issued, so it cannot be chosen by the caller;
      2. the `businessId` param, but ONLY for a caller whose bearer token
         SIGNATURE-VERIFIED (an operator or direct-API integration that already
         knows the ids). Merely *presenting* a token is not enough: the
         unverified decode accepts any self-minted `{"sub": ...}`, so trusting
         that would hand the param straight back to an attacker and reopen both
         holes this function exists to close;
      3. nothing — the caller is anonymous, so `_budget_key` derives a stable
         per-caller key from the rate key instead.
    """
    merchant_id = (portal_session or {}).get("merchantId")
    if merchant_id:
        return str(merchant_id)
    if verified_user_id and business_id_param:
        return business_id_param
    return None


def _budget_key(business_id: Optional[str], tenant_key: str) -> str:
    """The id `reserve_audio_seconds` charges the daily audio budget against.

    `business_id` here is the RESOLVED tenant business id (query param or
    portal-session merchant id — see the handshake) — deliberately NOT
    `tenant_key`, which stays IP/rate-key-scoped for the concurrency cap (see
    the handshake's comment on why merchant-scoping concurrency would
    collapse a demo portal's shared login onto one shared cap). Budget
    accounting is exactly the opposite: it MUST be merchant-scoped, or the
    persisted daily cap becomes per-caller-IP instead — 8 staff on 8 IPs get
    8x the intended spend, two merchants behind one NAT share (and drain)
    each other's budget, and every `ai_voice_usage_daily.business_id` in prod
    becomes a uuid5 of an IP, making per-tenant spend attribution useless.

    `ai_voice_usage_daily.business_id` is `uuid NOT NULL`, and
    `reserve_audio_seconds` fails OPEN on any DB error (silently unmetered) —
    so a `business_id` that isn't already a valid UUID (an anonymous portal
    has none at all; a tenant's merchant id format isn't ours to control) must be uuid5-
    derived rather than passed straight through, or the upsert would just
    fail and re-open the exact hole this function exists to close. The
    anonymous branch (no business_id at all) derives from `tenant_key` (an
    IP or "anonymous") as before, so repeat sessions from the same anonymous
    caller land on the same daily budget row.
    """
    if business_id:
        try:
            return str(uuid.UUID(str(business_id)))
        except (ValueError, AttributeError, TypeError):
            return str(uuid.uuid5(_ANON_BUDGET_NAMESPACE, f"voice-live-business:{business_id}"))
    return str(uuid.uuid5(_ANON_BUDGET_NAMESPACE, f"voice-live-anon:{tenant_key}"))


@router.websocket("/live")
async def voice_live(
    websocket: WebSocket,
    agentKind: str = Query(...),
    businessId: Optional[str] = Query(None),
    sessionId: Optional[str] = Query(None),
    locale: Optional[str] = Query(None),
    provider: Optional[str] = Query(None),
    simRun: Optional[str] = Query(None),
    # Outbound collection / sales simulations only: which fixture case the
    # tester dialled (the agent's own case fixtures). Ignored by every other
    # kind's builder.
    callCase: Optional[str] = Query(None),
):
    resolve_channel(websocket)
    # A WebSocket handshake gets no CORS preflight -- a browser will happily
    # open one to an arbitrary origin regardless of this server's CORS
    # allow-list, unlike fetch/XHR. Before the portal-session-cookie fix
    # (Task 13 review) this route carried no ambient credential, so an
    # unmatched Origin was harmless; now a portal's session cookie rides
    # along with it. `enforce_channel_kind` deliberately SKIPS when the
    # Origin matches no channel (the non-browser/server-to-server path
    # intentionally has no Origin at all) -- that's still correct for
    # callers with NO Origin header, unchanged below.
    #
    # THIS GATE IS OPT-IN AND INACTIVE BY DEFAULT -- it is NOT "reject any
    # unmatched Origin". `_match_channel` is an exact-string match against
    # `agent_channels.allowed_origins`, and today's demo-stack seeders seed
    # that channel row with `allowed_origins: []` (empty -- those stacks ship
    # `ALLOWED_ORIGINS=*` for CORS, i.e. no fixed origin is known at seed
    # time). Rejecting on ANY unmatched Origin would
    # therefore reject every real browser voice connection on every current
    # deployment (each one sends a same-origin Origin header) in exchange
    # for closing a risk `SameSite=Lax` already mitigates in current
    # browsers -- a certain break traded for a mitigated risk, the wrong
    # trade. So: reject an unmatched Origin ONLY once an operator has
    # actually opted in by giving SOME channel a non-empty `allowed_origins`
    # (`any_channel_has_origin_allowlist()`, mirroring `enforce_channel_kind`'s
    # own "empty allowed_agent_kinds = no restriction" convention, just
    # evaluated across the whole active-channel set since there's no single
    # matched channel to check "its own" list against when nothing matched).
    # Until that happens, the ONLY protection against a cross-site
    # WebSocket riding the session cookie is `SameSite=Lax` -- fine for a
    # top-level navigation, but NOT if the cookie is ever changed to
    # `SameSite=None` (e.g. to allow iframing the portal on a partner page).
    # Anyone making that cookie change MUST populate `allowed_origins` for
    # the deployment's channel FIRST, in `/admin/channels` -- see
    # `docs/admin-portal/00-architecture.md`'s Origin gate note.
    origin_header = websocket.headers.get("origin")
    # A page this engine served ITSELF (the /agents and other portals,
    # mounted on the same host as this socket) is not cross-site, which is
    # all the gate defends against — so it never needs a channel row. Before
    # this, any deployment whose gate was active rejected its own portals'
    # voice (found on the AI-agents demo portal, 2026-10-07).
    origin_present_and_unmatched = (
        bool(origin_header)
        and getattr(websocket.state, "channel", None) is None
        and not _same_origin(websocket, origin_header)
    )

    # Set only by a slug-addressed route (see VoiceRoute); None on the plain
    # socket, where everything below behaves exactly as it always has.
    route = _voice_route(websocket)
    agent_slug = route.agent_slug if route else None
    if agent_slug:
        # Which agent of the shared kind: a channel may allow just this one
        # (core.channel_resolver.enforce_channel_kind reads it from here).
        websocket.state.agent_slug = agent_slug
    if route is not None and route.own_page:
        # The portal's own page, checked by the route: not a channel's caller.
        websocket.state.channel = None
    route_caller: Optional[Dict[str, Any]] = None

    authorization = websocket.headers.get("authorization")
    user_token, user_id = authenticate_optional(authorization)
    # `user_id` above is decoded WITHOUT verifying the signature, which is fine
    # for the places it only scopes an RLS-checked read. It is NOT fine as
    # evidence of identity, and two decisions here need exactly that (whose
    # budget to charge, and whose concurrency slot to take). So verify it
    # separately and keep the two notions apart: a forged `{"sub": ...}` still
    # yields `user_id` (harmless) but never `verified_user_id`.
    verified_user_id = None
    if route is not None:
        # The route's own caller check decides, not a plain login: a generic
        # agent's callers are operators and API keys.
        user_token, user_id = None, None
        if authorization:
            route_caller = await _route_caller(route, authorization)
        if route_caller is not None:
            user_token = authorization.split(" ", 1)[1].strip()
            user_id = route_caller.get("user_id")
            verified_user_id = _caller_principal(route_caller)
    elif user_token:
        try:
            # Function-local import on purpose: core.auth is the seam the
            # test suite monkeypatches for signature verification.
            from core.auth import validate_token_verified

            # Off the loop: a cold JWKS cache is a network fetch (`_HANDSHAKE_OFF_LOOP`).
            verified_user_id = (
                await asyncio.to_thread(validate_token_verified, user_token) or {}
            ).get("sub")
        except Exception:
            # Unverifiable (forged, expired, or SUPABASE_JWT_SECRET unset) —
            # treat as anonymous rather than failing the socket: this endpoint
            # deliberately serves unauthenticated portal visitors.
            logger.debug("voice.live: bearer token did not verify", exc_info=True)

    # A browser's WebSocket constructor cannot set an Authorization header,
    # so a portal client never arrives with a bearer token --
    # its identity travels in the same signed httpOnly session cookie the
    # portal's HTTP chat resolves via read_session() (see
    # _resolve_portal_session). Only consulted when the header yielded
    # nothing, so an explicit bearer token always wins. An anonymous
    # portal's session carries no token at all (a visitor id), so this is a
    # no-op for it either way.
    portal_session = None if user_token else _resolve_portal_session(agentKind, websocket)
    if portal_session:
        user_token = portal_session.get("token") or user_token

    # Resolved here (right after the session is known) so every downstream
    # consumer -- the persisted daily audio-second budget, the built agent,
    # and the chat identity -- sees the same values. The explicit query
    # param always wins (an operator/direct-API caller who already knows the
    # ids shouldn't be second-guessed by a stale cookie); otherwise fall back
    # to what was resolved at portal login, matching what the portal's HTTP
    # chat stream does with the same session.
    #
    # Deliberately NOT threaded into `rate_key`/`tenant_key` below: the
    # demo portal may be one shared login, so every visitor's session
    # carries the SAME merchant id and userId -- feeding either into the
    # connect-rate-limiter or the per-tenant concurrency cap would collapse
    # every simultaneous demo visitor onto one shared budget of
    # connections/concurrent sessions instead of one each.
    resolved_business_id = businessId or (portal_session or {}).get("merchantId")
    merchant_user_id = ((portal_session or {}).get("user") or {}).get("userId")
    resolved_store_ids = (portal_session or {}).get("storeIds") or []
    resolved_stores = (portal_session or {}).get("stores") or []

    # The peer IP is the only thing here a caller cannot choose, so it is the
    # BASE of the key and a claimed identity may only narrow it. Composing an
    # unverified `sub` in (as `voice.py::_rate_key` does for the same reason)
    # would still let a rotating forged sub mint fresh buckets, so only a
    # VERIFIED id narrows; everyone else shares their IP's bucket.
    peer_ip = websocket.client.host if websocket.client else ""
    rate_key = f"{peer_ip}|{verified_user_id}" if verified_user_id else peer_ip

    # accept() FIRST: a close() before accept() becomes an opaque HTTP 403 at
    # the ASGI layer (see module docstring) -- every rejection below needs a
    # real WS close frame to reach the client, so every one of them runs
    # after this line.
    await websocket.accept()

    if origin_present_and_unmatched:
        if any_channel_has_origin_allowlist():
            # An operator HAS opted in (some channel has a non-empty
            # allowed_origins) -- this Origin doesn't match any of them, so
            # it's genuinely not permitted. Logged at WARNING too: a
            # legitimate caller hitting this means either a channel's
            # allow-list is stale or the portal moved to a new origin.
            logger.warning(
                "voice.live: rejecting agent_kind=%s -- Origin %r matched no "
                "configured channel allow-list",
                agentKind, origin_header,
            )
            await websocket.close(
                code=CLOSE_FORBIDDEN,
                reason="Origin is not permitted for this deployment.",
            )
            return
        # Gate INACTIVE: no channel anywhere has opted into origin
        # restriction, so there is nothing configured to enforce against --
        # allow the connection (see the module-level comment above this
        # check for why rejecting here would be wrong). Logged at WARNING
        # regardless, since a silent allow is exactly how this gets
        # forgotten -- an operator scanning logs should be able to find
        # "the duplex Origin gate is not doing anything yet" on their own.
        logger.warning(
            "voice.live: Origin %r matched no channel for agent_kind=%s, and no "
            "channel has an allowed_origins configured -- the duplex Origin gate "
            "is INACTIVE (opt in via /admin/channels); allowing the connection.",
            origin_header, agentKind,
        )

    try:
        enforce_channel_kind(websocket, agentKind)
        _connect_limiter.check(rate_key)
    except HTTPException as exc:
        code = CLOSE_RATE_LIMITED if exc.status_code == 429 else CLOSE_FORBIDDEN
        await websocket.close(code=code, reason=str(exc.detail)[:120])
        return

    if not _agent_kind_allowed(agentKind):
        await websocket.close(
            code=CLOSE_FORBIDDEN,
            reason=f"agentKind '{agentKind}' is not served by this deployment.",
        )
        return

    if voice_agent_slug_addressed(agentKind) and route is None:
        # Many profiles share this kind, so "the kind's active profile" names
        # no agent. It is only served where the route names the slug.
        await websocket.close(
            code=CLOSE_FORBIDDEN,
            reason="This agent is called at /v1/agents/{slug}/voice.",
        )
        return

    # First-frame auth for kinds that demand a real identity. Runs after
    # accept() (frames can only be received once accepted) and BEFORE
    # config/budget/agent build so everything identity-scoped downstream —
    # rate_key narrowing, the budget identity, the built agent's tokens —
    # sees the verified caller.
    kind_credential = voice_credential(agentKind)
    tenant_access = voice_tenant_access(agentKind)
    context_keys = voice_client_context_keys(agentKind)
    # The kind's second credential (see VoiceCredential), if it has one.
    credential_value: Optional[str] = None
    initial_context: Dict[str, Any] = {}
    auth_frame_data: Dict[str, Any] = {}
    if voice_agent_requires_auth(agentKind) and verified_user_id:
        # The bearer HEADER already signature-verified — the auth frame
        # exists only because a browser WebSocket cannot set headers, so a
        # server-to-server caller (the voice eval driver's live mode, ops
        # tooling) is not asked to repeat the same credential as a frame.
        # The kind's second credential rides a header for the same reason.
        if kind_credential is not None:
            credential_value = websocket.headers.get(kind_credential.header) or None
    elif voice_agent_requires_auth(agentKind) and tenant_access is None and (
        (provider or "").strip().lower() == "simulated"
        and _sim_override_allowed(websocket)
    ):
        # NOT a tenant-access kind: its brain reads the tenant's data AS the
        # caller and binds tools from the frame, so a sim run without the
        # auth frame is a call with no data at all — the eval driver sends
        # the frame (voice_ctx.auth) and it is verified below like any
        # caller's.
        # The eval harness's trusted sim path: VOICE_LIVE_SIM_ENABLED on
        # this deploy AND a loopback peer AND an explicit simulated request.
        # That is a replica the operator already controls — a JWT proves
        # nothing there, and demanding one would wall those kinds' suites out
        # of the sim eval tier. A non-loopback (or flag-off) ?provider=simulated
        # falls through to the frame gate below, so the param cannot become
        # an auth bypass.
        pass
    elif voice_agent_requires_auth(agentKind):
        auth_frame = await _receive_auth_frame(websocket)
        frame_sub = None
        if route is not None and auth_frame and auth_frame.get("token"):
            route_caller = await _route_caller(route, f"Bearer {auth_frame['token']}")
            if route_caller is not None:
                frame_sub = _caller_principal(route_caller)
        elif auth_frame and auth_frame.get("token"):
            try:
                # Same function-local import as the header path above — one
                # monkeypatch seam (core.auth) for signature verification.
                from core.auth import validate_token_verified

                frame_sub = (
                    await asyncio.to_thread(validate_token_verified, auth_frame["token"]) or {}
                ).get("sub")
            except Exception:
                logger.info(
                    "voice.live: auth frame token failed verification for %s",
                    agentKind, exc_info=True,
                )
        if not frame_sub:
            await websocket.close(code=CLOSE_FORBIDDEN, reason="Authentication required.")
            return
        user_token = auth_frame["token"]
        user_id = route_caller.get("user_id") if route_caller is not None else frame_sub
        verified_user_id = frame_sub
        if kind_credential is not None:
            credential_value = auth_frame.get(kind_credential.name) or None
        initial_context = _whitelist_context(auth_frame.get("context"), context_keys)
        auth_frame_data = auth_frame
        # Narrow the identity-scoped keys now that a verified sub exists —
        # the connect limiter already ran on the IP-only key (acceptable:
        # IP stays the base), but the concurrency cap and budget identity
        # below must see the verified caller.
        rate_key = f"{peer_ip}|{verified_user_id}" if verified_user_id else peer_ip

    # A kind with a second credential reads its backend AS the caller —
    # never as a project-wide credential. The trusted simulated-eval path
    # (loopback + flag, no auth frame at all) is exempt; every real caller
    # must carry the credential.
    if (
        kind_credential is not None
        and voice_agent_requires_auth(agentKind)
        and not credential_value
        and (verified_user_id or auth_frame_data)
    ):
        await websocket.close(code=CLOSE_FORBIDDEN, reason=f"{kind_credential.name}_required")
        return
    if kind_credential is not None and credential_value and kind_credential.verify is not None:
        # Validate it (and that it is this caller's) BEFORE any
        # agent/budget/provider work — the kind's own check.
        try:
            await asyncio.to_thread(kind_credential.verify, credential_value, verified_user_id)
        except VoiceCredentialRefused as exc:
            logger.info("voice.live: %s %s refused: %s", agentKind, kind_credential.name, exc.reason)
            await websocket.close(code=CLOSE_FORBIDDEN, reason=exc.detail[:120])
            return
    if is_sensitive_kind(agentKind):
        # Every span this connection's task (and the tasks/threads it spawns)
        # starts belongs to a redacted trace → exported with content stripped.
        # The handler runs in its own task context, so this cannot leak to
        # other connections.
        enter_redacted_scope()

    # A tenant-access kind (see VoiceTenantAccess): the caller must belong to
    # the tenant they name — budget, recording and every tool are keyed by
    # businessId. Runs before config/budget so a non-member costs nothing.
    tenant_params: Dict[str, Any] = {}
    pinned_ctx: Dict[str, Any] = {}
    if tenant_access is not None:
        if not (user_token and verified_user_id) or not await asyncio.to_thread(
            tenant_access.admit, user_token, businessId
        ):
            await websocket.close(code=CLOSE_FORBIDDEN, reason=tenant_access.refused_reason)
            return
        if not auth_frame_data:
            # Verified-Authorization-HEADER callers (see the auth block
            # above) never send the `{"type":"auth", ...}` frame, so the
            # kind's frame reader receives `{}` — the agent authenticates
            # fine but may bind no tools, silently, unless this is surfaced.
            logger.warning(
                "voice.live: %s call without an auth frame: frame-supplied "
                "access defaults to none — clients must send the auth frame "
                "first and no Authorization header", agentKind,
            )
        tenant_params, tenant_ctx = tenant_access.frame_params(auth_frame_data)
        pinned_ctx = {"user_id": verified_user_id, **tenant_ctx}

    if route is not None and route.profile_pin:
        # A profile pin lets an operator call a draft copy of the agent. Only
        # an operator may: a verified superuser token, or the trusted
        # simulated-eval path (a loopback replica the operator controls).
        if ((provider or "").strip().lower() == "simulated" and _sim_override_allowed(websocket)) or (
            user_token and await asyncio.to_thread(_is_operator_token, user_token)
        ):
            route.pin = route.profile_pin
        else:
            logger.warning(
                "voice.live: profile pin ignored for agent %r: the caller is not an operator",
                agent_slug,
            )

    try:
        # to_thread, like every other config/budget/profile call in this
        # handshake — see `_HANDSHAKE_OFF_LOOP` below for why.
        cfg = await asyncio.to_thread(_live_config_for, websocket, agentKind)
    except (MissingProfileError, MissingSnapshotError) as exc:
        # Only a slug-addressed route raises these here (the kind-addressed
        # load is tolerant): no such agent, or one never published to this
        # environment. Never answered by another agent.
        logger.info("voice.live: no agent %r (%s)", agent_slug, exc)
        await websocket.close(code=CLOSE_FORBIDDEN, reason="Agent not found.")
        return
    except LookupError as exc:
        if route is not None:
            # hydrate_live_model: voice is on but no realtime model row is
            # set, or the row is gone. Say what to do about it.
            logger.info("voice.live: agent %r has no usable realtime model (%s)", agent_slug, exc)
            await websocket.close(
                code=CLOSE_FORBIDDEN,
                reason="No realtime voice model is set for this agent. Pick one on its Voice tab.",
            )
            return
        # hydrate_live_model() raises when voice_config.live names a model_id
        # that isn't a real agent_models row -- a misconfigured tenant, same
        # bucket as the persona-prompt LookupError below.
        await websocket.close(code=CLOSE_FORBIDDEN, reason=str(exc)[:120])
        return
    except Exception:
        # _live_config_for also calls get_service_client()/fetch_model(),
        # which can raise ValueError (missing AI Supabase env vars) or a
        # network error -- neither is the caller's fault, so it's a
        # provider-side failure, not "this tenant is forbidden". Must be
        # caught: this runs right after accept(), and an unguarded raise
        # here would abort the ASGI task with no close frame at all.
        logger.exception("voice.live: config resolution failed for agent_kind=%s", agentKind)
        await websocket.close(code=CLOSE_PROVIDER_ERROR, reason="Voice configuration is unavailable.")
        return

    sim_override_applied = False
    if (provider or "").strip().lower() == "simulated" and _sim_override_allowed(websocket):
        # Eval-only: run this session on the in-process simulated provider.
        # cfg is per-connection (resolve_live_config builds a fresh object
        # every call), so mutating the slug here cannot leak into another
        # session.
        cfg.provider = "simulated"
        sim_override_applied = True

    if not cfg.enabled:
        await websocket.close(
            code=CLOSE_FORBIDDEN,
            reason=("Voice is not turned on for this agent." if route is not None
                    else "Duplex voice is disabled for this tenant."),
        )
        return

    if route is not None:
        # A realtime provider needs a key this server may not have. Refuse
        # with the reason before opening anything, instead of a bare
        # "provider unavailable" from inside session.start().
        problem = config_problem(cfg)
        if problem:
            logger.warning("voice.live: agent %r cannot take a call: %s", agent_slug, problem)
            await websocket.close(code=CLOSE_PROVIDER_ERROR, reason=problem[:120])
            return

    _apply_text_only_overrides(cfg)
    _apply_kind_voice_defaults(cfg, agentKind)
    if route is not None and route.greeting and not cfg.text_only:
        cfg.greeting_trigger = route.greeting
    # {tool name: handler} once the agent is built — non-empty only in
    # pre-fetched-context mode (see _direct_tools_for).
    direct_handlers: Dict[str, Any] = {}
    direct_turn_sink: Any = None
    direct_turn_text = {"buf": ""}

    # The default chat-session id. IP/rate-key-scoped ON PURPOSE (see
    # `resolved_business_id`'s comment above): a demo portal may be one
    # shared login, so keying this on the merchant id would collapse every
    # simultaneous visitor onto ONE chat session.
    tenant_key = businessId or rate_key or "anonymous"

    # The concurrency cap's key, deliberately NOT `tenant_key`. `businessId` is
    # an unauthenticated query param, so while it led that expression a caller
    # could rotate it and open unlimited simultaneous sessions -- the cap only
    # ever bound callers who didn't think to change it. `rate_key` is the
    # caller's own identity (bearer sub, else peer IP), which is the closest
    # thing this socket has to a party to hold responsible.
    concurrency_key = rate_key or "anonymous"
    # Two ceilings, because one is not enough. The per-identity cap gives each
    # caller a fair share; the per-IP cap is what actually BOUNDS abuse, since
    # only a verified id narrows `rate_key` and an unverified caller could
    # otherwise present a new identity per connection. The IP ceiling is the
    # looser of the two so genuine shared egress (a store's staff behind one
    # NAT) is not punished.
    ip_key = f"ip:{peer_ip}" if peer_ip else "ip:anonymous"
    if (
        _active.get(concurrency_key, 0) >= int(cfg.max_concurrent_sessions)
        or _active.get(ip_key, 0) >= int(cfg.max_concurrent_per_ip)
    ):
        await websocket.close(code=CLOSE_RATE_LIMITED, reason="Too many concurrent voice sessions.")
        return

    # Charged at handshake so an abandoned socket still costs something; the
    # periodic charger and the teardown charge (see `charged` below) then
    # book the rest of the session's real duration on top of this advance.
    handshake_seconds = max(0, int(cfg.handshake_charge_sec))
    # Tenant-extra acknowledgement tokens, normalised once per connection.
    backchannel_extra = _extra_backchannel_tokens(cfg)
    max_session_seconds = max(1.0, float(cfg.max_session_sec))

    # Budget IS merchant-scoped, unlike the concurrency key -- a persisted
    # daily spend cap keyed by IP would let 8 staff on 8 IPs spend 8x the
    # intended budget, and two merchants behind one NAT would share (and
    # drain) each other's. See `_budget_identity` for why the query param
    # alone is not evidence of who to charge, and `_budget_key` for the uuid
    # derivation.
    # A slug-addressed agent has no tenant: its calls are metered (and
    # recorded) under one bucket per agent. See `agent_budget_id`.
    agent_bucket = agent_budget_id(agentKind, agent_slug) if agent_slug else None
    budget_key = _budget_key(
        agent_bucket or _budget_identity(
            business_id_param=businessId,
            portal_session=portal_session,
            verified_user_id=verified_user_id,
        ),
        # `concurrency_key`, NOT `tenant_key`, for the anonymous derivation.
        # `tenant_key` is `businessId or rate_key`, so falling back to it left
        # the whole bypass intact by the back door: an anonymous caller who
        # rotated `businessId` still landed on a different uuid5 every time
        # and so got a fresh daily budget per connection.
        concurrency_key,
    )
    # A simulated (eval) call costs no audio, and it billed the TENANT's real
    # daily budget: one local eval pass of a voice suite used 3584 of
    # one tenant's 3600 seconds and its owner's next real call was refused
    # (2026-09-29). A separate capped bucket then filled up mid-suite too, so
    # sim calls are simply unmetered — they only exist on a loopback replica
    # with VOICE_LIVE_SIM_ENABLED, and the concurrency caps still apply.
    def _metered(name: str):
        def call(*args: Any, **kwargs: Any) -> Any:
            if sim_override_applied:
                return 0
            return globals()[name](*args, **kwargs)
        return call

    _check_budget = _metered("check_audio_budget")
    _reserve_budget = _metered("reserve_audio_seconds")
    # READ-ONLY here. The handshake's seconds are charged only once the
    # provider session is actually up (see after `session.start()` below):
    # charging first meant a client looping against a broken provider drained
    # the tenant's whole day without ever speaking, since nothing refunds it.
    try:
        await asyncio.to_thread(
            _check_budget, budget_key, seconds=handshake_seconds
        )
    except BudgetExhausted as exc:
        await websocket.close(code=CLOSE_RATE_LIMITED, reason=str(exc)[:120])
        return

    try:
        # build_agent_for_kind runs FIRST so its returned `profile` (the voice
        # SPECIALIST, when the parent names one) is in scope for
        # resolve_persona_prompt — it is only the misplaced-pin fallback there
        # since the persona pin lives on the parent (see that docstring).
        agent, profile = await asyncio.to_thread(
            build_agent_for_kind,
            agentKind,
            {
                "user_token": user_token,
                "business_id": resolved_business_id,
                "user_id": merchant_user_id,
                "store_ids": resolved_store_ids,
                "stores": resolved_stores,
                "locale": locale,
                # Serve the call from a DIFFERENT profile of the same kind
                # when the operator named one (voice_config.live.
                # agent_profile_slug) — a voice-optimized agent: short spoken
                # answers, terminal-keyed sales, no charts or CSV. `cfg` is
                # already resolved above; everything downstream (the persona
                # lookup below, run_turn's prompt/tools/model, and the turn's
                # telemetry) then sees the voice profile because
                # build_agent_for_kind returns it as `profile`.
                "profile_slug": cfg.agent_profile_slug,
                # The kind's second credential (first-frame auth), under its
                # own name; absent for every kind that registers none.
                **({kind_credential.name: credential_value} if kind_credential else {}),
                # The outbound collection call's dialled case (None for every
                # other kind; their builders never read it).
                "call_case": callCase,
                # A tenant-access kind's frame params (VoiceTenantAccess).
                **tenant_params,
                # A slug-addressed call: which agent, and the operator's pin.
                **({"agent_slug": agent_slug, "profile_pin": route.pin,
                    "build_brain": route.build_brain, "brief": route.brief}
                   if route else {}),
            },
        )
        # Pre-fetched-context mode (outbound simulations): the case file is in
        # the persona, the provider declares the agent's action tools instead
        # of ask_agent, and the ask_agent machinery stands down.
        direct_handlers = _direct_tools_for(cfg, agent)
        direct_turn_sink = (getattr(agent, "note_assistant_turn", None)
                            if direct_handlers else None)
        # Pin the spoken/heard language. The realtime model does BOTH
        # transcription and speech, so leaving this unset lets it infer the
        # language from the audio and drift mid-session (reported in the wild
        # as Indian- and French-sounding readings). Precedence:
        #   1. a KNOWN portal locale -- the caller's explicit choice;
        #   2. the operator's voice_config.live.language row (any tag, incl.
        #      ones our map doesn't know, e.g. ar-EG);
        #   3. the map's English default, so the language is never unpinned.
        # An unknown non-blank locale is logged, not silently anglicised.
        mapped = speech_locale(locale)
        if mapped is not None:
            cfg.language = session_language(locale, cfg.language, mapped[0])
        elif locale:
            logger.warning(
                "voice.live: unknown portal locale %r -- keeping %s",
                locale, cfg.language or "the default language",
            )
        if not cfg.language:
            cfg.language = _LOCALE_SPEECH[_DEFAULT_LOCALE][0]
        # The persona's governance owner is the PARENT profile — the same row
        # `_live_config_for` read `voice_config` (and `persona_prompt_slug`)
        # off, and the row the portal's persona editor repoints. The
        # specialist rides along only so an unmigrated stack whose pin is
        # still on it keeps working, loudly (see resolve_persona_prompt).
        # Both halves in ONE worker thread: `_active_profile_for` is its own
        # profile read and `resolve_persona_prompt` then reads the prompt
        # version row. A lambda (not a partial) so the module globals are
        # resolved at call time and the test monkeypatches still bind.
        persona_vars = getattr(agent, "persona_variables", None)
        # A kind whose agents are configuration only supplies a built-in
        # persona for a profile that names none (a generic agent); every
        # other kind keeps the rule that a missing persona slug is a
        # misconfiguration.
        persona_fallback = None if cfg.persona_prompt_slug else voice_persona_fallback(agentKind)
        if persona_fallback is not None:
            persona = await asyncio.to_thread(
                lambda: persona_fallback(
                    _owner_profile_for(websocket, agentKind),
                    language=language_name(cfg.language),
                )
            )
        else:
            persona = await asyncio.to_thread(
                lambda: resolve_persona_prompt(
                    cfg, agentKind,
                    profile=_owner_profile_for(websocket, agentKind),
                    specialist_profile=profile,
                    language=language_name(cfg.language),
                    # Test doubles are often MagicMocks: only a real dict counts.
                    extra_variables=persona_vars if isinstance(persona_vars, dict) else None,
                )
            )
        session = _session_for(cfg, persona=persona)
        await session.start()
        # The session is real, so the handshake is now a genuine cost. Charged
        # HERE rather than before the build (see the read-only check above):
        # every failure path between the two returns without charging, so a
        # reconnect loop against a broken provider can no longer drain the
        # tenant's day. BudgetExhausted can't fire on this reservation in
        # practice -- the check above just passed -- but if it does, treat it
        # like any other setup failure and let the handler below close cleanly.
        await asyncio.to_thread(_reserve_budget, budget_key, handshake_seconds)
    except BudgetExhausted as exc:
        # Reachable despite the check above when a CONCURRENT session charges in
        # between. `session.start()` has already opened an upstream realtime
        # connection by this point, and this early return happens BEFORE the
        # try/finally that owns `session.close()` -- so without closing here we
        # would strand a live, billing provider session with nobody reading it,
        # in the very path meant to control spend.
        try:
            await session.close()
        except Exception:
            logger.debug("voice.live: session close after budget refusal failed", exc_info=True)
        await websocket.close(code=CLOSE_RATE_LIMITED, reason=str(exc)[:120])
        return
    except (LookupError, HTTPException) as exc:
        await websocket.close(code=CLOSE_FORBIDDEN, reason=str(exc)[:120])
        return
    except (MissingProfileError, MissingSnapshotError) as exc:
        if route is None:
            # Unchanged for a kind-addressed call: a setup failure.
            logger.exception("voice.live: agent/persona/session setup failed for agent_kind=%s", agentKind)
            await websocket.close(code=CLOSE_PROVIDER_ERROR, reason="Voice session setup failed.")
            return
        # The brain a generic agent names (voice_config.agent_profile_slug)
        # does not exist here. Fail closed: no other agent answers.
        logger.warning("voice.live: agent %r: brain profile not found (%s)", agent_slug, exc)
        await websocket.close(code=CLOSE_FORBIDDEN, reason="Agent not found.")
        return
    except RealtimeProviderError as exc:
        # The provider's own text stays server-side: it carries upstream
        # detail ("session.update rejected: 'session.voice' should be …",
        # retest 2026-09-28) the client has no use for and should not see.
        logger.error("voice.live: realtime provider failed to start: %s", exc)
        await websocket.close(code=CLOSE_PROVIDER_ERROR, reason="Voice provider unavailable.")
        return
    except Exception:
        # A transient DB/transport blip anywhere in this block (e.g.
        # resolve_persona_prompt's slug lookup, or a MissingProfileError from
        # build_agent_for_kind) is not the caller's fault -- same "not a
        # misconfigured tenant" reasoning as _live_config_for's catch-all
        # above. Must be caught: an unguarded raise here would abort the
        # ASGI task with no close frame at all.
        logger.exception("voice.live: agent/persona/session setup failed for agent_kind=%s", agentKind)
        await websocket.close(code=CLOSE_PROVIDER_ERROR, reason="Voice session setup failed.")
        return

    session_metadata: Dict[str, Any] = {"channel": "voice"}  # read by core.conversation_insights
    if agent_slug:
        # Many agents share the kind: the slug is what says whose
        # conversation this is (same key the HTTP chat route stamps).
        session_metadata["agent_slug"] = agent_slug
    if portal_session:
        # Same tag the portal's HTTP chat sets, so a call placed BEFORE anything
        # is typed (the voice path creates the session row) still shows up in the
        # portal's History drawer. Only for cookie-bearing traffic: the scenario
        # harness connects without a portal session, and its runs must stay out.
        session_metadata["client"] = PORTAL_CLIENT_TAG
    # Audit linkage: the client-context keys the kind registers as session
    # metadata (mirrors what its HTTP chat route stamps on the session row).
    for _ctx_key in voice_session_metadata_keys(agentKind):
        if initial_context.get(_ctx_key):
            session_metadata[_ctx_key] = initial_context[_ctx_key]

    identity = TurnIdentity(
        agent_kind=agentKind,
        # A call with no client-supplied sessionId gets its OWN conversation.
        # This used to fall back to f"voice:{agentKind}:{tenant_key}" — and
        # `tenant_key` is `businessId or rate_key or "anonymous"`, i.e. CONSTANT
        # per tenant. coerce_session_id maps that constant to a deterministic
        # UUIDv5, so every anonymous call for an agent kind collapsed onto ONE
        # ai_chat_sessions row, one trace group in the observability explorer
        # (group_traces_by_session) and one recording bucket. On the landing-page
        # sales demo, where every visitor shares the one businessId, that meant
        # every caller's transcript accumulating in a single thread — and a
        # caller resuming a STRANGER's history rather than their own.
        #
        # Clients that want a spoken turn and a typed turn to share history send
        # their own sessionId (a portal page mints one at load);
        # an explicit value still wins, so reconnects still resume.
        session_id=sessionId or str(uuid.uuid4()),
        user_id=user_id or merchant_user_id,
        business_id=resolved_business_id,
        session_type=agentKind,
        # The kind's registered source (matching its HTTP chat path so
        # session analytics keyed on it keep working); "waj" otherwise.
        source=voice_identity_source(agentKind) or "waj",
        session_metadata=(session_metadata or None),
    )

    # One id per CONNECTION — the session id spans reconnects (it names the
    # chat conversation, and a second call in the same tab reuses it), so the
    # call recording needs its own key. Also stamped on every span of the call
    # as `waj.voice.call_id` (via turn_trace_attrs below) so the observability
    # explorer can match a trace back to its audio.
    call_id = str(uuid.uuid4())
    # What is said on the call, saved as a conversation when it ends
    # (voice/live/call_log.py). Only for an agent called at its own address:
    # its call is its own conversation. A kind whose client shares the session
    # with a typed chat keeps its history as it was, and a sensitive kind's
    # speech is never copied anywhere.
    call_log: Optional[CallLog] = (
        CallLog(identity, call_id=call_id)
        if agent_slug and not is_sensitive_kind(agentKind) else None
    )

    def _log_delegated(result: Any) -> None:
        """`run_turn` saved this turn's question (and its answer unless it
        failed), so the call log leaves the spoken copy out."""
        if call_log is not None and result is not None:
            call_log.delegated(ok=not getattr(result, "error", False))

    # A SIMULATED CONVERSATION is any bot-driven call: the text-only sim
    # provider, or a live-mode eval/scenario run that declares itself with
    # `?simRun=1` (admin/voice_eval_driver.py, admin/voice_scenario_runner.py).
    # Tagged on the call's spans + its recording so the observability screens
    # can tell these apart from real caller sessions. The declaration is
    # honoured only from a trusted loopback peer — the same gate the sim
    # provider override uses — because a public caller who can self-badge
    # "sim" gets dismissed as harness noise in triage, which is exactly the
    # cloak hostile traffic would want.
    is_sim_conversation = (session.provider == "simulated") or (
        _flag_on(simRun) and _trusted_loopback(websocket)
    )
    recorder: Optional[VoiceCallRecorder] = None
    # Calls of a sensitive kind (core.sensitive_content) are NEVER recorded:
    # no operator switch turns this on.
    if (recording_enabled() and not cfg.text_only and not is_sensitive_kind(agentKind)
            and _demo_may_record(websocket)):
        try:
            # Store under the COERCED session id — the same one chat
            # persistence keys `ai_chat_sessions` on and turn telemetry writes
            # to `agent_traces.session_id` — so the explorer's Audio tab joins
            # recordings to traces with a plain equality.
            from core.chat_persistence import coerce_session_id

            recorder = VoiceCallRecorder(
                call_id=call_id,
                session_id=coerce_session_id(str(identity.session_id)),
                agent_kind=agentKind,
                business_id=agent_bucket or _uuid_or_none(resolved_business_id),
                provider=session.provider,
                simulated=is_sim_conversation,
                input_sample_rate=session.audio_format.input_sample_rate,
                output_sample_rate=session.audio_format.output_sample_rate,
            )
        except Exception:  # noqa: BLE001 - recording must never block a call
            logger.warning(
                "voice.live: recorder init failed; call will not be recorded",
                exc_info=True,
            )
            recorder = None

    # Per-connection client turn context (the kind's `client_context_keys`),
    # seeded by the auth frame and REPLACED wholesale by each client
    # `context` frame — a user navigating to another record must not inherit
    # stale keys. Snapshotted (dict(...)) at each delegate call: a context frame
    # landing mid-turn applies to the NEXT turn, not the running one.
    delegate_ctx: Dict[str, Any] = {**initial_context, **pinned_ctx}

    _active[concurrency_key] = _active.get(concurrency_key, 0) + 1
    _active[ip_key] = _active.get(ip_key, 0) + 1
    # Monotonic "barge-in epoch", not a boolean: a single shared flag can't
    # tell two overlapping tool calls apart. `_handle_tool_call` captures the
    # epoch AT DISPATCH TIME and compares it against the current value when
    # its result is ready -- if an `interrupted` event bumped the counter in
    # between, THIS call was the one barged in on (SILENT), regardless of
    # whether some OTHER, later tool_call has since been dispatched.
    #
    # `confirmed`/`at` implement false-barge-in recovery: an `interrupted` is
    # only the provider's speech-START detection, which background noise trips
    # too. A real barge-in confirms itself within a beat — the overriding
    # utterance commits (`user_turn_end`) or becomes a new question
    # (`tool_call`). An epoch that is still unconfirmed when the delegated
    # answer is ready gets a short confirm window
    # (cfg.interrupt_confirm_window_sec, counted from `at`); if nothing
    # confirms it, the interrupt was noise and the answer is spoken after
    # all instead of being silently dropped.
    interrupt_epoch = {"n": 0, "confirmed": False, "at": 0.0}
    # When we last deliberately cut the floor ourselves — see
    # _SELF_CUT_ABSORB_SEC and the legacy `interrupted` arm in the pump loop.
    self_cut = {"at": 0.0}
    # Confirmed-barge-in state (see the spec in docs/superpowers/specs/
    # 2026-08-02-voice-live-confirmed-barge-in-design.md). Detection is not
    # confirmation: `interrupted` (speech-start over her voice) and
    # `playback_flush` (a commit) are both things BACKGROUND NOISE produces.
    # She is cut off — epoch bump, provider cancel, client flush — only when
    # the caller's transcript accumulates substantive content, or a
    # tool_call proves a new question. interrupt_confirm_window_sec doubles
    # as the held-flush safety valve; 0 restores the legacy immediate cut.
    pending_interrupt = {"at": None}          # last unconfirmed speech-start
    # Current utterance's confirmation latch, reset at its transcript final.
    # A dropped final leaves it latched for one utterance (the next barge-in
    # misses its epoch bump once) — accepted: the valve still flushes, so
    # the worst case is one superseded answer speaking, not a stuck session.
    utt_state = {"confirmed": False}
    # Caller-utterance clock: how many times the provider has reported the
    # caller STARTING TO SPEAK. Not a metric — it is the only boundary
    # between two caller utterances the endpoint can observe, and it is what
    # tells a `final` that BELONGS to an in-flight answer apart from one that
    # SUPERSEDES it.
    #
    # Both events that carry a speech onset tick it: `interrupted` (onset over
    # her voice) and `speech_started_idle` (onset into silence). Everything
    # else about one utterance — its commit, its transcript, the model's
    # tool_call for it — is DERIVED from that speech, so nothing belonging to
    # utterance N can arrive before N's onset. A `final` observed while the
    # clock still reads N therefore transcribes utterance N, whatever else
    # raced it (see the physics note in
    # tests/voice/test_voice_live_turn_invariants.py).
    #
    # `finalized` is the clock reading of the last utterance whose transcript
    # already closed. One utterance yields ONE final, so a second final at an
    # unchanged reading is a second utterance whose onset went unreported —
    # it must supersede, not belong. Belt and braces for a provider or a
    # turn-detection config that stops emitting speech-start (azure_voice_live
    # always configures VAD, so live this is never the deciding test); without
    # it, a session that reported no onsets at all would quietly lose the
    # supersede for the whole call.
    utterance_clock = {"n": 0, "finalized": -1}
    # Tool calls dispatched and not yet answered, keyed by call id. Each value
    # is that dispatch's own MUTABLE record: `epoch` (the barge-in epoch it is
    # measured against, see below) and `utterance` (the clock reading at
    # dispatch — which caller utterance it is answering). Mutable because a
    # `final` that turns out to belong to the same utterance CARRIES its
    # dispatch across the epoch bump instead of leaving it behind; see the
    # substantive-commit branch in the pump loop.
    inflight_answers: Dict[str, Dict[str, int]] = {}
    # Was the caller HEARING her when THIS utterance began? The provider's
    # `interrupted` answers that only for the case where it was still
    # GENERATING: it gates on `_response_active`, and generation runs ~4x
    # realtime, so the common shape by far is `speech_started_idle` — the
    # provider considers the answer delivered while the browser is still
    # playing seconds of it. Both are barge-in; only one is labelled.
    #
    # The converse is the case this flag exists for: an onset with an EMPTY
    # playback queue is ordinary turn-taking, not an interruption. Anything
    # queued by the time her transcript comes back was forwarded AFTER she
    # started speaking — the holding line or the reply to the very turn being
    # confirmed — so flushing it destroys the answer to her own question.
    # That is the 2026-08-05 "words are skipped" report: her filler
    # («إيه، ثانية وحدة») and the last sentences of the call, cut mid-word.
    onset = {"audible": False}
    held_flush = {"pending": False, "valve": None}
    # Greeting-echo guard (Fix 1, 2026-08-19 — see LiveVoiceConfig.
    # greeting_barge_in_guard_sec for the measured evidence). `active`
    # flips True the instant the greeting is dispatched below and False the
    # moment its own `turn_end` arrives -- whichever of THAT or `deadline`
    # (the cap, for a provider that never emits the turn_end) comes first is
    # read live at each `interrupted` event rather than tracked separately,
    # so the guard can never outlive either boundary.
    greeting_guard = {"active": False, "deadline": 0.0}
    # cfg.greeting_input_mute_sec: caller audio goes to the provider as
    # silence until the greeting has been generated (`generated`, its
    # turn_end) and played out, or the cap. See the field's comment.
    greeting_mute = {"active": False, "deadline": 0.0, "generated": False, "since": 0.0}
    # Set when the greeting is dispatched, cleared by the first turn_end —
    # which then carries `greeting: true` (see protocol.turn_end_frame).
    greeting_turn = {"pending": False}

    def _is_greeting_caption(role: Optional[str], out_of_band: bool) -> bool:
        """An assistant caption of the greeting response: every caption from
        the greeting's dispatch to its turn_end, even if the caller talks
        during it (responses are sequential, so nothing else is in between)."""
        return (
            (role or "assistant") == "assistant" and not out_of_band
            and greeting_turn["pending"]
        )

    # The response's reply verdict for its FIRST caption actually sent (a
    # provider stamps it on its first assistant transcript event, which the
    # tool-syntax scrubber may hold back — so it waits for the first frame).
    reply_hint: Dict[str, Optional[bool]] = {"value": None}

    def _take_reply_hint(role: Optional[str], out_of_band: bool) -> Optional[bool]:
        if (role or "assistant") != "assistant" or out_of_band:
            return None
        value, reply_hint["value"] = reply_hint["value"], None
        return value
    # Mirror of the browser's playback cursor (`_playAt` in
    # static/shared/voice-live.js): the wall-clock instant the audio already
    # forwarded will finish playing. The service streams a reply FAR faster
    # than realtime -- 28s of speech in 6s, measured live 2026-08-04 -- so for
    # most of an answer the caller is still listening to audio the provider
    # already considers delivered, and `turn_end` has long since fired. A
    # flush in that window destroys the unheard remainder: the answer stops
    # mid-sentence and the model, believing it said all of it, has nothing to
    # add. That is the "doesn't finish the last sentence, then dead air"
    # report. So the two UNCONFIRMED release paths below consult this; the
    # confirmed path (_confirm_barge_in) consults it only when the caller was
    # NOT hearing her at speech onset (see `onset` above) -- a real barge-in
    # must still cut instantly.
    playback = {"until": 0.0}
    out_rate = float(getattr(session.audio_format, "output_sample_rate", 0) or 24000)

    def _note_forwarded_audio(nbytes: int) -> None:
        # pcm16 mono: 2 bytes per frame. Anchored to `now` rather than chained
        # blindly, so a gap between turns can't accumulate a phantom queue.
        now = time.monotonic()
        playback["until"] = max(playback["until"], now) + (nbytes / 2.0 / out_rate)
        playback["sent_sec"] = playback.get("sent_sec", 0.0) + nbytes / 2.0 / out_rate

    def _unplayed_sec() -> float:
        return max(0.0, playback["until"] - time.monotonic())

    def _window() -> float:
        try:
            return max(0.0, float(cfg.interrupt_confirm_window_sec or 0))
        except (TypeError, ValueError):
            return 2.0

    # Confirmation needs a user-transcript verdict stream the provider must
    # actually deliver (azure_voice_live declares it; Gemini Live never
    # finalizes its input transcription and keeps the legacy path). Window 0
    # is the operator rollback lever to the same legacy path.
    confirm_mode = (
        bool(getattr(session, "supports_barge_in_confirmation", False))
        and _window() > 0
    )

    async def _send_flush(reason: str) -> None:
        # `reason` narrates WHY the queue is being dropped (barge_in /
        # input_end / speech_detected) — see interrupted_frame. The client
        # action is identical for all of them.
        try:
            await websocket.send_json(interrupted_frame(reason))
        except Exception:
            logger.debug("voice.live: flush frame failed", exc_info=True)
        # The client drops its whole queue on this frame, so whatever we had
        # forwarded is gone: the cursor restarts from now.
        playback["until"] = time.monotonic()
        # Mirror the discard in the recording — the truncated tail was audio
        # the caller never heard, and keeping it would play answers over the
        # interruption that killed them.
        if recorder is not None:
            recorder.mark_playback_flush()

    async def _send_agent_status(text: str) -> None:
        """Forward a delegate progress line ("Using tool: …") to the client.

        Purely informational transcript furniture — the company.example voice demo
        renders these as thinking/tool chips while the caller waits. Additive
        frame type: older portal clients ignore frames they don't
        know. Best-effort by the same contract as every other delegate
        nicety: a send failure must never abort the turn that produced it.
        """
        try:
            await websocket.send_json(agent_status_frame(text))
        except Exception:
            logger.debug("voice.live: agent_status frame failed", exc_info=True)

    def _clear_held_flush() -> None:
        held_flush["pending"] = False
        valve, held_flush["valve"] = held_flush["valve"], None
        if valve is not None and not valve.done():
            valve.cancel()

    def _drop_held_flush_for_playback(why: str) -> None:
        """Abandon a held flush because the caller is still HEARING the answer.

        Not a deferral: re-arming would just fire the same destructive flush a
        beat later. The unconfirmed commit that asked for this flush is, by
        construction, a caller who is listening -- a backchannel, a cough, room
        noise. If she really did take the floor OVER AUDIBLE SPEECH, her
        transcript comes back substantive and `_confirm_barge_in` flushes
        immediately, ungated (`onset["audible"]`).

        Guarded on `pending` exactly like `_release_held_flush`, and for a
        sharper reason: the reply-audio caller runs this per AUDIO CHUNK, so an
        unguarded version logged a line and bumped the counter for every chunk
        of every reply that happened to arrive with a queue -- 76 "protections"
        and 11 log lines in half a second on the first live call, for ONE
        actual protected answer. Harmless to the behaviour (clearing an already
        clear flush is a no-op) but it drowned the log and made the metric
        count chunks instead of answers.
        """
        if not held_flush["pending"]:
            return
        _clear_held_flush()
        metrics.on_event("playback_protected")
        logger.info(
            "voice.live: held flush dropped, caller still hearing the answer "
            "(%s, %.1fs unplayed)", why, _unplayed_sec(),
        )

    async def _release_held_flush(reason: str) -> None:
        if held_flush["pending"]:
            _clear_held_flush()
            await _send_flush(reason)

    async def _flush_valve() -> None:
        try:
            await asyncio.sleep(_window())
        except asyncio.CancelledError:
            return
        if held_flush["pending"]:
            # The valve fails toward flushing, which is right when the only
            # thing queued is a stale holding line -- and wrong when it is the
            # answer she is still hearing.
            if _unplayed_sec() > 0:
                _drop_held_flush_for_playback("valve expiry")
                return
            held_flush["pending"] = False
            held_flush["valve"] = None
            # Expired unconfirmed: the onset was never proven to be speech.
            await _send_flush("speech_detected")

    async def _confirm_barge_in() -> None:
        """The caller really took the floor: supersede in-flight answers,
        cut the talked-over response, clear caller-side playback. Idempotent
        per utterance — the once-per-utterance guard is what keeps a
        delta-confirm followed by a final-with-text, or a tool_call backstop
        followed by the utterance's lagging final (Azure transcribes on a
        slower pipeline than it emits model events), from double-bumping the
        epoch past this utterance's own tool_call."""
        was_pending = pending_interrupt["at"] is not None
        pending_interrupt["at"] = None
        if not cfg.barge_in_enabled:
            # Barge-in disabled for this tenant: she keeps the floor. No
            # cancel, no epoch bump, no client flush — the caller's utterance
            # is answered after the sentence she is already speaking, and any
            # answer already in flight still gets spoken instead of being
            # silently dropped.
            return
        if utt_state["confirmed"]:
            return
        utt_state["confirmed"] = True
        # Every substantive committed utterance reaches here — that is what
        # "confirmation" means, and it is NOT the same as an interruption.
        # Counted separately from the audible cut below because conflating the
        # two read as a catastrophe in the live counters: one 185s call showed
        # confirmed_cuts=8 across 9 user turns ("she is stopped on every
        # turn") when 8 was simply the number of ordinary turns she took.
        metrics.on_event("barge_in_confirmation")
        interrupt_epoch["n"] += 1
        interrupt_epoch["confirmed"] = True
        interrupt_epoch["at"] = time.monotonic()
        _cancel_sustained_cut()
        # Already cut on sustained speech (see _sustained_cut): the caller
        # stopped hearing her then, so this is bookkeeping, not a second cut.
        already_cut = sustain["cut_clock"] == utterance_clock["n"]
        # After a sustained cut she is no longer audible from the caller's
        # side: anything queued now arrived AFTER the cut (the holding line,
        # the reply to this very utterance) — the "silent at onset" case, not
        # a talk-over. Treating the stale onset as audible flushed that reply.
        audible = onset["audible"] and not already_cut
        try:
            await session.cancel_barged_in_response()
        except Exception:
            logger.debug("voice.live: barge-in cancel failed", exc_info=True)
        if held_flush["pending"]:
            if audible or _unplayed_sec() <= 0:
                # She was talking over audible speech (or there is nothing
                # queued and the flush is a formality): cut instantly, ungated
                # by playback — this is the interruption the caller asked for.
                #
                # Only the AUDIBLE case is a cut. The formality flush (empty
                # queue, ordinary turn-taking) used to be counted and labelled
                # `barge_in` too, so a session where nobody talked over her
                # read confirmed_cuts == user turns (live 2026-09-29:
                # confirmed_cuts=6 confirmations=6 over 6 idle onsets).
                if audible:
                    metrics.on_event("confirmed_cut")
                    await _release_held_flush("barge_in")
                else:
                    await _release_held_flush("input_end")
            else:
                # Ordinary turn-taking: the queue was EMPTY when she started
                # speaking, so everything in it now arrived afterwards — her
                # holding line, or the reply to this very utterance. Firing the
                # held flush here throws that away mid-word and the model,
                # believing it spoke, never repeats it. Same protection the two
                # unconfirmed release paths already apply, for the same reason.
                _drop_held_flush_for_playback("confirmed turn, silent at onset")
        elif was_pending:
            # Confirmed from the deltas while she was still mid-response
            # (the commit hasn't landed yet) — flush now, don't wait for it.
            # An ordinary substantive turn (nothing held, no speech-onset
            # over her voice) sends NO flush frame: there is nothing to cut,
            # and inventing one would change frame ordering for every
            # normal exchange.
            if not already_cut:
                metrics.on_event("confirmed_cut")
                await _send_flush("barge_in")

    # ── Sustained-speech cut (`barge_in_sustain_sec`) ──────────────────────
    # The confirmation above needs the caller's WORDS, and on azure-speech
    # those arrive only after the commit — i.e. once the caller has stopped
    # talking. So a real talk-over was cut ~1s after it ENDED. This arms at an
    # AUDIBLE onset and, if the caller is still speaking `barge_in_sustain_sec`
    # later (no speech_stopped/commit for the same utterance), stops her:
    # client flush + provider cancel. No epoch bump — whether an answer in
    # flight is superseded stays the transcript verdict's call.
    sustain: Dict[str, Any] = {"task": None, "cut_clock": -1, "answer": {}, "muting": False}
    answer_track: Dict[str, Any] = {"text": "", "start": None, "end": None}
    played_report: Dict[str, Any] = {"ms": None, "at": 0.0}

    def _sustain_sec() -> float:
        try:
            return max(0.0, float(getattr(cfg, "barge_in_sustain_sec", 0) or 0))
        except (TypeError, ValueError):
            return 0.0

    def _cancel_sustained_cut() -> None:
        task, sustain["task"] = sustain["task"], None
        if task is not None and not task.done():
            task.cancel()

    async def _sustained_cut(clock: int) -> None:
        try:
            await asyncio.sleep(_sustain_sec())
        except asyncio.CancelledError:
            return
        sustain["task"] = None
        if (
            utterance_clock["n"] != clock          # a newer onset owns the floor
            or utt_state["confirmed"]              # the transcript already cut
            or not cfg.barge_in_enabled
            or _unplayed_sec() <= 0                # nothing left to stop
        ):
            return
        sustain["cut_clock"] = clock
        sustain["cut_at"] = time.monotonic()
        # Where the caller's ear was: the stream offset (s) actually played.
        sustain["cut_offset"] = playback.get("sent_sec", 0.0) - _unplayed_sec()
        sustain["answer"] = dict(answer_track)
        # Audio the cancelled response still emits (generated before the
        # cancel landed) must not reach the flushed client: it would play the
        # middle of the answer after the cut, then again in the re-say.
        sustain["muting"] = bool(getattr(session, "response_in_flight", False))
        metrics.on_event("sustained_cut")
        logger.info(
            "voice.live: caller still speaking %.1fs into an audible onset — "
            "cutting her without waiting for the transcript (%.1fs unplayed)",
            _sustain_sec(), _unplayed_sec(),
        )
        try:
            await session.cancel_barged_in_response()
        except Exception:
            logger.debug("voice.live: sustained-cut cancel failed", exc_info=True)
        _clear_held_flush()
        await _send_flush("barge_in")

    # The delegated answer currently being SPOKEN, against the client audio
    # stream it occupies (seconds of forwarded audio): `start` at its first
    # audio frame, `end` when the response speaking it ends. azure_voice_live
    # delivers a whole answer as ONE tool result, so one answer is one
    # contiguous stretch of audio — which is also why the sustained cut is
    # never armed on a provider that streams sentences (see
    # _sustain_armable). Replaced by every new delivery.
    def _answer_remainder() -> str:
        """The part of the CURRENT answer the caller had not heard when the
        sustained cut fired, from the first sentence not fully played through
        the end — or "" when the cut landed on anything else (the model's own
        speech, a later turn, an answer already fully heard)."""
        track = sustain.get("answer") or {}
        text = str(track.get("text") or "")
        start, end = track.get("start"), track.get("end")
        if not text:
            return ""
        offset = float(sustain.get("cut_offset") or 0.0)
        report = played_report.get("ms")
        if report is not None and played_report.get("at", 0.0) >= float(sustain.get("cut_at") or 0.0):
            offset = float(report) / 1000.0        # the client's own account wins
        if start is None:
            heard_chars = 0                          # its audio never started playing
        else:
            if end is not None and offset >= end - 0.05:
                return ""                            # fully heard: the cut was over something else
            if offset < start - 0.05:
                heard_chars = 0
            else:
                heard = offset - start
                span = (end - start) if end is not None else None
                rate = (len(text) / span) if span and span > 0.2 else 13.0   # chars/s
                heard_chars = int(heard * rate)
        starts = [0] + [m.end() for m in re.finditer(r"[.!?؟。\n]\s*", text)]
        first_unheard = max((i for i in starts if i <= heard_chars), default=0)
        return text[first_unheard:].strip()

    async def _recover_sustained_cut() -> None:
        remainder = _answer_remainder()
        sustain["answer"] = {}
        if not remainder:
            return
        if inflight_answers:
            # A delegated answer is on its way (the caller had asked
            # something before): it owns the floor; cancelling now would kill
            # it or its holding line. The old answer's remainder is dropped.
            logger.info("voice.live: sustained-cut recovery skipped — an answer is in flight")
            return
        metrics.on_event("sustained_cut_recovered")
        logger.info(
            "voice.live: sustained cut was noise — re-saying the unheard remainder (%d chars)",
            len(remainder),
        )
        try:
            # Only the service's reply to the noise can be active here (no
            # delegated answer is in flight, checked above).
            self_cut["at"] = time.monotonic()
            await session.cancel_active_response()
            await session.send_text_turn(resolve_say_exactly_instruction(cfg, remainder))
            _note_endpoint_speech(remainder)
        except Exception:
            logger.debug("voice.live: sustained-cut recovery failed", exc_info=True)

    def _sustain_armable() -> bool:
        """The sustained cut AND the audible-idle-onset arming exist only for
        tenants that opt in (`barge_in_sustain_sec` > 0): every other tenant's
        barge-in is byte-identical to before. Never during the greeting guard:
        a caller's speaker echoing her own greeting is continuous "speech"."""
        if not (confirm_mode and cfg.barge_in_enabled) or _sustain_sec() <= 0:
            return False
        if getattr(session, "supports_streamed_tool_results", False):
            # Its recovery maps ONE answer onto one stretch of audio.
            return False
        if greeting_guard["active"] and time.monotonic() < greeting_guard["deadline"]:
            return False
        return True

    def _arm_sustained_cut() -> None:
        if not _sustain_armable():
            return
        _cancel_sustained_cut()
        sustain["task"] = asyncio.create_task(_sustained_cut(utterance_clock["n"]))

    started_at = time.monotonic()
    _tag_live_span(
        op="session", provider=cfg.provider, agent_kind=agentKind, text_only=cfg.text_only
    )
    # Stage-latency collector (voice/live/metrics.py): fed every provider
    # event by the pump loop, per-tool timers from _handle_tool_call, closed
    # in the teardown finally. Carries the same waj.* base attributes every
    # HTTP endpoint's root span gets, so its ClickHouse rows are env/agent/
    # channel-scoped like the rest of the Live Monitor's data.
    # Shared by the metrics collector AND every delegated turn's root span
    # (voice/live/delegate.py) — the same waj.* attributes every HTTP
    # endpoint's root span gets, so both the metric points and the real agent
    # turns are env/agent/channel-tagged in ClickHouse + trace metadata.
    turn_trace_attrs = root_span_attrs(
        websocket, agent_kind=agentKind, session_id=str(identity.session_id),
        agent_slug=agent_slug,
    )
    # Per-connection linkage for the explorer's Audio tab (see `call_id` above).
    turn_trace_attrs["waj.voice.call_id"] = call_id
    if is_sim_conversation:
        # Rides trace metadata into the explorer's list so bot-driven calls
        # (evals, scenarios) render a "sim" chip. Absent — not false — for
        # real sessions, keeping their metadata unchanged.
        turn_trace_attrs["waj.voice.simulated"] = "true"
    metrics = VoiceLiveMetrics(
        provider=cfg.provider,
        agent_kind=agentKind,
        base_attrs=turn_trace_attrs,
    )

    # Endpoint-side barge-in (voice_config.local_barge_in_sec, see
    # voice/live/onset.py): the caller talking over audio still QUEUED at
    # their side flushes it, whether or not the provider is still generating.
    onset_detector: Optional[SpeechOnsetDetector] = None
    if cfg.local_barge_in_sec > 0 and cfg.barge_in_enabled and not cfg.text_only:
        onset_detector = SpeechOnsetDetector(
            sample_rate=int(session.audio_format.input_sample_rate),
            min_speech_sec=cfg.local_barge_in_sec,
            threshold_dbfs=cfg.local_barge_in_dbfs,
        )

    async def _local_barge_in() -> None:
        if _unplayed_sec() < 0.3:
            return  # nothing audible left to cut: ordinary turn-taking
        if greeting_guard["active"] and time.monotonic() < greeting_guard["deadline"]:
            metrics.on_event("greeting_interrupt_suppressed")
            return
        logger.info(
            "voice.live: caller speech over %.1fs of queued audio — flushing playback",
            _unplayed_sec(),
        )
        metrics.on_event("confirmed_cut")
        try:
            await session.cancel_active_response()
        except Exception:
            logger.debug("voice.live: local barge-in cancel failed", exc_info=True)
        await _send_flush("barge_in")

    def _greeting_muted() -> bool:
        if not greeting_mute["active"]:
            return False
        now = time.monotonic()
        if now >= greeting_mute["deadline"]:
            reason = "cap"
        elif greeting_mute["generated"] and _unplayed_sec() < 0.15:
            reason = "played out"
        else:
            return True
        greeting_mute["active"] = False
        logger.info(
            "voice.live: greeting input mute released after %.1fs (%s)",
            now - greeting_mute["since"], reason,
        )
        return False

    async def _pump_client_to_provider() -> None:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                # The CALLER hung up. Logged because the alternative -- the
                # provider ending the stream on its own -- tears the session
                # down through an equally silent path, and with neither logged
                # a "the call keeps dropping" report is unfalsifiable from the
                # logs alone (2026-08-04: cost a full reproduction to rule out).
                logger.info(
                    "voice.live: client disconnected (code=%s)",
                    message.get("code"),
                )
                return
            data = message.get("bytes")
            if data:
                muted = _greeting_muted()
                # Same length, so the provider's audio clock is unchanged.
                await session.send_audio(bytes(len(data)) if muted else data)
                if recorder is not None:
                    recorder.add_inbound(data)
                if not muted and onset_detector is not None and onset_detector.feed(data):
                    await _local_barge_in()
                continue
            text = message.get("text")
            if not text:
                continue
            # The one client->server JSON frame: `{"type": "played", "ms": N}`
            # — total audio ms the browser actually played, sent on every
            # playback flush (see protocol.py). Routed to the provider so it
            # can truncate its conversation to what was HEARD rather than
            # what was sent. Best-effort by construction: a malformed frame
            # or a provider without the mechanism (the base default is a
            # no-op) must never take down the audio pump.
            try:
                frame = json.loads(text)
                if isinstance(frame, dict) and frame.get("type") == "played":
                    played_report.update(ms=float(frame.get("ms") or 0), at=time.monotonic())
                    await session.report_playback(float(frame.get("ms") or 0))
                elif (
                    isinstance(frame, dict)
                    and frame.get("type") == "text"
                    and session.provider == "simulated"
                ):
                    # Eval-only caller path: text-as-if-spoken. Real
                    # provider sessions never see this branch — the frame
                    # is silently dropped like any other unknown JSON.
                    await session.send_text_turn(str(frame.get("text") or ""))
                elif isinstance(frame, dict) and frame.get("type") == "context":
                    # Full replacement, not a merge: the user navigated, and
                    # a stale key from the previous screen would describe the
                    # wrong record. Keys outside the kind's whitelist are
                    # dropped. Any server-pinned keys (agentKind=business
                    # only) survive the replacement — see _apply_client_context.
                    _apply_client_context(
                        delegate_ctx, {k: v for k, v in frame.items() if k != "type"},
                        pinned=pinned_ctx, keys=context_keys,
                    )
                elif isinstance(frame, dict) and frame.get("type") == "auth":
                    # The handshake consumes a first auth frame ONLY on the
                    # frame-auth branch. A caller whose identity arrived
                    # another way (Authorization header, sim gate) still
                    # sends `{"type":"auth", "context":{...}}` as its first
                    # frame — which used to land here and be discarded at
                    # debug level, so `delegate_ctx` stayed empty for the
                    # whole call and the agent re-searched for the encounter
                    # already on screen. Rescue ONLY the context payload:
                    # identity was settled before the pump started and a
                    # mid-session frame must never re-bind it, so the
                    # frame's token and second credential are deliberately
                    # ignored.
                    frame_ctx = _whitelist_context(frame.get("context"), context_keys)
                    if frame_ctx:
                        # Same replacement semantics as a `context` frame,
                        # but gated on a non-empty payload so a stray bare
                        # auth frame cannot wipe context already delivered.
                        _apply_client_context(delegate_ctx, frame.get("context"),
                                              pinned=pinned_ctx, keys=context_keys)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("voice.live: bad client frame ignored", exc_info=True)

    # ── Relay mode: server-side custody of protected multi-beat flows ──────
    # The realtime model reliably delegates the FIRST beat of a protected
    # flow, then increasingly role-plays the later ones — one live day
    # produced a fabricated IBAN written on the update beat, a verification
    # code requested that was never sent, and a dead-air stall holding
    # digits with nowhere to put them. So once a delegated turn's tools show
    # an OTP flow opened (DelegateResult.flow_signals, recorded
    # deterministically by the tool closures), the ENDPOINT takes custody:
    # every committed caller utterance is routed straight into a delegated
    # turn until the flow closes. The model keeps ears, mouth and persona —
    # it just stops deciding what happens next mid-flow.
    # `refused` latches the one-per-session warning below when custody is
    # configured on a provider that cannot dispatch it — see _relay_can_dispatch.
    relay = {"open": False, "grace": 0, "turns": 0, "opened_at": 0.0, "refused": False}
    relay_lock = asyncio.Lock()
    relay_user_text = {"buf": ""}
    # Server-side dictation capture (voice_config.server_side_iban_capture,
    # default OFF) — a SEPARATE custody mechanism from relay mode above, not
    # gated on it: relay is documented off/untested in prod, and this exists
    # precisely to stop replaying its own model-driven flow. When capture
    # claims a commit (tally/confirm/commit) it takes PRECEDENCE over relay
    # for that turn — the two must never both dispatch the same utterance.
    # But relay_mode defaults TRUE and can open on the very same flow
    # signal, so when capture instead CLOSES (a non-digit
    # utterance, an overflow, a declined confirmation) the commit falls
    # through into relay's own intent/dispatch logic below rather than being
    # silently dropped — review found that without the fallthrough, a
    # relay_mode=True deployment (the out-of-the-box default) answered
    # NOTHING for that commit: capture closed without dispatching, the
    # pre-fix code skipped relay's `elif` entirely because it was inside an
    # `else`, and the model's own ask_agent for the same utterance then fell
    # into relay's OWN suppression branch and was closed silently too. See
    # voice/live/dictation.py for the contract and why.
    collector = dictation_capture()
    capture_state = collector.new_state() if collector else ClosedCaptureState()
    capture_lock = asyncio.Lock()
    capture_refused = {"warned": False, "text_only_warned": False}
    # Did capture actually CLAIM the most recent commit (tally/confirm/
    # commit), and at what epoch? The ask_agent-suppression branch in
    # _handle_tool_call must only close the model's own call when capture
    # genuinely answered THIS commit — gating on `capture_state.open`
    # alone (the pre-fix shape) suppressed every ask_agent for up to 180s
    # regardless of whether capture dispatched a replacement, which swallows
    # a call with nothing answering it on an "ignore" decision. Mirrors
    # `last_commit`'s epoch-keyed shape.
    capture_last_claim = {"epoch": -1, "claimed": False}
    # The last COMMITTED substantive utterance, and whether any delegated
    # turn has been dispatched for it (the model's own usable ask_agent, a
    # relayed turn, or the blank-call recovery below). Kept so an ask_agent
    # whose argument stream was CUT mid-way — a barge-in/custody cancel
    # truncating the JSON, surfacing as "malformed tool arguments" and an
    # empty query — can recover the question it was answering instead of
    # silently swallowing it (3 live dropped questions on 2026-08-03).
    # `epoch` records interrupt_epoch at the commit: a blank call whose
    # dispatch_epoch matches an UNCONSUMED commit is that race, not noise.
    # Cards shown this call: the persona pointing at one ("review it on your
    # screen and tap Confirm") is not a claim (persona_claims_a_result).
    cards_shown = {"n": 0}
    last_commit = {
        "text": "", "prev_text": "", "epoch": -1, "consumed": True,
        "stall_armed": False, "recovered": False,
    }
    # The assistant's own reply text for the CURRENT model turn, accumulated
    # from its transcript events and evaluated at turn_end by the
    # announce-then-stall tripwire below.
    assistant_reply = {"buf": ""}
    # --- Server-authored screen text for a STREAMED answer ------------------ #
    #
    # On a provider that declares `transcribes_streamed_tool_results = False`,
    # the words the caller READS for a delegated answer come from here, not
    # from the provider. Splitting one answer into N back-to-back tool results
    # makes Gemini Live speak every word and transcribe only the leading
    # fragment of each response, so the screen showed 'هل معك' where she said
    # 'هل معك رقم الطرفية للمشكلة،'. We know exactly what we asked her to say,
    # so we send that instead of a broken transcription of it.
    #
    # `pending` holds sentences DELIVERED AUDIBLY and not yet put on screen,
    # in delivery order. `current`/`pos` track how far the provider has got
    # through the sentence it is speaking now.
    #
    # WHY IT IS PACED RATHER THAN FLUSHED AT DELIVERY. Every sentence of an
    # answer is delivered within the same millisecond, but she speaks them over
    # the next ~8s. Emitting them all at delivery would put the whole answer on
    # screen before she says it — which is the exact regression #133 removed
    # (`display` arrived when the delegate finished GENERATING) — and would
    # also jump ahead of the announce line still being transcribed. So a
    # sentence goes on screen when the provider's own transcription shows her
    # STARTING it: the truncated fragment is useless as content and perfect as
    # a clock.
    #
    # The matcher only ever suppresses a fragment that literally matches text
    # we authored, at the position we are expecting it. Anything else is
    # forwarded untouched, so the greeting, the announce line, a holding line
    # and any improvisation still reach the screen from the provider. That is
    # deliberate: this must fail toward showing speech, never toward eating it,
    # which is how the old `voiceSuppressSpoken` latch went wrong.
    answer_echo: Dict[str, Any] = {"pending": [], "current": "", "pos": 0}
    # Assistant captions pass through this on their way to the client (see
    # the transcript branch of the event loop).
    speech_scrubber = ToolSyntaxScrubber()

    async def _release_caption_tail() -> None:
        """Forward what the scrubber still holds, as a caption of the response
        it belongs to — before anything that ends that response on the client
        (turn_end, a provider `interrupted`, the provider closing the stream —
        deliberately NOT the caller's final, see the transcript branch). Legitimate words come out; tool syntax,
        including a call cut off mid-arguments, does not (`flush`). Routed
        through the same holding-line / answer-echo labelling as any caption,
        so a held word can never leak into the NEXT response's first caption
        and break its `out_of_band` match.
        """
        tail = speech_scrubber.flush()
        if not tail:
            return
        oob = bool(holding_echo["key"]) and _is_holding_echo(tail)
        screen: Optional[str] = tail
        if not oob:
            if answer_echo["pending"] or answer_echo["current"]:
                screen = _answer_screen_text(tail)
            assistant_reply["buf"] += tail
        if screen:
            await websocket.send_json(
                transcript_frame("assistant", screen, final=False, out_of_band=oob,
                                 greeting=_is_greeting_caption("assistant", oob),
                                 reply=_take_reply_hint("assistant", oob))
            )
    # False on a provider whose streamed-result transcription is fine (or that
    # can't stream at all): then nothing here ever engages and the screen keeps
    # reading the provider, which is the better source when it works.
    server_authors_answer = bool(
        getattr(session, "supports_streamed_tool_results", False)
        and not getattr(session, "transcribes_streamed_tool_results", False)
    )

    def _echo_key(text: str) -> str:
        """Letters and digits only — the comparison alphabet.

        Neither side's punctuation or spacing can be trusted to match: the
        chunker cuts ON punctuation and strips around it, and the model
        re-punctuates what it reads (live: we sent
        `هل معك رقم الطرفية للمشكلة،` and its transcript came back
        `… للمشكلة؟`). Comparing letters is comparing the words.
        """
        return "".join(ch for ch in text if ch.isalnum())

    def _answer_screen_text(fragment: str) -> Optional[str]:
        """What the screen should show for ONE provider assistant fragment.

        Returns the text to forward, or None to forward nothing because the
        fragment is a truncated echo of a sentence already put on screen in
        full. Ordered and self-scoping: it holds no latch that can outlive the
        answer it belongs to — a fragment either matches authored text at the
        cursor or it does not.
        """
        key = _echo_key(fragment)
        if not key:
            # Punctuation/whitespace-only delta. It belongs to whatever is
            # being spoken; drop it while we own the sentence (we already sent
            # its punctuation), forward it otherwise.
            return None if answer_echo["current"] else fragment
        # (1) She is part-way through a sentence we already put on screen.
        current = answer_echo["current"]
        if current:
            whole = _echo_key(current)
            if whole.startswith(key, answer_echo["pos"]):
                answer_echo["pos"] += len(key)
                if answer_echo["pos"] >= len(whole):
                    answer_echo["current"] = ""
                    answer_echo["pos"] = 0
                return None
            # Fell off the sentence — she is improvising past it (a short tool
            # result invites that: we sent `أبشر،` and she said
            # `أبشر، تم فتح تذكرة صيانة.`). Stop owning it and let the rest of
            # this branch decide.
            answer_echo["current"] = ""
            answer_echo["pos"] = 0
        # (2) She is STARTING a sentence we delivered — put the whole thing on
        # screen now, at the moment she begins saying it.
        for index, sentence in enumerate(answer_echo["pending"]):
            whole = _echo_key(sentence)
            if whole and whole.startswith(key):
                # Anything skipped produced no transcription at all, so we
                # never learned she started it. Dropping it is the honest
                # move: this screen claims only what we saw her begin.
                del answer_echo["pending"][:index + 1]
                answer_echo["current"] = sentence
                answer_echo["pos"] = len(key)
                if answer_echo["pos"] >= len(whole):
                    answer_echo["current"] = ""
                    answer_echo["pos"] = 0
                return sentence
        # (3) Not ours: the greeting, the announce line, a holding line, or
        # speech that ran past an authored sentence. The provider transcribes
        # its OWN responses perfectly well — only streamed tool results break.
        return fragment

    # The holding line on a provider WITHOUT out-of-band speech (Gemini Live)
    # rides a WHEN_IDLE tool result and comes back as an ordinary assistant
    # transcript in its own response. Azure marks its holding line
    # `out_of_band` and the app keeps it out of the chat; on Gemini it became a
    # bubble of its own («ثواني بس.», 2026-09-28). The endpoint knows the words
    # it handed over, so the fragments that spell them — in order, from the
    # start of the line — are forwarded with `out_of_band: true` too. Same
    # fail-open rule as the answer echo: a fragment that does not match is
    # forwarded untouched, so this can only ever relabel our own line.
    holding_echo: Dict[str, Any] = {"key": "", "pos": 0}

    def _holding_key(text: str) -> str:
        return "".join(
            ch for ch in unicodedata.normalize("NFKC", text)
            if ch.isalnum() and unicodedata.category(ch) != "Mn"
        )

    def _arm_holding_echo(text: str) -> None:
        holding_echo["key"] = _holding_key(text)
        holding_echo["pos"] = 0

    def _is_holding_echo(fragment: str) -> bool:
        """True when `fragment` is the next piece of the holding line."""
        whole = holding_echo["key"]
        if not whole:
            return False
        pos = holding_echo["pos"]
        key = _holding_key(fragment)
        if not key:
            # Punctuation/whitespace: part of the line only once it started.
            return pos > 0
        if whole.startswith(key, pos):
            holding_echo["pos"] = pos + len(key)
            if holding_echo["pos"] >= len(whole):
                holding_echo["key"] = ""
                holding_echo["pos"] = 0
            return True
        if pos > 0:
            # She fell off the line mid-way: stop owning it.
            holding_echo["key"] = ""
            holding_echo["pos"] = 0
        return False

    def _reset_answer_echo() -> None:
        """A new question is being answered — nothing owed to the old one."""
        answer_echo["pending"] = []
        answer_echo["current"] = ""
        answer_echo["pos"] = 0
        holding_echo["key"] = ""
        holding_echo["pos"] = 0
    # ...and the part of that buffer the ENDPOINT wrote. The model speaks our
    # words as readily as its own — a holding line, an answer delivered as a
    # tool result, a say-exactly relay — and they come back as ordinary
    # assistant transcript, in its voice. The announce-then-stall tripwire's
    # entire premise is "the MODEL declared a lookup and then did not perform
    # one", which is why tier 1 skips BOTH of tier 2's gates (the caller
    # naming a topic term, the reply not being a question back) — the gates
    # that keep a recovery off a farewell and off the persona's own clarifying
    # question. So text we wrote must never be the evidence.
    #
    # 2026-08-19 fixed the ONE shape that identifies itself: a provider
    # surfacing out-of-band speech with `out_of_band=True` (the simulated
    # provider). Every other shape still read as the model announcing, and the
    # a client demo's live Arabic holding line was «لحظة، أتأكد لك الحين.» —
    # «لحظة» is an ANNOUNCE_TOKENS entry. Gemini Live has no out-of-band
    # speech at all, so that line rides a WHEN_IDLE tool result and returns
    # unflagged: our own filler armed our own tripwire at tier-1 confidence,
    # past both gates. The flag was the wrong hook; the endpoint knows what it
    # said, so record it here and subtract it at turn_end instead.
    #
    # An entry is CONSUMED the turn it is found (a line is read back once) and
    # otherwise carried for `_ENDPOINT_SPEECH_TURNS` more turn_ends, because
    # out-of-band speech is QUEUED rather than spoken and may not be read back
    # until the next turn. It must then be dropped, not kept: on a provider
    # that reports out-of-band lines separately (azure_voice_live) our holding
    # line never enters the reply buffer at all, and an entry left in this
    # list forever could subtract a phrase the MODEL later said on its own —
    # which would silently disarm a real stall. Expiring keeps Azure's
    # behaviour where it already was.
    endpoint_speech: List[List[Any]] = []          # [text, turns_left]
    _ENDPOINT_SPEECH_MAX = 8
    _ENDPOINT_SPEECH_TURNS = 2

    def _note_endpoint_speech(text: str) -> None:
        """Record text the endpoint just put in the model's mouth.

        Called wherever the endpoint hands the model something to SAY —
        never for a SILENT delivery, which is context repair the caller
        never hears and which therefore never reaches the reply buffer.
        """
        if text and text.strip():
            endpoint_speech.append([text, _ENDPOINT_SPEECH_TURNS])
            del endpoint_speech[:-_ENDPOINT_SPEECH_MAX]

    def _model_own_words(reply: str) -> str:
        """`reply` with the endpoint's own contributions removed, normalised.

        Works in the same normalised space every other matcher here uses, so
        ASR spacing and Arabic orthography variants cannot smuggle our line
        back in. Subtraction only ever REMOVES text, so the failure mode of a
        model that paraphrases our line is the pre-existing behaviour, never
        a new false negative about the model's own words.
        """
        norm = _normalize_intent_text(reply)
        carried: List[List[Any]] = []
        for entry in endpoint_speech:
            said, turns_left = entry
            n = _normalize_intent_text(said)
            if n and n in norm:
                norm = norm.replace(n, " ", 1)
                continue                      # consumed: read back, once
            if turns_left > 1:
                carried.append([said, turns_left - 1])
        endpoint_speech[:] = carried
        return norm

    # Monotonic stamp of the last assistant AUDIO event — the one signal that
    # proves the caller is actually hearing something. Read by the
    # post-delivery answer watchdog; audio is the ground truth (transcript
    # frames can arrive for a response whose audio was flushed).
    last_reply_audio = {"at": 0.0}
    # Live watchdog tasks, held so the event loop can't GC a sleeping one.
    watchdog_tasks: set = set()
    # The same answer watchdogs, keyed by the call they are guarding, so a
    # `tool_result_dropped` can stand DOWN the one it invalidates without
    # touching the others. Entries are removed as the tasks finish.
    watchdog_by_call: Dict[str, asyncio.Task] = {}

    def _relay_close(reason: str) -> None:
        if relay["open"]:
            logger.info(
                "voice.live: relay mode CLOSED (%s) after %d relayed turn(s)",
                reason, relay["turns"],
            )
        relay.update({"open": False, "grace": 0, "turns": 0, "opened_at": 0.0})

    def _relay_can_dispatch() -> bool:
        """Can this session's custody actually TAKE the turns it suppresses?

        Custody has two halves wired to different signals: it closes the
        model's own ask_agent unspoken (gated on `_relay_active()` alone), and
        it dispatches the replacement from ONE event — the FINAL fragment of
        the caller's user transcript (the `event.final` branch in the pump).
        On a provider that never finalizes its input transcription the first
        half fires and the second never can, so custody stops being custody
        and becomes a black hole: the caller reads their verification code,
        the model relays it, the endpoint swallows it, nobody answers. That is
        the live 2026-08-19 OTP failure on a client demo (Gemini Live:
        `relay mode OPEN (flow signals: ['send_otp'])`, two ask_agent calls
        closed unspoken, `relayed=0`, `tool_calls=4`).

        So custody FAILS SAFE: where it cannot dispatch it never opens, and
        the model keeps its own calls — the pre-relay behaviour, which answers
        the caller. Never suppress what you cannot replace.
        """
        return bool(getattr(session, "finalizes_user_transcripts", False))

    def _relay_refuse_once() -> None:
        # One WARNING per session, not per beat: the operator needs to know
        # that a configured `relay_mode` is inert on this provider (the flow
        # protections in docs/admin-portal/02-configure.md do not apply), and
        # a protected flow would otherwise log this on every turn.
        if relay["refused"]:
            return
        relay["refused"] = True
        logger.warning(
            "voice.live: relay_mode is ON but provider %r does not finalize "
            "user transcripts — custody cannot dispatch a relayed turn, so it "
            "stays CLOSED and the model keeps its own ask_agent calls. The "
            "protected-flow custody (fabricated-IBAN / unsent-code guard) is "
            "NOT active on this session.",
            getattr(session, "provider", "?"),
        )

    def _relay_open(reason: str) -> None:
        """Open custody — the single door, so the capability gate is unmissable."""
        if not _relay_can_dispatch():
            _relay_refuse_once()
            return
        relay.update({
            "open": True, "opened_at": time.monotonic(), "turns": 0, "grace": 0,
        })
        logger.info("voice.live: relay mode OPEN (%s)", reason)

    def _relay_update_from(signals) -> None:
        """Advance flow custody from one delegated turn's flow_signals.

        Any OTP activity (a send, a verify, pass or fail) opens/holds the
        flow. A turn with NO flow activity counts toward the quiet grace
        (`relay_quiet_grace_turns`, default 2) — the read-back beat
        legitimately calls no tool, so one quiet turn is normal; hitting the
        grace means the flow ended (update done, caller moved on, or caller
        abandoned) and the model gets its discretion back.
        """
        if not cfg.relay_mode or signals is None:
            return
        if any(sig.startswith(("send_otp", "verify_otp")) for sig in signals):
            if not relay["open"]:
                _relay_open(f"flow signals: {signals}")
            relay["grace"] = 0
        elif relay["open"]:
            relay["grace"] += 1
            if relay["grace"] >= max(1, int(cfg.relay_quiet_grace_turns)):
                _relay_close(f"{relay['grace']} flow-quiet turn(s)")

    def _relay_active() -> bool:
        # Custody is only ever ACTIVE where it can dispatch. `_relay_open` is
        # the door and already refuses, but the suppression branch in
        # `_handle_tool_call` reads THIS and nothing else, so the invariant it
        # depends on ("a suppressed call is always replaced") is asserted here
        # too rather than inferred from another function.
        if not _relay_can_dispatch():
            return False
        # Hard ceilings so custody can never wedge a call — operator-tunable
        # (voice_config.live.relay_max_turns / relay_max_sec; the defaults are
        # sized so a flow real callers finish in 4-5 beats gets 8, and 180s
        # covers the slowest OTP fumbling observed live with margin).
        if not relay["open"]:
            return False
        if relay["turns"] >= int(cfg.relay_max_turns):
            _relay_close("turn ceiling")
            return False
        if time.monotonic() - relay["opened_at"] > float(cfg.relay_max_sec):
            _relay_close("time ceiling")
            return False
        return True

    async def _handle_relayed_turn(utterance: str, dispatch_epoch: int) -> None:
        # Serialized: two commits in quick succession (the caller reading
        # digits in bursts) must reach the delegate in order, each seeing
        # the session history the previous one wrote.
        async with relay_lock:
            if not _relay_active():
                return
            relay["turns"] += 1
            tool_timer = metrics.tool_started(f"relay-{relay['turns']}")
            # The service auto-creates the model's own reply to the commit;
            # in custody that reply must never play — the relayed answer
            # replaces it. Best-effort (base default no-op).
            try:
                await session.cancel_active_response()
            except Exception:
                logger.debug("voice.live: cancel before relay failed", exc_info=True)
            # The relay path needs its own holding line: the cancel above
            # just silenced the model's reply, and the answer only arrives
            # after a full delegated turn (measured 6-17s live on 2026-08-03)
            # — without this the caller reads 16 digits into pure dead air.
            # Out-of-band only: there is no call id here to ride a WHEN_IDLE
            # tool result on, so a provider without out-of-band speech keeps
            # the (pre-existing) silence rather than a broken fallback. Same
            # interim_response exemption as the ask_agent path — a provider
            # filling the wait itself must not get a second line on top.
            progress_line = resolve_progress_spoken(cfg)

            async def _speak_relay_progress() -> None:
                try:
                    # Fix 2 (progress_on_dispatch): speak right at dispatch
                    # instead of waiting out the timer — see the field's
                    # comment in voice/live/config.py. False keeps this
                    # sleep unconditional, byte-identical to today.
                    if not cfg.progress_on_dispatch:
                        await asyncio.sleep(cfg.progress_after_sec)
                    if interrupt_epoch["n"] != dispatch_epoch:
                        # A newer utterance superseded this beat — "still
                        # working" on it would be a spoken non-sequitur.
                        return
                    await session.speak_out_of_band(progress_line)
                    _note_endpoint_speech(progress_line)
                    # NO transcript frame here. Out-of-band speech is
                    # QUEUED, not spoken: the provider drops the queue
                    # on a barge-in, a committed utterance or a
                    # playback flush, and emitting the frame at enqueue
                    # time told the caller they had heard a line that
                    # was never said. The frame is emitted on the
                    # provider's `out_of_band_started` instead.
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("voice.live: relay holding line failed", exc_info=True)

            progress_task = (
                asyncio.create_task(_speak_relay_progress())
                if (
                    cfg.progress_after_sec
                    and progress_line
                    and not getattr(session, "handles_interim_response", False)
                    and getattr(session, "supports_out_of_band_speech", False)
                )
                else None
            )
            result = None
            try:
                _t0 = time.monotonic()
                result = await run_delegated_turn(
                    agent=agent, profile=profile, message=utterance,
                    identity=identity, timeout_sec=cfg.delegate_timeout_sec,
                    language=cfg.language,
                    failure_spoken=resolve_failure_spoken(cfg),
                    prefetch_enabled=cfg.kb_prefetch_enabled,
                    prefetch_match_count=cfg.kb_prefetch_match_count,
                    deterministic_render=cfg.deterministic_render_enabled,
                    on_screen=resolve_on_screen_spoken(cfg),
                    on_sentence=None,
                    on_progress=_send_agent_status,
                    trace_attributes=turn_trace_attrs,
                    extra_context=(dict(delegate_ctx) if delegate_ctx else None),
                )
                tool_timer.delegate_ms = (time.monotonic() - _t0) * 1000.0
                _log_delegated(result)
                tool_timer.prefetch_hit = result.prefetch_hit
                tool_timer.rendered_tool = result.rendered_tool
                tool_timer.rendered_failure = getattr(result, "rendered_failure", False)
                _relay_update_from(result.flow_signals)
                _capture_update_from(result.flow_signals)
                # Same scribe rule as the ask_agent path: an extraction
                # turn's prose duplicates its proposal cards.
                if not _suppress_extraction_display(cfg, result):
                    await websocket.send_json(display_frame(result.display))
                if cfg.text_only:
                    # No audio will mark first speech; the display frame — or
                    # the proposal cards, when the display is suppressed — IS
                    # the answer's delivery in this mode.
                    tool_timer.mark_first_speech()
                for chart in result.charts:
                    await websocket.send_json(chart_frame(chart))
                for proposal in getattr(result, "proposals", None) or []:
                    cards_shown["n"] += 1
                    await websocket.send_json(proposal_frame(proposal))
                for link in getattr(result, "links", None) or []:
                    await websocket.send_json(links_frame(link))
            except Exception:
                logger.exception("voice.live: relayed turn failed")
                _relay_close("relayed turn error")
            finally:
                # The answer is here (or failed) — a holding line spoken
                # after the real reply would read as her working on nothing.
                if progress_task is not None:
                    progress_task.cancel()
                spoken = (result.spoken if result is not None else "") or failure_spoken_for(
                    cfg.language, override=resolve_failure_spoken(cfg)
                )
                if interrupt_epoch["n"] == dispatch_epoch:
                    # The delivery's response.create has NO collision retry
                    # (deliberate — a bare retry re-reads the last answer), so
                    # anything still active when it lands eats the answer
                    # ("already has an active response", observed live
                    # 2026-08-03 at a relay delivery). In custody the model's
                    # replies must never play anyway — cancel whatever is
                    # active (an in-flight holding line, the model catching
                    # up) right before speaking the answer.
                    try:
                        await session.cancel_active_response()
                    except Exception:
                        logger.debug(
                            "voice.live: cancel before relay delivery failed",
                            exc_info=True,
                        )
                    if cfg.text_only:
                        # Nothing is ever spoken: the display frame above
                        # already delivered the authoritative text; a
                        # say-exactly text turn here would just echo it back
                        # as a redundant second turn.
                        pass
                    else:
                        try:
                            await session.send_text_turn(
                                resolve_say_exactly_instruction(cfg, spoken)
                            )
                            _note_endpoint_speech(spoken)
                        except Exception:
                            logger.exception("voice.live: relayed delivery failed")
                        else:
                            tool_timer.mark_first_speech()
                metrics.relayed_turn()
                tool_timer.finish(
                    mode="relay",
                    error=(result is None or result.error),
                    streamed_sentences=0,
                )

    # ── Server-side dictation capture (voice_config.server_side_iban_capture,
    # default OFF) ───────────────────────────────────────────────────────────
    # A SEPARATE custody mechanism from relay mode above — see the comment on
    # `capture_state`'s declaration for why it is not layered on top of
    # relay_mode instead. The plugin's collector (voice/live/dictation.py)
    # owns the state machine (open/collect/confirm/commit, its closing
    # conditions, the tally/confirmation strings); everything here is delivery
    # glue, reusing exactly the mechanism relay custody already proved out:
    # cancel whatever the model was about to say on its own, then a
    # send_text_turn instructing it to read a server-authored line verbatim
    # (`resolve_say_exactly_instruction`), noted with `_note_endpoint_speech`
    # so it can't arm the endpoint's own stall tripwire (PR #135).
    capture_turn_count = {"n": 0}

    def _capture_can_dispatch() -> bool:
        # The digit buffer advances from the FINAL fragment of the caller's
        # user transcript — the exact same trigger relay custody's dispatch
        # needs, and for the exact same reason (_relay_can_dispatch's
        # docstring): a provider that never finalizes one can suppress the
        # model's own reply but can never hand the buffer anything to
        # collect, which would be a silent black hole for every digit the
        # caller reads out. Fail safe: refuse to open rather than open and
        # swallow.
        return bool(getattr(session, "finalizes_user_transcripts", False))

    def _capture_refuse_once() -> None:
        if capture_refused["warned"]:
            return
        capture_refused["warned"] = True
        logger.warning(
            "voice.live: server_side_iban_capture is ON but provider %r "
            "does not finalize user transcripts — the digit buffer cannot "
            "advance, so it stays CLOSED and the persona's own model-held "
            "tally runs instead, exactly as if this were OFF.",
            getattr(session, "provider", "?"),
        )

    def _capture_refuse_text_only_once() -> None:
        if capture_refused["text_only_warned"]:
            return
        capture_refused["text_only_warned"] = True
        logger.warning(
            "voice.live: server_side_iban_capture is ON but this session is "
            "text_only — nothing here is ever spoken, so the tally/"
            "confirmation would advance the buffer with no way for the "
            "caller to hear or see what they are about to consent to. "
            "Staying CLOSED, exactly as if this were OFF.",
        )

    def _capture_refuse_no_collector_once() -> None:
        if capture_refused.get("no_collector_warned"):
            return
        capture_refused["no_collector_warned"] = True
        logger.warning(
            "voice.live: server_side_iban_capture is ON but this build has no "
            "dictation collector (voice_capture plugin hook) — staying CLOSED, "
            "exactly as if this were OFF.",
        )

    def _capture_update_from(signals) -> None:
        """Open capture on the ENDPOINT's OWN signal.

        `DelegateResult.flow_signals` carries whatever the agent's tools
        reported on its per-session flow-event channel. The collector names
        the one that opens it (`open_signal`): for an account-change flow,
        a successful passcode verify is precisely the moment the next caller
        utterances become the value being dictated. So it opens on that
        signal alone, deliberately NOT on intent-term matching the way relay
        custody's speculative caller-intent trigger does: by the time the
        caller is dictating digits, the flow has already been proven open by
        the tool that gates the change.
        """
        if not cfg.server_side_iban_capture or signals is None:
            return
        if collector is None:
            _capture_refuse_no_collector_once()
            return
        if collector.open_signal not in signals:
            return
        if cfg.text_only:
            # A phone dictation flow has no business on an ambient-scribe
            # session — this combination cannot occur with today's tenants —
            # but refuse structurally rather than silently opening a
            # consent gate nothing can ever deliver: text_only sends no
            # send_text_turn, and tally/confirm decisions carried no
            # display_frame of their own before this fix either.
            _capture_refuse_text_only_once()
            return
        if not _capture_can_dispatch():
            _capture_refuse_once()
            return
        collector.open_capture(capture_state, now=time.monotonic())
        logger.info(
            "voice.live: server-side dictation capture OPEN (%s)", collector.open_signal,
        )

    async def _deliver_capture_decision(
        decision, dispatch_epoch: int, *, confirm_digits: str = "",
    ) -> None:
        """Speak a tally/confirmation, or run+deliver the confirmed commit.

        `decision` was already computed SYNCHRONOUSLY in the pump loop
        (`collector.feed` is a pure, non-blocking function — see
        voice/live/dictation.py) before this task was created, so there
        is no race on `capture_state` itself: the state machine only
        ever advances from the single-threaded pump loop. `capture_lock` here
        serializes DELIVERY the same way `relay_lock` does for relay's own
        dispatch — two commits in quick succession (the caller reading
        digits in bursts) must reach the provider in order.

        `confirm_digits` is the buffer SNAPSHOT taken synchronously at
        dispatch time, for exactly one purpose: if a "confirm" decision's
        delivery never actually reaches the caller (see the fail-safe below
        this function), it is the ONLY way to tell "the collector this
        rollback would touch is still the same one this decision produced"
        from "a newer commit already raced it and moved state on" — reset
        blind and a legitimate later confirm/commit could be destroyed.
        """
        async with capture_lock:
            capture_turn_count["n"] += 1
            tool_timer = metrics.tool_started(
                f"{collector.name}-{capture_turn_count['n']}"
            )
            # The service auto-created the model's own reply to this commit
            # (its own tally, per the persona's still-active instructions) —
            # it must never play, or the caller hears two tallies for one
            # answer. Same cancel-then-deliver discipline as relay custody.
            try:
                await session.cancel_active_response()
            except Exception:
                logger.debug(
                    "voice.live: cancel before dictation capture reply failed",
                    exc_info=True,
                )
            result = None
            if decision.action == "commit":
                try:
                    _t0 = time.monotonic()
                    result = await run_delegated_turn(
                        agent=agent, profile=profile, message=decision.message,
                        identity=identity, timeout_sec=cfg.delegate_timeout_sec,
                        language=cfg.language,
                        failure_spoken=resolve_failure_spoken(cfg),
                        prefetch_enabled=cfg.kb_prefetch_enabled,
                        prefetch_match_count=cfg.kb_prefetch_match_count,
                        deterministic_render=cfg.deterministic_render_enabled,
                        on_screen=resolve_on_screen_spoken(cfg),
                        on_sentence=None,
                        on_progress=_send_agent_status,
                        trace_attributes=turn_trace_attrs,
                        extra_context=(dict(delegate_ctx) if delegate_ctx else None),
                    )
                    tool_timer.delegate_ms = (time.monotonic() - _t0) * 1000.0
                    _log_delegated(result)
                    tool_timer.rendered_tool = getattr(result, "rendered_tool", None)
                    tool_timer.rendered_failure = getattr(result, "rendered_failure", False)
                    # A commit turn is not expected to touch the OTP flow
                    # again, but feeding relay's own bookkeeping here too
                    # costs nothing and keeps the two mechanisms from ever
                    # disagreeing about whether a flow is still open. NOT
                    # `_capture_update_from` here too — this delivery IS the
                    # capture's own commit, already closing on its way in.
                    _relay_update_from(getattr(result, "flow_signals", None))
                    if not _suppress_extraction_display(cfg, result):
                        await websocket.send_json(display_frame(result.display))
                    if cfg.text_only:
                        tool_timer.mark_first_speech()
                    for chart in result.charts:
                        await websocket.send_json(chart_frame(chart))
                    for proposal in getattr(result, "proposals", None) or []:
                        cards_shown["n"] += 1
                        await websocket.send_json(proposal_frame(proposal))
                    for link in getattr(result, "links", None) or []:
                        await websocket.send_json(links_frame(link))
                except Exception:
                    logger.exception("voice.live: dictation capture commit turn failed")
                spoken = (result.spoken if result is not None else "") or failure_spoken_for(
                    cfg.language, override=resolve_failure_spoken(cfg)
                )
            else:
                spoken = decision.spoken

            # True once THIS decision's line actually reached the caller —
            # spoken audio normally, or (text_only) the display frame, the
            # only channel that mode has. Read below by the Critical-3
            # fail-safe: a "confirm" that never got here must never leave
            # the collector awaiting consent to a number nobody heard.
            delivered = False
            if interrupt_epoch["n"] == dispatch_epoch:
                # Same delivery discipline as relay/recovery: cancel right
                # before speaking so nothing still active (a stray reply,
                # the model catching up) eats the response.create.
                try:
                    await session.cancel_active_response()
                except Exception:
                    logger.debug(
                        "voice.live: cancel before dictation capture delivery failed",
                        exc_info=True,
                    )
                display_ok = True
                if decision.action in ("tally", "confirm"):
                    # The screen mirrors what she is about to say — same
                    # convention as every other spoken line in this
                    # endpoint, and specifically load-bearing for "confirm":
                    # the read-back is the one line the caller is being
                    # asked to consent to, so it belongs on screen whether
                    # or not the audio itself lands.
                    try:
                        await websocket.send_json(display_frame(spoken))
                    except Exception:
                        display_ok = False
                        logger.debug(
                            "voice.live: dictation capture display frame failed",
                            exc_info=True,
                        )
                if cfg.text_only:
                    # No audio will ever mark delivery in this mode — the
                    # display frame just sent IS the delivery. (In practice
                    # capture refuses to open on a text_only session at all
                    # — _capture_refuse_text_only_once — so this branch is
                    # defensive, not a live path.)
                    delivered = display_ok
                else:
                    try:
                        await session.send_text_turn(
                            resolve_say_exactly_instruction(cfg, spoken)
                        )
                        _note_endpoint_speech(spoken)
                    except Exception:
                        logger.exception("voice.live: dictation capture delivery failed")
                    else:
                        tool_timer.mark_first_speech()
                        delivered = True

            if decision.action == "confirm" and not delivered:
                # Fail-safe (Critical-3): `state.confirming` was already set
                # True SYNCHRONOUSLY in the pump loop the instant feed()
                # decided "confirm" — well before this delivery task ever
                # ran. On Gemini (supports_barge_in_confirmation = False) an
                # unconfirmed barge-in — noise, or the caller simply
                # starting the next utterance — bumps interrupt_epoch and
                # this delivery is dropped SILENT; a send_text_turn
                # exception does the same. Left alone, the collector then
                # waits for consent to a number the caller never actually
                # heard, and the very next sound they make gets evaluated
                # as a yes/no answer to it. Reset — closing hands the turn
                # back to the model exactly like any other "close" — but
                # ONLY if the collector is still exactly the state this
                # decision produced: a newer commit may have already raced
                # this one and moved state on (a fresh dictation, even a
                # fresh open on a re-verify), and blindly resetting would
                # destroy THAT instead.
                if (
                    capture_state.confirming
                    and capture_state.digits == confirm_digits
                ):
                    capture_state.reset()
                    logger.warning(
                        "voice.live: dictation capture confirmation was not "
                        "delivered (dispatch_epoch=%s current_epoch=%s) — "
                        "closing rather than awaiting consent to an unheard "
                        "read-back",
                        dispatch_epoch, interrupt_epoch["n"],
                    )

            tool_timer.finish(
                mode=f"{collector.name}_{decision.action}",
                error=(
                    (decision.action == "commit" and (result is None or result.error))
                    or (decision.action == "confirm" and not delivered)
                ),
                streamed_sentences=0,
            )

    async def _recovery_superseded(dispatch_epoch: int) -> bool:
        """Did the caller REALLY take the floor from this recovery answer?

        The ask_agent path answers the same question with
        `_resolve_barge_in_mode`, and the recovery path used to answer it with
        a bare `interrupt_epoch["n"] == dispatch_epoch`. That equality is not
        the same question. An epoch bump on its own is not evidence the caller
        moved on: on a provider without barge-in confirmation (Gemini Live)
        EVERY `interrupted` bumps it unconfirmed — room noise, the caller's
        own echo, the provider reporting our own cancel back at us. Only an
        interrupt that a substantive commit confirms inside
        `interrupt_confirm_window_sec` means the floor genuinely changed
        hands.

        So the recovery answer was dropped on any bump at all, and the
        `display` frame had already gone out — the caller read the answer on
        screen and heard nothing, with no log line to say why. Live on company.example
        2026-09-03: two calls, three stall recoveries, `barge_ins=8` and `2`
        with `false_barge_ins_suppressed=0`, on a provider where the tool
        path would have spoken every one of them.
        """
        if interrupt_epoch["n"] == dispatch_epoch:
            return False
        window = cfg.interrupt_confirm_window_sec or 0
        if window <= 0:
            # The operator's rollback lever to the old unconditional silence.
            return True
        while not interrupt_epoch["confirmed"]:
            remaining = interrupt_epoch["at"] + window - time.monotonic()
            if remaining <= 0:
                logger.info(
                    "voice.live: interrupt unconfirmed after %.1fs — false "
                    "barge-in, speaking the recovered answer anyway", window,
                )
                return False
            await asyncio.sleep(min(0.05, remaining))
        return True

    async def _handle_recovered_turn(
        utterance: str, dispatch_epoch: int, call_id: str
    ) -> None:
        """Delegate a committed utterance whose ask_agent arrived unusable.

        The relay-dispatch pattern (say-exactly text turn, epoch-guarded)
        rather than the tool-result path on purpose: the truncated call's id
        was already closed by the caller of this function, and the cancelled
        response that truncated the arguments usually destroyed the
        function_call item with it — a tool result has nothing left to
        attach to (the "Tool call ID … not found" log shape).

        No relay bookkeeping here: custody, if any, opens the normal way —
        through this turn's own flow_signals via _relay_update_from.
        """
        metrics.on_event("recovered_turn")
        tool_timer = metrics.tool_started(f"recovery-{call_id}")
        progress_line = resolve_progress_spoken(cfg)

        async def _speak_recovery_progress() -> None:
            # Same holding-line contract as the relay path: out-of-band only
            # (no call id to ride), skipped when the provider fills the wait
            # itself, logged to the client so the record has no silent gap.
            try:
                # Fix 2 (progress_on_dispatch) — see _speak_relay_progress.
                if not cfg.progress_on_dispatch:
                    await asyncio.sleep(cfg.progress_after_sec)
                if interrupt_epoch["n"] != dispatch_epoch:
                    return
                await session.speak_out_of_band(progress_line)
                _note_endpoint_speech(progress_line)
                # NO transcript frame here. Out-of-band speech is
                # QUEUED, not spoken: the provider drops the queue
                # on a barge-in, a committed utterance or a
                # playback flush, and emitting the frame at enqueue
                # time told the caller they had heard a line that
                # was never said. The frame is emitted on the
                # provider's `out_of_band_started` instead.
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("voice.live: recovery holding line failed", exc_info=True)

        progress_task = (
            asyncio.create_task(_speak_recovery_progress())
            if (
                cfg.progress_after_sec
                and progress_line
                and not getattr(session, "handles_interim_response", False)
                and getattr(session, "supports_out_of_band_speech", False)
            )
            else None
        )
        result = None
        try:
            _t0 = time.monotonic()
            result = await run_delegated_turn(
                agent=agent, profile=profile, message=utterance,
                identity=identity, timeout_sec=cfg.delegate_timeout_sec,
                language=cfg.language,
                failure_spoken=resolve_failure_spoken(cfg),
                prefetch_enabled=cfg.kb_prefetch_enabled,
                prefetch_match_count=cfg.kb_prefetch_match_count,
                deterministic_render=cfg.deterministic_render_enabled,
                on_screen=resolve_on_screen_spoken(cfg),
                on_sentence=None,
                on_progress=_send_agent_status,
                trace_attributes=turn_trace_attrs,
                extra_context=(dict(delegate_ctx) if delegate_ctx else None),
            )
            tool_timer.delegate_ms = (time.monotonic() - _t0) * 1000.0
            _log_delegated(result)
            tool_timer.prefetch_hit = getattr(result, "prefetch_hit", False)
            tool_timer.rendered_tool = getattr(result, "rendered_tool", None)
            tool_timer.rendered_failure = getattr(result, "rendered_failure", False)
            _relay_update_from(getattr(result, "flow_signals", None))
            _capture_update_from(getattr(result, "flow_signals", None))
            # Same scribe rule as the ask_agent path: an extraction turn's
            # prose duplicates its proposal cards.
            if not _suppress_extraction_display(cfg, result):
                await websocket.send_json(display_frame(result.display))
            if cfg.text_only:
                # No audio will mark first speech; the display frame — or the
                # proposal cards, when the display is suppressed — IS the
                # answer's delivery in this mode.
                tool_timer.mark_first_speech()
            for chart in result.charts:
                await websocket.send_json(chart_frame(chart))
            for proposal in getattr(result, "proposals", None) or []:
                cards_shown["n"] += 1
                await websocket.send_json(proposal_frame(proposal))
            for link in getattr(result, "links", None) or []:
                await websocket.send_json(links_frame(link))
        except Exception:
            logger.exception("voice.live: recovered turn failed")
        finally:
            if progress_task is not None:
                progress_task.cancel()
            spoken = (result.spoken if result is not None else "") or failure_spoken_for(
                cfg.language, override=resolve_failure_spoken(cfg)
            )
            if await _recovery_superseded(dispatch_epoch):
                # A CONFIRMED barge-in: the caller moved on, so saying this
                # would talk over their new question. The screen keeps the
                # answer (the ask_agent path makes the same call — "the
                # screen is authoritative … regardless of what ends up being
                # spoken"), but this used to happen with no trace at all,
                # which is why "she showed it and never said it" took a live
                # report to find. Say so.
                logger.info(
                    "voice.live: recovered answer superseded by a confirmed "
                    "barge-in — on screen, not spoken"
                )
            else:
                # Same delivery discipline as the relay path: send_text_turn
                # has no collision retry, so anything still active when it
                # lands eats the answer — cancel right before speaking. The
                # cancel is a deliberate floor cut of our own: open the
                # self-cut absorb window (see _SELF_CUT_ABSORB_SEC) so the
                # provider reporting it back cannot bump the epoch — a bump
                # right here is what used to defeat the late-duplicate
                # suppression's epoch check and double-answer the question
                # this recovery just answered.
                self_cut["at"] = time.monotonic()
                try:
                    await session.cancel_active_response()
                except Exception:
                    logger.debug(
                        "voice.live: cancel before recovery delivery failed",
                        exc_info=True,
                    )
                if cfg.text_only:
                    # Nothing is ever spoken: the display frame above
                    # already delivered the authoritative text; a
                    # say-exactly text turn here would just echo it back
                    # as a redundant second turn.
                    pass
                else:
                    try:
                        await session.send_text_turn(
                            resolve_say_exactly_instruction(cfg, spoken)
                        )
                        _note_endpoint_speech(spoken)
                    except Exception:
                        logger.exception("voice.live: recovered delivery failed")
                    else:
                        tool_timer.mark_first_speech()
            tool_timer.finish(
                mode="recovery",
                error=(result is None or result.error),
                streamed_sentences=0,
            )

    async def _answer_delivery_watchdog(
        spoken: str, dispatch_epoch: int, delivered_at: float, call_id: str
    ) -> None:
        """One bounded re-delivery if a delivered answer never starts speaking.

        The post-delivery mute race (tid_shorthand ~1/3; live 2026-08-03
        15:12 session — delivery ok, then 74s of silence to session end): an
        INTERRUPT tool result's response.create is rejected by a response
        racing it, and the provider's own retry-on-done declines because a
        commit interleaved. That guard is right for a real new question — a
        re-created old answer would be a non-sequitur — but a NOISE commit
        (empty transcript, speech_started_idle) declines it too, and the
        result item then sits in the conversation with nothing speaking it:
        dead air with no error.

        The endpoint can judge what the provider can't: whether the caller
        actually superseded this answer. interrupt_epoch only moves on a
        SUBSTANTIVE commit, so an unchanged epoch + no assistant audio since
        the delivery = the mute race, whatever raced it. Re-deliver once via
        the relay discipline (cancel whatever is active, then a say-exactly
        text turn) — proven on every relayed answer. If the answer was merely
        slow to start, the cancel kills its first syllables and the re-say
        speaks it whole: a stutter, never a double answer.

        THAT LAST SENTENCE WAS WRONG, and 2026-08-05 17:46 is the proof: the
        "re-say" is a second `response.create` for the same text, and when the
        first one is still in flight the caller gets the answer TWICE,
        verbatim, on screen and in their ear. So audio is no longer the only
        ground truth — `session.response_in_flight` is asked too, and a
        response in flight buys another window rather than a re-delivery. The
        rescue itself is unchanged: an answer that truly never speaks (the
        mute race — a rejected answer-create leaving the result item in the
        conversation with nothing to say it) still gets re-delivered, and a
        floor that never goes idle is re-delivered anyway at the ceiling
        below, because a response that has held the line that long without a
        syllable is wedged, not slow.
        """
        window = max(0.1, float(cfg.answer_watchdog_sec))
        # Ceiling on the deferral, not on the first wait: three windows is
        # ~12s at the default, well past any measured delivery->speech (0.21s
        # / 0.33s idle, live-probed 2026-08-05) and past a full holding line
        # in front of it. Beyond that, silence with the floor still busy is
        # the wedged case the rescue exists for.
        deadline = delivered_at + window * 3
        while True:
            try:
                await asyncio.sleep(window)
            except asyncio.CancelledError:
                raise
            if interrupt_epoch["n"] != dispatch_epoch:
                return
            if last_reply_audio["at"] >= delivered_at:
                return
            if not getattr(session, "response_in_flight", False):
                break
            if time.monotonic() >= deadline:
                logger.warning(
                    "voice.live: answer for call_id=%s still silent with a "
                    "response in flight after %.1fs — re-delivering anyway",
                    call_id, window * 3,
                )
                break
            logger.info(
                "voice.live: answer watchdog for call_id=%s stood down — a "
                "response is in flight, waiting another %.1fs",
                call_id, window,
            )
        logger.warning(
            "voice.live: delivered answer for call_id=%s produced no audio "
            "within %.1fs — re-delivering as a say-exactly text turn",
            call_id, cfg.answer_watchdog_sec,
        )
        try:
            # The cancel is a deliberate floor cut of our own — open the
            # self-cut absorb window (see _SELF_CUT_ABSORB_SEC) so the
            # provider reporting it back cannot supersede the very answer
            # being rescued.
            self_cut["at"] = time.monotonic()
            await session.cancel_active_response()
            await session.send_text_turn(
                resolve_say_exactly_instruction(cfg, spoken)
            )
            _note_endpoint_speech(spoken)
        except Exception:
            logger.debug("voice.live: answer re-delivery failed", exc_info=True)

    def _arm_answer_watchdog(spoken: str, dispatch_epoch: int, call_id: str) -> None:
        if not cfg.answer_watchdog_sec:
            return
        if not getattr(session, "reports_response_in_flight", False):
            # Capability gate, not a tuning value. The watchdog's whole safety
            # margin is `session.response_in_flight`: delivery -> first audio
            # on an idle floor is 0.21s / 0.33s measured, so nothing about the
            # THRESHOLD tells a wedged answer from one still on its way — only
            # the floor's state does (see the docstring above, and
            # RealtimeSession.reports_response_in_flight). On a provider that
            # cannot report that state the base property reads False forever,
            # the deferral never engages, and the watchdog degrades to exactly
            # the unbraked shape that spoke one answer twice on 2026-08-05.
            #
            # Nothing is lost where the flag is False: the mute race this
            # rescues is a rejected answer-`response.create` leaving a result
            # item in the conversation with nothing speaking it, which is the
            # Azure Realtime protocol's failure and has no analogue on a
            # provider that hands the result to its service and lets the
            # service schedule the speech. A provider that DOES have such a
            # race declares the flag and gets the full rescue, unchanged.
            logger.debug(
                "voice.live: answer watchdog not armed for call_id=%s — "
                "provider %s does not report response_in_flight",
                call_id, cfg.provider,
            )
            return
        task = asyncio.create_task(_answer_delivery_watchdog(
            spoken, dispatch_epoch, time.monotonic(), call_id
        ))
        watchdog_tasks.add(task)
        task.add_done_callback(watchdog_tasks.discard)
        # Keyed too, so `tool_result_dropped` can cancel exactly this one.
        # Pop on completion rather than leaving stale entries behind: a long
        # call makes many of these and the map would otherwise only grow.
        watchdog_by_call[call_id] = task
        task.add_done_callback(
            lambda _t, _c=call_id: watchdog_by_call.pop(_c, None)
        )

    async def _stall_recovery(
        dispatch_epoch: int, grace_sec: float, announced: bool
    ) -> None:
        """Dispatch a committed question the model dropped without answering.

        Armed by the turn_end tripwire on two tiers, both requiring that the
        last substantive commit is still unconsumed (tool_calls=0, clean
        wire — the model just stops, and server VAD never prompts it again;
        three live client sessions in a row, 2026-08-04, EN and AR, after the
        persona same-breath rule failed to hold):

        1. the reply contains an announce phrase — it declared a lookup and
           then did not perform one, which nothing legitimate does;
        2. the reply does not, but the CALLER's utterance named a topic term
           and the reply is not a question back — so a turn ended without
           answering something only the agent can answer.

        `grace_sec` is the caller's confidence in that diagnosis; tier 2 waits
        longer because it has no declaration of intent to lean on. The sleep
        also lets a tool call that is merely in transit land —
        _handle_tool_call consumes the commit, and the re-check below stands
        down.
        """
        try:
            await asyncio.sleep(max(0.1, float(grace_sec)))
        except asyncio.CancelledError:
            raise
        if last_commit["consumed"] or last_commit["epoch"] != dispatch_epoch:
            return
        if interrupt_epoch["n"] != dispatch_epoch:
            return
        utterance = last_commit["text"]
        if not utterance:
            return
        if cfg.persona_claim_recovery_enabled:
            utterance = _refetch_request(utterance, last_commit.get("prev_text") or "")
        last_commit["consumed"] = True
        # Marked BEFORE the delegate, not after: the model's own late call can
        # arrive while this turn is still running (live 07:09 — its call landed
        # 5s after the recovery answer), and it must be suppressed on arrival
        # rather than after the fact.
        last_commit["recovered"] = True
        logger.warning(
            "voice.live: model ended its turn without calling ask_agent "
            "(announced=%s, grace=%.1fs) — recovering the committed question "
            "via direct delegation",
            announced, grace_sec,
        )
        await _handle_recovered_turn(utterance, dispatch_epoch, "stall")

    async def _silent_turn_recovery(dispatch_epoch: int, committed_at: float) -> None:
        """Recover a question the model answered with NOTHING AT ALL.

        The two turn_end tiers both read the model's reply, so neither can see
        a model that never speaks and never ends a turn. Measured on a cold
        session 2026-08-05: `sec=85 turns=1 user_turns=1 tool_calls=0` — the
        caller asked for last month's sales and got 85 seconds of silence,
        with nothing armed because there was no reply to inspect.

        So this one hangs off the CALLER's commit. That is the only trigger a
        silent model cannot blind. It stands down on every signal that the
        call is actually progressing:

          - the commit got consumed (a tool call landed);
          - the epoch moved (the caller has since said something newer, and
            recovering a superseded utterance is the goodbye bug in miniature);
          - a turn_end tier already armed for this commit (`stall_armed`),
            which judges the reply better than a timer can;
          - the model produced ANY audio after the commit — it replied, so
            this is chitchat, a clarifying question or a closure, and the
            turn_end tiers own those.

        Gated on the caller naming a topic term, same as the unannounced tier
        and for the same reason: a farewell must never reach this path.
        """
        try:
            await asyncio.sleep(max(0.1, float(cfg.stall_recovery_silent_sec)))
        except asyncio.CancelledError:
            raise
        if last_commit["consumed"] or last_commit["epoch"] != dispatch_epoch:
            return
        if last_commit["stall_armed"] or interrupt_epoch["n"] != dispatch_epoch:
            return
        if last_reply_audio["at"] >= committed_at:
            return
        utterance = last_commit["text"]
        if not utterance:
            return
        if not _mentions_a_topic_term(utterance, cfg.stall_recovery_topic_terms):
            return
        last_commit["stall_armed"] = True
        last_commit["consumed"] = True
        # Marked BEFORE the delegate, exactly as the turn_end tiers do. A model
        # that was silent for the whole grace is a model that is LATE, not
        # necessarily dead — its own ask_agent can still land while this
        # recovery runs, and without this flag the #32 suppression never
        # engages and the caller gets two turns for one question. Observed
        # live 2026-08-05 07:04:10 on `data_question`: tier 3 fired at 12.0s,
        # the model's own call followed, and the harness caught "MULTIPLE
        # unprompted responses after the answer".
        last_commit["recovered"] = True
        logger.warning(
            "voice.live: model produced NO reply and no tool call for %.1fs "
            "after the caller's question — recovering it via direct delegation",
            cfg.stall_recovery_silent_sec,
        )
        await _handle_recovered_turn(utterance, dispatch_epoch, "silent")

    def _arm_silent_recovery(dispatch_epoch: int) -> None:
        if not cfg.stall_recovery_silent_sec:
            return
        task = asyncio.create_task(
            _silent_turn_recovery(dispatch_epoch, time.monotonic())
        )
        watchdog_tasks.add(task)
        task.add_done_callback(watchdog_tasks.discard)

    async def _handle_direct_tool(event) -> None:
        """Run one direct action and hand its result back to the model, which
        speaks on it itself. Always answers the call_id — a provider waits
        forever for a function response that never comes."""
        name = event.tool_name or ""
        started = time.monotonic()
        try:
            result = direct_handlers[name](dict(event.arguments or {}))
        except Exception:
            logger.exception("voice.live: direct tool %s failed", name)
            result = {"ok": False, "error": "tool_failed",
                      "detail": "The action did not go through; say so and offer a follow-up."}
        logger.info(
            "voice.live: direct tool %s args=%s -> %s (%.0fms)",
            name, json.dumps(event.arguments or {}, ensure_ascii=False)[:300],
            json.dumps(result, ensure_ascii=False)[:300], (time.monotonic() - started) * 1000,
        )
        try:
            await session.send_tool_result(
                event.call_id or "", result, mode=ToolResultMode.WHEN_IDLE
            )
        except Exception:
            logger.warning("voice.live: direct tool result for %s not delivered", name,
                           exc_info=True)

    async def _handle_tool_call(event, dispatch: Dict[str, int]) -> None:
        # `dispatch` is this call's own record, not a plain int, and it is
        # read LIVE (`dispatch["epoch"]`) at every barge-in check below. The
        # pump loop can rewrite it: when the caller's lagging transcript turns
        # out to be the transcript of the very utterance THIS call answers,
        # the supersede bump carries this dispatch forward with it instead of
        # stranding it one epoch behind (see `utterance_clock`). Reading the
        # value at check time rather than closing over an int is what makes
        # that possible.
        #
        # Fire-and-forget (asyncio.create_task, nobody awaits this): an
        # unguarded exception here would vanish silently AND -- worse --
        # the realtime provider would wait forever for a function response
        # to `event.call_id` that never arrives, since nothing else ever
        # calls send_tool_result for it. The finally below guarantees one
        # is always sent, using FAILURE_SPOKEN on the failure path.
        # Reads the shared contract's argument name (voice/live/base.py's
        # ASK_AGENT_TOOL_SPEC), not a locally hardcoded "query" literal --
        # see ToolSpec's docstring for why this is a real cross-provider
        # contract, not an implementation detail of this one handler.
        query = (event.arguments or {}).get(ASK_AGENT_TOOL_SPEC.query_arg) or ""
        if last_commit["recovered"] and (
            last_commit["epoch"] == dispatch["epoch"]
            # The epoch match is exact but brittle — a noise `interrupted`
            # between the commit and the model's late call moves the epoch
            # and used to let the duplicate through (double read-back +
            # double book_demo_call, live 2026-08-31 13:44). The text match
            # catches the same duplicate across an epoch bump; a genuinely
            # NEW question still gets through on both prongs (different
            # text, and its own commit clears `recovered` anyway).
            or _same_recovered_question(query, last_commit["text"])
        ):
            # The stall recovery already answered THIS commit, so the model's
            # call for it is a late duplicate — it announced, stalled past the
            # grace, and then called anyway (live 07:09 on 2026-08-04: its call
            # landed 12s after the announce and 5s after the recovery had
            # spoken, giving two full delegated turns for one question; only
            # the provider's own "already has an active response" rejection
            # kept the caller from hearing the answer twice). Close it
            # silently, exactly as relay custody does with the calls it
            # supersedes. A NEW caller utterance clears the flag, so a genuine
            # follow-up question is never suppressed.
            logger.info(
                "voice.live: stall recovery already answered this question — "
                "closing the model's late ask_agent call_id=%s unspoken",
                event.call_id,
            )
            try:
                await session.send_tool_result(
                    event.call_id, {"spoken": ""}, mode=ToolResultMode.SILENT
                )
            except Exception:
                logger.debug(
                    "voice.live: could not close the late recovered call id",
                    exc_info=True,
                )
            return
        if (
            cfg.server_side_iban_capture
            and capture_state.open
            and capture_last_claim["claimed"]
            and capture_last_claim["epoch"] == dispatch["epoch"]
        ):
            # Suppress ONLY when capture genuinely CLAIMED this exact commit
            # (a tally/confirm/commit decision was dispatched for it) — not
            # merely because the buffer happens to be open. Gating on
            # `capture_state.open` alone (the pre-fix shape) closed
            # EVERY ask_agent call for up to 180s regardless of what capture
            # actually did with the matching commit: an "ignore" or "close"
            # decision dispatches nothing, so a model call landing for that
            # same epoch would have been swallowed with no replacement
            # answering it. `_relay_active()` asserts the equivalent
            # invariant explicitly for relay custody rather than inferring
            # it — this mirrors that.
            logger.info(
                "voice.live: server-side dictation capture claimed this commit — "
                "closing the model's own ask_agent call_id=%s unspoken",
                event.call_id,
            )
            try:
                await session.send_tool_result(
                    event.call_id, {"spoken": ""}, mode=ToolResultMode.SILENT
                )
            except Exception:
                logger.debug(
                    "voice.live: could not close the capture-duplicate "
                    "call id",
                    exc_info=True,
                )
            return
        if cfg.relay_mode and _relay_active():
            # Custody means the endpoint is already routing every committed
            # utterance straight to the delegate (_handle_relayed_turn) — the
            # model's own ask_agent for it is a concurrent DUPLICATE: two
            # ~10s delegated turns racing two answers at the caller (observed
            # live 2026-08-03: "نعم صحيح" ran relay-4 and the model's call
            # side by side, 12s each). Close the call id silently; the
            # relayed answer is the one that speaks.
            logger.info(
                "voice.live: relay custody open — closing the model's own "
                "ask_agent call_id=%s unspoken",
                event.call_id,
            )
            try:
                await session.send_tool_result(
                    event.call_id, {"spoken": ""}, mode=ToolResultMode.SILENT
                )
            except Exception:
                logger.debug(
                    "voice.live: could not close the custody-duplicate call id",
                    exc_info=True,
                )
            return
        if not query.strip():
            # A blank query is what a noise-triggered turn looks like: the VAD
            # committed an utterance that was not speech, the model called
            # ask_agent with nothing in it, and the persisted transcript shows
            # an EMPTY user message (observed on a client demo). Running it
            # anyway spends a full ~10s agent turn on nothing and then reads
            # FAILURE_SPOKEN ("I couldn't pull that up") to a caller who never
            # asked — an apology for a question that does not exist. Close the
            # call id so the provider is not left waiting, and say nothing.
            #
            # UNLESS the blank call is the OTHER thing that produces one: a
            # barge-in/custody cancel landing while the model was mid-way
            # through streaming these arguments (truncated JSON, the
            # provider's "malformed tool arguments" warning). Noise never
            # leaves a committed substantive utterance behind; that race
            # always does. So a blank call whose dispatch epoch matches an
            # unconsumed commit is a caller's real question about to be
            # dropped (3 live drops on 2026-08-03) — recover it by
            # delegating the committed words directly, relay-style.
            recovered = ""
            if (
                not last_commit["consumed"]
                and last_commit["text"]
                and last_commit["epoch"] == dispatch["epoch"]
            ):
                recovered = last_commit["text"]
                last_commit["consumed"] = True
                logger.warning(
                    "voice.live: blank ask_agent for call_id=%s right after an "
                    "unanswered committed utterance — recovering the question "
                    "via direct delegation",
                    event.call_id,
                )
            else:
                logger.info(
                    "voice.live: ignoring ask_agent with a blank query for call_id=%s",
                    event.call_id,
                )
            try:
                await session.send_tool_result(
                    event.call_id, {"spoken": ""}, mode=ToolResultMode.SILENT
                )
            except Exception:
                logger.debug(
                    "voice.live: could not close the blank-query call id", exc_info=True
                )
            if recovered:
                await _handle_recovered_turn(recovered, dispatch["epoch"], event.call_id)
            return
        # The caller's own committed words, when this call answers them. The
        # persona's `query` is its PARAPHRASE, and it can lose the question:
        # «أعطيني اليوم» ("give me today") reached the brain as «أبغى أحدث
        # الرصيد الحالي» ("the latest current balance") — user voice test
        # 2026-09-30. An agent may read `caller_heard` beside the query.
        caller_heard = ""
        if last_commit["epoch"] == dispatch["epoch"]:
            # The model's own usable ask_agent is answering the last committed
            # utterance — mark it consumed so a later blank call (a genuine
            # noise commit) can never trigger a SECOND answer via recovery.
            last_commit["consumed"] = True
            caller_heard = last_commit["text"] or ""
        turn_ctx = dict(delegate_ctx) if delegate_ctx else {}
        if caller_heard:
            turn_ctx["caller_heard"] = caller_heard
        result = None
        # Timing for this ask_agent call: dispatch is now, the triggering
        # user EOT is read off the collector, first SPOKEN delivery is marked
        # where the sends happen below. The holding line never marks it — it
        # is not the answer.
        tool_timer = metrics.tool_started(event.call_id)
        # Resolved per call: the holding line must be in the language the
        # caller is being spoken to, not whatever language the operator's
        # default string happens to be written in.
        progress_line = resolve_progress_spoken(cfg)
        # Was a holding line actually PUT ON THE FLOOR for this call? Not
        # "was one scheduled" — the streaming delivery below cancels the
        # pending task, and on a fast turn it never fires at all. See
        # `_speak_sentence`: this is what decides whether the answer's first
        # sentence has anything to displace.
        progress_delivered = {"n": False}

        async def _speak_progress() -> None:
            """Hold the caller while the agent works.

            A delegated turn is a full agent turn (LLM -> MCP tools -> LLM);
            measured against a client demo it is ~10s even on flash-lite
            with tools bound. The persona is told to announce before calling,
            but that only covers the first second or two -- past
            `progress_after_sec` the caller is just sitting in silence
            wondering if the line dropped.

            Delivered as an interim tool result at WHEN_IDLE so it is spoken
            only if the model has stopped talking, and never cuts off the
            filler it may still be delivering. `progress_spoken` is operator
            text asserting no facts, so it stays inside the same
            verbatim-safety contract as FAILURE_SPOKEN.

            Fix 2 (`cfg.progress_on_dispatch`): the product ask is to
            acknowledge a slow lookup right after the caller finishes, not
            `progress_after_sec` seconds into silence. This is THE path that
            has to carry it — the WHEN_IDLE send below is deferred by the
            PROVIDER until it goes idle regardless of when we call it, so
            sending it at dispatch instead of after the timer just means the
            line is already queued instead of registered late; it does not
            cut across whatever filler the model is still saying. See the
            field's comment in voice/live/config.py.
            """
            try:
                if not cfg.progress_on_dispatch:
                    await asyncio.sleep(cfg.progress_after_sec)
                    # The window is DEAD AIR, so it starts when the caller
                    # stops hearing her — not at dispatch. The persona
                    # announces in the same breath as the call, and her audio
                    # outruns real time: timed from dispatch, the line landed
                    # 1.4s after «لحظة. خلني أتأكد لك.» finished, two holding
                    # lines back to back (user voice test 2026-09-30,
                    # recording 66.1-68.5s announce, 69.9s holding line).
                    while True:
                        wait = cfg.progress_after_sec - (time.monotonic() - playback["until"])
                        if wait <= 0.02:
                            break
                        await asyncio.sleep(wait)
                if interrupt_epoch["n"] != dispatch["epoch"]:
                    # The caller barged in and moved on since this call was
                    # dispatched. A holding line for a question they abandoned
                    # is a spoken non-sequitur after their NEW question --
                    # skip it entirely (unlike the answer itself, "still
                    # working" has no context value worth a SILENT delivery).
                    return
                if getattr(session, "supports_out_of_band_speech", False):
                    # Said without consuming this call id — the only way a
                    # one-result-per-call-id provider can fill the wait.
                    await session.speak_out_of_band(progress_line)
                    # NO transcript frame here. Out-of-band speech is
                    # QUEUED, not spoken: the provider drops the queue
                    # on a barge-in, a committed utterance or a
                    # playback flush, and emitting the frame at enqueue
                    # time told the caller they had heard a line that
                    # was never said. The frame is emitted on the
                    # provider's `out_of_band_started` instead.
                else:
                    await session.send_tool_result(
                        event.call_id,
                        {"spoken": progress_line},
                        mode=ToolResultMode.WHEN_IDLE,
                    )
                    _arm_holding_echo(progress_line)
                _note_endpoint_speech(progress_line)
                progress_delivered["n"] = True
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("voice.live: progress line failed", exc_info=True)

        # `_speak_progress` (above) and the final answer (the `finally` block
        # below) both call send_tool_result on THIS SAME event.call_id -- two
        # results for one call id. A provider that only accepts ONE result per
        # call id (supports_streamed_tool_results=False, e.g. azure_voice_live)
        # rejects the second: on any delegated turn slower than
        # progress_after_sec (default 8s vs. ~10s measured turns), the holding
        # line goes out fine, then the final send raises RealtimeProviderError
        # -- caught and logged by the `finally` block's own except, so it never
        # crashes anything, but the caller hears "Still working on that" and
        # then silence, forever. So the holding line needs EITHER provider
        # capability: several results per call id, or out-of-band speech that
        # isn't a tool result at all (see _speak_progress, which prefers the
        # latter). With neither, the wait falls to the persona's
        # announce-then-call line -- a real product gap on a ~10s turn, which
        # is why azure_voice_live implements out-of-band speech.
        progress_task = (
            asyncio.create_task(_speak_progress())
            if (
                cfg.progress_after_sec
                and cfg.progress_spoken
                # A session whose upstream fills the wait itself (Azure's
                # server-side interim_response, per model row) must not get
                # a second holding line from here on top of it.
                and not getattr(session, "handles_interim_response", False)
                and (
                    getattr(session, "supports_streamed_tool_results", False)
                    or getattr(session, "supports_out_of_band_speech", False)
                )
            )
            else None
        )
        # Streaming delivery: each completed sentence is spoken as it lands,
        # so the caller hears the answer start at ~3s rather than waiting the
        # whole ~10s turn. The FIRST sentence interrupts (start talking now);
        # the rest queue behind it in order. Cancels the holding line, which
        # exists precisely because nothing was being said yet.
        spoken_count = {"n": 0}

        async def _speak_sentence(sentence: str) -> None:
            if progress_task is not None and not progress_task.done():
                progress_task.cancel()
            # Same barge-in rule the final path applies, checked PER SENTENCE:
            # once the caller interrupts, the rest of the abandoned answer
            # lands in context (SILENT) instead of audibly interrupting or
            # queueing behind their new question.
            #
            # …but ONLY for a CONFIRMED interrupt. An UNCONFIRMED one (noise
            # tripping speech-start, or the provider echoing our own
            # INTERRUPT delivery back at us) used to silence the answer
            # sentence by sentence, and each silenced sentence still counted
            # as "streamed" — which then short-circuited the whole-answer
            # delivery AND its false-barge-in recovery below. The caller
            # asked, waited, and heard nothing (live 2026-08-30 08:11 on the
            # landing sales tenant: first answer of the call, mode=silent
            # streamed=3, recovered_turns=0). Halt streaming instead: a
            # halted sentence is never recorded as streamed (_StreamHalted —
            # the delegate's on_sentence contract), and the final delivery
            # resolves the whole unspoken tail through
            # _resolve_barge_in_mode — spoken if the interrupt stays
            # unconfirmed for the window, silent context repair if a real
            # utterance confirms it. Sticky once tripped so a mid-answer
            # confirmation cannot interleave tail sentences out of order.
            if dispatch.get("stream_halted") or (
                interrupt_epoch["n"] != dispatch["epoch"]
                and not interrupt_epoch["confirmed"]
            ):
                dispatch["stream_halted"] = True
                dispatch.setdefault("halted_sentences", []).append(sentence)
                raise _StreamHalted(sentence)
            if interrupt_epoch["n"] != dispatch["epoch"]:
                # Confirmed supersede: the caller demonstrably moved on.
                mode = ToolResultMode.SILENT
            elif spoken_count["n"] == 0 and progress_delivered["n"]:
                # INTERRUPT displaces a holding line WE put on the floor.
                # That is the only thing it is for.
                mode = ToolResultMode.INTERRUPT
            else:
                # Nothing of ours is being said, so there is nothing to cut —
                # and cutting anyway is audible as the assistant interrupting
                # ITSELF. The only speech in flight on a fast turn is the
                # persona's own announce line ("لحظة، أشوف لك"), which the
                # prompt requires in the same breath as the tool call; an
                # INTERRUPT here cancels it server-side AND flushes the
                # browser's queue mid-word.
                #
                # It costs nothing to wait. Live A/B against
                # `gemini-3.1-flash-live-preview`, answer delivered 1.2s after
                # the tool_call, time from result to the answer's first audio:
                # INTERRUPT 1016/744ms, WHEN_IDLE 640/903ms — no separation.
                # The realtime model queues the result behind the announce
                # line it is already finishing, which is exactly the desired
                # turn-taking.
                #
                # Why this became audible only recently: Gemini generates
                # speech ~3x faster than it plays (measured on one turn,
                # `generation_complete` at +3.9s vs `turn_complete` at
                # +10.9s), so the browser holds SECONDS of announce audio the
                # caller has not heard yet; and the delegate got fast enough
                # (1.0-2.8s) that the holding line at `progress_after_sec`
                # rarely fires, so the INTERRUPT had nothing but the announce
                # line to land on.
                mode = ToolResultMode.WHEN_IDLE
            spoken_count["n"] += 1
            if mode is ToolResultMode.INTERRUPT:
                # Stamped BEFORE the send: the provider's cut-report can only
                # arrive after the service processes it, so the window opens
                # here — see _SELF_CUT_ABSORB_SEC.
                self_cut["at"] = time.monotonic()
            await session.send_tool_result(
                event.call_id, {"spoken": sentence}, mode=mode
            )
            # Only after a successful send, and only when it was audible: a
            # SILENT landing is context repair, not the caller hearing the
            # answer.
            if mode is not ToolResultMode.SILENT:
                tool_timer.mark_first_speech()
                # …and the same test decides what the screen may claim. A
                # SILENT sentence is a sentence the caller INTERRUPTED: it
                # lands in the model's context so the conversation stays
                # coherent, and is never spoken. Showing it would put words in
                # her mouth that the recording does not contain.
                if server_authors_answer:
                    answer_echo["pending"].append(sentence)
                _note_endpoint_speech(sentence)
            if mode is ToolResultMode.INTERRUPT:
                # Flush the browser's buffered audio (Microsoft's tool-result
                # recipe: cancel -> truncate -> flush local playback -> item
                # -> create). The provider cancelled generation server-side,
                # but the browser still holds seconds of filler audio — heard
                # as the holding line DELAYING the answer it was masking.
                # Only the first (interrupting) sentence: flushing WHEN_IDLE
                # sentences would cut each one off with the next. Reached only
                # when a holding line was really spoken (see the mode
                # decision above) — a flush with nothing of ours on the floor
                # discards the persona's own announce line mid-word.
                try:
                    await websocket.send_json(interrupted_frame("answer_ready"))
                    # The client just dropped its queue — mirror the discard
                    # into the recording or it plays the flushed filler tail
                    # the caller never heard (same rule as _send_flush).
                    if recorder is not None:
                        recorder.mark_playback_flush()
                except Exception:
                    logger.debug("voice.live: delivery flush frame failed", exc_info=True)

        async def _resolve_barge_in_mode() -> ToolResultMode:
            """SILENT for a confirmed barge-in; INTERRUPT when the interrupt
            stayed unconfirmed for the whole window (false alarm — recover
            the answer). Polling, not an event object: the pump loop is the
            only writer and a 50ms granularity is far inside the window."""
            window = cfg.interrupt_confirm_window_sec or 0
            if window <= 0:
                return ToolResultMode.SILENT
            while not interrupt_epoch["confirmed"]:
                remaining = interrupt_epoch["at"] + window - time.monotonic()
                if remaining <= 0:
                    logger.info(
                        "voice.live: interrupt unconfirmed after %.1fs -- "
                        "false barge-in, speaking the answer for call_id=%s",
                        window, event.call_id,
                    )
                    return ToolResultMode.INTERRUPT
                await asyncio.sleep(min(0.05, remaining))
            return ToolResultMode.SILENT

        # There is deliberately NO out-of-band flavour of sentence streaming
        # any more. Answers spoken via speak_out_of_band never became
        # conversation items, so the model's history never contained anything
        # it had said — every later response looked like a FIRST reply, and
        # the persona's disclose-you're-an-AI rule re-greeted the caller on
        # every turn (in a different register, heard as the voice changing),
        # while "say exactly this" sentences got embellished or replaced
        # outright. Two live incidents in two days (2026-07-30 repeat-loop,
        # 2026-07-31 greeting-storm) were both faces of the same mechanism.
        # A provider that can't take several tool results per call id
        # (azure_voice_live) now always gets the whole answer as its single
        # closing tool result — an in-conversation response with real
        # history. Slower to first speech than streaming; correct every time.
        try:
            _delegate_t0 = time.monotonic()
            result = await run_delegated_turn(
                agent=agent, profile=profile, message=query, identity=identity,
                timeout_sec=cfg.delegate_timeout_sec,
                # The session's resolved speech language: it decides how a long
                # identifier is laid out for the mouth (and, for Arabic, that
                # its letters are written in Arabic script) — deterministically,
                # in code. See speakable.to_speakable.
                language=cfg.language,
                # Resolved from config here, not inside the delegate: the
                # wording is operator-editable per tenant AND per language,
                # while the delegate only knows the session's language.
                failure_spoken=resolve_failure_spoken(cfg),
                prefetch_enabled=cfg.kb_prefetch_enabled,
                prefetch_match_count=cfg.kb_prefetch_match_count,
                deterministic_render=cfg.deterministic_render_enabled,
                on_screen=resolve_on_screen_spoken(cfg),
                # Operator knob AND provider capability: per-sentence
                # delivery rides streamed tool results where the provider
                # accepts several per call id, and stays off everywhere else
                # (see the no-out-of-band-streaming note above).
                #
                # Deliberately NOT gated on `transcribes_streamed_tool_results`.
                # That flag says the provider's transcription of a streamed
                # answer is unusable, which is a SCREEN problem, and turning
                # streaming off to fix a screen costs up to 3.7s of first
                # speech on a long answer — the caller's other live complaint
                # in the same session. `_speak_sentence` sends the screen its
                # own authoritative copy instead; see `answer_echo`.
                trace_attributes=turn_trace_attrs,
                on_sentence=(
                    _speak_sentence
                    if cfg.stream_sentences and getattr(session, "supports_streamed_tool_results", False)
                    else None
                ),
                on_progress=_send_agent_status,
                extra_context=(turn_ctx or None),
            )
            tool_timer.delegate_ms = (time.monotonic() - _delegate_t0) * 1000.0
            _log_delegated(result)
            tool_timer.prefetch_hit = getattr(result, "prefetch_hit", False)
            tool_timer.rendered_tool = getattr(result, "rendered_tool", None)
            tool_timer.rendered_failure = getattr(result, "rendered_failure", False)
            # Flow custody opens HERE: the first beat of a protected flow
            # arrives via the model's own ask_agent call, and its tool
            # activity is what flips relay mode on for the beats after it.
            _relay_update_from(getattr(result, "flow_signals", None))
            # Server-side dictation capture opens from the SAME signal, on the
            # SAME path — the merchant's OTP verify is a normal ask_agent
            # call the model makes itself right up until this point.
            _capture_update_from(getattr(result, "flow_signals", None))
            # The screen is authoritative: push the verbatim answer, charts
            # and proposals regardless of what ends up being spoken — except
            # a scribe-mode extraction turn, whose prose only narrates the
            # proposal cards sent just below (see _suppress_extraction_display).
            if not _suppress_extraction_display(cfg, result):
                await websocket.send_json(display_frame(result.display))
            if cfg.text_only:
                # No audio will mark first speech; the display frame — or the
                # proposal cards, when the display is suppressed — IS the
                # answer's delivery in this mode.
                tool_timer.mark_first_speech()
            for chart in result.charts:
                await websocket.send_json(chart_frame(chart))
            for proposal in getattr(result, "proposals", None) or []:
                cards_shown["n"] += 1
                await websocket.send_json(proposal_frame(proposal))
            for link in getattr(result, "links", None) or []:
                await websocket.send_json(links_frame(link))
        except Exception:
            logger.exception(
                "voice.live: tool_call handling failed for call_id=%s", event.call_id
            )
        finally:
            # The answer is here (or failed) — stop any pending holding line
            # so it can't be spoken after the real reply.
            if progress_task is not None:
                progress_task.cancel()
            # `streamed` only lists sentences that were actually DELIVERED
            # (computed here, before the mode, so an already-streamed answer
            # never sits in the confirm-window wait below for a delivery
            # that is not going to happen). A halted stream is NOT
            # already-streamed, whatever prefix got out: the halted tail is
            # exactly the part of the answer nobody has heard, and treating
            # the prefix as "the answer was streamed" is what used to skip
            # the recovery below and leave the caller in dead air.
            halted_tail = " ".join(dispatch.get("halted_sentences") or [])
            already_streamed = (
                result is not None and result.streamed and not result.error
                and not halted_tail
            )
            # If the caller barged in after THIS call was dispatched, land
            # the result in context without hijacking their new question —
            # but only for a CONFIRMED barge-in. An `interrupted` that no
            # committed utterance or follow-up question ever confirmed was
            # background noise tripping speech-start detection: silently
            # dropping the answer on that alone is the reported "asked,
            # waited, heard nothing". _resolve_barge_in_mode waits out the
            # confirm window (interrupt_confirm_window_sec; 0 restores the
            # old unconditional silence).
            if cfg.text_only:
                # Nothing is ever spoken: the display frame above already
                # delivered the authoritative text; the tool result lands in
                # the model's context silently so the conversation stays
                # coherent without generating a speech turn.
                mode = ToolResultMode.SILENT
            elif interrupt_epoch["n"] == dispatch["epoch"]:
                mode = ToolResultMode.INTERRUPT
            elif already_streamed:
                # Nothing further will be spoken either way — don't hold the
                # metrics hostage to a wait that decides nothing.
                mode = ToolResultMode.SILENT
            else:
                mode = await _resolve_barge_in_mode()
            # Defence in depth: `or` (not just a None check) so an empty
            # string can never reach the model. A blank `spoken` makes it read
            # nothing at all, which the caller experiences as dead air with no
            # error — worse than an apology. delegate.py already guarantees a
            # non-empty value; this is the second lock on a safety-critical path.
            spoken = (result.spoken if result is not None else "") or failure_spoken_for(
                cfg.language, override=resolve_failure_spoken(cfg)
            )
            if halted_tail and result is not None and not result.error:
                # Deliver exactly the sentences the halted streamer declined
                # — the audible prefix (if any) was already heard, and
                # re-saying it would make the assistant repeat itself.
                spoken = halted_tail
            # `streamed` only lists sentences that were actually DELIVERED
            # (delegate appends after a successful on_sentence), so skipping
            # here can never orphan the call id -- if every send failed,
            # `streamed` is empty and the whole answer goes out below. No
            # `return` inside this `finally`: that would swallow a pending
            # CancelledError (and is a SyntaxWarning from 3.14 / PEP 765).
            if already_streamed:
                # Sending the whole answer again would make the assistant
                # repeat itself verbatim right after finishing it.
                logger.debug(
                    "voice.live: %d sentence(s) already streamed for %s; "
                    "skipping the whole-answer delivery",
                    len(result.streamed), event.call_id,
                )
            else:
                if mode is ToolResultMode.INTERRUPT:
                    # Same rule as the streamed first sentence: the window for
                    # absorbing the provider's report of OUR OWN cut opens at
                    # the send — see _SELF_CUT_ABSORB_SEC.
                    self_cut["at"] = time.monotonic()
                # A response already on the floor (the announce line / holding
                # line) is NOT the answer: the answer's audio starts after that
                # response ends. Read BEFORE the send, which may create the
                # answer's own response.
                floor_busy = bool(getattr(session, "response_in_flight", False))
                try:
                    await session.send_tool_result(event.call_id, {"spoken": spoken}, mode=mode)
                except Exception:
                    logger.exception(
                        "voice.live: failed to deliver tool result for call_id=%s", event.call_id
                    )
                else:
                    if mode is not ToolResultMode.SILENT:
                        tool_timer.mark_first_speech()
                        _note_endpoint_speech(spoken)
                        # The answer now being spoken, for the sustained
                        # cut's recovery; its audio stretch is stamped as it
                        # arrives (see the audio / turn_end handlers).
                        answer_track.update(text=spoken, start=None, end=None,
                                            skip_turns=1 if floor_busy else 0)
                    if mode is ToolResultMode.INTERRUPT:
                        # Flush the browser's buffered audio — same recipe as
                        # the streamed first sentence above: the provider's
                        # cancel stops generation, but the filler audio the
                        # browser already holds would otherwise play out
                        # before the answer.
                        try:
                            await websocket.send_json(interrupted_frame("answer_ready"))
                            # Mirror the queue-drop into the recording — same
                            # rule as _send_flush.
                            if recorder is not None:
                                recorder.mark_playback_flush()
                        except Exception:
                            logger.debug(
                                "voice.live: delivery flush frame failed", exc_info=True
                            )
                        # Delivery is not the same as the caller HEARING it —
                        # see _answer_delivery_watchdog for the mute race a
                        # rejected answer-create leaves behind.
                        _arm_answer_watchdog(spoken, dispatch["epoch"], event.call_id)
            # Emit the voice.live.tool span whatever happened above —
            # `finish` is idempotent and never raises, and a failed delivery
            # is exactly the kind of turn the metrics must not lose.
            tool_timer.finish(
                mode=mode.value,
                error=(result is None or result.error),
                streamed_sentences=len(result.streamed) if result is not None else 0,
                # Spoken despite an interrupt = that interrupt was judged a
                # false alarm — the tunable false-barge-in signal.
                recovered=(
                    mode is ToolResultMode.INTERRUPT
                    and interrupt_epoch["n"] != dispatch["epoch"]
                ),
            )

    async def _pump_provider_to_client() -> None:
        pending: set = set()
        try:
            async for event in session.events():
                # Fed FIRST, before any forwarding await below can raise on a
                # gone socket: the stamp is the dequeue moment. Never raises
                # (the collector guards itself) and ignores types it doesn't
                # model — including `user_turn_end` and `speech_started_idle`,
                # which exist only for it and are deliberately not forwarded
                # to the client. `speech_started_idle` in particular must NOT
                # bump `interrupt_epoch` or flush playback: it is a caller
                # detected speaking while nothing was being said, counted so
                # a `barge_ins=0` session can be told apart from a service
                # that never detects the caller at all.
                metrics.on_event(event.type, role=event.role)
                if direct_turn_sink is not None:
                    # Pre-fetched-context mode: the action tools' yes-gate
                    # reads what she said in COMPLETED turns (see
                    # the agent's `note_assistant_turn`).
                    if event.type == "transcript" and event.role == "assistant" and event.text:
                        direct_turn_text["buf"] += event.text
                    elif event.type in ("turn_end", "interrupted"):
                        text, direct_turn_text["buf"] = direct_turn_text["buf"], ""
                        if text.strip():
                            try:
                                direct_turn_sink(text)
                            except Exception:
                                logger.debug("voice.live: direct turn sink failed", exc_info=True)
                if cfg.text_only and event.type == "transcript" and event.role == "assistant" and event.text:
                    # No audio will ever be produced in this mode, so assistant
                    # TEXT is the only proof the model actually replied. Tier 3
                    # stands down on this ledger; without stamping it here a
                    # text-only session looks permanently mute and would be
                    # "recovered" on every single turn.
                    last_reply_audio["at"] = time.monotonic()
                if event.type == "audio" and event.audio and sustain["muting"] and not event.reply:
                    # Late audio of the response the sustained cut cancelled:
                    # the client flushed it, and forwarding it would play the
                    # rest of the answer over the caller (then again in any
                    # re-say). Ends at that response's turn_end, or at the
                    # first audio of a NEW (reply) response.
                    continue
                if event.type == "audio" and event.audio:
                    sustain["muting"] = False
                    if (answer_track["text"] and answer_track["start"] is None
                            and not answer_track.get("skip_turns")):
                        answer_track["start"] = playback.get("sent_sec", 0.0)
                    # Every audio event is assistant speech — the ground
                    # truth the post-delivery answer watchdog stands down on.
                    last_reply_audio["at"] = time.monotonic()
                    if event.reply:
                        # Reply audio beat the transcript verdict: flush the
                        # old tail NOW so it can't play over (or get flushed
                        # WITH) the answer. No-op when nothing is held.
                        #
                        # UNLESS the "old tail" is an answer she is still
                        # hearing. This is the exact leak that reproduced live
                        # 2026-08-04: a backchannel commits, the flush is
                        # correctly held for the verdict, and then the
                        # provider's own auto-reply TO THAT BACKCHANNEL arrives
                        # and releases it 0.1s before the verdict lands --
                        # throwing away every still-queued second of the real
                        # answer (74% of a 28s answer, measured). The reply
                        # plays after the queue anyway (the client schedules
                        # sequentially), so holding costs nothing here.
                        if _unplayed_sec() > 0:
                            _drop_held_flush_for_playback("reply audio")
                        else:
                            # Nothing queued and the reply's audio is here:
                            # ordinary turn-taking, not an interruption.
                            await _release_held_flush("input_end")
                    await websocket.send_bytes(event.audio)
                    _note_forwarded_audio(len(event.audio))
                    if recorder is not None:
                        recorder.add_outbound(event.audio)
                elif event.type == "transcript":
                    # NOT released on the caller's final: on Azure her
                    # transcript routinely completes AFTER the reply has
                    # started, i.e. mid-announce — flushing a held
                    # " functions" there let ".ask" "_agent" through (the V5
                    # leak, re-review 2026-09-28). turn_end, interrupted and
                    # stream end are the response's real boundaries.
                    scrubbed_away = False
                    if (
                        (event.role or "assistant") == "assistant"
                        and getattr(event, "turn_reply", None) is not None
                        and not getattr(event, "out_of_band", False)
                    ):
                        reply_hint["value"] = bool(event.turn_reply)
                    if (
                        (event.role or "assistant") == "assistant"
                        and not getattr(event, "out_of_band", False)
                    ):
                        # Tool-call syntax the model SPOKE ("Let me check.
                        # functions.ask_agent", live 2026-09-28) never reaches
                        # the screen, the chat bubble built from these
                        # frames, or the announce-tripwire buffer below. The
                        # scrubber is streaming because the leak arrives split
                        # across deltas; see voice/live/speech_hygiene.py.
                        raw_text = event.text or ""
                        event.text = speech_scrubber.feed(raw_text)
                        if event.final:
                            event.text += speech_scrubber.flush()
                        scrubbed_away = bool(raw_text) and not event.text and not event.final
                    screen_text: Optional[str] = event.text or ""
                    if scrubbed_away:
                        # Everything in this delta was held back or removed:
                        # forward nothing rather than an empty caption.
                        screen_text = None
                    if (
                        (event.role or "assistant") == "assistant"
                        and not getattr(event, "out_of_band", False)
                        and holding_echo["key"]
                        and _is_holding_echo(event.text or "")
                    ):
                        # Our own holding line, spoken through a tool result:
                        # label it exactly as an out-of-band line is labelled
                        # (see `holding_echo`). The flag also keeps it out of
                        # the announce-tripwire buffer below.
                        event.out_of_band = True
                    if (
                        screen_text is not None
                        and (event.role or "assistant") == "assistant"
                        and not getattr(event, "out_of_band", False)
                        and (answer_echo["pending"] or answer_echo["current"])
                    ):
                        # A streamed answer is being spoken and this provider
                        # cannot transcribe one. Swap the provider's truncated
                        # fragment for the sentence WE handed it, released at
                        # the moment she starts saying it. Anything the matcher
                        # doesn't recognise comes through untouched — see
                        # `_answer_screen_text`. Out-of-band lines are excluded
                        # because they are the ENDPOINT's own speech, never an
                        # answer sentence.
                        screen_text = _answer_screen_text(event.text or "")
                    if screen_text is not None:
                        await websocket.send_json(
                            transcript_frame(
                                event.role or "assistant", screen_text,
                                final=event.final,
                                # Providers whose out-of-band lines arrive as
                                # ordinary transcript events (simulated) flag them
                                # on the event; the frame carries the flag so a
                                # client (the eval driver) can tell a holding line
                                # from the answer. False for every normal
                                # in-conversation transcript — the key is then
                                # absent from the frame entirely.
                                out_of_band=bool(getattr(event, "out_of_band", False)),
                                greeting=_is_greeting_caption(
                                    event.role, bool(getattr(event, "out_of_band", False))),
                                reply=_take_reply_hint(
                                    event.role, bool(getattr(event, "out_of_band", False))),
                            )
                        )
                    if call_log is not None and not getattr(event, "out_of_band", False):
                        # The same words the screen was sent (a holding line
                        # is the endpoint's filler, not part of the record).
                        if event.role == "user":
                            call_log.caller_text(event.text, final=bool(event.final))
                        elif screen_text:
                            call_log.agent_text(screen_text)
                    # The assistant's own reply text, per model turn — read
                    # by the announce-then-stall tripwire at turn_end.
                    if (
                        (event.role or "assistant") == "assistant"
                        and event.text
                        and not getattr(event, "out_of_band", False)
                    ):
                        # OUT-OF-BAND lines are excluded on purpose. This
                        # buffer feeds the announce-then-stall tripwire, whose
                        # whole premise is "the MODEL declared a lookup and
                        # then did not perform one" — a holding line the
                        # ENDPOINT spoke declares nothing about the model, and
                        # the shipped `progress_spoken` default literally
                        # contains "one moment", an ANNOUNCE_TOKENS entry. On a
                        # provider that surfaces out-of-band speech as an
                        # ordinary transcript (voice/live/simulated.py, which
                        # every eval run drives) that made the endpoint's own
                        # filler arm the recovery against a turn the model had
                        # answered perfectly well.
                        assistant_reply["buf"] += event.text
                    # Relay custody rides the caller's transcript: deltas
                    # accumulate, and the FINAL fragment closes the utterance
                    # (both azure shapes land here — streamed deltas ending
                    # in a final-empty, and a single final-with-text). Only
                    # consulted while a protected flow holds custody.
                    if event.role == "user":
                        if event.text:
                            relay_user_text["buf"] += event.text
                        # Cut latency rides the DELTAS: the moment the
                        # caller's partial transcript carries real words, the
                        # barge-in is confirmed — waiting for the `final`
                        # would leave her talking over them for the rest of
                        # the utterance. Guarded by the per-utterance latch
                        # so only the first substantive delta acts.
                        #
                        # Gated on a PENDING SPEECH-ONSET, the same
                        # barge-in-context test the empty-verdict branch below
                        # already applies before cancelling a reply. A
                        # transcript delta on its own is not evidence the
                        # caller spoke: on a live mic the recognizer invents
                        # words out of room noise and out of her own voice
                        # echoing back ("مبيعانش", "I'm doing a tout blurs" —
                        # both real, from one client call), and each invented
                        # fragment cancelled her ~1.4s into the reply. The
                        # caller's own words for it were "it keeps
                        # interrupting herself".
                        #
                        # Costs no latency on a REAL barge-in: the provider
                        # gates `interrupted` on `_response_active`, so
                        # speaking over her always raises it before her
                        # transcript arrives. The case with no onset — a
                        # commit during the SILENT tool-wait — never needed
                        # this branch anyway: nothing is being spoken to cut,
                        # and the `final` handler below still supersedes the
                        # in-flight answer.
                        if (
                            not event.final
                            and confirm_mode
                            and not utt_state["confirmed"]
                            and pending_interrupt["at"] is not None
                        ):
                            text_so_far = relay_user_text["buf"].strip()
                            if text_so_far and not _is_backchannel(text_so_far, backchannel_extra):
                                await _confirm_barge_in()
                        if event.final:
                            utterance = relay_user_text["buf"].strip()
                            # The caller has the floor, so the previous
                            # answer is over: nothing more of it will be
                            # spoken and nothing more of it may be claimed
                            # on screen. This is the hard release the old
                            # `voiceSuppressSpoken` latch never had.
                            _reset_answer_echo()
                            relay_user_text["buf"] = ""
                            # A committed, substantive utterance SUPERSEDES any
                            # answer dispatched before it.
                            #
                            # `interrupted` is not enough on its own: the
                            # provider only raises it while she is actually
                            # speaking (azure_voice_live gates it on
                            # `_response_active`), and the most common barge-in
                            # lands in the SILENT tool-wait of a delegated turn
                            # — where it is classified `speech_started_idle`
                            # and changes nothing. So the caller says "no, the
                            # best branch instead", the epoch never moves, and
                            # the superseded answer is spoken at them in full
                            # before their real one (reproduced live
                            # 2026-08-01: barge_ins=0, speech_started_idle=2,
                            # both results mode=interrupt).
                            #
                            # Bumping here is safe for the CURRENT question:
                            # its `tool_call` is dispatched after this event,
                            # so it captures the new epoch. Only genuinely
                            # earlier, still-in-flight answers are dropped —
                            # and they still land in context via the SILENT
                            # delivery, so she can refer to them if asked.
                            if not confirm_mode:
                                # Legacy path, byte-identical to pre-2026-08
                                # (window 0, or a provider with no transcript
                                # verdict). A substantive commit IS the
                                # confirmation and that is ALL it is: no
                                # provider cancel — the pre-confirmation code
                                # never made one, and window 0 is the rollback
                                # lever, so it must not introduce a new
                                # upstream call — and no held-flush
                                # bookkeeping, since nothing is ever held here.
                                if utterance and not _is_backchannel(utterance, backchannel_extra):
                                    interrupt_epoch["n"] += 1
                                    # A committed utterance IS the
                                    # confirmation — never make a real
                                    # supersede wait out the false-barge-in
                                    # window.
                                    interrupt_epoch["confirmed"] = True
                                    interrupt_epoch["at"] = time.monotonic()
                            elif utterance and not _is_backchannel(utterance, backchannel_extra):
                                # Substantive turn — confirmation actions run
                                # here iff the deltas didn't already (single
                                # final-with-text transcript shape).
                                #
                                # BELONGS vs. SUPERSEDES. The bump above is
                                # right for every answer dispatched for an
                                # EARLIER utterance, and catastrophic for one
                                # dispatched for THIS one: on Azure the
                                # caller's transcript rides a separate, slower
                                # ASR pipeline than the model's events, so an
                                # utterance's own tool_call routinely lands
                                # BEFORE its `final`. Left behind by the bump,
                                # that answer is delivered SILENT — dead air
                                # for the question the caller just asked (the
                                # into-silence half of the #52 defect class:
                                # with no `interrupted` there is no pending
                                # interrupt, so the tool_call backstop never
                                # latches and this final is the FIRST
                                # confirmation).
                                #
                                # The clock is what tells the two apart. It
                                # ticks only on a speech onset, and nothing
                                # derived from utterance N (its commit, its
                                # transcript, the model's call for it) can
                                # arrive before N's onset — so a dispatch
                                # stamped with the clock's CURRENT reading is
                                # answering the very utterance this final
                                # transcribes, while any older stamp is an
                                # answer the caller has genuinely moved on
                                # from. So: bump exactly as before, then carry
                                # this utterance's own answers across the bump
                                # rather than exempting anything from it.
                                # Answers dispatched for earlier utterances
                                # still go SILENT, which is what
                                # test_the_scope_of_these_invariants pins.
                                first_final_for_this_utterance = (
                                    utterance_clock["finalized"]
                                    != utterance_clock["n"]
                                )
                                mine = [
                                    d for d in inflight_answers.values()
                                    if d["utterance"] == utterance_clock["n"]
                                ] if first_final_for_this_utterance else []
                                await _confirm_barge_in()
                                for d in mine:
                                    if d["epoch"] != interrupt_epoch["n"]:
                                        logger.info(
                                            "voice.live: lagging final belongs to "
                                            "an answer already in flight — "
                                            "carrying it from epoch %d to %d "
                                            "instead of superseding it",
                                            d["epoch"], interrupt_epoch["n"],
                                        )
                                    d["epoch"] = interrupt_epoch["n"]
                            else:
                                # Noise/backchannel verdict: she keeps talking.
                                # DROP the held flush unconditionally — an
                                # empty verdict never releases it either way.
                                was_pending = pending_interrupt["at"] is not None
                                pending_interrupt["at"] = None
                                if held_flush["pending"]:
                                    _clear_held_flush()
                                # Cancelling the service's auto-reply is only
                                # safe in genuine barge-in context. On a NATIVE
                                # row the model hears the audio directly, so an
                                # empty transcript for an idle-line commit can
                                # still be real-but-quiet speech it understood —
                                # suppressing that reply would recreate the
                                # silent-answer bug. Only a commit that BEGAN as
                                # speech-over-her-voice (`interrupted` pending)
                                # is barge-in context, where the auto-reply to a
                                # noise burst is known spurious.
                                if was_pending:
                                    metrics.on_event("false_barge_in_suppressed")
                                    logger.info(
                                        "voice.live: false barge-in suppressed "
                                        "(held flush dropped, spurious reply "
                                        "cancelled)"
                                    )
                                    try:
                                        await session.suppress_spurious_reply()
                                    except Exception:
                                        logger.debug(
                                            "voice.live: spurious-reply suppress failed",
                                            exc_info=True,
                                        )
                                if sustain["cut_clock"] == utterance_clock["n"]:
                                    # The sustained cut already stopped her on
                                    # what now turns out to be noise or a
                                    # backchannel: the caller lost the rest of
                                    # the answer. Say it again from the cut
                                    # sentence — the same say-exactly recipe
                                    # the answer watchdog uses.
                                    t = asyncio.create_task(_recover_sustained_cut())
                                    pending.add(t)
                                    t.add_done_callback(pending.discard)
                            utt_state["confirmed"] = False  # next utterance
                            # This utterance's transcript is closed. Recorded
                            # for the carry-forward above, which must never
                            # hand the SAME clock reading to two finals — see
                            # `utterance_clock`.
                            utterance_clock["finalized"] = utterance_clock["n"]
                            # Onset audibility belongs to the utterance that
                            # just closed; the next one re-measures it at its
                            # own speech-start. Reset alongside the
                            # confirmation latch so the two can never disagree
                            # about which utterance they describe.
                            onset["audible"] = False
                            # A backchannel on a SILENT floor is the caller
                            # asking whether anyone is there — it must be able
                            # to arm recovery, or a stalled call cannot be
                            # broken by the caller at all. See
                            # _utterance_should_arm_recovery.
                            _last_audio_at = last_reply_audio["at"]
                            if utterance and _utterance_should_arm_recovery(
                                is_backchannel=_is_backchannel(
                                    utterance, backchannel_extra),
                                response_in_flight=bool(getattr(
                                    session, "response_in_flight", False)),
                                since_last_audio_sec=(
                                    float("inf") if not _last_audio_at
                                    else time.monotonic() - _last_audio_at),
                            ):
                                # Remember the commit for the blank-ask_agent
                                # recovery: the epoch bump above (confirm or
                                # legacy) already ran, so interrupt_epoch is
                                # THIS utterance's epoch — the same value the
                                # model's answering tool_call will capture.
                                last_commit.update({
                                    # The request a later «مش شايف إشي» means
                                    # (see `_refetch_request`).
                                    "prev_text": (
                                        last_commit.get("prev_text", "")
                                        if caller_sees_nothing(last_commit["text"])
                                        else last_commit["text"]),
                                    "text": utterance,
                                    "epoch": interrupt_epoch["n"],
                                    "consumed": False,
                                    "stall_armed": False,
                                    # A new question is never a duplicate of
                                    # the last recovered one — clear the
                                    # late-call suppression with the commit.
                                    "recovered": False,
                                })
                                # Last-line watchdog for a model that answers
                                # with nothing at all — armed HERE, on the
                                # caller's commit, because a silent model
                                # emits no turn_end for the other tiers to
                                # hang off. See _silent_turn_recovery.
                                _arm_silent_recovery(interrupt_epoch["n"])
                            # Server-side dictation capture takes PRECEDENCE over
                            # relay custody for this commit whenever it
                            # CLAIMS it (a tally/confirm/commit decision) —
                            # see capture_state's own declaration for
                            # why the two are independent mechanisms rather
                            # than one layered on the other. The decision is
                            # computed HERE, synchronously
                            # (collector.feed is a pure, non-blocking
                            # function — voice/live/dictation.py), so
                            # `last_commit`, `capture_last_claim` and the relay
                            # logic below all see a fully-resolved outcome
                            # before this event finishes processing; only
                            # the DELIVERY (cancel, speak, and for a commit,
                            # the delegated turn) runs as a background task.
                            #
                            # `run_relay_logic` governs whether relay's own
                            # intent-trigger/dispatch block below ALSO gets a
                            # look at this same commit. It does — unchanged
                            # — when capture is off/not open (the pre-fix
                            # shape) or on a "close" decision: relay_mode
                            # defaults TRUE and independently opens on the
                            # same flow signal capture does, so a
                            # commit capture explicitly declines (a subject
                            # change, an overflow, a declined confirmation)
                            # must still reach relay's own custody rather
                            # than being silently dropped — review found
                            # that without this fallthrough, the
                            # out-of-the-box relay_mode=True configuration
                            # answered NOTHING for such a commit: capture
                            # closed without dispatching, relay's `elif` was
                            # unreachable inside the old `else`, and the
                            # model's own ask_agent for it then fell into
                            # RELAY's own suppression branch and was closed
                            # silently too. "ignore" and a claimed
                            # tally/confirm/commit do NOT fall through —
                            # nothing meaningful happened (backchannel/bare
                            # prefix) or capture already fully answered it.
                            run_relay_logic = True
                            if cfg.server_side_iban_capture and utterance and capture_state.open:
                                capture_decision = collector.feed(
                                    capture_state, utterance,
                                    language=cfg.language,
                                    is_backchannel=_is_backchannel(
                                        utterance, backchannel_extra
                                    ),
                                    now=time.monotonic(),
                                )
                                capture_last_claim["epoch"] = interrupt_epoch["n"]
                                capture_last_claim["claimed"] = (
                                    capture_decision.action in CLAIMING_ACTIONS
                                )
                                if capture_decision.action in CLAIMING_ACTIONS:
                                    last_commit["consumed"] = True
                                    run_relay_logic = False
                                    t = asyncio.create_task(
                                        _deliver_capture_decision(
                                            capture_decision, interrupt_epoch["n"],
                                            # Snapshot NOW, synchronously —
                                            # see _deliver_capture_decision's
                                            # docstring for why this exact
                                            # value (not a re-read later) is
                                            # what the Critical-3 fail-safe
                                            # needs.
                                            confirm_digits=capture_state.digits,
                                        )
                                    )
                                    pending.add(t)
                                    t.add_done_callback(pending.discard)
                                elif capture_decision.action == "ignore":
                                    # Backchannel, or a bare spelled prefix with
                                    # nothing after it: nothing meaningful
                                    # happened. Stays open, no log spam, and
                                    # must NOT fall through to relay either
                                    # (relaying a nod would be pointless).
                                    run_relay_logic = False
                                else:
                                    # "close" — hand the turn back to the
                                    # model untouched: no cancel, no server
                                    # line, nothing dispatched BY CAPTURE.
                                    # run_relay_logic stays True (set above)
                                    # so relay gets its normal chance below.
                                    logger.info(
                                        "voice.live: server-side dictation "
                                        "capture CLOSED — handing the turn "
                                        "back to the model (and relay, if "
                                        "active) untouched by capture"
                                    )
                            if run_relay_logic:
                                # Intent-triggered custody: the model sometimes
                                # refuses to delegate the CHANGE REQUEST itself
                                # (or delegates it with an invented code bundled
                                # in) — and tool-signal custody can only start
                                # after a delegated turn exists. The caller's own
                                # words are the earlier, model-free trigger.
                                norm_utterance = _normalize_intent_text(utterance)
                                if (
                                    cfg.relay_mode
                                    and utterance
                                    and not relay["open"]
                                    and any(
                                        term and _normalize_intent_text(term) in norm_utterance
                                        for term in (cfg.relay_intent_terms or [])
                                    )
                                ):
                                    _relay_open("caller intent")
                                if cfg.relay_mode and utterance and _relay_active():
                                    # Custody answers this commit — the blank-call
                                    # recovery must never answer it a second time
                                    # (the model's own duplicate ask_agent is
                                    # routinely cancelled mid-arguments here).
                                    last_commit["consumed"] = True
                                    t = asyncio.create_task(
                                        _handle_relayed_turn(utterance, interrupt_epoch["n"])
                                    )
                                    pending.add(t)
                                    t.add_done_callback(pending.discard)
                elif event.type == "interrupted":
                    # The response being cut owns whatever caption is held.
                    await _release_caption_tail()
                    # A new caller utterance starts here, whatever the endpoint
                    # then decides to DO about it (nothing, on the disabled and
                    # unconfirmed paths below). Ticked before those branches so
                    # the clock records the caller's speech, not the endpoint's
                    # policy — see `utterance_clock`.
                    utterance_clock["n"] += 1
                    if not cfg.barge_in_enabled:
                        # See _confirm_barge_in: she keeps the floor.
                        pass
                    elif not confirm_mode:
                        # Legacy path, byte-identical to pre-2026-08: gemini
                        # (no transcript verdict to wait for) and the window-0
                        # rollback lever. An interrupt is a barge-in
                        # immediately, unconfirmed until user_turn_end /
                        # tool_call — _resolve_barge_in_mode's recovery keys
                        # off exactly this unconfirmed state.
                        #
                        # Greeting-echo guard (see LiveVoiceConfig.
                        # greeting_barge_in_guard_sec) is scoped to THIS ARM
                        # ONLY, deliberately: it is the only branch that ever
                        # bumps interrupt_epoch, sends interrupted_frame, or
                        # flushes playback on bare detection, so it is the
                        # only branch a caller's own echo of the greeting can
                        # hurt. The confirm-mode branch below does none of
                        # those three things by itself -- it only arms
                        # detection markers (`pending_interrupt`, `onset`)
                        # that a later, TRANSCRIPT-CONFIRMED barge-in consumes
                        # (_confirm_barge_in) -- so guarding it too would cost
                        # a real capability (Azure honouring a genuine
                        # caller interruption during the greeting) for zero
                        # required-behaviour gain.
                        if (
                            greeting_guard["active"]
                            and time.monotonic() < greeting_guard["deadline"]
                        ):
                            # The greeting is dispatched by US the instant the
                            # call connects, so the caller has said NOTHING
                            # yet — an "interruption" of it is echo or room
                            # noise by construction (a real call recording:
                            # the caller channel picks up the greeting at
                            # roughly equal amplitude ~0.3s after it starts).
                            # Take none of the barge-in actions below. The
                            # clock above still ticked, same as every other
                            # interrupted event.
                            metrics.on_event("greeting_interrupt_suppressed")
                        elif (
                            self_cut["at"]
                            and time.monotonic() - self_cut["at"]
                            < _SELF_CUT_ABSORB_SEC
                        ):
                            # The service reporting OUR OWN cut back to us: an
                            # INTERRUPT-mode delivery displaces the holding
                            # line server-side, and the provider surfaces that
                            # displacement as `interrupted` — indistinguishable
                            # on the wire from the caller speaking. On this
                            # no-confirmation arm there is no later verdict to
                            # clear it, so treating it as a caller utterance
                            # bumped the supersede epoch against the very
                            # answer being delivered (silencing its remaining
                            # sentences) and painted a phantom "you took the
                            # floor" chip on the caller's screen — reproduced
                            # live 2026-08-31 on every streamed answer that
                            # displaced a holding line. Absorb exactly one
                            # event per cut, only inside the window; a REAL
                            # barge-in mistaken for it still supersedes on its
                            # own substantive transcript commit below.
                            self_cut["at"] = 0.0
                            metrics.on_event("self_interrupt_absorbed")
                            logger.info(
                                "voice.live: provider interrupted inside the "
                                "self-cut window — absorbed as our own "
                                "delivery's displacement, no supersede",
                            )
                        else:
                            interrupt_epoch["n"] += 1
                            interrupt_epoch["confirmed"] = False
                            interrupt_epoch["at"] = time.monotonic()
                            await websocket.send_json(interrupted_frame("barge_in"))
                            # Mirror the queue-drop into the recording — same
                            # rule as _send_flush.
                            if recorder is not None:
                                recorder.mark_playback_flush()
                    else:
                        # Detection only — noise trips this too. Confirmation
                        # (transcript/tool_call) decides whether she stops.
                        # NOT gated by the greeting guard (see the comment on
                        # the `not confirm_mode` arm above): this branch never
                        # bumps the epoch, sends a frame, or flushes playback
                        # on its own, so the greeting's own echo landing here
                        # is harmless without suppression.
                        pending_interrupt["at"] = time.monotonic()
                        # She was mid-response by the provider's own gate, so
                        # the caller is hearing her by construction.
                        onset["audible"] = True
                        _arm_sustained_cut()
                elif event.type == "speech_started_idle":
                    # The other half of the utterance clock: an onset while
                    # nothing was being said is still the START of a caller
                    # utterance, and on this edge it is the COMMON one (the
                    # provider gates `interrupted` on its own generation,
                    # which finishes long before the caller has heard it).
                    utterance_clock["n"] += 1
                    # Still nothing forwarded and nothing cut — see the
                    # collector comment at the top of this loop. But it is the
                    # ONSET of the caller's utterance, and whether the browser
                    # had audio queued AT THAT MOMENT is what tells an
                    # interruption apart from ordinary turn-taking. The
                    # provider cannot answer that: it gates `interrupted` on
                    # its own generation, which finishes seconds before the
                    # caller has heard it.
                    if _unplayed_sec() > 0:
                        onset["audible"] = True
                        if _sustain_armable():
                            # A talk-over in every sense but the provider's
                            # label: she is still AUDIBLE. Arm it exactly like
                            # an `interrupted` onset — the first substantive
                            # delta confirms, an empty verdict cancels the
                            # service's reply to the noise — and start the
                            # sustained-speech cut. It used to arm nothing, so
                            # the cut waited for the commit AND the transcript:
                            # live 2026-09-29 `speech_started_idle=6
                            # barge_ins=0`, "interrupting doesn't work at all".
                            pending_interrupt["at"] = time.monotonic()
                            _arm_sustained_cut()
                elif event.type == "speech_stopped":
                    # VAD's end of the caller's speech: an onset that ends
                    # inside the sustain window was a blip, not a talk-over.
                    _cancel_sustained_cut()
                elif event.type == "user_transcript_failed":
                    # Client-only close of the caller's transcript: the
                    # client's late-transcript window for this utterance
                    # (turn_end `reply`) must not stay open for the NEXT
                    # utterance's words. Deliberately bypasses the transcript
                    # branch above — an empty user final there is a NOISE
                    # verdict (held flush dropped, reply possibly cancelled),
                    # and a transcription outage is not noise; the held
                    # flush's safety valve still decides, as before.
                    await websocket.send_json(transcript_frame("user", "", final=True))
                elif event.type == "user_turn_end":
                    if call_log is not None:
                        call_log.caller_committed()
                    # A committed utterance is the confirmation a pending
                    # interrupt was real. Metrics already counted it above;
                    # nothing is forwarded to the client for it (see the
                    # collector comment at the top of this loop).
                    interrupt_epoch["confirmed"] = True
                    # Speech is over: from here the transcript verdict decides.
                    _cancel_sustained_cut()
                elif event.type == "playback_flush":
                    # Flush the browser's playback WITHOUT bumping the
                    # barge-in epoch. Same wire frame as a real barge-in (the
                    # client's job is identical: drop queued audio), but the
                    # epoch is what decides whether an in-flight delegated
                    # answer gets spoken or silently dropped -- and a
                    # committed utterance is not evidence the caller
                    # abandoned their question. See voice/live/base.py's
                    # RealtimeEvent docstring.
                    if utt_state["confirmed"] or not confirm_mode:
                        # Confirmed turn (or legacy mode): flush now, before
                        # the reply's audio starts arriving.
                        await _send_flush("input_end")
                    else:
                        # Hold it for the transcript verdict; the valve fails
                        # toward flushing (today's behavior), never staleness.
                        _clear_held_flush()
                        held_flush["pending"] = True
                        held_flush["valve"] = asyncio.create_task(_flush_valve())
                elif event.type == "turn_end":
                    if call_log is not None:
                        call_log.response_ended()
                    sustain["muting"] = False
                    if answer_track.get("skip_turns") and answer_track["start"] is None:
                        # The announce/holding line ahead of the answer ended;
                        # the answer's own audio comes next.
                        answer_track["skip_turns"] -= 1
                    elif answer_track["start"] is not None and answer_track["end"] is None:
                        answer_track["end"] = playback.get("sent_sec", 0.0)
                    # The greeting is the very first thing dispatched in a
                    # session, so the FIRST turn_end a session ever sees is
                    # always its own — closing the guard here (rather than
                    # only via its time cap) is what keeps it from ever
                    # reaching into a later, genuine barge-in.
                    greeting_guard["active"] = False
                    # Generated is not heard: the mute holds until the
                    # queued greeting has also played out (_greeting_muted).
                    greeting_mute["generated"] = True
                    # A word the scrubber was still holding belongs to the
                    # turn that just ended: release it before the turn closes.
                    await _release_caption_tail()
                    # The client closes its open transcript bubble on this.
                    # It cannot use `transcript.final` for that: the live
                    # Gemini API never sets `Transcription.finished`, so every
                    # fragment is non-final and a `final`-only client would
                    # merge the whole session into one bubble.
                    # The FIRST turn_end after a dispatched greeting closes
                    # the greeting (the guard above relies on the same fact).
                    # It is marked so the app can keep the greeting out of the
                    # chat history — one greeting bubble per call otherwise.
                    is_greeting = greeting_turn["pending"] and event.turn_reply is not True
                    greeting_turn["pending"] = False
                    reply_hint["value"] = None   # never carried into the next response
                    await websocket.send_json(turn_end_frame(
                        reply=False if is_greeting else event.turn_reply,
                        greeting=is_greeting,
                    ))
                    # Announce-then-stall tripwire: this model turn completed
                    # containing a looking-it-up phrase while the caller's
                    # committed question is still unanswered. A tool call
                    # merely in transit is covered by _stall_recovery's grace
                    # re-check (its _handle_tool_call task consumes the
                    # commit); `stall_armed` keeps a second turn_end from
                    # double-arming for the same commit.
                    reply_text = assistant_reply["buf"]
                    assistant_reply["buf"] = ""
                    # The MODEL's own words, with anything the ENDPOINT put in
                    # its mouth this turn subtracted — the holding line, an
                    # answer delivered as a tool result, a say-exactly relay
                    # (see `_note_endpoint_speech`). Read here rather than
                    # inside the guard below because it CONSUMES the record:
                    # a line the model has now read back must not be
                    # subtracted again from a later turn, and one it has not
                    # read back yet must still be carried forward.
                    norm_reply = _model_own_words(reply_text)
                    if (
                        cfg.stall_recovery_sec
                        and not last_commit["consumed"]
                        and not last_commit["stall_armed"]
                        and last_commit["text"]
                        and last_commit["epoch"] == interrupt_epoch["n"]
                        and reply_text
                    ):
                        # Without the subtraction above, our own «لحظة»
                        # holding line reads as the model announcing a lookup
                        # — which arms tier 1 past BOTH of tier 2's gates
                        # (topic term, question-back) and answers a farewell.
                        announced = any(
                            _normalize_intent_text(tok) in norm_reply
                            for tok in _ANNOUNCE_TOKENS
                        )
                        # Tier 1b — the persona CLAIMED a result with no
                        # tool call: data "on the screen", "here they are",
                        # done/updated, or "refresh the page" (user voice test
                        # 2026-10-01: four turns of «الحين تشوف … قدامك» for
                        # invoices nothing fetched). Or the caller says they
                        # see nothing — always a re-fetch, never an assertion.
                        # Same confidence as an announce: the words prove the
                        # turn owed an ask_agent.
                        # Opt-in (voice_config.persona_claim_recovery_enabled,
                        # off unless a deployment turns it on) and never on a farewell —
                        # the same posture as tier 1/2 (re-review R-C1).
                        tier_1b = (
                            cfg.persona_claim_recovery_enabled
                            and not is_farewell(last_commit["text"])
                        )
                        claimed = tier_1b and persona_claims_a_result(
                            reply_text, card_shown=bool(cards_shown["n"]))
                        sees_nothing = tier_1b and caller_sees_nothing(last_commit["text"])
                        # Tier 1 — the model declared a lookup and then
                        # stopped. High confidence, short grace.
                        if announced or claimed or sees_nothing:
                            grace = float(cfg.stall_recovery_sec)
                        # Tier 2 — no announce phrase, so read the CALLER
                        # instead. A turn that ended without a tool call did
                        # not answer a question about the merchant's data,
                        # whatever the model said about it. Gated on the
                        # caller naming a topic term (a goodbye and "how are
                        # you" name none) and on the reply NOT being a
                        # question back (that is the persona's clarifying
                        # question, not a stall). Opt-in: topic terms are
                        # empty by default.
                        elif (
                            not _reply_ends_in_question(reply_text)
                            and _mentions_a_topic_term(
                                last_commit["text"], cfg.stall_recovery_topic_terms
                            )
                        ):
                            grace = float(cfg.stall_recovery_unannounced_sec)
                        else:
                            grace = 0.0
                        if grace:
                            last_commit["stall_armed"] = True
                            # FIRST, ask the model to finish what it started.
                            # A realtime turn is a request-scoped unit of
                            # work: a response that ended after "one moment"
                            # with no function_call in it is not late, it is
                            # DONE — live-probed 2026-08-05, no call ever
                            # arrives on its own (12s, gpt-realtime). What
                            # does arrive, in 0.37s, is the call it owed, the
                            # moment anything creates a new response. Live
                            # 16:39 shows the accident that had been carrying
                            # us: BOTH stalled turns produced the model's own
                            # ask_agent 0.4s/1.4s after the RECOVERY's holding
                            # line — i.e. after we happened to create a
                            # response 2.5s into a delegate we should never
                            # have needed. Asking on purpose, immediately,
                            # turns that accident into the mechanism and takes
                            # the whole grace + duplicate delegate (3-6s and
                            # 6s) off the caller's wait. The recovery below
                            # stays armed behind it: if the re-prompt produces
                            # no call, _stall_recovery answers exactly as it
                            # does today, and if it does, _handle_tool_call
                            # consumes the commit and the recovery stands down
                            # at its own re-check.
                            if claimed or sees_nothing:
                                metrics.on_event("persona_claim_stall")
                                logger.warning(
                                    "voice.live: persona turn %s with no ask_agent — "
                                    "treating it as a stall",
                                    "claimed a result" if claimed else "met 'I see nothing'",
                                )
                            if getattr(session, "supports_reprompt", False):
                                try:
                                    if await session.reprompt():
                                        metrics.on_event("reprompted")
                                        logger.info(
                                            "voice.live: turn ended owing an "
                                            "ask_agent (announced=%s) — handing "
                                            "the model the floor back before "
                                            "the %.1fs recovery grace",
                                            announced, grace,
                                        )
                                except Exception:
                                    logger.debug(
                                        "voice.live: stall re-prompt failed",
                                        exc_info=True,
                                    )
                            t = asyncio.create_task(
                                _stall_recovery(
                                    last_commit["epoch"], grace, announced
                                )
                            )
                            watchdog_tasks.add(t)
                            t.add_done_callback(watchdog_tasks.discard)
                elif event.type == "tool_call" and (event.tool_name or "") in direct_handlers:
                    # A direct action (pre-fetched-context mode): a local,
                    # instant function — no delegation, no epoch bookkeeping.
                    task = asyncio.create_task(_handle_direct_tool(event))
                    pending.add(task)
                    task.add_done_callback(pending.discard)
                elif event.type == "tool_call":
                    # Capture the epoch HERE, synchronously, at dequeue time
                    # -- not inside the spawned task. asyncio.create_task()
                    # only schedules the coroutine; it doesn't run until
                    # this loop actually yields. A later `tool_call`/
                    # `interrupted` pair processed by this same loop before
                    # the task's first line runs must not change what THIS
                    # call captured.
                    if (
                        confirm_mode
                        and pending_interrupt["at"] is not None
                        and time.monotonic() - pending_interrupt["at"] <= _window()
                    ):
                        # The caller's overriding words became a question the
                        # model already understood — confirmation without
                        # waiting for our own transcript check (backstop for
                        # transcription failure). Bumps BEFORE the capture
                        # below, so the barged-in call goes SILENT and this
                        # one speaks.
                        #
                        # The latch STAYS SET. On Azure the user transcript
                        # rides a separate, slower ASR pipeline than model
                        # events, so this utterance's substantive `final`
                        # routinely lands AFTER its tool_call — clearing the
                        # latch here let that final confirm a SECOND time,
                        # bump the epoch past this dispatch, and downgrade
                        # the caller's own answer to SILENT (dead air for a
                        # question they actually asked). The latch covers this
                        # order; the utterance-clock carry-forward in the
                        # substantive-final branch covers the same race when
                        # the caller spoke INTO SILENCE and there was no
                        # pending interrupt for this backstop to fire on at
                        # all. The final's handler
                        # resets the latch for the next utterance, exactly as
                        # in the delta-confirm shape; if transcription truly
                        # failed and no final ever comes, the latch stays set
                        # for one utterance — the accepted worst case
                        # documented at utt_state's declaration.
                        await _confirm_barge_in()
                    # Stamped with the caller utterance it is answering as
                    # well as the epoch: the clock cannot have moved since
                    # that utterance's onset without the provider reporting
                    # another one, so this is "which question this call is
                    # for" — the thing a lagging `final` needs in order to
                    # recognise its OWN answer rather than supersede it.
                    dispatch = {
                        "epoch": interrupt_epoch["n"],
                        "utterance": utterance_clock["n"],
                    }
                    # A new question also confirms any pending interrupt —
                    # the caller demonstrably moved on.
                    interrupt_epoch["confirmed"] = True
                    # Run delegation concurrently so the model keeps talking
                    # (announce-then-call) while the real agent works.
                    # A new question is being answered — the backstop release
                    # for a provider that never finalizes a user transcript
                    # (Gemini Live), where the branch above cannot fire.
                    _reset_answer_echo()
                    task = asyncio.create_task(_handle_tool_call(event, dispatch))
                    pending.add(task)
                    task.add_done_callback(pending.discard)
                    # In flight for exactly as long as the task lives: the
                    # answer is delivered in its `finally`, so an entry that
                    # is still here is an answer that can still be spoken —
                    # and one that is gone can no longer be helped by any
                    # carry-forward.
                    inflight_answers[event.call_id or ""] = dispatch
                    task.add_done_callback(
                        lambda _t, _c=(event.call_id or ""):
                            inflight_answers.pop(_c, None)
                    )
                elif event.type == "out_of_band_started":
                    # The provider's upstream accepted an out-of-band line, so
                    # it WILL be spoken. This is the honest moment to show it:
                    # audio-only responses carry no transcript of their own,
                    # and emitting at enqueue time lied whenever the queue was
                    # cleared first (a live call on 2026-08-05 displayed two
                    # lines the caller never heard).
                    if event.text:
                        # Out-of-band by definition — this branch only fires
                        # for lines spoken via `speak_out_of_band` (holding
                        # lines), never for the answer. The marker lets the
                        # eval driver measure the ANSWER's latency instead of
                        # clocking this line as the reply.
                        await websocket.send_json(
                            transcript_frame(
                                "assistant", event.text, final=True,
                                out_of_band=True,
                            )
                        )
                elif event.type == "tool_result_dropped":
                    # The provider's upstream refused this call's result: the
                    # function_call item is gone, so the answer will never be
                    # spoken from it. Stand the answer watchdog down.
                    #
                    # Re-saying the text is worse than saying nothing here.
                    # The watchdog re-issues it as "say exactly this", but the
                    # conversation no longer holds the call it belongs to, so
                    # the model improvises around the instruction instead of
                    # obeying it — a live call on 2026-08-05 replayed a
                    # mangled splice of the previous answer and the holding
                    # line, twice, and never answered the question the caller
                    # had actually just asked.
                    #
                    # Deliberately NOT a client-visible error frame: nothing
                    # here is the caller's problem, and the stall tiers can
                    # still recover the turn by dispatching it afresh.
                    dropped = watchdog_by_call.pop(event.call_id or "", None)
                    if dropped is not None and not dropped.done():
                        dropped.cancel()
                        logger.warning(
                            "voice.live: tool result for call_id=%s was dropped "
                            "upstream — answer watchdog stood down (re-saying a "
                            "dropped answer makes the model improvise)",
                            event.call_id,
                        )
                        metrics.on_event("answer_dropped_upstream")
                elif event.type == "error":
                    await websocket.send_json(error_frame(event.message or "voice error"))
            # Falling out of the `async for` means the PROVIDER ended the event
            # stream: a clean upstream close (`_recv_json` -> None ->
            # `events()` returns), not an error and not our own cancel -- a
            # cancel raises out of the loop and never reaches here. Silent
            # until now, and indistinguishable in the logs from the caller
            # hanging up, which is exactly the ambiguity that made a
            # "conversation keeps ending" report undiagnosable.
            await _release_caption_tail()
            logger.info(
                "voice.live: provider ended the event stream (upstream closed)"
            )
        finally:
            # Nothing held may outlive the stream (no await here: a torn-down
            # task would raise CancelledError on it).
            speech_scrubber.reset()
            # The flush valve is a bare create_task nobody awaits — leaving it
            # armed past the pump would fire a send_json at a socket that is
            # already being torn down (and log a destroyed-pending-task).
            _clear_held_flush()
            _cancel_sustained_cut()
            for task in list(pending):
                task.cancel()
            # A sleeping answer watchdog past the pump would fire its
            # re-delivery at a session that is already closing.
            for task in list(watchdog_tasks):
                task.cancel()

    # ── Periodic in-session budget charging ────────────────────────────────
    # Cumulative seconds already booked against the daily tally for THIS
    # session. Starts at the handshake advance (reserved right after
    # session.start() above); the periodic task below and the teardown
    # charge in the finally both read and advance it, so the sum of every
    # charge for a session is exactly `elapsed` (± the clamp at the cap) —
    # the same total the old two-charge scheme recorded, never more.
    charged = {"total": handshake_seconds}
    # Floored at the use site because voice_config.live is raw operator
    # JSON; <= 0 disables the periodic charge entirely (the rollback lever
    # back to the legacy handshake+teardown scheme).
    budget_interval = float(cfg.budget_charge_interval_sec)
    if budget_interval > 0:
        budget_interval = max(_BUDGET_CHARGE_INTERVAL_FLOOR_SEC, budget_interval)

    async def _charge_budget_periodically() -> None:
        """Book the elapsed-but-uncharged seconds every `budget_interval`.

        Why: with only the handshake advance and the teardown charge, a
        RUNNING session contributed 30s to the persisted tally no matter how
        long it had actually run. Two consequences, both observed on paper
        from the config defaults alone: (a) with max_session_sec=900 and 8
        concurrent per IP, ~2 hours of live audio could accrue against a
        1-hour daily cap before any of it was recorded — the next
        handshake's check_audio_budget read a tally that was missing all of
        it; (b) a replica killed mid-session (deploy/OOM) never reached the
        teardown charge, so a 15-minute call was recorded as 30 seconds.
        Charging the delta each interval keeps the tally within one
        interval of the truth.

        DELIBERATELY does not terminate the session on BudgetExhausted:
        mid-call cutoff is a UX/product decision, not an accounting fix.
        The goal here is an honest tally so the NEXT handshake's
        check_audio_budget refuses — this call plays out to its own end.

        Never raises out (except cancellation): the budget layer is
        fail-open by design (see voice/live/budget.py) and an accounting
        blip must not take down a live call — the same posture as the
        teardown charge in the finally below. Cancelled in that finally
        BEFORE the final charge, so it can never fire after the remainder
        is booked and double-count the tail.
        """
        while True:
            await asyncio.sleep(budget_interval)
            delta = int(time.monotonic() - started_at) - charged["total"]
            if delta <= 0:
                continue
            # BOOKED BEFORE THE AWAIT, then reversed on the paths that wrote
            # nothing. The charge is a blocking Supabase RPC, so it runs off
            # the loop (`_HANDSHAKE_OFF_LOOP`) — otherwise this task freezes
            # the audio pumps for a whole round trip every
            # `budget_charge_interval_sec` of every live call. But moving it
            # off the loop introduces an await point where there was none,
            # and the teardown below cancels this task and then reads
            # `charged["total"]` (its comment used to say "its charges are
            # sync calls, so it cannot be mid-charge" — that stops being
            # true here). A cancel landing on that await would abandon the
            # bookkeeping while the RPC still completed in its thread, and
            # the teardown would charge the same seconds a second time.
            # Booking first makes the cancel case conservative instead:
            # worst case (cancelled AND the write failed) the local tally is
            # a few seconds high for a session that is already ending, and
            # the DB — the only thing the next handshake reads — stays right.
            charged["total"] += delta
            try:
                recorded = await asyncio.to_thread(
                    _reserve_budget, budget_key, delta
                )
            except BudgetExhausted:
                # Over the cap — but the spend IS recorded, clamped at the
                # limit (see reserve_audio_seconds), so these seconds count
                # as charged. No mid-call termination here, on purpose —
                # see the docstring.
                pass
            except Exception:
                # Same fail-open posture as the teardown charge below —
                # anything else (e.g. ValueError from get_service_client()
                # when AI Supabase env vars are missing) must not kill the
                # pump's sibling tasks or the call. Nothing was written, so
                # give the delta back.
                charged["total"] -= delta
            else:
                if not recorded:
                    # A 0 return is reserve_audio_seconds' own fail-open path
                    # (DB unreachable — nothing was written). Un-book it so
                    # the next tick or the teardown catch-up books it if the
                    # DB comes back.
                    charged["total"] -= delta

    budget_task: Optional[asyncio.Task] = None

    try:
        # Sending `ready` lives INSIDE this try/finally, not before it: this
        # is the first write to a socket the caller may have already walked
        # away from, and every line from the counter increment above through
        # here must be covered by the same finally that decrements `_active`
        # and closes the provider session. A `ready` send that raises before
        # entering this block would otherwise leak both.
        ready = ready_frame(session.audio_format, str(identity.session_id),
                            greeting=bool(cfg.greeting_trigger))
        if sim_override_applied:
            # Finding 4: tell the driver it actually got the simulated
            # provider, so a sim eval run that silently fell through to a
            # real (billable) provider — e.g. VOICE_LIVE_SIM_ENABLED unset,
            # or the connection wasn't recognized as loopback — can detect
            # that and abort instead of burning real turns. Never sent
            # otherwise, so this isn't a probing surface for what a
            # deployment supports.
            ready["provider"] = "simulated"
        await websocket.send_json(ready)

        # Make the assistant speak first. A realtime session stays silent
        # until spoken to, so without this the caller opens the mic to dead
        # air and reasonably concludes the feature is broken. The trigger
        # text is operator config (voice_config.live.greeting_trigger);
        # blank disables it. Best-effort: a provider that refuses the text
        # turn must not take down an otherwise working session.
        if cfg.greeting_trigger:
            try:
                await session.send_text_turn(greeting_for_language(cfg.greeting_trigger, cfg.language))
            except RealtimeProviderError:
                logger.warning("voice.live: greeting turn failed; continuing", exc_info=True)
            else:
                greeting_turn["pending"] = True
                # Arm the greeting-echo guard the instant the greeting is
                # actually on the wire -- before the pump tasks below start
                # consuming session.events(), so no interrupted event can
                # ever race its activation. 0 is the documented rollback
                # lever back to legacy behaviour (see the field's comment in
                # voice/live/config.py); a failed dispatch above has nothing
                # to guard, so this only runs on the success path.
                if cfg.greeting_input_mute_sec > 0:
                    now = time.monotonic()
                    greeting_mute.update(
                        active=True, generated=False, since=now,
                        deadline=now + cfg.greeting_input_mute_sec,
                    )
                if cfg.greeting_barge_in_guard_sec > 0:
                    greeting_guard["active"] = True
                    greeting_guard["deadline"] = (
                        time.monotonic() + cfg.greeting_barge_in_guard_sec
                    )

        task_to_provider = asyncio.create_task(_pump_client_to_provider())
        task_to_client = asyncio.create_task(_pump_provider_to_client())
        pumps = {task_to_provider, task_to_client}
        # Alongside the pumps, but NOT in the `pumps` set: it never finishes
        # on its own, and the session must not end because a budget tick did.
        # It is awaited (post-cancel) in the finally, so a bug in it cannot
        # log as a never-retrieved task exception either.
        if budget_interval > 0:
            budget_task = asyncio.create_task(_charge_budget_periodically())

        # `asyncio.gather` with the default `return_exceptions=False` does
        # NOT cancel the sibling task when one of two gathered coroutines
        # returns or raises -- verified: an ordinary client disconnect ends
        # `_pump_client_to_provider` immediately, but `_pump_provider_to_client`
        # stays blocked in `async for ... session.events()` for the full
        # `max_session_seconds` ceiling, holding the concurrency slot AND
        # leaving the upstream realtime session (and its billing) running
        # the whole time -- the `finally` that calls `session.close()` never
        # runs until the ceiling (or an exception) ends the `try`. Likewise,
        # if one pump raises, the other is never cancelled, so an in-flight
        # `_handle_tool_call` can go on calling `send_json`/
        # `send_tool_result` against an already-closed socket/session.
        # `asyncio.wait(..., return_when=FIRST_COMPLETED)` plus an explicit
        # cancel-and-await of whichever pump is still running fixes both:
        # the first pump to end (disconnect, provider error, or a session
        # that just stops on its own) always tears down its sibling before
        # this block exits.
        done, pending_pumps = await asyncio.wait(
            pumps, timeout=max_session_seconds, return_when=asyncio.FIRST_COMPLETED
        )
        if done:
            # WHICH side ended the call, in one line, before the two pumps'
            # own messages are separated by teardown noise. This is the line to
            # read first on any "the call dropped" report.
            logger.info(
                "voice.live: session ended by %s",
                "the client" if task_to_provider in done else "the provider",
            )
        for task in pending_pumps:
            task.cancel()
        if pending_pumps:
            await asyncio.wait(pending_pumps)

        if not done:
            # Neither pump finished before the ceiling.
            raise asyncio.TimeoutError()

        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                raise exc
    except asyncio.TimeoutError:
        logger.info("voice.live: session hit the %ss ceiling", max_session_seconds)
        try:
            await websocket.close(code=CLOSE_RATE_LIMITED, reason="Voice session time limit reached.")
        except Exception:
            pass
    except WebSocketDisconnect:
        pass
    except RealtimeProviderError:
        logger.error("voice.live: provider failed mid-session", exc_info=True)
        try:
            await websocket.close(code=CLOSE_PROVIDER_ERROR, reason="Voice provider error.")
        except Exception:
            pass
    except Exception:
        logger.exception("voice.live: session failed")
        try:
            await websocket.close(code=CLOSE_PROVIDER_ERROR, reason="Voice session error.")
        except Exception:
            pass
    finally:
        if call_log is not None:
            # First, and with no await: this task may be on its way out
            # (cancelled by the server or by the demo's call cap), and an
            # await here could be the one that never returns. `save` hands
            # the rows to a worker thread and never raises.
            call_log.save()
        # Both counters, and POPPED at zero rather than left at 0: every key
        # here is remote-influenced (a peer IP, a claimed identity), so a bare
        # decrement would leave a dead entry in the dict forever for every
        # distinct caller that ever connected once.
        for _key in (concurrency_key, ip_key):
            _left = max(0, _active.get(_key, 1) - 1)
            if _left <= 0:
                _active.pop(_key, None)
            else:
                _active[_key] = _left
        # Cancel the periodic charger BEFORE booking the remainder: a tick
        # that landed between the final charge and the cancel would count
        # the same tail seconds twice. Awaited so it is genuinely finished
        # before `charged` is read.
        #
        # It CAN now be mid-charge — the charge moved off the loop, so the
        # task has an await point inside it (this comment used to say the
        # opposite, and that was load-bearing). The charger books its delta
        # BEFORE that await for exactly this reason, so `charged["total"]`
        # is already correct when we read it here, whether or not the cancel
        # interrupted a charge in flight. See the charger's own comment.
        if budget_task is not None:
            budget_task.cancel()
            try:
                await budget_task
            except (asyncio.CancelledError, Exception):
                pass
        # Charge the real duration, minus everything already booked — the
        # handshake advance plus every periodic delta — so the session's
        # recorded total is exactly `elapsed`, same as the old two-charge
        # scheme, with no double-counting.
        #
        # DELIBERATELY still synchronous, unlike every other blocking call
        # here (`_HANDSHAKE_OFF_LOOP`). This runs in the handler's `finally`,
        # and if the ASGI server is tearing the task down, a fresh await
        # point raises CancelledError immediately — which would skip
        # `session.close()` below and leak the provider connection, the exact
        # failure the surrounding try/except exists to prevent. One blocking
        # round trip on a socket that is already closing is the cheaper side
        # of that trade: it delays nothing the caller can hear, whereas the
        # calls that DID move off the loop each ran while other callers were
        # waiting.
        elapsed = int(time.monotonic() - started_at)
        try:
            _reserve_budget(budget_key, max(0, elapsed - charged["total"]))
        except Exception:
            # BudgetExhausted (already over -- the next handshake refuses
            # it) AND anything else (e.g. ValueError from
            # get_service_client() when AI Supabase env vars are missing)
            # must not skip session.close() below -- either would leak the
            # provider connection.
            pass
        # Session-summary span (turns / tool calls / barge-ins / duration).
        # Never raises; must not skip session.close() below either way.
        metrics.close(
            endpoint_requests_outstanding=getattr(
                session, "endpoint_requests_outstanding", None
            )
        )
        await session.close()
        # Assemble + persist the call recording LAST — the socket and the
        # provider session are both gone, so the only thing this can delay is
        # this coroutine's own exit. Bounded anyway: a long call streams
        # dozens of ~2MB chunk inserts to the AI project, and teardown must
        # not hang forever on a slow DB. finalize_and_store never raises on
        # its own.
        if recorder is not None:
            try:
                await asyncio.wait_for(recorder.finalize_and_store(), timeout=120)
            except Exception:  # noqa: BLE001 - incl. TimeoutError
                logger.warning(
                    "voice.live: call recording finalize failed", exc_info=True
                )
