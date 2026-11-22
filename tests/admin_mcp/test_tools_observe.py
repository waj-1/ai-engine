"""Tests for the Task 14 observe-domain tools: live_analytics, traces,
search. These wrap portal screen functions that hit Laminar/ClickHouse/DB
directly (no Supabase client), so tests monkeypatch the screen functions on
their SOURCE modules (admin.screens.live_analytics / admin.screens.observability
/ admin.search) rather than via a FakeClient — tools_observe.py imports them
lazily inside each tool function, so patching the source module's attribute
is picked up on the next call."""
from __future__ import annotations

import admin.screens.live_analytics as live_analytics_mod
import admin.screens.observability as observability_mod
import admin.search as search_mod
import admin_mcp.tools_observe as to


# ---------------------------------------------------------------------------
# live_analytics
# ---------------------------------------------------------------------------


def test_live_analytics_passes_kwargs_and_clamps_hours(monkeypatch):
    captured = {}

    def fake_load_analytics(*, hours, agent_kind, channel, status, environment):
        captured.update(hours=hours, agent_kind=agent_kind, channel=channel,
                        status=status, environment=environment)
        return {"ok": True, "totals": {"volume": 3}}

    monkeypatch.setattr(live_analytics_mod, "load_analytics", fake_load_analytics)

    # `environment` is the deprecated alias of trace_env — still accepted.
    out = to.live_analytics(hours=999999, agent_kind="business", channel="web",
                            status="error", environment="staging")

    assert out == {"ok": True, "totals": {"volume": 3}, "series_omitted": True}
    assert captured == {"hours": 8760, "agent_kind": "business", "channel": "web",
                        "status": "error", "environment": "staging"}


def test_live_analytics_trace_env_and_series_opt_in(monkeypatch):
    captured = {}

    def fake_load_analytics(**kw):
        captured.update(kw)
        return {"ok": True, "totals": {}, "series": {"x": [1]}, "buckets": [1]}

    monkeypatch.setattr(live_analytics_mod, "load_analytics", fake_load_analytics)

    out = to.live_analytics(trace_env="prod")
    assert captured["environment"] == "prod"
    assert "series" not in out and "buckets" not in out
    out = to.live_analytics(trace_env="prod", include_series=True)
    assert out["series"] == {"x": [1]}


def test_live_analytics_conflicting_env_names_is_an_error(monkeypatch):
    monkeypatch.setattr(live_analytics_mod, "load_analytics", lambda **kw: {})
    out = to.live_analytics(trace_env="prod", environment="staging")
    assert "error" in out


def test_live_analytics_rejects_bad_status():
    out = to.live_analytics(status="bogus")
    assert "error" in out
    assert "status" in out["error"]


# ---------------------------------------------------------------------------
# traces
# ---------------------------------------------------------------------------


def test_traces_search_pass_through(monkeypatch):
    captured = {}

    def fake_load_traces_list(*, past_hours, page_size, search, status,
                              channel_slug, agent_kind, env, include_background):
        captured.update(past_hours=past_hours, page_size=page_size, search=search,
                        status=status, channel_slug=channel_slug,
                        agent_kind=agent_kind, env=env,
                        include_background=include_background)
        return {"traces": [{"id": "t1"}, {"id": "t2"}], "total": 2, "notice": None}

    monkeypatch.setattr(observability_mod, "load_traces_list", fake_load_traces_list)

    out = to.traces(action="search", hours=48, search_text="hello",
                    channel_slug="wa", agent_kind="business", env="prod")

    assert out == {"traces": [{"id": "t1"}, {"id": "t2"}], "total": 2, "notice": None,
                   "offset": 0, "limit": 25, "has_more": False,
                   "tool_filter_applied": True}
    assert captured == {"past_hours": 48, "page_size": 100, "search": "hello",
                        "status": None, "channel_slug": "wa",
                        "agent_kind": "business", "env": "prod",
                        "include_background": False}


def test_traces_search_narrows_by_tool(monkeypatch):
    monkeypatch.setattr(
        observability_mod, "load_traces_list",
        lambda **kw: {"traces": [{"id": "t1"}, {"id": "t2"}], "total": 2,
                      "notice": None})
    monkeypatch.setattr(
        observability_mod, "filter_traces_by_tool_result",
        lambda trs, name: ([t for t in trs if t["id"] == "t1"], True))

    out = to.traces(action="search", tool="book_appointment")

    assert out["traces"] == [{"id": "t1"}]
    assert out["total"] == 1
    assert out["tool_filter_applied"] is True


def test_traces_search_no_tool_reports_filter_applied(monkeypatch):
    # No `tool` requested at all — nothing to narrow, so the flag reads as
    # applied (there is no unnarrowed-vs-narrowed ambiguity to warn about).
    monkeypatch.setattr(
        observability_mod, "load_traces_list",
        lambda **kw: {"traces": [{"id": "t1"}], "total": 1, "notice": None})

    out = to.traces(action="search")

    assert out["tool_filter_applied"] is True


def test_traces_search_tool_filter_failure_reports_unapplied_not_narrowed(monkeypatch):
    # The ClickHouse match query died: filter_traces_by_tool_result returns
    # the list UNNARROWED with applied=False. Before the fix, tools_observe.py
    # called the flag-less filter_traces_by_tool and an LLM operator would
    # have no way to tell this apart from "every trace in the window called
    # `book_appointment`" — a wrong forensic answer, not a degraded one.
    monkeypatch.setattr(
        observability_mod, "load_traces_list",
        lambda **kw: {"traces": [{"id": "t1"}, {"id": "t2"}], "total": 2,
                      "notice": None})
    monkeypatch.setattr(
        observability_mod, "filter_traces_by_tool_result",
        lambda trs, name: (trs, False))
    monkeypatch.setattr(
        observability_mod, "tool_filter_notice",
        lambda name: f"Tool filter {name!r} could NOT be applied")

    out = to.traces(action="search", tool="book_appointment")

    # Unnarrowed — both rows still present — but the flag says so.
    assert out["traces"] == [{"id": "t1"}, {"id": "t2"}]
    assert out["total"] == 2
    assert out["tool_filter_applied"] is False
    assert "could NOT be applied" in out["notice"]


def test_traces_get_span_transcript_overview_pass_through(monkeypatch):
    monkeypatch.setattr(observability_mod, "load_span_tree_json",
                        lambda trace_id: {"trace": {"id": trace_id}, "tree": []})
    monkeypatch.setattr(
        observability_mod, "load_span_detail_json",
        lambda trace_id, span_id: {"span": {"span_id": span_id, "trace": trace_id}})
    monkeypatch.setattr(
        observability_mod, "load_transcript_json",
        lambda trace_id, env=None: {"trace": {"id": trace_id}, "env": env, "turns": []})
    monkeypatch.setattr(
        observability_mod, "load_trace_overview_json",
        lambda trace_id, env=None: {"trace": {"id": trace_id}, "env": env, "tools": []})

    assert to.traces(action="get", trace_id="tr1") == {"trace": {"id": "tr1"}, "tree": []}
    assert to.traces(action="span", trace_id="tr1", span_id="sp1") == {
        "span": {"span_id": "sp1", "trace": "tr1"}}
    assert to.traces(action="transcript", trace_id="tr1", env="prod") == {
        "trace": {"id": "tr1"}, "env": "prod", "turns": []}
    assert to.traces(action="overview", trace_id="tr1", env="prod") == {
        "trace": {"id": "tr1"}, "env": "prod", "tools": []}


def test_traces_missing_trace_id_errors():
    out = to.traces(action="get")
    assert "error" in out
    assert "trace_id" in out["error"]


def test_traces_span_missing_span_id_errors(monkeypatch):
    monkeypatch.setattr(observability_mod, "load_span_tree_json", lambda trace_id: {})
    out = to.traces(action="span", trace_id="tr1")
    assert "error" in out
    assert "span_id" in out["error"]


def test_traces_unknown_action_errors():
    out = to.traces(action="bogus", trace_id="tr1")
    assert "error" in out
    assert "bogus" in out["error"]
    assert "search" in out["hint"] and "overview" in out["hint"]


def test_traces_rejects_bad_status():
    out = to.traces(action="search", status="bogus")
    assert "error" in out
    assert "status" in out["error"]


def test_traces_rejects_bad_env():
    out = to.traces(action="search", env="dev")
    assert "error" in out


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_pass_through(monkeypatch):
    captured = {}

    def fake_search(q, env):
        captured.update(q=q, env=env)
        return [{"type": "profile", "label": "Business", "sublabel": "",
                "href": "/admin/agent-profiles/p1"}]

    monkeypatch.setattr(search_mod, "search", fake_search)

    out = to.search(q="business", env="prod")

    assert captured == {"q": "business", "env": "prod"}
    assert out == {"results": [{"type": "profile", "label": "Business",
                               "sublabel": "", "href": "/admin/agent-profiles/p1"}]}


def test_search_rejects_bad_env():
    out = to.search(q="business", env="dev")
    assert "error" in out
