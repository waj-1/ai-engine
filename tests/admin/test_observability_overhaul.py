"""Tests for the observability overhaul.

Coverage:
* `load_transcript_json` classifies LLM vs tool spans into the right turn
  types (user / assistant / tool_call / tool_response) in chronological order.
* `agent_color` returns a fixed color for every known agent_kind plus a
  neutral fallback for unknown / legacy traces.
* `load_live_insights` aggregates errors, tokens, latency, top tools, and
  active agents from a stubbed trace set.
* The explorer shell renders the agent-card markup (colored chip + left
  border) and the transcript view toggle.

ClickHouse is stubbed via `tests.ch_fake` — no network.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from tests.ch_fake import ch_trace_row, make_ch_query


# ---------------------------------------------------------------------------
# load_transcript_json — built from the ClickHouse `spans` rows: tool spans keep
# their payload inline, LLM spans have theirs backfilled from `deduped_content`.
# ---------------------------------------------------------------------------


def _ch_llm_span(span_id, *, start, messages_in, output_text):
    """A ClickHouse-shaped LLM span row (span_type 1). `messages_in` is a list
    of {role, content}; output is an assistant message array."""
    return {
        "span_id": span_id,
        "name": "ChatGoogleGenerativeAI.chat",
        "span_type": 1,
        "start_time": start,
        "input": json.dumps(messages_in),
        "output": json.dumps([{"role": "assistant",
                               "content": [{"type": "text", "text": output_text}]}]),
    }


def _ch_tool_span(span_id, *, start, name, tool_input, tool_output=""):
    """A ClickHouse-shaped tool span row (DEFAULT type, name '<tool>.tool')."""
    return {
        "span_id": span_id,
        "name": f"{name}.tool",
        "span_type": 0,
        "start_time": start,
        "input": tool_input,
        "output": tool_output,
    }


def test_enrich_span_derives_duration_from_start_end_when_missing():
    """The Laminar dashboard API frequently omits `duration` on child spans,
    which rendered the Spans pane as "—". _enrich_span must fall back to
    (endTime - startTime) so every span shows a wall-clock time."""
    from admin.screens import observability as obs

    span = {
        "spanId": "s1", "name": "gemini.llm",
        "startTime": "2026-05-20T10:00:00.000Z",
        "endTime": "2026-05-20T10:00:02.500Z",
        # no "duration" key — exactly the dashboard-API shape we kept losing.
    }
    out = obs._enrich_span(span)
    assert out["duration_ms"] == 2500


def test_enrich_span_prefers_explicit_duration():
    from admin.screens import observability as obs

    span = {"spanId": "s2", "name": "x", "duration": 1.25,
            "startTime": "2026-05-20T10:00:00.000Z",
            "endTime": "2026-05-20T10:00:09.000Z"}
    out = obs._enrich_span(span)
    assert out["duration_ms"] == 1250  # explicit duration wins over start/end


def test_transcript_classifies_llm_and_tool_spans():
    from admin.screens import observability as obs

    spans = [
        _ch_llm_span("s1", start="2026-05-20T10:00:00.000Z",
                     messages_in=[{"role": "system", "content": "you are helpful"},
                                  {"role": "user", "content": "book me a slot"}],
                     output_text="let me check the calendar"),
        _ch_tool_span("s2", start="2026-05-20T10:00:01.000Z",
                      name="find_slots", tool_input='{"day": "tue"}', tool_output='["14:00"]'),
        _ch_llm_span("s3", start="2026-05-20T10:00:02.000Z",
                     messages_in=[{"role": "user", "content": "book me a slot"}],
                     output_text="Booked you at 14:00"),
    ]

    with patch.object(obs, "_clickhouse_spans", return_value=spans):
        with patch.object(obs, "ch_get_trace", return_value=None):
            data = obs.load_transcript_json("T1")

    roles = [t["role"] for t in data["turns"]]
    # Telemetry-only fallback (no persisted session linked): the span input's
    # user message becomes the user turn, the final LLM output becomes the
    # assistant answer, and the trace's tool calls attach to that assistant turn.
    assert roles == ["user", "assistant"]

    user_turn, asst_turn = data["turns"]
    assert "book me a slot" in user_turn["content"]
    assert asst_turn["content"] == "Booked you at 14:00"
    tools = asst_turn["tools"]
    assert tools[0]["name"] == "find_slots"
    assert "tue" in tools[0]["input"]
    assert "14:00" in tools[0]["output"]
    assert tools[0]["is_error"] is False


def test_transcript_tool_output_surfaced(monkeypatch):
    from admin.screens import observability as obs

    spans = [
        _ch_tool_span("s1", start="2026-05-20T10:00:00.000Z",
                      name="charge_card", tool_input='{"amount": 50}',
                      tool_output='{"error": "card declined"}'),
    ]
    with patch.object(obs, "_clickhouse_spans", return_value=spans):
        with patch.object(obs, "ch_get_trace", return_value=None):
            data = obs.load_transcript_json("T1")

    # Tool-only trace: the assistant turn carries the tool call + its output.
    asst = [t for t in data["turns"] if t["role"] == "assistant"][0]
    assert asst["tools"][0]["name"] == "charge_card"
    assert "declined" in asst["tools"][0]["output"]


def test_transcript_clickhouse_unavailable_returns_notice():
    from admin.screens import observability as obs

    # ClickHouse down (or not configured) AND no persisted session → empty
    # transcript with a notice, not a crash.
    tid = "3f7ef660-4ecf-45f8-a643-759468a18085"
    with patch.object(obs, "_clickhouse_spans", return_value=[]):
        with patch.object(obs, "ch_get_trace", return_value=None):
            with patch.object(obs, "ch_get_trace_result", return_value=(None, False)):
                data = obs.load_transcript_json(tid)
    assert data["turns"] == []
    assert data["found"] is False and data["unavailable"] is True
    # Partner-facing copy: plain words, no ClickHouse / env-var names.
    assert "temporarily unavailable" in data["notice"]
    assert "ClickHouse" not in data["notice"] and "LMNR" not in data["notice"]


def test_transcript_prefers_persisted_messages_and_merges_tools():
    """When a chat session is linked, the user/assistant spine comes from the
    persisted ai_chat_messages (the reliable record), and this trace's tool
    calls + raw model output attach to the assistant turn."""
    from admin.screens import observability as obs

    trace = {"id": "T1", "status": "success", "sessionId": "sess-1"}

    spans = [
        _ch_tool_span("s1", start="2026-05-20T10:00:01.000Z",
                      name="find_slots", tool_input='{"day":"tue"}', tool_output='["14:00"]'),
        _ch_llm_span("s2", start="2026-05-20T10:00:02.000Z",
                     messages_in=[{"role": "user", "content": "book me a slot"}],
                     output_text='{"answer":"Booked","raw":true}'),
    ]
    persisted = [
        {"role": "user", "content": "book me a slot", "ts": "2026-05-20T10:00:00Z", "source": "persisted", "tokens": None},
        {"role": "assistant", "content": "Booked you at 14:00", "ts": "2026-05-20T10:00:03Z", "source": "persisted", "tokens": 42},
    ]
    with patch.object(obs, "_clickhouse_spans", return_value=spans):
        with patch.object(obs, "ch_get_trace", return_value=trace):
            with patch.object(obs, "_persisted_conversation", return_value=persisted):
                data = obs.load_transcript_json("T1", env="dev")

    assert data["session_id"] == "sess-1"
    assert [t["role"] for t in data["turns"]] == ["user", "assistant"]
    asst = data["turns"][1]
    # Clean persisted answer wins over the raw model JSON.
    assert asst["content"] == "Booked you at 14:00"
    assert asst["tokens"] == 42
    # This trace's tool call is attached to the assistant turn.
    assert asst["tools"][0]["name"] == "find_slots"
    # The raw/structured model output is surfaced as collapsible reasoning.
    assert "raw" in asst["reasoning"]


def test_persisted_metadata_helpers_extract_tools_reasoning_tokens():
    from admin.screens import observability as obs

    meta = {
        "usage": {"input_tokens": 100, "output_tokens": 40},
        "thoughts": ["Analyzing...", "Using get_services"],
        "tool_calls": [
            {"name": "get_services", "args": {"month": "jun"}, "result": "[...]", "error": False},
            {"tool": "noname_skipped"},  # has a key but resolves a name → kept
        ],
    }
    assert obs._persisted_tokens(meta) == 140
    assert obs._persisted_reasoning(meta) == "Analyzing...\nUsing get_services"
    tools = obs._persisted_tool_calls(meta)
    assert tools[0]["name"] == "get_services"
    assert "month" in tools[0]["input"]
    assert tools[0]["is_error"] is False
    # tokens fallback to legacy "tokens" int
    assert obs._persisted_tokens({"tokens": 7}) == 7


def test_transcript_db_first_tools_not_overridden_by_spans():
    """When the persisted assistant turn already carries tool calls (from
    ai_chat_messages metadata), the Laminar span tools must NOT override them."""
    from admin.screens import observability as obs

    trace = {"id": "T1", "status": "success", "sessionId": "sess-1"}
    spans = [_ch_tool_span("s1", start="t", name="span_tool", tool_input="{}", tool_output="x")]
    persisted = [
        {"role": "user", "content": "hi", "ts": "t0", "source": "persisted"},
        {"role": "assistant", "content": "done", "ts": "t1", "source": "persisted",
         "tools": [{"name": "db_tool", "input": "{}", "output": "y", "is_error": False}],
         "reasoning": "db reasoning"},
    ]
    with patch.object(obs, "_clickhouse_spans", return_value=spans):
        with patch.object(obs, "ch_get_trace", return_value=trace):
            with patch.object(obs, "_persisted_conversation", return_value=persisted):
                data = obs.load_transcript_json("T1", env="dev")
    asst = data["turns"][1]
    assert [t["name"] for t in asst["tools"]] == ["db_tool"]  # span_tool did not override
    assert asst["reasoning"] == "db reasoning"


def test_transcript_links_session_from_span_attribute():
    """Laminar's trace.sessionId is null for our agents, so the chat session is
    recovered from the waj.session_id we tag on the root span (ClickHouse), and
    that drives the persisted-message lookup."""
    from admin.screens import observability as obs

    trace = {"id": "T1", "status": "success"}  # no sessionId

    span = _ch_llm_span("s1", start="2026-05-20T10:00:00.000Z",
                        messages_in=[], output_text="hi")
    span["waj_session_id"] = "sess-from-span"

    captured = {}

    def _fake_persisted(session_id, env):
        captured["session_id"] = session_id
        return [{"role": "user", "content": "hello", "ts": "t", "source": "persisted"}]

    with patch.object(obs, "_clickhouse_spans", return_value=[span]):
        with patch.object(obs, "ch_get_trace", return_value=trace):
            with patch.object(obs, "_persisted_conversation", side_effect=_fake_persisted):
                data = obs.load_transcript_json("T1", env="dev")

    assert captured["session_id"] == "sess-from-span"
    assert data["session_id"] == "sess-from-span"
    assert data["turns"][0]["role"] == "user"


def test_root_span_attrs_tags_session_id():
    from core import channel_resolver as cr
    from unittest.mock import MagicMock as MM

    req = MM()
    req.state = MM(spec=[])  # no channel attr
    attrs = cr.root_span_attrs(req, agent_kind="business", session_id="sess-9")
    assert attrs["waj.session_id"] == "sess-9"
    assert attrs["waj.agent_kind"] == "business"
    # Omitted when not provided.
    assert "waj.session_id" not in cr.root_span_attrs(req, agent_kind="business")


def test_overview_returns_trace_session_and_tool_rollup():
    from admin.screens import observability as obs

    trace = {
        "id": "T1", "status": "success", "sessionId": "sess-1",
        "startTime": "2026-05-20T10:00:00.000Z", "endTime": "2026-05-20T10:00:04.000Z",
        "inputTokens": 100, "outputTokens": 20, "totalCost": 0.0012,
    }
    spans = [
        _ch_tool_span("a", start="2026-05-20T10:00:01.000Z", name="t1", tool_input="{}", tool_output="x"),
        _ch_tool_span("b", start="2026-05-20T10:00:02.000Z", name="t1", tool_input="{}", tool_output="y"),
    ]

    class _Resp:
        data = [{"id": "sess-1", "agent_kind": "business", "business_id": "biz-1",
                 "branch_id": None, "title": "T", "message_count": 2,
                 "metadata": {"locale": "en"}, "created_at": "2026-05-20T10:00:00Z"}]

    class _Q:
        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def execute(self): return _Resp()

    class _Client:
        def schema(self, *a, **k): return self
        def table(self, *a, **k): return _Q()

    with patch.object(obs, "_clickhouse_spans", return_value=spans):
        with patch.object(obs, "ch_get_trace", return_value=trace):
            with patch("core.auth.get_service_client", return_value=_Client()):
                data = obs.load_trace_overview_json("T1")  # env=None → service client

    assert data["trace"]["duration_ms"] == 4000
    assert data["trace"]["input_tokens"] == 100
    assert data["tool_calls"] == 2
    assert data["tools"][0] == {"name": "t1", "calls": 2, "errors": 0}
    assert data["session"]["agent_kind"] == "business"
    assert data["session"]["locale"] == "en"


def test_transcript_tool_calls_preserve_clickhouse_order():
    from admin.screens import observability as obs

    # _clickhouse_spans returns rows ordered by start_time (ORDER BY in SQL);
    # the builder preserves that order in the assistant turn's tools list.
    spans = [
        _ch_tool_span("a", start="2026-05-20T10:00:02.000Z",
                      name="first", tool_input="{}", tool_output="A"),
        _ch_tool_span("b", start="2026-05-20T10:00:03.000Z",
                      name="second", tool_input="{}", tool_output="B"),
        _ch_llm_span("late", start="2026-05-20T10:00:05.000Z",
                     messages_in=[{"role": "user", "content": "x"}], output_text="LAST"),
    ]
    with patch.object(obs, "_clickhouse_spans", return_value=spans):
        with patch.object(obs, "ch_get_trace", return_value=None):
            data = obs.load_transcript_json("T1")

    asst = [t for t in data["turns"] if t["role"] == "assistant"][0]
    assert [t["name"] for t in asst["tools"]] == ["first", "second"]


# ---------------------------------------------------------------------------
# agent palette
# ---------------------------------------------------------------------------


def test_agent_color_known_kinds(monkeypatch):
    from admin.screens import observability as obs

    monkeypatch.setattr(obs, "AGENT_PALETTE", {"sample_a": "#111111", "sample_b": "#222222"})
    for kind in ("sample_a", "sample_b"):
        color = obs.agent_color(kind)
        assert color.startswith("#")
        assert color == obs.AGENT_PALETTE[kind]
    # Every known kind in this deployment's palette has a distinct color.
    from admin.agent_kinds import catalog_dict
    palette = catalog_dict("kind_colors")
    assert len(set(palette.values())) == len(palette)


def test_agent_color_fallback_for_unknown_and_none():
    from admin.screens.observability import agent_color

    fallback = agent_color(None)
    assert fallback.startswith("#")
    assert agent_color("totally_unknown_kind") == fallback
    assert agent_color("") == fallback


# ---------------------------------------------------------------------------
# load_live_insights
# ---------------------------------------------------------------------------


def _trace(trace_id, *, has_error=0, in_tok=10, out_tok=20, agent_kind=None,
           channel_slug=None, session_id=None, duration_sec=1.0,
           start="2026-05-20 10:00:00.000000000",
           end="2026-05-20 10:00:01.000000000"):
    """A `traces_replacing` row. agent_kind/channel resolve from trace METADATA
    (stamped on the root span via `Laminar.set_trace_metadata`, and carried on
    the trace aggregate) — that's what `_annotate_trace_channel_meta` reads.
    Error state is the events-aggregated `has_error` the trace queries join
    from `spans` — the `status` column is '' on every live row and unread."""
    return ch_trace_row(
        trace_id, agent_kind=agent_kind, channel_slug=channel_slug,
        session_id=session_id, has_error=has_error, in_tokens=in_tok,
        out_tokens=out_tok, duration_sec=duration_sec,
        top_span_name="agent.invoke", start=start, end=end,
    )


def test_live_insights_aggregates_errors_tokens_latency():
    from admin.screens import observability as obs

    traces = [
        _trace("T-ok", in_tok=100, out_tok=200, agent_kind="support_bot",
               duration_sec=0.5),
        _trace("T-err", has_error=1, in_tok=50, out_tok=10,
               agent_kind="business", duration_sec=2.0),
    ]
    # Tool volume is its own server-side aggregate now (one query for the whole
    # window), not a per-trace span fan-out.
    tools = [
        {"name": "find_slots", "calls": "1", "errors": "0"},
        {"name": "charge_card", "calls": "1", "errors": "1"},
    ]

    with patch("core.lmnr_clickhouse.query",
               make_ch_query(traces=traces, tools=tools)):
        out = obs.load_live_insights(past_hours=1)

    assert out["lmnr_ok"] is True
    # Tokens.
    assert out["tokens"]["in_1h"] == 150
    assert out["tokens"]["out_1h"] == 210
    # Error trace surfaced with explorer-linkable id.
    assert [e["id"] for e in out["error_traces"]] == ["T-err"]
    assert out["error_traces"][0]["agent_kind"] == "business"
    # Latency present.
    assert out["latency"]["p50_ms"] is not None
    assert out["latency"]["p95_ms"] is not None
    # Active agents (both kinds saw traffic) with colors.
    kinds = {a["kind"] for a in out["active_agents"]}
    assert kinds == {"support_bot", "business"}
    assert all(a["color"].startswith("#") for a in out["active_agents"])
    # Top tools with error rates.
    by_name = {t["name"]: t for t in out["top_tools"]}
    assert by_name["find_slots"]["calls"] == 1
    assert by_name["find_slots"]["error_rate_pct"] == 0.0
    assert by_name["charge_card"]["errors"] == 1
    assert by_name["charge_card"]["error_rate_pct"] == 100.0


def test_live_insights_clickhouse_down_degrades_gracefully():
    from admin.screens import observability as obs

    with patch("core.lmnr_clickhouse.query", make_ch_query(raises=True)):
        out = obs.load_live_insights()

    assert out["lmnr_ok"] is False
    assert out["notice"]
    assert out["error_traces"] == []
    assert out["top_tools"] == []
    assert out["active_agents"] == []


def test_live_insights_ttft_and_sessions_survive_a_dead_trace_list():
    """The regression this ordering exists for.

    `load_live_insights` used to bail out before its TTFT and session-count
    queries whenever the trace list came back unusable, so five Dashboard KPI
    tiles rendered "—" while the numbers they needed were sitting in ClickHouse.
    Each read is independent, so a trace-list miss must not strand the others.
    """
    from admin.screens import observability as obs

    def _query(sql, params=None, **_kw):
        if "uniqExact" in sql:
            return [{"sessions": 12}]
        if "waj.ttft_ms" in sql:
            return [{"ttft_p50": 800.0, "ttft_p95": 1500.0,
                     "ttlt_p50": 3000.0, "ttlt_p95": 5000.0}]
        if "FROM traces_replacing FINAL" in sql:
            raise RuntimeError("trace list query blew up")
        return []

    with patch("core.lmnr_clickhouse.query", _query):
        out = obs.load_live_insights(past_hours=24, env="prod")

    assert out["sessions_24h"] == 12
    assert out["ttft"] == {"p50_ms": 800, "p95_ms": 1500}
    assert out["ttlt"] == {"p50_ms": 3000, "p95_ms": 5000}
    # `_live_kpis` blanks every tile when this is False, so a partial outage must
    # still report the pipeline as usable.
    assert out["lmnr_ok"] is True
    # The rollups that genuinely had no source stay empty, without a fake zero.
    assert out["error_traces"] == []
    assert out["active_agents"] == []


# ---------------------------------------------------------------------------
# Explorer renders the agent-card markup + transcript toggle
# ---------------------------------------------------------------------------


def _ensure_explorer_loaded():
    from admin import observability_explorer_handlers  # noqa: F401


def test_explorer_renders_agent_card_markup(client):
    _ensure_explorer_loaded()

    traces = [_trace("T-sup", agent_kind="support_bot"), _trace("T-legacy")]

    with patch("core.lmnr_clickhouse.query", make_ch_query(traces=traces)):
        with patch("core.channels.list_active", return_value=[]):
            r = client.get("/admin/observability/explorer")

    assert r.status_code == 200, r.text[:200]
    # Rows name the agent in words — no agent_kind slug chips any more.
    assert "ox-agent-chip" not in r.text
    assert 'class="cv-agent">Support bot<' in r.text
    # A trace with no agent tag still lists (as an unlabelled agent).
    assert "Unlabelled agent" in r.text
    # Conversation view toggle present.
    assert "OX.setView('transcript')" in r.text
    assert 'id="ox-transcript"' in r.text


def test_explorer_transcript_endpoint_returns_turns(client):
    _ensure_explorer_loaded()

    spans = [
        _ch_llm_span("s1", start="2026-05-20T10:00:00.000Z",
                     messages_in=[{"role": "user", "content": "hi"}], output_text="hello"),
    ]

    from admin.screens import observability as obs

    with patch.object(obs, "_clickhouse_spans", return_value=spans):
        with patch.object(obs, "ch_get_trace", return_value=None):
            r = client.get("/admin/observability/explorer/trace/T1/transcript.json")

    assert r.status_code == 200, r.text[:200]
    payload = r.json()
    assert payload["trace"]["id"] == "T1"
    roles = [t["role"] for t in payload["turns"]]
    assert "user" in roles and "assistant" in roles


# ---------------------------------------------------------------------------
# _pretty_json — double-decoding for doubly-serialized JSON
# ---------------------------------------------------------------------------


def test_pretty_json_double_encoded_object():
    """A JSON string wrapping JSON (doubly serialized) must be decoded to real
    structure, not left as escaped literals."""
    from admin.screens.observability import _pretty_json

    inner = json.dumps({"role": "user", "content": "hello"})  # single-encoded
    double = json.dumps(inner)  # double-encoded: '"{\\"role\\": ...}"'

    result = _pretty_json(double)

    # Must contain the actual key without any escaping.
    assert '"role"' in result
    assert '\\"role\\"' not in result
    # Must be multi-line (pretty-printed).
    assert "\n" in result
    parsed_back = json.loads(result)
    assert parsed_back["role"] == "user"
    assert parsed_back["content"] == "hello"


def test_pretty_json_single_encoded_object():
    """A normal single-encoded JSON object still pretty-prints correctly."""
    from admin.screens.observability import _pretty_json

    raw = json.dumps({"action": "book_slot", "date": "2026-06-03"})
    result = _pretty_json(raw)

    assert "\n" in result
    parsed_back = json.loads(result)
    assert parsed_back["action"] == "book_slot"


def test_pretty_json_plain_string_returned_as_is():
    """A plain non-JSON string must be returned unchanged (not JSON-quoted)."""
    from admin.screens.observability import _pretty_json

    plain = "just some text with no JSON"
    assert _pretty_json(plain) == plain


def test_pretty_json_json_string_value_returned_unwrapped():
    """A JSON string that decodes to a plain string should be returned as the
    bare string, not re-wrapped in quotes."""
    from admin.screens.observability import _pretty_json

    inner_str = "I am just a string"
    encoded = json.dumps(inner_str)  # '"I am just a string"'
    result = _pretty_json(encoded)
    assert result == inner_str


def test_pretty_json_empty_input():
    from admin.screens.observability import _pretty_json

    assert _pretty_json("") == ""
    assert _pretty_json(None) == ""  # type: ignore[arg-type]


def test_pretty_json_invalid_json_returns_original():
    """On invalid JSON the original string is returned unchanged."""
    from admin.screens.observability import _pretty_json

    garbage = "{not valid json"
    assert _pretty_json(garbage) == garbage
