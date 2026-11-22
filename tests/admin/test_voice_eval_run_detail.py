"""Tests for rendering `voice_live` eval-run conversations in the portal.

Before this, `eval_run_detail.html` rendered NOTHING from a voice scenario's
`scorer_detail` (transcript, per-turn timing, limit_breaches, checks,
proposals — see `admin/voice_eval_runner.py:212-226`), so a judged
conversation showed as one flattened string and a scenario that failed
purely on a hard-gate `limit_breaches` entry read as a bare "fail" with no
explanation.

Covers:
  * `group_results_by_scenario()` already carries the full raw result row
    (including `scorer_detail`, when present) into `attempts` — it's the
    minimal `run_chart_data()` projection that strips fields, not this one.
    Pinned here so a future refactor can't accidentally start reshaping it.
  * `eval_run_detail.html` renders the transcript (role-labelled, `dir="auto"`
    for Arabic), per-turn timing table (+ max latency), limit-breach chips,
    checks table, and agent proposals/missing_proposals — only when
    `scorer_detail` is present; a chat-only run's attempts (no `scorer_detail`
    key at all) render exactly as before.
  * `eval_suite_edit.html`'s add-scenario form carries the new advanced
    "Scenario JSON" textarea, and `eval_suite_save()` merges it into the new
    scenario dict (explicit id/category/question/expected fields win on the
    four keys they share; everything else — `caller`/`limits`/`checks`/
    `expected_proposals` — passes through untouched).
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


# ---------------------------------------------------------------------------
# group_results_by_scenario carries scorer_detail through unchanged
# ---------------------------------------------------------------------------


def _voice_scorer_detail() -> Dict[str, Any]:
    return {
        "mode": "sim",
        "turns": 2,
        "termination": "hangup",
        "audio_bytes_received": 12345,
        "target": "orchard",
        "per_turn": [
            {"turn": 1, "latency_ms": 900, "turn_ms": 1500, "turn_end_seen": True},
            {"turn": 2, "latency_ms": 16000, "turn_ms": 17000, "turn_end_seen": False},
        ],
        "transcript": [
            {"role": "caller", "text": "مرحبا، أريد تغيير الآيبان"},
            {"role": "assistant", "text": "Sure, may I have your terminal ID?"},
        ],
        "checks": [
            {"step": 0, "type": "http_check", "ok": False, "url": "http://x", "body_head": "not found"},
        ],
        "limit_breaches": [
            {"turn": 2, "kind": "max_latency_ms", "value": 16000, "limit": 15000},
        ],
        "proposals": [{"type": "address_change", "confidence": 0.9}],
        "missing_proposals": ["router_order"],
    }


def test_group_results_by_scenario_carries_scorer_detail_when_present():
    results = [
        {
            "scenario_id": "iban_change", "question": None, "category": "voice",
            "passed": False, "score": 0.4, "elapsed_ms": 1200, "reason": "partial",
            "scorer_detail": _voice_scorer_detail(),
        },
    ]
    groups = eval_handlers.group_results_by_scenario(results)
    assert len(groups) == 1
    attempt = groups[0]["attempts"][0]
    assert attempt["scorer_detail"] == _voice_scorer_detail()


def test_group_results_by_scenario_omits_scorer_detail_for_chat_runs():
    """A chat-run result row has no `scorer_detail` key at all — the
    projection must not fabricate one."""
    results = [
        {"scenario_id": "hours_check", "question": "hi", "category": "grounding",
         "passed": True, "score": 1.0},
    ]
    groups = eval_handlers.group_results_by_scenario(results)
    attempt = groups[0]["attempts"][0]
    assert "scorer_detail" not in attempt
    assert attempt.get("scorer_detail") is None


# ---------------------------------------------------------------------------
# eval_run_detail.html renders the voice-eval scorer_detail
# ---------------------------------------------------------------------------


def _render_run_detail(results: List[Dict[str, Any]]) -> str:
    from admin.router import templates

    groups = eval_handlers.group_results_by_scenario(results)
    return templates.get_template("eval_run_detail.html").render(
        ctx={"email": "t@waj.local"},
        active="evals",
        current_env="dev",
        run={
            "run_id": "11111111-2222-3333-4444-555555555555",
            "status": "failed", "score": 0.4, "results": results,
            "started_at": None, "finished_at": None, "suite_id": None,
            "context": {},
        },
        run_kpis=None,
        chart_data=[],
        scenario_groups=groups,
        lmnr_spans=[],
        sibling_runs=[],
        cancel_job_id=None,
        asset_version="1",
    )


def test_run_detail_renders_voice_scorer_detail():
    results = [
        {
            "scenario_id": "iban_change", "question": None, "category": "voice",
            "passed": False, "score": 0.4, "elapsed_ms": 1200, "reason": "partial",
            "scorer_detail": _voice_scorer_detail(),
        },
    ]
    html = _render_run_detail(results)

    # Conversation (the voice transcript): role-labelled, in order, RTL-safe.
    assert "Conversation (2 turns)" in html
    assert "مرحبا، أريد تغيير الآيبان" in html
    assert "Sure, may I have your terminal ID?" in html
    assert 'dir="auto"' in html
    assert html.index("مرحبا") < html.index("Sure, may I have your terminal ID")

    # Per-turn timing table + slowest reply called out.
    assert "Per-turn timing" in html
    assert "slowest first reply 16.0 s" in html

    # Limit breaches — in "Why it failed", in words, technical name muted.
    assert "Why it failed" in html
    assert "Took too long to start replying" in html
    assert "max_latency_ms" in html
    assert "16000" in html and "15000" in html

    # Checks table.
    assert "Checks" in html
    assert "http://x" in html
    assert "not found" in html

    # Agent proposals.
    assert "Expected but never offered" in html
    assert "router_order" in html
    assert "Actions offered (1)" in html

    # Meta line.
    assert "Simulated call" in html
    assert "hangup" in html
    assert "12,345" in html  # audio bytes

    # Raw JSON fallback for anything not explicitly rendered.
    assert "Raw result (JSON)" in html


def test_run_detail_chat_only_run_unaffected():
    """A chat-only run (no scorer_detail anywhere) must render identically
    to before — no stray 'Voice call' section, no crash on a missing key."""
    results = [
        {"scenario_id": "hours_check", "question": "What are your hours?",
         "category": "grounding", "passed": True, "score": 1.0,
         "actual": "We are open 9-5.", "reason": "matches"},
    ]
    html = _render_run_detail(results)
    assert "hours_check" in html
    assert "We are open 9-5." in html
    assert "Simulated call" not in html
    assert "Conversation (" not in html
    assert "Per-turn timing" not in html


def test_run_detail_no_limit_breaches_key_absent_renders_no_breach_chip():
    """`limit_breaches` is absent entirely when the scenario has no
    `limits` (per admin/voice_eval_runner.py) — the template must not
    render an empty/failing breach block in that case."""
    sd = _voice_scorer_detail()
    del sd["limit_breaches"]
    del sd["missing_proposals"]
    results = [
        {"scenario_id": "data_question", "question": None, "category": "voice",
         "passed": True, "score": 0.9, "scorer_detail": sd},
    ]
    html = _render_run_detail(results)
    assert "Took too long" not in html
    assert "Expected but never offered" not in html
    # Everything else still renders.
    assert "Conversation (2 turns)" in html


# ---------------------------------------------------------------------------
# eval_suite_edit.html: advanced Scenario JSON textarea present
# ---------------------------------------------------------------------------


def test_suite_edit_renders_advanced_scenario_json_field():
    from admin.router import templates

    html = templates.get_template("eval_suite_edit.html").render(
        ctx={"email": "t@waj.local"}, active="evals", current_env="dev",
        suite={"suite_id": "s1", "name": "Test", "scenarios": [], "pass_threshold": 0.7,
               "is_active": True, "runner_kind": "voice_live", "evaluator_model_id": None,
               "agent_kind": "orchard", "default_context": {}},
        scenarios=[], models=[], profiles=[], imported_count=None, businesses=[],
    )
    assert 'name="new_scenario_json"' in html
    assert "Technical: case JSON (optional)" in html


# ---------------------------------------------------------------------------
# eval_suite_save(): new_scenario_json merges into the new scenario dict
# ---------------------------------------------------------------------------


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


async def test_eval_suite_save_merges_advanced_scenario_json(monkeypatch):
    client = _SuiteClient([{"suite_id": "s1", "name": "n", "pass_threshold": 0.7,
                             "scenarios": []}])
    monkeypatch.setattr(eval_handlers, "get_service_client", lambda: client)
    monkeypatch.setattr(eval_handlers, "record_change", lambda **_k: None)
    req = _FakeRequest(_FakeForm({
        "pass_threshold": "0.7",
        "new_scenario_json": (
            '{"caller": {"persona": "confused merchant", "goal": "change IBAN", '
            '"max_turns": 6}, "limits": {"max_latency_ms": 15000}, '
            '"expected_proposals": ["address_change"]}'
        ),
    }))

    await eval_handlers.eval_suite_save(suite_id="s1", request=req, ctx={"user_id": "u1"})

    assert client.updated, "a valid save should persist an update"
    scenarios = client.updated[0]["scenarios"]
    assert len(scenarios) == 1
    sc = scenarios[0]
    # JSON-only keys survive untouched.
    assert sc["caller"] == {"persona": "confused merchant", "goal": "change IBAN", "max_turns": 6}
    assert sc["limits"] == {"max_latency_ms": 15000}
    assert sc["expected_proposals"] == ["address_change"]
    # The four plain-form keys still exist (defaulted to empty since no
    # plain fields were submitted and the JSON didn't set them either).
    assert sc["question"] == ""
    assert sc["category"] == ""
    assert sc["expected_answer"] == ""
    assert sc["id"]  # auto-slugified fallback, never empty


async def test_eval_suite_save_plain_fields_win_over_json_on_shared_keys(monkeypatch):
    client = _SuiteClient([{"suite_id": "s1", "name": "n", "pass_threshold": 0.7,
                             "scenarios": []}])
    monkeypatch.setattr(eval_handlers, "get_service_client", lambda: client)
    monkeypatch.setattr(eval_handlers, "record_change", lambda **_k: None)
    req = _FakeRequest(_FakeForm({
        "pass_threshold": "0.7",
        "new_scenario_id": "iban_change",
        "new_scenario_category": "voice",
        "new_scenario_question": "Change my IBAN",
        "new_scenario_json": '{"id": "json_id", "category": "json_cat", "question": "json question", "caller": {"goal": "x"}}',
    }))

    await eval_handlers.eval_suite_save(suite_id="s1", request=req, ctx={"user_id": "u1"})

    sc = client.updated[0]["scenarios"][0]
    assert sc["id"] == "iban_change"
    assert sc["category"] == "voice"
    assert sc["question"] == "Change my IBAN"
    assert sc["caller"] == {"goal": "x"}  # non-conflicting JSON key still present


async def test_eval_suite_save_no_new_scenario_when_both_empty(monkeypatch):
    """Neither a question nor advanced JSON submitted — no scenario added
    (unchanged pre-existing behaviour, just re-pinned against the new
    `or new_json_raw` guard)."""
    client = _SuiteClient([{"suite_id": "s1", "name": "n", "pass_threshold": 0.7,
                             "scenarios": []}])
    monkeypatch.setattr(eval_handlers, "get_service_client", lambda: client)
    monkeypatch.setattr(eval_handlers, "record_change", lambda **_k: None)
    req = _FakeRequest(_FakeForm({"pass_threshold": "0.7"}))

    await eval_handlers.eval_suite_save(suite_id="s1", request=req, ctx={"user_id": "u1"})

    assert client.updated[0]["scenarios"] == []


async def test_eval_suite_save_malformed_json_ignored_not_500(monkeypatch):
    """Invalid JSON in the advanced textarea must not blow up the save —
    it's dropped, same spirit as scenario_extra_{idx}'s existing guard."""
    client = _SuiteClient([{"suite_id": "s1", "name": "n", "pass_threshold": 0.7,
                             "scenarios": []}])
    monkeypatch.setattr(eval_handlers, "get_service_client", lambda: client)
    monkeypatch.setattr(eval_handlers, "record_change", lambda **_k: None)
    req = _FakeRequest(_FakeForm({
        "pass_threshold": "0.7",
        "new_scenario_question": "still added",
        "new_scenario_json": "{not valid json",
    }))

    await eval_handlers.eval_suite_save(suite_id="s1", request=req, ctx={"user_id": "u1"})

    sc = client.updated[0]["scenarios"][0]
    assert sc["question"] == "still added"
