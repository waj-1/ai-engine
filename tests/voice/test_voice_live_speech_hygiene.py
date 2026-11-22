"""V5 (voice E2E 2026-09-28): tool-call syntax the realtime model SPOKE
("Let me check. functions.ask_agent", Azure) was forwarded as a caption and
committed into the app's chat bubble. It arrived split across three deltas,
so the scrubber is streaming — these tests replay that exact split."""
from __future__ import annotations

import re

import pytest

import endpoints.voice_live as live_ep
from voice.live.base import RealtimeEvent
from voice.live.speech_hygiene import ToolSyntaxScrubber, scrub_tool_syntax

# The deltas exactly as the WS tap recorded them (v7r-ws-B.jsonl).
LIVE_DELTAS = ["Let", " me", " check", ".", " ", " functions", ".ask", "_agent", " "]


def _stream(deltas):
    s = ToolSyntaxScrubber()
    return "".join(s.feed(d) for d in deltas) + s.flush()


def test_live_split_leak_is_removed():
    out = _stream(LIVE_DELTAS)
    assert "functions" not in out and "ask_agent" not in out
    assert out.strip() == "Let me check."


@pytest.mark.parametrize("text", [
    "Sure — call`ask_agent(query: what's today's revenue)`",
    "One sec. functions.ask_agent({\"query\": \"revenue\"})",
    "لحظة، خلني أشوف لك. functions.ask_agent",
    "ask_agent",
])
def test_tool_syntax_shapes_are_removed(text):
    assert "ask_agent" not in scrub_tool_syntax(text)
    assert "ask_agent" not in _stream([c for c in text])  # worst case: one char per delta


@pytest.mark.parametrize("text", [
    "I asked the team; it was fun.",
    "Your functions. Ask me anything.",
    "Revenue was 4,320 AED (up 12%).",
    "حاضر، جهّزت استراحة غداء لسارة.",
    "The get_ prefix is fine, and so is snake_case prose.",
])
def test_ordinary_speech_is_untouched(text):
    assert _stream(re.findall(r"\s*\S+", text)) == text
    assert scrub_tool_syntax(text) == text


def test_hold_is_bounded():
    s = ToolSyntaxScrubber()
    out = s.feed("ask_agent(" + "x " * 200)
    # Past the cap the text is released — the tool name is still removed,
    # the (possibly real) speech after it is kept.
    assert "ask_agent" not in out
    assert out.count("x") == 200
    assert not s.holding


@pytest.mark.parametrize("text", [
    "Use the per_hour (rate) option.",
    "The time_off (leave) policy applies.",
])
def test_snake_case_prose_with_a_parenthetical_is_kept(text):
    assert scrub_tool_syntax(text) == text
    assert _stream(re.findall(r"\s*\S+", text)) == text


def test_nested_arguments_do_not_leak():
    out = _stream(["Sure ", "ask_agent", "(query: x (y))", " done"])
    assert "query" not in out and "(y)" not in out and out.split() == ["Sure", "done"]


def test_a_generic_call_with_arguments_is_removed():
    assert scrub_tool_syntax('Checking get_bookings() and get_x(date="today") now.') == "Checking and now."


def test_flush_drops_a_call_cut_off_mid_arguments():
    s = ToolSyntaxScrubber()
    assert s.feed("One sec. functions.ask_agent(query: what") == "One sec. "
    assert s.flush() == ""


def test_flush_releases_a_legitimate_held_word():
    s = ToolSyntaxScrubber()
    assert s.feed("It was fun") == "It was "
    assert s.flush() == "fun"


@pytest.fixture
def endpoint(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from voice.live.base import AudioFormat
    from voice.live.config import LiveVoiceConfig
    from voice.live.testing import FakeRealtimeSession

    live_ep._connect_limiter._store.clear()
    state = {"cfg": LiveVoiceConfig(enabled=True, model_id="m-1", provider="fake")}
    monkeypatch.setattr(live_ep, "_live_config_for", lambda _c, _k: state["cfg"])
    monkeypatch.setattr(live_ep, "resolve_channel", lambda conn: None)
    monkeypatch.setattr(live_ep, "enforce_channel_kind", lambda conn, kind: None)
    monkeypatch.setattr(live_ep, "authenticate_optional", lambda authz: ("tok", "user-1"))
    monkeypatch.setattr(live_ep, "reserve_audio_seconds", lambda *a, **kw: 0)
    monkeypatch.setattr(live_ep, "build_agent_for_kind", lambda kind, params: (object(), object()))
    monkeypatch.setattr(live_ep, "resolve_persona_prompt", lambda cfg, kind, **kw: "p")
    session = FakeRealtimeSession(audio_format=AudioFormat(16000, 24000))
    monkeypatch.setattr(live_ep, "_session_for", lambda cfg, **kw: session)
    app = FastAPI()
    app.include_router(live_ep.router)
    state["session"] = session
    state["client"] = TestClient(app)
    return state


def _collect(ws, until, limit=30):
    frames = []
    for _ in range(limit):
        frame = ws.receive_json()
        frames.append(frame)
        if until(frame):
            return frames
    raise AssertionError(frames)


def _assistant_text(frames):
    return [f["text"] for f in frames if f.get("type") == "transcript" and f.get("role") == "assistant"]


@pytest.mark.parametrize("closer,expect", [
    ("turn_end", lambda f: f.get("type") == "turn_end"),
    ("interrupted", lambda f: f.get("type") == "interrupted"),
])
def test_a_held_word_is_released_before_the_response_ends(endpoint, closer, expect):
    """"fun" is held (it could become "functions."); whatever ends the
    response on the client must be preceded by it — never carried over."""
    session = endpoint["session"]
    session.supports_barge_in_confirmation = False
    with endpoint["client"].websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="transcript", role="assistant", text="That was fun", final=False))
        if closer == "turn_end":
            session.push(RealtimeEvent(type="turn_end", turn_reply=False))
        else:
            session.push(RealtimeEvent(type="interrupted"))
        frames = _collect(ws, expect)
    assert "".join(_assistant_text(frames)) == "That was fun", frames
    last_caption = max(i for i, f in enumerate(frames) if f.get("type") == "transcript"
                       and f.get("role") == "assistant")
    closing = next(i for i, f in enumerate(frames) if expect(f))
    assert last_caption < closing, frames


def test_a_held_word_does_not_break_the_next_holding_line_match(endpoint, monkeypatch):
    """Gemini shape: the previous response ended on a held word and was
    interrupted; the holding line that follows must still be marked
    out_of_band from its first fragment."""
    import asyncio as _a
    import time as _t

    from voice.live.config import LiveVoiceConfig
    from voice.live.delegate import DelegateResult

    endpoint["cfg"] = LiveVoiceConfig(enabled=True, model_id="m-1", provider="fake",
                                      progress_after_sec=0.05, progress_spoken="ثواني بس.")
    session = endpoint["session"]
    session.supports_out_of_band_speech = False
    session.supports_barge_in_confirmation = False

    async def _slow(**kwargs):
        await _a.sleep(0.6)
        return DelegateResult(spoken="x", display="x", charts=[])

    monkeypatch.setattr(live_ep, "run_delegated_turn", _slow)
    with endpoint["client"].websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="transcript", role="assistant", text="let me see what we can ask", final=False))
        session.push(RealtimeEvent(type="tool_call", call_id="c1", tool_name="ask_agent",
                                   arguments={"query": "q"}))
        for _ in range(50):
            if any(p.get("spoken") == "ثواني بس." for _c, p, _m in session.tool_results):
                break
            _t.sleep(0.02)
        session.push(RealtimeEvent(type="interrupted"))
        session.push(RealtimeEvent(type="transcript", role="assistant", text="ثواني بس.", final=False))
        frames = _collect(ws, lambda f: "ثواني" in (f.get("text") or ""))
    assert frames[-1]["text"] == "ثواني بس." and frames[-1].get("out_of_band") is True, frames
    assert "".join(_assistant_text(frames[:-1])) == "let me see what we can ask", frames


def test_provider_close_releases_the_held_word(endpoint):
    session = endpoint["session"]
    with endpoint["client"].websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        session.push(RealtimeEvent(type="transcript", role="assistant", text="goodbye and have fun", final=False))
        from voice.live.testing import _SENTINEL
        session.push(_SENTINEL)  # the service hangs up (end_stream, loop-safe)
        frames = _collect(ws, lambda f: "fun" in (f.get("text") or ""), limit=5)
    assert "".join(_assistant_text(frames)) == "goodbye and have fun"


def test_endpoint_never_forwards_the_leak(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from voice.live.base import AudioFormat
    from voice.live.config import LiveVoiceConfig
    from voice.live.testing import FakeRealtimeSession

    live_ep._connect_limiter._store.clear()
    cfg = LiveVoiceConfig(enabled=True, model_id="m-1", provider="fake")
    monkeypatch.setattr(live_ep, "_live_config_for", lambda _c, _k: cfg)
    monkeypatch.setattr(live_ep, "resolve_channel", lambda conn: None)
    monkeypatch.setattr(live_ep, "enforce_channel_kind", lambda conn, kind: None)
    monkeypatch.setattr(live_ep, "authenticate_optional", lambda authz: ("tok", "user-1"))
    monkeypatch.setattr(live_ep, "reserve_audio_seconds", lambda *a, **kw: 0)
    monkeypatch.setattr(live_ep, "build_agent_for_kind", lambda kind, params: (object(), object()))
    monkeypatch.setattr(live_ep, "resolve_persona_prompt", lambda cfg, kind, **kw: "p")
    session = FakeRealtimeSession(audio_format=AudioFormat(16000, 24000))
    monkeypatch.setattr(live_ep, "_session_for", lambda cfg, **kw: session)
    app = FastAPI()
    app.include_router(live_ep.router)

    frames = []
    with TestClient(app).websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()  # ready
        for delta in LIVE_DELTAS:
            session.push(RealtimeEvent(type="transcript", role="assistant", text=delta, final=False))
        session.push(RealtimeEvent(type="transcript", role="assistant", text="", final=True))
        session.push(RealtimeEvent(type="turn_end", turn_reply=True))
        for _ in range(20):
            frame = ws.receive_json()
            frames.append(frame)
            if frame.get("type") == "turn_end":
                break

    caption = "".join(f["text"] for f in frames if f.get("type") == "transcript")
    assert "functions" not in caption and "ask_agent" not in caption, caption
    assert caption.strip() == "Let me check."
    assert frames[-1] == {"type": "turn_end", "reply": True}


@pytest.mark.parametrize("split_at", range(len(LIVE_DELTAS) + 1))
def test_callers_late_final_mid_announce_does_not_release_the_leak(endpoint, split_at):
    """Azure completes the caller's transcript AFTER her reply has started —
    in the middle of the very announce that leaked. Her final landing between
    " functions" | ".ask" | "_agent" must not flush the held part."""
    session = endpoint["session"]
    with endpoint["client"].websocket_connect("/voice/live?agentKind=sample_voice&businessId=b1") as ws:
        ws.receive_json()
        for i, delta in enumerate(LIVE_DELTAS):
            if i == split_at:
                session.push(RealtimeEvent(type="transcript", role="user", text="What's today's revenue?", final=True))
            session.push(RealtimeEvent(type="transcript", role="assistant", text=delta, final=False))
        if split_at == len(LIVE_DELTAS):
            session.push(RealtimeEvent(type="transcript", role="user", text="What's today's revenue?", final=True))
        session.push(RealtimeEvent(type="transcript", role="assistant", text="", final=True))
        session.push(RealtimeEvent(type="turn_end", turn_reply=True))
        frames = _collect(ws, lambda f: f.get("type") == "turn_end")
    caption = "".join(_assistant_text(frames))
    assert "functions" not in caption and "ask" not in caption and "_agent" not in caption, caption
    assert caption.strip() == "Let me check."


# --- Gemini Live (google_genai), call log 2026-10: the model's OUTPUT
# TRANSCRIPTION carried its raw tool-call tokens. A transcription of the call
# RECORDING proved none of it was audible — it existed only in the text, which
# the portal showed in the call log. Shapes seen live, verbatim:
GEMINI_STARTCALL = (
    "let me check for you startcall:{query:How much do I owe and for which months?}"
    "let me check, how much you owe…"
)
GEMINI_STARTCALL_TRUNCATED = (
    "let me see. startcall:{query:Please send me the payment link to my registered number.}"
    "one sec, let me see about that startcall:{query:Please send me the payment link to"
    "one sec startcall:{query:Please send me the payment link to my registered number.}"
)
_LEAK_MARKERS = ("startcall", "query", "ask_agent", "{", "}", "ctrl")


def _leaks(out: str) -> list:
    return [m for m in _LEAK_MARKERS if m in out]


def _word_deltas(text):
    return re.findall(r"\s*\S+", text)


@pytest.mark.parametrize("text,expected", [
    (GEMINI_STARTCALL, "let me check for you let me check, how much you owe…"),
    ('ask_agent("How much do I owe, and for which months?")', ""),
    ('Sure. ask_agent\n{\n  "query": "How much do I owe?"\n}\nOne moment.', "Sure. One moment."),
    ('Sure. ask_agent {"query": "How much do I owe?"} One moment.', "Sure. One moment."),
    ('Sure.\n{\n  "query": "How much do I owe?"\n}\nOne moment.', "Sure. One moment."),
    ('Sure. {"query":"How much do I owe?"} One moment.', "Sure. One moment."),
    ("One sec <ctrl46>let me check.", "One sec let me check."),
    ("One sec call:ask_agent{query:How much do I owe?} let me check.", "One sec let me check."),
])
def test_gemini_tool_call_shapes_are_removed(text, expected):
    for out in (scrub_tool_syntax(text, unclosed="drop"),
                _stream(_word_deltas(text)),
                _stream(list(text))):  # worst case: one char per delta
        assert not _leaks(out), (text, out)
        assert " ".join(out.split()) == expected, (text, out)


def test_gemini_truncated_startcall_is_dropped_and_prose_survives():
    """The middle `startcall:{query:…link to` never closes before the next
    words are glued on ("toone sec"). The next opener ends it; the prose
    before the first token and between closed tokens survives."""
    for out in (_stream(_word_deltas(GEMINI_STARTCALL_TRUNCATED)),
                _stream(list(GEMINI_STARTCALL_TRUNCATED))):
        assert not _leaks(out), out
        assert out.startswith("let me see. one sec, let me see about that"), out


def test_startcall_split_across_deltas_is_held_then_removed():
    s = ToolSyntaxScrubber()
    out = s.feed("let me check for you ")
    out += s.feed("startcall:{query:How much do I")
    assert "startcall" not in out and "query" not in out
    out += s.feed(" owe?}")
    out += s.feed("let me check.")
    out += s.flush()
    assert not _leaks(out), out
    assert " ".join(out.split()) == "let me check for you let me check."


def test_unclosed_startcall_at_turn_end_is_dropped():
    s = ToolSyntaxScrubber()
    out = s.feed("one sec startcall:{query:Please send me the payment link to")
    assert out == "one sec "
    assert s.flush() == ""


def test_unclosed_json_object_at_turn_end_is_dropped():
    s = ToolSyntaxScrubber()
    out = s.feed('Sure. ask_agent\n{\n  "query": "How much')
    assert "ask_agent" not in out and "query" not in out
    assert s.flush() == ""


@pytest.mark.parametrize("text", [
    # Braces in prose are KEPT: only a `{` opening a `query` key (or one that
    # follows a tool opener) is tool syntax. The model has no reason to speak
    # a brace, so this is about never eating real words, not about braces.
    "Use the {name} placeholder in the template.",
    "My query: is the link still valid?",
    "Let's start. Call me back at 5, or start a call: whenever you like.",
    "حاضر، المبلغ المستحق 1,250 ريال عن شهري يوليو وأغسطس.",
    "تمام، راح أرسل لك رابط الدفع على رقمك المسجل.",
    "Please send me the payment link to my registered number.",
    "I'll check < 5 minutes, promise.",
])
def test_prose_near_the_gemini_shapes_is_untouched(text):
    assert scrub_tool_syntax(text) == text
    assert _stream(_word_deltas(text)) == text
    assert _stream(list(text)) == text


def test_hold_cap_on_an_unclosed_startcall_strips_the_opener_and_key():
    s = ToolSyntaxScrubber()
    out = s.feed("ok startcall:{query:" + "x " * 150)
    assert "startcall" not in out and "query" not in out and "{" not in out
    assert out.count("x") == 150 and not s.holding


# --- Review of #188: an unclosed block must not eat the rest of the turn.
PROBE_UNCLOSED = (
    "one sec startcall:{query:…link toone sec, I'm checking your account now "
    "and it shows 1,250 AED due."
)


@pytest.mark.parametrize("split", ["whole", "words", "chars"])
def test_an_unclosed_block_with_no_closer_releases_the_speech_after_it(split):
    deltas = {"whole": [PROBE_UNCLOSED], "words": _word_deltas(PROBE_UNCLOSED),
              "chars": list(PROBE_UNCLOSED)}[split]
    out = _stream(deltas)
    assert not _leaks(out), out
    assert out.startswith("one sec "), out
    assert "I'm checking your account now and it shows 1,250 AED due." in out, out


def test_an_unbalanced_inner_brace_does_not_eat_the_turn():
    text = "Sure. startcall:{query:find {unbalanced braces} done. Then more speech here."
    for out in (_stream([text]), _stream(_word_deltas(text)), _stream(list(text))):
        assert "startcall" not in out and "query" not in out, out
        assert out.startswith("Sure.") and out.endswith("Then more speech here."), out


def test_an_unclosed_block_is_bounded_mid_stream_without_waiting_for_turn_end():
    s = ToolSyntaxScrubber()
    out = s.feed("ok startcall:{query:" + "word " * 40)
    out += s.feed("and now real speech continues here.")
    assert "startcall" not in out and "query" not in out
    assert "real speech continues" in out, out


def test_a_force_ended_block_is_logged(caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="voice.live.speech_hygiene"):
        _stream([PROBE_UNCLOSED])
    assert any("force-ended" in r.getMessage() for r in caplog.records), caplog.records


def test_the_hold_cap_does_not_leak_the_closer():
    s = ToolSyntaxScrubber()
    out = s.feed("ok startcall:{query:")
    for i in range(60):
        out += s.feed(f" w{i}")
    out += s.feed("} after.")
    out += s.flush()
    assert "}" not in out and "startcall" not in out and "query" not in out, out
    assert out.rstrip().endswith("after."), out


def test_a_removed_token_between_chunks_leaves_no_double_space():
    out = _stream(["let me check ", "startcall:{query:x y z}", " ", "one sec."])
    assert "  " not in out, repr(out)
    assert out == "let me check one sec.", repr(out)
