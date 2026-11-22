"""Stream a WAV at the duplex voice edge and dump what comes back.

Not part of the pytest suite — needs a running engine, a real provider key,
and a tenant with voice_config.live.enabled = true. Not auto-discovered by
pytest anyway (`pytest.ini`'s `testpaths = tests` never looks in
`manual_tests/`), but it's also not shaped like a test: it drives a live
WebSocket and prints frames rather than asserting anything.

The WS route is mounted under the ACTIVE deployment scope's portal prefix
(see `_mount_prefix()` in `main.py` and `core/deployment.py`), or at a bare
`/voice/live` on a full build. Point `--url` at whichever one the target
engine is running as.

    python manual_tests/test_voice_live.py \
        --url ws://localhost:8000/voice/live \
        --wav /path/to/a-16bit-mono-16kHz-question.wav \
        --agent-kind custom --business-id <uuid>

Prints every JSON frame and writes the returned audio to reply.pcm. Play it
with:  ffplay -f s16le -ar <rate from the ready frame> -ac 1 reply.pcm

Exit code reflects what happened, so this is scriptable: 0 on a clean run,
1 if the server closed the connection (before `ready` or mid-session — the
close code/reason are always printed, never swallowed), 2 if the supplied
WAV's sample rate doesn't match what the server negotiated and
--force-mismatched-rate wasn't passed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import wave

import websockets

# `endpoints/voice_live.py`'s own module docstring explains this one: a
# `websocket.close()` called BEFORE `websocket.accept()` is translated by
# ASGI into a bare HTTP 403 handshake rejection, so the application close
# code/reason never reaches the client — it just sees an abnormal closure.
# The endpoint is written so every rejection happens AFTER accept() (real
# close code, real reason), but a 1006 here still means something upstream
# ate the close frame — worth saying out loud rather than leaving the
# operator to guess "did the server crash?".
_CLOSE_CODE_NOTES = {
    1006: (
        "no close frame reached the client at all. Either a rejection "
        "happened before the server's websocket.accept() (ASGI turns "
        "close-before-accept into a bare HTTP 403 -- ends up looking just "
        "like this), or the connection was dropped/reset (proxy timeout, "
        "server crash) before any application close code could be sent. "
        "Check server logs; don't assume the process is fine."
    ),
}


def _closed_message(ws: websockets.ClientConnection) -> str:
    """Render whatever close info the connection object has, uniformly.

    `ws.close_code` / `ws.close_reason` are populated on the connection as
    soon as the closing handshake (or an abnormal closure) completes --
    true whether we get here via an exception on the very first `recv()`
    (server rejected before ever sending `ready`) or via one raised out of
    the main receive loop (server closed mid-session). Same formatting
    either way so a scripted caller can grep one pattern.
    """
    reason = ws.close_reason or ""
    note = _CLOSE_CODE_NOTES.get(ws.close_code)
    msg = f"CLOSED code={ws.close_code} reason={reason!r}"
    if note:
        msg += f"\n  ({note})"
    return msg


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="e.g. ws://localhost:8000/voice/live")
    parser.add_argument("--wav", required=True, help="16-bit mono WAV to stream")
    parser.add_argument("--agent-kind", default="custom")
    parser.add_argument("--business-id", default="")
    parser.add_argument("--session-id", default="", help="optional; server derives one when omitted")
    parser.add_argument("--locale", default="", help="optional, e.g. en / ar")
    parser.add_argument("--token", default="", help="optional bearer JWT")
    parser.add_argument("--out", default="reply.pcm")
    parser.add_argument(
        "--force-mismatched-rate",
        action="store_true",
        help=(
            "Stream the WAV even if its sample rate doesn't match the "
            "server's negotiated ready.audio.input_sample_rate. Default is "
            "to refuse and exit(2): the server decodes mismatched-rate PCM "
            "as noise, which reads as 'the agent doesn't understand me' "
            "rather than the real, boring problem."
        ),
    )
    args = parser.parse_args()

    url = f"{args.url}?agentKind={args.agent_kind}&businessId={args.business_id}"
    if args.session_id:
        url += f"&sessionId={args.session_id}"
    if args.locale:
        url += f"&locale={args.locale}"
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}

    ok = True

    async with websockets.connect(url, additional_headers=headers) as ws:
        try:
            ready = json.loads(await ws.recv())
        except websockets.ConnectionClosed:
            # Every pre-`ready` rejection in the endpoint -- voice disabled,
            # daily budget exhausted, connect rate-limited, agentKind not
            # served by this deployment, voice config lookup failure --
            # closes the socket before ever sending `ready`. Without this,
            # that surfaces as an unhandled ConnectionClosedError traceback
            # instead of the diagnostic the script exists to produce.
            print(_closed_message(ws))
            return 1
        print("READY", json.dumps(ready, indent=2))
        rate = ready["audio"]["input_sample_rate"]

        with wave.open(args.wav, "rb") as wav:
            assert wav.getnchannels() == 1, "mono only"
            assert wav.getsampwidth() == 2, "16-bit only"
            wav_rate = wav.getframerate()
            if wav_rate != rate and not args.force_mismatched_rate:
                print(
                    f"ERROR: {args.wav} is {wav_rate}Hz but the server "
                    f"negotiated {rate}Hz. Sending it anyway makes the "
                    f"model misdecode the audio -- it comes out looking "
                    f"like 'the agent doesn't understand me', not a rate "
                    f"mismatch, which is a much harder bug to chase.\n"
                    f"  Convert first:  ffmpeg -i {args.wav} -ar {rate} -ac 1 "
                    f"-sample_fmt s16 fixed.wav\n"
                    f"  Or pass --force-mismatched-rate to send the raw file "
                    f"anyway."
                )
                return 2
            # ~100ms per send, roughly real time, so server VAD behaves.
            chunk_frames = rate // 10
            while True:
                frames = wav.readframes(chunk_frames)
                if not frames:
                    break
                await ws.send(frames)
                await asyncio.sleep(0.1)

        audio = bytearray()
        try:
            async with asyncio.timeout(45):
                async for message in ws:
                    if isinstance(message, bytes):
                        audio.extend(message)
                    else:
                        print("FRAME", message)
        except asyncio.TimeoutError:
            # No message for 45s while the socket is still open -- the
            # expected, quiet end of a healthy smoke run (the server has
            # nothing more to say and neither side hung up).
            pass
        except websockets.ConnectionClosed:
            # The exact bug this fix closes: the server can close
            # mid-session (e.g. CLOSE_PROVIDER_ERROR after streaming one
            # audio frame) and the old code swallowed it identically to the
            # TimeoutError case above -- "wrote N bytes to reply.pcm" either
            # way, with N sometimes non-zero, looking exactly like success.
            print(_closed_message(ws))
            ok = False

    with open(args.out, "wb") as handle:
        handle.write(audio)
    print(f"wrote {len(audio)} bytes of PCM to {args.out}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
