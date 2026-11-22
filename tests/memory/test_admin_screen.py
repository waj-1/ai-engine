# ai_agent/tests/memory/test_admin_screen.py
from admin.screens.memory import (
    active_profile_row,
    load_config_context,
    parse_config_form,
    shape_config_for_form,
)


class _FakeQuery:
    """Records .eq() filters and returns a canned row list on .execute()."""

    def __init__(self, rows, recorder):
        self._rows = rows
        self._rec = recorder

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._rec[col] = val
        return self

    def limit(self, _n):
        return self

    def execute(self):
        class _R:
            def __init__(self, data):
                self.data = data
        return _R(self._rows)


class _FakeClient:
    def __init__(self, rows):
        self._rows = rows
        self.filters = {}

    def table(self, _name):
        return _FakeQuery(self._rows, self.filters)


def test_shape_config_for_form_fills_defaults():
    form = shape_config_for_form({})
    assert form["enabled"] is False
    assert form["retrieval_top_k"] == 6
    assert form["summary_keep_recent_verbatim"] == 4
    # window defaults surface as flat fields so the HTML form can render them
    assert form["window_max_messages"] == 10
    assert form["window_max_message_chars"] == 4000
    assert form["window_suggestion_context_messages"] == 6


def test_config_form_roundtrip_preserves_window():
    # Regression: parse_config_form must emit the window block, else saving the
    # HTML form silently wipes an operator's window settings.
    raw = {
        "window_max_messages": "20",
        "window_max_message_chars": "2000",
        "window_suggestion_context_messages": "8",
        "retrieval_top_k": "6",
    }
    cfg = parse_config_form(raw)
    assert cfg["window"] == {
        "max_messages": 20, "max_message_chars": 2000,
        "suggestion_context_messages": 8,
    }
    # and it round-trips back through the shaper
    assert shape_config_for_form(cfg)["window_max_messages"] == 20


def test_parse_config_form_roundtrips():
    raw = {
        "enabled": "on",
        "extraction_importance_threshold": "0.6",
        "extraction_max_per_turn": "4",
        "dedup_similarity_threshold": "0.92",
        "retrieval_top_k": "8",
        "retrieval_min_similarity": "0.72",
        "retrieval_always_include_pinned": "on",
        "summary_trigger_every_n_turns": "5",
        "summary_keep_recent_verbatim": "3",
    }
    cfg = parse_config_form(raw)
    assert cfg["enabled"] is True
    assert cfg["extraction"]["importance_threshold"] == 0.6
    assert cfg["retrieval"]["top_k"] == 8
    assert cfg["retrieval"]["always_include_pinned"] is True
    assert cfg["summary"]["trigger_every_n_turns"] == 5


def test_active_profile_row_scopes_to_environment():
    # Regression: prod and staging profiles share one DB, so the lookup MUST
    # filter on `environment` or it returns whichever row sorts first.
    client = _FakeClient([{"profile_id": "prod-1", "memory_config": {}}])
    row = active_profile_row(client, "sales", "prod")
    assert row["profile_id"] == "prod-1"
    assert client.filters["environment"] == "prod"
    assert client.filters["agent_kind"] == "sales"
    assert client.filters["is_active"] is True
    assert client.filters["status"] == "active"


def test_active_profile_row_no_env_omits_filter():
    # A per-env-scoped client may deliberately pass env=None.
    client = _FakeClient([{"profile_id": "x", "memory_config": {}}])
    active_profile_row(client, "sales", None)
    assert "environment" not in client.filters


def test_load_config_context_threads_env_into_query():
    client = _FakeClient([{"profile_id": "prod-1", "slug": "default", "memory_config": {}}])
    ctx = load_config_context(client, "sales", "prod")
    assert ctx["profile"]["profile_id"] == "prod-1"
    assert client.filters["environment"] == "prod"
    # window defaults still surface through the form shaper
    assert ctx["config_form"]["enabled"] is False
