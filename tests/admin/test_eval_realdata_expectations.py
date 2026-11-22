"""Real-data (live_chats) eval runs must be graded: a judge model and the
operator's expectations are mandatory on both the run and the schedule, and
the expectations are fed into the quality judge prompt.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

AI_AGENT_DIR = Path(__file__).resolve().parents[1].parent
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

from admin import eval_runs_handlers as erh


def _base_form(**over):
    form = {
        "name": "nightly real-data",
        "source": "live_chats",
        "cadence": "daily",
        "profile_id": "11111111-1111-1111-1111-111111111111",
        "agent_kind": "sample_other",
        "judge_model": "gemini-2.5-pro",
        "expectations": "Must cite real revenue figures.",
    }
    form.update(over)
    return form


def test_schedule_requires_judge_model_for_live_chats():
    with pytest.raises(HTTPException) as exc:
        erh._schedule_payload_from_form(_base_form(judge_model=""))
    assert exc.value.status_code == 400
    assert "judge" in str(exc.value.detail).lower()


def test_schedule_defaults_expectations_when_omitted_for_live_chats():
    """Task 2.4/2.6: a blank expectation no longer 400s — it defaults to
    that agent_kind's rubric (default_expectation_for), not the generic
    _DEFAULT_EXPECTATIONS. Tab 2's create form pre-fills the field, so a
    blank value on the wire means "keep the default", not "forgot it"
    (contrast with judge_model, still mandatory — see the test above).
    _base_form's agent_kind is "sample_other", which has a bespoke rubric distinct
    from the generic fallback (tests/admin/test_default_expectations.py
    covers default_expectation_for's per-kind behavior itself); see
    test_portal_and_mcp_schedule_defaults_agree_for_known_kind below for the
    MCP-parity assertion (whole-branch review finding I1)."""
    payload = erh._schedule_payload_from_form(_base_form(expectations="   "))
    assert payload["coverage"]["expectations"] == erh.default_expectation_for("sample_other")
    assert payload["coverage"]["expectations"] != erh._DEFAULT_EXPECTATIONS


def test_schedule_stores_judge_and_expectations_in_coverage():
    payload = erh._schedule_payload_from_form(_base_form())
    cov = payload["coverage"]
    assert cov["judge_model"] == "gemini-2.5-pro"
    assert cov["expectations"] == "Must cite real revenue figures."


def test_suite_schedule_unaffected_by_live_chat_requirements():
    # A suite schedule has no judge/expectations requirement.
    payload = erh._schedule_payload_from_form({
        "name": "suite sched",
        "source": "suite",
        "cadence": "weekly",
        "suite_id": "22222222-2222-2222-2222-222222222222",
        "profile_id": "11111111-1111-1111-1111-111111111111",
    })
    assert payload["source"] == "suite"


def test_quality_judge_includes_expectations_in_prompt():
    captured = {}

    class _FakeJudge:
        def invoke(self, messages):
            # messages = [SystemMessage, HumanMessage]
            captured["user"] = messages[-1].content

            class _R:
                content = '{"score": 0.9, "pass": true, "rationale": "ok", "regressed": null}'
            return _R()

    out = erh._invoke_quality_judge(
        _FakeJudge(),
        question="What was revenue?",
        actual="Revenue was 10k.",
        pass_threshold=0.7,
        expectations="Must cite real revenue figures.",
    )
    assert out["pass"] is True
    assert "Operator expectations" in captured["user"]
    assert "Must cite real revenue figures." in captured["user"]


def test_default_expectations_is_nonempty():
    assert erh._DEFAULT_EXPECTATIONS.strip()


def test_portal_and_mcp_schedule_defaults_agree_for_known_kind(monkeypatch):
    """Review finding I1: the portal's _schedule_payload_from_form and the
    MCP eval_schedules tool (admin_mcp/tools_test.py) must compute the
    IDENTICAL blank-expectation default for a live_chats schedule of a known
    agent_kind (per-kind default_expectation_for, not the generic
    _DEFAULT_EXPECTATIONS fallback) — they used to disagree (portal always
    fell back to the generic rubric; MCP was already per-kind)."""
    # Portal side: direct agent_kind, no profile_id involved, blank
    # expectations.
    portal_payload = erh._schedule_payload_from_form({
        "name": "business live-chats",
        "source": "live_chats",
        "cadence": "daily",
        "agent_kind": "sample_agent",
        "judge_model": "gemini-2.5-pro",
        "expectations": "   ",
    })
    # Not a sensitive kind: those are refused as live-chat sources.
    expected = erh.default_expectation_for("sample_agent")
    assert portal_payload["coverage"]["expectations"] == expected
    assert expected != erh._DEFAULT_EXPECTATIONS  # sanity: bespoke, not generic

    # MCP side: same blank+kind input through admin_mcp.tools_test.eval_schedules.
    import admin_mcp.tools_test as tt
    from admin_mcp.context import MCP_ACTOR
    from tests.admin_mcp.conftest import FakeClient

    fake = FakeClient({"agent_eval_schedules": []})
    monkeypatch.setattr(tt, "client_for_env", lambda env: fake)
    monkeypatch.setattr(tt, "actor_audit", lambda *a, **k: None)
    token = MCP_ACTOR.set({"user_id": "u-1", "user_token": "tok",
                           "email": "t@waj.local"})
    try:
        out = tt.eval_schedules(action="create", name="business live-chats mcp",
                                source="live_chats", agent_kind="sample_agent",
                                judge_model="gemini-2.5-pro", expectations="")
    finally:
        MCP_ACTOR.reset(token)

    assert out["created"] is True
    inserted = fake.table("agent_eval_schedules").inserted[0]
    assert inserted["coverage"]["expectations"] == expected


import pytest as _pytest  # noqa: E402


@_pytest.fixture(autouse=True)
def _product_sample_data(product_build):
    """These tests use a product's agent kinds, model rows or seeded suites
    as sample data for generic logic; see tests/conftest.py::product_build."""
