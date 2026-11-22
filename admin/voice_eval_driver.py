"""Goal-driven LLM caller for voice eval scenarios (spec §2).

This module is the "agent that talks to the voice agent": given a scenario's
caller block (persona/goal/facts/max_turns), it converses over a real
WebSocket to /voice/live — via the simulated provider (free) or the real one
(billable, Task 6) — and returns a transcript record for judging.

Pure functions first (prompt build, reply parse, rendering) so the caller
brain is unit-testable without a model; the async loop lives below them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

HANGUP_TOKEN = "[HANGUP]"

_CALLER_SYSTEM_TEMPLATE = """You are role-playing a CALLER on a phone call \
with a voice assistant. You are NOT the assistant.

Your character: {persona}

Your goal for this call: {goal}

Facts you know (use them when asked; NEVER invent facts you were not given):
{facts}

Rules:
- Reply with ONLY the caller's next spoken utterance — one or two short,
  natural spoken sentences. No quotes, no stage directions, no "CALLER:"
  label, no markdown.
- Speak the language your character would speak.
- Answer the assistant's questions from your facts. If asked for something
  not in your facts, say you don't have it.
- When your goal is fully achieved, or clearly cannot be achieved, reply
  with exactly {hangup} (optionally after one short closing sentence).
- You have {turns_left} utterances left; wrap up before they run out."""


def build_caller_prompt(
    caller: Dict[str, Any], transcript: List[Dict[str, str]], turns_left: int
) -> List[Any]:
    from langchain_core.messages import HumanMessage, SystemMessage

    facts = caller.get("facts") or {}
    facts_block = (
        "\n".join(f"- {k}: {v}" for k, v in facts.items()) if facts else "- (none)"
    )
    system = _CALLER_SYSTEM_TEMPLATE.format(
        persona=str(caller.get("persona") or "an ordinary customer"),
        goal=str(caller.get("goal") or ""),
        facts=facts_block,
        hangup=HANGUP_TOKEN,
        turns_left=turns_left,
    )
    if transcript:
        history = render_transcript(transcript)
        human = (
            "The call so far:\n" + history + "\n\nYour next utterance:"
        )
    else:
        human = (
            "No one has spoken yet — start the call with your opening "
            "utterance."
        )
    return [SystemMessage(content=system), HumanMessage(content=human)]


def parse_caller_reply(text: str) -> Tuple[Optional[str], bool]:
    """(utterance, hangup). Empty/blank replies count as hangup — a caller
    LLM that returns nothing can't keep the call alive."""
    raw = (text or "").strip()
    if not raw:
        return None, True
    hangup = bool(re.search(re.escape(HANGUP_TOKEN), raw, re.IGNORECASE))
    utterance = re.sub(
        re.escape(HANGUP_TOKEN), "", raw, flags=re.IGNORECASE
    ).strip()
    # Strip a wrapping quote pair and a leading role label the model may add.
    # Include both straight quotes (" and ') and curly smart quotes (U+201C and U+201D)
    open_quotes = ('"', "'", "\u201c")
    close_quotes = ('"', "'", "\u201d")
    if len(utterance) >= 2 and utterance[0] in open_quotes and utterance[-1] in close_quotes:
        utterance = utterance[1:-1].strip()
    utterance = re.sub(r"^(CALLER|USER)\s*:\s*", "", utterance, flags=re.IGNORECASE)
    if not utterance:
        return None, True
    return utterance, hangup


def render_transcript(turns: List[Dict[str, str]]) -> str:
    lines = []
    for t in turns:
        label = "CALLER" if t.get("role") == "caller" else "ASSISTANT"
        lines.append(f"{label}: {t.get('text', '')}")
    return "\n".join(lines)


# ── The conversation loop ───────────────────────────────────────────────
#
# `run_voice_eval_scenario` is the "agent that talks to the voice agent": an
# aiohttp WS *client* that connects to a real /voice/live edge (in-process
# loopback by default, an override `target` in tests), drives the caller
# brain above turn by turn, and folds every failure mode into the returned
# status dict rather than raising — a broken scenario must never take down a
# batch eval run.

logger = logging.getLogger(__name__)

_TURN_DEADLINE_SIM_SEC = 30.0
_TURN_DEADLINE_LIVE_SEC = 60.0
_GREETING_WAIT_SEC = 15.0
# /voice/live admits 10 connects per minute per IP (`_connect_limiter`); a
# suite of short scripted scenarios over loopback exceeded it and the refused
# calls read as "no ready frame" failures (a voice suite, 2026-09-29).
# A suite paces its connects with `default_context.voice.connect_gap_sec`
# (6.5 stays under it).
_last_connect = {"t": 0.0}
_POLL_SEC = 0.1
_DEFAULT_MAX_TURNS = 10
_MAX_TURNS_CAP = 20


async def _stream_speech(ws, text: str, locale: Optional[str]) -> None:
    """Live mode: synthesize the caller utterance and stream it at real-time
    pace — same TTS + pacing as admin/voice_scenario_runner's `say` step
    (imported, not copied).

    Limitation: the recorded caller turn (see `_drive`'s
    `turns.append({"role": "caller", ...})`) is the caller LLM's INTENT — the
    text handed to `_synthesize` here — not an ASR echo of what the voice
    edge actually heard. Capturing the real ASR transcript back is out of
    scope for this task.
    """
    from admin.voice_scenario_runner import (
        _CHUNK_BYTES, _CHUNK_SEC, _DEFAULT_SILENCE_SEC, _SAMPLE_RATE,
        _synthesize, _voice_for,
    )

    step = {"language": locale}
    voice = _voice_for(step, locale)
    pcm = await _synthesize(text, voice=voice, language=locale)
    silence = b"\x00" * int(_SAMPLE_RATE * 2 * _DEFAULT_SILENCE_SEC)
    payload = pcm + silence
    for off in range(0, len(payload), _CHUNK_BYTES):
        chunk = payload[off:off + _CHUNK_BYTES]
        if chunk:
            await ws.send_bytes(chunk)
        await asyncio.sleep(_CHUNK_SEC)


def _normalize_llm_content(raw: Any) -> str:
    """`.content` off a LangChain chat-model reply, reduced to plain text.

    Most providers return a plain string. Some (Claude-style) return a list
    of content blocks (`[{"type": "text", "text": "..."}, ...]`) — join
    those into one string so `parse_caller_reply` never sees a Python
    `repr()` of a list. Used on BOTH the primary and the retry attempt (see
    `run_voice_eval_scenario`'s caller-LLM call): a reply shape that needs
    normalizing on the happy path needs it just as much after a retry.
    """
    text = getattr(raw, "content", raw)
    if isinstance(text, list):  # LangChain content blocks
        text = " ".join(
            str(b.get("text", b)) if isinstance(b, dict) else str(b)
            for b in text
        )
    return str(text)


def collect_assistant_turns(frames: List[Dict[str, Any]]) -> List[str]:
    """Reduce a run of assistant `transcript` frames into complete turns.

    Real providers (azure_voice_live, gemini_live) stream the assistant's
    reply as a run of `final=False` text deltas followed by an empty
    `final=True` closer (see `voice/live/protocol.py`'s `turn_end_frame`
    docstring: Gemini never populates `final` on its own, so the endpoint
    is the one stamping the closer). The simulated provider instead emits
    exactly one `final=True` frame per utterance
    (`voice/live/simulated.py`). Treating every frame as its own turn (the
    old behavior) is correct for sim but shreds a live transcript into
    word-fragment "turns" that feed the caller LLM, the judge, and
    substring checks.

    Accumulate consecutive assistant-frame text (space-joined, stripped)
    and flush ONE combined turn when a `final=True` assistant frame, or a
    `turn_end` frame, closes the run. Non-assistant/non-turn_end frames
    (audio, display, chart, user transcripts, ...) are ignored, matching
    the caller's existing assistant-only collection.

    For sim (each frame already final=True) this degenerates to exactly
    the old per-frame behavior: one turn per frame.
    """
    turns: List[str] = []
    buf: List[str] = []

    def _flush():
        joined = " ".join(s for s in buf if s).strip()
        if joined:
            turns.append(joined)
        buf.clear()

    for f in frames:
        if f.get("type") == "transcript" and f.get("role") == "assistant":
            text = (f.get("text") or "").strip()
            if text:
                buf.append(text)
            if f.get("final"):
                _flush()
        elif f.get("type") == "turn_end":
            _flush()
    _flush()
    return turns


def collect_proposals(frames: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Payloads of every `proposal` frame, in arrival order.

    Some agents surface dictated orders as
    `{"type": "proposal", "proposal": {...}}` frames (forwarded verbatim from
    the agent's extract_proposals events). They are the mechanism under test
    for those agents' suites — spoken text alone can claim "queued for your
    review" while no proposal ever left the engine — so the driver records
    them for the runner's `expected_proposals` gate and the judge transcript.
    A malformed frame with no payload is skipped, not raised."""
    return [
        f["proposal"] for f in frames
        if f.get("type") == "proposal" and isinstance(f.get("proposal"), dict)
    ]


def _reply_marks(
    frames: List[Dict[str, Any]], mark: int
) -> Tuple[Optional[float], Optional[float], bool]:
    """(first_reply_t, answer_t, holding_line) for the turn after `mark`.

    Frames carry a `_t` (time.monotonic() at receive) set by `_receiver`.
    `first_reply_t` is the receive-timestamp of the first assistant
    transcript of ANY kind — the pre-2026-08-06 "latency", which on tool
    turns is usually the out-of-band holding line the endpoint speaks at
    `progress_after_sec`, so it pegs there no matter how slow the real
    answer is. `answer_t` is the first assistant transcript that is NOT
    marked `out_of_band` (see voice/live/protocol.transcript_frame) — the
    actual answer. `holding_line` is True when an out-of-band line preceded
    the answer (or arrived and no answer ever did)."""
    first_t: Optional[float] = None
    answer_t: Optional[float] = None
    holding = False
    for f in frames[mark:]:
        if f.get("type") == "transcript" and f.get("role") == "assistant":
            if first_t is None:
                first_t = f.get("_t")
            if f.get("out_of_band"):
                if answer_t is None:
                    holding = True
            elif answer_t is None:
                answer_t = f.get("_t")
    return first_t, answer_t, holding


async def run_voice_eval_scenario(
    scenario: Dict[str, Any],
    voice_ctx: Dict[str, Any],
    *,
    caller_llm: Any,
    target: Optional[str] = None,
    timeout_sec: float = 240.0,
) -> Dict[str, Any]:
    """Drive one suite scenario's caller over a real /voice/live WS session.

    Never raises — every failure (unreachable target, no ready frame, a
    stuck turn, a caller-LLM exception, the scenario's own timeout) is
    folded into the returned status dict. See the module docstring/Task 4
    brief for the exact return-dict contract; Task 5 maps it onto
    `ai_voice_scenario_runs`.
    """
    from admin.voice_scenario_runner import default_target

    caller = dict(scenario.get("caller") or {})
    try:
        max_turns = int(caller.get("max_turns") or _DEFAULT_MAX_TURNS)
    except (TypeError, ValueError):
        # A malformed scenario (e.g. "max_turns": "five") must not raise out
        # of a "never raises" function — fall back to the default rather
        # than aborting the whole eval run over one bad field.
        max_turns = _DEFAULT_MAX_TURNS
    max_turns = min(_MAX_TURNS_CAP, max_turns)
    mode = str(voice_ctx.get("mode") or "sim")
    ws_path = str(voice_ctx.get("ws_path") or "/voice/live")
    locale = scenario.get("locale") or voice_ctx.get("locale")

    url = (target or default_target(ws_path)).rstrip("/")
    if target:
        url = f"{url}{ws_path}"
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}agentKind={voice_ctx.get('agent_kind', '')}"
    if voice_ctx.get("business_id"):
        url += f"&businessId={voice_ctx['business_id']}"
    # A demo agent plays a case (endpoints/voice_live.py `callCase`); the
    # scenario's own case wins over the suite's default.
    call_case = scenario.get("case_id") or voice_ctx.get("call_case")
    if call_case:
        url += f"&callCase={call_case}"
    if locale:
        url += f"&locale={locale}"
    if mode == "sim":
        url += "&provider=simulated"
    else:
        # A live-mode eval run uses a REAL provider, so nothing about the
        # connection says "bot caller". Self-declare it: the endpoint stamps
        # `waj.voice.simulated` on the call's spans + recording so these
        # sessions carry a "sim" chip in the observability screens instead of
        # blending in with real callers. (Sim mode needs no flag — the
        # simulated provider itself implies it.)
        url += "&simRun=1"

    if mode == "live" and not os.getenv("AZURE_SPEECH_KEY"):
        return {"status": "skipped",
                "reason": "AZURE_SPEECH_KEY is unset — cannot synthesize caller speech",
                "turns": [], "per_turn": [], "audio_bytes_received": 0,
                "proposals": [], "termination": "skipped", "target": url}

    import aiohttp

    try:
        gap = float(voice_ctx.get("connect_gap_sec") or 0)
    except (TypeError, ValueError):
        gap = 0.0
    wait = _last_connect["t"] + gap - time.monotonic()
    if gap > 0 and wait > 0:
        await asyncio.sleep(wait)
    _last_connect["t"] = time.monotonic()

    frames: list = []
    audio_bytes = {"n": 0}
    turns: list = []
    per_turn: list = []
    termination = "timeout"
    reason = None
    turn_deadline = _TURN_DEADLINE_LIVE_SEC if mode == "live" else _TURN_DEADLINE_SIM_SEC

    # A faithful playback model — the same three numbers `static/shared/
    # voice-live.js` keeps (`_scheduledMs`, `_playAt`, and the `played` frame
    # it reports on every flush). Without it this driver is not the client the
    # engine is written against: it swallows audio at wire speed, never
    # flushes, and never reports playback, so the entire
    # flush -> `played` -> `conversation.item.truncate` path — where the
    # 2026-08-06 mid-word truncation lived — cannot be reached from an eval at
    # all. `discarded_ms` is what a flush threw away unheard: the mechanical
    # measure of "the audio did NOT play to completion".
    play = {"scheduled_ms": 0.0, "play_at": 0.0, "discarded_ms": 0.0,
            "rate": 24000.0}

    async def _receiver(ws):
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                audio_bytes["n"] += len(msg.data)
                now = time.monotonic()
                dur = len(msg.data) / 2.0 / play["rate"]
                play["scheduled_ms"] += dur * 1000.0
                play["play_at"] = max(play["play_at"], now) + dur
            elif msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    parsed = json.loads(msg.data)
                    frame = (
                        parsed if isinstance(parsed, dict)
                        else {"type": "unparsed", "raw": msg.data[:200]}
                    )
                except Exception:
                    frame = {"type": "unparsed", "raw": msg.data[:200]}
                frame["_t"] = time.monotonic()
                frames.append(frame)
                if frame.get("type") == "ready":
                    rate = ((frame.get("audio") or {}).get("output_sample_rate"))
                    try:
                        play["rate"] = float(rate) or 24000.0
                    except (TypeError, ValueError):
                        pass
                elif frame.get("type") == "interrupted":
                    # The browser drops its whole queue here and tells the
                    # server how far it actually got, so the model's context
                    # can be repaired to what was HEARD. Mirrored exactly:
                    # `played` is the offset into the RECEIVED stream, not a
                    # sum of heard ms — audio a previous flush discarded stays
                    # counted, which is what keeps it aligned with the
                    # server-side ledger across multiple flushes.
                    now = time.monotonic()
                    unplayed = max(0.0, play["play_at"] - now)
                    play["discarded_ms"] += unplayed * 1000.0
                    played = max(0.0, play["scheduled_ms"] - unplayed * 1000.0)
                    play["play_at"] = now
                    try:
                        await ws.send_str(json.dumps(
                            {"type": "played", "ms": int(round(played))}))
                    except Exception:  # a closing socket is not a fault
                        pass
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                return

    def _collect_transcripts(mark):
        # Assistant-only: the caller's own turn is always appended at the
        # send site below from the text we actually sent, not read back off
        # the wire. The simulated (and a real ASR) provider echoes the
        # caller's utterance as its own `role="user"` transcript frame, so
        # also collecting non-assistant frames here would double-count every
        # caller turn against the one already recorded manually.
        #
        # `collect_assistant_turns` also folds streamed deltas (live-mode
        # providers) into whole turns rather than one fragment per frame —
        # see its docstring.
        for text in collect_assistant_turns(frames[mark:]):
            turns.append({"role": "assistant", "text": text})

    def _turn_record(turn_no, sent_at, marks, got_end, snap, turns_before, marks_from=0):
        """One per-turn metrics row. `audio_ms` is what the engine sent for
        this turn; `audio_discarded_ms` is how much of it a playback flush
        threw away before it could be heard. `text` is the assistant's reply
        for the turn, so a limit can assert the utterance is COMPLETE — the
        only wire-visible signature of a reply the service stopped generating
        (its audio and its transcript stop together, so no audio/text ratio
        can see it).

        Two latencies, deliberately: `first_reply_ms` (with `latency_ms` as
        its legacy alias — kept so existing `max_latency_ms` limits and old
        run rows keep meaning what they meant) clocks the first assistant
        transcript of ANY kind, which on a tool turn is usually the holding
        line at `progress_after_sec`; `answer_ms` clocks the first
        NON-out-of-band transcript — the real answer, the number a delegate
        regression actually moves. `holding_line` records that an
        out-of-band line preceded the answer this turn."""
        first_reply_t, answer_t, holding_line = marks
        first_reply_ms = (
            int((first_reply_t - sent_at) * 1000)
            if first_reply_t is not None else None
        )
        return {
            "turn": turn_no,
            "latency_ms": first_reply_ms,
            "first_reply_ms": first_reply_ms,
            "answer_ms": (
                int((answer_t - sent_at) * 1000)
                if answer_t is not None else None
            ),
            "holding_line": holding_line,
            "turn_ms": int((time.monotonic() - sent_at) * 1000),
            "turn_end_seen": got_end,
            "audio_ms": int(play["scheduled_ms"] - snap[0]),
            "audio_discarded_ms": int(play["discarded_ms"] - snap[1]),
            "text": " ".join(
                t.get("text") or "" for t in turns[turns_before:]
                if t.get("role") == "assistant"
            ).strip(),
            # What the SCREEN got this turn (the authoritative `display`
            # text, cards, status lines) — for deterministic checks.
            "display": " ".join(
                str(f.get("text") or "") for f in frames[marks_from:]
                if f.get("type") == "display"
            ).strip(),
            "proposals": collect_proposals(frames[marks_from:]),
            "charts": sum(1 for f in frames[marks_from:] if f.get("type") == "chart"),
            "status": [str(f.get("text") or "") for f in frames[marks_from:]
                       if f.get("type") == "agent_status"],
        }

    async def _await_frame(kind, mark, deadline_sec):
        deadline = time.monotonic() + deadline_sec
        while time.monotonic() < deadline:
            for f in frames[mark:]:
                if f.get("type") == kind:
                    return True
            await asyncio.sleep(_POLL_SEC)
        return False

    async def _speak(ws, text):
        if mode == "sim":
            await ws.send_str(json.dumps({"type": "text", "text": text}))
        else:
            await _stream_speech(ws, text, locale)  # Task 6

    async def _drive(ws):
        nonlocal termination, reason
        # Greeting: wait for ready, then the greeting turn to finish.
        if not await _await_frame("ready", 0, _GREETING_WAIT_SEC):
            termination, reason = "ws_closed", "no ready frame"
            return
        if mode == "sim":
            # Finding 4: a sim run must never silently fall through to a
            # real (billable) provider — e.g. VOICE_LIVE_SIM_ENABLED unset
            # on this deploy, or the connection wasn't recognized as
            # loopback. The endpoint marks a genuinely-simulated session
            # with `"provider": "simulated"` on the ready frame; its
            # absence means we're about to open real provider turns and
            # burn the turn deadline against a live model. Abort before
            # any caller turn or LLM call.
            ready_seen = next(
                (f for f in frames if f.get("type") == "ready"), None
            )
            if not ready_seen or ready_seen.get("provider") != "simulated":
                termination, reason = "ws_closed", (
                    "sim mode requested but the endpoint did not apply the "
                    "simulated provider — check VOICE_LIVE_SIM_ENABLED and "
                    "that the target is loopback"
                )
                return
        await _await_frame("turn_end", 0, _GREETING_WAIT_SEC)
        _collect_transcripts(0)

        loop = asyncio.get_running_loop()
        script = [str(x) for x in (caller.get("script") or []) if str(x).strip()]
        if script:
            # A scripted caller: deterministic utterances, no caller LLM —
            # for capability suites graded by per-turn checks.
            for turn_no, utterance in enumerate(script):
                mark = len(frames)
                snap = (play["scheduled_ms"], play["discarded_ms"])
                sent_at = time.monotonic()
                await _speak(ws, utterance)
                turns.append({"role": "caller", "text": utterance})
                turns_before = len(turns)
                got_end = await _await_frame("turn_end", mark, turn_deadline)
                marks = _reply_marks(frames, mark)
                _collect_transcripts(mark)
                per_turn.append(_turn_record(
                    turn_no, sent_at, marks, got_end, snap, turns_before, mark))
                if not got_end:
                    termination, reason = "error", f"no turn_end within {turn_deadline}s"
                    return
            termination = "hangup"
            return
        for turn_no in range(max_turns):
            turns_left = max_turns - turn_no
            if turn_no == 0 and (caller.get("opening") or "").strip():
                # Spec §1: an explicit fixed opening skips the LLM for the
                # first utterance (deterministic call openings).
                utterance, hangup = str(caller["opening"]).strip(), False
                mark = len(frames)
                snap = (play["scheduled_ms"], play["discarded_ms"])
                sent_at = time.monotonic()
                await _speak(ws, utterance)
                turns.append({"role": "caller", "text": utterance})
                turns_before = len(turns)
                got_end = await _await_frame("turn_end", mark, turn_deadline)
                marks = _reply_marks(frames, mark)
                _collect_transcripts(mark)
                per_turn.append(_turn_record(
                    turn_no, sent_at, marks, got_end, snap, turns_before, mark))
                if not got_end:
                    termination, reason = "error", f"no turn_end within {turn_deadline}s"
                    return
                continue
            msgs = build_caller_prompt(caller, turns, turns_left)
            try:
                raw = await loop.run_in_executor(
                    None, lambda: caller_llm.invoke(msgs)
                )
                text = _normalize_llm_content(raw)
            except Exception:  # retry once (spec §7), then error
                try:
                    raw = await loop.run_in_executor(
                        None, lambda: caller_llm.invoke(msgs)
                    )
                    text = _normalize_llm_content(raw)
                except Exception as exc2:
                    termination, reason = "error", f"caller LLM failed: {exc2}"
                    return
            utterance, hangup = parse_caller_reply(text)
            if utterance is None:
                termination = "hangup"
                return
            mark = len(frames)
            snap = (play["scheduled_ms"], play["discarded_ms"])
            sent_at = time.monotonic()
            await _speak(ws, utterance)
            turns.append({"role": "caller", "text": utterance})
            turns_before = len(turns)
            got_end = await _await_frame("turn_end", mark, turn_deadline)
            marks = _reply_marks(frames, mark)
            _collect_transcripts(mark)
            per_turn.append(_turn_record(
                turn_no, sent_at, marks, got_end, snap, turns_before, mark))
            if hangup:
                termination = "hangup"
                return
            if not got_end:
                termination, reason = "error", f"no turn_end within {turn_deadline}s"
                return
        termination = "turn_cap"

    try:
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(
                url, headers=dict(voice_ctx.get("headers") or {}), max_msg_size=0
            ) as ws:
                receiver = asyncio.create_task(_receiver(ws))
                auth = voice_ctx.get("auth")
                if isinstance(auth, dict) and voice_ctx.get("user_token"):
                    # Agent kinds that require auth (business) take the
                    # caller's JWT in a first `auth` frame — the same frame
                    # the WAJ app sends (ai_access, capabilities, branch).
                    await ws.send_str(json.dumps(
                        {"type": "auth", "token": voice_ctx["user_token"], **auth}))
                try:
                    await asyncio.wait_for(_drive(ws), timeout=timeout_sec)
                finally:
                    receiver.cancel()
                    await asyncio.gather(receiver, return_exceptions=True)
    except asyncio.TimeoutError:
        termination, reason = "timeout", f"scenario exceeded {timeout_sec}s"
    except Exception as exc:
        logger.exception("voice eval: scenario %s failed", scenario.get("id"))
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}",
                "turns": turns, "per_turn": per_turn,
                "audio_bytes_received": audio_bytes["n"],
                "proposals": collect_proposals(frames),
                "termination": "ws_closed", "target": url}

    status = "completed" if termination in ("hangup", "turn_cap") else "error"
    return {"status": status, "reason": reason, "turns": turns,
            "per_turn": per_turn, "audio_bytes_received": audio_bytes["n"],
            "proposals": collect_proposals(frames),
            "termination": termination, "target": url}
