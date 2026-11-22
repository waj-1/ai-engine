"""A deterministic in-process realtime provider for voice eval runs.

Registered under slug ``simulated`` so an eval run can drive the REAL
``/voice/live`` endpoint — state machines, delegation, ``to_speakable`` and
all — with zero provider network and zero audio cost. It models what a
conforming provider does with the engine's stimuli:

  first send_text_turn (the greeting trigger)  -> assistant greeting + turn_end
  later send_text_turn (a caller utterance)    -> user transcript + ask_agent
                                                  tool_call
  send_tool_result                             -> assistant transcript of
                                                  payload["spoken"] + a token
                                                  silence audio delta + turn_end
  speak_out_of_band                            -> assistant transcript

``supports_streamed_tool_results`` is deliberately False (azure_voice_live
posture): the endpoint then delivers exactly ONE tool result per turn, so the
session emits exactly one ``turn_end`` per logical turn — which is what the
eval driver's turn detection keys on.

The first-text-turn-is-greeting convention assumes the profile configures
``voice_config.live.greeting_trigger`` (the client demos all do). A profile
without one would have the caller's first utterance swallowed as the
greeting — documented in docs/admin-portal/03-test.md.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict, List, Optional

from voice.live.base import (
    ASK_AGENT_TOOL_SPEC,
    AudioFormat,
    RealtimeEvent,
    RealtimeSession,
    ToolResultMode,
)
from voice.live.factory import register_realtime_provider

_SENTINEL = object()

# 10ms of 24kHz mono pcm16 silence — a token audio delta so the endpoint's
# forwarded-audio bookkeeping (playback cursor, budget paths) runs in sim.
_SILENCE_10MS = b"\x00\x00" * 240


class SimulatedRealtimeSession(RealtimeSession):
    provider = "simulated"
    tool_spec = ASK_AGENT_TOOL_SPEC
    # One tool result per call id, delivered whole — see module docstring.
    supports_streamed_tool_results = False
    supports_out_of_band_speech = True
    supports_barge_in_confirmation = False
    # It DOES finalize: every simulated caller utterance is emitted as one
    # `final=True` user transcript (see send_text_turn), so the endpoint's
    # commit-keyed machinery — relay custody, the stall tiers, the blank-call
    # recovery — is genuinely exercised by an eval run. Declared because
    # supports_barge_in_confirmation = False above must not be read as "no
    # finals": an eval run that lost custody would stop exercising the
    # protected-flow path entirely.
    finalizes_user_transcripts = True
    # No wire to read: this session speaks its tool results synchronously, so
    # it can never report a response in flight and the endpoint must not arm
    # the answer watchdog against a floor it cannot see.
    reports_response_in_flight = False

    GREETING = "Hello! How can I help you today?"

    def __init__(self, cfg: Any = None, persona: str = "") -> None:
        self.audio_format = AudioFormat(
            input_sample_rate=24000, output_sample_rate=24000
        )
        self.cfg = cfg
        self.persona = persona
        self.sent_audio_bytes = 0
        self.text_turns: List[str] = []
        self.out_of_band: List[str] = []
        self.started = False
        self.closed = False
        self._greeted = False
        self._call_seq = 0
        self._queue: asyncio.Queue = asyncio.Queue()

    def _emit(self, event: RealtimeEvent) -> None:
        self._queue.put_nowait(event)

    async def start(self) -> None:
        self.started = True

    async def send_audio(self, pcm: bytes) -> None:
        self.sent_audio_bytes += len(pcm)

    async def send_text_turn(self, text: str) -> None:
        self.text_turns.append(text)
        if not self._greeted:
            # By protocol order the first text turn is the endpoint's
            # greeting trigger (sent right after the ready frame).
            self._greeted = True
            self._emit(RealtimeEvent(
                type="transcript", role="assistant",
                text=self.GREETING, final=True,
            ))
            self._emit(RealtimeEvent(type="turn_end"))
            return
        self._call_seq += 1
        self._emit(RealtimeEvent(
            type="transcript", role="user", text=text, final=True,
        ))
        self._emit(RealtimeEvent(
            type="tool_call",
            call_id=f"sim-{self._call_seq}",
            tool_name=self.tool_spec.name,
            arguments={self.tool_spec.query_arg: text},
        ))

    async def send_tool_result(
        self, call_id: str, payload: Dict[str, Any], *, mode: ToolResultMode
    ) -> None:
        if not isinstance(mode, ToolResultMode):
            raise TypeError(
                f"mode must be a ToolResultMode, got {type(mode).__name__}"
            )
        spoken = str((payload or {}).get("spoken") or "")
        if spoken:
            self._emit(RealtimeEvent(
                type="transcript", role="assistant", text=spoken, final=True,
            ))
            self._emit(RealtimeEvent(type="audio", audio=_SILENCE_10MS, reply=True))
        self._emit(RealtimeEvent(type="turn_end"))

    async def speak_out_of_band(self, text: str) -> None:
        self.out_of_band.append(text)
        # Marked out_of_band so the endpoint's transcript forward can stamp
        # the WS frame — otherwise a sim holding line is indistinguishable
        # from the answer, and the eval driver clocks `progress_after_sec`
        # as the turn's latency no matter how slow the real answer is.
        self._emit(RealtimeEvent(
            type="transcript", role="assistant", text=text, final=True,
            out_of_band=True,
        ))

    async def events(self) -> AsyncIterator[RealtimeEvent]:
        while True:
            item = await self._queue.get()
            if item is _SENTINEL:
                return
            yield item

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._queue.put_nowait(_SENTINEL)


def _build_simulated(cfg: Any, persona: str) -> SimulatedRealtimeSession:
    return SimulatedRealtimeSession(cfg, persona=persona)


register_realtime_provider("simulated", _build_simulated)
