"""Conversation insights — outcome / topic / language per conversation.

Covers the derivation (core/conversation_insights.py), the bounded sweep
(lease, candidates, upsert), the Overview payload (admin/screens/insights.py)
including partner-build scoping and the "collecting" / "unavailable" states,
the rendered Overview section, and the admin MCP tool.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core import conversation_insights as ci


def _msgs(*pairs):
    t0 = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)
    return [{"role": r, "content": c, "created_at": (t0 + timedelta(seconds=i)).isoformat()}
            for i, (r, c) in enumerate(pairs)]


# --------------------------------------------------------------------------- #
# Language
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("texts,expected", [
    (["مرحبا، أبغى أحجز موعد"], "ar"),
    (["Hi, I want to book a haircut"], "en"),
    (["أبغى booking for tomorrow please ok", "شكرا جزيلا لك"], "mixed"),
    (["123 456", "👍"], None),
    ([], None),
])
def test_detect_language_by_script(texts, expected):
    assert ci.detect_language(texts) == expected


# --------------------------------------------------------------------------- #
# Rules (no classifier)
# --------------------------------------------------------------------------- #

def test_rule_outcome_is_conservative():
    assert ci.rule_outcome(_msgs(("user", "price?"), ("assistant", "100 AED"),
                                 ("user", "Thanks!"))) == "resolved"
    assert ci.rule_outcome(_msgs(("user", "شكرا"))) == "resolved"
    assert ci.rule_outcome(_msgs(("user", "my card was charged twice"),
                                 ("assistant", "I'm connecting you with our team now."))) == "escalated"
    assert ci.rule_outcome(_msgs(("user", "مشكلة"),
                                 ("assistant", "سأحولك لأحد الزملاء"))) == "escalated"
    assert ci.rule_outcome(_msgs(("user", "hello?"))) == "abandoned"
    # Anything else is NOT claimed as resolved.
    assert ci.rule_outcome(_msgs(("user", "price?"), ("assistant", "100 AED"))) == "unknown"


def test_transcript_keeps_start_and_end_of_long_conversations():
    pairs = [("user" if i % 2 == 0 else "assistant", "message %03d " % i + "x" * 300)
             for i in range(60)]
    t = ci.build_transcript(_msgs(*pairs), max_chars=3000)
    assert len(t) <= 3200
    assert "message 000" in t and "message 059" in t
    assert "middle of the conversation omitted" in t
    assert t.startswith("Customer: ")


# --------------------------------------------------------------------------- #
# Classifier normalisation
# --------------------------------------------------------------------------- #

class _FakeLLM:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(content=self.content)


@pytest.fixture(autouse=True)
def _sample_topic_sets(monkeypatch):
    """Fictional agents with label sets of their own (a plugin supplies a
    deployment's through agent_catalog.topic_sets())."""
    monkeypatch.setattr(ci, "TOPIC_SETS", {
        **ci.TOPIC_SETS,
        "sample_agent": ci._CUSTOMER_SERVICE,
        "sample_helper": ci._CUSTOMER_SERVICE,
        "sample_other": {"features": "What the product does", "pricing_plans": "Pricing and plans"},
    })


def test_classify_normalises_to_the_agents_label_set(monkeypatch):
    llm = _FakeLLM('```json\n{"topic": "BOOKING", "outcome": "resolved", "language": "AR"}\n```')
    monkeypatch.setattr(ci, "_llm", lambda _m: llm)
    out = ci.classify("sample_agent", "Customer: hi", model_string="m")
    assert out == {"topic": "booking", "outcome": "resolved", "language": "ar"}
    # The prompt offers the agent's own labels plus the shared ones.
    prompt = llm.calls[0][1].content
    assert "reschedule_or_cancel" in prompt and "greeting_only" in prompt and "other" in prompt


def test_classify_maps_unknown_values_and_tolerates_block_content(monkeypatch):
    llm = _FakeLLM([{"type": "text", "text": '{"topic": "weather", "outcome": "unclear", "language": "english"}'}])
    monkeypatch.setattr(ci, "_llm", lambda _m: llm)
    out = ci.classify("agents_collection", "Customer: hi", model_string="m")
    assert out == {"topic": "other", "outcome": "unknown", "language": None}


def test_classify_returns_none_on_failure(monkeypatch):
    class Boom:
        def invoke(self, _m):
            raise RuntimeError("quota")
    monkeypatch.setattr(ci, "_llm", lambda _m: Boom())
    assert ci.classify("sales", "Customer: hi", model_string="m") is None
    monkeypatch.setattr(ci, "_llm", lambda _m: _FakeLLM("not json at all"))
    assert ci.classify("sales", "Customer: hi", model_string="m") is None


# --------------------------------------------------------------------------- #
# analyze()
# --------------------------------------------------------------------------- #

_SESSION = {"session_id": "11111111-1111-1111-1111-111111111111", "agent_kind": "support_bot",
            "metadata": {}, "created_at": "2026-10-11T09:00:00+00:00",
            "updated_at": "2026-10-11T09:05:00+00:00"}


def test_telemetry_handoff_beats_the_classifier(monkeypatch):
    monkeypatch.setattr(ci, "classify", lambda *a, **k: {"topic": "prices", "outcome": "resolved",
                                                          "language": "en"})
    row = ci.analyze(_SESSION, _msgs(("user", "hi"), ("assistant", "hello")),
                     signal={"handoff": True, "env": "prod"}, model_string="m")
    assert (row["outcome"], row["outcome_source"]) == ("escalated", "telemetry")
    assert row["topic"] == "prices" and row["environment"] == "prod"
    assert row["classifier_model"] == "m"


def test_analyze_without_classifier_uses_rules_and_metadata_env():
    s = {**_SESSION, "metadata": {"environment": "staging", "channel": "voice"}}
    row = ci.analyze(s, _msgs(("user", "كم السعر؟"), ("assistant", "١٠٠ درهم"), ("user", "شكرا")),
                     signal={"env": "prod"}, model_string=None)
    assert (row["outcome"], row["outcome_source"]) == ("resolved", "rule")
    assert row["topic"] is None and row["classifier_model"] is None
    assert row["language"] == "ar"
    assert row["environment"] == "staging"  # the writer's stamp wins over telemetry
    assert row["channel"] == "voice"
    assert row["user_turns"] == 2 and row["message_count"] == 3
    assert row["last_message_at"] == _SESSION["updated_at"]


def test_classifier_language_refines_latin_script_only(monkeypatch):
    monkeypatch.setattr(ci, "classify", lambda *a, **k: {"topic": "other", "outcome": "unknown",
                                                          "language": "fr"})
    row = ci.analyze(_SESSION, _msgs(("user", "Bonjour, je voudrais un rendez-vous")),
                     model_string="m")
    assert row["language"] == "fr"
    row = ci.analyze(_SESSION, _msgs(("user", "مرحبا")), model_string="m")
    assert row["language"] == "ar"


def test_simulated_voice_call_is_flagged():
    row = ci.analyze(_SESSION, _msgs(("user", "hi")), voice={"simulated": True})
    assert row["is_simulated"] is True and row["channel"] == "voice"


# --------------------------------------------------------------------------- #
# Telemetry lookup
# --------------------------------------------------------------------------- #

def test_telemetry_signals_batches_two_queries(monkeypatch):
    sid = "22222222-2222-2222-2222-222222222222"
    calls = []

    def fake(sql, params):
        calls.append((sql, params))
        if "FROM traces_replacing" in sql:
            return [{"trace_id": "t1", "session_id": sid, "env": "prod"},
                    {"trace_id": "t2", "session_id": sid, "env": ""}]
        return [{"trace_id": "t2"}]

    monkeypatch.setattr(ci, "_ch", fake)
    out = ci.telemetry_signals([sid, "not-a-uuid"], datetime.now(timezone.utc) - timedelta(days=2))
    assert out == {sid: {"env": "prod", "handoff": True}}
    assert len(calls) == 2
    assert calls[0][1]["sids"] == "['%s']" % sid  # junk ids never reach SQL
    assert calls[0][1]["hours"] >= 48
    assert "handoff_re" in calls[1][1]


def test_telemetry_signals_degrade_to_empty(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("clickhouse down")
    monkeypatch.setattr(ci, "_ch", boom)
    assert ci.telemetry_signals(["22222222-2222-2222-2222-222222222222"],
                                datetime.now(timezone.utc)) == {}


# --------------------------------------------------------------------------- #
# Sweep
# --------------------------------------------------------------------------- #

class _Q:
    """Minimal chainable supabase query double."""

    def __init__(self, store, table):
        self.store, self.table = store, table
        self.op = None

    def __getattr__(self, name):
        def chain(*a, **k):
            if name in ("update", "upsert", "select", "insert"):
                self.op = (name, a, k)
            return self
        return chain

    def execute(self):
        name = self.op[0] if self.op else None
        if self.table == "ai_background_leases" and name == "update":
            self.store.setdefault("lease_updates", []).append(self.op[1][0])
            return SimpleNamespace(data=[{"name": "x"}] if self.store.get("lease_free", True) else [])
        if self.table == "ai_conversation_insights" and name == "upsert":
            self.store["upserted"] = self.op[1][0]
            return SimpleNamespace(data=self.op[1][0])
        if self.table == "ai_chat_messages":
            return SimpleNamespace(data=self.store.get("messages", []))
        if self.table == "ai_voice_recordings":
            return SimpleNamespace(data=self.store.get("recordings", []))
        return SimpleNamespace(data=[])


class _Client:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        return _Q(self.store, name)

    def rpc(self, name, params):
        self.store.setdefault("rpc", []).append((name, params))
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=self.store.get("candidates", [])))


def test_sweep_analyses_a_bounded_batch(monkeypatch):
    sid_a = "33333333-3333-3333-3333-333333333333"
    sid_b = "44444444-4444-4444-4444-444444444444"
    store = {
        "candidates": [
            {"session_id": sid_a, "agent_kind": "support_bot", "metadata": {"environment": "prod"},
             "created_at": "2026-10-11T08:00:00+00:00", "updated_at": "2026-10-11T08:10:00+00:00"},
            {"session_id": sid_b, "agent_kind": "agents_support", "metadata": {},
             "created_at": "2026-10-11T07:00:00+00:00", "updated_at": "2026-10-11T07:10:00+00:00"},
        ],
        "messages": [
            {"session_id": sid_a, "role": "user", "content": "book me", "created_at": "1"},
            {"session_id": sid_a, "role": "assistant", "content": "done", "created_at": "2"},
            {"session_id": sid_b, "role": "user", "content": "help", "created_at": "1"},
        ],
        "recordings": [{"session_id": sid_b, "simulated": True}],
    }
    monkeypatch.setenv("GOOGLE_API_KEY", "x")
    monkeypatch.setenv("CONVERSATION_INSIGHTS_CALL_GAP_SEC", "0")
    monkeypatch.setattr(ci, "telemetry_signals", lambda sids, since: {sid_b: {"env": "staging", "handoff": False}})
    classified = []

    def fake_classify(kind, transcript, *, model_string):
        classified.append(kind)
        return {"topic": "booking", "outcome": "resolved", "language": "en"}

    monkeypatch.setattr(ci, "classify", fake_classify)
    out = ci.sweep_once(_Client(store), limit=2)
    assert out["ran"] and out["analysed"] == 2 and out["classified"] == 1
    assert out["pending"] is True  # full batch → the loop comes back sooner
    name, params = store["rpc"][0]
    assert name == "ai_conversation_insight_candidates"
    assert params["p_limit"] == 2 and params["p_retry_unclassified"] is True
    rows = {r["session_id"]: r for r in store["upserted"]}
    assert rows[sid_a]["outcome"] == "resolved" and rows[sid_a]["environment"] == "prod"
    # The simulated call is stored (so it is not re-picked) but never sent to the model.
    assert classified == ["support_bot"]
    assert rows[sid_b]["is_simulated"] is True and rows[sid_b]["environment"] == "staging"
    assert store["lease_updates"][-1]["last_result"]["analysed"] == 2


def test_sweep_skips_when_the_lease_is_held(monkeypatch):
    store = {"lease_free": False, "candidates": [{"session_id": "x"}]}
    out = ci.sweep_once(_Client(store))
    assert out["ran"] is False and "rpc" not in store


def test_sweep_without_key_does_not_call_the_model(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(ci, "telemetry_signals", lambda *a: {})
    monkeypatch.setattr(ci, "classify", lambda *a, **k: pytest.fail("model called"))
    store = {"candidates": [{"session_id": "55555555-5555-5555-5555-555555555555",
                             "agent_kind": "sales", "metadata": {}}],
             "messages": [{"session_id": "55555555-5555-5555-5555-555555555555",
                           "role": "user", "content": "hi", "created_at": "1"}]}
    out = ci.sweep_once(_Client(store), use_lease=False)
    assert out["analysed"] == 1 and out["classified"] == 0
    assert store["rpc"][0][1]["p_retry_unclassified"] is False
    assert store["upserted"][0]["outcome_source"] == "rule"


@pytest.mark.parametrize("flag,mcp,expected", [
    ("", "1", True), ("", "", False), ("0", "1", False), ("1", "", True),
])
def test_sweep_runs_on_the_control_plane_by_default(monkeypatch, flag, mcp, expected):
    monkeypatch.setenv("CONVERSATION_INSIGHTS", flag)
    monkeypatch.setenv("ENABLE_ADMIN_MCP", mcp)
    assert ci.sweep_enabled() is expected


def test_migration_is_additive_and_registers_rls():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    migration = root / "supabase/migrations/20261011120000_conversation_insights.sql"
    if not migration.exists():
        import pytest

        pytest.skip("this tree ships a schema baseline, not the migration history")
    sql = migration.read_text()
    import re

    for forbidden in (r"DROP\s+(TABLE|COLUMN)", r"ALTER\s+TABLE\s+public\.ai_chat", r"TRUNCATE",
                      r"ALTER\s+TYPE"):
        assert not re.search(r"^\s*" + forbidden, sql, re.IGNORECASE | re.MULTILINE), forbidden
        assert not re.search(r";\s*" + forbidden, sql, re.IGNORECASE), forbidden
    assert "CREATE TABLE IF NOT EXISTS public.ai_conversation_insights" in sql
    rls = (root / "supabase/db-functions/rls/agent_tables.ts").read_text()
    assert "'ai_conversation_insights'" in rls and "'ai_background_leases'" in rls


# --------------------------------------------------------------------------- #
# Overview payload
# --------------------------------------------------------------------------- #

_ROWS = [
    {"period": "cur", "agent_kind": "sample_agent", "outcome": "resolved", "topic": "booking",
     "language": "ar", "channel": "chat", "n": 6},
    {"period": "cur", "agent_kind": "sample_agent", "outcome": "abandoned", "topic": "prices",
     "language": "en", "channel": "chat", "n": 2},
    {"period": "cur", "agent_kind": "sample_helper", "outcome": "escalated", "topic": "booking",
     "language": "ar", "channel": "voice", "n": 1},
    {"period": "cur", "agent_kind": "sample_helper", "outcome": "unknown", "topic": None,
     "language": None, "channel": "chat", "n": 1},
    {"period": "prev", "agent_kind": "sample_agent", "outcome": "resolved", "topic": "booking",
     "language": "ar", "channel": "chat", "n": 2},
    {"period": "prev", "agent_kind": "sample_agent", "outcome": "abandoned", "topic": "other",
     "language": "en", "channel": "chat", "n": 2},
]


def test_summarize_outcomes_topics_languages():
    from admin.screens.insights import summarize

    out = summarize(_ROWS, agent_names={"sample_agent": "Lina"})
    assert out["has_data"] and out["total"] == 10 and out["prev_total"] == 4
    oc = {o["key"]: o for o in out["outcomes"]}
    assert [o["key"] for o in out["outcomes"]] == ["resolved", "escalated", "abandoned", "unknown"]
    assert oc["resolved"]["pct"] == 60.0 and oc["resolved"]["label"] == "Resolved"
    assert oc["resolved"]["delta"] == {"text": "+10.0 pts", "tone": "ok"}
    assert oc["abandoned"]["delta"]["tone"] == "ok"  # 50% → 20%: fewer left early
    assert oc["abandoned"]["label"] == "Left before finishing"
    # Topics merge across agents on the plain label; unclassified are counted apart.
    topics = {t["label"]: t for t in out["topics"]}
    assert topics["Booking an appointment"]["count"] == 7
    assert out["topics_unclassified"] == 1 and out["topics_classified"] == 9
    assert out["topics"][0]["bar"] == 100
    langs = {l["label"]: l["count"] for l in out["languages"]}
    assert langs == {"Arabic": 7, "English": 2, "Unknown": 1}
    assert out["channels"] == {"chat": 9, "voice": 1}
    lina = out["by_kind"]["sample_agent"]
    assert lina["name"] == "Lina" and lina["resolved_text"] == "75"
    assert lina["top_topic"] == "Booking an appointment"


def test_summarize_puts_other_last():
    from admin.screens.insights import summarize

    rows = [{"period": "cur", "agent_kind": "sample_other", "outcome": "unknown", "topic": "other",
             "language": "en", "channel": "chat", "n": 9},
            {"period": "cur", "agent_kind": "sample_other", "outcome": "unknown", "topic": "features",
             "language": "en", "channel": "chat", "n": 1}]
    out = summarize(rows)
    assert [t["label"] for t in out["topics"]] == ["What the product does", "Something else"]


def test_build_insights_unavailable_is_not_zero(monkeypatch):
    from admin.screens import insights

    monkeypatch.setattr(insights, "_rollup", MagicMock(side_effect=RuntimeError("no function")))
    out = insights.build_insights(env="prod", hours=24, client=object())
    assert out["available"] is False and out["has_data"] is False


def test_build_insights_is_scoped_to_the_partner_build(monkeypatch):
    from admin.screens import insights

    seen = []
    monkeypatch.setattr(insights, "_rollup",
                        lambda client, hours, env, kinds: seen.append((hours, env, kinds)) or [])
    monkeypatch.setattr("admin.screens.overview._client_kinds", lambda: ("orchard", "orchard_outbound"))
    out = insights.build_insights(env="prod", hours=168, client=object())
    assert out["available"] is True and out["has_data"] is False
    assert out["collecting_text"].startswith("Collecting data")
    assert seen[-1] == (168, "prod", ("orchard", "orchard_outbound"))
    insights.build_insights(env="prod", hours=24, client=object(), agent_kind="support_bot")
    assert seen[-1][2] == ("__none__",)  # another partner's agent: nothing
    insights.build_insights(env="prod", hours=24, client=object(), agent_kind="orchard")
    assert seen[-1][2] == ("orchard",)


# --------------------------------------------------------------------------- #
# Rendered Overview
# --------------------------------------------------------------------------- #

def _render_overview(client, rows):
    from tests.ch_fake import make_ch_query

    with patch("core.lmnr_clickhouse.query", make_ch_query()), \
            patch("admin.screens.insights._rollup", return_value=rows):
        return client.get("/admin/live?period=7d")


def test_overview_shows_collecting_state_instead_of_hiding(client):
    r = _render_overview(client, [])
    assert r.status_code == 200
    assert 'id="insights"' in r.text
    assert "Collecting data — appears after new conversations" in r.text


def test_overview_renders_outcome_mix_topics_and_languages(client):
    r = _render_overview(client, _ROWS)
    assert r.status_code == 200
    html = r.text
    for text in ("How conversations ended", "Left before finishing", "Handed to a person",
                 "Top topics", "Booking an appointment", "Customer language", "Arabic",
                 "an estimate", "How is this worked out?"):
        assert text in html, text
    assert "Collecting data" not in html


# --------------------------------------------------------------------------- #
# Admin MCP parity
# --------------------------------------------------------------------------- #

def test_mcp_conversation_insights_uses_the_portal_builder(monkeypatch):
    from admin_mcp import tools_observe

    captured = {}

    def fake_build(**kw):
        captured.update(kw)
        return {"available": True, "has_data": False, "by_kind": {}}

    monkeypatch.setattr("admin.screens.insights.build_insights", fake_build)
    monkeypatch.setattr(tools_observe, "client_for_env", lambda env: "client-" + env)
    monkeypatch.setattr("admin.screens.plain_language.load_agent_display_names", lambda _e: {})
    fn = getattr(tools_observe.conversation_insights, "fn", tools_observe.conversation_insights)
    out = fn(period="30d", trace_env="staging", agent_kind="sales")
    assert captured["hours"] == 720 and captured["env"] == "staging"
    assert captured["agent_kind"] == "sales" and captured["client"] == "client-staging"
    assert out["period"] == "30d" and "by_kind" not in out
