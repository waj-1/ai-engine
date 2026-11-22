"""The trace list's "Group by session" toggle.

One conversation = one chat session = many traces (one per turn). The
flat list makes a multi-turn test session read as unrelated rows;
grouping buckets them under a collapsible session header. Session ids
ride the same trace metadata as agent_kind (waj.session_id, stamped by
root_span_attrs), so grouping costs zero extra queries.
"""
from __future__ import annotations

from pathlib import Path

from admin.screens.observability import (
    _annotate_trace_channel_meta,
    group_traces_by_session,
)

TEMPLATE = Path(__file__).resolve().parents[2] / "admin" / "templates" / "observability_explorer.html"


def _trace(tid, sid=None, kind="sales", status="success", start="2026-06-11T10:00:00Z",
           dur=100, tokens=(10, 20)):
    return {
        "id": tid, "session_id": sid, "agent_kind": kind, "channel_slug": None,
        "status": status, "startTime": start, "duration_ms": dur,
        "inputTokens": tokens[0], "outputTokens": tokens[1],
    }


def test_groups_keep_recency_order_and_aggregate():
    # Newest-first list: turn3(s1), turn1(s2), turn2(s1), turn1(s1)
    traces = [
        _trace("t3", "s1", start="2026-06-11T10:30:00Z", dur=300, status="error"),
        _trace("x1", "s2", start="2026-06-11T10:20:00Z", dur=50),
        _trace("t2", "s1", start="2026-06-11T10:10:00Z", dur=200),
        _trace("t1", "s1", start="2026-06-11T10:00:00Z", dur=100),
    ]
    groups = group_traces_by_session(traces)
    assert [g["session_id"] for g in groups] == ["s1", "s2"]
    s1 = groups[0]
    assert s1["turns"] == 3
    assert [t["id"] for t in s1["traces"]] == ["t3", "t2", "t1"]
    assert s1["latest_start"] == "2026-06-11T10:30:00Z"
    assert s1["earliest_start"] == "2026-06-11T10:00:00Z"
    assert s1["duration_ms_total"] == 600
    assert s1["tokens_total"] == 90
    assert s1["has_error"] is True
    assert groups[1]["has_error"] is False


def test_sessionless_traces_stay_visible_as_singletons():
    traces = [_trace("a", None), _trace("b", "s1"), _trace("c", None)]
    groups = group_traces_by_session(traces)
    assert [g["session_id"] for g in groups] == [None, "s1", None]
    assert all(g["turns"] == 1 for g in groups if g["session_id"] is None)


def test_annotate_reads_session_id_from_trace_metadata():
    t = {"metadata": '{"waj.agent_kind": "sales", "waj.session_id": "sess-9"}'}
    _annotate_trace_channel_meta([t])
    assert t["session_id"] == "sess-9"
    # The trace row's own session_id is the fallback when the metadata key is absent.
    t2 = {"metadata": "", "sessionId": "native-1"}
    _annotate_trace_channel_meta([t2])
    assert t2["session_id"] == "native-1"


def test_template_has_toggle_and_grouped_rendering():
    src = TEMPLATE.read_text()
    # Hidden field carries the state so the "one row per reply" toggle
    # round-trips as group=none (it lives under More filters now).
    assert 'name="group"' in src
    assert "One row per agent reply" in src
    # One row per conversation; clicking opens its newest turn.
    assert 'class="cv-row"' in src
    assert "OX.selectTrace('{{ g.newest_trace_id }}')" in src
    # Every turn of the conversation is matchable for deep links.
    assert "data-trace-ids" in src


def test_explorer_route_renders_session_groups(client):
    """?group=session renders collapsible session groups with nested turns,
    and the toggle round-trips as checked."""
    from admin import observability_explorer_handlers  # noqa: F401 — route registration
    from unittest.mock import patch

    from tests.ch_fake import ch_trace_row, make_ch_query

    rows = [
        ch_trace_row("T2", agent_kind="sales", session_id="sess-1",
                     start="2026-06-11 10:10:00.000000000",
                     end="2026-06-11 10:10:01.000000000"),
        ch_trace_row("T1", agent_kind="sales", session_id="sess-1",
                     start="2026-06-11 10:00:00.000000000",
                     end="2026-06-11 10:00:02.000000000"),
        # Legacy turn: no metadata at all, so no session id — must stay visible.
        ch_trace_row("T0", top_span_name="old",
                     start="2026-06-11 09:00:00.000000000",
                     end="2026-06-11 09:00:01.000000000"),
    ]
    ch_query = make_ch_query(traces=rows)

    with patch("core.lmnr_clickhouse.query", ch_query):
        # No ?group param: grouping is the DEFAULT now.
        r = client.get("/admin/observability/explorer")
    assert r.status_code == 200, r.text[:300]
    body = r.text
    # One row per conversation: the session's two turns collapse into ONE
    # row keyed by its newest trace, listing both turns for deep links.
    assert body.count('class="cv-row"') == 2
    assert 'data-trace-id="T2"' in body
    assert 'data-trace-ids="T2 T1"' in body
    assert "2 replies" in body
    # The sessionless legacy trace still renders as its own row.
    assert 'data-trace-id="T0"' in body
    # Grouping is the default; ?group=none gives one row per turn.
    assert 'value="session"' in body
    with patch("core.lmnr_clickhouse.query", ch_query):
        flat = client.get("/admin/observability/explorer?group=none")
    assert flat.text.count('class="cv-row"') == 3
    assert 'data-trace-id="T1"' in flat.text


def test_group_error_dot_lights_from_the_span_derived_error(client):
    """`has_error` on a session header inherits from per-trace `status`, which
    is derived from span error EVENTS (the `traces_replacing.status` column is
    '' on every live row). A turn whose spans errored must light the group dot;
    with the column read instead, every group rendered green forever."""
    from admin import observability_explorer_handlers  # noqa: F401 — route registration
    from unittest.mock import patch

    from tests.ch_fake import ch_trace_row, make_ch_query

    rows = [
        ch_trace_row("T2", agent_kind="sales", session_id="sess-err", has_error=1,
                     start="2026-06-11 10:10:00.000000000",
                     end="2026-06-11 10:10:01.000000000"),
        ch_trace_row("T1", agent_kind="sales", session_id="sess-err",
                     start="2026-06-11 10:00:00.000000000",
                     end="2026-06-11 10:00:02.000000000"),
    ]
    with patch("core.lmnr_clickhouse.query", make_ch_query(traces=rows)):
        r = client.get("/admin/observability/explorer")
    assert r.status_code == 200, r.text[:300]
    # The conversation's outcome inherits the span-derived error of ANY turn.
    assert 'chip chip-err">Needs attention' in r.text
    assert 'chip chip-ok">No problems' not in r.text
