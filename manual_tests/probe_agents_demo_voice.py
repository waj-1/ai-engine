"""Drive scripted LIVE VOICE calls against the AI-agents demo portal's agents.

Not part of the pytest suite — it needs a running engine, a realtime provider
key and seeded profiles. Each script dials WS /voice/live for one agent (with
callCase for the outbound ones), lets the agent open the call, then plays the
caller's lines as real speech: every line is synthesized once with Gemini TTS
(cached as WAV under --audio-dir), and a background "microphone" streams it in
real time between continuous silence, exactly like a browser mic would.

Per caller turn it reports end-of-speech → first agent audio, the agent's
spoken transcript, and (from --engine-log, when given) which tools fired.

    python manual_tests/probe_agents_demo_voice.py --url ws://127.0.0.1:8077/voice/live \
        --engine-log /path/to/engine.log --json calls.json
"""
from __future__ import annotations

import argparse
import asyncio
import audioop
import json
import os
import re
import sys
import time
import uuid
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FRAME_MS = 40

SCRIPTS: Dict[str, Dict[str, Any]] = {
    "support-S01-en": {"agentKind": "agents_support", "locale": "en-US", "callCase": "S01", "voice": "Leda",
                       "quiet": 6.0, "lines": [
        "Hi, my Wi-Fi is crawling in the living room and the match keeps freezing!",
        "My account number is four one two zero three three one seven, and my date of birth is the fourteenth of March, nineteen ninety-one.",
        "Okay. Do I need a technician, or can I fix it myself?",
        "No, that's all. Thanks!",
    ]},
    "support-S03-en": {"agentKind": "agents_support", "locale": "en-US", "callCase": "S03", "voice": "Kore",
                       "quiet": 6.0, "lines": [
        "Hi. Why is my September bill so high? What's on it?",
        "My account number is seven one three zero four four one two, and my ID ends in one one eight seven.",
        "Oh, the roaming. Okay, when is it due?",
        "Got it, thanks, that's all.",
    ]},
    "support-S04-ar": {"agentKind": "agents_support", "locale": "ar-SA", "callCase": "S04", "voice": "Puck",
                       "quiet": 6.0, "lines": [
        "السلام عليكم، انخصمت مني الفاتورة مرتين على البطاقة.",
        "رقم الحساب 41209908، وآخر أربعة أرقام من الهوية 2290.",
        "رجعها على البطاقة لو سمحت.",
        "تمام، مشكور، هذا كل شي.",
    ]},
    "inquiries-I06-en": {"agentKind": "agents_inquiries", "locale": "en-US", "callCase": "I06",
                         "voice": "Aoede", "quiet": 6.0, "lines": [
        "Hi! I'm a video editor in Dubai Marina. I'm torn between Home Fiber 500 and the 1 Gig plan.",
        "What's the difference in price per month?",
        "I upload about forty gigabytes per project. Which one would you pick?",
        "Great, that's all, thank you.",
    ]},
    "inquiries-I04-ar": {"agentKind": "agents_inquiries", "locale": "ar-SA", "callCase": "I04", "voice": "Charon",
                         "quiet": 6.0, "lines": [
        "السلام عليكم، عندي مقهى في حي الروضة في جدة وعندي ستة موظفين. وش عندكم للإنترنت والجوالات؟",
        "طيب كم سعرها؟ وفيه شي احتياطي إذا انقطع الفايبر؟",
        "تمام، شكراً، هذا كل شي.",
    ]},
    "collection-C01-en": {"agentKind": "agents_collection", "locale": "en-US", "callCase": "C01", "voice": "Puck", "lines": [
        "Yes, this is Yousef.",
        "My date of birth is the eighth of August, nineteen eighty-four.",
        "Okay, fine. Yes, please send me the link.",
        "That's all, thanks. Bye.",
    ]},
    "sales-O09-en": {"agentKind": "agents_sales", "locale": "en-US", "callCase": "O09", "voice": "Fenrir", "lines": [
        "Sure, I have a minute.",
        "The last four digits of my ID are five five nine four.",
        "Honestly, the kids complain about lag all the time.",
        "How much is it? Is there any discount?",
        "Okay, yes, send me the details.",
        "That's all, bye.",
    ]},
}


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

def synthesize(text: str, voice: str, path: Path) -> Path:
    """24kHz mono pcm16 WAV of ``text`` (Gemini TTS), cached by path."""
    if path.exists():
        return path
    from dotenv import load_dotenv
    from google import genai
    from google.genai import types

    load_dotenv(ROOT / ".env")
    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    model = os.getenv("VOICE_TTS_MODEL", "gemini-2.5-flash-preview-tts")
    pcm = b""
    for _attempt in range(3):  # the TTS preview sometimes returns a text-only candidate
        resp = client.models.generate_content(
            model=model, contents=f"Say naturally, like a customer on the phone: {text}",
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))),
            ),
        )
        for cand in resp.candidates or []:
            for part in (cand.content.parts if cand.content else []) or []:
                if part.inline_data and part.inline_data.data:
                    pcm = part.inline_data.data
                    break
        if pcm:
            break
    if not pcm:
        raise RuntimeError(f"TTS returned no audio for {text!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm)
    return path


def load_pcm(path: Path, rate: int) -> bytes:
    with wave.open(str(path), "rb") as w:
        src_rate = w.getframerate()
        pcm = w.readframes(w.getnframes())
    if src_rate != rate:
        pcm, _ = audioop.ratecv(pcm, 2, 1, src_rate, rate, None)
    return pcm


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------

class Call:
    def __init__(self) -> None:
        self.started = time.monotonic()
        self.in_rate = 16000
        self.ready = asyncio.Event()
        self.closed = asyncio.Event()
        self.last_agent_audio = 0.0
        self.agent_audio_since: Optional[float] = None   # first agent audio after a mark
        self.audio_times: List[float] = []              # arrival time of every agent audio frame
        self.mark_at: Optional[float] = None
        self.agent_text: List[str] = []
        self.user_text: List[str] = []
        self.frames: List[Dict[str, Any]] = []
        self.close_info = ""
        self.speech_queue: "asyncio.Queue[bytes]" = asyncio.Queue()
        self.speaking_done = asyncio.Event()

    def now(self) -> float:
        return time.monotonic() - self.started


async def receiver(ws: aiohttp.ClientWebSocketResponse, call: Call) -> None:
    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.BINARY:
            t = time.monotonic()
            call.last_agent_audio = t
            call.audio_times.append(t)
            if call.mark_at is not None and call.agent_audio_since is None and t > call.mark_at:
                call.agent_audio_since = t
        elif msg.type == aiohttp.WSMsgType.TEXT:
            frame = json.loads(msg.data)
            call.frames.append({"t": round(call.now(), 2), **frame})
            if frame.get("type") == "ready":
                call.in_rate = int(((frame.get("audio") or {}).get("input_sample_rate")) or 16000)
                call.ready.set()
            elif frame.get("type") == "transcript":
                (call.agent_text if frame.get("role") == "assistant" else call.user_text).append(frame.get("text") or "")
        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
            break
    call.close_info = f"{ws.close_code}"
    call.closed.set()


async def microphone(ws: aiohttp.ClientWebSocketResponse, call: Call) -> None:
    """Continuous real-time audio: queued speech, otherwise silence."""
    await call.ready.wait()
    frame_bytes = int(call.in_rate * FRAME_MS / 1000) * 2
    silence = b"\x00" * frame_bytes
    buf = b""
    next_t = time.monotonic()
    while not call.closed.is_set():
        if not buf and not call.speech_queue.empty():
            buf = call.speech_queue.get_nowait()
        if buf:
            chunk, buf = buf[:frame_bytes], buf[frame_bytes:]
            if len(chunk) < frame_bytes:
                chunk += silence[: frame_bytes - len(chunk)]
            if not buf:
                call.mark_at = time.monotonic() + FRAME_MS / 1000
                call.agent_audio_since = None
                call.speaking_done.set()
        else:
            chunk = silence
        try:
            await ws.send_bytes(chunk)
        except Exception:
            return
        next_t += FRAME_MS / 1000
        await asyncio.sleep(max(0.0, next_t - time.monotonic()))


async def wait_quiet(call: Call, quiet: float, timeout: float) -> None:
    """Until the agent has produced no audio for ``quiet`` seconds (after it started)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not call.closed.is_set():
        if call.last_agent_audio and time.monotonic() - call.last_agent_audio > quiet:
            return
        await asyncio.sleep(0.1)


def answer_start(times: List[float], since: Optional[float], gap: float = 0.8) -> Optional[float]:
    """Seconds from end-of-speech to the start of the LONGEST agent speech
    burst after it — the answer itself, not a "let me check" filler or a
    holding line that a delegated turn speaks first."""
    if since is None:
        return None
    pts = [t for t in times if t > since]
    if not pts:
        return None
    bursts, start, prev = [], pts[0], pts[0]
    for t in pts[1:]:
        if t - prev > gap:
            bursts.append((start, prev))
            start = t
        prev = t
    bursts.append((start, prev))
    best = max(bursts, key=lambda b: b[1] - b[0])
    return round(best[0] - since, 2)


def repeated_sentences(text: str) -> List[str]:
    """Sentences the agent said more than once in one turn (the repeat bug)."""
    parts = [p.strip().casefold() for p in re.split(r"(?<=[.!?؟])\s*", text or "") if len(p.strip()) > 12]
    seen, dup = set(), []
    for p in parts:
        if p in seen and p not in dup:
            dup.append(p)
        seen.add(p)
    return dup


def tools_in_log(log_path: Optional[str], offset: int) -> List[str]:
    if not log_path or not os.path.exists(log_path):
        return []
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        fh.seek(offset)
        text = fh.read()
    names = re.findall(r"direct tool (\w+) args=", text)
    for batch in re.findall(r"agents_demo agents_\w+: tools=\[([^\]]*)\]", text):
        names += [f"ask_agent→{n.strip(chr(39))}" for n in batch.split(", ") if n]
    return names


def delegate_ms_in_log(log_path: Optional[str], offset: int) -> List[float]:
    if not log_path or not os.path.exists(log_path):
        return []
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        fh.seek(offset)
        return [float(x) for x in re.findall(r"delegate_ms=([0-9.]+)", fh.read())]


async def run_script(url: str, name: str, script: Dict[str, Any], audio_dir: Path,
                     log_path: Optional[str]) -> Dict[str, Any]:
    query = {"agentKind": script["agentKind"], "locale": script["locale"], "sessionId": str(uuid.uuid4())}
    if script.get("callCase"):
        query["callCase"] = script["callCase"]
    qs = "&".join(f"{k}={v}" for k, v in query.items())
    wavs = [synthesize(line, script["voice"], audio_dir / f"{name}-{i}.wav") for i, line in enumerate(script["lines"])]
    log_offset = os.path.getsize(log_path) if log_path and os.path.exists(log_path) else 0
    call = Call()
    turns: List[Dict[str, Any]] = []
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(f"{url}?{qs}", max_msg_size=0) as ws:
            rx = asyncio.create_task(receiver(ws, call))
            mic = asyncio.create_task(microphone(ws, call))
            try:
                await asyncio.wait_for(call.ready.wait(), 20)
            except asyncio.TimeoutError:
                return {"script": name, "error": "no ready frame", "frames": call.frames[:5]}
            # The agent opens the call (greeting_trigger).
            t0 = time.monotonic()
            while not call.last_agent_audio and time.monotonic() - t0 < 15:
                await asyncio.sleep(0.05)
            greet_latency = round(call.last_agent_audio - t0, 2) if call.last_agent_audio else None
            await wait_quiet(call, 1.4, 30)
            greeting = "".join(call.agent_text).strip()
            for i, (line, wav) in enumerate(zip(script["lines"], wavs)):
                before = len(call.agent_text)
                call.speaking_done.clear()
                await call.speech_queue.put(load_pcm(wav, call.in_rate))
                await call.speaking_done.wait()
                end_of_speech = call.mark_at
                await asyncio.sleep(0.2)
                t_wait = time.monotonic()
                while call.agent_audio_since is None and time.monotonic() - t_wait < 20 and not call.closed.is_set():
                    await asyncio.sleep(0.05)
                first_audio = (round(call.agent_audio_since - end_of_speech, 2)
                               if call.agent_audio_since and end_of_speech else None)
                # A delegated (ask_agent) agent says "let me check" and goes
                # quiet for the 2-5s the brain takes — a real caller waits.
                await wait_quiet(call, float(script.get("quiet", 1.6)), 45)
                agent_said = "".join(call.agent_text[before:]).strip()
                turns.append({"caller": line, "first_audio_s": first_audio,
                              "answer_audio_s": answer_start(call.audio_times, end_of_speech),
                              "agent": agent_said, "repeats": repeated_sentences(agent_said)})
                if call.closed.is_set():
                    break
            await asyncio.sleep(0.5)
            await ws.close()
            mic.cancel()
            await asyncio.gather(rx, return_exceptions=True)
    await asyncio.sleep(0.5)
    return {"script": name, "greeting_first_audio_s": greet_latency, "greeting": greeting, "turns": turns,
            "tools": tools_in_log(log_path, log_offset), "close": call.close_info,
            "delegate_ms": delegate_ms_in_log(log_path, log_offset),
            "user_transcripts": "".join(call.user_text)[:600]}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8000/voice/live")
    ap.add_argument("--only", help="substring filter on script names")
    ap.add_argument("--audio-dir", default=str(ROOT / "manual_tests" / ".agents_demo_audio"))
    ap.add_argument("--engine-log", help="engine log file, to report which tools fired")
    ap.add_argument("--json", help="write results here")
    args = ap.parse_args()
    results = []
    for name, script in SCRIPTS.items():
        if args.only and args.only not in name:
            continue
        print(f"\n=== {name} ===", flush=True)
        res = await run_script(args.url, name, script, Path(args.audio_dir), args.engine_log)
        results.append(res)
        if res.get("error"):
            print("ERROR", res["error"], res.get("frames"))
            continue
        print(f"[greeting {res['greeting_first_audio_s']}s] {res['greeting']}")
        for t in res["turns"]:
            rep = f" REPEATS={t['repeats']}" if t.get("repeats") else ""
            print(f"> {t['caller']}\n< [first {t['first_audio_s']}s / answer {t['answer_audio_s']}s]{rep} "
                  f"{t['agent']}")
        print("tools:", res["tools"], "| delegate_ms:", res["delegate_ms"], "| close:", res["close"])
    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
