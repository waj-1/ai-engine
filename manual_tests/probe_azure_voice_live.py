"""Manual probe: real Azure Voice Live round trip through the provider.

Usage:
    AZURE_VOICE_LIVE_API_KEY=... venv/bin/python manual_tests/probe_azure_voice_live.py \
        --endpoint https://voice-live-example.services.ai.azure.com \
        --model gpt-4.1-mini [--language ar-SA] [--voice ar-SA-HamedNeural] \
        [--wav path/to/16bit_24k_mono.wav]

Without --wav it only exercises the text path (greeting via send_text_turn).
With --wav it streams the file as caller audio and prints what the service
heard. Prints every event; totals audio bytes received.

Doubles as the region/model availability check — a rejected model answers
"is <model> served from this resource's region?" in one line, and it is the
ONLY reliable way to ask. Voice Live models are fully managed, so
`az cognitiveservices account deployment list` returns nothing for a working
resource and `az cognitiveservices model list` omits gpt-realtime even in
regions where it demonstrably runs:

    AZURE_VOICE_LIVE_NATIVE_API_KEY=... venv/bin/python \\
        manual_tests/probe_azure_voice_live.py \\
        --endpoint https://voice-live-example.services.ai.azure.com \\
        --model gpt-realtime --native --voice marin \\
        --auth-env AZURE_VOICE_LIVE_NATIVE_API_KEY

An unsupported model fails at start() with "Model <name> is not supported in
this region." Use the Foundry host (`*.services.ai.azure.com`) — the
`*.cognitiveservices.azure.com` host 404s the /voice-live/realtime path even
on a resource that works.

`--truncate-inflight` is a third mode: the A/B reproduction of the 2026-08-06
mid-word cut. See probe_truncate_inflight() for the measured numbers.

    AZURE_VOICE_LIVE_NATIVE_API_KEY=... venv/bin/python \\
        manual_tests/probe_azure_voice_live.py --truncate-inflight \\
        --arm control \\
        --endpoint https://voice-live-example.services.ai.azure.com \\
        --model gpt-realtime --native --voice marin --language ar-SA \\
        --auth-env AZURE_VOICE_LIVE_NATIVE_API_KEY
"""
import argparse
import asyncio
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voice.live.config import LiveVoiceConfig  # noqa: E402
from voice.live.providers.azure_voice_live import AzureVoiceLiveSession  # noqa: E402


async def probe_interrupt_response_false(args) -> None:
    """Confirmed-barge-in probe (2026-08-02 plan, Task 2): with
    `turn_detection.interrupt_response: false`, what happens when a caller
    turn COMMITS while a response is still active?

    Prints RAW wire event types (tapped before translation) with monotonic
    timestamps, so the four verdicts can be read straight off the log:
      1. does speech_started still fire mid-response?
      2. does the active response keep streaming through caller speech?
      3. commit-while-active: is the auto-created reply rejected
         ("already has an active response"), queued, or does it implicitly
         cancel the active response?
      4. do input transcription deltas arrive during speech or only after
         committed (and with what latency)?
    """
    cfg = LiveVoiceConfig()
    cfg.model_string = args.model
    cfg.language = args.language
    cfg.voice_id = args.voice
    cfg.extra_config = {
        "endpoint": args.endpoint,
        # Live-probed 2026-08-02: the service schema REQUIRES interrupt_response
        # for auto_truncate (validate_auto_truncate_requires_interrupt_response)
        # — interrupt_response:false with auto_truncate:true is rejected at
        # session.update. Null removes the default via the provider merge.
        "turn_detection": {"interrupt_response": False, "auto_truncate": None},
    }
    if args.native:
        cfg.extra_config["voice_type"] = "native"
    if args.auth_env:
        cfg.extra_config["auth_env_var"] = args.auth_env
    session = AzureVoiceLiveSession(
        cfg, persona="You are a voice assistant probe. Do exactly as told."
    )
    t0 = time.monotonic()
    orig_translate = session._translate_nosend

    def tap(evt):
        etype = evt.get("type") or ""
        extra = ""
        if etype == "error":
            extra = f" err={((evt.get('error') or {}).get('message') or '')[:140]!r}"
        print(f"RAW {time.monotonic() - t0:7.2f}s {etype}{extra}")
        return orig_translate(evt)

    session._translate_nosend = tap
    await session.start()
    print("connected + session.updated (interrupt_response=false)")

    async def reader():
        async for evt in session.events():
            if evt.type != "audio":
                print(
                    f"EVT {time.monotonic() - t0:7.2f}s [{evt.type}] "
                    f"role={evt.role} final={evt.final} "
                    f"text={(evt.text or '')[:60]!r} msg={evt.message}"
                )

    task = asyncio.create_task(reader())
    await session.send_text_turn(
        "Count slowly from one to thirty, one number at a time, no other words."
    )
    await asyncio.sleep(2.5)
    print(f"--- {time.monotonic() - t0:7.2f}s streaming caller speech over the reply ---")
    with wave.open(args.wav, "rb") as w:
        assert w.getframerate() == 24000 and w.getnchannels() == 1, \
            "probe wants 24kHz mono pcm16"
        data = w.readframes(w.getnframes())
    data += b"\x00\x00" * (24000 * 2)  # 2s silence so semantic VAD closes the turn
    chunk = 24000 * 2 // 25  # 40ms
    for i in range(0, len(data), chunk):
        await session.send_audio(data[i:i + chunk])
        await asyncio.sleep(0.04)
    print(f"--- {time.monotonic() - t0:7.2f}s caller audio done (incl. 2s silence) ---")
    await asyncio.sleep(12)
    await session.close()
    await asyncio.sleep(0.2)
    task.cancel()


async def probe_truncate_inflight(args) -> None:
    """Does a playback-flush truncate land on a response that is STILL
    GENERATING, and does the service then end that response mid-word?

    The reproduction for the 2026-08-06 04:13 "she stops mid-word and nothing
    is in the logs" report. Run it twice — `--arm control` and `--arm flush` —
    against the same model row; the only difference is one `report_playback`
    call 1.0s into a 30-second reply, with the offset a browser reports when
    it flushes (200ms behind the received stream).

    Measured 2026-08-06 on `gpt-realtime` (uaenorth resource's India hop),
    with a live client native row's turn_detection:

        control  30150ms audio, counted 1..20 in full   status=completed
        flush     1750ms audio, transcript dies at "اثنان"  status=completed

    Note the status: the service does NOT report this as cancelled or
    incomplete, which is exactly why the engine log was silent. With the
    deferred-truncate fix in place the flush arm returns to ~32000ms and the
    truncate is sent after `response.done`.

    No caller audio and no TTS needed — one text turn, one flush, ~35s.
    """
    cfg = LiveVoiceConfig()
    cfg.model_string = args.model
    cfg.language = args.language
    cfg.voice_id = args.voice
    cfg.extra_config = {"endpoint": args.endpoint}
    if args.native:
        cfg.extra_config["voice_type"] = "native"
    if args.auth_env:
        cfg.extra_config["auth_env_var"] = args.auth_env

    session = AzureVoiceLiveSession(
        cfg, persona="You are a voice assistant probe. Do exactly as told."
    )
    t0 = time.monotonic()
    orig_translate = session._translate_nosend
    state = {"text": "", "done": None}

    def tap(evt):
        etype = evt.get("type") or ""
        ts = time.monotonic() - t0
        if etype == "response.audio_transcript.delta":
            state["text"] += evt.get("delta") or ""
        elif etype == "response.done":
            resp = evt.get("response") or {}
            state["done"] = resp.get("status")
            print(f"RAW {ts:6.2f}s response.done status={resp.get('status')!r} "
                  f"details={resp.get('status_details')!r}")
        elif etype == "error":
            print(f"RAW {ts:6.2f}s error "
                  f"{((evt.get('error') or {}).get('message') or '')[:200]!r}")
        elif etype == "response.created":
            print(f"RAW {ts:6.2f}s response.created")
        return orig_translate(evt)

    session._translate_nosend = tap
    orig_send = session._send

    async def send_tap(frame, **kw):
        if (frame or {}).get("type") == "conversation.item.truncate":
            print(f"SENT {time.monotonic() - t0:6.2f}s truncate "
                  f"item={frame.get('item_id')} "
                  f"audio_end_ms={frame.get('audio_end_ms')}")
        return await orig_send(frame, **kw)

    session._send = send_tap
    await session.start()
    print(f"--- arm {args.arm}: connected ---")

    async def reader():
        async for _ in session.events():
            pass

    task = asyncio.create_task(reader())
    await session.send_text_turn(args.ask)

    deadline = time.monotonic() + 8.0
    while session._audio_ms_total < 1500 and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    if args.arm == "flush":
        played = max(0.0, session._audio_ms_total - 200.0)
        print(f"--- {time.monotonic() - t0:6.2f}s report_playback({played:.0f}) "
              f"of {session._audio_ms_total:.0f}ms generated "
              f"(response_active={session._response_active}) ---")
        await session.report_playback(played)

    for _ in range(400):
        if state["done"] is not None:
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(1.0)
    print(f"=== arm {args.arm} RESULT ===")
    print(f"  audio generated : {session._audio_ms_total:.0f} ms")
    print(f"  response.done   : {state['done']}")
    print(f"  transcript      : {state['text']!r}")
    await session.close()
    await asyncio.sleep(0.2)
    task.cancel()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--model", default="gpt-4.1-mini")
    ap.add_argument("--language", default="en-US")
    ap.add_argument("--voice", default="en-US-AvaMultilingualNeural")
    ap.add_argument("--wav", default=None)
    ap.add_argument("--phrase", action="append", default=[])
    ap.add_argument("--caller-language", action="append", default=[],
                    help="Repeat for auto-detect among several (ar-JO, en-US).")
    ap.add_argument("--native", action="store_true",
                    help="native-audio model row (bare-string voice shape)")
    ap.add_argument("--auth-env", default=None,
                    help="env var holding the api key (extra_config.auth_env_var)")
    ap.add_argument("--interrupt-response-false", action="store_true",
                    help="run the commit-while-active probe with "
                         "turn_detection.interrupt_response=false (needs --wav "
                         "of real speech, 24kHz mono pcm16)")
    ap.add_argument("--truncate-inflight", action="store_true",
                    help="reproduce the mid-word cut: one report_playback "
                         "while the response is still generating")
    ap.add_argument("--arm", choices=("control", "flush"), default="flush",
                    help="--truncate-inflight arm: `control` never reports "
                         "playback, `flush` reports it once mid-response")
    ap.add_argument("--ask", default=(
        "عدّ ببطء من واحد إلى عشرين، رقماً رقماً، بالعربية، بدون أي كلمات أخرى."),
        help="the long answer to cut into")
    args = ap.parse_args()

    if args.truncate_inflight:
        await probe_truncate_inflight(args)
        return

    if args.interrupt_response_false:
        if not args.wav:
            ap.error("--interrupt-response-false needs --wav (real speech)")
        await probe_interrupt_response_false(args)
        return

    cfg = LiveVoiceConfig()
    cfg.model_string = args.model
    cfg.language = args.language
    cfg.voice_id = args.voice
    cfg.phrase_list = args.phrase
    cfg.caller_languages = args.caller_language
    cfg.extra_config = {"endpoint": args.endpoint}
    # --native / --auth-env apply here too, not only to the
    # --interrupt-response-false path. They used to be read solely by
    # probe_interrupt_response_false(), so the plain probe silently ran a
    # native model row as if it were cascaded and authenticated against
    # whatever AZURE_VOICE_LIVE_API_KEY happened to hold — which makes a
    # region/model availability check answer the wrong question.
    if args.native:
        cfg.extra_config["voice_type"] = "native"
    if args.auth_env:
        cfg.extra_config["auth_env_var"] = args.auth_env

    session = AzureVoiceLiveSession(
        cfg,
        persona=(
            "You are a voice assistant probe. Greet briefly, answer briefly, "
            f"speak {args.language}."
        ),
    )
    await session.start()
    print("connected + session.updated")

    audio_bytes = 0

    async def reader():
        nonlocal audio_bytes
        async for evt in session.events():
            if evt.type == "audio":
                audio_bytes += len(evt.audio or b"")
            else:
                print(f"[{evt.type}] role={evt.role} final={evt.final} "
                      f"text={evt.text!r} call={evt.call_id} args={evt.arguments} "
                      f"msg={evt.message}")

    task = asyncio.create_task(reader())
    await session.send_text_turn("Say hello in one short sentence.")
    await asyncio.sleep(6)

    if args.wav:
        with wave.open(args.wav, "rb") as w:
            assert w.getframerate() == 24000 and w.getnchannels() == 1, \
                "probe wants 24kHz mono pcm16"
            data = w.readframes(w.getnframes())
        # Trailing SILENCE is load-bearing: server VAD closes a turn on
        # silence_duration_ms of quiet audio, not on the stream stopping. A
        # file that ends mid-breath never ends its turn — pad ~1.5s of zeros.
        data += b"\x00\x00" * (24000 * 3 // 2)
        chunk = 24000 * 2 // 25  # 40ms
        for i in range(0, len(data), chunk):
            await session.send_audio(data[i:i + chunk])
            await asyncio.sleep(0.04)
        await asyncio.sleep(12)

    await session.close()
    await asyncio.sleep(0.2)
    task.cancel()
    print(f"total reply audio bytes: {audio_bytes}")


if __name__ == "__main__":
    asyncio.run(main())
