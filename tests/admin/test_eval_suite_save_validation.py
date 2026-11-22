"""Regression test for the eval_suite_save pass_threshold guard (evals-4).

The create path (eval_suite_create) rejected an out-of-range pass_threshold,
but the SAVE path silently persisted it — a negative threshold makes every run
"pass" and neuters the promotion eval-gate. eval_suite_save now mirrors the
create-path [0,1] validation; these tests pin it.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

from admin import eval_handlers


class _FakeForm:
    """Minimal Starlette-FormData stand-in (get + getlist)."""

    def __init__(self, data: Dict[str, Any]):
        self._data = data

    def get(self, key, default=None):
        return self._data.get(key, default)

    def getlist(self, key):
        v = self._data.get(key, [])
        return v if isinstance(v, list) else []


class _FakeRequest:
    def __init__(self, form: _FakeForm):
        self._form = form

    async def form(self):
        return self._form


class _SuiteClient:
    """Fake supabase client: select returns one existing suite; update is
    recorded so the test can assert whether the bad value was persisted."""

    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows
        self.updated: List[Dict[str, Any]] = []
        self._op = "select"
        self._payload = None

    def table(self, *_a, **_k):
        self._op = "select"
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def execute(self):
        if self._op == "update":
            self.updated.append(self._payload)
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=self._rows)


@pytest.mark.parametrize("bad", ["1.5", "-0.1", "5"])
async def test_eval_suite_save_rejects_out_of_range_threshold(monkeypatch, bad):
    client = _SuiteClient([{"suite_id": "s1", "name": "n", "pass_threshold": 0.7}])
    monkeypatch.setattr(eval_handlers, "get_service_client", lambda: client)
    req = _FakeRequest(_FakeForm({"pass_threshold": bad}))

    with pytest.raises(HTTPException) as ei:
        await eval_handlers.eval_suite_save(suite_id="s1", request=req, ctx={"user_id": "u1"})
    assert ei.value.status_code == 400
    # The bad threshold must never have been persisted.
    assert client.updated == []


async def test_eval_suite_save_accepts_in_range_threshold(monkeypatch):
    client = _SuiteClient([{"suite_id": "s1", "name": "n", "pass_threshold": 0.7}])
    monkeypatch.setattr(eval_handlers, "get_service_client", lambda: client)
    monkeypatch.setattr(eval_handlers, "record_change", lambda **_k: None)
    req = _FakeRequest(_FakeForm({"pass_threshold": "0.8"}))

    resp = await eval_handlers.eval_suite_save(suite_id="s1", request=req, ctx={"user_id": "u1"})
    assert resp.status_code in (302, 303)
    assert client.updated, "a valid save should persist an update"
    assert client.updated[0]["pass_threshold"] == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# Finding 3a: saving a suite must not silently wipe its top-level
# default_context.voice block (the eval_suite_save form has no field for
# it — it's editor-only via the Advanced JSON box).
# ---------------------------------------------------------------------------

async def test_eval_suite_save_preserves_existing_voice_block(monkeypatch):
    client = _SuiteClient([{
        "suite_id": "s1", "name": "n", "pass_threshold": 0.7,
        "default_context": {"business_id": "b1", "voice": {"greeting_trigger": "Hi"}},
    }])
    monkeypatch.setattr(eval_handlers, "get_service_client", lambda: client)
    monkeypatch.setattr(eval_handlers, "record_change", lambda **_k: None)
    # No default_context_json in the form at all — mirrors the plain
    # settings-tab save a seeded voice_live suite would get.
    req = _FakeRequest(_FakeForm({"pass_threshold": "0.8"}))

    await eval_handlers.eval_suite_save(suite_id="s1", request=req, ctx={"user_id": "u1"})

    assert client.updated, "a valid save should persist an update"
    saved_ctx = client.updated[0]["default_context"]
    assert saved_ctx["voice"] == {"greeting_trigger": "Hi"}


async def test_eval_suite_save_advanced_json_voice_wins_over_existing(monkeypatch):
    client = _SuiteClient([{
        "suite_id": "s1", "name": "n", "pass_threshold": 0.7,
        "default_context": {"voice": {"greeting_trigger": "Old"}},
    }])
    monkeypatch.setattr(eval_handlers, "get_service_client", lambda: client)
    monkeypatch.setattr(eval_handlers, "record_change", lambda **_k: None)
    req = _FakeRequest(_FakeForm({
        "pass_threshold": "0.8",
        "default_context_json": '{"voice": {"greeting_trigger": "New"}, "screen_context": "s1"}',
    }))

    await eval_handlers.eval_suite_save(suite_id="s1", request=req, ctx={"user_id": "u1"})

    saved_ctx = client.updated[0]["default_context"]
    # New value wins, and it lands top-level, not nested under extra.
    assert saved_ctx["voice"] == {"greeting_trigger": "New"}
    assert saved_ctx["extra"] == {"screen_context": "s1"}
    assert "voice" not in saved_ctx["extra"]


async def test_eval_suite_save_no_voice_anywhere_stays_absent(monkeypatch):
    client = _SuiteClient([{
        "suite_id": "s1", "name": "n", "pass_threshold": 0.7,
        "default_context": {"business_id": "b1"},
    }])
    monkeypatch.setattr(eval_handlers, "get_service_client", lambda: client)
    monkeypatch.setattr(eval_handlers, "record_change", lambda **_k: None)
    req = _FakeRequest(_FakeForm({"pass_threshold": "0.8"}))

    await eval_handlers.eval_suite_save(suite_id="s1", request=req, ctx={"user_id": "u1"})

    saved_ctx = client.updated[0]["default_context"]
    assert "voice" not in saved_ctx
