"""LLM span I/O comes from the content-addressed store, not `spans.input/output`.

Measured on the live stack: `spans.input` and `spans.output` are **empty for
100% of LLM spans** (0 of 18,051 over 7 days). The messages live in
`deduped_content`, reachable only by array-joining the span's
`input_message_hashes` / `output_message_hashes` against it (12,825/12,825 hash
refs resolve — no misses). Tool / DEFAULT spans DO keep their payload inline.

So a reader that only selects the two columns renders **blank model I/O** for
every LLM span — which is exactly what the span-detail and transcript panes did
in production, silently, with no test covering it. These tests pin the join.

`deduped_content` is the live table. `llm_messages`, `deduped_content_dict` and
`llm_messages_dict` are all 0 rows; a reader built against those would also look
correct and return nothing.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from tests.ch_fake import ch_content_rows, make_ch_query


# --------------------------------------------------------------------------- #
# Row builders — the two span shapes that matter
# --------------------------------------------------------------------------- #
def _llm_span_io_row(span_id: str, **over):
    """A `_SPAN_IO_SQL` row for an LLM span: `input`/`output` EMPTY, as measured."""
    row = {
        "span_id": span_id,
        "name": "ChatGoogleGenerativeAI.chat",
        "span_type": 1,
        "start_time": "2026-08-05 10:00:00.000000000",
        "input": "",
        "output": "",
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "has_error": 0,
        "waj_session_id": "",
        "waj_env": "",
    }
    row.update(over)
    return row


def _tool_span_io_row(span_id: str, name: str, **over):
    """A `_SPAN_IO_SQL` row for a tool span: payload IS inline."""
    row = _llm_span_io_row(span_id)
    row.update({
        "name": f"{name}.tool",
        "span_type": 0,
        "input": '{"day": "tue"}',
        "output": '["14:00"]',
    })
    row.update(over)
    return row


LLM_SPAN = "00000000-0000-0000-afc1-b3f70b5cfd5c"
TOOL_SPAN = "00000000-0000-0000-9ab2-c1d4e5f60718"


# --------------------------------------------------------------------------- #
# _clickhouse_spans backfill
# --------------------------------------------------------------------------- #
def test_llm_span_io_is_backfilled_from_deduped_content():
    """The defect: an LLM span's `input`/`output` are empty, so the panes showed
    nothing. They must be filled from the content store."""
    from admin.screens import observability as obs

    content = ch_content_rows(
        LLM_SPAN,
        inputs=[
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "book me a slot"},
        ],
        outputs=[{"role": "assistant", "content": "Booked you at 14:00"}],
    )

    with patch("core.lmnr_clickhouse.query",
               make_ch_query(span_io=[_llm_span_io_row(LLM_SPAN)], content=content)):
        spans = obs._clickhouse_spans("T1")

    assert len(spans) == 1
    parsed_in = json.loads(spans[0]["input"])
    assert [m["role"] for m in parsed_in] == ["system", "user"]
    assert parsed_in[1]["content"] == "book me a slot"
    parsed_out = json.loads(spans[0]["output"])
    assert parsed_out[0]["content"] == "Booked you at 14:00"


def test_message_order_is_preserved():
    """The ARRAY JOIN pairs each hash with `arrayEnumerate`, and the query orders
    by that index — otherwise the reassembled conversation is shuffled and the
    transcript picks the wrong "last user message"."""
    from admin.screens import observability as obs

    content = ch_content_rows(
        LLM_SPAN,
        inputs=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "mid"},
            {"role": "user", "content": "second"},
        ],
    )
    with patch("core.lmnr_clickhouse.query",
               make_ch_query(span_io=[_llm_span_io_row(LLM_SPAN)], content=content)):
        spans = obs._clickhouse_spans("T1")

    contents = [m["content"] for m in json.loads(spans[0]["input"])]
    assert contents == ["sys", "first", "mid", "second"]


def test_inline_tool_payload_is_never_overwritten():
    """Tool spans keep their I/O inline. The backfill must not clobber it — a
    tool span carries no message hashes, so a naive overwrite would blank it."""
    from admin.screens import observability as obs

    rows = [_tool_span_io_row(TOOL_SPAN, "find_slots"), _llm_span_io_row(LLM_SPAN)]
    content = ch_content_rows(
        LLM_SPAN,
        # Deliberately claim content for the TOOL span too: if the mapping keyed
        # on anything other than span_id, this would land on the wrong row.
        inputs=[{"role": "user", "content": "hi"}],
    ) + ch_content_rows(TOOL_SPAN, inputs=["WRONG"])

    with patch("core.lmnr_clickhouse.query",
               make_ch_query(span_io=rows, content=content)):
        spans = {s["span_id"]: s for s in obs._clickhouse_spans("T1")}

    assert spans[TOOL_SPAN]["input"] == '{"day": "tue"}'
    assert spans[TOOL_SPAN]["output"] == '["14:00"]'
    assert "WRONG" not in spans[TOOL_SPAN]["input"]
    assert "hi" in spans[LLM_SPAN]["input"]


def test_content_store_outage_leaves_spans_usable():
    """A failing content query must degrade to empty I/O, not lose the spans."""
    from admin.screens import observability as obs

    def _query(sql, params=None, **_kw):
        if "deduped_content" in sql:
            raise RuntimeError("content query blew up")
        if "toInt16(span_type) AS span_type" in sql:
            return [_llm_span_io_row(LLM_SPAN)]
        return []

    with patch("core.lmnr_clickhouse.query", _query):
        spans = obs._clickhouse_spans("T1")

    assert [s["span_id"] for s in spans] == [LLM_SPAN]
    assert spans[0]["input"] == ""


# --------------------------------------------------------------------------- #
# The SQL itself
# --------------------------------------------------------------------------- #
def test_content_sql_targets_the_live_table_and_orders_messages():
    from admin.screens.observability import _SPAN_CONTENT_SQL

    sql = _SPAN_CONTENT_SQL
    assert "deduped_content" in sql
    # The empty look-alike tables must not be used — a query against them
    # returns cleanly with zero rows, which reads as "no content captured".
    assert "llm_messages" not in sql
    assert "deduped_content_dict" not in sql
    # Both sides, order-preserving, and joined per project.
    assert "input_message_hashes" in sql and "output_message_hashes" in sql
    assert "arrayEnumerate" in sql
    assert "d.project_id = x.project_id" in sql
    # The trace id travels as a param, never inlined.
    assert "{trace_id:String}" in sql


def test_span_list_sql_does_not_trust_the_status_column():
    """`spans.status` is '' on 27,687 of 27,694 rows, so it can't be the error
    signal; the OTel `events` array is."""
    from admin.screens.observability import _SPAN_LIST_SQL

    assert "arrayExists(x -> x.2 IN ('exception', 'error'), events)" in _SPAN_LIST_SQL
    assert "status AS" not in _SPAN_LIST_SQL


# --------------------------------------------------------------------------- #
# End-to-end: the pane the defect was visible in
# --------------------------------------------------------------------------- #
def test_transcript_renders_an_llm_turn_with_no_inline_io():
    """The user-visible symptom: a trace whose only span is an LLM call rendered
    an empty transcript, because the reader looked at the empty columns."""
    from admin.screens import observability as obs

    content = ch_content_rows(
        LLM_SPAN,
        inputs=[{"role": "user", "content": "what are my bookings?"}],
        outputs=[{"role": "assistant", "content": "You have two today."}],
    )
    with patch("core.lmnr_clickhouse.query",
               make_ch_query(span_io=[_llm_span_io_row(LLM_SPAN)], content=content)):
        with patch.object(obs, "_persisted_conversation", return_value=[]):
            data = obs.load_transcript_json("T1")

    roles = [t["role"] for t in data["turns"]]
    assert roles == ["user", "assistant"]
    assert "what are my bookings?" in data["turns"][0]["content"]
    assert data["turns"][1]["content"] == "You have two today."


def test_span_detail_renders_llm_io_from_the_content_store():
    """Same for the Spans tab's LLM I/O block."""
    from admin.screens import observability as obs
    from tests.ch_fake import ch_span_row

    content = ch_content_rows(
        LLM_SPAN,
        inputs=[{"role": "user", "content": "hello there"}],
        outputs=[{"role": "assistant", "content": "general kenobi"}],
    )
    spans = [ch_span_row(LLM_SPAN, name="ChatGoogleGenerativeAI.chat", span_type=1,
                         attributes={"gen_ai.request.model": "gemini-3-flash"})]

    with patch("core.lmnr_clickhouse.query",
               make_ch_query(spans=spans, span_io=[_llm_span_io_row(LLM_SPAN)],
                             content=content)):
        out = obs.load_span_detail_json("T1", LLM_SPAN)

    span = out["span"]
    assert span["type"] == "LLM"
    assert span["model"] == "gemini-3-flash"
    assert "hello there" in span["llm_io"]["input"]
    assert "general kenobi" in span["llm_io"]["output"]
