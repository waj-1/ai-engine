"""Tests for the three-pane observability explorer.

Coverage:
* HTML shell route renders the explorer template.
* JSON endpoints return the expected shapes (with ClickHouse stubbed).
* `_enrich_span` extracts `tool_io` defensively from a few different
  attribute conventions OpenLLMetry has used for LangChain tool spans.
* `filter_traces_by_tool` narrows the trace list correctly — and, when the
  query fails, does NOT narrow it to nothing.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.ch_fake import ch_span_row, ch_trace_row, make_ch_query

TRACE_WITH = "aaaaaaaa-0000-0000-0000-000000000001"
TRACE_WITHOUT = "aaaaaaaa-0000-0000-0000-000000000002"


# ---------------------------------------------------------------------------
# tool_io extraction
# ---------------------------------------------------------------------------

def test_enrich_span_extracts_tool_io_gen_ai_keys() -> None:
    """The canonical OpenLLMetry tool-span shape."""
    from admin.screens.observability import _enrich_span

    span = {
        "spanId": "s1",
        "name": "lookup_order",
        "spanType": "TOOL",
        "duration": 0.123,
        "attributes": {
            "gen_ai.tool.name": "lookup_order",
            "gen_ai.tool.input": '{"order_id": "abc"}',
            "gen_ai.tool.output": '{"name": "Layla"}',
        },
    }
    enriched = _enrich_span(span)
    assert enriched["tool_io"]["name"] == "lookup_order"
    assert "order_id" in enriched["tool_io"]["input"]
    assert "Layla" in enriched["tool_io"]["output"]
    assert enriched["duration_ms"] == 123


def test_enrich_span_extracts_tool_io_traceloop_kind() -> None:
    """When the span itself is typed DEFAULT but the traceloop.span.kind
    attribute marks it as a tool, we still pick up the I/O."""
    from admin.screens.observability import _enrich_span

    span = {
        "spanId": "s2",
        "name": "create_booking",
        "spanType": "DEFAULT",
        "attributes": {
            "traceloop.span.kind": "tool",
            "input.value": "create_booking({'slot': '14:00'})",
            "output.value": "booking_id=42",
        },
    }
    enriched = _enrich_span(span)
    assert enriched["tool_io"]["name"]  # span name fallback
    assert "slot" in enriched["tool_io"]["input"]
    assert "42" in enriched["tool_io"]["output"]


def test_enrich_span_non_tool_returns_empty_tool_io() -> None:
    """LLM spans shouldn't get a tool_io block populated — that would
    confuse the template into rendering a tool section for every LLM
    call."""
    from admin.screens.observability import _enrich_span

    span = {
        "spanId": "s3",
        "name": "ChatGoogle",
        "spanType": "LLM",
        "attributes": {
            "gen_ai.system": "google",
            "gen_ai.prompt.0.content": "hi",
        },
    }
    enriched = _enrich_span(span)
    assert enriched["tool_io"]["name"] is None
    assert enriched["tool_io"]["input"] is None
    assert enriched["tool_io"]["output"] is None


# ---------------------------------------------------------------------------
# filter_traces_by_tool
# ---------------------------------------------------------------------------

def test_filter_traces_by_tool_narrows_results() -> None:
    """One ClickHouse query answers for the whole page: the traces it names
    survive, the rest are dropped."""
    from admin.screens import observability as obs

    traces = [{"id": TRACE_WITH}, {"id": TRACE_WITHOUT}]
    calls: list = []
    with patch("core.lmnr_clickhouse.query",
               make_ch_query(tool_matches=[TRACE_WITH], calls=calls)):
        out = obs.filter_traces_by_tool(traces, "lookup_order")

    assert [t["id"] for t in out] == [TRACE_WITH]
    # One query for the page, not one per trace.
    assert len(calls) == 1
    # The tool name is parameterised, never concatenated into the SQL.
    assert calls[0]["params"] == {"tool": "lookup_order"}
    assert "lookup_order" not in calls[0]["sql"]


def test_filter_traces_by_tool_empty_name_passthrough() -> None:
    from admin.screens import observability as obs

    traces = [{"id": "A"}, {"id": "B"}]
    out = obs.filter_traces_by_tool(traces, "")
    assert out == traces


def test_filter_traces_by_tool_degrades_unnarrowed_not_empty() -> None:
    """When the query can't run, the list comes back UNNARROWED.

    Returning `[]` reads as "no trace called this tool" — a wrong answer the
    operator can't tell from a real one. Unnarrowed is visibly wrong instead.
    """
    from admin.screens import observability as obs

    traces = [{"id": TRACE_WITH}, {"id": TRACE_WITHOUT}]
    with patch("core.lmnr_clickhouse.query", make_ch_query(raises=True)):
        out = obs.filter_traces_by_tool(traces, "lookup_order")

    assert out == traces


def test_filter_traces_by_tool_only_inlines_validated_uuids() -> None:
    """Trace ids are inlined into the IN-list, so anything that isn't a
    canonical UUID must be dropped before it reaches the SQL — that validation
    is what makes the inlining safe."""
    from admin.screens import observability as obs

    traces = [{"id": TRACE_WITH}, {"id": "'; DROP TABLE spans; --"}]
    calls: list = []
    with patch("core.lmnr_clickhouse.query",
               make_ch_query(tool_matches=[TRACE_WITH], calls=calls)):
        out = obs.filter_traces_by_tool(traces, "lookup_order")

    assert [t["id"] for t in out] == [TRACE_WITH]
    assert "DROP TABLE" not in calls[0]["sql"]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

def _ensure_explorer_loaded() -> None:
    """Importing the sidecar module triggers route registration. main.py
    already does this on app boot, but importing here makes the
    test-suite resilient to any future refactor that lazy-loads it."""
    from admin import observability_explorer_handlers  # noqa: F401


def test_explorer_shell_renders(client) -> None:
    _ensure_explorer_loaded()
    r = client.get("/admin/observability/explorer")
    assert r.status_code == 200, r.text[:200]
    # Partner-facing name (portal revamp): "Conversations", not "Traces".
    assert "Conversations" in r.text
    # Two panes: conversation list + conversation viewer.
    assert 'id="cv-shell"' in r.text
    assert 'id="ox-trace-list"' in r.text and 'id="cv-view"' in r.text
    # Tabs: Conversation · Call audio · Technical.
    for label in (">Conversation<", ">Call audio<", ">Technical<"):
        assert label in r.text


# Span ids are OTel 8-byte values zero-left-padded into UUIDs — only the last 16
# hex chars carry information. Use realistic ones so nothing here quietly relies
# on short ids.
ROOT_SPAN = "00000000-0000-0000-afc1-b3f70b5cfd5c"
TOOL_SPAN = "00000000-0000-0000-9ab2-c1d4e5f60718"


def test_explorer_spans_json_shape(client) -> None:
    """The middle-pane endpoint returns {trace, tree, notice}."""
    _ensure_explorer_loaded()

    trace_row = ch_trace_row(
        "T1", session_id="sess-1", in_tokens=10, out_tokens=20,
        top_span_name="agent.invoke",
    )
    spans = [
        ch_span_row(ROOT_SPAN, name="agent.invoke", span_type=0, duration=1.0),
        ch_span_row(
            TOOL_SPAN, name="lookup_order", span_type=6,
            parent_span_id=ROOT_SPAN, duration=0.3,
            start="2026-05-20 10:00:00.200000000",
            end="2026-05-20 10:00:00.500000000",
            attributes={
                "gen_ai.tool.name": "lookup_order",
                "gen_ai.tool.input": '{"q": "ahmed"}',
                "gen_ai.tool.output": '{"ok": true}',
            },
        ),
    ]

    with patch("core.lmnr_clickhouse.query",
               make_ch_query(trace_row=trace_row, spans=spans)):
        r = client.get("/admin/observability/explorer/trace/T1/spans.json")
    assert r.status_code == 200, r.text[:200]
    payload = r.json()
    assert payload["trace"]["id"] == "T1"
    assert payload["trace"]["duration_ms"] == 1000
    # `traces_replacing` has no top_span_type column — it comes off the root span.
    assert payload["trace"]["top_span_type"] == "DEFAULT"
    assert payload["tree"], "expected at least one root span node"
    root = payload["tree"][0]
    assert root["span_id"] == ROOT_SPAN
    assert root["children"], "expected child span under root"
    child = root["children"][0]
    assert child["span_id"] == TOOL_SPAN
    assert child["tool_name"] == "lookup_order"
    assert child["type"] == "TOOL"


def test_explorer_spans_json_notice_when_clickhouse_down(client) -> None:
    """ClickHouse is the only source, so an outage is reported, not faked."""
    _ensure_explorer_loaded()

    tid = "3f7ef660-4ecf-45f8-a643-759468a18085"
    with patch("core.lmnr_clickhouse.query", make_ch_query(raises=True)):
        r = client.get(f"/admin/observability/explorer/trace/{tid}/spans.json")
    payload = r.json()
    assert payload["trace"] is None
    assert payload["tree"] == []
    assert payload["unavailable"] is True
    assert payload["found"] is False
    # Technical view only — engineering wording is allowed there.
    assert "ClickHouse" in (payload.get("notice") or "")


def test_explorer_bogus_trace_is_not_found_not_a_fake_record(client) -> None:
    """Audit P0 #4: a bad/expired link must read "not found" on every pane —
    never a zero-filled record and never "unavailable" (ClickHouse rejecting a
    malformed id is not an outage)."""
    _ensure_explorer_loaded()

    for raises in (False, True):
        with patch("core.lmnr_clickhouse.query", make_ch_query(raises=raises)):
            tx = client.get("/admin/observability/explorer/trace/abc123/transcript.json").json()
            ov = client.get("/admin/observability/explorer/trace/abc123/overview.json").json()
            sp = client.get("/admin/observability/explorer/trace/abc123/spans.json").json()
        for payload in (tx, ov, sp):
            assert payload["found"] is False
            assert payload["unavailable"] is False
        assert ov["trace"] is None, "Details must not render a zero-filled trace"
        assert "couldn't find" in tx["notice"]


def test_explorer_missing_uuid_trace_is_not_found_when_clickhouse_answers(client) -> None:
    _ensure_explorer_loaded()
    tid = "11111111-1111-4111-8111-111111111111"
    with patch("core.lmnr_clickhouse.query", make_ch_query()):
        tx = client.get(f"/admin/observability/explorer/trace/{tid}/transcript.json").json()
    assert tx["found"] is False and tx["unavailable"] is False
    with patch("core.lmnr_clickhouse.query", make_ch_query(raises=True)):
        tx = client.get(f"/admin/observability/explorer/trace/{tid}/transcript.json").json()
    assert tx["found"] is False and tx["unavailable"] is True
    assert "temporarily unavailable" in tx["notice"]


def test_explorer_span_detail_json_includes_tool_io(client) -> None:
    """The right-pane endpoint returns the full span dict with tool_io
    populated for tool spans."""
    _ensure_explorer_loaded()

    spans = [
        ch_span_row(
            TOOL_SPAN, name="lookup_order", span_type=6, duration=0.3,
            start="2026-05-20 10:00:00.200000000",
            end="2026-05-20 10:00:00.500000000",
            attributes={
                "gen_ai.tool.name": "lookup_order",
                "gen_ai.tool.input": '{"q": "ahmed"}',
                "gen_ai.tool.output": '{"ok": true}',
                "lmnr.span.path": ["agent", "tool"],
                # The per-message family is huge and llm_io already renders it —
                # it must not be echoed into the attributes table.
                "gen_ai.prompt.0.content": "x" * 50,
                "gen_ai.completion.0.content": "y" * 50,
            },
        ),
    ]

    with patch("core.lmnr_clickhouse.query", make_ch_query(spans=spans)):
        r = client.get(f"/admin/observability/explorer/span/T1/{TOOL_SPAN}.json")
    assert r.status_code == 200, r.text[:200]
    span = r.json()["span"]
    assert span["span_id"] == TOOL_SPAN
    assert span["type"] == "TOOL"
    assert span["tool_io"]["name"] == "lookup_order"
    assert "ahmed" in span["tool_io"]["input"]
    assert "ok" in span["tool_io"]["output"]
    # Attributes must round-trip as JSON-safe (no datetime objects etc).
    assert "gen_ai.tool.name" in span["attributes"]
    assert "gen_ai.prompt.0.content" not in span["attributes"]
    assert "gen_ai.completion.0.content" not in span["attributes"]


def test_explorer_span_detail_derives_error_status_from_events(client) -> None:
    """`spans.status` is empty on ~all rows (7 of 27,694 carry 'error'), so the
    error signal is the OTel `events` array. A span with an exception event must
    read as an error even though the column says nothing."""
    _ensure_explorer_loaded()

    spans = [
        ch_span_row(
            TOOL_SPAN, name="charge_card.tool", span_type=0,
            events=[[1_780_000_000_000_000_000, "exception", "{}"]],
            has_error=1,
        ),
    ]
    with patch("core.lmnr_clickhouse.query", make_ch_query(spans=spans)):
        r = client.get(f"/admin/observability/explorer/span/T1/{TOOL_SPAN}.json")
    span = r.json()["span"]
    assert span["status"] == "error"
    assert span["events"][0]["name"] == "exception"
    # Int64 nanosecond epoch → an ISO string the detail pane can slice.
    assert span["events"][0]["timestamp"].startswith("2026-")


def test_explorer_span_detail_returns_404_shape_when_missing(client) -> None:
    _ensure_explorer_loaded()
    with patch("core.lmnr_clickhouse.query", make_ch_query(spans=[])):
        r = client.get("/admin/observability/explorer/span/T1/missing.json")
    # The endpoint returns 200 with a notice — caller branches on that.
    assert r.status_code == 200
    payload = r.json()
    assert payload["span"] is None
    assert "not found" in (payload.get("notice") or "").lower()


# ---------------------------------------------------------------------------
# build_span_tree — pure, and now module-level (it used to be a staticmethod on
# the deleted Laminar frontend client).
# ---------------------------------------------------------------------------

def test_build_span_tree_nests_by_parent_and_sorts_siblings() -> None:
    from admin.screens.observability import build_span_tree

    a = "00000000-0000-0000-1111-111111111111"
    b = "00000000-0000-0000-2222-222222222222"
    c = "00000000-0000-0000-3333-333333333333"
    spans = [
        {"spanId": c, "parentSpanId": a, "startTime": "2026-05-20T10:00:02Z"},
        {"spanId": a, "parentSpanId": None, "startTime": "2026-05-20T10:00:00Z"},
        {"spanId": b, "parentSpanId": a, "startTime": "2026-05-20T10:00:01Z"},
    ]
    roots = build_span_tree(spans)
    assert [r["spanId"] for r in roots] == [a]
    assert [ch["spanId"] for ch in roots[0]["children"]] == [b, c]
    assert roots[0]["depth"] == 0
    assert all(ch["depth"] == 1 for ch in roots[0]["children"])


def test_build_span_tree_treats_zero_uuid_parent_as_root() -> None:
    """A root span's `parent_span_id` is the zero UUID, not NULL — reading it as
    a real parent id would leave the tree empty."""
    from admin.screens.observability import build_span_tree

    root = "00000000-0000-0000-abcd-abcdabcdabcd"
    spans = [{"spanId": root,
              "parentSpanId": "00000000-0000-0000-0000-000000000000",
              "startTime": "2026-05-20T10:00:00Z"}]
    assert [r["spanId"] for r in build_span_tree(spans)] == [root]


def test_build_span_tree_renders_orphans_at_root() -> None:
    from admin.screens.observability import build_span_tree

    orphan = "00000000-0000-0000-dead-beefdeadbeef"
    spans = [{"spanId": orphan,
              "parentSpanId": "00000000-0000-0000-9999-999999999999",
              "startTime": "2026-05-20T10:00:00Z"}]
    assert [r["spanId"] for r in build_span_tree(spans)] == [orphan]
