"""admin/_model_picker.py must never surface a realtime-flagged model
(extra_config.realtime) as something an operator can pick as a chat brain —
see admin/screens/agents.py for the sibling model_main_id dropdown query,
which shares the same `is_chat_model` predicate.
"""
from __future__ import annotations


class _FakeTable:
    def __init__(self, rows):
        self._rows = list(rows)
        self._filters = {}

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        rows = [r for r in self._rows if all(r.get(k) == v for k, v in self._filters.items())]
        return type("R", (), {"data": rows})


class _FakeClient:
    def __init__(self, rows):
        self._rows = rows

    def table(self, _name):
        return _FakeTable(self._rows)


def _rows():
    return [
        {
            "model_id": "chat-1", "name": "Gemini 3.5 Flash", "model_string": "gemini-3.5-flash",
            "provider": "google_genai", "is_active": True, "environment": "staging",
            "extra_config": {"location": "global", "pricing_mode": "auto"},
        },
        {
            "model_id": "voice-1", "name": "Gemini Live (duplex voice)",
            "model_string": "gemini-2.5-flash-native-audio-preview-09-2025",
            "provider": "google_genai", "is_active": True, "environment": "staging",
            "extra_config": {"location": "global", "pricing_mode": "auto", "realtime": True},
        },
    ]


def test_is_chat_model_excludes_realtime_flagged_rows():
    from admin._model_picker import is_chat_model

    chat, voice = _rows()
    assert is_chat_model(chat) is True
    assert is_chat_model(voice) is False


def test_fetch_models_for_picker_excludes_realtime_row(monkeypatch):
    import admin._model_picker as picker

    monkeypatch.setattr(picker, "admin_service_client", lambda: _FakeClient(_rows()))

    result = picker.fetch_models_for_picker(environment="staging")
    ids = {m["model_id"] for m in result}

    assert "chat-1" in ids
    assert "voice-1" not in ids
