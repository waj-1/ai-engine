# ai_agent/tests/memory/test_summarize.py
import asyncio


class _FakeMsg:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self, content):
        self._content = content
        self.calls = 0

    async def ainvoke(self, _messages):
        self.calls += 1
        return _FakeMsg(self._content)


def _history(n):
    # n turns → 2n messages alternating user/assistant
    out = []
    for i in range(n):
        out.append({"role": "user", "content": f"u{i}"})
        out.append({"role": "assistant", "content": f"a{i}"})
    return out


def test_no_refresh_below_threshold():
    from core.memory.summarize import maybe_update_summary

    cfg = {"trigger_every_n_turns": 6, "keep_recent_verbatim": 4}
    llm = _FakeLLM("SUMMARY")
    result = asyncio.run(maybe_update_summary(
        llm, prior_summary="", prior_count=0, messages=_history(3), cfg=cfg,
    ))
    assert result is None
    assert llm.calls == 0


def test_refresh_when_threshold_crossed():
    from core.memory.summarize import maybe_update_summary

    cfg = {"trigger_every_n_turns": 6, "keep_recent_verbatim": 4}
    llm = _FakeLLM("NEW SUMMARY")
    msgs = _history(8)  # 16 messages
    result = asyncio.run(maybe_update_summary(
        llm, prior_summary="old", prior_count=0, messages=msgs, cfg=cfg,
    ))
    assert result is not None
    summary, count = result
    assert summary == "NEW SUMMARY"
    # keep_recent_verbatim=4 turns → 8 verbatim messages stay out of the summary
    assert count == len(msgs) - 8
    assert llm.calls == 1


def test_needs_refresh_counts_the_whole_thread():
    from core.memory.summarize import needs_refresh

    cfg = {"trigger_every_n_turns": 6, "keep_recent_verbatim": 4}
    assert not needs_refresh(11, 0, cfg)   # under 6 turns: no summary yet
    assert needs_refresh(12, 0, cfg)
    # That refresh covered 4 messages and kept the newest 8 out on purpose, so
    # the transcript stood at 12: the next is due 6 turns later, at 24.
    assert not needs_refresh(23, 4, cfg)
    assert needs_refresh(24, 4, cfg)


def test_refresh_folds_only_uncovered_messages():
    from core.memory.summarize import maybe_update_summary

    class _Spy(_FakeLLM):
        async def ainvoke(self, messages):
            self.prompt = messages[0].content
            return await super().ainvoke(messages)

    cfg = {"trigger_every_n_turns": 6, "keep_recent_verbatim": 4}
    llm = _Spy("NEXT")
    msgs = _history(16)  # 32 messages; the prior summary covers the first 12
    result = asyncio.run(maybe_update_summary(
        llm, prior_summary="covers u0..a5", prior_count=12, messages=msgs, cfg=cfg,
    ))
    assert result == ("NEXT", 32 - 8)
    assert "user: u5\n" not in llm.prompt and "user: u6\n" in llm.prompt


def test_refresh_accepts_only_the_uncovered_part_of_the_transcript():
    from core.memory.summarize import maybe_update_summary

    class _Spy(_FakeLLM):
        async def ainvoke(self, messages):
            self.prompt = messages[0].content
            return await super().ainvoke(messages)

    cfg = {"trigger_every_n_turns": 6, "keep_recent_verbatim": 4}
    llm = _Spy("NEXT")
    whole = _history(16)  # 32 messages; the prior summary covers the first 12
    result = asyncio.run(maybe_update_summary(
        llm, prior_summary="covers u0..a5", prior_count=12,
        messages=whole[12:], offset=12, cfg=cfg,
    ))
    assert result == ("NEXT", 32 - 8)
    assert "user: u5\n" not in llm.prompt and "user: u6\n" in llm.prompt