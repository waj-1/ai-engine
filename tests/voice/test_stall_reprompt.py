"""The stall re-prompt: ask the model to finish, don't sit out the grace.

Why this exists as its own file rather than another case in the stall-recovery
tests: those pin what the endpoint says when the model never comes back. This
pins the step BEFORE that — the endpoint asking at all.

The measurement behind it (client demo VM, 2026-08-05, live client call):
every single tool result in the session was a stall recovery, at
eot_to_result 10.2s and 12.3s. The wire showed why. Both times the model
ended its turn on the announce with no function_call in it, and both times
its own `ask_agent` DID arrive — 0.4s and 1.4s after the recovery's holding
line created a response, 2.5s into a delegate that was already running. The
call was never late; the model was finished, and the only thing that got the
owed call out of it was a new response happening to exist. Live-probed
against gpt-realtime the same day: no re-prompt, no call within 12s; a bare
`response.create`, and the call lands in 0.37s carrying the caller's own
question.

So the endpoint asks on purpose and immediately, and the recovery stays armed
behind it as the fallback it always was.
"""
import time as _t

import pytest

import endpoints.voice_live as live_ep
from voice.live.base import RealtimeEvent
from voice.live.config import LiveVoiceConfig
from voice.live.delegate import DelegateResult

from tests.voice.test_voice_live_endpoint import harness  # noqa: F401

TOPIC_TERMS = ["sales", "terminal", "branch", "iban", "ticket"]


def _drive(harness, monkeypatch, *, caller, reply, reprompt_reply=None,
           grace=0.4, terms=TOPIC_TERMS):
    """One caller utterance + one model turn that calls no tool.

    Returns (session, messages the delegate was asked to run). `grace` is
    deliberately long enough that the assertions about the re-prompt land
    BEFORE the recovery could have fired — the whole point is that the
    re-prompt does not wait for it.
    """
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", progress_after_sec=0,
        stall_recovery_sec=grace,
        stall_recovery_unannounced_sec=grace,
        stall_recovery_topic_terms=terms,
    )
    session = state["session"]
    session.reprompt_reply = reprompt_reply
    called = []

    async def _delegate(**kwargs):
        called.append(kwargs.get("message"))
        return DelegateResult(spoken="RECOVERED", display="d", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate)
    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(type="transcript", role="user", text=caller, final=True))
        session.push(RealtimeEvent(type="transcript", role="assistant", text=reply, final=True))
        session.push(RealtimeEvent(type="turn_end"))
        _t.sleep(grace + 0.5)
    return session, called


@pytest.mark.parametrize("reply", [
    "One moment, let me check that for you.",     # tier 1: announced
    "Sure thing.",                                # tier 2: caller named a topic
])
def test_a_turn_that_ends_owing_a_tool_call_re_prompts_the_model(
    harness, monkeypatch, reply  # noqa: F811
):
    """The fix, at its narrowest: the model is ASKED to finish.

    Before this, the endpoint's only move was to wait out the grace and then
    run the caller's question through a second, parallel delegate — paying
    3-6s of silence for a call the model would produce in under a second if
    anything asked it to."""
    session, _ = _drive(
        harness, monkeypatch, caller="what were my sales yesterday", reply=reply,
    )
    assert session.reprompts == 1


def test_the_re_prompted_call_answers_the_turn_and_stands_the_recovery_down(
    harness, monkeypatch  # noqa: F811
):
    """The re-prompt is not a nudge alongside the recovery — it REPLACES it.

    When the model comes back with the ask_agent it owed, that call consumes
    the commit, so the recovery must find nothing to do: exactly one delegated
    turn, driven by the model's own query. Two would be the 2026-08-05 shape,
    where the recovery's answer and the model's late call raced and one of
    them was thrown away ("Tool call ID ... not found in conversation")."""
    session, called = _drive(
        harness, monkeypatch,
        caller="what were my sales yesterday",
        reply="One moment, let me check that for you.",
        reprompt_reply=RealtimeEvent(
            type="tool_call", call_id="call_1", tool_name="ask_agent",
            arguments={"query": "sales for yesterday"},
        ),
    )
    assert session.reprompts == 1
    assert called == ["sales for yesterday"], (
        "the model's own re-prompted call must be the one delegated, once"
    )


@pytest.mark.parametrize("caller,reply", [
    ("no, that's all thanks", "Thank you for calling us. Goodbye!"),
    ("لا، هذا كل شي", "شكراً لاتصالك فينا، مع السلامة."),
])
def test_a_closing_turn_is_never_re_prompted(harness, monkeypatch, caller, reply):  # noqa: F811
    """The false positive that would cost more than the stall it fixes.

    A re-prompt is a free turn handed to a model that just said goodbye — it
    will use it, and the caller gets talked at after the farewell. So the
    re-prompt rides the stall tripwire's own verdict and nothing else: no
    grace armed, no floor handed back. Same reading as tier 2's closure
    test, one step earlier in the sequence."""
    session, called = _drive(harness, monkeypatch, caller=caller, reply=reply)
    assert session.reprompts == 0
    assert called == []


def test_a_provider_that_cannot_re_prompt_keeps_todays_recovery(
    harness, monkeypatch  # noqa: F811
):
    """`supports_reprompt` is a real capability gate, not decoration.

    Gemini Live has no equivalent primitive. On such a provider the endpoint
    must fall straight through to the recovery it has always run — the stall
    still gets answered, just the slow way."""
    client, state = harness
    state["session"].supports_reprompt = False   # instance shadow of the class attr
    session, called = _drive(
        harness, monkeypatch,
        caller="what were my sales yesterday",
        reply="One moment, let me check that for you.",
    )
    assert session.reprompts == 0
    assert called == ["what were my sales yesterday"], "the recovery must still fire"
