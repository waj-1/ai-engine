"""The /voice/live wire protocol — one definition for server and browser.

Client -> server:
  binary frames  : raw PCM at `ready.audio.input_sample_rate`
  {"type":"mute"}/{"type":"unmute"} : pause/resume capture (advisory)
  {"type":"played","ms":N} : playback reached N ms into the received audio
      stream — sent on every playback flush. N is a STREAM OFFSET (audio a
      previous flush discarded stays counted), which is what lets the server
      map it onto its sent-audio ledger and truncate the conversation item
      the caller was actually hearing (`RealtimeSession.report_playback`).
      Best-effort and additive: old clients never send it, old servers
      ignore it.

Server -> client:
  binary frames  : raw PCM at `ready.audio.output_sample_rate`
  ready | transcript | display | chart | interrupted | error  (JSON, below)

`chart` is forwarded verbatim from the agent's own event so the portals'
existing `case "chart"` branch keeps working untouched.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from voice.live.base import AudioFormat

# Application close codes (RFC 6455 reserves 4000-4999 for applications).
CLOSE_FORBIDDEN = 4403        # voice disabled for the tenant, or channel forbids the kind
CLOSE_RATE_LIMITED = 4429     # per-minute limiter, concurrency cap, or daily budget
CLOSE_PROVIDER_ERROR = 4502   # upstream realtime provider failed


def ready_frame(audio_format: AudioFormat, session_id: str, *,
                greeting: Optional[bool] = None) -> Dict[str, Any]:
    frame: Dict[str, Any] = {
        "type": "ready",
        "session_id": session_id,
        "audio": {
            "input_sample_rate": audio_format.input_sample_rate,
            "output_sample_rate": audio_format.output_sample_rate,
            "encoding": audio_format.encoding,
        },
    }
    if greeting is not None:
        # Will this call open with a greeting (voice_config.greeting_trigger)?
        # Additive: lets a client know up front that the first response is
        # the greeting.
        frame["greeting"] = bool(greeting)
    return frame


def transcript_frame(
    role: str, text: str, *, final: bool, out_of_band: bool = False, greeting: bool = False,
    reply: Optional[bool] = None,
) -> Dict[str, Any]:
    """`out_of_band` marks assistant speech that is NOT the answer to the
    caller's utterance — a holding line ("still working on that") or any
    other filler spoken outside the conversation proper. Additive and
    optional: the key is present only when True, so an absent key means a
    normal in-conversation transcript and old clients (which ignore unknown
    fields) are unaffected. The voice eval driver keys on it to measure
    answer latency instead of clocking the holding line as the reply."""
    frame: Dict[str, Any] = {
        "type": "transcript", "role": role, "text": text, "final": final,
    }
    if out_of_band:
        frame["out_of_band"] = True
    if greeting:
        # The call's opening greeting, marked WHILE it streams (the same
        # response whose turn_end carries `greeting: true`) so a client can
        # keep it out of the chat history without waiting for turn_end.
        # Additive; present only when true.
        frame["greeting"] = True
    if reply is not None:
        # The response's `reply` verdict (same meaning as turn_end.reply) on
        # its FIRST assistant caption, from providers that decide it at
        # response start (azure_voice_live). Absent when unknown.
        frame["reply"] = bool(reply)
    return frame


def display_frame(text: str) -> Dict[str, Any]:
    """The agent's verbatim answer — authoritative, unlike the spoken audio."""
    return {"type": "display", "text": text}


def agent_status_frame(text: str) -> Dict[str, Any]:
    """A delegate progress line — the agent's own `thought` events ("Using
    tool: …", "Retrieved data from …") forwarded while the caller waits, so
    a client can show live thinking/tool activity. Informational only, never
    spoken, and additive: clients that don't know the type ignore it."""
    return {"type": "agent_status", "text": text}


def chart_frame(agent_event: Dict[str, Any]) -> Dict[str, Any]:
    return agent_event


def links_frame(agent_event: Dict[str, Any]) -> Dict[str, Any]:
    """Downloadable files (a CSV export), forwarded verbatim from the agent's
    `links` event — the SAME shape text chat streams over SSE, so the app
    parses it with its existing DownloadLink model:
    `{"type": "links", "content": <label>, "files": [{"url", "filename", "rows"}]}`.
    Never spoken (the URL is never read aloud); additive — clients that do
    not know the type ignore it."""
    return agent_event


def proposal_frame(agent_event: Dict[str, Any]) -> Dict[str, Any]:
    """An agent's proposal, forwarded verbatim from its own event so the
    client's existing `proposal` handling (the same frame its HTTP chat
    sends) keeps working. Proposals are validated suggestions for the user to
    approve ON SCREEN — the voice layer never executes them."""
    return agent_event


def interrupted_frame(reason: str | None = None) -> Dict[str, Any]:
    """Flush the client's playback queue, tagged with WHY.

    The client-side action is identical for every reason (drop queued audio),
    but the reasons narrate very different moments and UIs render them
    differently:

      barge_in        — the caller took the floor; a confirmed cut.
      input_end       — ordinary turn-taking: the caller finished speaking
                        and the queue is cleared before the reply's audio.
      speech_detected — the provider heard a speech onset that was never
                        confirmed as real speech (may be background noise).
      answer_ready    — filler audio is flushed so a delegated tool answer
                        can play; not an interruption at all.

    `reason` is additive: old clients ignore unknown fields, and omitting it
    (older callers) keeps the legacy frame byte-identical."""
    frame: Dict[str, Any] = {"type": "interrupted"}
    if reason is not None:
        frame["reason"] = reason
    return frame


def turn_end_frame(reply: Optional[bool] = None, *, greeting: bool = False) -> Dict[str, Any]:
    """One conversational turn finished — close any open transcript bubble.

    This is the authoritative end-of-turn signal, and the client must not rely
    on `transcript.final` instead. Verified against the live Gemini API: it
    never populates `Transcription.finished`, so every transcript fragment
    arrives non-final. A client that only closes its bubble on `final` would
    accumulate every utterance of the session into one bubble forever.

    `reply` (additive, optional) says whether the provider response that just
    ended ANSWERS a new caller utterance (True) or is a continuation /
    endpoint-driven response with no new caller speech behind it — the
    greeting, the answer after an announce line, a holding line, a stall
    recovery (False). It exists for completed-only STT (azure-speech /
    whisper-1 transcribe the caller only after their utterance is complete),
    where the caller's transcript can arrive ENTIRELY after this frame: the
    client cannot tell "the reply to speech whose transcript is still coming"
    from "a follow-up to the previous turn" without it. None (the provider
    cannot tell) omits the key, keeping the legacy frame byte-identical.

    `greeting` (additive, present only when true) marks the turn_end that
    closes the call's opening greeting. The engine does not persist the
    greeting; the app committed it into the chat as an assistant bubble, one
    more per call (5 calls → 5 greetings, 2026-09-28). A client that keeps a
    chat transcript drops the assistant text of the turn this frame closes.
    Always sent with `reply: false`.
    """
    frame: Dict[str, Any] = {"type": "turn_end"}
    if reply is not None:
        frame["reply"] = reply
    if greeting:
        frame["greeting"] = True
    return frame


def error_frame(message: str) -> Dict[str, Any]:
    return {"type": "error", "message": message}
