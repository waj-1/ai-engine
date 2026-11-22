"""Manual probe: what does CALLER SILENCE do on Gemini Live?

Written for a live report on 2026-08-19 — "she answers, then I pause, and she
immediately tries to fill the emptiness" — where the first job was telling two
very different causes apart before touching anything:

  1. the SERVICE commits a turn the caller never took (a noise burst opening
     `automatic_activity_detection`, closed by `silence_duration_ms`), and the
     model dutifully answers it; or
  2. OUR machinery creates the response — the endpoint's stall tiers, the
     answer watchdog, relay custody, all of which went live on this provider
     for the first time when `UserTurnState` started synthesizing the user
     transcript `final` the API never sends.

This probe settles (1) directly. It replays ONE full delegated turn against
the real service using the provider's own `_connect` (imported, not
re-created, so the LiveConnectConfig is byte-identical to production), answers
the model's `ask_agent` with sentence-streamed WHEN_IDLE tool results exactly
as `endpoints/voice_live.py::_speak_sentence` does, and then streams real-time
SILENCE — or low-level room-ish noise — for as long as you ask, recording every
server message.

It reports three things, and the second and third are worth as much as the
first:

  * how many NEW responses the model started while the caller said nothing;
  * whether any `input_transcription` fragment arrives AFTER the model has
    taken the floor for the same utterance — the shape that would leave
    `UserTurnState.user_open` dangling and let a LATER floor-taking (our own
    answer delivery, seconds later, with the caller silent) synthesize a
    commit for words she is not saying;
  * end-of-speech -> first audio, honestly measured. Production cannot report
    this on Gemini (`eot_to_first_audio_ms` is None there by design: the
    synthesized final is derived FROM the first audio, so a metric built on it
    would read ~0ms forever and measure nothing). Here the probe owns the
    clock on the caller's side, so the number is real.

RESULTS, 2026-08-19, `gemini-3.1-flash-live-preview`, `silence_duration_ms`
600 (the shipped default), one Saudi-Arabic utterance, trailing TTS silence
trimmed, everything streamed in real time at 20ms/frame:

    tail signal            new responses during the tail
    silence (digital 0)    0  (30s after the answer's turn_complete)
    noise ~ -47 dBFS       0  (25s)
    noise ~ -29 dBFS       0  (13s after the answer)

    input_transcription fragments arriving after first audio: 0 of 5 runs,
    including a 21-SECOND utterance whose entire transcript arrived in ONE
    message 157ms BEFORE the first audio frame.

    end-of-speech -> first audio: 1073ms (21s utterance, model went straight
    into its announce line) / 1490-1575ms (2.5s utterance, model produced a
    greeting first). The ~1.07s is the floor; the rest is generation.

Read: **caller silence is inert at the service** — continuous room noise has
no offset, so the VAD never closes a turn on it, and nothing the model does is
"filling" a pause on its own. An unprompted-sounding reply is a reply to
something the service DID commit. And the transcript never straddles the
floor, so the synthesized-final design in `gemini_live.UserTurnState` cannot
manufacture a commit out of a stale fragment: do not "fix" that speculatively.

    GOOGLE_API_KEY=... venv/bin/python manual_tests/probe_gemini_silence.py \\
        --modes silence,noise-quiet,noise-loud --tail 45

macOS `say -v Majed` synthesizes the caller, so no speech-service key is
needed.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import random
import struct
import subprocess
import sys
import time
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.genai import types as gt  # noqa: E402

from voice.live.config import LiveVoiceConfig  # noqa: E402
from voice.live.providers.gemini_live import _connect  # noqa: E402

RATE = 16000
FRAME_MS = 20
FRAME_BYTES = int(RATE * 2 * FRAME_MS / 1000)
SILENCE = b"\x00" * FRAME_BYTES

QUESTION = "ما هي مبيعات الشهر الماضي؟"
# The answer, as the endpoint delivers it: one WHEN_IDLE tool result per
# streamed sentence (see _speak_sentence). Each becomes its own response and
# its own turn_complete, which is exactly the shape the endpoint's turn_end
# tripwire is evaluated against.
SENTENCES = [
    "بلغت مبيعات شهر يوليو 410,175 ريالاً و28 هللة.",
    "وإجمالي العمليات 5,368 عملية.",
    "أعلى فرع كان ريد سي مول بـ 48,841 ريالاً.",
]

# The probe times the SERVICE, so the persona is deliberately the real one:
# a model that re-greets or announces twice inflates every reading, and
# noticing that it does is itself a finding.
def _persona() -> str:
    from runtime.prompts.voice import VOICE_PERSONA_PROMPT
    return (
        VOICE_PERSONA_PROMPT
        .replace("{language}", "Saudi Arabic")
        .replace("{business_name}", "the business")
        .replace("{assistant_name}", "the assistant")
    )


def _synthesize(text: str) -> bytes:
    """Caller speech at 16kHz pcm16, with TRAILING SILENCE TRIMMED.

    The trim is not cosmetic: `say` pads an utterance, the VAD's clock starts
    at the last real sample, and an untrimmed probe reports every window
    hundreds of milliseconds early. Same lesson as
    probe_gemini_turn_detection.py.
    """
    out = os.path.join("/tmp", "_probe_caller.wav")
    subprocess.run(
        ["say", "-v", "Majed", "--data-format=LEI16@16000",
         "--file-format=WAVE", "-o", out, text],
        check=True,
    )
    raw = open(out, "rb").read()
    pcm = raw[raw.find(b"data") + 8:]
    end = len(pcm)
    while end > FRAME_BYTES:
        frame = pcm[end - FRAME_BYTES:end]
        if max(abs(v) for v in struct.unpack(f"<{len(frame)//2}h", frame)) > 300:
            break
        end -= FRAME_BYTES
    return pcm[:end]


def _noise(amp: int) -> bytes:
    return struct.pack(
        f"<{FRAME_BYTES//2}h",
        *[random.randint(-amp, amp) for _ in range(FRAME_BYTES // 2)],
    )


_TAIL_SIGNAL = {"silence": None, "noise-quiet": 150, "noise-loud": 1200}


async def _run(mode: str, tail_sec: float, silence_ms: Optional[int], model: str):
    cfg = LiveVoiceConfig(
        model_string=model, voice_id="Kore", language="ar",
        extra_config=(
            {"turn_detection": {"silence_duration_ms": silence_ms}}
            if silence_ms is not None else {}
        ),
    )
    caller = _synthesize(QUESTION)
    t0 = time.monotonic()
    rows: List = []

    def log(kind: str, detail: str = "") -> None:
        at = time.monotonic() - t0
        rows.append((at, kind, detail))
        print(f"  +{at*1000:8.0f}ms  {kind:20} {detail}")

    print(f"\n=== mode={mode} silence_duration_ms={silence_ms} "
          f"caller={len(caller)/2/RATE:.2f}s tail={tail_sec}s ===")

    async with _connect(cfg, _persona()) as sess:
        stop = asyncio.Event()
        st: Dict = {"eos": None, "first_audio": None, "in_tx": [],
                    "responses": 0, "phase": "lead"}
        amp = _TAIL_SIGNAL[mode]

        async def send() -> None:
            async def frame(pcm: bytes) -> None:
                await sess.send_realtime_input(
                    audio=gt.Blob(data=pcm, mime_type=f"audio/pcm;rate={RATE}")
                )
                await asyncio.sleep(FRAME_MS / 1000)   # real time, always

            for _ in range(20):
                await frame(SILENCE)
            st["phase"] = "speech"
            for i in range(0, len(caller), FRAME_BYTES):
                await frame(caller[i:i + FRAME_BYTES])
            st["eos"] = time.monotonic() - t0
            log("CALLER-EOS", "the caller says nothing from here on")
            st["phase"] = "tail"
            deadline = time.monotonic() + tail_sec
            while time.monotonic() < deadline and not stop.is_set():
                await frame(SILENCE if amp is None else _noise(amp))
            stop.set()

        async def receive() -> None:
            while not stop.is_set():
                try:
                    async for msg in sess.receive():
                        sc = getattr(msg, "server_content", None)
                        if sc is not None:
                            if getattr(sc, "interrupted", False):
                                log("interrupted")
                            itx = getattr(sc, "input_transcription", None)
                            if itx is not None:
                                st["in_tx"].append(
                                    (time.monotonic() - t0, getattr(itx, "text", "") or "")
                                )
                                log("in_tx", f"finished={getattr(itx,'finished',None)} "
                                             f"{(getattr(itx,'text','') or '')[:60]!r}")
                            otx = getattr(sc, "output_transcription", None)
                            if otx is not None and (getattr(otx, "text", None) or ""):
                                log("out_tx", repr(otx.text)[:60])
                            parts = getattr(getattr(sc, "model_turn", None), "parts", None) or []
                            if any(getattr(getattr(p, "inline_data", None), "data", None)
                                   for p in parts):
                                if st["first_audio"] is None:
                                    st["first_audio"] = time.monotonic() - t0
                                    log("FIRST-AUDIO")
                            if getattr(sc, "generation_complete", False):
                                st["responses"] += 1
                                log("generation_complete",
                                    f"[{st['phase']}] response #{st['responses']}")
                            if getattr(sc, "turn_complete", False):
                                log("turn_complete", f"[{st['phase']}]")
                        tc = getattr(msg, "tool_call", None)
                        for call in getattr(tc, "function_calls", None) or []:
                            log("TOOL_CALL", f"{call.name} {dict(call.args or {})}")
                            await asyncio.sleep(2.5)      # a real delegated turn
                            for sentence in SENTENCES:
                                await sess.send_tool_response(function_responses=[
                                    gt.FunctionResponse(
                                        id=call.id, name=call.name,
                                        response={"spoken": sentence},
                                        scheduling=gt.FunctionResponseScheduling.WHEN_IDLE,
                                    )])
                                await asyncio.sleep(0.05)
                            log("ANSWER-DELIVERED", f"{len(SENTENCES)} sentences")
                    if stop.is_set():
                        return
                    await asyncio.sleep(0)
                except Exception as exc:      # a torn-down socket ends the probe
                    log("recv-ended", repr(exc)[:100])
                    return

        await asyncio.gather(send(), receive())

    eos, first_audio = st["eos"], st["first_audio"]
    after_floor = [t for t, _ in st["in_tx"]
                   if first_audio is not None and t > first_audio]
    tail_responses = [r for r in rows
                      if r[1] == "generation_complete" and "[tail]" in r[2]]
    print(f"\n  -- {mode} --")
    if eos is not None and first_audio is not None:
        print(f"  end-of-speech -> first audio        : {(first_audio-eos)*1000:.0f}ms")
    print(f"  responses in the whole run          : {st['responses']}")
    print(f"  ...of which completed after EOS     : {len(tail_responses)}")
    print(f"  in_tx fragments after first audio   : {len(after_floor)} "
          f"(non-zero means UserTurnState.user_open can dangle)")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemini-3.1-flash-live-preview")
    ap.add_argument("--tail", type=float, default=45.0,
                    help="seconds of caller silence/noise after end-of-speech")
    ap.add_argument("--silence-ms", type=int, default=600,
                    help="turn_detection.silence_duration_ms; -1 = provider default")
    ap.add_argument("--modes", default="silence",
                    help="comma-separated: " + ",".join(_TAIL_SIGNAL))
    args = ap.parse_args()
    window = None if args.silence_ms < 0 else args.silence_ms
    for mode in [m.strip() for m in args.modes.split(",") if m.strip()]:
        if mode not in _TAIL_SIGNAL:
            raise SystemExit(f"unknown mode {mode!r}; pick from {sorted(_TAIL_SIGNAL)}")
        await _run(mode, args.tail, window, args.model)


if __name__ == "__main__":
    asyncio.run(main())
