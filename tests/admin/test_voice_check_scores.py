"""Voice checks in the shared Testing scores (admin/voice_checks.py).

Voice checks (ai_voice_scenarios / ai_voice_scenario_runs) used to live only on
the Voice checks tab. They now feed the per-agent Testing Overview, the Runs
list, Compare (voice-vs-voice; a text/voice mix is refused with a reason) and
the admin MCP's eval_runs list/get/compare/overview.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from admin import voice_checks as vc
from tests.admin_mcp.conftest import FakeClient
from tests.scoped_build import full_build

SCENARIO = {
    "scenario_id": "sc1", "slug": "orchard_greeting", "agent_kind": "orchard",
    "title": "Greets the caller", "enabled": True,
    "steps": [
        {"type": "say", "text": "Hello?"},
        {"type": "expect_transcript", "role": "assistant", "pattern": "ahlan|hello"},
        {"type": "wait", "sec": 1},
        {"type": "expect_frame", "frame_type": "display", "pattern": "balance"},
    ],
}
SCENARIO_2 = {"scenario_id": "sc2", "slug": "orchard_simple_question", "agent_kind": "orchard",
              "title": None, "enabled": True,
              "steps": [{"type": "say", "text": "What is my balance?"},
                        {"type": "expect_transcript", "role": "assistant", "pattern": "SAR"}]}
OTHER_KIND = {"scenario_id": "sc3", "slug": "grove_hello", "agent_kind": "grove",
              "enabled": True, "steps": [{"type": "say", "text": "hi"}]}


def _checks(*oks):
    """say(0) ok, expect_transcript(1), wait(2) ok, expect_frame(3)."""
    return [
        {"step": 0, "type": "say", "ok": True, "said": "Hello?"},
        {"step": 1, "type": "expect_transcript", "ok": oks[0], "frame_type": "transcript",
         "pattern": "ahlan|hello"},
        {"step": 2, "type": "wait", "ok": True},
        {"step": 3, "type": "expect_frame", "ok": oks[1], "frame_type": "display",
         "pattern": "balance"},
    ]


def _run(run_id, status, oks, started, slug="orchard_greeting", reason=None):
    detail = {"checks": _checks(*oks)} if oks else {}
    if reason:
        detail["reason"] = reason
    return {"run_id": run_id, "scenario_id": "sc1", "slug": slug, "status": status,
            "target": "ws://127.0.0.1:8000/voice/live", "detail": detail,
            "started_at": started, "finished_at": started.replace(":00Z", ":40Z")}


PROFILES = [
    {"profile_id": "p-orchard", "agent_kind": "orchard", "status": "active",
     "environment": "staging", "parent_profile_id": None, "display_name": "Orchard assistant",
     "slug": "orchard_default"},
    # The voice specialist is a child — calls are answered by the parent.
    {"profile_id": "p-orchard-voice", "agent_kind": "orchard", "status": "specialist",
     "environment": "staging", "parent_profile_id": "p-orchard", "display_name": "Layla"},
    {"profile_id": "p-orchard-prod", "agent_kind": "orchard", "status": "active",
     "environment": "prod", "parent_profile_id": None, "display_name": "Orchard assistant"},
    {"profile_id": "p-biz", "agent_kind": "business", "status": "active",
     "environment": "staging", "parent_profile_id": None, "display_name": "Front desk assistant",
     "slug": "biz"},
]

RUNS = [
    _run("v3", "failed", (True, False), "2026-10-11T10:00:00Z"),
    _run("v2", "passed", (True, True), "2026-10-10T10:00:00Z"),
    _run("v1", "failed", (False, False), "2026-10-09T10:00:00Z"),
    {"run_id": "w1", "scenario_id": "sc2", "slug": "orchard_simple_question", "status": "passed",
     "detail": {"checks": [{"step": 0, "type": "say", "ok": True},
                           {"step": 1, "type": "expect_transcript", "ok": True, "pattern": "SAR"}]},
     "started_at": "2026-10-11T09:00:00Z", "finished_at": "2026-10-11T09:00:30Z"},
]


def _tables(**extra):
    t = {"ai_voice_scenarios": [dict(SCENARIO), dict(SCENARIO_2), dict(OTHER_KIND)],
         "ai_voice_scenario_runs": [dict(r) for r in RUNS],
         "agent_profiles": [dict(p) for p in PROFILES]}
    t.update(extra)
    return t


@pytest.fixture(autouse=True)
def _full_engine(monkeypatch):
    full_build(monkeypatch)


# ---------------------------------------------------------------------------
# A voice run in the eval-run shape
# ---------------------------------------------------------------------------


def test_voice_run_maps_checked_steps_to_cases():
    run = vc.as_eval_run(_run("v3", "failed", (True, False), "2026-10-11T10:00:00Z"),
                         SCENARIO, profile_id="p-orchard")
    assert run["kind"] == "voice"
    assert run["suite_id"] == "voice:orchard_greeting"
    assert run["profile_id"] == "p-orchard"
    # say + wait are not test cases; the two expectations are.
    assert [r["passed"] for r in run["results"]] == [True, False]
    assert run["results"][0]["scenario_id"] == "Step 2 · Agent mentions “ahlan” or “hello”"
    assert run["results"][1]["scenario_id"] == "Step 4 · Shows a display update with “balance”"
    assert run["score"] == 0.5
    assert run["_check_name"] == "Greets the caller"


@pytest.mark.parametrize("status,expected", [
    ("error", None), ("skipped", None), ("running", None),
])
def test_unscored_voice_runs_have_no_score(status, expected):
    run = vc.as_eval_run(_run("x", status, None, "2026-10-11T10:00:00Z", reason="no key"), SCENARIO)
    assert run["score"] is expected
    assert run["status"] == {"error": "errored"}.get(status, status)
    assert run["error"] == "no key"


def test_step_labels_in_plain_words():
    assert vc.step_label({"type": "expect_transcript", "pattern": "(?i)orchard|payment|البستان"}) == \
        "Agent mentions “orchard”, “payment” or “البستان”"
    assert vc.step_label({"type": "expect_transcript", "pattern": r"\d{8}"}) == "Agent mentions “\\d{8}”"
    assert vc.step_label({"type": "expect_transcript", "pattern": ""}) == "Agent replies"
    assert vc.step_label({"type": "http_check"}) == "Follow-up check on the system passes"


def test_voice_profile_is_the_kinds_active_top_level_profile():
    m = vc.voice_profile_by_kind(PROFILES, env="staging")
    assert m["orchard"] == "p-orchard"           # not the specialist child, not prod
    assert vc.voice_profile_by_kind(PROFILES, env="prod")["orchard"] == "p-orchard-prod"


def test_voice_group_rolls_up_latest_result_per_check():
    runs = [vc.as_eval_run(r, SCENARIO if r["slug"] == "orchard_greeting" else SCENARIO_2)
            for r in RUNS]
    g = vc.voice_group([SCENARIO, SCENARIO_2], runs)
    assert g["kind"] == "voice" and g["suite"]["name"] == "Voice checks"
    assert g["n_tested"] == 2 and g["n_checks"] == 2
    assert g["latest"]["score"] == 0.5          # greeting failed, simple question passed
    assert g["target"] == 1.0 and g["verdict"] == "needs_work"
    assert g["failing"] == 1
    assert g["latest"]["run_id"] == "v3"        # the failing call is the one to open
    assert round(g["delta"], 2) == -0.5          # greeting passed the time before


def test_voice_group_untested_when_nothing_ran():
    g = vc.voice_group([SCENARIO], [])
    assert g["latest"] is None and g["verdict"] == "untested" and g["delta"] is None


# ---------------------------------------------------------------------------
# Testing Overview
# ---------------------------------------------------------------------------


def _overview(tables):
    from admin import eval_runs_handlers as h

    fake = FakeClient(tables)
    with patch.object(h, "admin_service_client", return_value=fake), \
            patch("admin._business_picker.fetch_businesses_for_picker", return_value=[]), \
            patch("core.auth.get_admin_env", return_value="staging"):
        return h.build_testing_overview_context()


def test_overview_card_for_an_agent_with_only_voice_checks():
    ctx = _overview(_tables(agent_eval_suites=[], agent_eval_runs=[]))
    cards = {c["profile_id"]: c for c in ctx["cards"]}
    assert "p-orchard" in cards and "p-orchard-voice" not in cards
    card = cards["p-orchard"]
    assert card["has_voice"] and card["verdict"] == "needs_work"
    (row,) = card["suites"]
    assert row["kind"] == "voice" and row["latest"]["score"] == 0.5
    assert ctx["summary"]["voice_checks"] == 2


def test_overview_shows_voice_checks_alongside_text_suites():
    tables = _tables(
        agent_eval_suites=[{"suite_id": "11111111-1111-1111-1111-111111111111", "name": "Merchant questions",
                            "profile_id": "p-orchard", "agent_kind": "orchard", "pass_threshold": 0.8,
                            "scenarios": [{}], "runner_kind": "chat"},
                           {"suite_id": "22222222-2222-2222-2222-222222222222", "name": "Voice calls",
                            "profile_id": "p-orchard", "agent_kind": "orchard", "pass_threshold": 0.9,
                            "scenarios": [{}], "runner_kind": "voice_live"}],
        agent_eval_runs=[{"run_id": "r1", "suite_id": "11111111-1111-1111-1111-111111111111",
                          "profile_id": "p-orchard", "status": "passed", "score": 0.95,
                          "created_at": "2026-10-11T08:00:00Z", "finished_at": "2026-10-11T08:05:00Z",
                          "context": {}, "results": [{"passed": True}]}],
    )
    ctx = _overview(tables)
    card = next(c for c in ctx["cards"] if c["profile_id"] == "p-orchard")
    kinds = sorted((x["kind"], x["suite"]["name"]) for x in card["suites"])
    # A voice_live eval suite is labelled Voice too.
    assert kinds == [("text", "Merchant questions"), ("voice", "Voice calls"), ("voice", "Voice checks")]
    # Text suite meets its target, but the voice checks don't: the agent needs work.
    assert card["verdict"] == "needs_work"
    assert card["focus"]["kind"] == "voice"


def test_overview_page_labels_voice_rows(client):
    from admin import eval_runs_handlers as h

    fake = FakeClient(_tables(agent_eval_suites=[], agent_eval_runs=[]))
    with patch.object(h, "admin_service_client", return_value=fake), \
            patch("core.auth.get_admin_env", return_value="staging"):
        r = client.get("/admin/evals")
    assert r.status_code == 200
    assert "Orchard assistant" in r.text
    assert ">Voice</span>" in r.text and "Voice checks" in r.text
    assert "/admin/evals/runs?kind=voice&amp;profile_id=p-orchard" in r.text
    assert "Run voice checks" in r.text
    assert "run_suite=None" not in r.text


def test_partner_build_drops_other_partners_voice_checks(monkeypatch):
    monkeypatch.setattr("admin.agent_kinds.build_kinds", lambda: frozenset({"orchard"}))
    fake = FakeClient(_tables())
    data = vc.load_voice_runs(fake, env="staging")
    assert {s["slug"] for s in data["scenarios"]} == {"orchard_greeting", "orchard_simple_question"}


# ---------------------------------------------------------------------------
# Runs list
# ---------------------------------------------------------------------------


def _runs_ctx(**filters):
    from admin import eval_runs_handlers as h

    fake = FakeClient(_tables(
        agent_eval_suites=[{"suite_id": "s1", "name": "booking_questions", "profile_id": "p-biz",
                            "agent_kind": "business", "pass_threshold": 0.9}],
        agent_eval_runs=[{"run_id": "t1", "suite_id": "s1", "profile_id": "p-biz", "status": "passed",
                          "score": 0.95, "created_at": "2026-10-10T12:00:00Z", "context": {},
                          "results": [{"passed": True}]}],
        agent_eval_schedules=[], agent_models=[], agent_profile_versions=[]))
    with patch.object(h, "admin_service_client", return_value=fake), \
            patch("admin._business_picker.fetch_businesses_for_picker", return_value=[]), \
            patch("core.auth.get_admin_env", return_value="staging"):
        return h.build_eval_runs_context(**filters)


def test_runs_list_includes_voice_runs_newest_first():
    ctx = _runs_ctx()
    ids = [r["run_id"] for r in ctx["recent_runs"]]
    assert ids == ["v3", "w1", "t1", "v2", "v1"]
    v3 = ctx["recent_runs"][0]
    assert v3["kind"] == "voice" and v3["_suite_name"] == "Greets the caller"
    assert v3["_target"] == 1.0
    assert v3["_cases_passed"] == 1 and v3["_cases_total"] == 2
    assert v3["_prev"]["run_id"] == "v2" and round(v3["_delta"], 2) == -0.5
    assert {v["suite_id"] for v in ctx["voice_checks"]} >= {"voice:orchard_greeting"}


def test_runs_list_type_filter():
    assert {r["kind"] for r in _runs_ctx(kind="voice")["recent_runs"]} == {"voice"}
    assert [r["run_id"] for r in _runs_ctx(kind="text")["recent_runs"]] == ["t1"]
    only = _runs_ctx(suite_id="voice:orchard_simple_question")["recent_runs"]
    assert [r["run_id"] for r in only] == ["w1"]
    assert [r["run_id"] for r in _runs_ctx(agent_kind="orchard")["recent_runs"]] == ["v3", "w1", "v2", "v1"]


def test_runs_page_marks_voice_rows_for_the_compare_picker(client):
    from admin import eval_runs_handlers as h

    fake = FakeClient(_tables(agent_eval_suites=[], agent_eval_runs=[], agent_eval_schedules=[]))
    with patch.object(h, "admin_service_client", return_value=fake), \
            patch("core.auth.get_admin_env", return_value="staging"):
        r = client.get("/admin/evals/runs")
    assert r.status_code == 200
    assert 'data-kind="voice"' in r.text
    assert "Voice checks</option>" in r.text or '<optgroup label="Voice checks">' in r.text


# ---------------------------------------------------------------------------
# Compare + run detail
# ---------------------------------------------------------------------------

VA, VB, VC_ = ("aaaaaaaa-0000-0000-0000-000000000001", "aaaaaaaa-0000-0000-0000-000000000002",
               "aaaaaaaa-0000-0000-0000-000000000003")
TEXT_ID = "bbbbbbbb-0000-0000-0000-000000000001"


def _uuid_tables():
    runs = [dict(_run(VB, "failed", (True, False), "2026-10-11T10:00:00Z")),
            dict(_run(VA, "passed", (True, True), "2026-10-10T10:00:00Z")),
            dict(_run(VC_, "failed", (False, False), "2026-10-09T10:00:00Z"))]
    return _tables(ai_voice_scenario_runs=runs,
                   agent_eval_runs=[{"run_id": TEXT_ID, "suite_id": "s1", "profile_id": "p-biz",
                                     "status": "passed", "score": 1.0, "results": [], "context": {},
                                     "created_at": "2026-10-10T00:00:00Z"}],
                   agent_eval_suites=[])


def test_compare_two_voice_runs_step_by_step(client):
    from admin import eval_handlers as eh

    fake = FakeClient(_uuid_tables())
    with patch.object(eh, "admin_service_client", return_value=fake), \
            patch("core.auth.get_admin_env", return_value="staging"):
        r = client.get(f"/admin/evals/compare?runs={VA},{VB}")
    assert r.status_code == 200, r.text[:400]
    assert "What changed, step by step" in r.text
    assert "1 broke" in r.text or "0 cases fixed, 1 broke" in r.text
    assert "Shows a display update" in r.text
    assert "Greets the caller" in r.text and ">Voice</span>" in r.text


def test_compare_mixed_text_and_voice_is_refused_with_reason(client):
    from admin import eval_handlers as eh

    fake = FakeClient(_uuid_tables())
    with patch.object(eh, "admin_service_client", return_value=fake), \
            patch("core.auth.get_admin_env", return_value="staging"):
        r = client.get(f"/admin/evals/compare?runs={TEXT_ID},{VB}")
    assert r.status_code == 400
    assert "can only be compared with other voice-check runs" in r.text


def test_voice_run_detail_page(client):
    from admin import router as rt

    fake = FakeClient(_uuid_tables())
    with patch.object(rt, "client_for", return_value=fake), \
            patch("core.auth.get_admin_env", return_value="staging"):
        r = client.get(f"/admin/evals/runs/{VB}")
    assert r.status_code == 200, r.text[:400]
    assert "The call, step by step" in r.text
    assert "1 of 2 checked steps passed" in r.text
    assert f"/admin/evals/compare?runs={VA},{VB}" in r.text   # previous scored run
    assert "Broke" in r.text                                   # step 4 passed last time


def test_unknown_run_is_still_404(client):
    from admin import router as rt

    fake = FakeClient(_uuid_tables())
    with patch.object(rt, "client_for", return_value=fake):
        r = client.get("/admin/evals/runs/cccccccc-0000-0000-0000-000000000009")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Admin MCP parity
# ---------------------------------------------------------------------------


def _mcp(monkeypatch, tables):
    import admin_mcp.tools_test as tt

    client = FakeClient(tables)
    monkeypatch.setattr(tt, "client_for_env", lambda env: client)
    return tt


def test_mcp_list_includes_voice_runs_with_kind(monkeypatch):
    tt = _mcp(monkeypatch, _uuid_tables())
    out = tt.eval_runs(action="list")
    kinds = [(r["run_id"], r["kind"]) for r in out["runs"]]
    assert (VB, "voice") in kinds and (TEXT_ID, "text") in kinds
    voice = next(r for r in out["runs"] if r["run_id"] == VB)
    assert voice["suite_id"] == "voice:orchard_greeting" and voice["score"] == 0.5
    assert voice["check_name"] == "Greets the caller"
    assert not any(k.startswith("_") for k in voice)
    assert {r["kind"] for r in tt.eval_runs(action="list", kind="voice")["runs"]} == {"voice"}
    assert {r["kind"] for r in tt.eval_runs(action="list", kind="text")["runs"]} == {"text"}


def test_mcp_get_voice_run(monkeypatch):
    tt = _mcp(monkeypatch, _uuid_tables())
    out = tt.eval_runs(action="get", run_id=VB)
    run = out["run"]
    assert run["kind"] == "voice"
    assert run["results_summary"]["scenarios"] == 2 and run["results_summary"]["failed"] == 1


def test_mcp_compare_voice_runs_and_refuse_mixed(monkeypatch):
    tt = _mcp(monkeypatch, _uuid_tables())
    out = tt.eval_runs(action="compare", run_ids=[VA, VB])
    assert out["labels"]["regressed"] == 1 and out["labels"]["held"] == 1
    mixed = tt.eval_runs(action="compare", run_ids=[TEXT_ID, VB])
    assert "error" in mixed and "voice" in mixed["error"]


def test_mcp_overview_has_voice_rows(monkeypatch):
    from admin import eval_runs_handlers as h

    tt = _mcp(monkeypatch, {})
    fake = FakeClient(_tables(agent_eval_suites=[], agent_eval_runs=[]))
    with patch.object(h, "admin_service_client", return_value=fake), \
            patch("admin._business_picker.fetch_businesses_for_picker", return_value=[]), \
            patch("core.auth.get_admin_env", return_value="staging"):
        out = tt.eval_runs(action="overview")
    agent = next(a for a in out["agents"] if a["profile_id"] == "p-orchard")
    (row,) = agent["suites"]
    assert row["kind"] == "voice" and row["score"] == 0.5 and row["target"] == 1.0
    assert {c["slug"] for c in row["checks"]} == {"orchard_greeting", "orchard_simple_question"}
    assert out["summary"]["voice_checks"] == 2
