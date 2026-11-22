"""Pure parts of the voice-eval caller driver: prompt build, reply parse,
transcript rendering. The async loop is integration-tested separately."""
from admin.voice_eval_driver import (
    HANGUP_TOKEN,
    build_caller_prompt,
    collect_assistant_turns,
    collect_proposals,
    parse_caller_reply,
    render_transcript,
    _normalize_llm_content,
)


def test_collect_proposals_extracts_payloads_in_order():
    frames = [
        {"type": "ready"},
        {"type": "transcript", "role": "assistant", "text": "Queued.", "final": True},
        {"type": "proposal", "proposal": {"type": "allergy", "substance": "Peanut"}},
        {"type": "turn_end"},
        {"type": "proposal", "proposal": {"type": "lab_order", "test_name": "CBC"}},
        {"type": "proposal"},  # malformed: no payload — skipped, not crashed
        {"type": "display", "text": "x"},
    ]
    assert collect_proposals(frames) == [
        {"type": "allergy", "substance": "Peanut"},
        {"type": "lab_order", "test_name": "CBC"},
    ]

CALLER = {
    "persona": "Ali, a brusque Orchard merchant, speaks Arabic",
    "goal": "Change payout IBAN to SA0380000000608010167519",
    "facts": {"tid": "TID-1001"},
    "max_turns": 8,
}


def test_prompt_contains_persona_goal_facts_and_budget():
    msgs = build_caller_prompt(CALLER, [], turns_left=8)
    assert len(msgs) == 2
    system = msgs[0].content
    assert "Ali, a brusque Orchard merchant" in system
    assert "SA0380000000608010167519" in system
    assert "TID-1001" in system
    assert "8" in system  # turns_left surfaced
    assert HANGUP_TOKEN in system


def test_prompt_includes_transcript_and_opens_when_empty():
    empty = build_caller_prompt(CALLER, [], turns_left=8)[1].content
    assert "no one has spoken yet" in empty.lower() or "start the call" in empty.lower()
    turns = [
        {"role": "assistant", "text": "Hello! How can I help you today?"},
        {"role": "caller", "text": "I want to change my IBAN."},
    ]
    with_history = build_caller_prompt(CALLER, turns, turns_left=7)[1].content
    assert "ASSISTANT: Hello! How can I help you today?" in with_history
    assert "CALLER: I want to change my IBAN." in with_history


def test_parse_plain_utterance():
    assert parse_caller_reply("I want to change my IBAN.") == (
        "I want to change my IBAN.", False,
    )


def test_parse_hangup_variants():
    assert parse_caller_reply("[HANGUP]") == (None, True)
    assert parse_caller_reply("  [hangup]  ") == (None, True)
    # Token embedded after a final utterance: speak it, then hang up.
    utt, hangup = parse_caller_reply("Thanks, that's all. [HANGUP]")
    assert utt == "Thanks, that's all."
    assert hangup is True


def test_parse_strips_quotes_and_labels():
    assert parse_caller_reply('"Hello there."') == ("Hello there.", False)
    assert parse_caller_reply("CALLER: Hello there.") == ("Hello there.", False)


def test_parse_strips_curly_quotes():
    # Test curly/smart quotes (U+201C and U+201D)
    assert parse_caller_reply('"Hello there."') == ("Hello there.", False)


def test_parse_empty_is_hangup():
    # A caller LLM that returns nothing can't keep the call alive.
    assert parse_caller_reply("") == (None, True)
    assert parse_caller_reply("   ") == (None, True)


def test_render_transcript_roundtrip():
    turns = [
        {"role": "assistant", "text": "Hello!"},
        {"role": "caller", "text": "Hi."},
    ]
    assert render_transcript(turns) == "ASSISTANT: Hello!\nCALLER: Hi."


def test_normalize_llm_content_plain_string():
    import types

    raw = types.SimpleNamespace(content="Hello there.")
    assert _normalize_llm_content(raw) == "Hello there."


def test_normalize_llm_content_joins_content_blocks():
    # Claude-style list-of-dicts content, as seen off a LangChain chat model.
    import types

    raw = types.SimpleNamespace(content=[
        {"type": "text", "text": "Hello"},
        {"type": "text", "text": "there."},
    ])
    assert _normalize_llm_content(raw) == "Hello there."


# ── collect_assistant_turns (Finding 2: live-mode transcript fragmentation) ──

def test_collect_assistant_turns_sim_style_one_turn_per_final_frame():
    # Sim emits exactly one final=True frame per utterance -- must stay
    # exactly the old per-frame behavior (the 4 driver-loop integration
    # tests in test_voice_eval_driver_loop.py depend on this unmodified).
    frames = [
        {"type": "transcript", "role": "assistant", "text": "Hello there.", "final": True},
        {"type": "turn_end"},
        {"type": "transcript", "role": "assistant", "text": "You said: hi.", "final": True},
        {"type": "turn_end"},
    ]
    assert collect_assistant_turns(frames) == ["Hello there.", "You said: hi."]


def test_collect_assistant_turns_delta_style_combines_into_one_turn():
    # Real providers (azure_voice_live, gemini_live) stream deltas
    # final=False, closed by an empty final=True frame.
    frames = [
        {"type": "transcript", "role": "assistant", "text": "Your", "final": False},
        {"type": "transcript", "role": "assistant", "text": "sales were", "final": False},
        {"type": "transcript", "role": "assistant", "text": "500 riyals", "final": False},
        {"type": "transcript", "role": "assistant", "text": "", "final": True},
        {"type": "turn_end"},
    ]
    assert collect_assistant_turns(frames) == ["Your sales were 500 riyals"]


def test_collect_assistant_turns_flushes_on_turn_end_without_final():
    # Defensive: even absent a final=True closer, a turn_end boundary must
    # still flush the accumulated deltas rather than losing them or
    # bleeding them into the next turn.
    frames = [
        {"type": "transcript", "role": "assistant", "text": "Partial", "final": False},
        {"type": "turn_end"},
        {"type": "transcript", "role": "assistant", "text": "Next turn.", "final": True},
    ]
    assert collect_assistant_turns(frames) == ["Partial", "Next turn."]


def test_collect_assistant_turns_ignores_non_assistant_and_other_frames():
    frames = [
        {"type": "transcript", "role": "user", "text": "hi there", "final": True},
        {"type": "audio"},
        {"type": "display", "text": "You said: hi there"},
        {"type": "transcript", "role": "assistant", "text": "Hi!", "final": True},
    ]
    assert collect_assistant_turns(frames) == ["Hi!"]


def test_collect_assistant_turns_empty_frames_returns_empty():
    assert collect_assistant_turns([]) == []
