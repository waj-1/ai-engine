"""The dedicated duplex-voice screen: `/admin/agents/{profile_id}/voice`.

Why this exists at all
----------------------
`docs/superpowers/specs/2026-08-17-voice-config-unification-design.md` (D2)
opens on a counted defect: **fourteen operator-honoured `voice_config` keys had
no UI**. Two of them — `barge_in_enabled` and `answer_watchdog_sec` — are the
levers `CLAUDE.md` marks as *silent when wrong*, and one of those was pulled
during incident #38 and left disengaged for days. The levers most likely to be
switched off in an outage and forgotten were precisely the ones with no field
to see or restore them.

So the contract of this module is not "another form". It is:

1. **Every** key in `voice/live/config.py::_VOICE_FIELDS` has a typed field
   here, or an explicit entry in `_NO_UI_BY_DESIGN` saying why not.
   `tests/admin/test_voice_screen_field_coverage.py` asserts that partition
   generatively — a key added to `_VOICE_FIELDS` with no field and no
   exemption fails the suite with no pattern written for it, the same way
   a bilingual-parity test's rule-count assertion fires for a rule nobody
   registered.
2. A field the provider (or another setting) **overrides** renders an inline
   effective-value note instead of quietly displaying a value the session will
   never use — `describe_effective_overrides` below. `stream_sentences` is the
   worst case in the tree: it is AND-gated on `supports_streamed_tool_results`,
   which is `False` on `azure_voice_live` — every production duplex tenant — so
   the checkbox has read "on" while sentence streaming has never engaged on any
   span in the system's history.
3. A **load-bearing default** is stated, not left as an empty box.
   `stall_recovery_topic_terms` defaults `[]` and gates BOTH residual stall
   tiers, so empty means "both tiers are disabled on this deployment" — which
   an empty textarea says to nobody.

Reads and writes go through `voice/live/config.py::promote_legacy_voice_config`,
the same writer-side shim `admin/screens/agents.py` and the runtime reader use.
That is deliberate: a fourth copy of the flatten-the-legacy-`live`-block logic is
exactly the defect Part A removed, and the shim must stay until every stack has
run the `unify_voice_config` migration (Part B input #7).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from voice.live.config import LiveVoiceConfig, _VOICE_FIELDS, promote_legacy_voice_config

logger = logging.getLogger(__name__)

# Form-field prefix. Deliberately NOT `live_`: that prefix belonged to the
# Agents mapping form's fieldset (deleted 2026-08-26 — this screen is the
# single portal writer of `voice_config` now), and two forms sharing input
# names over one column is how a partial submission silently becomes a full
# overwrite. The prefix stays distinct so a stale scripted `live_*` POST can
# never be misread as a cockpit submission.
FIELD_PREFIX = "vc_"

# The marker this screen's form posts. A writer must be able to prove the form
# was rendered before it takes ownership of a default-True boolean.
FORM_MARKER = "_voice_form"

# The brain panel's own marker — a SEPARATE form with a SEPARATE write target.
# The voice form posts `voice_config` to the OWNER row; the brain form posts
# `model_main_id` / `memory_config` to the SPECIALIST row (the brain named by
# `voice_config.agent_profile_slug`). Sharing one marker would let one
# submission claim ownership of fields on two different rows.
BRAIN_FORM_MARKER = "_brain_form"


# --------------------------------------------------------------------------- #
# Field registry
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class VoiceField:
    """One typed control on the voice screen.

    `kind` drives both the rendered widget and the parse in
    `voice_config_from_form`:

      bool      checkbox        — owned unconditionally by a marked submission
      number    float input     — blank clears (falls back to the default)
      int       integer input   — blank clears
      text      one-line text   — blank clears
      lines     textarea, one entry per line -> list[str]; blank clears
      variants  textarea, one line -> str, several -> list[str]; blank clears
      json      textarea holding a JSON OBJECT -> dict; blank clears
      model     realtime-model <select>
    """

    key: str
    label: str
    section: str
    kind: str
    help: str = ""
    placeholder: str = ""

    @property
    def field_name(self) -> str:
        return f"{FIELD_PREFIX}{self.key}"


# Section order is the spec's, verbatim (D2). The blurbs say what the section
# decides, so an operator who lands here mid-incident can find the lever
# without reading voice/live/config.py.
VOICE_SECTIONS: Tuple[Tuple[str, str, str], ...] = (
    ("session", "Session",
     "What answers the call: the realtime model, the voice, the persona, and "
     "which profile supplies the brain."),
    ("turn", "Turn behaviour",
     "Who holds the floor, what rescues a turn that stalls, and how an "
     "answer gets said. All three incident levers live here — barge-in, "
     "the answer watchdog, and deterministic rendering."),
    ("speech", "Speech",
     "Everything the caller hears that is NOT an answer: holding lines, "
     "failure wording, and pace."),
    ("relay", "Relay",
     "Server-side custody of protected multi-beat flows (the OTP / IBAN "
     "change). Off means the realtime model runs those flows itself."),
    ("limits", "Limits",
     "Session, budget and concurrency ceilings. Per engine replica and "
     "best-effort; the persisted daily audio budget is the hard spend control."),
    ("recognition", "Recognition",
     "What the ASR stage is told to listen for."),
)

VOICE_FIELDS: Tuple[VoiceField, ...] = (
    # ---- Session ---------------------------------------------------------- #
    VoiceField(
        "enabled", "Duplex voice enabled", "session", "bool",
        help="Off closes every connect with CLOSE_FORBIDDEN. Defaults to OFF, "
             "so a profile that has never been configured has no duplex voice.",
    ),
    VoiceField(
        "text_only", "Ambient-scribe mode (text only)", "session", "bool",
        help="The realtime model answers in TEXT modality only — no synthesized "
             "speech ever reaches the client. Set on a dictation (ambient "
             "scribe) profile. Turning it on force-disables the holding line, the answer "
             "watchdog, sentence streaming and the greeting for the session.",
    ),
    VoiceField(
        "model_id", "Realtime model", "session", "model",
        help="An agent_models row with extra_config.realtime — the ears and "
             "mouth, never an agent's chat brain. Unset with voice enabled "
             "closes the socket at handshake rather than defaulting silently.",
    ),
    VoiceField(
        "voice_id", "Voice ID", "session", "text", placeholder="Kore",
        help="Provider voice identifier. Blank falls back to the dataclass "
             "default 'Kore' (a Gemini name) — set an Azure voice name on an "
             "azure_voice_live row.",
    ),
    VoiceField(
        "voice_by_language", "Voice by language", "session", "json",
        placeholder='{"ar": "ar-SA-HamedNeural"}',
        help="Primary language subtag → voice name. Keyed on 'ar', not 'ar-SA', "
             "so one entry covers every regional variant. Invalid JSON is "
             "ignored on save (the prior value is kept), never applied.",
    ),
    VoiceField(
        "language", "Language", "session", "text", placeholder="ar-SA",
        help="BCP-47 tag. Blank = provider default. Also selects which "
             "*_by_language entry every per-language field below resolves to.",
    ),
    VoiceField(
        "persona_prompt_slug", "Persona prompt slug", "session", "text",
        placeholder="support_voice_persona",
        help="An agent_prompts slug resolved at connect. The persona VERSION a "
             "caller hears is pinned on THIS row and frozen into its promote "
             "snapshot — see the served-vs-latest banner above.",
    ),
    VoiceField(
        "agent_profile_slug", "Serve calls with profile slug", "session", "text",
        placeholder="(blank = the active profile of this agent_kind)",
        help="The BRAIN. Blank — or a slug that does not resolve — falls back to "
             "the kind's active profile, which makes this a one-field rollback "
             "lever with the same posture as the realtime model.",
    ),
    VoiceField(
        "greeting_trigger", "Greeting trigger", "session", "text",
        placeholder="spoken first, before the caller says anything",
        help="A realtime session is silent until spoken to. Blank means the "
             "assistant waits — and the caller hangs up thinking the line is "
             "dead. Operator content, not a source constant.",
    ),

    # ---- Turn behaviour --------------------------------------------------- #
    VoiceField(
        "greeting_barge_in_guard_sec", "Greeting barge-in guard (sec)", "turn",
        "number", placeholder="8",
        help="Suppresses barge-in on the greeting only, from dispatch until "
             "its own turn ends (capped here). The caller's speakerphone "
             "echoing the greeting reads as speech to the provider's VAD, "
             "which used to cut the greeting mid-word on every single call "
             "(measured live: turn_ms 650-707, interrupted=True on every "
             "session's turn 0). 0 disables the guard entirely — the "
             "rollback lever back to that behaviour.",
    ),
    VoiceField(
        "barge_in_enabled", "Barge-in enabled", "turn", "bool",
        help="INCIDENT LEVER (#38). Off means the caller cannot interrupt at "
             "all and every in-flight answer plays to the end. Defaults ON. "
             "Pulled during an outage and left off for days — which is why it "
             "has a field here at all.",
    ),
    VoiceField(
        "interrupt_confirm_window_sec", "Interrupt confirm window (sec)",
        "turn", "number", placeholder="2",
        help="How long an interrupt may stay unconfirmed before it is judged "
             "background noise and the pending answer is spoken after all. "
             "0 restores the legacy cut-on-detection path.",
    ),
    VoiceField(
        "barge_in_sustain_sec", "Barge-in on sustained speech (sec)",
        "turn", "number", placeholder="0",
        help="Cut her when the caller has been talking this long over audio "
             "they can still hear — without waiting for their transcript, "
             "which azure-speech delivers only after they stop. 0 = off "
             "(transcript-confirmed cuts only). 0.6 is a good start.",
    ),
    VoiceField(
        "local_barge_in_sec", "Barge-in from caller audio level (sec)",
        "turn", "number", placeholder="0",
        help="The endpoint listens to the caller's (echo-cancelled) mic itself "
             "and flushes her queued audio after this much caller speech. For "
             "Gemini Live, which stops reporting interruptions once it has "
             "finished GENERATING — most of each sentence then plays from the "
             "browser buffer and cannot be cut. 0 = off. Outbound seeds 0.6.",
    ),
    VoiceField(
        "greeting_input_mute_sec", "Don't hear the caller during the greeting (sec)",
        "turn", "number", placeholder="0",
        help="The caller's audio reaches the model as silence until the greeting "
             "has finished playing, capped at this many seconds — echo or room "
             "noise can no longer cut the opening dead. The caller cannot talk "
             "over the greeting. 0 = off. The agents demo seeds 12.",
    ),
    VoiceField(
        "local_barge_in_dbfs", "Caller speech level threshold (dBFS)",
        "turn", "number", placeholder="-35",
        help="Level above which a caller audio frame counts as speech for the "
             "setting above. Raise towards -30 if room noise or echo cuts her; "
             "lower towards -40 if quiet callers cannot interrupt.",
    ),
    VoiceField(
        "prefetched_context", "Pre-fetched context (outbound)", "turn", "bool",
        help="Outbound simulations only. The case is fetched before dialling "
             "and rendered into the persona's {case_file}; the voice answers "
             "from it and calls the agent's ACTION tools directly — no "
             "ask_agent, no brain round trip, and every ask_agent recovery tier "
             "stands down. Needs a persona written for it (…_direct_persona). "
             "Off: the delegated ask_agent session.",
    ),
    VoiceField(
        "answer_watchdog_sec", "Answer watchdog (sec)", "turn", "number",
        placeholder="4",
        help="INCIDENT LEVER. Re-delivers ONCE an answer that was delivered and "
             "then produced no audio (the mute race: 74s of dead air observed "
             "live). 0 disables that rescue entirely.",
    ),
    VoiceField(
        "stall_recovery_sec", "Stall recovery — announced (sec)", "turn",
        "number", placeholder="2",
        help="The model said 'one moment, let me check' and ended the turn with "
             "no tool call. A realtime turn is request-scoped, so nothing "
             "prompts it again — this grace then dispatches the question. "
             "0 disables this tier.",
    ),
    VoiceField(
        "stall_recovery_unannounced_sec", "Stall recovery — unannounced (sec)",
        "turn", "number", placeholder="6",
        help="Residual tier: the turn ended with no announce phrase AND no tool "
             "call, while the caller's utterance named a topic term. Gated on "
             "the topic terms below — with those empty this value is inert.",
    ),
    VoiceField(
        "stall_recovery_silent_sec", "Stall recovery — silent (sec)", "turn",
        "number", placeholder="12",
        help="Last line: the model never spoke and never ended a turn (85s of "
             "silence, measured). Hangs off the caller's commit, so nothing the "
             "model does can blind it. Also gated on the topic terms below.",
    ),
    VoiceField(
        "stall_recovery_topic_terms", "Stall recovery topic terms", "turn",
        "lines",
        help="One per line. LOAD-BEARING DEFAULT: empty, which disables BOTH "
             "residual tiers above. The engine is unbranded and cannot guess a "
             "tenant's vocabulary, so this tier is opt-in — list the words only "
             "your agent can answer ('revenue', 'branch', 'IBAN', 'الفرع').",
    ),
    VoiceField(
        "persona_claim_recovery_enabled", "Persona-claim stall recovery", "turn", "bool",
        help="Off by default. On: a persona turn with NO ask_agent that claims a "
             "result ('here are your invoices', «الحين تشوف … قدامك», 'refresh "
             "the page'), or that answers a caller saying the screen is empty, "
             "is treated as a stall and the request is delegated. Farewells "
             "never arm it. Useful for a voice agent that works inside an app the "
             "caller is looking at.",
    ),
    VoiceField(
        "stream_sentences", "Stream sentences", "turn", "bool",
        help="Speak the answer sentence by sentence as it streams (~10s → ~3s "
             "to first speech) instead of waiting for the whole turn. Defaults "
             "ON, and is AND-gated on the provider declaring "
             "supports_streamed_tool_results.",
    ),
    VoiceField(
        "kb_prefetch_enabled", "KB prefetch enabled", "turn", "bool",
        help="Retrieves the documentation BEFORE the brain's first call, so a "
             "KB question costs one model call instead of two. Defaults ON. "
             "Off silently returns every documentation question to the "
             "3568ms two-call path — nothing reports it, answers stay "
             "correct and just get slow.",
    ),
    VoiceField(
        "kb_prefetch_match_count", "KB prefetch chunk count", "turn", "int",
        placeholder="3",
        help="How many KB chunks are injected before the first call. Too "
             "high re-creates the prompt bloat the prefetch exists to "
             "remove (6 chunks measured 12,815 chars p50); too low silently "
             "starves the answer of the passage that contains it. Floored "
             "at 1.",
    ),
    VoiceField(
        "deterministic_render_enabled", "Deterministic rendering enabled",
        "turn", "bool",
        help="INCIDENT LEVER. Renders OTP/ticket/IBAN results from a "
             "template and ends the turn with no synthesis call, so a "
             "renderer is structurally incapable of inventing an identifier. "
             "Defaults ON. Off hands that wording back to the model and "
             "reopens the fabrication surface — a live call once read back "
             "an IBAN matching neither the merchant's record nor the number "
             "he had dictated minutes earlier.",
    ),

    # ---- Speech ----------------------------------------------------------- #
    VoiceField(
        "progress_after_sec", "Holding line after (sec)", "speech", "number",
        placeholder="8",
        help="Seconds of a long delegated turn before the holding line is "
             "spoken. 8, not 5: the persona already announces the lookup, and "
             "at 5s the caller heard two near-identical waits back to back. "
             "0 disables it.",
    ),
    VoiceField(
        "progress_on_dispatch", "Speak the holding line on dispatch", "speech",
        "bool",
        help="MODE, not a smarter timer. Off (default): today's exact "
             "behaviour — wait 'Holding line after' seconds before speaking. "
             "On: speak it immediately when the lookup starts instead of "
             "waiting, so the caller hears 'understood, one moment' right "
             "after they finish instead of four seconds into silence. "
             "'Holding line after' still has to be non-zero for a holding "
             "line to exist at all — this only changes WHEN it is said. The "
             "endpoint cannot predict a tool's duration, so this also speaks "
             "on a sub-second turn that today says nothing at all — a brief "
             "'one moment' right before an answer that was already fast.",
    ),
    VoiceField(
        "progress_spoken", "Holding line", "speech", "variants",
        placeholder="Still working on that, one moment.",
        help="One line, or SEVERAL lines for variants picked per lookup — a "
             "caller who hears the identical line on every wait clocks it as "
             "fake. Asserts no facts, so it is safe to speak verbatim.",
    ),
    VoiceField(
        "progress_spoken_by_language", "Holding line by language", "speech",
        "json", placeholder='{"ar": "لحظة، أتأكد لك الحين."}',
        help="Primary subtag → line (or a list of variants). Without an entry "
             "the single line above is spoken as written — which is how an "
             "English 'still working on that' landed mid-call in an Arabic "
             "session on the demo VM.",
    ),
    VoiceField(
        "failure_spoken", "Failure line", "speech", "text",
        placeholder="(blank = built-in per-language wording)",
        help="Spoken when a delegated turn errors, times out, or returns "
             "nothing. Blank falls back to built-in wording, never to silence.",
    ),
    VoiceField(
        "failure_spoken_by_language", "Failure line by language", "speech",
        "json", placeholder='{"ar": "صار عندنا خلل بسيط."}',
        help="Primary subtag → failure line. Same keying as the voice map.",
    ),
    VoiceField(
        "on_screen_spoken", "On-screen pointer line", "speech", "text",
        placeholder="(blank = built-in per-language wording)",
        help="Spoken when an answer's substance is visual (a chart) and there "
             "is nothing useful to read aloud.",
    ),
    VoiceField(
        "on_screen_spoken_by_language", "On-screen pointer by language",
        "speech", "json", placeholder='{"ar": "عرضته لك على الشاشة."}',
        help="Primary subtag → on-screen pointer line.",
    ),
    VoiceField(
        "say_exactly_instruction", "Say-exactly instruction", "speech", "text",
        placeholder="(blank = built-in per-language wording)",
        help="Wraps an answer the ENDPOINT delivers itself — relay custody, "
             "stall/blank-call recovery, the answer watchdog's re-delivery. It "
             "reaches the model as a caller-side text turn, so it is a PROMPT: "
             "the model obeys it or improvises around it, and it obeys best in "
             "the language of the call. Must contain {spoken}; a value that "
             "cannot take the answer falls back to the built-in rather than "
             "delivering an answer with the words missing.",
    ),
    VoiceField(
        "say_exactly_instruction_by_language", "Say-exactly by language",
        "speech", "json",
        placeholder='{"ar": "قل هذا للمتصل حرفياً، ولا تقل شيئاً غيره: {spoken}"}',
        help="Primary subtag → instruction. Same keying and same {spoken} "
             "requirement as the single line above.",
    ),
    VoiceField(
        "speaking_rate", "Speaking rate", "speech", "text",
        placeholder="+10% / -20% / 0.9",
        help="Azure prosody rate string. Blank sends no rate key at all — a "
             "provider that never receives the key cannot reject it.",
    ),

    # ---- Relay ------------------------------------------------------------ #
    VoiceField(
        "relay_mode", "Relay custody enabled", "relay", "bool",
        help="Defaults ON. While a protected flow is open the ENDPOINT routes "
             "every committed utterance straight into a delegated turn: the "
             "realtime model keeps ears, mouth and persona but loses mid-flow "
             "discretion. Built after one live day produced a fabricated IBAN "
             "write, a phantom code request and a dead-air stall.",
    ),
    VoiceField(
        "relay_intent_terms", "Relay intent terms", "relay", "lines",
        help="One per line. A caller utterance containing any of these opens "
             "custody speculatively, BEFORE any tool runs — tool-signal custody "
             "can only start after a delegated turn exists, and the garbled "
             "opening beat is exactly the one that escapes it. Blank restores "
             "the built-in IBAN spellings.",
    ),
    VoiceField(
        "relay_max_turns", "Relay max turns", "relay", "int", placeholder="8",
        help="Custody ceiling. Floored at 1 — a 0 or negative value would close "
             "custody the moment it opens and run the protected flow in the "
             "realtime model, which is the 2026-07-31 incident mode.",
    ),
    VoiceField(
        "relay_max_sec", "Relay max duration (sec)", "relay", "number",
        placeholder="180",
        help="The other custody ceiling, so a wedged flow can never hold a call "
             "hostage. Floored at 1 for the same reason as the turn ceiling.",
    ),
    VoiceField(
        "relay_quiet_grace_turns", "Relay quiet-turn grace", "relay", "int",
        placeholder="2",
        help="Consecutive flow-quiet delegated turns that close custody "
             "normally. The read-back beat legitimately calls no tool, so one "
             "quiet turn is expected.",
    ),
    VoiceField(
        "server_side_iban_capture", "Server-side IBAN digit capture", "relay",
        "bool",
        help="Defaults OFF. ON, the ENDPOINT holds the IBAN digit tally "
             "instead of the model — it opens on a successful OTP verify "
             "(DelegateResult.flow_signals: verify_otp_ok), folds each "
             "caller utterance into a server-held buffer, and hands the "
             "brain agent the complete number only once the caller confirms "
             "the full read-back. Fixes three live failures of the "
             "model-held tally: divergent caller/spoken/on-screen numbers, "
             "an invisible miscount (no tool call to audit), and an ASR "
             "hallucination silently corrupting the buffer. OFF keeps the "
             "persona's own tally-in-context instructions running "
             "byte-identically to today — this is an alternative path, not "
             "a replacement.",
    ),

    # ---- Limits ----------------------------------------------------------- #
    VoiceField(
        "delegate_timeout_sec", "Delegate timeout (sec)", "limits", "number",
        placeholder="30",
        help="Hard cap on ONE delegated agent turn. 30, not 15: a multi-tool "
             "turn blew the 15s cap and the caller was told 'there was a system "
             "problem' for what was really impatience on our side.",
    ),
    VoiceField(
        "max_session_sec", "Max session length (sec)", "limits", "number",
        placeholder="900",
        help="Bounds OUR exposure to a socket nobody is listening to; the "
             "provider caps session length anyway (~30 min on Azure).",
    ),
    VoiceField(
        "handshake_charge_sec", "Handshake budget charge (sec)", "limits",
        "number", placeholder="30",
        help="Audio-seconds booked the moment the provider session comes up, so "
             "an abandoned socket still costs something.",
    ),
    VoiceField(
        "budget_charge_interval_sec", "Budget charge interval (sec)", "limits",
        "number", placeholder="60",
        help="How often a RUNNING session books its elapsed-but-uncharged "
             "seconds. Without it a replica killed mid-session booked 30s for a "
             "15-minute call. <= 0 reverts to the legacy handshake+teardown "
             "scheme; a positive value is floored at 5s server-side.",
    ),
    VoiceField(
        "max_concurrent_sessions", "Max concurrent sessions (per identity)",
        "limits", "int", placeholder="3",
        help="Per verified caller (or peer IP when unverified). Floored at 1: "
             "a typo'd 0 or -1 makes the FIRST connection already over-limit "
             "and every call dies CLOSE_RATE_LIMITED.",
    ),
    VoiceField(
        "max_concurrent_per_ip", "Max concurrent sessions (per IP)", "limits",
        "int", placeholder="8",
        help="The ceiling that actually bounds abuse — only a signature-verified "
             "bearer narrows the identity key, so an anonymous caller could "
             "otherwise mint a fresh identity per connection. Also floored at 1.",
    ),

    # ---- Recognition ------------------------------------------------------ #
    VoiceField(
        "phrase_list", "Recognition phrase list", "recognition", "lines",
        help="One domain term per line ('IBAN', product names, branch names), boosted "
             "in the ASR stage. Consumed only by a provider with a real "
             "recognizer.",
    ),
    VoiceField(
        "caller_languages", "Caller languages", "recognition", "lines",
        help="One BCP-47 tag per line (ar-JO, en-US). With two or more, the "
             "Azure recognizer auto-detects which one the caller is speaking "
             "instead of being locked to the session language (which the "
             "app's UI locale can override). Azure recognizer only.",
    ),
    VoiceField(
        "backchannel_tokens", "Extra backchannel tokens", "recognition", "lines",
        help="One per line, MERGED into the built-in EN+AR set — whole-utterance "
             "nods that must never supersede an in-flight answer. Add a "
             "tenant's assistant name here: 'يا' is built in, 'جوهرة' is not.",
    ),
)

# Keys in `_VOICE_FIELDS` that deliberately have NO control on this screen.
#
# EMPTY, and that is the point: as of this change every operator-honoured
# duplex key is typed and visible. The set exists so a future key CAN be
# excluded — but only out loud, with the reason written next to it, because the
# alternative is what the spec counted: fourteen honoured keys that no operator
# could see. `tests/admin/test_voice_screen_field_coverage.py` asserts
# `{f.key for f in VOICE_FIELDS} | _NO_UI_BY_DESIGN == set(_VOICE_FIELDS)` and
# that the two do not overlap, so adding a key to `_VOICE_FIELDS` fails the
# suite until it is placed on one side or the other.
#
# Note what is NOT in scope for that partition: `provider`, `model_string` and
# `extra_config` are `LiveVoiceConfig` fields but not `_VOICE_FIELDS` entries —
# `hydrate_live_model` fills them from the agent_models row, they are never
# operator input, and they surface on this screen read-only as the resolved
# provider panel.
_NO_UI_BY_DESIGN: frozenset = frozenset()

_FIELD_BY_KEY: Dict[str, VoiceField] = {f.key: f for f in VOICE_FIELDS}


def fields_for_section(section: str) -> List[VoiceField]:
    return [f for f in VOICE_FIELDS if f.section == section]


# --------------------------------------------------------------------------- #
# Provider capability lookup
# --------------------------------------------------------------------------- #
def provider_capabilities(provider: Optional[str]) -> Optional[Dict[str, Any]]:
    """Declared capability flags for a realtime provider slug, or None.

    Read straight off the `RealtimeSession` subclass — these are CLASS
    attributes on the contract (`voice/live/base.py`), set by each provider from
    its own SDK reality, so this is a read of declared capability rather than a
    second, drifting opinion about what a provider can do. Building a session to
    ask would need credentials and a socket; the flags do not depend on either.

    Imported lazily and defensively: the providers pull in optional SDKs, and
    the admin portal must render a voice screen even on a build where one of
    them will not import.
    """
    slug = (provider or "").strip().lower()
    if not slug:
        return None
    try:
        if slug == "azure_voice_live":
            from voice.live.providers.azure_voice_live import AzureVoiceLiveSession as cls
        elif slug == "google_genai":
            from voice.live.providers.gemini_live import GeminiLiveSession as cls
        else:
            return None
    except Exception as exc:  # pragma: no cover - optional SDK missing
        logger.warning("agent_voice: provider capability import failed (%s)", exc)
        return None
    return {
        "provider": slug,
        "supports_streamed_tool_results": bool(
            getattr(cls, "supports_streamed_tool_results", False)),
        "transcribes_streamed_tool_results": bool(
            getattr(cls, "transcribes_streamed_tool_results", False)),
        "supports_out_of_band_speech": bool(
            getattr(cls, "supports_out_of_band_speech", False)),
        "supports_barge_in_confirmation": bool(
            getattr(cls, "supports_barge_in_confirmation", False)),
        "finalizes_user_transcripts": bool(
            getattr(cls, "finalizes_user_transcripts", False)),
        "supports_reprompt": bool(getattr(cls, "supports_reprompt", False)),
        "reports_response_in_flight": bool(
            getattr(cls, "reports_response_in_flight", False)),
        "finalizes_user_transcripts": bool(
            getattr(cls, "finalizes_user_transcripts", False)),
    }


# --------------------------------------------------------------------------- #
# Effective-value notes
# --------------------------------------------------------------------------- #
def describe_effective_overrides(
    block: Dict[str, Any],
    *,
    model_row: Optional[Dict[str, Any]] = None,
    capabilities: Optional[Dict[str, Any]] = None,
    agent_kind: Optional[str] = None,
) -> Dict[str, List[Dict[str, str]]]:
    """`{voice_config key: [{level, text}, …]}` — what actually happens.

    The spec's fix for problem 6: a screen must not assert a value the session
    will not use. Every entry here is derived from a gate that exists in the
    tree, named in the text so an operator can go read it:

      * `stream_sentences`  × `supports_streamed_tool_results`
        (`endpoints/voice_live.py`'s streaming branch), and separately
        × `transcribes_streamed_tool_results`, which does not gate streaming
        at all — it says whether the SCREEN reads the provider or the
        endpoint's own copy of each sentence
      * `text_only` force-disables the holding line, the watchdog, sentence
        streaming and the greeting (`_apply_text_only_overrides`)
      * `stall_recovery_topic_terms` gates BOTH residual stall tiers
      * `speaking_rate` is dropped on `extra_config.voice_type: "native"`
      * `phrase_list` needs `transcription_model == "azure-speech"`
      * `voice_by_language` is ignored by Gemini Live, which reads `voice_id`
      * `relay_*` ceilings do nothing while `relay_mode` is off
      * `answer_watchdog_sec` needs `reports_response_in_flight`
      * every commit-keyed lever (the stall tiers, `relay_intent_terms`,
        `backchannel_tokens`) needs `finalizes_user_transcripts`

    `level` is `"off"` (the value is not used at all), `"warn"` (an incident
    lever is disengaged) or `"info"` (a default worth stating). The caller
    renders; nothing here decides anything.

    `agent_kind` matters for a kind that registers voice defaults or a
    built-in persona (a generic agent): a blank greeting or persona slug is
    then a default in use, not a misconfiguration.
    """
    from runtime.agents.voice_registry import (voice_agent_slug_addressed,
                                               voice_config_defaults,
                                               voice_persona_fallback)

    kind_defaults = voice_config_defaults(agent_kind or "")
    builtin_persona = voice_persona_fallback(agent_kind or "") is not None
    slug_addressed = voice_agent_slug_addressed(agent_kind or "")
    extra = (model_row or {}).get("extra_config") or {}
    if not isinstance(extra, dict):
        extra = {}
    caps = capabilities or {}
    notes: Dict[str, List[Dict[str, str]]] = {}

    def add(key: str, level: str, text: str) -> None:
        notes.setdefault(key, []).append({"level": level, "text": text})

    defaults = LiveVoiceConfig()

    def value(key: str) -> Any:
        return block.get(key, getattr(defaults, key, None))

    def num(key: str) -> float:
        """`value(key)` as a float, mirroring `_coerce_numeric_fields`.

        `voice_config` is RAW OPERATOR JSON — `update_profile` over the admin
        MCP and a direct DB edit both bypass this form's float(), so a string
        like "eight" or a null can reach here. The runtime coerces the same way
        at the config boundary and falls back to the dataclass default; a note
        generator that raised instead would take the whole screen down on
        exactly the misconfigured tenant it exists to explain.
        """
        try:
            return float(value(key))
        except (TypeError, ValueError):
            return float(getattr(defaults, key, 0) or 0)

    provider = caps.get("provider") or ""
    text_only = bool(value("text_only"))

    # --- the whole block is inert while voice is off ----------------------- #
    if not value("enabled"):
        add("enabled", "off",
            "Duplex voice is OFF for this profile: every connect closes with "
            "CLOSE_FORBIDDEN and nothing else on this page takes effect.")

    # --- stream_sentences: the spec's worst case --------------------------- #
    if value("stream_sentences"):
        if provider and not caps.get("supports_streamed_tool_results"):
            add("stream_sentences", "off",
                f"Off on this profile's provider — `{provider}` declares "
                "supports_streamed_tool_results = False, so it accepts only one "
                "tool result per call id and the endpoint refuses to stream "
                "regardless of this checkbox.")
        elif provider and not caps.get("transcribes_streamed_tool_results"):
            # NOT an "off" note: streaming is ON here. What changes is where
            # the SCREEN's copy of the answer comes from.
            add("stream_sentences", "info",
                f"On, and the screen does not read it from the provider — "
                f"`{provider}` declares transcribes_streamed_tool_results = "
                "False: it speaks every word of a streamed answer but "
                "transcribes only the first two of each sentence. The "
                "endpoint sends the caller's screen its own copy of each "
                "sentence instead, released as she starts saying it, so "
                "streaming keeps its latency win without costing the "
                "transcript.")
        elif not provider:
            add("stream_sentences", "info",
                "Effective value unknown until a realtime model is assigned: "
                "this is AND-gated on the provider declaring "
                "supports_streamed_tool_results.")
    if text_only and value("stream_sentences"):
        add("stream_sentences", "off",
            "Also forced off by ambient-scribe mode (text_only): there is no "
            "audio to stream.")

    # --- text_only's other force-disables ---------------------------------- #
    if text_only:
        add("text_only", "info",
            "On: no synthesized speech reaches the client. The holding line, "
            "the answer watchdog and the greeting are force-disabled for the "
            "session; the stall tiers stay armed and read assistant TEXT.")
        if value("progress_after_sec"):
            add("progress_after_sec", "off",
                "Forced to 0 by ambient-scribe mode (text_only) — a scribe "
                "speaks no holding line.")
        if value("answer_watchdog_sec"):
            add("answer_watchdog_sec", "off",
                "Forced to 0 by ambient-scribe mode (text_only) — there is no "
                "audio for the watchdog to miss.")
        if value("greeting_trigger"):
            add("greeting_trigger", "off",
                "Cleared by ambient-scribe mode (text_only) — a scribe listens, "
                "it does not greet.")

    # --- the three incident levers ------------------------------------------ #
    if not value("barge_in_enabled"):
        add("barge_in_enabled", "warn",
            "OFF — the caller cannot interrupt at all and every in-flight "
            "answer plays to the end. This is an incident rollback lever (#38); "
            "if it was pulled during an outage, this is where it goes back on.")
        add("interrupt_confirm_window_sec", "off",
            "Barge-in is OFF on this profile, so no interrupt ever silences a "
            "reply and this window has nothing to recover.")
    elif not num("interrupt_confirm_window_sec"):
        add("interrupt_confirm_window_sec", "info",
            "0 — the legacy path: any interrupt silences the pending answer "
            "immediately, including one tripped by background noise.")
    elif provider and not caps.get("supports_barge_in_confirmation"):
        add("interrupt_confirm_window_sec", "info",
            f"`{provider}` does not declare supports_barge_in_confirmation, so "
            "the held playback-flush valve never engages; the window still "
            "governs false-barge-in answer recovery.")

    if not num("answer_watchdog_sec") and not text_only:
        add("answer_watchdog_sec", "warn",
            "0 — the re-delivery that rescues an answer which never starts "
            "speaking is DISABLED. That rescue exists for a measured 74s of "
            "dead air; 0 is a rollback lever, not a tuning value.")
    elif num("answer_watchdog_sec") and provider \
            and not caps.get("reports_response_in_flight"):
        add("answer_watchdog_sec", "off",
            f"Not armed on this profile's provider — `{provider}` does not "
            "report whether a response is in flight, and the watchdog's only "
            "safety margin IS that reading (delivery to first audio on an idle "
            "floor is 0.21s, so no threshold tells a wedged answer from one "
            "still on its way). The mute race it rescues is a rejected "
            "answer-create leaving a result item unspoken, which is specific "
            "to a provider that creates the answer response itself.")

    if not value("deterministic_render_enabled"):
        add("deterministic_render_enabled", "warn",
            "OFF — OTP/ticket/IBAN results are phrased by the model instead "
            "of rendered from a template, reopening the fabrication surface "
            "PR #137 exists for: a live call once read back an IBAN matching "
            "neither the merchant's record nor the number he had dictated "
            "minutes earlier. This is an incident rollback lever — if it was "
            "pulled during an outage, this is where it goes back on.")

    # --- the commit-keyed levers ------------------------------------------- #
    # Everything below reads `last_commit`, which is populated only from the
    # FINAL fragment of a user transcript. On a provider that never finalizes
    # one these boxes accept input and change nothing — the exact "a screen
    # must not assert a value the session will not use" failure this module
    # exists for, and invisible without the declared flag.
    if provider and not caps.get("finalizes_user_transcripts"):
        for key, what in (
            ("stall_recovery_sec", "the announced stall tier"),
            ("stall_recovery_unannounced_sec", "the unannounced stall tier"),
            ("stall_recovery_silent_sec", "the silent stall tier"),
            ("stall_recovery_topic_terms", "both residual stall tiers"),
            ("relay_intent_terms", "caller-intent relay custody"),
            ("backchannel_tokens", "the backchannel verdict"),
            ("server_side_iban_capture", "server-side IBAN digit capture"),
        ):
            add(key, "off",
                f"Inert on this profile's provider — `{provider}` does not "
                "close a caller utterance with a final transcript "
                "(finalizes_user_transcripts = False), and "
                f"{what} is armed from that commit. Relay custody still opens "
                "on a delegated turn's own flow signals.")

    # --- the residual stall tiers ------------------------------------------ #
    topic_terms = value("stall_recovery_topic_terms") or []
    if not topic_terms:
        add("stall_recovery_topic_terms", "warn",
            "EMPTY — both residual stall tiers (unannounced and silent) are "
            "DISABLED on this deployment. That is the shipped default: the "
            "engine is unbranded and arming on every turn would speak over the "
            "persona's goodbye, so the tiers are opt-in.")
        for key, tier in (("stall_recovery_unannounced_sec", "unannounced"),
                          ("stall_recovery_silent_sec", "silent")):
            if num(key):
                add(key, "off",
                    f"Inert: the {tier} tier is gated on the topic terms above, "
                    "which are empty.")
    if not num("stall_recovery_sec"):
        add("stall_recovery_sec", "info",
            "0 — the announced tier is disabled. A turn that says 'one moment, "
            "let me check' and emits no tool call is FINISHED, not late, and "
            "nothing will prompt the model again.")

    # --- speech ------------------------------------------------------------ #
    voice_type = str(extra.get("voice_type") or "azure-standard")
    if value("speaking_rate") and voice_type == "native":
        add("speaking_rate", "off",
            "Ignored: the assigned realtime model is a NATIVE voice row "
            "(extra_config.voice_type = \"native\"), whose voice field is a bare "
            "string with no object to hang a rate key off.")
    lang = str(value("language") or "").split("-")[0].strip().lower()
    for base, mapping_key in (("progress_spoken", "progress_spoken_by_language"),
                              ("failure_spoken", "failure_spoken_by_language"),
                              ("on_screen_spoken", "on_screen_spoken_by_language"),
                              ("say_exactly_instruction",
                               "say_exactly_instruction_by_language")):
        mapping = value(mapping_key)
        if lang and isinstance(mapping, dict) and mapping.get(lang):
            add(base, "off",
                f"Overridden for this profile's language ({lang}) by the "
                f"per-language entry below.")

    if lang and lang != "en" and not text_only:
        from voice.live.config import _PROGRESS_SPOKEN

        mapping = value("progress_spoken_by_language")
        flat = value("progress_spoken")
        if (
            lang in _PROGRESS_SPOKEN
            and not (isinstance(mapping, dict) and mapping.get(lang))
            and flat == defaults.progress_spoken
        ):
            add("progress_spoken", "info",
                f"Not spoken as written: nobody has set a holding line, so the "
                f"built-in {lang} wording is used instead of this English "
                "default. Type your own here and it wins in every language.")

    # --- relay ------------------------------------------------------------- #
    if not value("relay_mode"):
        add("relay_mode", "warn",
            "OFF — the realtime model runs protected multi-beat flows itself. "
            "That is the configuration under which a live call wrote an "
            "INVENTED IBAN and asked for a code that was never sent.")
        for key in ("relay_intent_terms", "relay_max_turns", "relay_max_sec",
                    "relay_quiet_grace_turns"):
            add(key, "off", "Inert while relay custody is off.")
    elif provider and not caps.get("finalizes_user_transcripts"):
        # Custody dispatches its relayed turn from the FINAL fragment of the
        # caller's transcript and nothing else, so on a provider that never
        # sends one it can only ever suppress. The endpoint now refuses to open
        # it there (it used to open and swallow the model's ask_agent calls —
        # the caller's OTP code reached nobody, live 2026-08-19), but the
        # operator still needs to see that the guard they configured is inert.
        add("relay_mode", "off",
            f"ON but INERT: `{provider}` does not finalize user transcripts, "
            "which is the only event custody can dispatch a relayed turn from. "
            "The endpoint keeps custody CLOSED rather than suppressing calls it "
            "cannot replace, so protected flows run on the model's own "
            "ask_agent calls — the fabricated-IBAN / unsent-code guard is not "
            "active. Assign an azure_voice_live realtime model row to get it.")
        for key in ("relay_intent_terms", "relay_max_turns", "relay_max_sec",
                    "relay_quiet_grace_turns"):
            add(key, "off",
                f"Inert: custody never opens on `{provider}` (see relay_mode).")

    if value("server_side_iban_capture") and provider and not caps.get(
        "finalizes_user_transcripts"
    ):
        # Same fail-safe shape as relay_mode just above: the digit buffer
        # advances from the FINAL fragment of the caller's transcript and has
        # no other trigger, so a provider that never sends one leaves this ON
        # in the portal while the endpoint refuses to open it.
        add("server_side_iban_capture", "off",
            f"ON but INERT: `{provider}` does not finalize user transcripts, "
            "which is the only event the capture buffer can advance from. The "
            "IBAN dictation runs on the persona's own model-held tally "
            "instead — exactly as if this were OFF.")

    # --- recognition ------------------------------------------------------- #
    stt_model = str(extra.get("transcription_model") or "azure-speech")
    if value("phrase_list"):
        if provider == "google_genai":
            add("phrase_list", "off",
                "Ignored: Gemini Live has no separate recognizer stage to boost.")
        elif provider and stt_model != "azure-speech":
            add("phrase_list", "off",
                f"Ignored: phrase lists exist only on the Azure recognizer, and "
                f"this model row sets transcription_model = \"{stt_model}\".")
    if value("caller_languages"):
        if provider == "google_genai":
            add("caller_languages", "off",
                "Ignored: Gemini Live has no separate recognizer to set languages on.")
        elif provider and stt_model != "azure-speech":
            add("caller_languages", "off",
                f"Ignored: language auto-detection is set only on the Azure "
                f"recognizer, and this model row sets transcription_model = \"{stt_model}\".")
    if value("voice_by_language") and provider == "google_genai":
        add("voice_by_language", "off",
            "Ignored: Gemini Live reads voice_id directly and never consults "
            "the per-language map.")

    # --- session ----------------------------------------------------------- #
    if value("enabled") and not value("model_id"):
        add("model_id", "warn",
            "Unset while duplex voice is enabled — every connect raises "
            "LookupError at handshake and the socket closes. This is a "
            "misconfiguration the runtime surfaces loudly rather than defaulting.")
    if not str(value("persona_prompt_slug") or "").strip():
        if builtin_persona:
            add("persona_prompt_slug", "info",
                "Blank: this agent speaks with the built-in voice persona, which "
                "sends every question to the agent and says its answer. Name a "
                "prompt here only to replace it with your own.")
        elif value("enabled"):
            add("persona_prompt_slug", "warn",
                "Unset while duplex voice is enabled — the connect has no persona "
                "to speak and closes.")
    if not str(value("greeting_trigger") or "").strip() and not text_only:
        if kind_defaults.get("greeting_trigger"):
            add("greeting_trigger", "info",
                "Blank: the built-in greeting is used "
                f"(\"{kind_defaults['greeting_trigger']}\"). Write your own to "
                "replace it.")
        elif value("enabled"):
            add("greeting_trigger", "info",
                "Blank — the assistant stays silent until the caller speaks first. "
                "On a phone-shaped deployment that reads as a dead line.")
    if slug_addressed and not str(value("agent_profile_slug") or "").strip():
        add("agent_profile_slug", "info",
            "Blank: this agent answers its own calls. A slug here must be "
            "another agent of the same kind; one that does not exist refuses "
            "the call instead of falling back.")

    return notes


# --------------------------------------------------------------------------- #
# Render values
# --------------------------------------------------------------------------- #
def _render_value(field: VoiceField, block: Dict[str, Any]) -> Any:
    """The value to put in `field`'s widget: stored value, else "" / False.

    Deliberately NOT the dataclass default: an operator must be able to tell
    "nobody has ever set this, so the default applies" from "someone set it to
    exactly the default". The default is shown as the placeholder and repeated
    in the field's help text instead. `stall_recovery_topic_terms` is the case
    that proves the rule — an empty box there means both residual stall tiers
    are off, which is a fact about this deployment, not an absence.
    """
    raw = block.get(field.key)
    if field.kind == "bool":
        # An unset boolean falls back to the DATACLASS default, because that is
        # what the session will resolve — a checkbox has no "unset" position, so
        # rendering False for an absent `stream_sentences` (default True) would
        # be the screen asserting the opposite of what runs.
        if raw is None:
            return bool(getattr(LiveVoiceConfig(), field.key, False))
        return bool(raw)
    if raw is None:
        return ""
    if field.kind == "lines":
        return "\n".join(str(v) for v in raw) if isinstance(raw, (list, tuple)) else str(raw)
    if field.kind == "variants":
        if isinstance(raw, (list, tuple)):
            return "\n".join(str(v) for v in raw)
        return str(raw)
    if field.kind == "json":
        if isinstance(raw, dict):
            return json.dumps(raw, indent=2, ensure_ascii=False)
        return str(raw)
    return raw


def render_values(block: Dict[str, Any]) -> Dict[str, Any]:
    return {f.key: _render_value(f, block) for f in VOICE_FIELDS}


def field_defaults() -> Dict[str, Any]:
    """The `LiveVoiceConfig` default for every field, for the "unset means…"
    line under each control. Read off the dataclass, never re-typed here."""
    defaults = LiveVoiceConfig()
    return {f.key: getattr(defaults, f.key, None) for f in VOICE_FIELDS}


# --------------------------------------------------------------------------- #
# Writer
# --------------------------------------------------------------------------- #
def voice_config_from_form(form, existing: Optional[dict] = None) -> Optional[dict]:
    """The whole flat `voice_config` from this screen's form.

    Returns ``None`` when the submission did not come from this screen (no
    `_voice_form=1` marker), which tells the caller to leave the column alone.
    Same gate, and the same reason, the retired mapping-form fieldset carried
    (`live_fieldset=1`): this screen owns every key in `_VOICE_FIELDS`,
    including booleans whose dataclass default is True, so it may only take
    ownership of an ABSENT checkbox when it can prove the checkbox was
    rendered. Since the fieldset's deletion (2026-08-26) this function is the
    ONLY portal writer of `voice_config`.

    `existing` is promoted through `promote_legacy_voice_config` on the way in,
    so a save from this screen MIGRATES an unmigrated row (flat keys written
    beside a surviving `live` key are invisible to the reader, which resolves
    the nested block alone). Keys outside `_VOICE_FIELDS` — `_retired_turn_based`
    above all — are carried through untouched.

    Blank clears: a text/number/list/JSON field left empty is omitted from the
    result, so the runtime falls back to the dataclass default. That is the only
    way to reset a field from a form, and it is why `existing` is not merged
    wholesale.

    Blank is not the same as ABSENT, and the difference is load-bearing. The
    rendered screen posts every non-checkbox control on every save, empty when
    the operator cleared it, so a key whose field is missing from the
    submission entirely did not come from this screen's form and is carried
    through untouched. Checkboxes are the exception: they have no "absent"
    state to distinguish, which is exactly why the marker above has to prove
    the controls were rendered before they can be read as explicit offs.
    """
    if form.get(FORM_MARKER) != "1":
        return None
    base = promote_legacy_voice_config(existing)
    out: Dict[str, Any] = {
        k: v for k, v in base.items() if k not in _FIELD_BY_KEY
    }
    for field in VOICE_FIELDS:
        name = field.field_name
        if field.kind == "bool":
            out[field.key] = form.get(name) in ("on", "true", "1", True)
            continue
        if name not in form:
            _keep_prior(out, base, field.key)
            continue
        raw = form.get(name)
        raw = "" if raw is None else str(raw).strip()
        if not raw:
            continue  # blank clears -> dataclass default
        if field.kind in ("number",):
            try:
                out[field.key] = float(raw)
            except ValueError:
                logger.warning(
                    "agent_voice: %s must be a number — ignoring %r and keeping "
                    "the prior value", field.key, raw[:40],
                )
                _keep_prior(out, base, field.key)
            continue
        if field.kind == "int":
            try:
                out[field.key] = int(float(raw))
            except ValueError:
                logger.warning(
                    "agent_voice: %s must be an integer — ignoring %r and "
                    "keeping the prior value", field.key, raw[:40],
                )
                _keep_prior(out, base, field.key)
            continue
        if field.kind == "lines":
            items = [line.strip() for line in raw.replace(",", "\n").splitlines()
                     if line.strip()]
            if items:
                out[field.key] = items
            continue
        if field.kind == "variants":
            items = [line.strip() for line in raw.splitlines() if line.strip()]
            if len(items) == 1:
                out[field.key] = items[0]
            elif items:
                out[field.key] = items
            continue
        if field.kind == "json":
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                out[field.key] = parsed
            else:
                # Malformed operator JSON is IGNORED, never rejected and never
                # applied: a typo must not 500 the save, and it must not wipe a
                # previously-good map either. Same contract as the mapping
                # form's voice_by_language handling.
                logger.warning(
                    "agent_voice: %s must be a JSON object — ignoring invalid "
                    "input and keeping the prior value", field.key,
                )
                _keep_prior(out, base, field.key)
            continue
        # text / model
        out[field.key] = raw
    return out


def _keep_prior(out: Dict[str, Any], base: Dict[str, Any], key: str) -> None:
    """Restore `key` from the pre-save block after unparseable input.

    Without this an invalid number would fall through the "blank clears" path
    and reset the field to its dataclass default — a typo silently re-arming a
    lever an operator had deliberately set.
    """
    if key in base:
        out[key] = base[key]


# --------------------------------------------------------------------------- #
# Brain panel — the SPECIALIST row's own settings, on the cockpit
# --------------------------------------------------------------------------- #
def brain_update_from_form(
    form, existing_memory_config: Optional[dict] = None,
) -> Optional[Dict[str, Any]]:
    """The `agent_profiles` payload for the BRAIN row from the cockpit's
    brain form. This is the panel's whole point: every key here lands on the
    SPECIALIST (`voice_config.agent_profile_slug`), never on the owner —
    `model_main_id` and `memory_config` are read off the brain at request
    time, so writing them to the owner looks applied and does nothing.

    Returns ``None`` when the submission did not come from the brain form
    (no `_brain_form=1` marker) — same refuse-don't-guess posture as
    `voice_config_from_form`. The memory checkbox is owned by every marked
    submission (absent = unchecked = explicit False); `memory_config` is
    MERGED onto the row's existing dict so sibling keys (`retrieval`,
    `window`, …) survive the toggle. A blank model select is skipped, not a
    clear: a brain with no `model_main_id` cannot answer at all, and the
    cockpit offers no legitimate way to want that.
    """
    if form.get(BRAIN_FORM_MARKER) != "1":
        return None
    payload: Dict[str, Any] = {}
    model_id = str(form.get("brain_model_main_id") or "").strip()
    if model_id:
        payload["model_main_id"] = model_id
    memory = dict(existing_memory_config) if isinstance(existing_memory_config, dict) else {}
    memory["enabled"] = form.get("brain_memory_enabled") in ("on", "true", "1", True)
    payload["memory_config"] = memory
    return payload


def _load_chat_models(
    client, env: str, assigned_id: Optional[str],
) -> List[Dict[str, Any]]:
    """Chat-flagged `agent_models` rows for the brain's model picker.

    The exact inverse of `_load_realtime_models` below — `is_chat_model`
    excludes realtime rows so the brain cannot be pointed at an
    ears-and-mouth model (building one for text chat breaks every turn).
    Same assigned-row escape hatch: the select posts "" for a row it never
    rendered, and model_main_id must never be cleared by a rendering gap.
    """
    from admin._model_picker import is_chat_model

    cols = "model_id, name, provider, model_string, is_active, environment, extra_config"
    try:
        rows = [
            m for m in (
                client.table("agent_models").select(cols)
                .eq("is_active", True).eq("environment", env)
                .order("name").execute().data or []
            )
            if is_chat_model(m)
        ]
        if not rows:
            rows = [
                m for m in (
                    client.table("agent_models").select(cols)
                    .eq("is_active", True).order("name").execute().data or []
                )
                if is_chat_model(m)
            ]
        if assigned_id and not any(m.get("model_id") == assigned_id for m in rows):
            rows = (
                client.table("agent_models").select(cols)
                .eq("model_id", assigned_id).execute().data or []
            ) + rows
        return rows
    except Exception as exc:
        logger.warning("agent_voice: chat model lookup failed (%s)", exc)
        return []


def collect_brain_panel(
    client, env: str, brain_row: Dict[str, Any], *, is_specialist: bool,
) -> Dict[str, Any]:
    """Everything the cockpit's brain panel renders about the SPECIALIST row.

    The bound-tool list is resolved the way the RUNTIME resolves it — from
    the `agent_profile_mcp_servers` attachments narrowed by each attachment's
    `tool_subset` — **not** from `agent_profiles.tool_ids`, which the
    tool-resolution path reads nowhere. A `tool_ids` entry that does not bind
    is reported as exactly that (`listed_not_bound`), because the checkbox
    list on the Agents screen looks authoritative and is not.

    The listing COMPUTATION is not this module's: it is
    `admin/screens/agents.py::build_bound_tools_panel` — the same helper
    behind the Agents screen's "Bound at runtime" panel, itself built on
    `bound_tool_names_from_attachments` (the predictor promote-time
    validation uses). One computation, two screens, so the cockpit's
    "listed but not bound" can never disagree with the Tools card's
    "Not bound". This function keeps only what is cockpit-specific: WHICH
    row is the brain (the specialist resolution), and the flat presentation
    shape its template renders.

    Every lookup degrades to an empty value (logged) rather than 500-ing the
    cockpit — same posture as the rest of this module.
    """
    from admin.screens.agents import build_bound_tools_panel
    from core.agent_profile import _fetch_mcp_attachments

    profile_id = brain_row.get("profile_id")
    memory_config = brain_row.get("memory_config")
    if not isinstance(memory_config, dict):
        memory_config = {}

    # --- served system prompt version (read-only) -------------------------- #
    system_prompt: Optional[Dict[str, Any]] = None
    pinned_id = (brain_row.get("prompt_version_ids") or {}).get("system") \
        if isinstance(brain_row.get("prompt_version_ids"), dict) else None
    if pinned_id:
        try:
            rows = (
                client.table("agent_prompt_versions")
                .select("prompt_version_id, version_number, created_at, notes")
                .eq("prompt_version_id", pinned_id).limit(1).execute().data or []
            )
            if rows:
                system_prompt = {
                    "version_number": rows[0].get("version_number"),
                    "created_at": rows[0].get("created_at"),
                    "pinned": True,
                }
            else:
                system_prompt = {"version_number": None, "created_at": None,
                                 "pinned": True, "dangling": True}
        except Exception as exc:
            logger.warning("agent_voice: brain prompt pin lookup failed (%s)", exc)

    # --- bound tools: the SHARED computation -------------------------------- #
    # `build_bound_tools_panel` owns both halves — the bound set (via the
    # runtime's own subset predicate) and the tool_ids-not-bound remainder —
    # so this panel and the Agents screen's "Bound at runtime" panel cannot
    # drift apart. This function only fetches the brain row's inputs and
    # flattens the result for the cockpit's chip rendering.
    attachments: List[Dict[str, Any]] = []
    try:
        attachments = _fetch_mcp_attachments(client, profile_id) if profile_id else []
    except Exception as exc:
        logger.warning("agent_voice: brain attachment lookup failed (%s)", exc)
    tool_ids = brain_row.get("tool_ids") or []
    tool_rows: List[Dict[str, Any]] = []
    if tool_ids:
        try:
            tool_rows = (
                client.table("agent_tools").select("tool_id, python_name")
                .in_("tool_id", tool_ids).execute().data or []
            )
        except Exception as exc:
            logger.warning("agent_voice: brain tool_ids lookup failed (%s)", exc)
    panel = build_bound_tools_panel(attachments, tool_ids, tool_rows)
    bound = sorted(
        t["python_name"] for g in panel["groups"] for t in g["tools"]
    )
    external_servers = [
        {"name": e.get("name"), "endpoint_only": bool(e.get("endpoint_only")),
         "tool_subset": e.get("subset") or []}
        for e in panel["external"]
    ]
    # The helper reports an unresolvable tool_id as a truncated-id
    # placeholder; keep it — a dangling id in tool_ids is still a row that
    # looks attached and binds nothing.
    listed_not_bound = sorted(panel["legacy_not_bound"])

    return {
        "row": brain_row,
        "is_specialist": is_specialist,
        "chat_models": _load_chat_models(client, env, brain_row.get("model_main_id")),
        "memory_enabled": bool(memory_config.get("enabled")),
        "memory_config": memory_config,
        "system_prompt": system_prompt,
        "bound_tools": bound,
        "external_servers": external_servers,
        "listed_not_bound": listed_not_bound,
        "tool_ids_count": len(tool_ids),
    }


# --------------------------------------------------------------------------- #
# Screen context
# --------------------------------------------------------------------------- #
def resolve_voice_pair(
    client, environment: str, profile_row: Dict[str, Any],
) -> Dict[str, Any]:
    """The parent/specialist pair for `profile_row`, resolved from the DB.

    D10: the specialist relationship is the page's STRUCTURE, not a conditional
    notice. `admin/screens/agents.py::resolve_voice_config_owner` scans the
    Agents rail's already-filtered profile list, so an env or scoped-build filter
    that hides the owning parent also hides the notice — and the specialist's
    blank voice card then looks authoritative. This lookup goes to the database
    by slug instead, so no filter state can make the relationship vanish.

    Returns ``{owner, specialist, viewing}``:
      owner       the row that STORES voice_config (may be `profile_row`)
      specialist  the row that supplies the brain (may be None, or == owner)
      viewing     "owner" | "specialist"
    """
    from runtime.agents.voice_registry import voice_agent_slug_addressed

    slug = (profile_row.get("slug") or "").strip()
    kind = profile_row.get("agent_kind")
    own_block = promote_legacy_voice_config(_safe_voice_config(profile_row))
    # A slug-addressed kind (generic agents): every agent has its own address
    # and so owns its own voice settings, even when another agent of the kind
    # borrows it as a brain.
    self_owned = voice_agent_slug_addressed(str(kind or ""))

    siblings: List[Dict[str, Any]] = []
    try:
        siblings = (
            client.table("agent_profiles")
            .select("profile_id, slug, display_name, agent_kind, status, "
                    "voice_config, prompt_version_ids, served_version_id, "
                    "environment, model_main_id")
            .eq("environment", environment).eq("agent_kind", kind)
            .execute().data or []
        )
    except Exception as exc:
        logger.warning("agent_voice: sibling lookup failed (%s)", exc)

    # Is another row in this kind serving its calls THROUGH this one?
    owner_row: Optional[Dict[str, Any]] = None
    if slug and not self_owned:
        for sib in siblings:
            if sib.get("profile_id") == profile_row.get("profile_id"):
                continue
            sib_block = promote_legacy_voice_config(_safe_voice_config(sib))
            if sib_block.get("agent_profile_slug") == slug:
                owner_row = sib
                break

    if owner_row is not None:
        owner = owner_row
        specialist = profile_row
        viewing = "specialist"
    else:
        owner = profile_row
        served_slug = (own_block.get("agent_profile_slug") or "").strip()
        specialist = None
        if served_slug and served_slug != slug:
            specialist = next(
                (s for s in siblings if s.get("slug") == served_slug), None,
            )
        viewing = "owner"
    return {"owner": owner, "specialist": specialist, "viewing": viewing}


def _safe_voice_config(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """`row.voice_config` when it is a dict, else None.

    A hand-edited row can hold a bare string in that column; every reader raises
    on it, and this screen is where an operator would repair it — so render an
    empty block rather than 500 the one page that can fix the row.
    """
    stored = (row or {}).get("voice_config")
    return stored if isinstance(stored, dict) else None


def load_voice_context(env: str, profile_id: str) -> Optional[Dict[str, Any]]:
    """Everything `agent_voice.html` renders. None when the profile is gone."""
    from admin.router import client_for

    client = client_for(env)
    rows = (
        client.table("agent_profiles").select("*")
        .eq("profile_id", profile_id).limit(1).execute().data or []
    )
    if not rows:
        return None
    profile = rows[0]
    pair = resolve_voice_pair(client, env, profile)
    owner = pair["owner"]
    block = promote_legacy_voice_config(_safe_voice_config(owner))

    realtime_models = _load_realtime_models(client, env, block.get("model_id"))
    model_row = next(
        (m for m in realtime_models if m.get("model_id") == block.get("model_id")),
        None,
    )
    caps = provider_capabilities((model_row or {}).get("provider"))

    persona_versions = None
    try:
        from admin._persona_versions import collect_persona_version_status

        persona_versions = collect_persona_version_status(
            client, environment=env,
            parent_row=owner, specialist_row=pair["specialist"],
        )
    except Exception as exc:
        logger.warning("agent_voice: persona version status failed (%s)", exc)

    # The BRAIN: the specialist when the owner names one, else the owner
    # itself (a blank/unresolvable `agent_profile_slug` falls back to the
    # kind's active profile at connect time, and the panel mirrors that).
    brain_row = pair["specialist"] or owner
    brain = collect_brain_panel(
        client, env, brain_row, is_specialist=pair["specialist"] is not None,
    )

    # The persona TEXT, editable here. The save posts through the same
    # version-create path the Agents screen uses, with the hidden profile_id
    # ALWAYS the owner's — the runtime reads the persona pin off the PARENT
    # (endpoints/voice_live.py::resolve_persona_prompt), and a specialist-side
    # pin is inert and warns. The cockpit always knows the owner, so unlike
    # the Agents screen it never needs a read-only fallback.
    persona_prompt = None
    persona_slug = str(block.get("persona_prompt_slug") or "").strip()
    if persona_slug:
        try:
            prow = (
                client.table("agent_prompts").select("prompt_id, slug")
                .eq("agent_kind", owner.get("agent_kind"))
                .eq("slug", persona_slug).limit(1).execute().data or []
            )
            if prow:
                versions = (
                    client.table("agent_prompt_versions")
                    .select("content, version_number")
                    .eq("prompt_id", prow[0]["prompt_id"])
                    .order("version_number", desc=True).limit(1).execute().data or []
                )
                persona_prompt = {
                    "prompt_id": prow[0]["prompt_id"],
                    "slug": persona_slug,
                    "content": (versions[0].get("content") if versions else "") or "",
                    "latest_version_number": (
                        versions[0].get("version_number") if versions else None
                    ),
                }
        except Exception as exc:
            logger.warning("agent_voice: persona prompt lookup failed (%s)", exc)

    return {
        "profile": profile,
        "owner": owner,
        "specialist": pair["specialist"],
        "viewing": pair["viewing"],
        "voice_block": block,
        "values": render_values(block),
        "defaults": field_defaults(),
        "notes": describe_effective_overrides(
            block, model_row=model_row, capabilities=caps,
            agent_kind=owner.get("agent_kind")),
        "generic_voice": generic_voice_panel(owner, block, model_row),
        "sections": VOICE_SECTIONS,
        "fields_by_section": {
            sid: fields_for_section(sid) for sid, _t, _b in VOICE_SECTIONS
        },
        "realtime_models": realtime_models,
        "model_row": model_row,
        "capabilities": caps,
        "persona_versions": persona_versions,
        "persona_prompt": persona_prompt,
        "brain": brain,
        "field_prefix": FIELD_PREFIX,
        "form_marker": FORM_MARKER,
        "brain_form_marker": BRAIN_FORM_MARKER,
        # Derived, never hand-counted: the "N settings" copy drifted twice
        # (38 in the docs while the registry held 40) before it was derived.
        "voice_key_count": len(_VOICE_FIELDS),
    }


def generic_voice_panel(
    owner: Dict[str, Any], block: Dict[str, Any], model_row: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """What the page shows for an agent called at its own address (a generic
    agent): where to call it, whether a call can start on THIS server, and
    the id its calls are metered under. None for every other kind.

    `problem` is the reason a call would be refused right now, in the same
    words the socket closes with, so a missing realtime key reads as "set
    this key" here instead of as a failed call.
    """
    from runtime.agents.voice_registry import voice_agent_slug_addressed

    kind = str(owner.get("agent_kind") or "")
    slug = str(owner.get("slug") or "")
    if not voice_agent_slug_addressed(kind) or not slug:
        return None
    from endpoints.agents_v1 import generic_agents_enabled
    from endpoints.voice_live import agent_budget_id
    from voice.live.readiness import realtime_problem

    enabled = bool(block.get("enabled"))
    api_on = generic_agents_enabled()
    live = bool(owner.get("is_active")) and owner.get("status") in ("active", "specialist")
    if not api_on:
        problem = ("The generic agents API is switched off on this server "
                   "(ENABLE_GENERIC_AGENTS), so this agent has no address to call.")
    elif not enabled:
        problem = "Voice is not turned on for this agent."
    elif not model_row:
        problem = "No realtime voice model is set for this agent."
    else:
        problem = realtime_problem(model_row.get("provider"), model_row.get("extra_config"))
    return {
        "path": f"/v1/agents/{slug}/voice",
        "slug": slug,
        "profile_id": owner.get("profile_id"),
        "problem": problem,
        "can_call": problem is None,
        # A draft is reached by pinning it; say so rather than let an API
        # caller wonder why the address refuses.
        "published": live,
        "budget_id": agent_budget_id(kind, slug),
    }


def _load_realtime_models(
    client, env: str, assigned_id: Optional[str],
) -> List[Dict[str, Any]]:
    """Realtime-flagged `agent_models` rows for the picker.

    The only loader of its kind since the Agents mapping form's fieldset (and
    its `admin/screens/agents.py::_load_realtime_models` twin) was deleted
    on 2026-08-26. Its load-bearing escape hatch: the ASSIGNED row must always
    be present even when inactive or tagged for another env, because the
    select posts "" for a row it never rendered — and that CLEARS `model_id`
    on the next save. `_load_chat_models` above is its inverse-gate sibling.
    """
    from admin._model_picker import is_realtime_model

    cols = ("model_id, name, provider, model_string, is_active, environment, "
            "extra_config")
    try:
        rows = [
            m for m in (
                client.table("agent_models").select(cols)
                .eq("is_active", True).eq("environment", env)
                .order("name").execute().data or []
            )
            if is_realtime_model(m)
        ]
        if not rows:
            rows = [
                m for m in (
                    client.table("agent_models").select(cols)
                    .eq("is_active", True).order("name").execute().data or []
                )
                if is_realtime_model(m)
            ]
        if assigned_id and not any(m.get("model_id") == assigned_id for m in rows):
            rows = (
                client.table("agent_models").select(cols)
                .eq("model_id", assigned_id).execute().data or []
            ) + rows
        return rows
    except Exception as exc:
        logger.warning("agent_voice: realtime model lookup failed (%s)", exc)
        return []
