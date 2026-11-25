"""The public demo's traces: each model call counted once, real prices,
sample traffic around the clock, and insights within a budget.

What these pin:

1. `core.llm_calls`: a model call exported as two LLM spans (the LangChain
   wrapper's and the provider SDK's) is counted once, a span with no twin is
   always counted, and the three trace queries read those counts.
2. The demo's model rows carry Azure's list prices, and only where the
   deployment is known.
3. Northwind's hand-over tool and its one real failure; a tool server's error
   becomes a failed action in telemetry only where that is switched on.
4. The sample channel, and no stored example chats where tracing is on.
5. The sample-traffic generator: the script pool, the schedule, the budget
   (visitors first), one process per slot, the chat it posts.
6. Conversation insights in demo mode: sample conversations only, capped.
7. Generic agents are told apart by slug in the technical rollups and in the
   insight rows.
"""
from __future__ import annotations

import asyncio
import random
import re
from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from core import conversation_insights as ci
from core import llm_calls
from demo_mode import content, insights, limits, northwind, prices, reset, sample_traffic, seed
from tests.admin.test_demo_mode import _Client, db, demo  # noqa: F401 — fixtures


# --------------------------------------------------------------------------
# 1. Each model call once
# --------------------------------------------------------------------------

def _span(start, end, tin, tout, *, wrapper, cost=0.0, cache=0):
    return {"start_time": "2026-11-24 16:52:%s" % start, "end_time": "2026-11-24 16:52:%s" % end,
            "input_tokens": tin, "output_tokens": tout, "total_cost": cost,
            "cache_read_tokens": cache, "wrapper": wrapper}


def test_a_call_exported_twice_is_counted_once():
    # As stored for one streamed Azure OpenAI call (2026-11-24): the wrapper
    # has no model and no cost, the SDK span has both; 2 ms apart.
    pair = [_span("18.924490627", "19.970000000", 875, 45, wrapper=1),
            _span("18.926304110", "19.969000000", 875, 45, wrapper=0, cost=0.000859, cache=128)]
    once = llm_calls.count_once(pair)
    assert (once["llm_calls"], once["input_tokens"], once["output_tokens"]) == (1, 875, 45)
    assert once["total_cost"] == pytest.approx(0.000859) and once["cache_read_tokens"] == 128
    assert once["dropped"] == [0]   # the wrapper, never the span that names the model


def test_a_single_span_and_separate_calls_are_all_counted():
    single = [_span("18.9", "19.9", 300, 20, wrapper=0, cost=0.0003)]
    assert llm_calls.count_once(single)["input_tokens"] == 300
    # A provider whose SDK is not instrumented: only the wrapper exists.
    assert llm_calls.count_once([_span("18.9", "19.9", 300, 20, wrapper=1)])["llm_calls"] == 1
    # Two rounds of one turn, each exported twice, plus an embedding call.
    turn = [_span("18.924", "19.970", 875, 45, wrapper=1), _span("18.926", "19.969", 875, 45, wrapper=0),
            _span("19.976", "21.100", 924, 67, wrapper=1), _span("19.977", "21.099", 924, 67, wrapper=0),
            _span("19.980", "19.990", 16, 0, wrapper=0)]
    once = llm_calls.count_once(turn)
    assert (once["llm_calls"], once["input_tokens"], once["output_tokens"]) == (3, 875 + 924 + 16, 112)
    # The same token counts at ANOTHER time are another call, not a twin.
    later = [_span("18.9", "19.9", 500, 50, wrapper=1), _span("30.0", "31.0", 500, 50, wrapper=0)]
    assert llm_calls.count_once(later)["input_tokens"] == 1000


def test_cost_is_not_doubled_when_both_spans_are_priced():
    # A plain (not streamed) call: the wrapper carries the model and a cost too.
    pair = [_span("01.0", "03.0", 888, 141, wrapper=1, cost=0.00867),
            _span("01.1", "02.9", 888, 141, wrapper=0, cost=0.00867)]
    assert llm_calls.count_once(pair)["total_cost"] == pytest.approx(0.00867)


def test_wrapper_is_recognised_from_stored_attributes():
    text = '{"traceloop.association.properties.ls_model_type":"chat","gen_ai.request.model":"unknown"}'
    assert llm_calls.is_wrapper({"attributes": text}) is True
    assert llm_calls.is_wrapper({"attributes": {"gen_ai.response.model": "gpt-5.4-mini"}}) is False


def test_trace_queries_read_the_once_counts():
    from admin.screens import live_analytics, observability

    listing, _params = observability._trace_list_sql(env="staging", include_background=False, status=None)
    for sql in (listing, observability._TRACE_ONE_SQL, live_analytics._TRACE_SQL):
        assert "__ONCE__" not in sql and "AS llm_calls," in sql
        assert llm_calls.WRAPPER_MARKER in sql
        # Laminar's own per-trace totals (which add both spans) are not read.
        assert not re.search(r"\bt\.(input_tokens|output_tokens|total_cost)\b", sql)
        assert not re.search(r"toInt64\((input|output)_tokens\) AS (input|output)Tokens", sql)
    assert "err.input_tokens_once" in listing and "sagg.cost_once" in live_analytics._TRACE_SQL
    assert "sagg.llm_calls_once" in live_analytics._TRACE_SQL


def test_conversation_cache_tokens_are_counted_once():
    from admin.screens import observability

    rows = [dict(_span("18.924", "19.970", 875, 45, wrapper=1, cache=128), span_type=1),
            dict(_span("18.926", "19.969", 875, 45, wrapper=0, cache=128), span_type=1),
            dict(_span("19.0", "19.5", 0, 0, wrapper=0), span_type=0)]
    assert observability._cache_tokens_once(rows) == {"read": 128, "creation": 0}


# --------------------------------------------------------------------------
# 2. Prices
# --------------------------------------------------------------------------

def test_demo_model_rows_carry_list_prices(demo, monkeypatch):
    agent = seed._model_row("agent", "gpt-5.4-mini", "staging")["extra_config"]
    builder = seed._model_row("builder", "gpt-5.5", "staging")["extra_config"]
    assert (agent["cost_per_1k_in"], agent["cost_per_1k_out"]) == (0.00075, 0.0045)
    assert (builder["cost_per_1k_in"], builder["cost_per_1k_out"]) == (0.005, 0.03)
    from core import pricing

    # 1M tokens in and 1M out at the list price.
    assert pricing.cost_usd({"model_string": "gpt-5.4-mini", "extra_config": agent},
                            1_000_000, 1_000_000) == pytest.approx(5.25)
    # A deployment under another name, or another provider: no price, not a guess.
    assert "cost_per_1k_in" not in seed._model_row("agent", "my-mini", "staging")["extra_config"]
    assert prices.manual_rates("openai", "gpt-5.4-mini") == {}
    monkeypatch.setenv("DEMO_MODEL_PROVIDER", "openai")
    assert "cost_per_1k_in" not in seed._model_row("agent", "gpt-5.4-mini", "staging")["extra_config"]


# --------------------------------------------------------------------------
# 3. Hand-over, failures
# --------------------------------------------------------------------------

def _call(name, **arguments):
    out = northwind.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": name, "arguments": arguments}})["result"]
    return out["isError"], out["content"][0]["text"]


def test_hand_over_tool_is_what_the_overview_counts():
    from admin.screens import plain_language as pl

    failed, text = _call("escalate_to_human", reason="wants a person")
    assert failed is False and "person" in text
    assert pl._HANDOFF.search("escalate_to_human")
    # The other tools, and the Support desk's hand-off to the billing AGENT, are not a person.
    for name in ("check_outage", "get_order_status", "create_ticket", "list_plans", "ask_billing_helper"):
        assert not pl._HANDOFF.search(name)
    assert "escalate_to_human" in content.SUPPORT_INSTRUCTIONS


def test_a_malformed_order_number_fails_and_an_unknown_one_does_not():
    failed, text = _call("get_order_status", order_id="NW-10423")
    assert failed is True and "NW- and four digits" in text
    assert _call("get_order_status", order_id="1042-B")[0] is True
    failed, text = _call("get_order_status", order_id="NW-2001")
    assert failed is False and "No order NW-2001 found" in text
    assert _call("get_order_status", order_id="nw-1042") == (False, "Router shipped yesterday, arrives tomorrow.")


class _Span:
    def __init__(self, name):
        self.name, self.events = name, []

    def is_recording(self):
        return True

    def add_event(self, name, attributes=None):
        self.events.append((name, attributes))


def test_tool_server_error_is_a_failed_action_only_when_switched_on(monkeypatch):
    from opentelemetry import trace

    from runtime.tools import _external_adapter

    tool, root = _Span("get_order_status.tool"), _Span("custom.agent")
    monkeypatch.setattr(trace, "get_current_span", lambda: tool)
    monkeypatch.delenv("TOOL_ERROR_EVENTS", raising=False)
    _external_adapter._mark_tool_failed("Invalid order number")
    assert tool.events == []   # as before, outside the demo
    monkeypatch.setenv("TOOL_ERROR_EVENTS", "1")
    _external_adapter._mark_tool_failed("Invalid order number")
    assert tool.events == [("error", {"message": "Invalid order number"})]
    # Never on the request's own span: that would read as the agent failing.
    monkeypatch.setattr(trace, "get_current_span", lambda: root)
    _external_adapter._mark_tool_failed("Invalid order number")
    assert root.events == []


# --------------------------------------------------------------------------
# 4. The sample channel; stored chats only without tracing
# --------------------------------------------------------------------------

def test_seed_makes_the_sample_channel(db):
    from core import channel_resolver

    seed.seed(_Client(db))
    by_slug = {c["slug"]: c for c in db["agent_channels"]}
    assert set(by_slug) == {"northwind-website", "sample-traffic"}
    sample = by_slug["sample-traffic"]
    assert sample["name"] == "Sample traffic" and sample["allowed_origins"] == [content.SAMPLE_ORIGIN]
    # Every agent a person could talk to, the billing specialist included; never the builder.
    assert sample["allowed_agent_kinds"] == ["custom:support-desk", "custom:billing-helper",
                                             "custom:sales-assistant", "custom:it-helpdesk"]
    # A sample chat's origin resolves to it, a visitor's page to the website's, none to neither.
    match = lambda origin: (channel_resolver._match_channel(origin, by_slug) or {}).get("slug")  # noqa: E731
    assert match(content.SAMPLE_ORIGIN) == "sample-traffic"
    assert match("https://demo.example.com") == "northwind-website"
    assert match(None) is None
    assert sample_traffic.chat_headers("tok")["Origin"] == content.SAMPLE_ORIGIN
    before = len(db["agent_channels"])
    seed.seed(_Client(db))
    assert len(db["agent_channels"]) == before


def test_stored_example_chats_are_seeded_only_without_tracing(db, monkeypatch):
    monkeypatch.delenv("LMNR_DISABLED", raising=False)
    monkeypatch.setenv("LMNR_CLICKHOUSE_URL", "http://127.0.0.1:8123")
    out = seed.seed(_Client(db))
    # Tracing on: Conversations and Overview read traces only, so nothing is
    # seeded that no report would count.
    assert out["conversations_added"] == 0 and not db.get("ai_chat_sessions")
    monkeypatch.setenv("LMNR_DISABLED", "true")
    monkeypatch.delenv("LMNR_CLICKHOUSE_URL", raising=False)
    assert seed.seed(_Client(db))["conversations_added"] == len(content.CONVERSATIONS)


# --------------------------------------------------------------------------
# 5. The generator
# --------------------------------------------------------------------------

def test_script_pool_is_consistent_with_the_demo():
    scripts = sample_traffic.SCRIPTS
    ids = [s["id"] for s in scripts]
    assert len(ids) == len(set(ids)) >= 30
    agents = {a["slug"] for a in content.AGENTS if not a.get("code_owned")}
    assert {s["agent"] for s in scripts} == agents
    assert all(1 <= len(s["messages"]) <= 4 and all(m.strip() for m in s["messages"]) for s in scripts)
    by_label: Dict[str, List[Dict[str, Any]]] = {}
    for s in scripts:
        by_label.setdefault(s["label"], []).append(s)
    for label in (sample_traffic.TICKET, sample_traffic.BILLING_HANDOFF, sample_traffic.TOOL_FAILURE,
                  sample_traffic.NOT_FOUND, sample_traffic.PERSON, sample_traffic.ARABIC):
        assert by_label.get(label), label
    # Tickets, billing hand-overs, failures and hand-overs to a person need the
    # Support desk: it alone has those tools and the billing specialist.
    for label in (sample_traffic.TICKET, sample_traffic.BILLING_HANDOFF, sample_traffic.TOOL_FAILURE,
                  sample_traffic.PERSON):
        assert {s["agent"] for s in by_label[label]} == {content.SUPPORT_SLUG}
    text = " ".join(m for s in scripts for m in s["messages"])
    # Order numbers: the two that exist, one unknown, and the malformed ones.
    for order in re.findall(r"NW-\d+", text):
        failed, answer = _call("get_order_status", order_id=order)
        assert failed is (order == "NW-10423")
        assert ("found" in answer) is (order == "NW-2001")
    for s in by_label[sample_traffic.TOOL_FAILURE]:
        bad = re.search(r"NW-\d{5}|\d{4}-[A-Z]", s["messages"][0]).group(0)
        assert _call("get_order_status", order_id=bad)[0] is True
    arabic = [s for s in by_label[sample_traffic.ARABIC]]
    assert all(ci.detect_language(s["messages"]) == "ar" for s in arabic)
    assert "—" not in text
    assert set(sample_traffic.BURST_FIRST) <= set(ids)


def test_pick_avoids_recent_scripts_and_repeats():
    rng = random.Random(7)
    recent: deque = deque(maxlen=12)
    seen: List[str] = []
    for _ in range(12):
        got = [s["id"] for s in sample_traffic.pick(2, rng, recent)]
        assert len(set(got)) == 2 and not set(got) & set(seen[-10:])
        seen += got
    assert len(set(seen)) > 12   # it walks the pool, not a favourite few
    opening = sample_traffic.pick(8, random.Random(1), deque(maxlen=12),
                                  first=sample_traffic.BURST_FIRST)
    assert [s["id"] for s in opening] == list(sample_traffic.BURST_FIRST)
    labels = {s["label"] for s in opening}
    assert {sample_traffic.TICKET, sample_traffic.BILLING_HANDOFF, sample_traffic.TOOL_FAILURE,
            sample_traffic.PERSON} <= labels
    assert {s["agent"] for s in opening} == {content.SUPPORT_SLUG, content.SALES_SLUG,
                                             content.BILLING_SLUG, content.IT_SLUG}


def test_schedule_is_denser_in_support_hours(demo, monkeypatch):
    noon = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    night = datetime(2026, 11, 24, 2, 0, tzinfo=timezone.utc)
    assert sample_traffic.support_hours(noon) and not sample_traffic.support_hours(night)
    rng = random.Random(3)
    day_gaps = [sample_traffic.next_gap(noon, rng) for _ in range(200)]
    night_gaps = [sample_traffic.next_gap(night, rng) for _ in range(200)]
    assert 1200 <= min(day_gaps) and max(day_gaps) <= 2400
    assert 2400 <= min(night_gaps) and max(night_gaps) <= 4800
    counts = [sample_traffic.conversations_for_tick(noon, rng) for _ in range(400)]
    assert set(counts) == {1, 2} and 100 < counts.count(2) < 220
    assert {sample_traffic.conversations_for_tick(night, rng) for _ in range(100)} == {1}
    # Northwind's clock can be moved: 02:00 UTC is 10:00 at UTC+8.
    monkeypatch.setenv("DEMO_SAMPLE_UTC_OFFSET", "8")
    assert sample_traffic.support_hours(night)
    # A whole day at the default pace stays well inside the default budget.
    per_day = (12 * 3600 / 1800) * 1.4 + (12 * 3600 / 3600)
    mean_turns = sum(len(s["messages"]) for s in sample_traffic.SCRIPTS) / len(sample_traffic.SCRIPTS)
    assert per_day * mean_turns + sample_traffic.burst_size() * 2 < sample_traffic.daily_turns()


class _SharedCounter:
    """The database's counter, shared by every "process": demo_usage_take's rule."""

    def __init__(self):
        self.used: Dict[str, int] = {}

    def take(self, bucket, limit, amount=1):
        if self.used.get(bucket, 0) + amount > limit:
            return False
        self.used[bucket] = self.used.get(bucket, 0) + amount
        return True

    def read(self, bucket):
        return self.used.get(bucket, 0)


@pytest.fixture
def counter(demo, monkeypatch):
    shared = _SharedCounter()
    monkeypatch.setattr(limits, "_db_take", shared.take)
    monkeypatch.setattr(limits, "_db_used", shared.read)
    return shared


def test_one_process_per_slot(counter):
    at = lambda h, m, s=0: datetime(2026, 11, 24, h, m, s, tzinfo=timezone.utc)  # noqa: E731
    assert sample_traffic.slot_of(at(0, 0)) == 1 and sample_traffic.slot_of(at(0, 20)) == 2
    # Two processes tick in the same 20-minute slot: one holds the conversation.
    assert sample_traffic.claim_slot(at(10, 1)) is True
    assert sample_traffic.claim_slot(at(10, 15)) is False
    assert sample_traffic.claim_slot(at(10, 21)) is True
    # A process whose clock is behind cannot take a slot that is already past.
    assert sample_traffic.claim_slot(at(9, 0)) is False
    # Quiet hours skip slots; nothing is owed for them later.
    assert sample_traffic.claim_slot(at(13, 0)) is True
    assert sample_traffic.claim_slot(at(13, 5)) is False
    assert counter.used[sample_traffic.SLOT_BUCKET] == sample_traffic.slot_of(at(13, 0))
    # A race: both read the same count, the database grants one.
    stale = counter.read(sample_traffic.SLOT_BUCKET)
    slot = sample_traffic.slot_of(at(14, 0))
    assert [limits.take(sample_traffic.SLOT_BUCKET, slot, slot - stale) for _ in range(2)] == [True, False]


def test_start_burst_is_claimed_once_a_day(counter):
    assert sample_traffic.claim_boot_burst() is True
    assert sample_traffic.claim_boot_burst() is False   # a restart, or a second process


def test_budget_stops_before_visitors_are_starved(counter, monkeypatch):
    monkeypatch.setenv("DEMO_DAILY_TURNS", "100")
    monkeypatch.setenv("DEMO_SAMPLE_DAILY_TURNS", "5")
    assert [sample_traffic.may_spend() for _ in range(6)] == [True] * 5 + [False]
    # Its own budget is left, but the day's cap is 60% used (by visitors): it stops first.
    monkeypatch.setenv("DEMO_SAMPLE_DAILY_TURNS", "50")
    counter.used[limits.TURNS_BUCKET] = 59
    assert sample_traffic.may_spend() is True
    counter.used[limits.TURNS_BUCKET] = 60
    assert sample_traffic.may_spend() is False
    assert limits.take_turn() is True   # visitors still have the other 40
    monkeypatch.setenv("DEMO_SAMPLE_STOP_AT_PERCENT", "80")
    assert sample_traffic.may_spend() is True


def test_switch(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    assert sample_traffic.enabled() is False
    asyncio.run(sample_traffic.run_loop())   # returns at once
    monkeypatch.setenv("DEMO_MODE", "true")
    assert sample_traffic.enabled() is True
    monkeypatch.setenv("DEMO_SAMPLE_TRAFFIC", "off")
    assert sample_traffic.enabled() is False
    asyncio.run(sample_traffic.run_loop())


@pytest.fixture
def posts(counter, monkeypatch):
    """Every chat the generator posts, answered without a model."""
    from admin_mcp import tools_dispatch
    from demo_mode import account

    sent: List[Dict[str, Any]] = []
    state = {"answer": "ok", "status": 200}

    async def _post(endpoint, body, headers, env, timeout):
        sent.append({"endpoint": endpoint, "headers": dict(headers), "env": env, **body})
        text = 'data: {"type": "answer", "content": %s}\n\n' % __import__("json").dumps(state["answer"])
        return state["status"], "text/event-stream", text, "in-process"

    marks: List[tuple] = []
    monkeypatch.setattr(tools_dispatch, "_post_chat", _post)
    monkeypatch.setattr(account, "sign_in", lambda: "tok")
    monkeypatch.setattr(seed, "_env", lambda: "staging")
    monkeypatch.setattr(sample_traffic, "mark_session", lambda sid, script: marks.append((sid, script)))
    return SimpleNamespace(sent=sent, state=state, marks=marks)


def _script(script_id):
    return next(s for s in sample_traffic.SCRIPTS if s["id"] == script_id)


def test_a_conversation_is_held_like_a_website_would(posts, counter):
    script = _script("support-ticket-red-light")
    turns = asyncio.run(sample_traffic.hold([script], rng=random.Random(1), pause=0))
    assert turns == 2 and [p["message"] for p in posts.sent] == list(script["messages"])
    assert {p["endpoint"] for p in posts.sent} == {"/v1/agents/support-desk/chat"}
    assert len({p["session_id"] for p in posts.sent}) == 1   # one conversation
    head = posts.sent[0]["headers"]
    assert head["Authorization"] == "Bearer tok" and head["Origin"] == content.SAMPLE_ORIGIN
    # Marked once, as a sample conversation, with the script it came from.
    assert posts.marks == [(posts.sent[0]["session_id"], "support-ticket-red-light")]
    assert counter.used[sample_traffic.TURNS_BUCKET] == 2


def test_no_turn_is_posted_without_budget(posts, counter, monkeypatch):
    monkeypatch.setenv("DEMO_SAMPLE_DAILY_TURNS", "3")
    scripts = [_script("support-ticket-red-light"), _script("sales-family-of-four")]
    assert asyncio.run(sample_traffic.hold(scripts, rng=random.Random(1), pause=0)) == 3
    assert len(posts.sent) == 3
    # The budget is used: the next round posts nothing at all.
    assert asyncio.run(sample_traffic.hold(scripts, rng=random.Random(1), pause=0)) == 0
    assert len(posts.sent) == 3


def test_it_stops_when_the_demo_cap_answers(posts):
    posts.state["answer"] = limits.CAP_MESSAGE   # the route's answer once the day's turns are used
    scripts = [_script("support-ticket-red-light"), _script("it-vpn")]
    assert asyncio.run(sample_traffic.hold(scripts, rng=random.Random(1), pause=0)) == 0
    assert len(posts.sent) == 1 and posts.marks == []


def test_a_failing_agent_does_not_stop_later_ticks(posts):
    posts.state["status"] = 503
    assert asyncio.run(sample_traffic.hold([_script("it-vpn")], rng=random.Random(1), pause=0)) == 0
    posts.state["status"] = 200
    assert asyncio.run(sample_traffic.hold([_script("it-vpn")], rng=random.Random(1), pause=0)) == 1


def test_loop_bursts_at_start_then_ticks_and_never_blocks_the_loop(posts, counter, monkeypatch):
    monkeypatch.setenv("DEMO_SAMPLE_MIN_GAP_SEC", "5")
    monkeypatch.setenv("DEMO_SAMPLE_MAX_GAP_SEC", "5")
    monkeypatch.setenv("DEMO_SAMPLE_TURN_DELAY_SEC", "0")
    monkeypatch.setenv("DEMO_SAMPLE_BURST", "3")
    import threading

    main_thread = threading.get_ident()
    threads: Dict[str, set] = {"claims": set(), "budget": set()}
    real_claim, real_spend = sample_traffic.claim_slot, sample_traffic.may_spend

    def _claim(now=None):
        threads["claims"].add(threading.get_ident())
        return real_claim(now)

    def _spend():
        threads["budget"].add(threading.get_ident())
        return real_spend()

    monkeypatch.setattr(sample_traffic, "claim_slot", _claim)
    monkeypatch.setattr(sample_traffic, "may_spend", _spend)
    real_sleep = asyncio.sleep
    slept: List[float] = []

    async def _sleep(seconds):
        slept.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(sample_traffic.asyncio, "sleep", _sleep)

    async def _run():
        task = asyncio.ensure_future(sample_traffic.run_loop(random.Random(5)))
        for _ in range(4000):
            await real_sleep(0)
            if threads["claims"] and len({p["session_id"] for p in posts.sent}) >= 4:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())
    sessions = []
    for p in posts.sent:
        if p["session_id"] not in sessions:
            sessions.append(p["session_id"])
    by_script = [script for _sid, script in posts.marks]
    # The burst first, opening with the labelled cases; then the trickle.
    assert by_script[:3] == list(sample_traffic.BURST_FIRST[:3]) and len(sessions) >= 4
    assert counter.used[sample_traffic.BURST_BUCKET] == 1
    # Every database call of its own ran off the event loop's thread.
    assert threads["claims"] and main_thread not in threads["claims"] | threads["budget"]


def test_reset_asks_for_a_burst(demo, monkeypatch):
    monkeypatch.setattr(reset, "reset", lambda: None)
    monkeypatch.setattr(reset, "_start_warmup", lambda: None)
    sample_traffic._burst_requested.clear()
    assert reset.run_once_today() is True
    assert sample_traffic._burst_requested.is_set()
    sample_traffic._burst_requested.clear()
    assert reset.run_once_today() is False and not sample_traffic._burst_requested.is_set()


def test_mark_session_keeps_the_rest_of_the_metadata(db, monkeypatch):
    db["ai_chat_sessions"] = [{"id": "s1", "metadata": {"agent_slug": "support-desk", "source": "api"}}]
    monkeypatch.setattr(seed, "_client", lambda: _Client(db))
    sample_traffic.mark_session("s1", "support-outage-riverside")
    assert db["ai_chat_sessions"][0]["metadata"] == {
        "agent_slug": "support-desk", "source": "api", "sample": "support-outage-riverside"}
    sample_traffic.mark_session("missing", "x")   # never raises


# --------------------------------------------------------------------------
# 6. Insights within a budget
# --------------------------------------------------------------------------

def test_insights_cover_sample_conversations_only(demo):
    assert insights.covers({"metadata": {"agent_slug": "support-desk", "sample": "support-vpn"}})
    assert not insights.covers({"metadata": {"agent_slug": "support-desk"}})
    assert not insights.covers({"metadata": None})
    row = insights.model_row()
    assert row["model_string"] == "gpt-5.4-mini" and row["max_output_tokens"] == 400
    assert ci.classifier_available() is True and ci.settings()["model"] == "gpt-5.4-mini"
    assert ci.settings()["idle_minutes"] == 30


def test_insight_calls_are_capped_and_count_as_demo_turns(counter, monkeypatch):
    monkeypatch.setenv("DEMO_INSIGHTS_DAILY_CALLS", "2")
    assert [insights.take_call() for _ in range(3)] == [True, True, False]
    assert counter.used[limits.TURNS_BUCKET] == 2
    # The demo's own cap is used up: no call either.
    monkeypatch.setenv("DEMO_INSIGHTS_DAILY_CALLS", "50")
    monkeypatch.setenv("DEMO_DAILY_TURNS", "2")
    assert insights.take_call() is False
    monkeypatch.setenv("DEMO_INSIGHTS_DAILY_CALLS", "0")
    assert insights.enabled() is False and insights.model_row() is None
    assert ci.classifier_available() is False


def test_classifier_is_not_called_past_the_cap(counter, monkeypatch):
    monkeypatch.setenv("DEMO_INSIGHTS_DAILY_CALLS", "1")
    calls: List[Any] = []

    class _LLM:
        def invoke(self, messages):
            calls.append(messages)
            return SimpleNamespace(content='{"topic": "technical_issue", "outcome": "resolved", "language": "en"}')

    monkeypatch.setattr(ci, "_llm", lambda model_string: _LLM())
    first = ci.classify("custom", "Customer: my internet is down", model_string="gpt-5.4-mini")
    assert first == {"topic": "technical_issue", "outcome": "resolved", "language": "en"}
    assert ci.classify("custom", "Customer: hello", model_string="gpt-5.4-mini") is None
    assert len(calls) == 1


def test_demo_sweep_sends_only_sample_conversations_to_the_model(counter, monkeypatch):
    from tests.admin.test_conversation_insights import _Client as SweepClient

    sample, visitor = "33333333-3333-3333-3333-333333333333", "44444444-4444-4444-4444-444444444444"
    store = {
        "candidates": [
            {"session_id": sample, "agent_kind": "custom", "created_at": "2026-11-24T08:00:00+00:00",
             "updated_at": "2026-11-24T08:10:00+00:00",
             "metadata": {"agent_slug": "support-desk", "sample": "support-outage-riverside"}},
            {"session_id": visitor, "agent_kind": "custom", "created_at": "2026-11-24T08:00:00+00:00",
             "updated_at": "2026-11-24T08:10:00+00:00", "metadata": {"agent_slug": "support-desk"}},
        ],
        "messages": [
            {"session_id": sample, "role": "user", "content": "Is there an outage?", "created_at": "1"},
            {"session_id": visitor, "role": "user", "content": "my secret", "created_at": "1"},
        ],
    }
    monkeypatch.setenv("CONVERSATION_INSIGHTS_CALL_GAP_SEC", "0")
    monkeypatch.setattr(ci, "telemetry_signals", lambda sids, since: {})
    read: List[str] = []

    def fake_classify(kind, transcript, *, model_string):
        read.append(transcript)
        return {"topic": "technical_issue", "outcome": "resolved", "language": "en"}

    monkeypatch.setattr(ci, "classify", fake_classify)
    out = ci.sweep_once(SweepClient(store), use_lease=False)
    assert out["analysed"] == 2 and out["classified"] == 1 and out["classifier"] == "gpt-5.4-mini"
    assert read == ["Customer: Is there an outage?"]   # the visitor's words never reach the model
    rows = {r["session_id"]: r for r in store["upserted"]}
    assert rows[sample]["is_simulated"] is False and rows[sample]["topic"] == "technical_issue"
    # Filed so it is not picked again, and left out of every rollup.
    assert rows[visitor]["is_simulated"] is True and rows[visitor]["classifier_model"] is None
    # The candidates are asked for with the demo's quiet window.
    assert store["rpc"][0][1]["p_idle_minutes"] == 30


# --------------------------------------------------------------------------
# 7. Generic agents, one by one
# --------------------------------------------------------------------------

def test_insight_rows_name_the_generic_agent():
    assert ci.stored_agent_kind("custom", {"agent_slug": "support-desk"}) == "custom:support-desk"
    assert ci.stored_agent_kind("custom", {}) == "custom"
    assert ci.stored_agent_kind("sales", {"agent_slug": "whatever"}) == "sales"   # a kind is one agent
    row = ci.analyze({"session_id": "s", "agent_kind": "custom",
                      "metadata": {"agent_slug": "it-helpdesk"}},
                     [{"role": "user", "content": "hi"}])
    assert row["agent_kind"] == "custom:it-helpdesk"


def test_technical_rollups_split_generic_agents_by_slug():
    from admin.screens import live_analytics as la

    def row(slug, tokens, minute):
        return la._map_trace_row({"created_at": "2026-11-24 10:%02d:00" % minute, "agent_kind": "custom",
                                  "agent_slug": slug, "input_tokens": tokens, "output_tokens": 10,
                                  "cost_usd": 0.001, "latency_ms": 900, "iteration_count": 1})

    rows = [row("support-desk", 100, 1), row("support-desk", 200, 2), row("it-helpdesk", 50, 40)]
    rows.append(la._map_trace_row({"created_at": "2026-11-24 10:03:00", "agent_kind": "sales",
                                   "agent_slug": "", "input_tokens": 7, "output_tokens": 1}))
    per_agent = {r["agent_kind"]: r for r in la.per_agent_rollup(rows, {})}
    assert set(per_agent) == {"custom:support-desk", "custom:it-helpdesk", "sales"}
    assert per_agent["custom:support-desk"]["volume"] == 2
    assert per_agent["custom:support-desk"]["tokens_in"] == 300
    assert {r["agent_kind"] for r in la.cost_share(rows, {})} == set(per_agent)
    volumes = la.bucket_series(rows, la.choose_bucket(1), {})["volume_by_agent"]
    assert set(volumes) >= {"custom:support-desk", "custom:it-helpdesk"}
    # Every series has a value for every bucket, zero before the agent first appears.
    buckets = len(la.bucket_series(rows, la.choose_bucket(1), {})["buckets"])
    assert all(len(v) == buckets for v in volumes.values())
    assert sum(volumes["custom:it-helpdesk"]) == 1 and sum(volumes["custom:support-desk"]) == 2


def test_needs_attention_names_the_generic_agent(monkeypatch):
    from admin.screens import conversations, overview

    sql, _params = overview.failures_sql("staging")
    assert "AS agent_slug" in sql
    monkeypatch.setattr(conversations, "_agent_slug_name", lambda kind, slug: "Support desk")
    rows = [{"name": "get_order_status.tool", "is_tool": 1, "event_attrs": '{"message": "Invalid order number"}',
             "output": "", "trace_id": "t1", "ts": "2026-11-24 17:09:00", "session_id": "s1",
             "agent_kind": "custom", "agent_slug": "support-desk"},
            {"name": "lookup.tool", "is_tool": 1, "event_attrs": '{"message": "boom"}', "output": "",
             "trace_id": "t2", "ts": "2026-11-24 17:10:00", "session_id": "s2",
             "agent_kind": "sales", "agent_slug": ""}]
    groups = overview.group_failures(rows, tool_names={}, agent_names={"sales": "Sales bot"}, hours=24)
    assert sorted(a for g in groups for a in g["agents"]) == ["Sales bot", "Support desk"]


def test_insight_classifier_call_is_one_background_trace(counter, monkeypatch):
    import contextlib

    from admin.screens import observability
    from core import observability as tracing

    opened: List[str] = []

    @contextlib.contextmanager
    def fake_root_span(name, attributes=None, **_k):
        opened.append(name)
        yield None

    class _LLM:
        def invoke(self, messages):
            assert opened == ["conversation.insights"]   # the call runs inside it
            return SimpleNamespace(content='{"topic": "pricing", "outcome": "resolved", "language": "en"}')

    monkeypatch.setattr(tracing, "root_span", fake_root_span)
    monkeypatch.setattr(ci, "_llm", lambda model_string: _LLM())
    assert ci.classify("custom", "Customer: how much?", model_string="m")["topic"] == "pricing"
    # ...and that trace is never listed as a conversation.
    assert ci.TRACE_NAME in observability._BACKGROUND_TOP_SPANS
    assert "demo.builder_assistant" in observability._BACKGROUND_TOP_SPANS
