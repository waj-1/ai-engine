"""Overview > Technical details: test traffic and the engine's own background
work each get a row that says what they are.

The technical rollups count every trace (the token spend is the whole bill),
but the per-agent table and the cost share grouped by agent kind alone. A
test run's judge, the Build with AI assistant, the insights sweep and KB
embeddings carry no agent kind, so they all landed in one row that read
"Unlabelled agent / unknown"; and an agent's turns made by a test run were
counted as that agent's traffic. `observability.activity_class` names them
from what the engine itself stamps on a trace: the eval runner's
``waj.eval.run_id`` and the names of the engine's own root spans.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from admin.screens import live_analytics as la
from admin.screens import observability as obs

RUN = "11111111-2222-3333-4444-555555555555"


def _raw(top, *, kind="", slug="", eval_run="", tokens=100, cost=0.01, minute=0):
    return {"created_at": "2026-11-25 10:%02d:00" % minute, "agent_kind": kind, "agent_slug": slug,
            "channel_slug": "", "top_span_name": top, "eval_run_id": eval_run,
            "input_tokens": tokens, "output_tokens": 10, "cache_read_tokens": 0, "cost_usd": cost,
            "latency_ms": 500, "iteration_count": 1, "error": 0, "ttft_ms": 0, "ttlt_ms": 0}


RAW = [
    _raw("custom.agent", kind="custom", slug="support-desk", tokens=1000, cost=0.10),
    _raw("custom.agent", kind="custom", slug="support-desk", tokens=1000, cost=0.10, minute=1),
    # The same agent answering a test run's cases.
    _raw("custom.agent", kind="custom", slug="support-desk", eval_run=RUN, tokens=300, cost=0.03, minute=2),
    _raw("business.agent", kind="business", eval_run=RUN, tokens=200, cost=0.02, minute=3),
    _raw("business.agent", kind="business", tokens=50, cost=0.005, minute=4),
    # The runner's own trace: its judge's model calls.
    _raw("eval_suite::Support desk: everyday questions", tokens=400, cost=0.04, minute=5),
    _raw("demo.builder_assistant", tokens=500, cost=0.05, minute=6),
    _raw("conversation.insights", tokens=60, cost=0.006, minute=7),
    _raw("openai.embeddings", tokens=7, cost=0.0, minute=8),
    _raw("AzureChatOpenAI.chat", tokens=20, cost=0.002, minute=9),
    _raw("ResponseStreamWriter", tokens=0, cost=0.0, minute=10),
    _raw("voice.live.turn", tokens=0, cost=0.0, minute=11),
    # Nothing identifies this one: it stays what it was.
    _raw("something.else", tokens=9, cost=0.001, minute=12),
]


@pytest.mark.parametrize("top, kind, eval_run, expected", [
    ("custom.agent", "custom", RUN, "activity:test_runs"),
    ("eval_suite::Any suite", "", "", "activity:test_judge"),
    ("demo.builder_assistant", "", "", "activity:builder_assistant"),
    ("conversation.insights", "", "", "activity:conversation_insights"),
    ("openai.embeddings", "", "", "activity:embeddings"),
    ("ChatOpenAI.chat", "", "", "activity:model_calls"),
    ("gemini.generate_content", "", "", "activity:model_calls"),
    ("ResponseStreamWriter", "", "", "activity:admin_tools"),
    ("voice.live.session", "", "", "activity:voice_measurements"),
    # An agent's ordinary turn, whatever its top span is called.
    ("custom.agent", "custom", "", None),
    ("openai.chat", "sales", "", None),
    ("conversation.insights", "business", "", None),
    ("something.else", "", "", None),
    ("", "", "", None),
])
def test_activity_class(top, kind, eval_run, expected):
    assert obs.activity_class(top, agent_kind=kind or None, eval_run_id=eval_run or None) == expected


def test_every_class_has_a_label_and_covers_the_background_span_names():
    assert set(obs.ACTIVITY_LABELS) == {key for key, *_ in obs.ACTIVITY_CLASSES} | {obs.ACTIVITY_TEST_RUNS}
    assert all(key.startswith(obs.ACTIVITY_PREFIX) for key in obs.ACTIVITY_LABELS)
    # Every untagged top span the Conversations list already treats as
    # background has a name here: none of them falls back to "unknown".
    for name in obs._BACKGROUND_TOP_SPANS:
        assert obs.activity_class(name) is not None, name


def test_rollup_names_each_kind_of_activity_and_keeps_the_totals(monkeypatch):
    # The by-agent chart lists the build's own agent kinds. Name one here, so
    # the test reads the same on a build with a product plugin and without.
    from admin.screens import live_analytics

    monkeypatch.setattr(live_analytics, "AGENT_KINDS", ("business",))
    with patch.object(la, "ch_query", side_effect=[RAW, []]), \
            patch.object(la, "_agent_key_labels", lambda keys: {"custom:support-desk": "Support desk"}):
        payload = la.load_analytics(hours=24)

    rows = {r["agent_kind"]: r for r in payload["per_agent"]}
    assert set(rows) == {
        "custom:support-desk", "business", "activity:test_runs", "activity:test_judge",
        "activity:builder_assistant", "activity:conversation_insights", "activity:embeddings",
        "activity:model_calls", "activity:admin_tools", "activity:voice_measurements", "unknown"}
    # The agents' rows hold their own traffic only...
    assert rows["custom:support-desk"]["volume"] == 2 and rows["custom:support-desk"]["tokens_in"] == 2000
    assert rows["business"]["volume"] == 1 and rows["business"]["tokens_in"] == 50
    # ...and the test run's turns are one row, whichever agent answered.
    assert rows["activity:test_runs"]["volume"] == 2 and rows["activity:test_runs"]["tokens_in"] == 500
    assert rows["activity:test_judge"]["tokens_in"] == 400
    assert rows["unknown"]["volume"] == 1
    labels = {r["agent_kind"]: r["label"] for r in payload["per_agent"]}
    assert labels["activity:test_runs"] == "Test runs" and labels["activity:test_judge"] == "Test judge"
    assert labels["activity:builder_assistant"] == "Builder assistant"
    assert labels["activity:conversation_insights"] == "Conversation insights"
    assert labels["custom:support-desk"] == "Support desk"
    # Agents first, the other activity after them.
    flags = [r["is_agent"] for r in payload["per_agent"]]
    assert flags == sorted(flags, reverse=True) and flags.count(True) == 2
    # Nothing is counted twice or dropped: the rows add up to the totals.
    totals = payload["totals"]
    assert totals["volume"] == len(RAW) == sum(r["volume"] for r in rows.values())
    assert totals["tokens_in"] == sum(r["input_tokens"] for r in RAW) == sum(r["tokens_in"] for r in rows.values())
    assert totals["cost_usd"] == pytest.approx(sum(r["cost_usd"] for r in RAW))
    shares = {r["agent_kind"]: r for r in payload["cost_share"]}
    assert set(shares) == set(rows)
    assert sum(r["cost_usd"] for r in shares.values()) == pytest.approx(totals["cost_usd"])
    assert shares["activity:builder_assistant"]["label"] == "Builder assistant"
    # The per-agent volume chart shows agents only; its total line still counts everything.
    assert set(payload["volume_by_agent"]) == {"Support desk", "business"}
    assert sum(sum(v) for v in payload["volume_by_agent"].values()) == 3
    assert sum(payload["series"]["volume"]) == len(RAW)


def test_agent_kinds_are_grouped_as_before_when_there_is_no_test_traffic():
    raw = [_raw("business.agent", kind="business"), _raw("sales.agent", kind="sales", minute=1),
           _raw("custom.agent", kind="custom", slug="a", minute=2), _raw("x", minute=3)]
    rows = [la._map_trace_row(r) for r in raw]
    assert [r["agent_kind"] for r in la.per_agent_rollup(rows, {})] == ["business", "sales", "custom:a", "unknown"]


def test_trace_query_reads_what_the_classes_need():
    assert "t.top_span_name AS top_span_name" in la._TRACE_SQL
    # The run id of the trace's own root span, not of any span inside it: a test
    # run the Build with AI assistant starts nests its suite span in the assistant's trace.
    assert "simpleJSONExtractString(t.metadata, 'waj.eval.run_id') AS eval_run_id" in la._TRACE_SQL
    assert "attributes, 'waj.eval.run_id'" not in la._TRACE_SQL


def test_technical_table_shows_the_label_not_unlabelled_agent(client):
    analytics = {"ok": True, "totals": {"p95_ms": 900, "cost_usd": 0.2, "cache_hit_rate": 0.0,
                                        "cache_read_tokens": 0},
                 "per_agent": [
                     {"agent_kind": "activity:test_runs", "label": "Test runs", "volume": 2,
                      "error_rate": 0.0, "tokens_in": 500, "tokens_out": 20, "cost_usd": 0.05,
                      "p50_ms": 500, "p95_ms": 500}]}
    with patch("admin.screens.live_analytics.load_analytics", return_value=analytics):
        page = client.get("/admin/live").text
    assert "Test runs" in page and "Unlabelled agent" not in page
