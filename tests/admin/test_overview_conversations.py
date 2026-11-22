"""Portal revamp B2 — Overview + Conversations behaviour.

Covers the audit fixes that are not markup: one environment default across
the Operate screens, honest tiles (no confident zero, labelled fallback),
failures grouped by cause in plain words, and the outcome columns.
"""
from __future__ import annotations

import inspect
from unittest.mock import patch

import pytest

from tests.ch_fake import make_ch_query
from tests.scoped_build import full_build


# --------------------------------------------------------------------------- #
# Environment default — one dependency for every Operate screen (P0 #3)
# --------------------------------------------------------------------------- #

def _env_dependency(fn):
    return inspect.signature(fn).parameters["env"].default.dependency


def test_operate_screens_share_one_env_default(monkeypatch):
    full_build(monkeypatch)
    import admin.observability_explorer_handlers as ex
    from admin import router as r

    screens = [
        r.live_monitor, r.observability_list, r.lmnr_diagnose,
        ex.observability_explorer, ex.observability_explorer_transcript_json,
        ex.observability_explorer_overview_json, ex.observability_explorer_audio_json,
    ]
    for fn in screens:
        assert _env_dependency(fn) is r.current_env_live, fn.__name__
    # …and with nothing selected they open on production.
    assert r.current_env_live(None, None) == "prod"


# --------------------------------------------------------------------------- #
# Plain language
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,phrase", [
    ("TimeoutError: gateway timeout after 600ms", "did not respond in time"),
    ("httpx.ConnectError [Errno 111] Connection refused", "could not be reached"),
    ("Client error '404 Not Found'", "could not be found"),
    ("403 Forbidden", "access was refused"),
    ("429 Too Many Requests", "usage limit"),
    ("ValueError: missing required field: amount", "missing or invalid"),
    ("boom", "something unexpected"),
])
def test_plain_failure_reason(text, phrase):
    from admin.screens.plain_language import plain_failure_reason

    out = plain_failure_reason(text)
    assert phrase in out
    assert "Error" not in out  # never echoes the exception class


def test_action_label_prefers_display_name_then_humanises():
    from admin.screens.plain_language import action_label, agent_label, is_handoff_tool

    assert action_label("send_payment_link.tool", {"send_payment_link": "Send payment link"}) == "Send payment link"
    assert action_label("lookup_balance.tool") == "Lookup balance"
    assert agent_label("agents_collection") == "Collection"
    assert agent_label("orchard", {"orchard": "Jawhara"}) == "Jawhara"
    assert is_handoff_tool("support_handoff_to_human.tool")
    assert is_handoff_tool("collection_escalate")
    assert not is_handoff_tool("send_payment_link")


# --------------------------------------------------------------------------- #
# Overview tiles
# --------------------------------------------------------------------------- #

def test_tiles_compare_with_the_previous_period():
    from admin.screens.overview import build_tiles

    cur = {"conversations": 120, "attention": 12, "handoffs": 3, "actions": 40,
           "failed_actions": 5, "turns": 300, "avg_response_sec": 2.4}
    prev = {"conversations": 100, "attention": 5, "handoffs": 3, "actions": 30,
            "failed_actions": 10, "turns": 250, "avg_response_sec": 3.0}
    tiles = {t["key"]: t for t in build_tiles(cur, prev, previous_label="previous 7 days")}
    assert tiles["conversations"]["value"] == "120"
    assert tiles["conversations"]["delta"] == {"text": "+20%", "tone": "ok"}
    # Share needing attention went 5% → 10%: +5 points, bad.
    assert tiles["attention"]["delta"]["text"] == "+5.0 pts"
    assert tiles["attention"]["delta"]["tone"] == "bad"
    assert tiles["failed_actions"]["delta"] == {"text": "-50%", "tone": "ok"}
    assert tiles["response"]["value"] == "2.4 s"
    assert tiles["handoffs"]["delta"]["text"] == "No change"
    # Nothing claims "resolved" — the data can't say that.
    assert not any("esolved" in t["label"] for t in tiles.values())


def test_tiles_when_clickhouse_is_down_show_dashes_and_label_the_fallback():
    from admin.screens.overview import build_tiles

    tiles = build_tiles(None, None, previous_label="previous 24 hours",
                        fallback_conversations=8, env="prod")
    assert tiles[0]["value"] == "8"
    assert "All environments" in tiles[0]["caption"]
    assert "not limited to production" in tiles[0]["caption"]
    assert all(t["value"] == "—" and t["unavailable"] for t in tiles[1:])

    bare = build_tiles(None, None, previous_label="x")
    assert bare[0]["value"] == "—"


def test_build_overview_reports_a_dead_source_as_down(monkeypatch):
    from admin.screens import overview as ov

    monkeypatch.setattr("core.lmnr_clickhouse.query", make_ch_query(raises=True))
    monkeypatch.setattr(ov, "voice_usage", lambda *_a, **_k: None)
    monkeypatch.setattr(ov, "conversation_log_count", lambda *_a, **_k: None)
    monkeypatch.setattr(ov.pl, "load_agent_display_names", lambda _e: {})
    out = ov.build_overview(env="prod", period_key="7d", client=None)
    assert out["status"]["state"] == "down"
    assert out["sources"]["outcomes"] is False
    assert out["agents"] == [] and out["attention"] == []
    assert all(t["value"] == "—" for t in out["tiles"])


def test_outcome_sql_is_env_scoped_and_excludes_background(monkeypatch):
    from admin.screens import observability
    from admin.screens.overview import outcome_sql

    monkeypatch.setattr(observability, "_BACKGROUND_AGENT_KINDS", ("sample_background",))
    sql, params = outcome_sql("prod")
    assert "sagg.env = {env:String}" in sql and params["env"] == "prod"
    assert "'sample_background'" in sql
    assert "lmnr_diagnose_test_span" in sql
    # The FINAL scan is filtered in its own subquery (CH 26.5 FINAL+JOIN bug).
    assert "FROM (SELECT id, start_time" in sql
    sql_s, _ = outcome_sql("staging")
    assert "sagg.env = ''" in sql_s


def test_outcome_rows_roll_up_into_tiles_and_agents(monkeypatch):
    from admin.screens import overview as ov

    rows = [
        {"period": "cur", "agent_kind": "agents_collection", "turns": 10, "conversations": 4,
         "conv_attention": 1, "conv_handoff": 1, "actions": 6, "failed_actions": 1,
         "avg_response_sec": 2.0},
        {"period": "prev", "agent_kind": "agents_collection", "turns": 5, "conversations": 2,
         "conv_attention": 0, "conv_handoff": 0, "actions": 2, "failed_actions": 0,
         "avg_response_sec": 3.0},
    ]

    def fake(sql, params=None, **_k):
        if "AS period" in sql:
            return rows
        return []

    monkeypatch.setattr("core.lmnr_clickhouse.query", fake)
    monkeypatch.setattr(ov, "voice_usage", lambda *_a, **_k: None)
    monkeypatch.setattr(ov.pl, "load_agent_display_names",
                        lambda _e: {"agents_collection": "Salma — Collections"})
    monkeypatch.setattr(ov.pl, "load_tool_display_names", lambda _e: {})
    out = ov.build_overview(env="prod", period_key="7d", client=None)
    assert out["status"]["state"] == "ok"
    tiles = {t["key"]: t for t in out["tiles"]}
    assert tiles["conversations"]["value"] == "4"
    assert tiles["conversations"]["delta"]["text"] == "+100%"
    (agent,) = out["agents"]
    assert agent["name"] == "Salma — Collections"
    assert "agent_kind=agents_collection" in agent["href"] and "hours=168" in agent["href"]


# --------------------------------------------------------------------------- #
# Needs attention — grouped by cause
# --------------------------------------------------------------------------- #

def test_failures_group_by_cause_and_count_each_failure_once():
    from admin.screens.overview import group_failures

    exc = '{"exception.type":"TimeoutError","exception.message":"gateway timeout"}'
    rows = [
        # trace A: the tool failed AND the agent span around it recorded it too.
        {"trace_id": "A", "name": "send_payment_link.tool", "is_tool": 1, "event_attrs": exc,
         "session_id": "s1", "agent_kind": "agents_collection", "ts": "2026-10-11 09:00:00"},
        {"trace_id": "A", "name": "agents_collection.chat_turn", "is_tool": 0, "event_attrs": exc,
         "session_id": "s1", "agent_kind": "agents_collection", "ts": "2026-10-11 09:00:00"},
        {"trace_id": "B", "name": "send_payment_link.tool", "is_tool": 1, "event_attrs": exc,
         "session_id": "s2", "agent_kind": "agents_collection", "ts": "2026-10-11 10:00:00"},
        # trace C: no tool failed — one "agent ran into a problem" entry.
        {"trace_id": "C", "name": "AzureChatOpenAI.chat", "is_tool": 0,
         "event_attrs": '{"exception.type":"RateLimitError","exception.message":"429"}',
         "session_id": "s3", "agent_kind": "agents_support", "ts": "2026-10-11 11:00:00"},
    ]
    groups = group_failures(rows, tool_names={"send_payment_link": "Send payment link"},
                            agent_names={}, hours=24)
    top = groups[0]
    assert top["sentence"] == "Send payment link failed — the service did not respond in time"
    assert top["count"] == 2 and top["conversations"] == 2
    assert top["href"].endswith("#trace=A") and "hours=24" in top["href"]
    assert "TimeoutError" in top["technical"]
    other = groups[1]
    assert other["sentence"].startswith("The agent ran into a problem")
    assert other["count"] == 1


# --------------------------------------------------------------------------- #
# Conversations list — outcome per conversation
# --------------------------------------------------------------------------- #

def test_group_outcomes_from_action_counts():
    from admin.screens.observability import group_traces_by_session
    from admin.screens.conversations import _group_outcome

    traces = [
        {"id": "t1", "session_id": "s1", "status": "success", "tool_calls": 2, "tool_errors": 1},
        {"id": "t2", "session_id": "s2", "status": "success", "tool_calls": 1, "handoffs": 1},
        {"id": "t3", "session_id": "s3", "status": "success"},
        {"id": "t4", "session_id": "s4", "status": "error"},
    ]
    groups = {g["session_id"]: g for g in group_traces_by_session(traces)}
    assert groups["s1"]["failed_actions"] == 1
    assert [_group_outcome(groups[s]) for s in ("s1", "s2", "s3", "s4")] == [
        "attention", "handoff", "ok", "attention"]


def test_conversations_route_filters_by_outcome(client):
    from admin import observability_explorer_handlers  # noqa: F401
    from tests.ch_fake import ch_trace_row

    rows = [
        ch_trace_row("T-bad", agent_kind="sales", session_id="s-bad", has_error=1),
        ch_trace_row("T-good", agent_kind="sales", session_id="s-good"),
    ]
    with patch("core.lmnr_clickhouse.query", make_ch_query(traces=rows)):
        r = client.get("/admin/observability?outcome=attention")
    assert r.status_code == 200
    assert 'data-trace-id="T-bad"' in r.text
    assert 'data-trace-id="T-good"' not in r.text


def test_transcript_attaches_every_conversation_action_and_summarises_failures():
    """Tool calls of ALL the conversation's turns attach to the reply they fed,
    labelled in words, and a failure produces a plain summary sentence."""
    from admin.screens import observability as obs

    trace = {"id": "T2", "status": "error", "sessionId": "sess-1",
             "startTime": "2026-10-11T09:05:00Z",
             "metadata": '{"waj.agent_kind":"agents_collection"}'}
    persisted = [
        {"role": "user", "content": "hi", "ts": "2026-10-11T09:00:00+00:00"},
        {"role": "assistant", "content": "Your balance is 250", "ts": "2026-10-11T09:00:05+00:00"},
        {"role": "user", "content": "send the link", "ts": "2026-10-11T09:04:58+00:00"},
        {"role": "assistant", "content": "A colleague will follow up", "ts": "2026-10-11T09:05:06+00:00"},
    ]
    session_tools = [
        {"name": "lookup_balance", "input": "{}", "output": "{}", "is_error": False,
         "error": None, "ts": "2026-10-11T09:00:02Z", "trace_id": "T1"},
        {"name": "send_payment_link", "input": "{}", "output": '{"error":"gateway timeout"}',
         "is_error": True, "error": "TimeoutError: gateway timeout", "ts": "2026-10-11T09:05:01Z",
         "trace_id": "T2"},
    ]
    with patch.object(obs, "ch_get_trace", return_value=trace), \
         patch.object(obs, "_clickhouse_spans", return_value=[]), \
         patch.object(obs, "_persisted_conversation", return_value=persisted), \
         patch.object(obs, "_session_tool_spans", return_value=session_tools), \
         patch.object(obs, "_session_row", return_value=None), \
         patch("admin.screens.plain_language.load_tool_display_names",
               return_value={"send_payment_link": "Send payment link"}), \
         patch("admin.screens.plain_language.load_agent_display_names",
               return_value={"agents_collection": "Salma"}):
        data = obs.load_transcript_json("T2", env="prod")

    assert data["found"] is True
    first, second = data["turns"][1], data["turns"][3]
    assert [t["label"] for t in first["tools"]] == ["Lookup balance"]
    assert [t["label"] for t in second["tools"]] == ["Send payment link"]
    s = data["summary"]
    assert s["agent_name"] == "Salma"
    assert s["outcome"] == "attention"
    (failure,) = s["failures"]
    assert failure["sentence"] == "Send payment link failed — the service did not respond in time"
    assert failure["agent_reply"] == "A colleague will follow up"
    assert failure["turn_index"] == 3
