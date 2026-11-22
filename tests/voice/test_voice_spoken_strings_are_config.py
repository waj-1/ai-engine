"""Every string the caller HEARS is operator config with a sane default.

Three of the voice layer's spoken strings already worked this way — the
holding line, the failure line and the on-screen pointer, each with a flat
key, a `*_by_language` map and a built-in fallback. Two did not, and the
gaps had the same shape: an English literal at the use site, spoken into
a call being conducted in Arabic.

  * the SAY-EXACTLY instruction wrapping every answer the endpoint delivers
    itself (relay custody, stall/blank-call recovery, the answer watchdog) —
    an English literal at three sites in `endpoints/voice_live.py`, and it is
    a PROMPT: the model obeys it or improvises around it;
  * the HOLDING LINE's own per-language default. The map existed, the flat
    field's default did not have one, so a tenant that never touched either
    heard "Still working on that, one moment." in an Arabic call — which is
    verbatim the failure the map was added to prevent.

The new template takes an operator's raw JSON and reaches a `.format()` on
the answer-delivery path, so the tests below spend as much effort on what
happens to a BAD template as on a good one: a typo in a text box must not
cost the caller the answer.

(A third gap, the masked-identifier phrasing, was given a lever here too and
then removed with the masking itself on 2026-08-19 — see
`voice/live/speakable.py`'s module docstring.)
"""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import endpoints.voice_live as live_ep
from voice.live.base import AudioFormat, RealtimeEvent
from voice.live.delegate import DelegateResult
from voice.live.testing import FakeRealtimeSession
from voice.live.config import (
    LiveVoiceConfig,
    resolve_progress_spoken,
    resolve_say_exactly_instruction,
)


# --------------------------------------------------------------------------- #
# say-exactly instruction
# --------------------------------------------------------------------------- #
def test_the_built_in_instruction_follows_the_call_s_language():
    """The whole point: the instruction is a prompt, and a model follows an
    instruction best in the language it is already speaking."""
    en = resolve_say_exactly_instruction(
        LiveVoiceConfig(language="en-US"), "Twelve terminals."
    )
    ar = resolve_say_exactly_instruction(
        LiveVoiceConfig(language="ar-SA"), "اثنا عشر جهازاً."
    )
    assert "Twelve terminals." in en
    assert en.startswith("Say exactly this")
    assert "اثنا عشر جهازاً." in ar
    # Not the English literal that used to be concatenated here regardless of
    # the call's language.
    assert "Say exactly this" not in ar


def test_an_unmapped_language_falls_back_to_english_not_to_nothing():
    out = resolve_say_exactly_instruction(LiveVoiceConfig(language="fr"), "Douze.")
    assert "Douze." in out
    assert out.startswith("Say exactly this")


def test_the_operator_s_flat_instruction_wins_over_the_built_in():
    cfg = LiveVoiceConfig(
        language="en", say_exactly_instruction="Read this out, word for word: {spoken}"
    )
    assert resolve_say_exactly_instruction(cfg, "Nine.") == (
        "Read this out, word for word: Nine."
    )


def test_the_per_language_instruction_wins_over_the_flat_one():
    cfg = LiveVoiceConfig(
        language="ar-SA",
        say_exactly_instruction="flat: {spoken}",
        say_exactly_instruction_by_language={"ar": "اقرأ هذا حرفياً: {spoken}"},
    )
    assert resolve_say_exactly_instruction(cfg, "تسعة") == "اقرأ هذا حرفياً: تسعة"


@pytest.mark.parametrize(
    "template,why",
    [
        ("Say this: {answer}", "unknown field"),
        ("Say this: {", "unbalanced brace"),
        ("Say this and nothing else.", "no placeholder — would DROP the answer"),
        ("Say this: {spoken!z}", "bad conversion spec"),
    ],
)
def test_a_template_that_cannot_carry_the_answer_falls_back_instead_of_losing_it(
    template, why
):
    """A typo in a text box must never cost the caller the answer.

    Two failure shapes, one outcome. A broken format spec RAISES on the
    delivery path — the answer the agent already computed never reaches the
    model at all. A template with no placeholder does not raise; it formats
    cleanly and silently delivers an instruction with the answer missing,
    which is the same dead air by a quieter route. Both fall back.
    """
    cfg = LiveVoiceConfig(language="en", say_exactly_instruction=template)
    out = resolve_say_exactly_instruction(cfg, "Nine hundred.")
    assert "Nine hundred." in out, why
    assert out.startswith("Say exactly this"), why


def test_the_answer_is_substituted_not_appended():
    """`{spoken}` is a placeholder, not a suffix.

    Arabic reads right to left and the instruction does not always belong in
    front of the answer; a tenant must be able to put it after.
    """
    cfg = LiveVoiceConfig(
        language="ar", say_exactly_instruction="{spoken} — قل هذا حرفياً."
    )
    assert resolve_say_exactly_instruction(cfg, "تسعمائة").startswith("تسعمائة")


# --------------------------------------------------------------------------- #
# holding line: the built-in per-language default
# --------------------------------------------------------------------------- #
def test_an_arabic_call_no_one_configured_does_not_get_an_english_holding_line():
    """The gap this closes. `failure_spoken` and `on_screen_spoken` both ship
    an Arabic built-in; `progress_spoken`'s dataclass default was English and
    had no per-language fallback of its own, so an untouched tenant spoke
    "Still working on that, one moment." into an Arabic call."""
    line = resolve_progress_spoken(LiveVoiceConfig(language="ar-SA"))
    assert line
    assert "Still working" not in line
    assert any("؀" <= ch <= "ۿ" for ch in line), line


def test_the_english_default_is_untouched():
    assert resolve_progress_spoken(LiveVoiceConfig(language="en-US")) == (
        "Still working on that, one moment."
    )
    # And an unmapped language keeps the shipped default rather than silence.
    assert resolve_progress_spoken(LiveVoiceConfig(language="fr")) == (
        "Still working on that, one moment."
    )


def test_an_operator_s_own_single_line_wins_over_the_built_in_arabic():
    """The precedence rung that needs pinning: a tenant who wrote ONE custom
    line and no per-language map asked for that line, in every language. A
    built-in that overrode it would silently discard their wording."""
    cfg = LiveVoiceConfig(language="ar-SA", progress_spoken="Hold please.")
    assert resolve_progress_spoken(cfg) == "Hold please."


def test_the_per_language_map_still_wins_over_everything():
    cfg = LiveVoiceConfig(
        language="ar-SA",
        progress_spoken="Hold please.",
        progress_spoken_by_language={"ar": "لحظة، أتأكد لك."},
    )
    assert resolve_progress_spoken(cfg) == "لحظة، أتأكد لك."


def test_the_built_in_arabic_holding_line_is_not_an_announce_phrase():
    """A holding line the ENDPOINT speaks must not read as the MODEL declaring
    a lookup. `common.announce.ANNOUNCE_TOKENS` is matched against the
    assistant's own reply to arm the stall recovery, and the shipped English
    default already contains "one moment" — an entry in that list. Adding an
    Arabic default that also matched would have armed the recovery against the
    endpoint's own filler on every Arabic lookup."""
    from common.announce import ANNOUNCE_TOKENS, normalize_intent_text

    line = normalize_intent_text(resolve_progress_spoken(LiveVoiceConfig(language="ar")))
    hits = [t for t in ANNOUNCE_TOKENS if normalize_intent_text(t) in line]
    assert not hits, hits


# --------------------------------------------------------------------------- #
# The endpoint wiring: resolvers are only worth having if they reach the model
# --------------------------------------------------------------------------- #
@pytest.fixture
def harness(monkeypatch):
    """Same wiring as tests/voice/test_voice_live_endpoint.py's fixture — see
    the note there about why it is duplicated rather than imported."""
    state = {}
    live_ep._connect_limiter._store.clear()

    def _cfg(_conn, _kind):
        return state.get(
            "cfg", LiveVoiceConfig(enabled=True, model_id="m-1", provider="fake")
        )

    monkeypatch.setattr(live_ep, "_live_config_for", _cfg)
    monkeypatch.setattr(live_ep, "resolve_channel", lambda conn: None)
    monkeypatch.setattr(live_ep, "enforce_channel_kind", lambda conn, kind: None)
    monkeypatch.setattr(live_ep, "authenticate_optional", lambda authz: ("tok", "user-1"))
    monkeypatch.setattr(live_ep, "reserve_audio_seconds", lambda *a, **kw: 0)
    monkeypatch.setattr(
        live_ep, "build_agent_for_kind", lambda kind, params: (object(), object())
    )
    monkeypatch.setattr(
        live_ep, "resolve_persona_prompt", lambda cfg, kind, **kw: "You are a voice agent."
    )

    session = FakeRealtimeSession(audio_format=AudioFormat(16000, 24000))
    monkeypatch.setattr(live_ep, "_session_for", lambda cfg, **kw: session)
    state["session"] = session

    app = FastAPI()
    app.include_router(live_ep.router)
    return TestClient(app), state


def _read_until(ws, kind, limit=10):
    for _ in range(limit):
        frame = ws.receive_json()
        if frame.get("type") == kind:
            return frame
    raise AssertionError(f"no {kind!r} frame in {limit} frames")


def _wait_for(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_the_endpoint_delivers_a_recovered_answer_with_the_configured_instruction(
    harness, monkeypatch
):
    """The three say-exactly sites in the endpoint must read the resolver, not
    a literal. Driven through the blank-`ask_agent` recovery, which is the
    cheapest of the three to reach: a committed utterance followed by a call
    whose arguments were truncated to nothing.
    """
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake", language="ar-SA",
        progress_after_sec=0,
        say_exactly_instruction_by_language={"ar": "اقرأ هذا حرفياً: {spoken}"},
    )
    session = state["session"]

    async def _delegate(**kwargs):
        return DelegateResult(spoken="عندك اثنا عشر جهازاً.", display="12", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(
            type="transcript", role="user", text="كم جهاز عندي؟", final=True,
        ))
        session.push(RealtimeEvent(
            type="tool_call", call_id="c1", tool_name="ask_agent", arguments={},
        ))
        _read_until(ws, "display")
        assert _wait_for(lambda: any(
            t == "اقرأ هذا حرفياً: عندك اثنا عشر جهازاً." for t in session.text_turns
        )), session.text_turns


def test_the_endpoint_s_own_holding_line_never_arms_the_stall_recovery(
    harness, monkeypatch
):
    """The announce tripwire reads the MODEL's reply, not ours.

    `assistant_reply` accumulates assistant transcript text and `turn_end`
    tests it against `ANNOUNCE_TOKENS`. The shipped holding line contains
    "one moment" — an entry in that list — and a provider that surfaces
    out-of-band speech as an ordinary transcript event (voice/live/simulated.py,
    which every eval run drives) therefore fed the endpoint's own filler into
    the tripwire. The recovery then fired against a turn the model had
    answered perfectly well, spending a second full delegated turn on it.
    """
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0, answer_watchdog_sec=0, stall_recovery_sec=0.2,
    )
    session = state["session"]
    calls = []

    async def _delegate(**kwargs):
        calls.append(kwargs.get("message"))
        return DelegateResult(spoken="Twelve.", display="12", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(
            type="transcript", role="user", text="how many terminals", final=True,
        ))
        # The endpoint's holding line, as an out-of-band-flagged transcript.
        session.push(RealtimeEvent(
            type="transcript", role="assistant",
            text="Still working on that, one moment.", final=True, out_of_band=True,
        ))
        session.push(RealtimeEvent(type="turn_end"))
        time.sleep(0.6)   # three stall graces

    assert calls == [], calls


def test_a_real_announce_from_the_model_still_arms_it(harness, monkeypatch):
    """The other half — proving the fix above narrowed the tripwire rather
    than disarming it. Identical text, in-conversation rather than
    out-of-band, is the model declaring a lookup it never performed."""
    client, state = harness
    state["cfg"] = LiveVoiceConfig(
        enabled=True, model_id="m-1", provider="fake",
        progress_after_sec=0, answer_watchdog_sec=0, stall_recovery_sec=0.2,
    )
    session = state["session"]
    calls = []

    async def _delegate(**kwargs):
        calls.append(kwargs.get("message"))
        return DelegateResult(spoken="Twelve.", display="12", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _delegate)

    with client.websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        session.push(RealtimeEvent(
            type="transcript", role="user", text="how many terminals", final=True,
        ))
        session.push(RealtimeEvent(
            type="transcript", role="assistant",
            text="Sure, one moment.", final=True,
        ))
        session.push(RealtimeEvent(type="turn_end"))
        assert _wait_for(lambda: calls == ["how many terminals"]), calls
