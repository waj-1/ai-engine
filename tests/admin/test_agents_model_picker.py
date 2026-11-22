"""load_agents_context's `all_models` (the model_main_id dropdown behind
agents.html, admin/screens/agents.py:344-380) must exclude realtime-flagged
agent_models rows -- an operator picking one for model_main_id would break
every text turn for that profile (runtime/llm_factory.py builds a chat
completion call against a native-audio-dialog model id).
"""
from __future__ import annotations

from typing import Any, Dict, List


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self._rows = list(rows)

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def in_(self, col, vals):
        allowed = set(vals)
        self._rows = [r for r in self._rows if r.get(col) in allowed]
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        return _Result(self._rows)


class FakeSupabase:
    def __init__(self, tables: Dict[str, List[Dict[str, Any]]]):
        self._tables = tables

    def table(self, name):
        return _Query(self._tables.get(name, []))


def _tables() -> Dict[str, List[Dict[str, Any]]]:
    return {
        "agent_profiles": [
            {"profile_id": "p1", "agent_kind": "sales", "environment": "staging",
             "status": "active", "is_active": True},
        ],
        "agent_models": [
            {"model_id": "chat-1", "name": "Gemini 3.5 Flash", "provider": "google_genai",
             "model_string": "gemini-3.5-flash", "is_active": True, "environment": "staging",
             "extra_config": {"location": "global", "pricing_mode": "auto"}},
            {"model_id": "voice-1", "name": "Gemini Live (duplex voice)", "provider": "google_genai",
             "model_string": "gemini-2.5-flash-native-audio-preview-09-2025",
             "is_active": True, "environment": "staging",
             "extra_config": {"location": "global", "pricing_mode": "auto", "realtime": True}},
        ],
    }


def test_all_models_excludes_realtime_flagged_row(monkeypatch):
    import admin.screens.agents as ag

    fake = FakeSupabase(_tables())
    monkeypatch.setattr("admin.router.client_for", lambda env: fake)

    ctx = ag.load_agents_context(env="staging", selected_kind=None)
    ids = {m["model_id"] for m in ctx["all_models"]}

    assert "chat-1" in ids
    assert "voice-1" not in ids


def test_all_models_still_shows_a_realtime_row_if_already_assigned(monkeypatch):
    """The 'assigned model must always be present' escape hatch (so a save
    without touching the field is a no-op, and a pre-existing bad
    assignment is visible to fix) is NOT itself realtime-filtered."""
    import admin.screens.agents as ag

    tables = _tables()
    tables["agent_profiles"][0]["model_main_id"] = "voice-1"
    fake = FakeSupabase(tables)
    monkeypatch.setattr("admin.router.client_for", lambda env: fake)

    ctx = ag.load_agents_context(env="staging", selected_kind=None)
    ids = {m["model_id"] for m in ctx["all_models"]}

    assert "voice-1" in ids


def test_model_pickers_ask_the_database_for_the_flag_they_filter_on():
    """`is_chat_model` reads `extra_config.realtime`. The agent page's query
    did not select `extra_config`, so against a real database the filter saw
    no flag and offered the duplex-voice model as a main model; the fake
    client in the tests above returns every column whatever is selected."""
    import inspect

    from admin import eval_runs_handlers
    from admin.screens import agents

    cols = inspect.getsource(agents).split('_model_cols = "', 1)[1].split('"', 1)[0]
    assert "extra_config" in cols
    src = inspect.getsource(eval_runs_handlers)
    assert 'select("model_id, name, provider, is_active, extra_config")' in src
    assert "_is_chat_model(m)" in src
