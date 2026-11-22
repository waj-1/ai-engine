"""Observability explorer trace list must filter a background agent's flood at
the SOURCE (ClickHouse), not client-side.

Background: a one-shot background endpoint emitted ~200 traces/hour. The
explorer fetches the newest N traces and (previously) filtered client-side,
so the newest page was ~100% background traces and genuine chat sessions fell off it.
The fix reads the per-trace aggregate table (``traces_replacing``) and excludes
non-conversational kinds + scopes by ``waj.env`` IN THE QUERY, so chat sessions
surface regardless of background volume.
"""
from __future__ import annotations

import sys
from pathlib import Path

AI_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


import pytest


@pytest.fixture(autouse=True)
def _a_background_kind(monkeypatch):
    """A fictional background agent kind (a plugin names its own through
    agent_catalog.background_kinds())."""
    from admin.screens import observability

    monkeypatch.setattr(observability, "_BACKGROUND_AGENT_KINDS", ("sample_background",))


def test_a_build_with_no_background_kind_writes_valid_sql(monkeypatch):
    """`NOT IN ()` is not valid ClickHouse: with no background kind the
    clause is left out instead of breaking every trace list."""
    from admin.screens import observability, overview

    monkeypatch.setattr(observability, "_BACKGROUND_AGENT_KINDS", ())
    sql, _ = observability._trace_list_sql(env="prod", include_background=False, status=None)
    assert "IN ()" not in sql
    sql, _ = overview.outcome_sql("prod")
    assert "IN ()" not in sql


def test_sql_excludes_background_kinds_and_scopes_env_by_default():
    from admin.screens.observability import _trace_list_sql

    sql, params = _trace_list_sql(env="prod", include_background=False, status=None)

    # Background-kind traces are excluded at the source.
    assert "sample_background" in sql
    # Untagged bare-LLM flood is excluded by its top-span signature too.
    assert "gemini.generate_content" in sql
    # Env scoping is parameterised (no string interpolation of user input).
    assert params.get("env") == "prod"
    assert "waj.env" in sql


def test_sql_includes_background_when_requested():
    from admin.screens.observability import _trace_list_sql

    sql, _params = _trace_list_sql(env="prod", include_background=True, status=None)

    # When the operator opts in, the exclusion predicates are gone.
    assert "sample_background" not in sql
    assert "gemini.generate_content" not in sql


def test_sql_excludes_voice_metric_point_spans_by_default():
    from admin.screens.observability import _trace_list_sql

    sql, _params = _trace_list_sql(env="prod", include_background=False, status=None)

    # voice.live.* point spans are ~0ms metric markers, not conversations —
    # they must be excluded at the source or a voice-heavy window shows a
    # wall of sub-ms "traces" and buries the real chats.
    assert "startsWith(top_span_name, 'voice.live.')" in sql


def test_sql_keeps_voice_metric_spans_when_background_requested():
    from admin.screens.observability import _trace_list_sql

    sql, _params = _trace_list_sql(env="prod", include_background=True, status=None)

    assert "voice.live." not in sql


def test_sql_derives_trace_error_state_from_span_events():
    """`traces_replacing.status` is '' on EVERY row the live stack writes (same
    as `spans.status` — see the module docstring), so reading it mapped every
    trace to success, the Error filter to the empty set, and the Dashboard's
    error card to a permanent all-clear. Trace error state is the span
    predicate aggregated per trace, joined in ONE query over the window's
    spans — never an N+1 per listed row, never the dead column."""
    from admin.screens.observability import _trace_list_sql

    sql, _params = _trace_list_sql(env=None, include_background=False, status=None)

    # The events aggregate is joined and selected…
    assert "arrayExists(x -> x.2 IN ('exception', 'error'), events)" in sql
    assert "AS has_error" in sql
    assert "LEFT JOIN" in sql
    assert "GROUP BY trace_id" in sql
    # …and the dead column is not read at all.
    assert "toString(status)" not in sql


def test_sql_error_and_success_filters_use_the_events_aggregate():
    """The list filter must run on the joined aggregate: filtering the dead
    column made Error return nothing and Success return everything."""
    from admin.screens.observability import _trace_list_sql

    err_sql, _ = _trace_list_sql(env=None, include_background=False, status="error")
    ok_sql, _ = _trace_list_sql(env=None, include_background=False, status="success")

    assert "err.has_error = 1" in err_sql
    assert "err.has_error != 1" in ok_sql
    assert "status = 'error'" not in err_sql
    assert "status != 'error'" not in ok_sql


def test_sql_filters_the_final_scan_in_a_subquery_before_the_join():
    """`traces_replacing FINAL` joined DIRECTLY to the span aggregate, with the
    trace filters in the outer WHERE, fails on the deployed ClickHouse
    (26.5.6.64) with `Code: 10 Not found column and(greater(__table1.start_time
    …` — the analyzer pushes the WHERE into the FINAL scan and loses the
    combined filter column. Every unfiltered trace-list load 500'd from #147
    (2026-08-26) on, and the explorer showed "Telemetry unavailable". The FINAL
    scan and every trace-level filter must live in their own subquery; only the
    `err.*` predicates may sit on the join."""
    from admin.screens.observability import _trace_list_sql

    for status in (None, "error", "success"):
        sql, _ = _trace_list_sql(
            env="prod", include_background=False, status=status, search="x"
        )
        assert "FROM (SELECT * FROM traces_replacing FINAL WHERE " in sql
        inner, _, outer = sql.partition(") AS t LEFT JOIN (")
        assert outer, sql
        # Trace-level filters are applied inside the FINAL scan…
        for fragment in (
            "start_time > now() - toIntervalHour({hours:UInt32})",
            "'waj.env') = {env:String}",
            "sample_background",
            "voice.live.",
            "{search:String}",
            "lmnr_diagnose_test_span",
        ):
            assert fragment in inner, fragment
        # …and the outer WHERE carries nothing but the joined aggregate.
        _, _, outer_where = outer.partition(") AS err ON err.trace_id = t.id ")
        outer_where = outer_where.split("ORDER BY")[0]
        assert "metadata" not in outer_where
        assert "top_span_name" not in outer_where
        if status is None:
            assert "WHERE" not in outer_where


def test_load_clickhouse_traces_pushes_the_error_filter_down(monkeypatch):
    import admin.screens.observability as obs

    captured: dict = {}

    def _fake_query(sql, params=None, **_kw):
        captured["sql"] = sql
        return []

    monkeypatch.setattr(obs, "_ch_trace_query", _fake_query)
    obs.load_clickhouse_traces(
        past_hours=24, page_size=100, env=None,
        include_background=False, status="error",
    )
    assert "err.has_error = 1" in captured["sql"]


def test_trace_one_sql_does_not_trust_the_status_column():
    """`ch_get_trace` feeds the explorer header, Transcript and Details tabs —
    the same dead-column rule applies to the single-trace lookup."""
    from admin.screens.observability import _TRACE_ONE_SQL

    assert "toString(status)" not in _TRACE_ONE_SQL
    assert "arrayExists(x -> x.2 IN ('exception', 'error'), events)" in _TRACE_ONE_SQL
    assert "AS has_error" in _TRACE_ONE_SQL


def test_ch_get_trace_maps_span_error_aggregate_to_status(monkeypatch):
    import admin.screens.observability as obs

    # ClickHouse's JSON interface may quote the aggregate — both shapes count.
    row = {"id": "T1", "has_error": "1", "topSpanName": "agent.invoke"}
    monkeypatch.setattr(obs, "_ch_trace_query", lambda _sql, _params=None: [row])
    assert obs.ch_get_trace("T1")["status"] == "error"

    row["has_error"] = 0
    assert obs.ch_get_trace("T1")["status"] == "success"


def test_live_monitor_inherits_the_source_side_exclusions(monkeypatch):
    """There is no client-side twin of these exclusions any more.

    `_drop_voice_metric_traces` existed to re-apply the voice-metric narrow on
    the list path that fetched from the (now deleted) dashboard API, which had no
    server-side predicate. Every path reads `_trace_list_sql` now, so the
    exclusion is applied once, in SQL — including for the Live Monitor, which is
    what this pins. A reintroduced in-memory twin would just be a second place
    for the two to drift apart.
    """
    import admin.screens.observability as obs

    assert not hasattr(obs, "_drop_voice_metric_traces")

    seen = []

    def _fake_query(sql, params=None, **_kw):
        seen.append(sql)
        return []

    monkeypatch.setattr(obs, "_ch_trace_query", _fake_query)
    obs.load_live_insights(past_hours=1)

    trace_sql = [s for s in seen
                 if "FROM traces_replacing FINAL" in s
                 and "top_span_name AS topSpanName" in s]
    assert trace_sql, "the Live Monitor did not read the trace list at all"
    for sql in trace_sql:
        assert "startsWith(top_span_name, 'voice.live.')" in sql
        assert "sample_background" in sql
        assert "lmnr_diagnose_test_span" in sql


def test_map_ch_trace_row_to_template_shape():
    from admin.screens.observability import _map_ch_trace_row

    row = {
        "id": "abc-123",
        # ClickHouse renders DateTime64(9) with a space separator and nine
        # fractional digits, and quotes 64-bit ints — both exactly as they
        # arrive over the HTTP JSON interface.
        "startTime": "2026-06-16 20:00:00.000000000",
        "endTime": "2026-06-16 20:00:01.500000000",
        "inputTokens": "100",
        "outputTokens": "50",
        # Events-aggregated by the join in `_trace_list_sql` — the `status`
        # COLUMN is '' on every live row and no longer selected.
        "has_error": 1,
        "topSpanName": "sample_agent.agent",
        "metadata": '{"waj.env":"prod","waj.agent_kind":"sample_agent"}',
        "sessionId": "sess-9",
    }

    mapped = _map_ch_trace_row(row)

    assert mapped["id"] == "abc-123"
    # Normalised to ISO-8601: `datetime.fromisoformat` (which the template
    # helpers use) rejects a 9-digit fraction, so it truncates to microseconds.
    assert mapped["startTime"] == "2026-06-16T20:00:00.000000Z"
    assert mapped["endTime"] == "2026-06-16T20:00:01.500000Z"
    # Coerced to int — the template ADDS these two, and strings would render
    # "10050" instead of 150.
    assert mapped["inputTokens"] == 100
    assert mapped["outputTokens"] == 50
    assert mapped["status"] == "error"
    assert mapped["topSpanName"] == "sample_agent.agent"
    # metadata must survive so _annotate_trace_channel_meta can read waj.*
    assert mapped["metadata"] == '{"waj.env":"prod","waj.agent_kind":"sample_agent"}'
    assert mapped["sessionId"] == "sess-9"


def test_map_ch_trace_row_status_defaults_to_success():
    from admin.screens.observability import _map_ch_trace_row

    # ClickHouse quotes UInt8 in some client modes too — "0"/absent both mean
    # no span carried an error event.
    mapped = _map_ch_trace_row({"id": "x", "has_error": "0", "topSpanName": "sample_other.agent"})
    assert mapped["status"] == "success"
    mapped = _map_ch_trace_row({"id": "x", "topSpanName": "sample_other.agent"})
    assert mapped["status"] == "success"


def test_map_ch_trace_row_ignores_the_dead_status_column():
    """The regression that made every trace green: `traces_replacing.status`
    is '' on every row the live stack writes, so status must come from the
    events-aggregated `has_error` and NEVER from a `status` key. A row that
    (impossibly) carried `status='error'` with no span error events must map
    to success — anything else means the mapper is reading the dead column
    again."""
    from admin.screens.observability import _map_ch_trace_row

    mapped = _map_ch_trace_row({"id": "x", "status": "error", "has_error": 0})
    assert mapped["status"] == "success"


def test_load_clickhouse_traces_filters_background_via_query(monkeypatch):
    import admin.screens.observability as obs

    captured = {}

    def _fake_query(sql, params=None, **kw):
        captured["sql"] = sql
        captured["params"] = params or {}
        # ClickHouse already applied the WHERE clause; return only chat traces.
        return [
            {
                "id": "t1",
                "startTime": "2026-06-16 20:00:00.000000000",
                "endTime": "2026-06-16 20:00:01.000000000",
                "inputTokens": 10,
                "outputTokens": 5,
                "has_error": 0,
                "topSpanName": "sample_agent.agent",
                "metadata": '{"waj.env":"prod","waj.agent_kind":"sample_agent"}',
                "sessionId": "s1",
            }
        ]

    monkeypatch.setattr(obs, "_ch_trace_query", _fake_query)

    rows = obs.load_clickhouse_traces(
        past_hours=24, page_size=100, env="prod", include_background=False, status=None
    )

    assert rows is not None
    assert len(rows) == 1
    assert rows[0]["id"] == "t1"
    assert rows[0]["status"] == "success"
    # The query was env-scoped and time-bounded.
    assert captured["params"].get("env") == "prod"
    assert captured["params"].get("hours") == 24


def test_tool_name_regex_matches_live_analytics(monkeypatch):
    """The Live Monitor's tool rollup and the Dashboard chart grid's must strip
    the `.tool` suffix with the SAME regex literal.

    They are two separately-authored SQL strings over the same span names, and
    the escaping is easy to get wrong in a way source review won't catch: a
    Python `"...'\\\\.tool$'..."` and a `\"\"\"...'\\.tool$'...\"\"\"` look alike but reach
    ClickHouse as different regexes. Compare what actually goes over the wire.
    """
    import re

    import admin.screens.observability as obs
    from admin.screens.live_analytics import _TOOLS_SQL

    sent = []
    monkeypatch.setattr(obs, "_ch_trace_query",
                        lambda sql, params=None, **kw: sent.append(sql) or [])
    obs._top_tools(past_hours=1, env=None)

    pattern = r"replaceRegexpOne\(s\.name, '([^']*)'"
    theirs = re.search(pattern, _TOOLS_SQL).group(1)
    ours = re.search(pattern, sent[0]).group(1)
    assert ours == theirs, f"tool-name regex drifted: {ours!r} != {theirs!r}"
