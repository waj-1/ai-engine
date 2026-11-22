import asyncio
import time

import pytest

import voice.live.delegate as delegate_mod
from voice.live.delegate import run_delegated_turn
from voice.live.speakable import ON_SCREEN


def _collect(sink):
    """An `on_sentence` that records what was actually delivered."""
    async def _on_sentence(text):
        sink.append(text)

    return _on_sentence


def _fake_run_turn(events, *, delay=0.0):
    async def _gen(**kwargs):
        for event in events:
            if delay:
                await asyncio.sleep(delay)
            yield event

    return _gen


async def test_answer_events_accumulate_into_display(monkeypatch):
    monkeypatch.setattr(
        delegate_mod, "run_turn",
        _fake_run_turn([
            {"type": "answer", "content": "Net sales were "},
            {"type": "answer", "content": "AED 1,240."},
            {"type": "done"},
        ]),
    )
    result = await run_delegated_turn(agent=object(), profile=object(), message="q", identity=object())
    assert result.display == "Net sales were AED 1,240."
    assert result.error is False


async def test_spoken_prefers_the_agent_authored_summary(monkeypatch):
    monkeypatch.setattr(
        delegate_mod, "run_turn",
        _fake_run_turn([
            {"type": "answer", "content": "| Store | Net |\n|---|---|\n| Dubai | 900 |"},
            {"type": "turn_meta", "metadata": {"spoken_summary": "Dubai took nine hundred dirhams."}},
            {"type": "done"},
        ]),
    )
    result = await run_delegated_turn(agent=object(), profile=object(), message="q", identity=object())
    assert result.spoken == "Dubai took nine hundred dirhams."


async def test_top_level_spoken_summary_event_wins_over_fallback(monkeypatch):
    # The live channel: `run_turn` forwards any event type it doesn't
    # special-case, so a top-level `spoken_summary` event (unlike `turn_meta`,
    # which `run_turn` swallows for its own telemetry) is how an agent
    # actually gets a spoken summary through today.
    monkeypatch.setattr(
        delegate_mod, "run_turn",
        _fake_run_turn([
            {"type": "answer", "content": "**AED 1,240** collected."},
            {"type": "spoken_summary", "content": "You collected twelve forty dirhams."},
            {"type": "done"},
        ]),
    )
    result = await run_delegated_turn(agent=object(), profile=object(), message="q", identity=object())
    assert result.spoken == "You collected twelve forty dirhams."


async def test_charts_only_turn_speaks_the_on_screen_pointer(monkeypatch):
    chart = {"type": "chart", "chart_id": "c1", "title": "Net", "caption": "", "spec": {"mark": "bar"}}
    monkeypatch.setattr(
        delegate_mod, "run_turn",
        _fake_run_turn([chart, {"type": "done"}]),
    )
    result = await run_delegated_turn(agent=object(), profile=object(), message="q", identity=object())
    assert result.display == ""
    assert result.spoken == ON_SCREEN
    assert result.error is False


async def test_spoken_falls_back_to_to_speakable(monkeypatch):
    monkeypatch.setattr(
        delegate_mod, "run_turn",
        _fake_run_turn([
            {"type": "answer", "content": "**AED 1,240** collected."},
            {"type": "done"},
        ]),
    )
    result = await run_delegated_turn(agent=object(), profile=object(), message="q", identity=object())
    assert result.spoken == "AED 1,240 collected."


async def test_charts_are_collected_verbatim(monkeypatch):
    chart = {"type": "chart", "chart_id": "c1", "title": "Net", "caption": "", "spec": {"mark": "bar"}}
    monkeypatch.setattr(
        delegate_mod, "run_turn",
        _fake_run_turn([{"type": "answer", "content": "See chart."}, chart, {"type": "done"}]),
    )
    result = await run_delegated_turn(agent=object(), profile=object(), message="q", identity=object())
    assert result.charts == [chart]


async def test_agent_error_event_marks_the_result_and_apologises(monkeypatch):
    monkeypatch.setattr(
        delegate_mod, "run_turn",
        _fake_run_turn([{"type": "error", "message": "tool exploded"}, {"type": "done"}]),
    )
    result = await run_delegated_turn(agent=object(), profile=object(), message="q", identity=object())
    assert result.error is True
    assert result.spoken
    # The error text is for the log, never the screen (user voice test
    # 2026-09-30 showed Azure's raw 400 body); the screen gets the apology.
    assert "tool exploded" not in result.display
    assert result.display == result.spoken


async def test_timeout_yields_an_apology_not_an_exception(monkeypatch):
    monkeypatch.setattr(
        delegate_mod, "run_turn",
        _fake_run_turn([{"type": "answer", "content": "slow"}, {"type": "done"}], delay=0.2),
    )
    result = await run_delegated_turn(
        agent=object(), profile=object(), message="q", identity=object(), timeout_sec=0.01
    )
    assert result.error is True
    assert result.spoken


async def test_a_raising_agent_is_contained(monkeypatch):
    async def _boom(**kwargs):
        raise RuntimeError("upstream down")
        yield  # pragma: no cover — makes this an async generator

    monkeypatch.setattr(delegate_mod, "run_turn", _boom)
    result = await run_delegated_turn(agent=object(), profile=object(), message="q", identity=object())
    assert result.error is True


async def test_raw_exception_text_never_reaches_the_client_facing_display(monkeypatch):
    """Smaller finding: `display` used to fall back to
    f"{type(exc).__name__}: {exc}" -- raw internal exception text landing in
    the client-facing transcript. The detail must be logged, not shown."""

    async def _boom(**kwargs):
        raise RuntimeError("db connection to internal-host:5432 refused")
        yield  # pragma: no cover — makes this an async generator

    monkeypatch.setattr(delegate_mod, "run_turn", _boom)
    result = await run_delegated_turn(agent=object(), profile=object(), message="q", identity=object())
    assert result.error is True
    assert "RuntimeError" not in result.display
    assert "internal-host" not in result.display
    assert "db connection" not in result.display
    assert result.display  # still non-empty -- a neutral message, not nothing
    assert result.spoken == delegate_mod.FAILURE_SPOKEN


async def test_an_agent_that_produces_nothing_is_a_failure_not_dead_air(monkeypatch):
    """Reproduced live: a client profile whose MCP tools were unbound logged
    'resolved to ZERO tools' and returned an empty answer. Handing the model an
    empty `spoken` makes it read nothing, so the caller hears dead air with no
    error. That must be reported as a failure instead."""
    monkeypatch.setattr(delegate_mod, "run_turn", _fake_run_turn([{"type": "done"}]))
    result = await run_delegated_turn(
        agent=object(), profile=object(), message="q", identity=object()
    )
    assert result.error is True
    assert result.spoken == delegate_mod.FAILURE_SPOKEN
    assert result.spoken.strip()


async def test_whitespace_only_answer_is_also_treated_as_nothing(monkeypatch):
    monkeypatch.setattr(
        delegate_mod, "run_turn",
        _fake_run_turn([{"type": "answer", "content": "   \n  "}, {"type": "done"}]),
    )
    result = await run_delegated_turn(
        agent=object(), profile=object(), message="q", identity=object()
    )
    assert result.error is True
    assert result.spoken == delegate_mod.FAILURE_SPOKEN


async def test_failed_sentence_delivery_is_not_recorded_as_streamed(monkeypatch):
    """Review finding (Critical): `streamed` must list only sentences that
    were actually DELIVERED. Recording an undelivered one suppresses the
    whole-answer fallback keyed on `streamed`, and the provider then waits
    forever for a tool result that never comes."""
    monkeypatch.setattr(
        delegate_mod, "run_turn",
        _fake_run_turn([
            {"type": "answer", "content": "Sales were 900 SAR. Across 12 sales.\n"},
            {"type": "done"},
        ]),
    )

    async def _always_fails(sentence):
        raise RuntimeError("provider hiccup")

    result = await run_delegated_turn(
        agent=object(), profile=object(), message="q", identity=object(),
        on_sentence=_always_fails,
    )
    assert result.streamed == []          # nothing was delivered
    assert result.error is False
    assert "900" in result.spoken          # whole-answer fallback intact


async def test_partial_sentence_failure_records_only_what_was_delivered(monkeypatch):
    monkeypatch.setattr(
        delegate_mod, "run_turn",
        _fake_run_turn([
            {"type": "answer", "content": "First sentence here. Second sentence here.\n"},
            {"type": "done"},
        ]),
    )
    calls = {"n": 0}

    async def _fails_second(sentence):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("hiccup")

    result = await run_delegated_turn(
        agent=object(), profile=object(), message="q", identity=object(),
        on_sentence=_fails_second,
    )
    assert result.streamed == ["First sentence here."]


async def test_streaming_a_table_speaks_on_screen_once_not_per_row(monkeypatch):
    """Review finding: each markdown table row is 'visual' on its own, so the
    per-sentence transform said 'The figures are on your screen.' once per
    row. Consecutive identical speakables must collapse."""
    monkeypatch.setattr(
        delegate_mod, "run_turn",
        _fake_run_turn([
            {"type": "answer", "content": "Here it is:\n| a | 1 |\n| b | 2 |\n| c | 3 |\n"},
            {"type": "done"},
        ]),
    )
    got = []

    async def _collect(sentence):
        got.append(sentence)

    await run_delegated_turn(
        agent=object(), profile=object(), message="q", identity=object(),
        on_sentence=_collect,
    )
    from voice.live.speakable import ON_SCREEN
    assert got.count(ON_SCREEN) <= 1, got


async def test_streaming_skips_sentences_with_no_letters_or_digits(monkeypatch):
    monkeypatch.setattr(
        delegate_mod, "run_turn",
        _fake_run_turn([
            {"type": "answer", "content": "```\nReal sentence here.\n"},
            {"type": "done"},
        ]),
    )
    got = []

    async def _collect(sentence):
        got.append(sentence)

    await run_delegated_turn(
        agent=object(), profile=object(), message="q", identity=object(),
        on_sentence=_collect,
    )
    assert all(any(c.isalnum() for c in s) for s in got), got


async def test_the_delegated_turn_is_marked_as_a_voice_turn(monkeypatch):
    # Agents key off context["voice_turn"] to skip the title/suggestions LLM
    # round-trips a caller would otherwise sit through: suggestion chips are
    # untappable on a call, and the delegate only breaks on `done`, so those
    # awaited calls land squarely inside the time-to-first-speech.
    captured = {}

    async def _gen(**kwargs):
        captured.update(kwargs)
        yield {"type": "answer", "content": "Hello."}
        yield {"type": "done"}

    monkeypatch.setattr(delegate_mod, "run_turn", _gen)
    await run_delegated_turn(
        agent=object(), profile=object(), message="q", identity=object()
    )
    assert (captured.get("extra_context") or {}).get("voice_turn") is True


async def test_delegated_turn_opens_a_tagged_root_span(monkeypatch):
    # The HTTP chat endpoints get their tagged request-root span from
    # sse_response; the voice delegate has no HTTP request, so it must open
    # the equivalent root itself. Without it every voice turn lands as
    # untagged top-level traces that a scoped build's trace explorer drops —
    # voice traffic then shows NOTHING in /admin observability.
    opened = {}

    from contextlib import contextmanager

    @contextmanager
    def _fake_root_span(name, attributes=None):
        opened["name"] = name
        opened["attributes"] = attributes
        yield None

    monkeypatch.setattr(delegate_mod, "root_span", _fake_root_span)
    monkeypatch.setattr(
        delegate_mod, "run_turn",
        _fake_run_turn([
            {"type": "answer", "content": "Hello."},
            {"type": "done"},
        ]),
    )

    attrs = {"waj.agent_kind": "sample_voice", "waj.env": "staging"}
    result = await run_delegated_turn(
        agent=object(), profile=object(), message="q", identity=object(),
        trace_attributes=attrs,
    )

    assert result.error is False
    assert opened["name"] == "voice.agent_turn"
    assert opened["attributes"] == attrs


def test_failure_line_follows_the_session_language():
    """Spoken on every error, timeout and empty-answer path — the moments a
    caller is already unsure. An English apology mid-Arabic call reads as a
    second failure."""
    from voice.live.delegate import FAILURE_SPOKEN, failure_spoken_for

    ar = failure_spoken_for("ar-SA")
    assert ar != FAILURE_SPOKEN
    assert any("؀" <= c <= "ۿ" for c in ar)
    assert failure_spoken_for("en-US") == FAILURE_SPOKEN
    assert failure_spoken_for(None) == FAILURE_SPOKEN
    assert failure_spoken_for("de-DE") == FAILURE_SPOKEN      # unmapped -> English
    assert failure_spoken_for("ar", override="عذراً") == "عذراً"


async def test_a_failed_arabic_turn_is_apologised_for_in_arabic(monkeypatch):
    import voice.live.delegate as mod

    async def _boom(**kwargs):
        raise RuntimeError("upstream exploded")
        yield  # pragma: no cover

    monkeypatch.setattr(mod, "run_turn", _boom)
    result = await mod.run_delegated_turn(
        agent=object(), profile=object(), message="كم مبيعاتي؟",
        identity=object(), language="ar-SA",
    )
    assert result.error is True
    assert result.spoken == mod.failure_spoken_for("ar-SA")
    assert any("؀" <= c <= "ۿ" for c in result.spoken)


async def test_an_operator_override_wins_over_the_builtin(monkeypatch):
    import voice.live.delegate as mod

    async def _boom(**kwargs):
        raise RuntimeError("nope")
        yield  # pragma: no cover

    monkeypatch.setattr(mod, "run_turn", _boom)
    result = await mod.run_delegated_turn(
        agent=object(), profile=object(), message="q", identity=object(),
        language="ar-SA", failure_spoken="ما قدرت أجيبها، جرب مرة ثانية.",
    )
    assert result.spoken == "ما قدرت أجيبها، جرب مرة ثانية."


async def test_flow_signals_drained_from_the_agent_channel(monkeypatch):
    """The tool closures append protected-flow markers to the AGENT's
    per-session list; the delegate drains them onto the result exactly once
    (a second turn with no tool activity reports none)."""
    from voice.live import delegate as d

    class _Agent:
        def __init__(self):
            self.flow_events = ["send_otp"]

    agent = _Agent()

    async def _fake_run_turn(**kwargs):
        yield {"type": "answer", "content": "Code sent."}

    monkeypatch.setattr(d, "run_turn", _fake_run_turn)
    result = await d.run_delegated_turn(
        agent=agent, profile=object(), message="m", identity=object(),
    )
    assert result.flow_signals == ["send_otp"]
    assert agent.flow_events == []          # drained, not copied

    result2 = await d.run_delegated_turn(
        agent=agent, profile=object(), message="m2", identity=object(),
    )
    assert result2.flow_signals == []


# ── Proposals + extra turn context ──


async def test_proposal_events_are_collected_verbatim(monkeypatch):
    proposal_event = {
        "type": "proposal",
        "proposal": {"type": "address_change", "street": "12 Harbor Road",
                     "confidence": 0.92, "source_text": "I moved to 12 Harbor Road"},
    }
    monkeypatch.setattr(
        delegate_mod, "run_turn",
        _fake_run_turn([
            proposal_event,
            {"type": "answer", "content": "Queued the new address for your review."},
            {"type": "done"},
        ]),
    )
    result = await run_delegated_turn(agent=object(), profile=object(), message="q", identity=object())
    assert result.proposals == [proposal_event]
    assert result.error is False


async def test_proposals_survive_a_later_agent_error(monkeypatch):
    """A proposal extracted before the turn errored is still a validated,
    operator-reviewed suggestion -- dropping it silently loses a dictated
    order."""
    proposal_event = {
        "type": "proposal",
        "proposal": {"type": "order", "item": "Router upgrade",
                     "confidence": 0.9, "source_text": "order the router upgrade"},
    }
    monkeypatch.setattr(
        delegate_mod, "run_turn",
        _fake_run_turn([
            proposal_event,
            {"type": "error", "message": "tool timeout"},
        ]),
    )
    result = await run_delegated_turn(agent=object(), profile=object(), message="q", identity=object())
    assert result.error is True
    assert result.proposals == [proposal_event]


async def test_default_result_has_no_proposals(monkeypatch):
    monkeypatch.setattr(
        delegate_mod, "run_turn",
        _fake_run_turn([{"type": "answer", "content": "Hello."}, {"type": "done"}]),
    )
    result = await run_delegated_turn(agent=object(), profile=object(), message="q", identity=object())
    assert result.proposals == []


async def test_extra_context_is_merged_and_voice_turn_cannot_be_clobbered(monkeypatch):
    seen = {}

    async def _capture(**kwargs):
        seen.update(kwargs)
        yield {"type": "answer", "content": "ok"}
        yield {"type": "done"}

    monkeypatch.setattr(delegate_mod, "run_turn", _capture)
    await run_delegated_turn(
        agent=object(), profile=object(), message="q", identity=object(),
        extra_context={"account_id": "a-1", "ticket_id": "t-1", "voice_turn": False},
    )
    assert seen["extra_context"]["account_id"] == "a-1"
    assert seen["extra_context"]["ticket_id"] == "t-1"
    assert seen["extra_context"]["voice_turn"] is True


async def test_no_extra_context_still_marks_the_voice_turn(monkeypatch):
    seen = {}

    async def _capture(**kwargs):
        seen.update(kwargs)
        yield {"type": "answer", "content": "ok"}
        yield {"type": "done"}

    monkeypatch.setattr(delegate_mod, "run_turn", _capture)
    await run_delegated_turn(agent=object(), profile=object(), message="q", identity=object())
    assert seen["extra_context"] == {
        "voice_turn": True,
        "deterministic_render": False,
        # The delegate's own per-stage timing channel — empty until the
        # stage owners (chat_session history, the agent loop) fill it.
        "stage_timings": {},
    }


async def test_thought_events_reach_on_progress(monkeypatch):
    monkeypatch.setattr(
        delegate_mod, "run_turn",
        _fake_run_turn([
            {"type": "thought", "content": "Using tool: Search Documentation"},
            {"type": "thought", "content": "Retrieved data from Search Documentation"},
            {"type": "answer", "content": "WAJ supports online booking."},
            {"type": "done"},
        ]),
    )
    seen = []

    async def _progress(text):
        seen.append(text)

    result = await run_delegated_turn(
        agent=object(), profile=object(), message="q", identity=object(),
        on_progress=_progress,
    )
    assert seen == [
        "Using tool: Search Documentation",
        "Retrieved data from Search Documentation",
    ]
    # Thoughts are status, not answer content.
    assert result.display == "WAJ supports online booking."


async def test_thought_events_are_dropped_without_on_progress(monkeypatch):
    monkeypatch.setattr(
        delegate_mod, "run_turn",
        _fake_run_turn([
            {"type": "thought", "content": "Using tool: Search Documentation"},
            {"type": "answer", "content": "Answer."},
            {"type": "done"},
        ]),
    )
    result = await run_delegated_turn(agent=object(), profile=object(), message="q", identity=object())
    assert result.display == "Answer."
    assert result.error is False


async def test_a_failing_on_progress_never_aborts_the_turn(monkeypatch):
    monkeypatch.setattr(
        delegate_mod, "run_turn",
        _fake_run_turn([
            {"type": "thought", "content": "Using tool: Search Documentation"},
            {"type": "answer", "content": "Answer survives."},
            {"type": "done"},
        ]),
    )

    async def _progress(text):
        raise RuntimeError("socket went away")

    result = await run_delegated_turn(
        agent=object(), profile=object(), message="q", identity=object(),
        on_progress=_progress,
    )
    assert result.error is False
    assert result.display == "Answer survives."


# ---------------------------------------------------------------------------
# The chunker's TAIL — the fragment after the last sentence boundary — used to
# be flushed only once the whole `run_turn` generator had finished. That is
# strictly later than the answer being finished: run_turn awaits
# `asyncio.gather(_persist_transcript(), _emit_telemetry())` before it yields
# `done` (~0.58s p50 measured in prod, 62ms on the demo VM's local Supabase),
# so the last thing the caller hears was queued behind two Supabase round
# trips it does not depend on.
# ---------------------------------------------------------------------------


async def test_tail_is_spoken_before_the_post_answer_bookkeeping(monkeypatch):
    """Ordering, not wall time: assert the tail lands BEFORE the work that
    run_turn does between the answer and `done`. A timing assertion would be
    flaky; a sequence assertion pins the same bug deterministically."""
    order = []

    async def _gen(**kwargs):
        yield {"type": "answer", "content": "Settlement cleared. Net was AED 1,240"}
        # Every streaming agent in this repo yields `usage` right after its
        # final answer text; run_turn's persistence runs after that.
        yield {"type": "usage", "model": "m", "input_tokens": 1, "output_tokens": 1}
        order.append("bookkeeping")
        await asyncio.sleep(0.05)  # stands in for the transcript+telemetry gather
        yield {"type": "done"}

    monkeypatch.setattr(delegate_mod, "run_turn", _gen)

    spoken = []

    async def _on_sentence(text):
        order.append("spoke:%s" % text)
        spoken.append(text)

    result = await run_delegated_turn(
        agent=object(), profile=object(), message="q", identity=object(),
        on_sentence=_on_sentence,
    )

    assert spoken == ["Settlement cleared.", "Net was AED 1,240"]
    # The tail ("Net was AED 1,240", no terminal punctuation) must precede the
    # bookkeeping, not follow it.
    assert order.index("spoke:Net was AED 1,240") < order.index("bookkeeping")
    assert result.streamed == spoken


async def test_tail_is_still_flushed_for_an_agent_that_emits_no_usage(monkeypatch):
    """The backstop. `usage` is a convention, not part of the run_turn
    contract — an agent that skips it must still get its tail spoken."""
    monkeypatch.setattr(
        delegate_mod, "run_turn",
        _fake_run_turn([
            {"type": "answer", "content": "All good. No trailing stop here"},
            {"type": "done"},
        ]),
    )
    spoken = []

    result = await run_delegated_turn(
        agent=object(), profile=object(), message="q", identity=object(),
        on_sentence=_collect(spoken),
    )
    assert spoken == ["All good.", "No trailing stop here"]
    assert result.streamed == spoken


async def test_tail_is_not_spoken_twice_when_usage_and_done_both_arrive(monkeypatch):
    """`_flush_tail` runs on `usage`, again on `done`, and once more as the
    post-loop backstop. Speaking the answer's last fragment three times is
    exactly the duplicate-answer failure mode this codebase keeps hitting, so
    pin the idempotence rather than trusting it."""
    monkeypatch.setattr(
        delegate_mod, "run_turn",
        _fake_run_turn([
            {"type": "answer", "content": "Done. Nothing else to add"},
            {"type": "usage", "model": "m", "input_tokens": 1, "output_tokens": 1},
            {"type": "done"},
        ]),
    )
    spoken = []

    await run_delegated_turn(
        agent=object(), profile=object(), message="q", identity=object(),
        on_sentence=_collect(spoken),
    )
    assert spoken == ["Done.", "Nothing else to add"]


async def test_an_errored_turn_never_speaks_a_half_generated_tail(monkeypatch):
    """Unchanged contract, re-pinned because the flush moved: a turn that
    failed mid-sentence speaks the failure line, not the fragment it had."""
    monkeypatch.setattr(
        delegate_mod, "run_turn",
        _fake_run_turn([
            {"type": "answer", "content": "Your settlement total is"},
            {"type": "error", "message": "tool exploded"},
            {"type": "done"},
        ]),
    )
    spoken = []

    result = await run_delegated_turn(
        agent=object(), profile=object(), message="q", identity=object(),
        on_sentence=_collect(spoken),
    )
    assert spoken == []
    assert result.error is True


async def test_prefetch_result_reaches_the_agent_context(monkeypatch):
    captured = {}

    async def _fake_run_turn(*, agent, message, identity, profile, extra_context):
        captured.update(extra_context)
        yield {"type": "answer", "content": "ok"}
        yield {"type": "done"}

    class _Agent:
        async def prefetch_voice_context(self, message, *, match_count):
            return f"## Doc\nchunks for {message} x{match_count}"

    monkeypatch.setattr("voice.live.delegate.run_turn", _fake_run_turn)

    await run_delegated_turn(
        agent=_Agent(), profile=object(), message="error 119",
        identity=object(), prefetch_enabled=True, prefetch_match_count=3,
    )
    assert captured["prefetched_docs"] == "## Doc\nchunks for error 119 x3"
    assert captured["voice_turn"] is True


async def test_prefetch_is_not_run_when_disabled(monkeypatch):
    captured = {}
    called = []

    async def _fake_run_turn(*, agent, message, identity, profile, extra_context):
        captured.update(extra_context)
        yield {"type": "done"}

    class _Agent:
        async def prefetch_voice_context(self, message, *, match_count):
            called.append(message)
            return "should not be used"

    monkeypatch.setattr("voice.live.delegate.run_turn", _fake_run_turn)
    await run_delegated_turn(
        agent=_Agent(), profile=object(), message="hi",
        identity=object(), prefetch_enabled=False,
    )
    assert called == []
    assert "prefetched_docs" not in captured


async def test_a_raising_prefetch_never_aborts_the_turn(monkeypatch):
    async def _fake_run_turn(*, agent, message, identity, profile, extra_context):
        yield {"type": "answer", "content": "answered anyway"}
        yield {"type": "done"}

    class _Agent:
        async def prefetch_voice_context(self, message, *, match_count):
            raise RuntimeError("retrieval exploded")

    monkeypatch.setattr("voice.live.delegate.run_turn", _fake_run_turn)
    result = await run_delegated_turn(
        agent=_Agent(), profile=object(), message="hi",
        identity=object(), prefetch_enabled=True,
    )
    assert result.error is False
    assert "answered anyway" in result.spoken


async def test_an_agent_without_the_hook_is_unaffected(monkeypatch):
    captured = {}

    async def _fake_run_turn(*, agent, message, identity, profile, extra_context):
        captured.update(extra_context)
        yield {"type": "done"}

    monkeypatch.setattr("voice.live.delegate.run_turn", _fake_run_turn)
    await run_delegated_turn(
        agent=object(), profile=object(), message="hi",
        identity=object(), prefetch_enabled=True,
    )
    assert "prefetched_docs" not in captured


async def test_a_hanging_prefetch_never_hangs_the_turn(monkeypatch):
    """The turn's own `timeout_sec` guards `run_turn`, not the prefetch call
    that runs before it -- the prefetch needs its own bound
    (`_PREFETCH_TIMEOUT_SEC`) or a stuck embedding/RPC blocks the turn with
    nothing to stop it. Shrink the constant rather than actually waiting it
    out, so the test stays fast."""
    captured = {}

    async def _fake_run_turn(*, agent, message, identity, profile, extra_context):
        captured.update(extra_context)
        yield {"type": "answer", "content": "answered anyway"}
        yield {"type": "done"}

    class _Agent:
        async def prefetch_voice_context(self, message, *, match_count):
            await asyncio.sleep(3600)  # would hang forever without a bound
            return "should never be reached"

    monkeypatch.setattr("voice.live.delegate.run_turn", _fake_run_turn)
    monkeypatch.setattr(delegate_mod, "_PREFETCH_TIMEOUT_SEC", 0.05)

    result = await run_delegated_turn(
        agent=_Agent(), profile=object(), message="hi",
        identity=object(), prefetch_enabled=True,
    )
    assert result.error is False
    assert "answered anyway" in result.spoken
    assert "prefetched_docs" not in captured


async def test_turn_stats_reach_the_delegate_result(monkeypatch):
    async def _fake_run_turn(*, agent, message, identity, profile, extra_context):
        yield {"type": "answer", "content": "done"}
        yield {"type": "voice_turn_stats",
               "prefetch_hit": True, "rendered_tool": "send_otp"}
        yield {"type": "done"}

    monkeypatch.setattr("voice.live.delegate.run_turn", _fake_run_turn)
    result = await run_delegated_turn(
        agent=object(), profile=object(), message="hi", identity=object(),
    )
    assert result.prefetch_hit is True
    assert result.rendered_tool == "send_otp"
    assert result.rendered_failure is False


async def test_turn_stats_rendered_failure_reaches_the_delegate_result(monkeypatch):
    # an agent's deterministic failure render sets this
    # True when the turn ended on a failed tool's truthful decline, not a
    # successful render.
    async def _fake_run_turn(*, agent, message, identity, profile, extra_context):
        yield {"type": "answer", "content": "couldn't open the ticket"}
        yield {"type": "voice_turn_stats", "prefetch_hit": False,
               "rendered_tool": "create_ticket", "rendered_failure": True}
        yield {"type": "done"}

    monkeypatch.setattr("voice.live.delegate.run_turn", _fake_run_turn)
    result = await run_delegated_turn(
        agent=object(), profile=object(), message="hi", identity=object(),
    )
    assert result.rendered_tool == "create_ticket"
    assert result.rendered_failure is True


async def test_turn_stats_default_to_false_when_absent(monkeypatch):
    """An agent that never yields `voice_turn_stats` (every agent without KB prefetch
    today) must produce clean defaults, not an AttributeError."""
    async def _fake_run_turn(*, agent, message, identity, profile, extra_context):
        yield {"type": "answer", "content": "done"}
        yield {"type": "done"}

    monkeypatch.setattr("voice.live.delegate.run_turn", _fake_run_turn)
    result = await run_delegated_turn(
        agent=object(), profile=object(), message="hi", identity=object(),
    )
    assert result.prefetch_hit is False
    assert result.rendered_tool is None
    assert result.rendered_failure is False


# ---------------------------------------------------------------------------
# Per-stage timing on the delegated turn. `delegate_ms` is one aggregate
# number; the `stage_timings` dict the delegate threads through extra_context
# is where each stage owner (its own prefetch, chat_session's history load,
# the agent loop's memory/LLM/tool stages) accumulates wall time, and
# _stamp_stage_timings turns whatever accumulated into
# `waj.voice.live.stage.*` attributes on the `voice.agent_turn` root span —
# attributes on the EXISTING root span, never new point spans.
# ---------------------------------------------------------------------------

from contextlib import contextmanager  # noqa: E402


class _RecordingSpan:
    """Mirrors the real OTel contract: an attribute set after the span has
    ENDED is dropped (raises here, and refuses to record). This is what pins
    the load-bearing ordering in delegate._consume — the stamp callback is
    registered AFTER enter_context so ExitStack's LIFO unwinding runs it
    BEFORE the span closes. Registered the other way round, the stamp fires
    on a closed span: the raise is swallowed by the stamp's per-key guard,
    the sink stays empty, and the stamped-attribute assertions fail."""

    def __init__(self, sink):
        self._sink = sink
        self.closed = False

    def set_attribute(self, key, value):
        if self.closed:
            raise AssertionError(f"set_attribute({key!r}) after span close")
        self._sink[key] = value


def _span_root_span(sink):
    @contextmanager
    def _fake_root_span(name, attributes=None):
        span = _RecordingSpan(sink)
        try:
            yield span
        finally:
            span.closed = True

    return _fake_root_span


async def test_stage_timings_are_stamped_on_the_root_span(monkeypatch):
    """A fake agent that sleeps in known stages: the prefetch is timed by the
    delegate itself; the other stages accumulate into the SAME dict via
    extra_context (the way chat_session and the product agents fill theirs) and everything
    lands on the root span under the waj.voice.live.stage. prefix."""
    stamped = {}
    monkeypatch.setattr(delegate_mod, "root_span", _span_root_span(stamped))

    class _Agent:
        async def prefetch_voice_context(self, message, *, match_count):
            await asyncio.sleep(0.03)
            return "## Doc\nprefetched chunks"

    async def _fake_run_turn(**kwargs):
        timings = kwargs["extra_context"]["stage_timings"]
        started = time.monotonic()
        await asyncio.sleep(0.02)          # stands in for the history load
        timings["history_ms"] = round((time.monotonic() - started) * 1000.0, 1)
        timings["llm_ms"] = 1542.0
        timings["llm_rounds"] = 2
        timings["tools_ms"] = 77.0
        timings["tool_names"] = "get_transactions,render_chart"
        yield {"type": "answer", "content": "Done."}
        yield {"type": "done"}

    monkeypatch.setattr(delegate_mod, "run_turn", _fake_run_turn)
    result = await run_delegated_turn(
        agent=_Agent(), profile=object(), message="q", identity=object(),
        prefetch_enabled=True,
    )
    assert result.error is False
    # Measured by the delegate around its own prefetch await.
    assert stamped["waj.voice.live.stage.prefetch_ms"] >= 25
    # Filled by the (fake) stage owners, stamped verbatim with the prefix.
    assert stamped["waj.voice.live.stage.history_ms"] >= 15
    assert stamped["waj.voice.live.stage.llm_ms"] == 1542.0
    assert stamped["waj.voice.live.stage.llm_rounds"] == 2
    assert stamped["waj.voice.live.stage.tools_ms"] == 77.0
    assert stamped["waj.voice.live.stage.tool_names"] == "get_transactions,render_chart"


async def test_stage_timings_survive_a_timeout(monkeypatch):
    """A timed-out turn is exactly the one whose breakdown you want — the
    stamp runs on the wait_for cancellation path too, so the stages that
    completed before the budget ran out are kept."""
    stamped = {}
    monkeypatch.setattr(delegate_mod, "root_span", _span_root_span(stamped))

    async def _fake_run_turn(**kwargs):
        timings = kwargs["extra_context"]["stage_timings"]
        timings["llm_ms"] = 9000.0
        timings["llm_rounds"] = 1
        await asyncio.sleep(3600)          # blows straight past timeout_sec
        yield {"type": "done"}  # pragma: no cover

    monkeypatch.setattr(delegate_mod, "run_turn", _fake_run_turn)
    result = await run_delegated_turn(
        agent=object(), profile=object(), message="q", identity=object(),
        timeout_sec=0.05,
    )
    assert result.error is True
    assert stamped["waj.voice.live.stage.llm_ms"] == 9000.0
    assert stamped["waj.voice.live.stage.llm_rounds"] == 1


async def test_a_raising_span_stamp_never_breaks_the_turn(monkeypatch):
    """Timing is telemetry: a span whose set_attribute raises (exporter torn
    down mid-call) must cost the caller nothing."""

    class _RaisingSpan:
        def set_attribute(self, key, value):
            raise RuntimeError("exporter down")

    @contextmanager
    def _fake_root_span(name, attributes=None):
        yield _RaisingSpan()

    monkeypatch.setattr(delegate_mod, "root_span", _fake_root_span)

    async def _fake_run_turn(**kwargs):
        kwargs["extra_context"]["stage_timings"]["llm_ms"] = 10.0
        yield {"type": "answer", "content": "Answer survives."}
        yield {"type": "done"}

    monkeypatch.setattr(delegate_mod, "run_turn", _fake_run_turn)
    result = await run_delegated_turn(
        agent=object(), profile=object(), message="q", identity=object(),
    )
    assert result.error is False
    assert result.display == "Answer survives."


async def test_nothing_is_stamped_when_no_stage_ran(monkeypatch):
    """Zero-change contract: a turn where nothing fills the dict stamps no
    stage attributes at all (no zero-valued noise on every span)."""
    stamped = {}
    monkeypatch.setattr(delegate_mod, "root_span", _span_root_span(stamped))
    monkeypatch.setattr(
        delegate_mod, "run_turn",
        _fake_run_turn([{"type": "answer", "content": "Hello."}, {"type": "done"}]),
    )
    result = await run_delegated_turn(
        agent=object(), profile=object(), message="q", identity=object(),
    )
    assert result.error is False
    assert stamped == {}
