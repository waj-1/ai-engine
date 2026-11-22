"""Observability must SIGNAL failure, not degrade into plausible-looking data.

Every screen here reads ClickHouse and nothing else, so a ClickHouse problem is
an observability blackout — and the moment it renders as a number instead of a
notice, the operator reads confident wrong values during exactly the incident
the Dashboard exists for. Three shapes of that, each verified against the real
code path:

* **Partial Dashboard outage.** ``lmnr_ok`` is an OR across five independent
  reads (deliberately — one hiccup must not blank five tiles). So the heavy
  1000-row 24h trace query can die while the cheap TTFT aggregate answers, and
  the page would show a ``$0`` "Token spend 24h" tile plus an affirmative
  "No error traces in this window." card beside live numbers, ``notice=None``.
* **Span detail during an outage.** An empty span list means both "unreachable"
  and "not in this trace"; reporting the first as "Span not found" sends the
  operator hunting for data loss while the tree pane still shows the span.
* **Tool filter failing open.** The unnarrowed list reads as "every trace called
  this tool" while the picker still shows the tool as applied.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from tests.ch_fake import ch_trace_row, make_ch_query

TRACE_A = "11111111-1111-4111-8111-111111111111"
TRACE_B = "22222222-2222-4222-8222-222222222222"

_TTFT = {"ttft_p50": 800.0, "ttft_p95": 1500.0,
         "ttlt_p50": 3000.0, "ttlt_p95": 5000.0}


def _traces() -> List[Dict[str, Any]]:
    return [
        ch_trace_row(TRACE_A, agent_kind="support_bot", has_error=0,
                     in_tokens=100, out_tokens=200, duration_sec=0.5,
                     top_span_name="agent.invoke"),
        ch_trace_row(TRACE_B, agent_kind="business", has_error=1,
                     in_tokens=50, out_tokens=10, duration_sec=2.0,
                     top_span_name="agent.invoke"),
    ]


def _healthy_query(**over):
    kwargs: Dict[str, Any] = dict(
        traces=_traces(), sessions=7, ttft=dict(_TTFT),
        tools=[{"name": "find_slots", "calls": "4", "errors": "0"}],
    )
    kwargs.update(over)
    return make_ch_query(**kwargs)


def _query_with_dead(*, page_size: Optional[int] = None, dead: str = ""):
    """A healthy ClickHouse with ONE read broken.

    ``page_size`` kills a specific trace-list window (200 = the 1h rollups,
    1000 = the 24h token window); ``dead`` kills a non-trace read by SQL
    fingerprint."""
    base = _healthy_query()

    def _query(sql: str, params: Optional[Dict[str, Any]] = None, **kw):
        params = params or {}
        is_trace_list = ("FROM traces_replacing FINAL" in sql
                         and "uniqExact" not in sql)
        if page_size is not None and is_trace_list:
            if int(params.get("limit") or 0) == page_size:
                raise RuntimeError("clickhouse read timed out")
        if dead and dead in sql:
            raise RuntimeError("clickhouse read timed out")
        return base(sql, params, **kw)

    return _query


# ---------------------------------------------------------------------------
# load_live_insights — per-source health
# ---------------------------------------------------------------------------


def test_a_healthy_stack_reports_every_source_ok(monkeypatch):
    from admin.screens import observability as obs

    monkeypatch.setattr("core.lmnr_clickhouse.query", _healthy_query())
    out = obs.load_live_insights(past_hours=1)

    assert out["lmnr_ok"] is True
    assert out["degraded"] is False
    assert out["notice"] is None
    assert all(out["sources"].values()), out["sources"]


def test_a_dead_24h_trace_read_is_named_not_priced_as_zero(monkeypatch):
    """The headline case. The 24h window feeds the token sums; dead, they are 0
    and price to a confident `$0.00` that is indistinguishable from an idle
    day."""
    from admin.screens import observability as obs

    monkeypatch.setattr("core.lmnr_clickhouse.query", _query_with_dead(page_size=1000))
    out = obs.load_live_insights(past_hours=1)

    # The OR is intentional and stays: the other four reads answered.
    assert out["lmnr_ok"] is True
    assert out["sources"]["traces_24h"] is False
    assert out["sources"]["traces_recent"] is True
    assert out["degraded"] is True
    assert out["notice"], "a partial outage must not render with notice=None"
    assert "24h traces" in out["notice"]
    # The live reads are still reported — a partial failure must not blank them.
    assert out["sessions_24h"] == 7
    assert out["tokens"]["in_1h"] == 150


def test_a_dead_recent_trace_read_is_named(monkeypatch):
    from admin.screens import observability as obs

    monkeypatch.setattr("core.lmnr_clickhouse.query", _query_with_dead(page_size=200))
    out = obs.load_live_insights(past_hours=1)

    assert out["lmnr_ok"] is True
    assert out["sources"]["traces_recent"] is False
    assert out["error_traces"] == []
    assert "recent traces" in (out["notice"] or "")


def test_a_dead_tool_rollup_is_distinguishable_from_no_tool_calls(monkeypatch):
    """`top_tools == []` means both "no calls in the window" and "the query
    never ran". Only the second may blank the Tool-call success % tile."""
    from admin.screens import observability as obs

    monkeypatch.setattr("core.lmnr_clickhouse.query",
                        _query_with_dead(dead="LIKE '%.tool'"))
    dead = obs.load_live_insights(past_hours=1)
    assert dead["top_tools"] == []
    assert dead["sources"]["top_tools"] is False

    monkeypatch.setattr("core.lmnr_clickhouse.query", _healthy_query(tools=[]))
    quiet = obs.load_live_insights(past_hours=1)
    assert quiet["top_tools"] == []
    assert quiet["sources"]["top_tools"] is True
    assert quiet["degraded"] is False


def test_a_total_outage_keeps_the_full_unavailable_notice(monkeypatch):
    from admin.screens import observability as obs

    monkeypatch.setattr("core.lmnr_clickhouse.query", make_ch_query(raises=True))
    out = obs.load_live_insights(past_hours=1)

    assert out["lmnr_ok"] is False
    assert out["degraded"] is True
    assert out["notice"] == obs._CH_NOTICE
    assert not any(out["sources"].values())


# ---------------------------------------------------------------------------
# _live_kpis — a tile whose source failed shows a dash, never a zero
# ---------------------------------------------------------------------------


class _NullDB:
    """A Supabase stand-in whose every query raises, so the KPI values under
    test can only come from `insights`."""

    def table(self, _name):
        return self

    def select(self, *_a, **_kw):
        return self

    def eq(self, *_a, **_kw):
        return self

    def gte(self, *_a, **_kw):
        return self

    def limit(self, *_a, **_kw):
        return self

    def execute(self):
        raise RuntimeError("no db in this test")


def _kpis(insights: Dict[str, Any]) -> Dict[str, Any]:
    from admin.router import _live_kpis

    return _live_kpis(_NullDB(), "prod", insights)


def _insights(**over) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "lmnr_ok": True,
        "sessions_24h": 7,
        "tokens": {"in_1h": 0, "out_1h": 0, "in_24h": 0, "out_24h": 0},
        "top_tools": [],
        "latency": {"p50_ms": None, "p95_ms": None},
        "ttft": {"p50_ms": 800, "p95_ms": 1500},
        "ttlt": {"p50_ms": 3000, "p95_ms": 5000},
        "sources": {"traces_recent": True, "traces_24h": True,
                    "sessions": True, "ttft": True, "top_tools": True},
    }
    base.update(over)
    return base


def test_token_spend_tile_is_a_dash_when_its_source_failed():
    """`$0` beside live-looking numbers is the lie this fix exists for."""
    src = {"traces_recent": True, "traces_24h": False,
           "sessions": True, "ttft": True, "top_tools": True}
    assert _kpis(_insights(sources=src))["token_spend_usd"] == "—"
    # …and an ACTUALLY idle 24h still prices, so the dash means "unknown".
    assert _kpis(_insights())["token_spend_usd"] == 0.0


def test_tool_success_tile_is_a_dash_when_the_rollup_failed():
    src = {"traces_recent": True, "traces_24h": True,
           "sessions": True, "ttft": True, "top_tools": False}
    tools = [{"name": "find_slots", "calls": 4, "errors": 0}]
    assert _kpis(_insights(sources=src, top_tools=tools))["tool_success_pct"] == "—"
    assert _kpis(_insights(top_tools=tools))["tool_success_pct"] == 100.0


def test_ttft_tiles_are_dashes_when_the_ttft_read_failed():
    src = {"traces_recent": True, "traces_24h": True,
           "sessions": True, "ttft": False, "top_tools": True}
    out = _kpis(_insights(sources=src))
    assert out["ttft_p50_ms"] == "—"
    assert out["ttlt_p50_ms"] == "—"


def test_kpis_without_a_sources_map_are_unchanged():
    """Legacy/stubbed insights carry no `sources`; unknown must read as OK so a
    healthy render is untouched."""
    ins = _insights()
    ins.pop("sources")
    out = _kpis(ins)
    assert out["ttft_p50_ms"] == 800
    assert out["token_spend_usd"] == 0.0


# ---------------------------------------------------------------------------
# live.html — the error-traces card must not claim an all-clear
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def jinja_env():
    from admin.router import templates

    return templates.env


def _ov(**over: Any) -> Dict[str, Any]:
    """A minimal Overview context (admin.screens.overview.build_overview)."""
    from admin.screens.overview import build_tiles, resolve_period

    cur = {"conversations": 3, "attention": 1, "handoffs": 0, "actions": 4,
           "failed_actions": 1, "turns": 5, "avg_response_sec": 2.0}
    ov = {
        "period": resolve_period("24h"),
        "periods": [{"key": "24h", "label": "Last 24 hours"}],
        "status": {"state": "ok", "headline": "All agents running normally.", "detail": None},
        "sources": {"outcomes": True, "attention": True, "voice_usage": True},
        "tiles": build_tiles(cur, None, previous_label="previous 24 hours"),
        "agents": [], "attention": [], "voice_usage": None, "has_traffic": True,
        "fallback_conversations": None,
    }
    ov.update(over)
    return ov


def _render_live(jinja_env, ov: Dict[str, Any]) -> str:
    return jinja_env.get_template("live.html").render(
        request=None,
        ctx={"email": "admin@waj.test"},
        active="live",
        current_env="prod",
        tab="overview",
        ov=ov,
        kpis={"sessions_24h": 7, "sessions_scope": "env", "tool_success_pct": "—",
              "latency_p95_ms": "—", "token_spend_usd": "—", "ttft_p50_ms": 800,
              "ttlt_p50_ms": 3000, "cache_hit_rate_pct": "—", "cache_saved_usd": "—"},
        lmnr_insights={}, voice_live={"ch_ok": False}, show_voice=False,
        analytics_ok=False, per_agent_tech=[], agent_kinds=[], agent_names={},
        channels=[], hours=24,
    )


def test_error_card_does_not_claim_all_clear_when_its_query_failed(jinja_env):
    """The failure-list read died: Needs attention is UNKNOWN, not empty."""
    html = _render_live(jinja_env, _ov(
        sources={"outcomes": True, "attention": False, "voice_usage": True},
        status={"state": "partial", "headline": "Some live statistics are delayed.",
                "detail": "x"},
    ))
    assert "No failed actions or errors" not in html
    assert "unknown — not empty" in html


def test_error_card_still_says_all_clear_when_the_query_ran(jinja_env):
    html = _render_live(jinja_env, _ov())
    assert "No failed actions or errors" in html


def test_a_partial_outage_banner_renders_above_the_tiles(jinja_env):
    """ONE status strip carries the outage, above the tiles."""
    headline = "Some live statistics are delayed — the list of problems below could not be loaded."
    html = _render_live(jinja_env, _ov(
        sources={"outcomes": True, "attention": False, "voice_usage": True},
        status={"state": "partial", "headline": headline, "detail": "ClickHouse said no"},
    ))
    assert headline in html
    assert html.index(headline) < html.index('class="stat-grid"')
    assert html.count('class="status-strip') == 1


def test_a_dead_outcome_source_never_renders_a_confident_zero(jinja_env):
    """ClickHouse down: every outcome tile is "—", and Conversations shows the
    conversation-log count LABELLED as all environments (audit P0 #2)."""
    from admin.screens.overview import build_tiles

    tiles = build_tiles(None, None, previous_label="previous 24 hours",
                        fallback_conversations=8, env="prod")
    html = _render_live(jinja_env, _ov(
        tiles=tiles, has_traffic=False,
        sources={"outcomes": False, "attention": False, "voice_usage": True},
        status={"state": "down", "headline": "Live statistics are temporarily unavailable.",
                "detail": None},
    ))
    assert tiles[0]["value"] == "8" and "All environments" in tiles[0]["caption"]
    assert all(t["value"] == "—" for t in tiles[1:])
    assert 'kpi-value num">0<' not in html
    assert "No conversations in the" not in html


# ---------------------------------------------------------------------------
# load_span_detail_json — outage vs. genuinely-absent span
# ---------------------------------------------------------------------------


def test_span_detail_reports_the_outage_not_span_not_found(monkeypatch):
    from admin.screens import observability as obs

    monkeypatch.setattr("core.lmnr_clickhouse.query", make_ch_query(raises=True))
    out = obs.load_span_detail_json(TRACE_A, "00000000-0000-0000-afc1-b3f70b5cfd5c")

    assert out["span"] is None
    assert out["notice"] == obs._CH_NOTICE
    assert "not found" not in out["notice"].lower()


def test_span_detail_still_says_not_found_when_the_store_answered(monkeypatch):
    from admin.screens import observability as obs

    monkeypatch.setattr("core.lmnr_clickhouse.query", make_ch_query(spans=[]))
    out = obs.load_span_detail_json(TRACE_A, "00000000-0000-0000-afc1-b3f70b5cfd5c")

    assert out["span"] is None
    assert out["notice"] == "Span not found"


# ---------------------------------------------------------------------------
# filter_traces_by_tool — a filter that failed open must say so
# ---------------------------------------------------------------------------


def test_tool_filter_reports_when_it_could_not_be_applied(monkeypatch):
    from admin.screens import observability as obs

    traces = [{"id": TRACE_A}, {"id": TRACE_B}]
    monkeypatch.setattr("core.lmnr_clickhouse.query", make_ch_query(raises=True))
    out, applied = obs.filter_traces_by_tool_result(traces, "lookup_order")

    assert out == traces, "unnarrowed is still the right list to show"
    assert applied is False


def test_tool_filter_reports_applied_on_a_real_narrow(monkeypatch):
    from admin.screens import observability as obs

    traces = [{"id": TRACE_A}, {"id": TRACE_B}]
    monkeypatch.setattr("core.lmnr_clickhouse.query",
                        make_ch_query(tool_matches=[TRACE_A]))
    out, applied = obs.filter_traces_by_tool_result(traces, "lookup_order")

    assert [t["id"] for t in out] == [TRACE_A]
    assert applied is True


def test_the_explorer_surfaces_a_failed_tool_filter(monkeypatch):
    """The handler is where the flag becomes visible: without this the picker
    shows `tool` as selected over a list of every trace in the window."""
    import asyncio

    import admin.observability_explorer_handlers as handlers
    from admin.screens import observability as obs

    captured: Dict[str, Any] = {}

    class _FakeTemplates:
        def TemplateResponse(self, _request, _name, ctx):
            captured.update(ctx)
            return "RENDERED"

    monkeypatch.setattr(handlers, "templates", _FakeTemplates())
    monkeypatch.setattr(
        obs, "load_traces_list",
        lambda **_kw: {"traces": [{"id": TRACE_A}, {"id": TRACE_B}], "notice": None},
    )
    monkeypatch.setattr(obs, "list_tool_names_from_db", lambda *_a, **_kw: ["lookup_order"])
    monkeypatch.setattr("core.lmnr_clickhouse.query", make_ch_query(raises=True))

    asyncio.run(handlers.observability_explorer(
        request=None, env="prod", hours=24, status=None, search=None,
        tool="lookup_order", channel_slug=None, agent_kind=None, outcome=None,
        group="none", background=0, ctx={"email": "a@b.c"},
    ))

    assert captured["tool_filter_failed"] is True
    assert captured["tool_filter_notice"], "a filter that failed open must reach the template"
    assert "lookup_order" in captured["tool_filter_notice"]
    assert "NOT be applied" in captured["tool_filter_notice"]


def test_the_explorer_marks_a_working_tool_filter_as_applied(monkeypatch):
    import asyncio

    import admin.observability_explorer_handlers as handlers
    from admin.screens import observability as obs

    captured: Dict[str, Any] = {}

    class _FakeTemplates:
        def TemplateResponse(self, _request, _name, ctx):
            captured.update(ctx)
            return "RENDERED"

    monkeypatch.setattr(handlers, "templates", _FakeTemplates())
    monkeypatch.setattr(
        obs, "load_traces_list",
        lambda **_kw: {"traces": [{"id": TRACE_A}, {"id": TRACE_B}], "notice": None},
    )
    monkeypatch.setattr(obs, "list_tool_names_from_db", lambda *_a, **_kw: ["lookup_order"])
    monkeypatch.setattr("core.lmnr_clickhouse.query",
                        make_ch_query(tool_matches=[TRACE_A]))

    asyncio.run(handlers.observability_explorer(
        request=None, env="prod", hours=24, status=None, search=None,
        tool="lookup_order", channel_slug=None, agent_kind=None, outcome=None,
        group="none", background=0, ctx={"email": "a@b.c"},
    ))

    assert captured["tool_filter_failed"] is False
    assert captured["tool_filter_notice"] is None
    assert [t["id"] for t in captured["traces"]] == [TRACE_A]


def test_the_explorer_template_flags_an_unapplied_tool_filter(jinja_env):
    tmpl = jinja_env.get_template("observability_explorer.html")
    ctx = dict(
        request=None, ctx={"email": "a@b.c"}, active="observability",
        current_env="prod", groups=[], total=0, traces=[], tool_names=["lookup_order"],
        channels=[], agent_options=[], agent_kinds=[], hours_options=((24, "Last 24 hours"),),
        outcome_options=(("", "Any outcome"),), past_hours=24, search="", status="",
        outcome="", tool="lookup_order", channel_slug="", agent_kind="",
        group_by_session=False, show_background=False, more_filters_active=True,
        list_unavailable=False, list_detail=None, tool_filter_notice=None,
    )
    flag = "filter could not be applied"
    assert flag not in tmpl.render(**ctx, tool_filter_failed=False)
    assert flag in tmpl.render(**ctx, tool_filter_failed=True)
