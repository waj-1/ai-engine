"""Manual probe: does Gemini Live TRANSCRIBE an answer delivered sentence by
sentence?

Accepting several tool results for one call id is not the same as transcribing
the speech generated from them, and only the first of those was ever verified.
This probe measures the second, because it is what decides whether a caller
watching a Live tab that mirrors the transcript reads what she hears: it delivers one answer two
ways — as N separate tool results (`stream_sentences`, the shape
`endpoints/voice_live.py::_speak_sentence` produces) and as ONE — and prints
the wire order plus the joined `output_transcription` against the text actually
delivered.

    GOOGLE_API_KEY=... venv/bin/python \\
        manual_tests/probe_gemini_streamed_transcription.py            # streamed
    GOOGLE_API_KEY=... venv/bin/python \\
        manual_tests/probe_gemini_streamed_transcription.py --whole    # control
    ... --raw     # bypass the SDK's per-turn generator break entirely

This is why `GeminiLiveSession.transcribes_streamed_tool_results = False`, which
does NOT turn streaming off — it moves the on-screen text for a streamed answer
to the endpoint's own copy of each sentence (`answer_echo` in
`endpoints/voice_live.py`). Re-run this before flipping that flag, and re-run
the `--whole` control in the same session — the finding is a DIFFERENCE between
the two shapes, and either one alone proves nothing.

RESULTS, 2026-08-19, `gemini-3.1-flash-live-preview`, replaying the answer from
that morning's client call (`أبشر، ولا تشيل هم. هل معك رقم الطرفية للمشكلة، ولا
أفتحها على أي فرع؟`) as the four chunks voice/chunker.py cuts it into — which
is `streamed=4`, exactly what the demo VM's own metrics logged for that turn:

    delivered                     transcribed
    أبشر،                          أبشر،
    ولا تشيل هم.                    ولا تشيل            <-- truncated
    هل معك رقم الطرفية للمشكلة،      هل معك              <-- truncated
    ولا أفتحها على أي فرع؟           ولا أفتحها          <-- truncated

    --whole:  transcript == delivered answer, BYTE-IDENTICAL, 4 runs of 4.

The audio speaks every word in both shapes; only the transcript differs. The
mechanism is visible in the wire order this prints: transcription lags the
generated audio by ~1s, and each response's outstanding remainder is abandoned
when that response's generation ends — a 1-2s response never gets it out,
while the single long response has time to finish. `--raw` reproduces the loss
identically, so it is upstream, not the SDK's `receive()` breaking its
generator on every `turn_complete`.

Nothing here writes anywhere: one Live session, closed on exit.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google import genai
from google.genai import types as gt

# The persona only has to do one thing: delegate, then read the tool result
# back verbatim. Anything more elaborate makes the model embellish, and an
# embellished response transcribes its OWN words — which measures nothing.
PERSONA = (
    "أنتِ مساعدة صوتية سعودية لهذا النشاط التجاري. لا تجيبين على أسئلة البيانات بنفسك: "
    "استخدمي أداة ask_agent فوراً. بعد ما يرجع لك نص `spoken` اقرئيه حرفياً كما هو "
    "بدون تغيير ولا إضافة."
)

DECL = {
    "name": "ask_agent",
    "description": "Ask the agent to handle the caller's request.",
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}

# voice/chunker.py cuts on . ! ? ؟ ، and newlines, so the Arabic comma makes
# four chunks out of this one answer. Kept verbatim rather than parameterised:
# it is the exact string from the 2026-08-19 07:58 call the finding came from.
SENTENCES = [
    "أبشر،",
    "ولا تشيل هم.",
    "هل معك رقم الطرفية للمشكلة،",
    "ولا أفتحها على أي فرع؟",
]

T0 = time.monotonic()


def _ts() -> str:
    return f"{(time.monotonic() - T0):7.3f}"


async def _run(model: str, sentences: list[str], raw: bool) -> int:
    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    config = gt.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=PERSONA,
        speech_config=gt.SpeechConfig(
            voice_config=gt.VoiceConfig(
                prebuilt_voice_config=gt.PrebuiltVoiceConfig(voice_name="Aoede")
            ),
            language_code="ar",
        ),
        input_audio_transcription=gt.AudioTranscriptionConfig(),
        output_audio_transcription=gt.AudioTranscriptionConfig(),
        session_resumption=gt.SessionResumptionConfig(),
        tools=[{"function_declarations": [dict(DECL, behavior="NON_BLOCKING")]}],
    )

    events: list[tuple[str, str, str]] = []
    transcript: list[str] = []
    turn_completes: list[str] = []

    async with client.aio.live.connect(model=model, config=config) as session:
        await session.send_client_content(
            turns={"role": "user", "parts": [{"text": "افتح لي تكت صيانة"}]},
            turn_complete=True,
        )

        delivered = False
        deadline = time.monotonic() + 90

        def _note(message) -> None:
            nonlocal delivered
            sc = getattr(message, "server_content", None)
            if sc is not None:
                otx = getattr(sc, "output_transcription", None)
                if otx is not None:
                    text = getattr(otx, "text", None) or ""
                    transcript.append(text)
                    events.append((
                        _ts(), "out_tx",
                        f"finished={getattr(otx, 'finished', None)!r} {text!r}",
                    ))
                turn = getattr(sc, "model_turn", None)
                frames = sum(
                    1 for part in getattr(turn, "parts", None) or []
                    if getattr(getattr(part, "inline_data", None), "data", None)
                )
                if frames:
                    events.append((_ts(), "audio", f"{frames} part(s)"))
                if getattr(sc, "generation_complete", False):
                    events.append((_ts(), "GENERATION_COMPLETE", ""))
                if getattr(sc, "turn_complete", False):
                    turn_completes.append(_ts())
                    events.append((_ts(), "TURN_COMPLETE", f"#{len(turn_completes)}"))

        async def _deliver(call_id: str) -> None:
            nonlocal delivered
            delivered = True
            # Exactly what _speak_sentence does: one send_tool_response per
            # chunk, WHEN_IDLE (no holding line is spoken on a fast turn).
            for i, sentence in enumerate(sentences):
                await session.send_tool_response(
                    function_responses=[
                        gt.FunctionResponse(
                            id=call_id, name="ask_agent",
                            response={"spoken": sentence},
                            scheduling=gt.FunctionResponseScheduling.WHEN_IDLE,
                        )
                    ]
                )
                events.append((_ts(), "DELIVER", f"chunk{i + 1} {sentence!r}"))

        async def _handle(message) -> None:
            _note(message)
            for call in getattr(getattr(message, "tool_call", None),
                                "function_calls", None) or []:
                cid = getattr(call, "id", "") or ""
                events.append((_ts(), "TOOL_CALL", f"{call.name} id={cid}"))
                if not delivered:
                    await _deliver(cid)

        want = len(sentences) + 1   # the tool-call turn, then one per delivery
        if raw:
            # No `session.receive()`, so the SDK never breaks its generator on
            # a turn_complete. Isolates "is the loss ours or the service's?".
            while time.monotonic() < deadline:
                try:
                    message = await asyncio.wait_for(session._receive(), timeout=5.0)
                except asyncio.TimeoutError:
                    break
                if message is None:
                    break
                await _handle(message)
                if delivered and len(turn_completes) >= want:
                    break
        else:
            while time.monotonic() < deadline:
                saw = False
                async for message in session.receive():
                    saw = True
                    await _handle(message)
                if not saw or (delivered and len(turn_completes) >= want):
                    break
                await asyncio.sleep(0)

    print("\n================ WIRE ORDER ================")
    for stamp, kind, detail in events:
        print(f"{stamp}  {kind:20s} {detail}")

    joined = "".join(transcript)
    answer = "".join(sentences)
    print("\n================ SUMMARY ================")
    print(f"tool results delivered : {len(sentences)}")
    print(f"turn_complete count    : {len(turn_completes)} at {turn_completes}")
    print(f"transcript joined      : {joined!r}")
    print(f"answer delivered       : {answer!r}")
    verdict = "COMPLETE" if joined.replace(" ", "") == answer.replace(" ", "") else "TRUNCATED"
    print(f"verdict                : {verdict}")
    return 0 if verdict == "COMPLETE" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="gemini-3.1-flash-live-preview")
    parser.add_argument("--whole", action="store_true",
                        help="control: deliver the answer as ONE tool result")
    parser.add_argument("--raw", action="store_true",
                        help="read with a continuous _receive() loop instead of "
                             "session.receive(), which breaks per turn")
    args = parser.parse_args()
    if not os.environ.get("GOOGLE_API_KEY"):
        print("GOOGLE_API_KEY is required", file=sys.stderr)
        return 2
    sentences = ["".join(SENTENCES)] if args.whole else list(SENTENCES)
    return asyncio.run(_run(args.model, sentences, args.raw))


if __name__ == "__main__":
    raise SystemExit(main())
