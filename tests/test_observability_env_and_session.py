"""Regression tests for two Live-Monitor / Observability fixes:

Issue #1 — the Live Monitor KPI tiles ignored the selected environment, so
switching STAGING/PRODUCTION never changed the numbers. ``load_live_insights``
must env-scope its trace rollups (``_item_env_matches``).

Issue #2 — a trace's ``waj.session_id`` is the RAW client id, but the chat
tables persist under ``coerce_session_id`` (non-UUID → deterministic UUIDv5).
The transcript reader queried the raw id and missed the coerced row, so
conversational agents (public chat widgets with non-UUID ids) fell back
to telemetry reconstruction. It must coerce on read. One-shot runs may
persist their structured result in message metadata — surface it. And the
"reconstructed" notice must read as informational for genuinely non-chat kinds.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

AI_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


_CHAIN_METHODS = ("select", "eq", "order", "limit", "insert", "upsert", "update")


def _make_client(rows):
    """Mock Supabase client whose ai_chat_messages chain returns ``rows`` and
    records the filter calls so tests can assert what session_id was queried."""
    client = MagicMock()
    chain = MagicMock(name="chain")
    for m in _CHAIN_METHODS:
        getattr(chain, m).return_value = chain
    chain.execute.return_value = MagicMock(data=rows, error=None)
    client.schema.return_value = client
    client.table.return_value = chain
    return client, chain


# --------------------------------------------------------------------------- #
# Issue #2a — coerce session id on read
# --------------------------------------------------------------------------- #
def test_persisted_conversation_coerces_non_uuid_session_id():
    """A non-UUID trace session id must be mapped through coerce_session_id
    before querying, matching how chat_persistence writes it."""
    from admin.screens import observability
    from core.chat_persistence import coerce_session_id

    raw = "visitor_abc-123"           # a public chat widget's style of id
    expected = coerce_session_id(raw)  # deterministic UUIDv5
    assert expected != raw

    client, chain = _make_client([
        {"role": "user", "content": "hi", "metadata": {}, "created_at": "t1"},
        {"role": "assistant", "content": "hello", "metadata": {}, "created_at": "t2"},
    ])
    with patch("core.auth.get_service_client", return_value=client):
        turns = observability._persisted_conversation(raw, None)

    # The query filtered on the COERCED id, not the raw one.
    chain.eq.assert_any_call("session_id", expected)
    assert [t["role"] for t in turns] == ["user", "assistant"]


def test_persisted_conversation_passes_uuid_session_id_unchanged():
    """A real UUID coerces to itself, so UUID sessions keep working."""
    from admin.screens import observability

    sid = "a4c1f2e0-7b3d-4e6a-9c2f-3d8b1e5a6c7d"
    client, chain = _make_client([])
    with patch("core.auth.get_service_client", return_value=client):
        observability._persisted_conversation(sid, None)
    chain.eq.assert_any_call("session_id", sid)


# --------------------------------------------------------------------------- #
# Issue #2b — surface a structured result stashed in message metadata
# --------------------------------------------------------------------------- #
def _fake_portal_display_hook(monkeypatch):
    """A plugin `portal_display` hook that renders one made-up structured type."""
    import types

    import core.plugins as plugins

    hook = types.SimpleNamespace(
        message_markdown=lambda meta: (
            "**Findings**\n" + "\n".join(f"- {x}" for x in meta.get("findings", []))
            if meta.get("type") == "audit_report" else None),
        non_chat_agent_kinds=lambda: ("report_writer",),
    )
    real = plugins.load_hook
    monkeypatch.setattr(plugins, "load_hook",
                        lambda name: [hook] if name == "portal_display" else real(name))


def test_structured_message_md_comes_from_the_plugin_hook(monkeypatch):
    from admin.screens.observability import _structured_message_md

    _fake_portal_display_hook(monkeypatch)
    md = _structured_message_md({"type": "audit_report", "findings": ["Late invoice"]})
    assert md is not None and "Late invoice" in md
    assert _structured_message_md({"type": "other"}) is None
    assert _structured_message_md({}) is None


def test_structured_message_md_none_without_a_plugin(monkeypatch):
    import engine_plugins
    from admin.screens.observability import _structured_message_md

    monkeypatch.setattr(engine_plugins, "PLUGINS", ())
    assert _structured_message_md({"type": "audit_report", "findings": ["x"]}) is None


def test_persisted_conversation_surfaces_structured_content(monkeypatch):
    from admin.screens import observability

    _fake_portal_display_hook(monkeypatch)
    client, _chain = _make_client([
        {"role": "assistant", "content": "Audit done.",
         "metadata": {"type": "audit_report", "findings": ["Late invoice", "Missing receipt"]},
         "created_at": "t1"},
    ])
    with patch("core.auth.get_service_client", return_value=client):
        turns = observability._persisted_conversation("audit-1", None)

    assert len(turns) == 1
    body = turns[0]["content"]
    assert "Audit done." in body
    assert "Late invoice" in body and "Missing receipt" in body


def test_plugin_non_chat_kinds_get_the_informational_notice(monkeypatch):
    from admin.screens.observability import _reconstruction_notice

    _fake_portal_display_hook(monkeypatch)
    assert "by design" in _reconstruction_notice("report_writer").lower()


# --------------------------------------------------------------------------- #
# Issue #2c — agent-kind-aware reconstruction notice
# --------------------------------------------------------------------------- #
def test_reconstruction_notice_is_informational_for_non_chat_kinds(monkeypatch):
    from admin.screens import observability
    from admin.screens.observability import _reconstruction_notice

    monkeypatch.setattr(observability, "_NON_CHAT_AGENT_KINDS", ("voice", "sample_oneshot"))
    for kind in ("voice", "sample_oneshot"):
        note = _reconstruction_notice(kind)
        assert "by design" in note.lower()
        # Non-alarming: no scary "no persisted chat session" wording.
        assert "no persisted chat session" not in note.lower()


def test_reconstruction_notice_diagnostic_for_conversational_kinds():
    from admin.screens.observability import _reconstruction_notice

    note = _reconstruction_notice("sales")
    assert "telemetry" in note.lower()


# --------------------------------------------------------------------------- #
# Issue #1 — env scoping of the Live Monitor tiles
# --------------------------------------------------------------------------- #
def test_item_env_matches_prod_is_strict():
    from admin.screens.observability import _item_env_matches

    prod = {"metadata": '{"waj.env": "prod"}'}
    staging = {"metadata": '{"waj.env": "staging"}'}
    untagged = {"metadata": ""}

    assert _item_env_matches(prod, "prod") is True
    assert _item_env_matches(staging, "prod") is False
    # Prod is strict: an untagged trace is NOT counted as prod.
    assert _item_env_matches(untagged, "prod") is False


def test_item_env_matches_staging_admits_untagged():
    from admin.screens.observability import _item_env_matches

    staging = {"metadata": '{"waj.env": "staging"}'}
    untagged = {"metadata": ""}
    prod = {"metadata": '{"waj.env": "prod"}'}

    assert _item_env_matches(staging, "staging") is True
    # Staging admits legacy untagged traces (mirrors _filter_clauses).
    assert _item_env_matches(untagged, "staging") is True
    assert _item_env_matches(prod, "staging") is False


def test_item_env_matches_none_env_admits_all():
    from admin.screens.observability import _item_env_matches

    assert _item_env_matches({"metadata": '{"waj.env": "prod"}'}, None) is True
    assert _item_env_matches({"metadata": ""}, None) is True


def test_load_live_insights_scopes_tokens_to_env():
    """End-to-end wiring: with env='prod', only prod-tagged traces feed the
    token/latency rollups; staging traces are dropped.

    The trace-list SQL deliberately admits env-LESS traces for either env (so a
    mis-tagged conversational trace is never hidden from the explorer), which is
    why the tiles narrow again in memory via ``_item_env_matches``. The fake
    returns both envs' rows to prove that second narrow is what does the work.
    """
    from admin.screens import observability
    from tests.ch_fake import ch_trace_row, make_ch_query

    rows = [
        ch_trace_row("p1", env="prod", in_tokens=100, out_tokens=10),
        ch_trace_row("s1", env="staging", in_tokens=999, out_tokens=99),
    ]

    with patch("core.lmnr_clickhouse.query", make_ch_query(traces=rows)):
        prod = observability.load_live_insights(past_hours=1, env="prod")
        allenv = observability.load_live_insights(past_hours=1, env=None)

    # prod-only: staging's 999/99 excluded.
    assert prod["tokens"]["in_1h"] == 100
    assert prod["tokens"]["out_1h"] == 10
    # global: both counted.
    assert allenv["tokens"]["in_1h"] == 100 + 999
