"""A fake provider plus the conformance suite every provider must pass.

This ships in-package on purpose: it is what makes adding the Azure provider
a fill-in rather than a rewrite, and it lets the whole WebSocket handler be
tested with no network.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

from voice.live.base import (
    ASK_AGENT_TOOL_SPEC,
    AudioFormat,
    RealtimeEvent,
    RealtimeSession,
    ToolResultMode,
)

_SENTINEL = object()


class FakeRealtimeSession(RealtimeSession):
    """Scriptable in-process provider.

    Pass `script` to replay a fixed sequence, and/or call `push()` to inject
    events while a test is running. `close()` ends the event iterator.
    """

    provider = "fake"
    # Explicit, not inherited: RealtimeSession.tool_spec has no default (see
    # base.py) precisely so a subclass that forgets to set it fails the
    # conformance suite instead of silently passing. This fake session
    # exists to model a CONFORMING provider, so it sets the canonical
    # contract itself rather than relying on any inherited value.
    tool_spec = ASK_AGENT_TOOL_SPEC
    # Same reasoning as tool_spec above: this fake models a CONFORMING
    # provider, so it declares the capability explicitly (True -- it can
    # take several tool results per call id) rather than relying on
    # anything inherited.
    supports_streamed_tool_results = True
    # …and, unlike Gemini Live, it transcribes them — the fake models the
    # fully-capable provider, so the screen keeps reading the provider by
    # default and a test that wants the Gemini shape (server-authored screen
    # text) flips this off explicitly.
    transcribes_streamed_tool_results = True
    # The fake models a fully-capable provider; out-of-band speech is recorded
    # rather than spoken so endpoint tests can assert on it.
    supports_out_of_band_speech = True
    # The fake models a fully-capable provider (azure_voice_live posture):
    # it delivers final user transcripts, so the endpoint's confirmed-
    # barge-in machinery is exercised by default in endpoint tests.
    supports_barge_in_confirmation = True
    # Same posture: the fake delivers a final user transcript per utterance,
    # so endpoint tests exercise relay custody AND the commit-keyed stall
    # tiers by default. A test modelling Gemini Live shadows this with
    # False on the instance.
    finalizes_user_transcripts = True
    # The fake models a fully-capable provider: the endpoint may hand it the
    # floor again after a stall (see RealtimeSession.reprompt).
    supports_reprompt = True
    # Live state, not a capability (see RealtimeSession.response_in_flight).
    # A plain class attribute deliberately shadows the base property so an
    # endpoint test can drive it — `session.response_in_flight = True` models
    # a provider with a response on the wire, which is what the answer
    # watchdog must not mistake for a mute answer.
    response_in_flight = False
    # ...and the fake DECLARES that the attribute above is a real reading, so
    # the endpoint arms the answer watchdog against it. Without this the fake
    # would stop modelling a fully-capable provider the moment the watchdog
    # became capability-gated, and every watchdog test in
    # tests/voice/test_voice_live_endpoint.py would go green by never running
    # the code under test.
    reports_response_in_flight = True

    def __init__(
        self,
        script: Optional[List[RealtimeEvent]] = None,
        *,
        audio_format: Optional[AudioFormat] = None,
    ) -> None:
        self.audio_format = audio_format or AudioFormat(
            input_sample_rate=16000, output_sample_rate=24000
        )
        self.sent_audio: List[bytes] = []
        self.text_turns: List[str] = []
        self.tool_results: List[Tuple[str, Dict[str, Any], ToolResultMode]] = []
        self.out_of_band: List[str] = []
        self.playback_reports: List[float] = []
        # Relay-mode custody cancels the model's in-flight response before
        # substituting a relayed delivery — recorded so endpoint tests can
        # assert the model was actually silenced.
        self.cancelled = 0
        # Confirmed-barge-in machinery (endpoints/voice_live.py): recorded so
        # endpoint tests can assert exactly which cancel path fired.
        self.barge_in_cancels = 0
        self.spurious_suppressions = 0
        # Stall re-prompts the endpoint asked for, and what the model answers
        # with. A test sets `reprompt_reply` to the event a re-prompted model
        # produces (live: the ask_agent it owed, ~0.4s later); left None the
        # fake models the model that stays silent even when asked.
        self.reprompts = 0
        self.reprompt_reply: Optional[RealtimeEvent] = None
        self.started = False
        self.closed = False
        self._queue: asyncio.Queue = asyncio.Queue()
        for event in script or []:
            self._queue.put_nowait(event)
        self._auto_close = script is not None

    async def start(self) -> None:
        self.started = True
        # Captured for push(): endpoint tests call push() from the
        # TestClient's TEST thread while the app's event loop runs in a
        # worker thread. asyncio.Queue is not thread-safe and, worse, a
        # cross-thread put_nowait does not WAKE an idle loop — on a quiet
        # CI runner the pushed event sat unprocessed until teardown, whose
        # cancellation then failed the test (locally, incidental socket
        # traffic kept waking the loop and hid it).
        self._loop = asyncio.get_running_loop()
        if self._auto_close:
            # A fixed script ends on its own so `async for` terminates.
            self._queue.put_nowait(_SENTINEL)

    def push(self, event: RealtimeEvent) -> None:
        loop = getattr(self, "_loop", None)
        if loop is not None and not loop.is_closed():
            try:
                if asyncio.get_running_loop() is loop:
                    self._queue.put_nowait(event)
                    return
            except RuntimeError:
                pass  # no running loop on this thread -> cross-thread push
            loop.call_soon_threadsafe(self._queue.put_nowait, event)
            return
        self._queue.put_nowait(event)

    async def send_audio(self, pcm: bytes) -> None:
        self.sent_audio.append(pcm)

    async def send_text_turn(self, text: str) -> None:
        self.text_turns.append(text)

    async def reprompt(self) -> bool:
        self.reprompts += 1
        reply, self.reprompt_reply = self.reprompt_reply, None
        if reply is not None:
            self.push(reply)
        return True

    async def speak_out_of_band(self, text: str) -> None:
        self.out_of_band.append(text)
        # A conforming provider REPORTS the line once its upstream has
        # committed to speaking it -- that report is what the endpoint now
        # logs the transcript from, instead of assuming an enqueued line was
        # said. This fake models the happy path (accepted immediately); a
        # test that wants the dropped case pushes no event.
        self.push(RealtimeEvent(type="out_of_band_started", text=text))

    async def report_playback(self, played_total_ms: float) -> None:
        self.playback_reports.append(played_total_ms)

    async def cancel_active_response(self) -> None:
        self.cancelled += 1

    async def cancel_barged_in_response(self) -> None:
        self.barge_in_cancels += 1

    async def suppress_spurious_reply(self) -> None:
        self.spurious_suppressions += 1

    async def send_tool_result(
        self, call_id: str, payload: Dict[str, Any], *, mode: ToolResultMode
    ) -> None:
        if not isinstance(mode, ToolResultMode):
            raise TypeError(f"mode must be a ToolResultMode, got {type(mode).__name__}")
        self.tool_results.append((call_id, payload, mode))

    async def events(self) -> AsyncIterator[RealtimeEvent]:
        if not self.started:
            # A scripted session's end-of-script sentinel is only queued by
            # start() (see there). Iterating before start() would otherwise
            # wait on a sentinel that never arrives -- fail loudly instead.
            raise RuntimeError(
                "FakeRealtimeSession.events() was iterated before start() "
                "was called -- call start() first"
            )
        while True:
            item = await self._queue.get()
            if item is _SENTINEL:
                return
            yield item

    def end_stream(self) -> None:
        """End events() the way a real provider's clean upstream close does.

        Sync on purpose, so a test driving the endpoint through the (sync)
        TestClient can trigger it mid-session — the same reason `push()` is
        sync. Distinct from `close()`: this is the SERVICE hanging up, not us
        tearing the session down, and the endpoint treats the two differently.
        """
        self._queue.put_nowait(_SENTINEL)

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._queue.put_nowait(_SENTINEL)


# Bounds every wait in the contract below. A conforming provider answers
# in-process (fake) or against a stub transport (real provider under test),
# so this is generous, not a real-network budget. Its job is to turn "a
# hollow provider hangs forever" into "the contract fails in under a second."
_CONTRACT_PROBE_TIMEOUT = 0.5


async def _drain_until_end(iterator: AsyncIterator[RealtimeEvent], timeout: float) -> None:
    """Consume whatever is left of `iterator`, but never hang.

    A provider whose `close()` doesn't actually terminate `events()` is just
    as broken as one that never emits anything -- both must fail the
    contract instead of blocking the caller forever.
    """
    try:
        while True:
            await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
    except StopAsyncIteration:
        return
    except asyncio.TimeoutError:
        raise AssertionError(
            "close() must terminate events() -- the iterator did not end "
            f"within {timeout}s of close() being called"
        )


async def assert_realtime_contract(
    make_session: Callable[[], RealtimeSession]
) -> None:
    """Assert a provider honours the RealtimeSession contract.

    Every provider — Gemini today, Azure later — must pass this. Call it from
    that provider's own test module with a factory that returns an unstarted
    session wired to a stub transport.

    This is a behavioural gate, not just a shape check: a "hollow" provider
    that connects, does nothing, and disconnects (all methods no-ops,
    `events()` never yields) must fail here. That is the whole point of the
    suite -- it is what catches a half-built provider months from now,
    before a WIP implementation ships silently green.
    """
    session = make_session()

    fmt = session.audio_format
    assert isinstance(fmt, AudioFormat), "audio_format must be declared"
    assert fmt.input_sample_rate > 0 and fmt.output_sample_rate > 0
    assert isinstance(session.provider, str) and session.provider

    # The tool contract is cross-provider, not a per-provider choice: the WS
    # handler (`endpoints/voice_live.py::_handle_tool_call`) reads
    # `event.arguments.get(ASK_AGENT_TOOL_SPEC.query_arg)` with no fallback,
    # and the persona prompt names `ask_agent` explicitly for every provider.
    # Before this assertion existed, nothing here checked that a provider's
    # OWN declared tool actually matched -- a provider declaring e.g.
    # `ask_assistant(question=...)` would have passed this suite and
    # then silently delivered an empty query to the agent on every real call.
    tool_spec = getattr(session, "tool_spec", None)
    assert tool_spec is not None, (
        "RealtimeSession.tool_spec must be set -- see ASK_AGENT_TOOL_SPEC in voice/live/base.py"
    )
    assert tool_spec.name == ASK_AGENT_TOOL_SPEC.name, (
        f"provider declared tool name {tool_spec.name!r}, but the shared "
        f"contract requires {ASK_AGENT_TOOL_SPEC.name!r} -- "
        "endpoints/voice_live.py's _handle_tool_call and the persona prompt "
        "both assume this exact name"
    )
    assert tool_spec.query_arg == ASK_AGENT_TOOL_SPEC.query_arg, (
        f"provider declared its tool's argument as {tool_spec.query_arg!r}, "
        f"but the shared contract requires {ASK_AGENT_TOOL_SPEC.query_arg!r} "
        "-- endpoints/voice_live.py's _handle_tool_call reads exactly that "
        "key out of event.arguments with no fallback"
    )

    streamed_flag = getattr(session, "supports_streamed_tool_results", None)
    assert isinstance(streamed_flag, bool), (
        "RealtimeSession.supports_streamed_tool_results must be set to a bool -- "
        "the endpoint refuses per-sentence delivery on providers that declare False"
    )

    oob_flag = getattr(session, "supports_out_of_band_speech", None)
    assert isinstance(oob_flag, bool), (
        "RealtimeSession.supports_out_of_band_speech must be set to a bool -- "
        "the endpoint checks it before speaking the holding line during a "
        "delegated turn"
    )

    in_flight = getattr(session, "response_in_flight", None)
    assert isinstance(in_flight, bool), (
        "RealtimeSession.response_in_flight must read as a bool -- the "
        "endpoint's answer watchdog asks it before re-delivering an answer, "
        "and anything truthy-but-not-bool (a coroutine from an accidental "
        "`async def`, say) would make it stand down forever and lose the "
        "mute-race rescue entirely"
    )

    finalizes = getattr(session, "finalizes_user_transcripts", None)
    assert isinstance(finalizes, bool), (
        "RealtimeSession.finalizes_user_transcripts must be set to a bool -- "
        "it is the single declared answer to 'does this provider close an "
        "utterance with a FINAL user transcript', and the endpoint's whole "
        "commit-keyed layer keys on it: relay custody suppresses the model's "
        "own ask_agent and dispatches its replacement from that final, and "
        "the stall tiers and blank-call recovery hang off the same commit. A "
        "provider that leaves this unanswered would have custody swallow the "
        "caller's turn"
    )

    in_flight_flag = getattr(session, "reports_response_in_flight", None)
    assert isinstance(in_flight_flag, bool), (
        "RealtimeSession.reports_response_in_flight must be a bool -- it is "
        "what tells the endpoint whether `response_in_flight` above is a real "
        "reading of the wire or the base default, and the answer watchdog is "
        "armed only where it is real"
    )
    if in_flight_flag:
        # Declaring it is what arms the answer watchdog on this provider. The
        # base property returns False unconditionally, so a provider that
        # declares the capability without overriding it hands the watchdog a
        # permanently-idle floor -- exactly the unbraked state that spoke the
        # same answer twice on 2026-08-05 -- while looking configured.
        assert (
            type(session).response_in_flight is not RealtimeSession.response_in_flight
        ), (
            f"{type(session).__name__} declares reports_response_in_flight = "
            "True but does not override response_in_flight -- the base default "
            "always reads False, so the answer watchdog would judge every "
            "delivered answer against a floor it believes is idle and "
            "re-deliver answers that were merely still on their way."
        )

    if getattr(session, "supports_barge_in_confirmation", False):
        # Confirmation is the strictly stronger claim: it needs the final user
        # transcript AND a playback_flush on commit. A provider declaring it
        # without the final is describing a contract it cannot deliver -- the
        # endpoint would either wait for a verdict that never comes or refuse
        # custody it could actually take.
        assert finalizes, (
            f"{type(session).__name__} declares supports_barge_in_confirmation "
            "= True but finalizes_user_transcripts = False. The confirmation "
            "contract IS a verdict read off the final user transcript, so the "
            "stronger flag cannot hold without the weaker one."
        )
        # Declaring the flag opts the provider into the endpoint's confirmed-
        # barge-in path, which awaits both cancels from INSIDE its
        # `async for event in session.events()` pump body. The base defaults
        # in RealtimeSession delegate to `cancel_active_response`, which
        # SETTLES (`_await_idle`) -- and a settle can never complete while the
        # pump that would deliver the settling event is blocked awaiting it,
        # so it burns its full timeout on every barge-in, stalling the flush
        # frame and all audio/transcript forwarding behind it. A confirming
        # provider MUST ship its own non-settling overrides.
        _base_msg = (
            "declares supports_barge_in_confirmation = True but does not "
            "override {name}() -- the RealtimeSession base default delegates "
            "to the SETTLING cancel_active_response, and the endpoint awaits "
            "this from inside its events() pump, where the settle can never "
            "resolve and freezes the stream for its whole timeout. Ship a "
            "non-settling override (see "
            "voice/live/providers/azure_voice_live.py)."
        )
        assert (
            type(session).cancel_barged_in_response
            is not RealtimeSession.cancel_barged_in_response
        ), f"{type(session).__name__} " + _base_msg.format(
            name="cancel_barged_in_response"
        )
        assert (
            type(session).suppress_spurious_reply
            is not RealtimeSession.suppress_spurious_reply
        ), f"{type(session).__name__} " + _base_msg.format(
            name="suppress_spurious_reply"
        )

    if getattr(session, "supports_reprompt", False):
        # Declaring the flag is what makes the endpoint's stall tripwire hand
        # the floor back instead of sitting out its recovery grace. The base
        # default returns False unconditionally, so a provider that declares
        # the capability without implementing it would silently take the slow
        # path forever — no error, just every stall costing ~10s again.
        assert type(session).reprompt is not RealtimeSession.reprompt, (
            f"{type(session).__name__} declares supports_reprompt = True but "
            "does not override reprompt() -- the base default always returns "
            "False, so the endpoint would fall through to the recovery grace "
            "on every stall while looking configured."
        )

    await session.start()
    await session.send_audio(b"\x00\x00")
    # Every provider must be able to inject a text turn -- it is how the
    # endpoint makes the assistant greet first (see base.send_text_turn).
    await session.send_text_turn("contract probe")

    # Distinct call id per mode: a provider is free to accept only ONE result
    # per call id (azure_voice_live does, by design -- see
    # supports_streamed_tool_results=False there). Reusing one id across all
    # three sends would make every such provider fail this provider-neutral
    # contract on its second send, even though nothing in the contract is
    # supposed to depend on multi-result-per-call support -- that capability
    # is exactly what the flag above exists to gate, separately.
    for mode in ToolResultMode:
        await session.send_tool_result(
            f"call-contract-{mode.value}", {"spoken": "ok"}, mode=mode
        )

    # ToolResultMode subclasses str, so `"interrupt" == ToolResultMode.INTERRUPT`
    # is True even though `isinstance` is False. A provider that only does
    # string comparisons (not isinstance checks) could silently misroute a
    # malformed value instead of rejecting it. Require real rejection.
    try:
        await session.send_tool_result("call-contract-typecheck", {}, mode="interrupt")
    except TypeError:
        pass
    else:
        raise AssertionError(
            "send_tool_result must reject a mode that is not a ToolResultMode "
            "with TypeError -- string equality with a ToolResultMode value is "
            "not enough"
        )

    iterator = session.events()
    assert hasattr(iterator, "__anext__"), "events() must return an async iterator"

    if hasattr(session, "push"):
        # The session declares event-injection support (FakeRealtimeSession
        # does). Prove events() is actually wired to it: push a marker no
        # real event would ever collide with, and require it to surface.
        # This is the strongest form of the check, but it only applies to
        # sessions that expose this test-only capability -- a real provider
        # under a stub transport won't have `push` and falls to the branch
        # below instead.
        marker = RealtimeEvent(type="__contract_probe__")
        session.push(marker)
        seen = 0
        found = False
        deadline = asyncio.get_running_loop().time() + _CONTRACT_PROBE_TIMEOUT
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                event = await asyncio.wait_for(iterator.__anext__(), timeout=remaining)
            except (asyncio.TimeoutError, StopAsyncIteration):
                break
            seen += 1
            if event is marker:
                found = True
                break
        assert found, (
            "session exposes push() but events() never delivered the pushed "
            f"event within {_CONTRACT_PROBE_TIMEOUT}s (saw {seen} other "
            "event(s) first) -- a provider that connects and does nothing "
            "must not satisfy this contract"
        )
    else:
        # No injection hook available (e.g. a real/stub-backed provider). We
        # can't force an event, but a genuinely open session must not have
        # already ended on its own -- a hollow provider whose events() never
        # yields anything, ever, fails here instead of passing silently.
        try:
            await asyncio.wait_for(iterator.__anext__(), timeout=_CONTRACT_PROBE_TIMEOUT)
        except asyncio.TimeoutError:
            pass  # legitimately idle and still open -- fine
        except StopAsyncIteration:
            raise AssertionError(
                "events() ended before close() was ever called -- a session "
                "that never reports anything (a provider that connects and "
                "does nothing) must not satisfy this contract"
            )

    # close() must actually terminate events(), and must be idempotent.
    await session.close()
    await _drain_until_end(iterator, _CONTRACT_PROBE_TIMEOUT)
    await session.close()
