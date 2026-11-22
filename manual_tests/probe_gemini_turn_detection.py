"""Manual probe: what Gemini Live's end-of-speech window actually costs.

Measures, per `silence_duration_ms`, the wall time from the caller's LAST
SPEECH SAMPLE to the first audio frame the model sends back — and, separately,
whether a mid-sentence think-pause of a given length survives as one turn or
splits into two. This is the Gemini analogue of the Azure A/B recorded in
`voice/live/providers/azure_voice_live.py::_session_payload`, and it is how the
`_DEFAULT_TURN_DETECTION` value in `voice/live/providers/gemini_live.py` was
chosen. Re-run it before changing that number.

    GOOGLE_API_KEY=... AZURE_SPEECH_KEY=... AZURE_SPEECH_REGION=uaenorth \\
        venv/bin/python manual_tests/probe_gemini_turn_detection.py \\
        --model gemini-3.1-flash-live-preview

Two things about the method matter enough to state, because getting either
wrong makes the whole measurement lie:

  * TRAILING TTS SILENCE IS TRIMMED. Azure Speech pads a synthesized utterance
    with ~900ms of silence. Left in, the VAD's clock starts nine hundred
    milliseconds before the probe thinks speech ended, every reading shifts by
    that amount, and the differences between settings vanish into the noise.
    An untrimmed first pass of this probe read the provider default at 526ms;
    trimmed, the same configuration reads 1392ms.
  * SILENCE IS STREAMED IN REAL TIME AFTER THE UTTERANCE. End-of-speech is
    detected on silence ARRIVING, not on the socket going quiet. A probe that
    simply stops sending audio measures nothing at all.

Results, 2026-08-19, `gemini-3.1-flash-live-preview`, one Saudi-Arabic
utterance (3.16s after trimming), 5 runs per value:

    silence_duration_ms   eos -> first audio      500ms pause   800ms pause
    400                   1085ms (1038..1162)     SPLIT (1/2)   SPLIT (2/2)
    600                   1114ms (1084..1272)     held  (2/2)   SPLIT (2/2)
    700                   1264ms (1230..1315)     --            SPLIT (3/3)
    800                   1315ms (1301..1373)     --            SPLIT (3/3)
    900                   1467ms (1436..1470)     --            held  (3/3)
    provider default      1392ms (1380..1421)    held  (2/2)   held  (2/2)

Read: the provider default behaves like ~850-900ms and costs ~280ms per turn
against 600; below 600 the curve flattens onto a ~1.08s floor (the model's own
time-to-first-audio) while think-pause tolerance starts going; 700 and 800 are
dominated — they pay 150-200ms more than 600 for the same 800ms-pause failure.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import struct
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google import genai
from google.genai import types as gt

RATE = 16000
FRAME_MS = 20
FRAME_BYTES = int(RATE * 2 * FRAME_MS / 1000)
SILENCE_FRAME = b"\x00" * FRAME_BYTES

# Split into two halves so the pause test can insert a gap at a natural
# mid-sentence point rather than between words the ASR would rejoin anyway.
UTTERANCE_HEAD = "ابغى اعرف اجمالي التسوية"
UTTERANCE_TAIL = "امس بالرياض"

# The persona is deliberately trivial: this probe times the SERVICE's
# end-of-speech detection, and a model that thinks before answering would
# fold its own latency into every reading equally but noisily.
PERSONA = "Reply with the single Arabic word: نعم. Say nothing else, ever."


async def synthesize(text: str) -> bytes:
    """Caller speech, via the same Azure TTS the scenario harness uses."""
    from voice.providers.azure import AzureSpeechVoiceProvider

    provider = AzureSpeechVoiceProvider()
    return await provider.synthesize_pcm(
        text, voice="ar-SA-HamedNeural", language="ar-SA", sample_rate=RATE
    )


def trim_trailing_silence(pcm: bytes, *, threshold: int = 300) -> Tuple[bytes, int]:
    """Cut the synthesizer's trailing padding. See the module docstring."""
    samples = len(pcm) // 2
    values = struct.unpack("<%dh" % samples, pcm[: samples * 2])
    last_voiced = 0
    for index, value in enumerate(values):
        if abs(value) > threshold:
            last_voiced = index
    keep = min(samples, last_voiced + 1)
    return pcm[: keep * 2], round((samples - keep) / RATE * 1000)


def live_config(model: str, turn_detection: Optional[Dict[str, Any]]) -> gt.LiveConnectConfig:
    realtime_input = None
    if turn_detection is not None:
        realtime_input = gt.RealtimeInputConfig(
            automatic_activity_detection=gt.AutomaticActivityDetection(**turn_detection)
        )
    return gt.LiveConnectConfig(
        # AUDIO, not TEXT: `gemini-3.1-flash-live-preview` rejects a TEXT-only
        # modality outright ("The requested combination of response modalities
        # (TEXT) is not supported by the model"), which is also worth knowing
        # before pointing a text_only profile at this row.
        response_modalities=["AUDIO"],
        system_instruction=PERSONA,
        speech_config=gt.SpeechConfig(
            voice_config=gt.VoiceConfig(
                prebuilt_voice_config=gt.PrebuiltVoiceConfig(voice_name="Kore")
            )
        ),
        input_audio_transcription=gt.AudioTranscriptionConfig(),
        realtime_input_config=realtime_input,
    )


async def one_run(
    client: Any,
    model: str,
    turn_detection: Optional[Dict[str, Any]],
    segments: List[bytes],
    gap_ms: int,
    wait_sec: float = 5.0,
) -> List[int]:
    """Stream `segments` (gap_ms of silence between them); return the offset in
    ms of every distinct model response relative to the last speech sample.

    A negative offset means the service answered BEFORE the utterance was
    finished — i.e. it split the turn at the gap."""
    responses: List[float] = []
    eos_holder: List[float] = [0.0]

    async with client.aio.live.connect(model=model, config=live_config(model, turn_detection)) as session:
        stop = asyncio.Event()

        async def receive() -> None:
            try:
                while not stop.is_set():
                    async for message in session.receive():
                        now = time.monotonic()
                        content = getattr(message, "server_content", None)
                        if content is None:
                            continue
                        turn = getattr(content, "model_turn", None)
                        has_audio = turn is not None and any(
                            getattr(getattr(part, "inline_data", None), "data", None)
                            for part in (getattr(turn, "parts", None) or [])
                        )
                        # Coalesce the frames of one response; a fresh
                        # response after >1s of quiet is a separate turn.
                        if has_audio and (not responses or now - responses[-1] > 1.0):
                            responses.append(now)
                    await asyncio.sleep(0)
            except Exception:
                pass

        receiver = asyncio.create_task(receive())

        async def send(frame: bytes) -> None:
            await session.send_realtime_input(
                audio=gt.Blob(data=frame, mime_type="audio/pcm;rate=%d" % RATE)
            )
            await asyncio.sleep(FRAME_MS / 1000.0)

        # Lead-in silence gives the VAD a noise floor before speech starts.
        for _ in range(20):
            await send(SILENCE_FRAME)
        for position, segment in enumerate(segments):
            for offset in range(0, len(segment), FRAME_BYTES):
                await send(segment[offset : offset + FRAME_BYTES])
            if position < len(segments) - 1:
                for _ in range(int(gap_ms / FRAME_MS)):
                    await send(SILENCE_FRAME)
        eos_holder[0] = time.monotonic()

        deadline = eos_holder[0] + wait_sec
        while time.monotonic() < deadline:
            await send(SILENCE_FRAME)
        stop.set()
        await asyncio.sleep(0.05)
        receiver.cancel()

    return [round((stamp - eos_holder[0]) * 1000) for stamp in responses]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gemini-3.1-flash-live-preview")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--pause-runs", type=int, default=3)
    parser.add_argument(
        "--silence",
        default="default,400,600,700,800,900",
        help="comma-separated silence_duration_ms values; 'default' sends no "
             "realtime_input_config at all",
    )
    parser.add_argument("--pause-ms", default="500,800")
    args = parser.parse_args()

    whole, trimmed_whole = trim_trailing_silence(
        await synthesize(UTTERANCE_HEAD + " " + UTTERANCE_TAIL)
    )
    head, _ = trim_trailing_silence(await synthesize(UTTERANCE_HEAD))
    tail, _ = trim_trailing_silence(await synthesize(UTTERANCE_TAIL))
    print(
        "utterance %.2fs after trimming %dms of trailing TTS silence"
        % (len(whole) / (RATE * 2.0), trimmed_whole)
    )

    configs: List[Tuple[str, Optional[Dict[str, Any]]]] = []
    for token in args.silence.split(","):
        token = token.strip()
        if not token:
            continue
        if token == "default":
            configs.append(("provider default", None))
        else:
            configs.append(("silence=%s" % token, {"silence_duration_ms": int(token)}))

    client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY") or None)

    print()
    print("### end-of-speech -> first audio")
    for label, turn_detection in configs:
        readings: List[Any] = []
        for _ in range(args.runs):
            try:
                offsets = await one_run(client, args.model, turn_detection, [whole], 0)
                readings.append(offsets[0] if offsets else None)
            except Exception as exc:  # a probe must report, not raise
                readings.append("ERR:%s" % type(exc).__name__)
            await asyncio.sleep(0.8)
        numeric = sorted(r for r in readings if isinstance(r, int))
        median = numeric[len(numeric) // 2] if numeric else None
        print("  %-18s median=%-6s runs=%s" % (label, median, readings))

    print()
    print("### mid-utterance pause tolerance (1 turn = held, 2 = split)")
    for pause in [int(p) for p in args.pause_ms.split(",") if p.strip()]:
        for label, turn_detection in configs:
            outcomes = []
            for _ in range(args.pause_runs):
                try:
                    offsets = await one_run(
                        client, args.model, turn_detection, [head, tail], pause
                    )
                    outcomes.append("%d turn(s) @%s" % (len(offsets), offsets))
                except Exception as exc:
                    outcomes.append("ERR:%s" % type(exc).__name__)
                await asyncio.sleep(0.8)
            print("  gap=%4dms %-18s -> %s" % (pause, label, outcomes))


if __name__ == "__main__":
    asyncio.run(main())
