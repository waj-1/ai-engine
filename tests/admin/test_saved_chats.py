"""Conversations with no tracing store (admin/screens/saved_chats.py): the
list, one conversation, the honest notes, and that a configured tracing store
is read exactly as before."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from admin.screens import observability as obs
from admin.screens import saved_chats as sc
from core import lmnr_clickhouse as ch
from tests.fake_pg import Client

S1 = "11111111-1111-4111-8111-111111111111"
S2 = "22222222-2222-4222-8222-222222222222"
S3 = "33333333-3333-4333-8333-333333333333"
S_OLD = "44444444-4444-4444-8444-444444444444"
S_GONE = "55555555-5555-4555-8555-555555555555"


def _ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def tracing_off(monkeypatch):
    monkeypatch.setenv("LMNR_DISABLED", "true")
    monkeypatch.delenv("LMNR_CLICKHOUSE_URL", raising=False)


@pytest.fixture
def db(monkeypatch, tracing_off):
    store = {
        "agent_profiles": [
            {"agent_kind": "custom", "slug": "demo", "display_name": "Demo agent", "status": "active"},
            {"agent_kind": "custom", "slug": "helpdesk", "display_name": "Help desk", "status": "active"},
        ],
        "ai_chat_sessions": [
            {"id": S1, "agent_kind": "custom", "title": "New Chat", "business_id": None, "deleted_at": None,
             "metadata": {"agent_slug": "demo", "source": "api"},
             "created_at": _ago(hours=2), "updated_at": _ago(hours=2)},
            {"id": S2, "agent_kind": "custom", "title": "Refund question", "business_id": None,
             "deleted_at": None, "metadata": {"agent_slug": "helpdesk", "source": "api", "channel_slug": "web"},
             "created_at": _ago(hours=5), "updated_at": _ago(minutes=30)},
            {"id": S3, "agent_kind": "orchard", "title": "New Chat", "business_id": None, "deleted_at": None,
             "metadata": {}, "created_at": _ago(hours=3), "updated_at": _ago(hours=3)},
            {"id": S_OLD, "agent_kind": "custom", "title": "Old", "business_id": None, "deleted_at": None,
             "metadata": {}, "created_at": _ago(days=9), "updated_at": _ago(days=9)},
            {"id": S_GONE, "agent_kind": "custom", "title": "Deleted", "business_id": None,
             "deleted_at": _ago(minutes=5), "metadata": {},
             "created_at": _ago(minutes=10), "updated_at": _ago(minutes=10)},
        ],
        "ai_chat_messages": [
            {"session_id": S1, "role": "user", "content": "What is  this engine?", "metadata": {},
             "created_at": _ago(hours=2)},
            {"session_id": S1, "role": "assistant", "content": "I am a demo agent.", "metadata": {},
             "created_at": _ago(hours=1, minutes=59)},
            {"session_id": S1, "role": "user", "content": "How do I add a model?", "metadata": {},
             "created_at": _ago(hours=1, minutes=58)},
            {"session_id": S1, "role": "assistant", "content": "Agents > Models.",
             "metadata": {"tools_used": ["search_knowledge"]}, "created_at": _ago(hours=1, minutes=57)},
            {"session_id": S2, "role": "user", "content": "I want a REFUND for order 77", "metadata": {},
             "created_at": _ago(minutes=31)},
            {"session_id": S3, "role": "user", "content": "private orchard notes", "metadata": {},
             "created_at": _ago(hours=3)},
            {"session_id": S3, "role": "assistant", "content": "orchard reply",
             "metadata": {"tool_calls": [{"name": "lookup", "input": {"q": 1}, "output": "row"}]},
             "created_at": _ago(hours=2, minutes=59)},
        ],
        "agent_traces": [
            {"session_id": S1, "created_at": _ago(hours=1, minutes=59), "status": "ok", "input_tokens": 100,
             "output_tokens": 20, "cache_read_tokens": 0, "latency_ms": 900, "model_string": "qwen2.5:3b",
             "cost_usd": 0.001},
            {"session_id": S1, "created_at": _ago(hours=1, minutes=57), "status": "ok", "input_tokens": 200,
             "output_tokens": 30, "cache_read_tokens": 50, "latency_ms": 1100, "model_string": "qwen2.5:3b",
             "cost_usd": 0.002},
            {"session_id": S2, "created_at": _ago(minutes=30), "status": "error", "input_tokens": 0,
             "output_tokens": 0, "latency_ms": 400},
        ],
    }
    client = Client(store)
    monkeypatch.setattr(sc, "_client", lambda env: client)
    monkeypatch.setattr("admin.router.client_for", lambda env: client)
    monkeypatch.setattr(obs, "admin_service_client", lambda: client)
    monkeypatch.setattr(sc.pl, "fetch_business_names", lambda ids: {})
    monkeypatch.setattr(sc.pl, "load_tool_display_names", lambda env=None: {})
    return store


# ------------------------------------------------------------------ the switch
def test_tracing_is_off_only_when_disabled_and_no_store_address(monkeypatch):
    monkeypatch.delenv("LMNR_DISABLED", raising=False)
    monkeypatch.delenv("LMNR_CLICKHOUSE_URL", raising=False)
    assert ch.tracing_configured() is True  # the default: as before
    monkeypatch.setenv("LMNR_DISABLED", "true")
    assert ch.tracing_configured() is False
    monkeypatch.setenv("LMNR_CLICKHOUSE_URL", "http://store:8123")
    assert ch.tracing_configured() is True  # a reader that exports nothing still reads


def test_a_query_with_tracing_off_makes_no_call_and_records_no_failure(tracing_off, monkeypatch):
    def no_network(*_a, **_k):
        raise AssertionError("no network call when tracing is off")

    monkeypatch.setattr(ch.httpx, "post", no_network)
    ch.clear_last_failure()
    with pytest.raises(ch.ClickHouseUnavailable) as err:
        ch.query("SELECT 1")
    assert err.value.kind == "off"
    assert ch.last_failure() is None  # not an outage: no "could not be reached" banner


# --------------------------------------------------------------------- listing
def test_lists_saved_conversations_newest_activity_first(db):
    rows = sc.list_conversations(env="staging", hours=24)
    assert [g["session_id"] for g in rows] == [S2, S1, S3]  # old and deleted ones are out
    demo = rows[1]
    assert demo["agent_name"] == "Demo agent"  # by the slug saved on the session
    assert rows[0]["agent_name"] == "Help desk"
    assert demo["first_message"] == "What is this engine?"
    assert demo["message_count"] == 4 and demo["turns"] == 2
    assert demo["channel_name"] == "API" and rows[0]["channel_name"] == "Web"
    assert demo["newest_trace_id"] == S1 and demo["traces"] == [{"id": S1, "topSpanName": ""}]
    assert demo["outcome"] == "ok" and rows[0]["outcome"] == "attention"  # a failed reply
    assert rows[0]["title"] == "Refund question" and demo["title"] is None


def test_filters_by_agent_period_channel_outcome_and_message_text(db):
    ids = lambda **kw: [g["session_id"] for g in sc.list_conversations(env="staging", **kw)]  # noqa: E731
    assert ids(hours=24, agent_kind="orchard") == [S3]
    assert ids(hours=1) == [S2]
    assert S_OLD in ids(hours=24 * 30)
    # Started long ago, continued today: listed by its recent message.
    db["ai_chat_messages"].append({"session_id": S_OLD, "role": "user", "content": "back again",
                                   "metadata": {}, "created_at": _ago(minutes=10)})
    assert ids(hours=24)[0] == S_OLD and ids(hours=24, agent_kind="orchard") == [S3]
    db["ai_chat_messages"].pop()
    assert ids(hours=24, channel_slug="web") == [S2]
    assert ids(hours=24, outcome="attention") == [S2]
    assert ids(hours=24, search="refund") == [S2]  # in a message, case-insensitive
    assert ids(hours=24, search="add a model") == [S1]
    assert ids(hours=24, search="no such words") == []
    assert ids(hours=24, search="100%") == []  # a wildcard character is searched literally


def test_the_list_context_says_tracing_is_off_without_an_error(db):
    from admin.screens.conversations import build_conversations_context

    ctx = build_conversations_context(env="staging", hours=24, search=None, status=None, outcome=None,
                                      tool=None, channel_slug=None, agent_kind=None, group="session",
                                      background=0)
    assert ctx["saved_chats"] is True and ctx["total"] == 3
    assert ctx["list_unavailable"] is False and ctx["list_detail"] is None
    assert "tracing stack" in ctx["saved_chats_note"]
    assert ctx["groups"][0]["session_id"] == S2 and ctx["tool_names"] == []


def test_a_database_failure_is_reported_as_itself(tracing_off, monkeypatch):
    def boom(**_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(sc, "list_conversations", boom)
    ctx = sc.build_list_context(env="staging", hours=24, search=None, outcome=None,
                                channel_slug=None, agent_kind=None)
    assert ctx["list_unavailable"] is True and "database" in ctx["list_detail"]


# ------------------------------------------------------------ one conversation
def test_opens_the_transcript_in_order_with_usage_and_tool_names(db):
    out = sc.load_transcript_json(S1, env="staging")
    assert out["found"] is True and out["unavailable"] is False and out["notice"] is None
    assert [(t["role"], t["content"]) for t in out["turns"]] == [
        ("user", "What is  this engine?"), ("assistant", "I am a demo agent."),
        ("user", "How do I add a model?"), ("assistant", "Agents > Models.")]
    first, second = out["turns"][1], out["turns"][3]
    assert (first["tokens"], first["latency_ms"], first["model"]) == (120, 900, "qwen2.5:3b")
    assert (second["tokens"], second["cache_read"]) == (230, 50)
    assert second["tools"] == [{"name": "search_knowledge", "input": "", "output": "", "is_error": False,
                                "name_only": True, "label": second["tools"][0]["label"], "is_handoff": False}]
    assert "tools" not in first
    summary = out["summary"]
    assert (summary["agent_name"], summary["messages"], summary["actions"]) == ("Demo agent", 4, 1)
    assert summary["outcome"] == "ok"


def test_a_failed_reply_marks_the_conversation_and_does_not_shift_usage(db):
    db["agent_traces"].insert(0, {"session_id": S1, "created_at": _ago(hours=2), "status": "error",
                                  "input_tokens": 0, "output_tokens": 0, "latency_ms": 5})
    out = sc.load_transcript_json(S1, env="staging")
    assert out["turns"][1]["tokens"] == 120 and out["turns"][3]["tokens"] == 230
    assert out["summary"]["outcome"] == "attention" and out["summary"]["error_without_action"] is True


def test_totals_for_the_technical_pane_come_from_the_turn_rows(db):
    out = sc.load_overview_json(S1, env="staging")
    t = out["trace"]
    assert (t["input_tokens"], t["output_tokens"], t["duration_ms"], t["cache_read_tokens"]) == (300, 50, 2000, 50)
    assert t["total_cost"] == pytest.approx(0.003) and t["replies"] == 2 and t["failed_replies"] == 0
    assert t["models"] == ["qwen2.5:3b"] and t["status"] == "ok"
    assert out["session"]["id"] == S1 and out["session"]["source"] == "api" and out["found"] is True


def test_the_technical_pane_counts_the_saved_messages(db):
    # ai_chat_sessions.message_count is never maintained (it stays 0): the
    # Technical pane must show the messages the Conversation pane shows.
    for row in db["ai_chat_sessions"]:
        row["message_count"] = 0
    assert sc.load_overview_json(S1, env="staging")["session"]["message_count"] == 4
    assert sc.load_overview_json(S2, env="staging")["session"]["message_count"] == 1


def test_the_tracing_backed_technical_pane_counts_the_saved_messages(db, monkeypatch):
    for row in db["ai_chat_sessions"]:
        row["message_count"] = 0
    monkeypatch.setattr(obs, "ch_get_trace", lambda *a, **k: {
        "id": "T1", "status": "success", "sessionId": S1,
        "startTime": "2026-05-20T10:00:00.000Z", "endTime": "2026-05-20T10:00:01.000Z"})
    monkeypatch.setattr(obs, "_clickhouse_spans", lambda *a, **k: [])
    out = obs.load_trace_overview_json("T1", env="staging")
    assert out["session"]["id"] == S1 and out["session"]["message_count"] == 4


def test_steps_are_a_plain_note_not_an_error():
    out = sc.load_steps_json(S1)
    assert out["found"] is True and out["unavailable"] is False and out["tree"] == []
    assert "tracing stack" in out["notice"] and "unavailable" not in out["notice"].lower()


def test_an_unknown_or_deleted_conversation_is_not_found(db):
    for sid in ("99999999-9999-4999-8999-999999999999", S_GONE):
        assert sc.load_transcript_json(sid, env="staging")["found"] is False
        assert sc.load_overview_json(sid, env="staging")["found"] is False


def test_the_transcript_is_exactly_the_one_the_tracing_backed_screen_shows(db):
    """The access rule. A kind whose telemetry is exported redacted
    (core.sensitive_content) is shown by the tracing-backed screen from the
    saved messages, read by `_persisted_conversation`. This screen reads the
    same function and adds no other content, for a sensitive kind too."""
    import core.sensitive_content as sensitive

    original = sensitive.sensitive_agent_kinds
    sensitive.sensitive_agent_kinds = lambda: frozenset({"orchard"})
    try:
        assert sensitive.is_sensitive_kind("orchard")
        traced = obs._persisted_conversation(S3, "staging")
        saved = sc.load_transcript_json(S3, env="staging")["turns"]
    finally:
        sensitive.sensitive_agent_kinds = original
    strip = lambda turns: [{k: v for k, v in t.items() if k not in ("label", "is_handoff")}  # noqa: E731
                           for t in [{**t, "tools": [dict(x) for x in t.get("tools", [])]} for t in turns]]
    assert [(t["role"], t["content"], t.get("ts")) for t in saved] == \
        [(t["role"], t["content"], t.get("ts")) for t in traced]
    assert [[x["name"] for x in t.get("tools", [])] for t in saved] == \
        [[x["name"] for x in t.get("tools", [])] for t in traced]
    assert strip(saved)[1]["tools"][0]["output"] == strip(traced)[1]["tools"][0]["output"]


# ----------------------------------------------------- routes and other screens
def test_the_panes_are_served_from_saved_chats_only_when_tracing_is_off(db, monkeypatch):
    from admin import observability_explorer_handlers as handlers

    assert handlers._saved_chats() is sc
    monkeypatch.setenv("LMNR_CLICKHOUSE_URL", "http://store:8123")
    assert handlers._saved_chats() is None  # configured: the tracing store, as before


def test_with_tracing_configured_the_list_reads_the_tracing_store(db, monkeypatch):
    from admin.screens import conversations

    monkeypatch.setenv("LMNR_CLICKHOUSE_URL", "http://store:8123")
    seen = {}

    def traces(**kw):
        seen.update(kw)
        return {"traces": [], "notice": None}

    monkeypatch.setattr(obs, "load_traces_list", traces)
    monkeypatch.setattr(sc, "build_list_context", lambda **_k: pytest.fail("saved chats must not be used"))
    monkeypatch.setattr(conversations, "enrich_groups", lambda *a, **k: None)
    monkeypatch.setattr(obs, "list_tool_names_from_db", lambda env=None: [])
    ctx = conversations.build_conversations_context(
        env="staging", hours=24, search=None, status=None, outcome=None, tool=None,
        channel_slug=None, agent_kind=None, group="session", background=0)
    assert seen["past_hours"] == 24 and "saved_chats" not in ctx


def test_the_overview_says_tracing_is_off_instead_of_unavailable(db, monkeypatch):
    from admin.screens import overview

    monkeypatch.setattr(overview, "_ch", lambda *a, **k: pytest.fail("no tracing query when tracing is off"))
    monkeypatch.setattr("admin.screens.insights.build_insights", lambda **k: {"available": False})
    monkeypatch.setattr(overview, "voice_usage", lambda client, hours: None)
    out = overview.build_overview(env="staging", period_key=None, client=Client(db))
    assert out["sources"]["tracing_off"] is True and out["status"]["state"] == "off"
    assert "Tracing is off" in out["status"]["headline"]
    assert "unavailable" not in out["status"]["headline"].lower()
    tiles = {t["key"]: t for t in out["tiles"]}
    assert tiles["conversations"]["caption"] == "From saved conversations"
    assert tiles["conversations"]["value"] != "—"
    assert all(tiles[k]["caption"] == "Needs tracing" for k in ("attention", "handoffs", "failed_actions", "response"))
    assert out["attention"] == []


def test_the_charts_notice_says_tracing_is_off(tracing_off):
    from admin.screens.live_analytics import load_analytics

    out = load_analytics(hours=24)
    assert out["ok"] is False and out["notice"].startswith("Tracing is off")


def test_the_templates_carry_the_honest_states():
    from pathlib import Path

    root = Path(sc.__file__).parents[1] / "templates"
    explorer = (root / "observability_explorer.html").read_text()
    assert 'data-testid="saved-chats-note"' in explorer and "{% if not saved_chats %}" in explorer
    live = (root / "live.html").read_text()
    assert 'data-testid="tracing-off"' in live and "Turning the tracing stack on adds" in live


def test_a_session_link_opens_the_saved_chat(tracing_off):
    from admin.router import _redirect_session_to_trace

    assert _redirect_session_to_trace(S1, "staging").headers["location"] == \
        f"/admin/observability/explorer#trace={S1}"
    assert _redirect_session_to_trace("not-an-id", "staging").headers["location"] == \
        "/admin/observability/explorer"


# ------------------------------------------------------------- rendered pages
def test_the_conversations_page_renders_the_saved_list(client, db):
    r = client.get("/admin/observability?hours=24")
    assert r.status_code == 200
    assert 'data-testid="saved-chats-note"' in r.text and "Tracing is off" in r.text
    assert "temporarily unavailable" not in r.text.split("<script")[0]
    assert f'data-trace-id="{S1}"' in r.text and "What is this engine?" in r.text
    assert "Demo agent" in r.text and "More filters" not in r.text
    assert "3 conversations" in r.text


def test_the_pane_endpoints_serve_a_saved_chat(client, db):
    base = "/admin/observability/explorer"
    tx = client.get(f"{base}/trace/{S1}/transcript.json").json()
    assert tx["saved_chat"] is True and len(tx["turns"]) == 4 and tx["found"] is True
    ov = client.get(f"{base}/trace/{S1}/overview.json").json()
    assert ov["trace"]["input_tokens"] == 300
    steps = client.get(f"{base}/trace/{S1}/spans.json").json()
    assert steps["tree"] == [] and "tracing stack" in steps["notice"]
    audio = client.get(f"{base}/trace/{S1}/audio.json").json()
    assert audio["recordings"] == []
    span = client.get(f"{base}/span/{S1}/abc.json").json()
    assert span["span"] is None and "tracing stack" in span["notice"]


def test_the_overview_page_renders_the_tracing_off_state(client, db):
    r = client.get("/admin/live")
    assert r.status_code == 200
    assert 'data-testid="tracing-off"' in r.text and "status-off" in r.text
    assert "Live statistics are temporarily unavailable" not in r.text
    assert "The list of problems could not be loaded" not in r.text
