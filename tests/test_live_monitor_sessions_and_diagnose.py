"""Follow-up fixes reported from prod after PR #1265:

1. The **sessions** KPI tile still showed the same number on both env tabs.
   Root cause: `_live_kpis` counted distinct `session_id` in `agent_traces`,
   which has NO env column and no env filter — so it can't differ per env.
   Fix: source an env-scoped distinct-session count from ClickHouse
   (`load_live_insights` → `sessions_24h`), like the other tiles.

2. The trace explorer listed many empty **`lmnr_diagnose_test_span`** traces —
   the telemetry self-test (`/admin/lmnr/diagnose`) emits one such span each
   time it runs. Fix: exclude that top-span name from the trace list
   unconditionally.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

AI_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


# --------------------------------------------------------------------------- #
# Issue 2 — exclude the diagnose self-test span from the trace list
# --------------------------------------------------------------------------- #
def test_trace_list_sql_excludes_diagnose_span_always():
    from admin.screens.observability import _trace_list_sql

    for include_bg in (False, True):
        sql, _ = _trace_list_sql(env="prod", include_background=include_bg, status=None)
        assert "lmnr_diagnose_test_span" in sql, (
            f"diagnose span not excluded (include_background={include_bg})"
        )
        assert "top_span_name NOT IN" in sql


# --------------------------------------------------------------------------- #
# Env SQL clause helper (shared by TTFT + sessions queries)
# --------------------------------------------------------------------------- #
def test_env_sql_clause_prod_strict_on_attributes():
    from admin.screens.observability import _env_sql_clause

    clause, params = _env_sql_clause("prod", "attributes")
    assert "simpleJSONExtractString(attributes, 'waj.env') = {env:String}" in clause
    assert "OR" not in clause  # prod is strict — no untagged admission
    assert params == {"env": "prod"}


def test_env_sql_clause_staging_admits_untagged_on_metadata():
    from admin.screens.observability import _env_sql_clause

    clause, params = _env_sql_clause("staging", "metadata")
    assert "simpleJSONExtractString(metadata, 'waj.env')" in clause
    assert "= ''" in clause  # staging also admits untagged
    assert params == {"env": "staging"}


def test_env_sql_clause_none_is_empty():
    from admin.screens.observability import _env_sql_clause

    assert _env_sql_clause(None, "metadata") == ("", {})


# --------------------------------------------------------------------------- #
# Issue 1 — env-scoped session count in load_live_insights
# --------------------------------------------------------------------------- #
def test_load_live_insights_sessions_count_env_scoped(monkeypatch):
    from admin.screens import observability

    monkeypatch.setattr(observability, "_BACKGROUND_AGENT_KINDS", ("sample_background",))
    from tests.ch_fake import make_ch_query

    calls = []
    with patch("core.lmnr_clickhouse.query",
               make_ch_query(sessions=7, calls=calls)):
        insights = observability.load_live_insights(past_hours=24, env="prod")

    captured = next(
        {"sessions_sql": c["sql"], "sessions_params": c["params"]}
        for c in calls if "uniqExact" in c["sql"]
    )
    assert insights["sessions_24h"] == 7
    # The session count is env-scoped and drops the diagnose self-test span, but
    # counts ALL agent kinds (background kinds included — total session activity).
    sql = captured["sessions_sql"]
    assert "waj.env" in sql
    assert "sample_background" not in sql
    assert "lmnr_diagnose_test_span" in sql
    assert captured["sessions_params"].get("env") == "prod"


def test_load_live_insights_sessions_absent_when_clickhouse_down():
    from admin.screens import observability
    from tests.ch_fake import make_ch_query

    with patch("core.lmnr_clickhouse.query", make_ch_query(raises=True)):
        insights = observability.load_live_insights(past_hours=24, env="prod")

    # No key (or None) → _live_kpis falls back to the agent_traces index.
    assert insights.get("sessions_24h") in (None,)
    # ClickHouse is the ONLY source now, so a total failure is reported as such
    # rather than silently rendering zeros.
    assert insights["lmnr_ok"] is False
    assert insights["notice"]


# --------------------------------------------------------------------------- #
# _live_kpis prefers the env-scoped count over the agent_traces fallback
# --------------------------------------------------------------------------- #
def test_live_kpis_prefers_env_scoped_sessions():
    from admin.router import _live_kpis

    client = MagicMock()  # must NOT be queried when insights supplies the count
    insights = {
        "lmnr_ok": True,
        "sessions_24h": 42,
        "top_tools": [],
        "latency": {"p95_ms": 100},
        "tokens": {"in_24h": 0, "out_24h": 0},
    }
    kpis = _live_kpis(client, "prod", insights)
    assert kpis["sessions_24h"] == 42
    # The agent_traces fallback must NOT be hit (other tables, e.g. agent_models
    # for the token-spend tile, may still be queried).
    tables = [c.args[0] for c in client.table.call_args_list if c.args]
    assert "agent_traces" not in tables


def test_live_kpis_falls_back_to_agent_traces_when_no_count():
    from admin.router import _live_kpis

    client = MagicMock()
    chain = client.table.return_value
    chain.select.return_value = chain
    chain.gte.return_value = chain
    chain.limit.return_value = chain
    chain.execute.return_value = MagicMock(
        data=[{"session_id": "s1"}, {"session_id": "s1"}, {"session_id": "s2"}]
    )
    insights = {"lmnr_ok": False}  # no sessions_24h
    kpis = _live_kpis(client, "prod", insights)
    assert kpis["sessions_24h"] == 2  # distinct s1/s2
    client.table.assert_called_with("agent_traces")
