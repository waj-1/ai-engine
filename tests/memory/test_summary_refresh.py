"""The rolling summary keeps refreshing in long threads, and two saves running
at once never overwrite each other.

Both were live bugs (found testing a customer-chat agent, 2026-10-01):
1. Agents pass `process_turn` only their capped history window, and the summary
   counted that list, so after its first refresh the count never grew again
   and the summary froze for the rest of the thread.
2. Each reply's save runs in the background; two quick messages ran two at
   once, and the one that finished last overwrote the other's summary.

The turns below replay production ORDER: agents start the save while still
streaming, and run_turn persists the assistant row only afterwards
(common/chat_session.py), so a save usually sees this turn's user row but not
its reply yet.
"""
import asyncio

import pytest

from core.agent_profile import Profile

_SESSION = "s1"
_WINDOW = 10  # the history window run_turn hands agents (memory_config.window)


def _profile():
    return Profile(
        profile_id="p1", agent_kind="customer_chat", slug="customer_chat_default",
        environment="staging",
        model_main={"provider": "google_genai", "model_string": "gemini-x"},
        memory_config={"enabled": True},
    )


class _FakeSessionStore:
    """The session's persisted transcript + summary row, in memory. The summary
    write honours `expected_count` the way the real conditional update does."""

    def __init__(self):
        self.messages = []
        self.summary = ""
        self.count = 0
        self.writes = []

    async def get_session_summary(self, _client, *, session_id):
        return {"running_summary": self.summary, "summary_message_count": self.count}

    async def count_messages(self, _client, *, session_id):
        return len(self.messages)

    async def list_session_history(self, _client, *, session_id, start=0, end=None):
        return list(self.messages[start:end])

    async def set_session_summary(self, _client, *, session_id, summary, message_count,
                                  expected_count=None):
        if expected_count is not None and expected_count != self.count:
            return False
        self.summary, self.count = summary, message_count
        self.writes.append(summary)
        return True


class _SummaryLLM:
    """Returns a summary naming the newest message it was asked to fold in."""

    def __init__(self, gate=None):
        self.calls = 0
        self.gate = gate

    async def ainvoke(self, messages):
        self.calls += 1
        if self.gate:
            await self.gate()
        prompt = messages[0].content
        last = [line for line in prompt.splitlines() if line.startswith(("user:", "assistant:"))][-1]

        class R:
            content = f"summary up to {last}"
        return R()


@pytest.fixture
def wired(monkeypatch):
    from core.memory import service

    fake = _FakeSessionStore()
    for name in ("get_session_summary", "count_messages", "list_session_history",
                 "set_session_summary"):
        monkeypatch.setattr(service.store, name, getattr(fake, name))

    async def no_facts(*_a, **_k):
        return []
    monkeypatch.setattr(service, "extract_facts", no_facts)
    return service, fake


async def _turn(service, fake, llm, n, monkeypatch, persisted_before_save=("user",)):
    """One turn. `persisted_before_save` is which of this turn's rows run_turn
    has written when the save runs; the rest land after it, as in production."""
    monkeypatch.setattr(service, "build_chat_model", lambda _m: llm)
    rows = {"user": {"role": "user", "content": f"u{n}"},
            "assistant": {"role": "assistant", "content": f"a{n}"}}
    window = fake.messages[-_WINDOW:]  # what the agent hands process_turn
    early = [rows[r] for r in ("user", "assistant") if r in persisted_before_save]
    late = [rows[r] for r in ("user", "assistant") if r not in persisted_before_save]
    fake.messages += early
    await service.process_turn(
        profile=_profile(), user_id="u1", business_id="b1", agent_kind="customer_chat",
        session_id=_SESSION, user_message=f"u{n}", assistant_response=f"a{n}",
        previous_messages=window, client=object(),
    )
    fake.messages += late


async def _refresh_turns(service, fake, monkeypatch, turns, **kwargs):
    """Run `turns` turns; return the turn numbers whose save refreshed the summary."""
    refreshed = []
    for n in range(1, turns + 1):
        before = len(fake.writes)
        await _turn(service, fake, _SummaryLLM(), n, monkeypatch, **kwargs)
        if len(fake.writes) > before:
            refreshed.append(n)
    return refreshed


async def test_summary_keeps_refreshing_in_a_long_thread(wired, monkeypatch):
    service, fake = wired
    refreshed = await _refresh_turns(service, fake, monkeypatch, 20)
    assert len(refreshed) > 1, "summary froze after its first refresh"
    # The latest summary reaches past the history window, not just turn 6.
    assert fake.count > _WINDOW


async def test_refreshes_every_n_turns_not_every_two(wired, monkeypatch):
    # trigger_every_n_turns defaults to 6. The 8 newest messages a refresh
    # leaves out of the summary are not "new" at the next check.
    service, fake = wired
    assert await _refresh_turns(service, fake, monkeypatch, 20) == [6, 12, 18]


@pytest.mark.parametrize("persisted", [(), ("user",), ("user", "assistant")])
async def test_this_turn_counts_whether_or_not_it_is_saved_yet(wired, monkeypatch, persisted):
    # The 6th turn is due a refresh whatever run_turn has persisted so far;
    # counting only saved rows used to push it to the next message, which for
    # a customer coming back is exactly the turn that needed it.
    service, fake = wired
    refreshed = await _refresh_turns(service, fake, monkeypatch, 6,
                                     persisted_before_save=persisted)
    assert refreshed == [6]


async def test_each_refresh_folds_only_what_is_not_summarised_yet(wired, monkeypatch):
    service, fake = wired
    prompts = []

    class _Spy(_SummaryLLM):
        async def ainvoke(self, messages):
            prompts.append(messages[0].content)
            return await super().ainvoke(messages)

    llm = _Spy()
    for n in range(1, 21):
        await _turn(service, fake, llm, n, monkeypatch)
    assert len(prompts) > 1
    # The second refresh gets the first summary, not the first turns again.
    assert "user: u1\n" not in prompts[1]


async def test_two_saves_at_once_do_not_overwrite_each_other(wired, monkeypatch):
    service, fake = wired
    for n in range(1, 6):  # five quiet turns: no summary yet
        await _turn(service, fake, _SummaryLLM(), n, monkeypatch)
    assert fake.writes == []

    # Turns 6 and 7 arrive back to back. Both saves read the same prior summary
    # before either writes; turn 6's model call is the slow one, so it finishes LAST.
    release_slow = asyncio.Event()

    async def slow():
        await release_slow.wait()

    async def fast():
        return None

    turn6 = asyncio.create_task(_turn(service, fake, _SummaryLLM(gate=slow), 6, monkeypatch))
    await asyncio.sleep(0.01)
    await _turn(service, fake, _SummaryLLM(gate=fast), 7, monkeypatch)
    first = fake.summary
    release_slow.set()
    await turn6

    assert fake.writes == [first], "the slower save overwrote the summary written first"