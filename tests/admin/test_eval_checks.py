"""admin/services/eval_checks.py — deterministic per-turn checks for chat and voice evals."""
from __future__ import annotations

import json
from datetime import date

import pytest

from admin.services import eval_checks as ec


def _one(check, **kw):
    kw.setdefault("business_id", "biz")
    kw.setdefault("user_token", "tok")
    return ec.run_checks([check], **kw)[0]


@pytest.mark.parametrize("answer,ok", [
    ("Branch ID: ea22654d-00ca-4379-b83e-05c5e40ef45e — on screen", False),
    ("Use ea22654d-… for that", False),
    ("Revenue is AED 11,216 on 2026-09-29.", True),
])
def test_no_uuid(answer, ok):
    assert _one({"type": "no_uuid"}, answer=answer)["ok"] is ok


def test_no_question_about_a_branch():
    check = {"type": "no_question", "pattern": "branch|فرع"}
    assert not _one(check, answer="Do you want the whole business or a specific branch?")["ok"]
    assert _one(check, answer="Revenue is AED 5. Want the daily breakdown?")["ok"]


def test_asks_accepts_a_statement_clarification():
    assert _one({"type": "asks"}, answer="I need the time to block for Sara tomorrow.")["ok"]
    assert not _one({"type": "asks"}, answer="Done.")["ok"]


def test_tools_any():
    assert _one({"type": "tools_any", "tools": ["get_bookings"]}, answer="", tools=["get_bookings"])["ok"]
    assert not _one({"type": "tools_any", "tools": ["get_bookings"]}, answer="", tools=[])["ok"]


def test_no_saved_claim():
    assert not _one({"type": "no_saved_claim"}, answer="Done — I blocked Sara.")["ok"]
    assert not _one({"type": "no_saved_claim"}, answer="تم الحفظ")["ok"]
    assert _one({"type": "no_saved_claim"}, answer="Nothing is saved by voice — tap Confirm.")["ok"]


def test_a_broken_check_fails_instead_of_raising():
    res = _one({"type": "regex"}, answer="x")       # missing pattern
    assert res["ok"] is False and "KeyError" in res["detail"]


def test_a_plugin_check_type_grades_against_the_run_facts(monkeypatch):
    """`proposal`, `truth` and the like are a plugin's: it supplies the check
    and the facts it reads; the engine resolves placeholders and calls it."""
    from types import SimpleNamespace
    from core import plugins

    def card_starts(check, turn):
        want = turn.resolve(check["start"])
        got = [p.get("start") for p in turn.proposals]
        return got == [want], f"cards {got}, want {want}"

    module = SimpleNamespace(
        facts=lambda business_id, user_token: {
            "truths": {}, "ids": {"room:Blue": "room-1"}, "today": date(2026, 9, 29)},
        check_types=lambda: {"card_starts": card_starts},
        status_tool_labels=lambda: {"get_rooms": ("rooms", "الغرف")},
    )
    monkeypatch.setattr(plugins, "load_hook", lambda hook: [module] if hook == "eval_checks" else [])
    check = {"type": "card_starts", "start": "{d+1}T15:00 {room:Blue}"}
    assert _one(check, answer="", proposals=[{"start": "2026-09-30T15:00 room-1"}])["ok"]
    assert not _one(check, answer="", proposals=[{"start": "2026-09-30T16:00 room-1"}])["ok"]
    assert ec.tools_from_status_lines(["Using rooms..."]) == ["get_rooms"]

    monkeypatch.setattr(plugins, "load_hook", lambda hook: [])
    res = _one(check, answer="", proposals=[])
    assert res["ok"] is False and "unknown check type" in res["detail"]


def test_proposals_from_sse():
    body = "\n".join("data: " + json.dumps(e) for e in [
        {"type": "thought", "content": "thinking"},
        {"type": "proposal", "proposal": {"id": "p1", "kind": "sample"}},
        {"type": "answer", "content": "ok"},
    ])
    assert ec.proposals_from_sse(body) == [{"id": "p1", "kind": "sample"}]
