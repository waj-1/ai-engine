"""Ordering-invariant tests for one caller turn on the duplex voice edge.

WHY THIS FILE EXISTS

The interrupt/recovery machinery is four mechanisms coordinating through
shared mutable dicts: confirm-window barge-in, three-tier stall recovery,
the answer-delivery watchdog, and relay custody. Roughly twenty fixes landed
against it in three days, and every one of them arrived with a regression test
scripting the exact event ORDER that had just broken. Those tests are correct
and worth keeping — but each pins one interleaving, so the next defect simply
appeared in a different one:

  * #26/#27/#29/#30  a question announced, truncated, or never dispatched
  * #34/#36/#37      a commit or hallucinated transcript truncating an answer
  * #42/#44/#32/#48  stall tiers firing against the persona, or too late
  * #52/#72          the confirm-latch defect: `tool_call` before its own
                     lagging `final`, over the agent (#52) and into silence
                     (#72 — the order this file listed as a strict xfail
                     until the endpoint could tell a final that BELONGS to an
                     in-flight answer from one that supersedes it)

That last one is the shape this file generalizes. On Azure the caller's
transcript rides a SEPARATE, SLOWER ASR pipeline than the model's events, so
for one single utterance the edge can observe `tool_call` first or `final`
first — a genuine race, not a rare one, and the losing order delivered the
caller's own answer as SILENT: dead air for a question they had just asked.

So rather than script one more order, these tests enumerate the orders that
one utterance can physically produce and assert the invariants that must hold
across ALL of them. A new defect in an un-scripted interleaving fails here
without anyone having written a test for that specific sequence.

THE PHYSICAL CONSTRAINT (why this is 4 cases and not 3! = 6)

`interrupted` is SPEECH ONSET. Both the model's `tool_call` and the ASR's
`final` are DERIVED from that speech, so onset cannot follow them for the same
utterance. Only `tool_call` and `final` genuinely race. Crossed with whether
the caller spoke over the agent (onset raises a pending interrupt) or into
silence (it does not), that is exactly four realizable orders.

Permuting all six would test two physically impossible sequences and, worse,
assert the wrong thing about them: an onset arriving AFTER a dispatch is a
NEW utterance superseding the old one, where SILENT is the correct outcome,
not a bug. The scope is deliberate — see `test_the_scope_of_these_invariants`.

THE INVARIANTS

For one utterance, whatever the arrival order:

  1. exactly one delivery for the call — never zero (a hung tool call), never
     two (the same answer spoken twice);
  2. the delivery is AUDIBLE — no newer utterance exists to supersede it, so
     SILENT here is always dead air;
  3. the agent is cancelled at most once — the once-per-utterance rule.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import endpoints.voice_live as live_ep
from voice.live.base import AudioFormat, RealtimeEvent, ToolResultMode
from voice.live.config import LiveVoiceConfig
from voice.live.delegate import DelegateResult
from voice.live.testing import FakeRealtimeSession

QUESTION = "how many terminals do I have"
ANSWER = "You have twelve."


@pytest.fixture(params=[0.0, 0.05], ids=["sustain-off", "sustain-on"])
def harness(monkeypatch, request):
    """Same wiring as tests/voice/test_voice_live_endpoint.py's fixture.

    Duplicated rather than imported: that fixture resets a module-level
    connection limiter shared across ITS file, and reaching across test
    modules for a fixture couples two files' rate budgets together.
    """
    live_ep._connect_limiter._store.clear()
    state = {}

    def _cfg(_conn, _kind):
        # Every enumeration also runs with the sustained-speech cut armed
        # (`barge_in_sustain_sec` > 0, the business setting): the invariants
        # must hold whether or not it exists (eval review 2026-09-30).
        return state.get(
            "cfg", LiveVoiceConfig(enabled=True, model_id="m-1", provider="fake",
                                   barge_in_sustain_sec=request.param)
        )

    monkeypatch.setattr(live_ep, "_live_config_for", _cfg)
    monkeypatch.setattr(live_ep, "resolve_channel", lambda conn: None)
    monkeypatch.setattr(live_ep, "enforce_channel_kind", lambda conn, kind: None)
    monkeypatch.setattr(
        live_ep, "authenticate_optional", lambda authz: ("tok", "user-1")
    )
    monkeypatch.setattr(live_ep, "reserve_audio_seconds", lambda *a, **kw: 0)
    monkeypatch.setattr(
        live_ep, "build_agent_for_kind", lambda kind, params: (object(), object())
    )
    monkeypatch.setattr(
        live_ep, "resolve_persona_prompt", lambda cfg, kind, **kw: "You are a voice agent."
    )

    session = FakeRealtimeSession(audio_format=AudioFormat(16000, 24000))
    state["sustain"] = request.param
    monkeypatch.setattr(live_ep, "_session_for", lambda cfg, **kw: session)
    state["session"] = session

    app = FastAPI()
    app.include_router(live_ep.router)
    return TestClient(app), state


def _read_until(ws, kind, limit=12):
    for _ in range(limit):
        frame = ws.receive_json()
        if frame.get("type") == kind:
            return frame
    raise AssertionError(f"no {kind!r} frame in {limit} frames")


def _tool_call_event(call_id="c1", query=QUESTION):
    return RealtimeEvent(
        type="tool_call",
        call_id=call_id,
        tool_name="ask_agent",
        arguments={"query": query},
    )


def _final_event():
    return RealtimeEvent(type="transcript", role="user", text=QUESTION, final=True)


# (id, spoke_over_agent, first_of_the_racing_pair)
#
# `spoke_over_agent` decides whether speech onset raised a pending interrupt;
# the pair is the genuine race between the model's tool_call and the ASR's
# lagging final.
#
# `into-silence__tool_call-first` was a strict xfail until #72: with no
# `interrupted` there is no pending interrupt, so the tool_call backstop that
# #52 fixed never fires, and the utterance's own lagging `final` superseded
# the answer to the question it transcribes. It is a normal case now — the
# endpoint carries an in-flight answer across the epoch bump when the final
# belongs to the same caller utterance (`utterance_clock`).
ORDERS = [
    ("over-agent__tool_call-first", True, "tool_call"),
    ("over-agent__final-first", True, "final"),
    ("into-silence__tool_call-first", False, "tool_call"),
    ("into-silence__final-first", False, "final"),
]


@pytest.mark.parametrize(
    "case_id,spoke_over_agent,first",
    [pytest.param(o[0], o[1], o[2], id=o[0]) for o in ORDERS],
)
def test_one_utterance_is_answered_once_and_audibly(
    harness, monkeypatch, case_id, spoke_over_agent, first
):
    """The generative check: every realizable order for ONE utterance.

    No ordering here contains a second caller utterance, so there is nothing
    that could legitimately supersede this answer. A SILENT delivery is
    therefore always dead air, and a missing delivery is always a hung call —
    regardless of which pipeline happened to win the race.
    """
    client, state = harness
    session = state["session"]
    if state.get("sustain"):
        # The sustained cut arms only without sentence streaming
        # (azure_voice_live's posture) — run this enumeration in it.
        session.supports_streamed_tool_results = False

    async def _slow_delegate(**kwargs):
        # Non-zero so the racing event lands while the answer is in flight —
        # the window where the confirm-latch defect actually bit.
        await asyncio.sleep(0.05)
        return DelegateResult(spoken=ANSWER, display=ANSWER, charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow_delegate)

    with client.websocket_connect(
        "/voice/live?agentKind=sample_voice&businessId=b1"
    ) as ws:
        ws.receive_json()  # ready

        if spoke_over_agent:
            # Speech onset over the agent: detection only, pending interrupt.
            session.push(RealtimeEvent(type="interrupted"))

        if first == "tool_call":
            session.push(_tool_call_event())
            if spoke_over_agent:
                # The backstop confirms here; let it be processed before the
                # lagging final lands, or the race under test is not the one
                # being exercised.
                _read_until(ws, "interrupted")
            session.push(_final_event())
        else:
            session.push(_final_event())
            if spoke_over_agent:
                _read_until(ws, "interrupted")
            session.push(_tool_call_event())

        _read_until(ws, "display")

    deliveries = [r for r in session.tool_results if r[0] == "c1"]

    assert len(deliveries) == 1, (
        f"[{case_id}] expected exactly one delivery for the caller's question, "
        f"got {len(deliveries)}: {deliveries!r}. Zero means the tool call was "
        f"left hanging; two means the same answer was spoken twice."
    )

    _call_id, payload, mode = deliveries[0]
    assert mode is not ToolResultMode.SILENT, (
        f"[{case_id}] the answer was delivered SILENT. This utterance is the "
        f"only one in the session — nothing exists that could supersede it — "
        f"so the caller asked a question and heard dead air. This is the "
        f"confirm-latch defect class: a second epoch bump for one utterance "
        f"pushes the dispatch's captured epoch out of date."
    )
    assert payload.get("spoken") == ANSWER

    assert session.barge_in_cancels <= 1, (
        f"[{case_id}] the agent was cancelled {session.barge_in_cancels} times "
        f"for ONE utterance; confirmation must be idempotent per utterance."
    )


def test_the_scope_of_these_invariants(harness, monkeypatch):
    """Guards the boundary the file's docstring claims.

    A NEWER utterance genuinely does supersede an older in-flight answer, and
    SILENT is the CORRECT outcome there. If that stopped being true, the
    invariant above would be over-broad rather than merely scoped — so pin the
    distinction instead of leaving it as prose.
    """
    client, state = harness
    session = state["session"]

    async def _slow_delegate(**kwargs):
        await asyncio.sleep(0.05)
        return DelegateResult(spoken=ANSWER, display=ANSWER, charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow_delegate)

    with client.websocket_connect(
        "/voice/live?agentKind=sample_voice&businessId=b1"
    ) as ws:
        ws.receive_json()
        # First question dispatches...
        session.push(_tool_call_event())
        # ...then the caller cuts in with a genuinely NEW utterance.
        session.push(RealtimeEvent(type="interrupted"))
        session.push(
            RealtimeEvent(
                type="transcript", role="user", text="no, my sales instead", final=True
            )
        )
        _read_until(ws, "interrupted")
        _read_until(ws, "display")

    deliveries = [r for r in session.tool_results if r[0] == "c1"]
    assert len(deliveries) == 1, "the superseded call must still be answered-to"
    assert deliveries[0][2] is ToolResultMode.SILENT, (
        "a superseded answer must go to context SILENTLY — speaking it would "
        "answer a question the caller has already abandoned."
    )


def test_two_finals_at_one_clock_reading_are_two_utterances(harness, monkeypatch):
    """The supersede cannot switch itself off when onsets stop being reported.

    The clock is fed by the provider, and a provider (or a turn-detection
    config) that reports no speech onset at all would peg it — every later
    final would look like it belonged to whatever was in flight, and the
    caller could never replace a question. One utterance yields ONE final, so
    a second final at an unchanged reading is a second utterance regardless of
    what the onset stream did or didn't say.

    azure_voice_live always configures VAD, so this is belt and braces rather
    than a shape seen live — which is exactly why it needs a test.
    """
    client, state = harness
    session = state["session"]

    async def _slow_delegate(**kwargs):
        await asyncio.sleep(0.15)
        return DelegateResult(spoken=ANSWER, display=ANSWER, charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow_delegate)

    with client.websocket_connect(
        "/voice/live?agentKind=sample_voice&businessId=b1"
    ) as ws:
        ws.receive_json()  # ready
        # Not one onset event in the whole exchange.
        session.push(_final_event())
        session.push(_tool_call_event("c1"))
        session.push(
            RealtimeEvent(
                type="transcript", role="user", text="my sales instead", final=True
            )
        )
        _read_until(ws, "display")

    deliveries = [r for r in session.tool_results if r[0] == "c1"]
    assert len(deliveries) == 1
    assert deliveries[0][2] is ToolResultMode.SILENT, (
        "the caller asked a second question; the first answer must not be "
        "read at them just because the provider never reported an onset."
    )


def test_a_second_question_into_silence_still_supersedes_the_first(
    harness, monkeypatch
):
    """The same boundary, on the path with NO `interrupted` at all.

    `test_the_scope_of_these_invariants` above pins the supersede using an
    onset over the agent's voice, which is also the order the tool_call
    backstop already covered. This one uses the order the carry-forward
    actually reaches: both questions asked into silence, so nothing raises
    `interrupted` and the caller's `final` is the FIRST confirmation either
    utterance gets.

    If "belongs to the in-flight answer" were implemented as a time window, a
    latch, or "don't bump while anything is in flight", the first answer would
    be spoken here — at the caller, after they had already replaced the
    question. It is implemented as an identity instead (which caller utterance
    each dispatch is answering), so only the second question's own answer is
    carried across the bump.
    """
    client, state = harness
    session = state["session"]

    async def _slow_delegate(**kwargs):
        await asyncio.sleep(0.15)
        return DelegateResult(spoken=ANSWER, display=ANSWER, charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow_delegate)

    with client.websocket_connect(
        "/voice/live?agentKind=sample_voice&businessId=b1"
    ) as ws:
        ws.receive_json()  # ready
        # Question one, asked into silence: onset, then the model's call.
        session.push(RealtimeEvent(type="speech_started_idle"))
        session.push(_tool_call_event("c1"))
        # Question two, also into silence, while the first is still running.
        session.push(RealtimeEvent(type="speech_started_idle"))
        session.push(_tool_call_event("c2", "my sales instead"))
        # ...and only NOW does the ASR return the second utterance's words.
        session.push(
            RealtimeEvent(
                type="transcript", role="user", text="my sales instead", final=True
            )
        )
        _read_until(ws, "display")
        _read_until(ws, "display")

    by_call = {r[0]: r for r in session.tool_results}
    assert set(by_call) == {"c1", "c2"}, (
        f"both questions must be answered-to, got {sorted(by_call)}"
    )
    assert by_call["c1"][2] is ToolResultMode.SILENT, (
        "the ABANDONED question's answer must stay silent: the caller replaced "
        "it before it was ready. Carrying every in-flight answer across the "
        "epoch bump would speak it at them."
    )
    assert by_call["c2"][2] is not ToolResultMode.SILENT, (
        "the answer to the question this final actually transcribes must be "
        "audible — that is the defect the carry-forward exists to fix."
    )


# ---------------------------------------------------------------------------
# Relay custody, across the USER-TRANSCRIPT shapes a provider can produce
# ---------------------------------------------------------------------------
#
# Custody has two halves that are wired to DIFFERENT signals:
#
#   * it SUPPRESSES the model's own ask_agent (closed unspoken) whenever
#     `_relay_active()` — a purely internal flag, fed by a delegated turn's
#     flow_signals or by an intent term;
#   * it DISPATCHES the replacement turn from exactly one event: the FINAL
#     fragment of the caller's user transcript.
#
# A provider that never finalizes its input transcription therefore satisfies
# the first half and can never reach the second, and custody becomes a black
# hole: the caller reads their verification code, the model dutifully relays
# it as an ask_agent, the endpoint closes that call unspoken, and nobody ever
# answers. Live on a client demo 2026-08-19, one 51s call on Gemini Live:
# `relay mode OPEN (flow signals: ['send_otp'])`, two `relay custody open —
# closing the model's own ask_agent ... unspoken`, and `relayed=0`.
#
# So the shapes below are not three tests of one provider — they are the
# realizable user-transcript streams across providers, and the invariant is
# the one custody exists to keep: while the endpoint holds a flow, every
# caller utterance in it reaches the delegate EXACTLY ONCE. Never zero (the
# black hole above), never twice (the concurrent-duplicate race custody was
# built to stop).

CODE_UTTERANCE = "the code is one two three four five six"


def _user_deltas(text):
    """The caller's transcript as incremental fragments, no final."""
    words = text.split()
    cut = len(words) // 2
    return [
        RealtimeEvent(
            type="transcript", role="user", text=" ".join(words[:cut]), final=False
        ),
        RealtimeEvent(
            type="transcript", role="user", text=" " + " ".join(words[cut:]), final=False
        ),
    ]


def _deltas_then_empty_final(text):
    """azure_voice_live's streamed shape: deltas, closed by an empty final."""
    return _user_deltas(text) + [
        RealtimeEvent(type="transcript", role="user", text="", final=True)
    ]


def _single_final_with_text(text):
    """The other azure shape: one transcript event carrying the whole thing."""
    return [RealtimeEvent(type="transcript", role="user", text=text, final=True)]


def _deltas_never_final(text):
    """Gemini Live: input transcription streams and is never finalized.

    Documented on RealtimeSession (voice/live/base.py) and relied on by
    `confirm_mode` in the endpoint — this is the provider's shipped shape,
    not a degraded one.
    """
    return _user_deltas(text)


# (id, the provider finalizes user transcripts, transcript shape, which of
#  the transcript and the model's own ask_agent lands first)
CUSTODY_SHAPES = [
    ("finalizing__deltas_then_final__transcript_first", True, _deltas_then_empty_final, "transcript"),
    ("finalizing__deltas_then_final__model_call_first", True, _deltas_then_empty_final, "model_call"),
    ("finalizing__single_final_with_text", True, _single_final_with_text, "transcript"),
    ("never_finalizing__deltas_only", False, _deltas_never_final, "transcript"),
]


def _wait_until(predicate, timeout=2.0):
    """Poll from the TEST thread so the app's loop thread can make progress."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.mark.parametrize(
    "case_id,finalizes,build_transcript,first",
    [pytest.param(*s, id=s[0]) for s in CUSTODY_SHAPES],
)
def test_custody_never_swallows_the_utterance_it_took_custody_of(
    harness, monkeypatch, case_id, finalizes, build_transcript, first
):
    """One beat of a protected flow reaches the delegate exactly once.

    Whichever transcript shape the provider produces, and whichever of the
    caller's transcript and the model's own call wins the race, the caller's
    words must be delegated once: suppressing the model's call is only ever
    legitimate when the endpoint takes the turn itself.
    """
    client, state = harness
    session = state["session"]
    # Model the provider, not a mock: azure_voice_live delivers a final
    # fragment per caller utterance, Gemini Live never finalizes its input
    # transcription (voice/live/base.py).
    session.supports_barge_in_confirmation = finalizes
    session.finalizes_user_transcripts = finalizes

    calls = []

    async def _fake_delegate(**kwargs):
        calls.append(kwargs["message"])
        if len(calls) == 1:
            return DelegateResult(
                spoken="I have sent the code, read it back to me.",
                display="d1", charts=[], flow_signals=["send_otp"],
            )
        return DelegateResult(
            spoken="Verified — the IBAN is updated.",
            display="d2", charts=[], flow_signals=["verify_otp_ok"],
        )

    monkeypatch.setattr(live_ep, "run_delegated_turn", _fake_delegate)

    with client.websocket_connect(
        "/voice/live?agentKind=sample_voice&businessId=b1"
    ) as ws:
        ws.receive_json()  # ready
        # Beat one — the model delegates the change request and the delegate's
        # tools open the protected flow. THIS is what puts the endpoint in
        # custody; it needs no transcript at all, on any provider.
        session.push(_tool_call_event("c1", "change my iban"))
        _read_until(ws, "display")

        # Beat two — the caller reads the code back. The model relays it as
        # its own ask_agent, verbatim, exactly as the persona instructs.
        transcript = build_transcript(CODE_UTTERANCE)
        model_call = _tool_call_event("c2", CODE_UTTERANCE)
        if first == "transcript":
            for event in transcript:
                session.push(event)
            session.push(model_call)
        else:
            session.push(model_call)
            for event in transcript:
                session.push(event)

        _wait_until(lambda: len(calls) >= 2)

    assert len(calls) >= 2, (
        f"[{case_id}] the delegate saw {calls!r}. Custody held the flow and "
        f"then answered NOBODY: the caller's verification code reached "
        f"neither a relayed turn (custody's own dispatch rides a FINAL user "
        f"transcript — which this shape may never send) nor the model's "
        f"ask_agent (closed unspoken because custody was open). Suppressing a "
        f"call the endpoint cannot replace is a black hole; this is the live "
        f"2026-08-19 OTP failure on a client demo."
    )
    assert len(calls) == 2, (
        f"[{case_id}] the delegate saw {calls!r} — the caller's one utterance "
        f"was delegated TWICE, two ~10s turns racing two answers at them. "
        f"Custody suppresses the model's ask_agent for exactly this reason, so "
        f"whatever stops suppressing must also stop relaying."
    )
    assert calls[1] == CODE_UTTERANCE, (
        f"[{case_id}] the second delegated turn must carry the caller's own "
        f"words, got {calls[1]!r}"
    )

    relayed = [
        t for t in session.text_turns
        if t.startswith("Say exactly this to the caller") and "Verified" in t
    ]
    via_tool_result = [
        r for r in session.tool_results
        if r[0] == "c2"
        and r[2] is not ToolResultMode.SILENT
        and (r[1] or {}).get("spoken")
    ]
    assert relayed or via_tool_result, (
        f"[{case_id}] the answer never reached the caller: no say-exactly "
        f"relay delivery and no audible tool result. text_turns="
        f"{session.text_turns!r} tool_results={session.tool_results!r}"
    )
    assert not (relayed and via_tool_result), (
        f"[{case_id}] the answer was delivered TWICE — once by custody's "
        f"relayed turn and once through the model's own call. That duplicate "
        f"is exactly why custody suppresses the model's ask_agent; a fix that "
        f"stops suppressing must also stop relaying."
    )


# ---------------------------------------------------------------------------
# The endpoint's OWN spoken text, across the shapes it reaches the model in
# ---------------------------------------------------------------------------
#
# The announce-then-stall tripwire arms on one premise: "the MODEL declared a
# lookup and then did not perform one". Nothing legitimate does that, which is
# why tier 1 skips BOTH of tier 2's gates — the caller naming a topic term,
# and the reply not being a question back. Those gates are what keep a
# recovery off a farewell and off the persona's own clarifying question.
#
# But the endpoint puts text in the model's mouth too: the holding line, an
# answer delivered as a tool result, a say-exactly relay. The model reads it
# back and it returns as ordinary assistant transcript — the model's voice,
# the endpoint's words. #133 excluded the ONE shape that is self-identifying
# (a provider surfacing out-of-band speech with `out_of_band=True`, which the
# simulated provider does) and stopped there. Every other shape still reads as
# the model declaring a lookup, and the shipped Arabic holding line on the
# client demo is «لحظة، أتأكد لك الحين.» — «لحظة» is an ANNOUNCE_TOKENS entry.
# On Gemini Live, which has no out-of-band speech, that line rides a WHEN_IDLE
# tool result and comes back unflagged: our own filler arms our own tripwire
# at tier-1 confidence, past both gates, and the recovery answers a caller who
# said goodbye.
#
# So the parametrization is the DELIVERY SHAPES the endpoint's own speech can
# take, and the invariant is that the tripwire's verdict must not depend on
# which one this provider uses: what the endpoint said is never evidence about
# what the model declared.

HOLDING_LINE = "لحظة، أتأكد لك الحين."   # live progress_spoken_by_language.ar
DATA_QUESTION = "كم مبيعات الشهر الماضي؟"
FAREWELL = "لا شكراً، هذا كل شيء"
TOPIC_TERMS = ["مبيعات", "sales"]

# (id, provider speaks out of band, its transcript carries out_of_band=True)
ENDPOINT_SPEECH_SHAPES = [
    ("out_of_band__flagged", True, True),
    ("out_of_band__unflagged", True, False),
    ("tool_result__read_back", False, False),
]


@pytest.mark.parametrize(
    "case_id,provider_speaks_oob,transcript_flagged",
    [pytest.param(*s, id=s[0]) for s in ENDPOINT_SPEECH_SHAPES],
)
def test_the_endpoints_own_holding_line_never_arms_the_stall_tripwire(
    harness, monkeypatch, case_id, provider_speaks_oob, transcript_flagged
):
    """A farewell is not a stall, whatever our own filler said a moment ago.

    One delegated question (slow enough that the holding line fires), then the
    caller says goodbye — an utterance naming no topic term, which tier 2
    refuses by design. The only announce phrase anywhere in the model's turn
    is the one the ENDPOINT wrote. If that arms tier 1, the recovery delegates
    the farewell and the assistant answers a question the caller never asked,
    straight into the pause after her last answer.
    """
    client, state = harness
    session = state["session"]
    session.supports_out_of_band_speech = provider_speaks_oob
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_spoken=HOLDING_LINE,
        progress_after_sec=0.02,
        stall_recovery_sec=0.15,
        stall_recovery_unannounced_sec=0.15,
        stall_recovery_topic_terms=TOPIC_TERMS,
        # Tier 3 hangs off a timer, not the reply; keep it out of this test.
        stall_recovery_silent_sec=0,
    )

    calls = []

    async def _slow_delegate(**kwargs):
        calls.append(kwargs["message"])
        await asyncio.sleep(0.3)          # long enough for the holding line
        return DelegateResult(spoken=ANSWER, display=ANSWER, charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow_delegate)

    with client.websocket_connect(
        "/voice/live?agentKind=sample_voice&businessId=b1"
    ) as ws:
        ws.receive_json()  # ready

        # A real question, answered by the model's own ask_agent. Its commit
        # is consumed here — nothing about THIS utterance can arm anything.
        session.push(
            RealtimeEvent(type="transcript", role="user",
                          text=DATA_QUESTION, final=True)
        )
        session.push(_tool_call_event("c1", DATA_QUESTION))
        _read_until(ws, "display")

        # The holding line went out while the delegate ran, by whichever
        # route this provider offers. Then the model reads it back: assistant
        # transcript, in the model's voice, carrying the endpoint's words.
        assert session.out_of_band or any(
            (r[1] or {}).get("spoken") == HOLDING_LINE for r in session.tool_results
        ), (
            f"[{case_id}] the holding line never went out, so this case is "
            f"not exercising anything: out_of_band={session.out_of_band!r} "
            f"tool_results={session.tool_results!r}"
        )
        session.push(
            RealtimeEvent(type="transcript", role="assistant",
                          text=HOLDING_LINE, final=False,
                          out_of_band=transcript_flagged)
        )

        # ...and the caller is done. A farewell: no topic term, so tier 2
        # refuses it, and no announce phrase of the MODEL's own anywhere.
        #
        # It lands inside the same model turn as the holding line, which is
        # not a contrivance: `turn_end` is the provider's turn_complete, and
        # Gemini paces that to the AUDIO timeline — probed at 6-9s after the
        # model stopped generating. Anything the caller commits in that window
        # is judged against a reply buffer still holding our filler.
        session.push(
            RealtimeEvent(type="transcript", role="user", text=FAREWELL, final=True)
        )
        session.push(
            RealtimeEvent(type="transcript", role="assistant",
                          text="العفو، سعدت بخدمتك.", final=False)
        )
        session.push(RealtimeEvent(type="turn_end"))

        # Give a wrongly-armed tier 1 every chance to fire (grace 0.15s).
        _wait_until(lambda: len(calls) >= 2, timeout=1.5)

    assert calls == [DATA_QUESTION], (
        f"[{case_id}] the delegate saw {calls!r}. The caller said goodbye and "
        f"the endpoint delegated it: the only announce phrase in that model "
        f"turn was the holding line the ENDPOINT wrote, and tier 1 skips both "
        f"of tier 2's gates, so our own filler talked the recovery past the "
        f"farewell guard. What the endpoint said is not evidence about what "
        f"the model declared."
    )
    assert not [t for t in session.text_turns if ANSWER in t], (
        f"[{case_id}] a say-exactly delivery was read at the caller after "
        f"their goodbye: {session.text_turns!r}"
    )


# ── The sustained-speech cut's own interleavings ───────────────────────────
# One utterance spoken over AUDIBLE playback, long enough for the sustained cut
# to fire, then its end (speech_stopped, commit), its lagging words and the
# model's tool_call in every realizable order. Invariants: the caller's
# question is answered exactly once and audibly, the talk-over produces at
# most one `barge_in` cut, and she is cancelled at most once.
_TWO_SECONDS = b"\x00" * (2 * 24000 * 2)
SUSTAIN_ORDERS = [
    ("stop-commit-final-call", ["speech_stopped", "commit", "final", "tool_call"]),
    ("stop-commit-call-final", ["speech_stopped", "commit", "tool_call", "final"]),
    ("commit-call-final", ["commit", "tool_call", "final"]),
    ("call-commit-final", ["tool_call", "commit", "final"]),
]


@pytest.mark.parametrize("order", [pytest.param(o[1], id=o[0]) for o in SUSTAIN_ORDERS])
def test_a_sustained_cut_utterance_is_answered_once_and_audibly(harness, monkeypatch, order):
    import time as _time

    client, state = harness
    session = state["session"]
    state["cfg"] = LiveVoiceConfig(enabled=True, model_id="m-1", provider="fake",
                                   interrupt_confirm_window_sec=5.0, barge_in_sustain_sec=0.05)
    session.supports_streamed_tool_results = False

    async def _slow_delegate(**kwargs):
        await asyncio.sleep(0.05)
        return DelegateResult(spoken=ANSWER, display=ANSWER, charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow_delegate)
    frames = []
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="audio", audio=_TWO_SECONDS))
        assert ws.receive_bytes() == _TWO_SECONDS
        session.push(RealtimeEvent(type="speech_started_idle"))
        _time.sleep(0.3)                                  # the sustained cut fires
        for step in order:
            if step == "speech_stopped":
                session.push(RealtimeEvent(type="speech_stopped"))
            elif step == "commit":
                session.push(RealtimeEvent(type="user_turn_end"))
                session.push(RealtimeEvent(type="playback_flush"))
            elif step == "final":
                session.push(_final_event())
            else:
                session.push(_tool_call_event())
        while True:
            msg = ws.receive()
            if msg.get("text"):
                import json as _json
                f = _json.loads(msg["text"])
                frames.append(f)
                if f.get("type") == "display":
                    break
    deliveries = [r for r in session.tool_results if r[0] == "c1"]
    assert len(deliveries) == 1 and deliveries[0][2] is not ToolResultMode.SILENT, deliveries
    cuts = [f for f in frames if f.get("type") == "interrupted" and f.get("reason") == "barge_in"]
    assert len(cuts) == 1, frames
    assert session.barge_in_cancels <= 2      # the cut + at most one confirm (no-op on idle)
