"""Eval runner: tool-result error detection + bounded scenario concurrency.

Two defects pinned here:

1. A tool that FAILED but returned an error-shaped result (an HTTP tool
   wrapper answers `{"error": "get_customer_details returned 500",
   "details": ...}`) still counted as a clean call, so the run read
   "0 tool errors". `_tool_result_error` / `_extract_tool_calls` read each
   result; `_run_scenario` records `tool_errors` and fails the tool assertion
   when an EXPECTED tool only ever errored.
2. Scenarios x repeats ran one at a time (~80s per reply). Attempts now
   run on a bounded pool — these tests pin deterministic ordering, real
   overlap, ContextVar propagation (Laminar's current span and the admin env
   live in ContextVars), cancellation, sequential multi-turn turns, and that
   voice_live stays sequential.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import random
import threading
import time
from typing import Any, Dict, List

import pytest

from tests.admin.test_eval_cancellation import _FakeClient, _seed_eval_run


def _sse(*events: Dict[str, Any]) -> str:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events)


# ---------------------------------------------------------------------------
# _tool_result_error / _tool_result_empty
# ---------------------------------------------------------------------------


class TestToolResultError:
    @pytest.mark.parametrize("result, needle", [
        # an HTTP tool wrapper on a >=400
        ('{"error": "get_customer_details returned 500", "details": "boom"}',
         "get_customer_details returned 500"),
        # get_customer_summary's session path wraps it one level down
        ('{"details": {"error": "get_customer_details returned 500"}, "memberships": []}',
         "get_customer_details returned 500"),
        ('{"ok": false, "message": "slot taken"}', "slot taken"),
        ('{"success": false, "reason": "no staff"}', "no staff"),
        ('{"status": "error", "message": "upstream down"}', "upstream down"),
        ('{"error": {"message": "rate limited", "code": 429}}', "rate limited"),
        ("Error: tool call failed: timeout", "Error: tool call failed"),
        # clipped mid-JSON by the agent (observation.result is [:500])
        ('{"error": "get_services returned 503", "details": "<html>' + "x" * 600,
         "get_services returned 503"),
        ({"error": "already parsed"}, "already parsed"),
    ])
    def test_error_shapes_are_detected(self, result, needle):
        from admin.evals import _tool_result_error

        got = _tool_result_error(result)
        assert got and needle in got
        assert len(got) <= 160

    @pytest.mark.parametrize("result", [
        '{"found": false, "customer": null}',
        '{"error": null, "data": [1]}',
        '{"error": "", "services": []}',
        '[{"id": 1, "status": "failed"}]',          # a payment row, not a tool failure
        '{"bookings": [{"status": "failed"}]}',     # lists are not walked
        '{"items": {"status": "error"}}',           # status only counts top-level
        "Here are the services: cut, colour",
        "",
        None,
        '{"ok": true}',
    ])
    def test_ordinary_results_are_not_errors(self, result):
        from admin.evals import _tool_result_error

        assert _tool_result_error(result) is None

    @pytest.mark.parametrize("result, empty", [
        ("", True), ("[]", True), ("{}", True), ("null", True),
        ('{"data": []}', True), ('{"services": [], "note": null}', True),
        ('{"found": false, "customer": null}', False),
        ('[{"id": 1}]', False), ("some text", False),
    ])
    def test_empty_results(self, result, empty):
        from admin.evals import _tool_result_empty

        assert _tool_result_empty(result) is empty


def test_extract_tool_calls_reads_observation_events_in_order():
    from admin.evals import _extract_tool_calls

    body = _sse(
        {"type": "action", "tool": "get_customer_summary", "args": {}},
        {"type": "observation", "tool": "get_customer_summary",
         "result": json.dumps({"error": "get_customer_details returned 500", "details": "x"})},
        {"type": "observation", "tool": "get_business_info", "result": "{}"},
        {"type": "tool_result", "tool": "list_services_public", "output": "[]"},
        {"type": "observation", "tool": "get_customer_summary", "result": '{"found": true}'},
        {"type": "answer", "content": "Hi"},
    )
    calls = _extract_tool_calls(body)
    assert [c["tool"] for c in calls] == [
        "get_customer_summary", "get_business_info", "list_services_public",
        "get_customer_summary"]
    assert calls[0]["error"].startswith("get_customer_details returned 500")
    assert [c["empty"] for c in calls] == [False, True, True, False]
    assert calls[3]["error"] is None


# ---------------------------------------------------------------------------
# _run_scenario policy
# ---------------------------------------------------------------------------


_ERR = json.dumps({"error": "get_customer_details returned 500", "details": "x"})


def _fake_invoke_with(observations: List[Dict[str, Any]], answer: str = "ok answer"):
    def fake(**kw):
        from admin.evals import _extract_executed_tools, _extract_tool_calls

        body = _sse(*[{"type": "action", "tool": o["tool"]} for o in observations],
                    *[{"type": "observation", **o} for o in observations])
        metrics = kw.get("metrics")
        if metrics is not None:
            metrics["executed_tools"] = _extract_executed_tools(body)
            metrics["tool_calls"] = _extract_tool_calls(body)
        return answer, "", 200, 5
    return fake


def _run(monkeypatch, scenario, observations, **kw):
    from admin import evals

    monkeypatch.setattr(evals, "_invoke_endpoint", _fake_invoke_with(observations))
    return evals._run_scenario(
        scenario, profile_id="p", base_url="http://x", endpoint="/e",
        judge=None, pass_threshold=0.7, **kw)


class TestScenarioPolicy:
    def test_incidental_tool_error_is_recorded_but_does_not_fail(self, monkeypatch):
        r = _run(monkeypatch, {"id": "s", "question": "q", "expected_substrings": ["ok"]},
                 [{"tool": "get_customer_summary", "result": _ERR},
                  {"tool": "get_business_info", "result": "{}"}])
        assert r["passed"] is True
        assert r["tool_error_count"] == 1
        assert r["tool_errors"] == [{"tool": "get_customer_summary",
                                     "error": "get_customer_details returned 500 — x"}]
        assert r["empty_tool_results"] == ["get_business_info"]
        assert r["errored_expected_tools"] is None

    def test_expected_tool_that_only_errored_fails_the_tool_assertion(self, monkeypatch):
        r = _run(monkeypatch, {"id": "s", "question": "q", "expected_substrings": ["ok"],
                               "expected_tools": ["get_customer_summary"]},
                 [{"tool": "get_customer_summary", "result": _ERR}])
        assert r["passed"] is False
        assert r["tool_assertion_passed"] is False
        assert r["errored_expected_tools"] == ["get_customer_summary"]
        assert r["missing_expected_tools"] is None  # it WAS called

    def test_a_retry_that_succeeded_clears_the_expected_tool(self, monkeypatch):
        r = _run(monkeypatch, {"id": "s", "question": "q", "expected_substrings": ["ok"],
                               "expected_tools": ["get_customer_summary"]},
                 [{"tool": "get_customer_summary", "result": _ERR},
                  {"tool": "get_customer_summary", "result": '{"found": true}'}])
        assert r["passed"] is True
        assert r["tool_assertion_passed"] is True
        assert r["tool_error_count"] == 1  # still visible

    def test_fail_on_tool_error_opt_in(self, monkeypatch):
        sc = {"id": "s", "question": "q", "expected_substrings": ["ok"]}
        obs = [{"tool": "get_business_info", "result": _ERR}]
        r = _run(monkeypatch, sc, obs, run_context={"fail_on_tool_error": True})
        assert r["passed"] is False and r["tool_assertion_passed"] is False
        # the scenario's own flag wins over the suite's
        r = _run(monkeypatch, {**sc, "fail_on_tool_error": False}, obs,
                 run_context={"fail_on_tool_error": True})
        assert r["passed"] is True

    def test_clean_run_reports_zero(self, monkeypatch):
        r = _run(monkeypatch, {"id": "s", "question": "q", "expected_substrings": ["ok"]},
                 [{"tool": "get_business_info", "result": '{"name": "Harbor Cafe"}'}])
        assert r["tool_error_count"] == 0 and r["tool_errors"] is None

    def test_multi_turn_errors_carry_their_turn(self, monkeypatch):
        from admin import evals

        def fake(**kw):
            q = kw["scenario"]["question"]
            body = _sse({"type": "action", "tool": "get_services"},
                        {"type": "observation", "tool": "get_services",
                         "result": _ERR if q == "second" else "[1]"},
                        {"type": "answer", "content": "fine"})
            kw["metrics"]["body"] = body
            kw["metrics"]["tool_calls"] = evals._extract_tool_calls(body)
            return "fine", "", 200, 5

        monkeypatch.setattr(evals, "_invoke_endpoint", fake)
        r = evals._run_scenario(
            {"id": "mt", "turns": [{"question": "first", "checks": []},
                                   {"question": "second", "checks": []}]},
            profile_id="p", base_url="http://x", endpoint="/e", judge=None,
            pass_threshold=0.7)
        assert r["tool_errors"] == [{"tool": "get_services", "turn": 2,
                                     "error": "get_customer_details returned 500 — x"}]


class TestResolveConcurrency:
    def test_default_env_context_and_clamp(self, monkeypatch):
        from admin.evals import _MAX_CONCURRENCY, _resolve_concurrency

        monkeypatch.delenv("EVAL_CONCURRENCY", raising=False)
        assert _resolve_concurrency(None) == 4
        assert _resolve_concurrency({"concurrency": "2"}) == 2
        assert _resolve_concurrency({"concurrency": 99}) == _MAX_CONCURRENCY
        assert _resolve_concurrency({"concurrency": 0}) == 4
        assert _resolve_concurrency({"concurrency": "nope"}) == 4
        monkeypatch.setenv("EVAL_CONCURRENCY", "1")
        assert _resolve_concurrency({}) == 1
        assert _resolve_concurrency({"concurrency": 3}) == 3

    def test_voice_live_is_always_sequential(self):
        from admin.evals import _resolve_concurrency

        assert _resolve_concurrency({"concurrency": 8}, runner_kind="voice_live") == 1


# ---------------------------------------------------------------------------
# run_eval_suite_sync with concurrency
# ---------------------------------------------------------------------------


def _scenarios(n: int) -> List[Dict[str, Any]]:
    return [{"id": f"s{i}", "question": f"q{i}", "expected_substrings": ["a"]}
            for i in range(n)]


def _setup(monkeypatch, *, scenarios, context=None):
    from admin import evals

    client = _FakeClient()
    _seed_eval_run(client, scenarios=scenarios)
    if context:
        client.tables["agent_eval_runs"].rows[0]["context"] = context
    monkeypatch.setattr(evals, "get_service_client", lambda: client)
    monkeypatch.delenv("EVAL_CONCURRENCY", raising=False)
    return client


class _InFlight:
    def __init__(self):
        self.lock = threading.Lock()
        self.now = 0
        self.peak = 0

    @contextlib.contextmanager
    def track(self):
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
        try:
            yield
        finally:
            with self.lock:
                self.now -= 1


def test_results_keep_scenario_then_attempt_order_and_overlap(monkeypatch):
    from admin import evals

    client = _setup(monkeypatch, scenarios=_scenarios(5),
                    context={"repeats": 3, "concurrency": 4})
    flight = _InFlight()
    rng = random.Random(7)
    delays = {f"q{i}": rng.uniform(0.005, 0.04) for i in range(5)}

    def fake(**kw):
        with flight.track():
            time.sleep(delays[kw["scenario"]["question"]] * rng.uniform(0.5, 1.5))
        return "a", "", 200, 1

    monkeypatch.setattr(evals, "_invoke_endpoint", fake)
    evals.run_eval_suite_sync(run_id="run-1")

    final = client.tables["agent_eval_runs"].rows[0]
    got = [(r["scenario_id"], r["attempt"]) for r in final["results"]]
    assert got == [(f"s{i}", a) for i in range(5) for a in range(3)]
    assert final["status"] == "passed"
    assert 1 < flight.peak <= 4


def test_concurrency_one_never_overlaps(monkeypatch):
    from admin import evals

    _setup(monkeypatch, scenarios=_scenarios(4), context={"concurrency": 1})
    flight = _InFlight()
    threads = set()

    def fake(**kw):
        threads.add(threading.get_ident())
        with flight.track():
            time.sleep(0.005)
        return "a", "", 200, 1

    monkeypatch.setattr(evals, "_invoke_endpoint", fake)
    evals.run_eval_suite_sync(run_id="run-1")
    assert flight.peak == 1
    assert threads == {threading.get_ident()}  # inline, no pool


_PARENT: contextvars.ContextVar = contextvars.ContextVar("test_parent_span", default=None)


def test_each_scenario_span_parents_under_the_suite_span(monkeypatch):
    """Laminar's current span lives in a ContextVar. Worker threads don't
    inherit ContextVars, so without copy_context() every scenario span would
    open with NO parent (a separate root trace). Simulate the span stack with
    a ContextVar and record each scenario span's parent."""
    from admin import evals

    _setup(monkeypatch, scenarios=_scenarios(6), context={"concurrency": 3})
    parents: Dict[str, Any] = {}
    lock = threading.Lock()

    @contextlib.contextmanager
    def fake_span(name, span_type="DEFAULT", attributes=None):
        with lock:
            parents[name] = _PARENT.get()
        token = _PARENT.set(name)
        try:
            yield None
        finally:
            _PARENT.reset(token)

    def fake(**kw):
        # The invoke runs inside its own scenario span, whatever thread.
        assert _PARENT.get() == f"eval_scenario::{kw['scenario']['id']}"
        time.sleep(0.01)
        return "a", "", 200, 1

    monkeypatch.setattr(evals, "_lmnr_span", fake_span)
    monkeypatch.setattr(evals, "_invoke_endpoint", fake)
    evals.run_eval_suite_sync(run_id="run-1")
    assert parents.pop("eval_suite::test_suite") is None
    assert parents == {f"eval_scenario::s{i}": "eval_suite::test_suite" for i in range(6)}


def test_cancel_stops_dispatch_and_keeps_finished_work(monkeypatch):
    from admin import evals

    client = _setup(monkeypatch, scenarios=_scenarios(6), context={"concurrency": 2})
    polls = {"n": 0}

    def fake_cancel(_job):
        polls["n"] += 1
        return polls["n"] >= 4  # tasks 0-2 start, task 3 is refused

    monkeypatch.setattr(evals, "_is_job_cancelled", fake_cancel)
    monkeypatch.setattr(evals, "_invoke_endpoint",
                        lambda **kw: (time.sleep(0.01), ("a", "", 200, 1))[1])
    evals.run_eval_suite_sync(run_id="run-1", _job_id="job-1")

    final = client.tables["agent_eval_runs"].rows[0]
    assert final["status"] == "cancelled"
    assert [r["scenario_id"] for r in final["results"]] == ["s0", "s1", "s2"]
    assert "4/6" in final["error"]


def test_multi_turn_turns_stay_sequential_within_a_scenario(monkeypatch):
    from admin import evals

    scenarios = [{"id": f"m{i}", "turns": [{"question": f"m{i}-t{t}", "checks": []}
                                            for t in range(3)]} for i in range(4)]
    client = _setup(monkeypatch, scenarios=scenarios, context={"concurrency": 4})
    per_scn: Dict[str, List[str]] = {}
    active: Dict[str, int] = {}
    lock = threading.Lock()

    def fake(**kw):
        q = kw["scenario"]["question"]
        sid = q.split("-")[0]
        with lock:
            assert active.get(sid, 0) == 0, "two turns of one scenario overlapped"
            active[sid] = 1
            per_scn.setdefault(sid, []).append(q)
        time.sleep(0.005)
        with lock:
            active[sid] = 0
        return "fine", "", 200, 1

    monkeypatch.setattr(evals, "_invoke_endpoint", fake)
    evals.run_eval_suite_sync(run_id="run-1")
    assert per_scn == {f"m{i}": [f"m{i}-t{t}" for t in range(3)] for i in range(4)}
    final = client.tables["agent_eval_runs"].rows[0]
    assert [r["scenario_id"] for r in final["results"]] == ["m0", "m1", "m2", "m3"]


def test_voice_live_runs_sequentially_even_when_asked_for_more(monkeypatch):
    from admin import evals
    import admin.voice_eval_runner as ver

    client = _setup(monkeypatch, scenarios=_scenarios(4), context={"concurrency": 8})
    suite = client.tables["agent_eval_runs"].rows[0]["agent_eval_suites"]
    suite["runner_kind"] = "voice_live"
    monkeypatch.setattr(evals, "_try_build_judge", lambda *_a, **_k: object())
    monkeypatch.setattr(evals, "_resolve_evaluator_model", lambda *_a: "judge-model")
    flight = _InFlight()

    def fake_voice(scenario, **_kw):
        with flight.track():
            time.sleep(0.005)
        return {"scenario_id": scenario["id"], "passed": True, "score": 1.0}

    monkeypatch.setattr(ver, "run_voice_scenario_sync", fake_voice)
    evals.run_eval_suite_sync(run_id="run-1")
    final = client.tables["agent_eval_runs"].rows[0]
    assert [r["scenario_id"] for r in final["results"]] == ["s0", "s1", "s2", "s3"]
    assert flight.peak == 1


def test_each_worker_thread_gets_its_own_judge(monkeypatch):
    from admin import evals
    from admin import e2e_runner

    client = _setup(monkeypatch, scenarios=[
        {"id": f"s{i}", "question": f"q{i}", "expected_answer": "a"} for i in range(6)],
        context={"concurrency": 3})
    client.tables["agent_eval_runs"].rows[0]["agent_eval_suites"]["evaluator_model_id"] = "m1"
    monkeypatch.setattr(evals, "_resolve_evaluator_model", lambda *_a: "judge-model")
    built: List[Any] = []
    lock = threading.Lock()

    def build(*_a, **_k):
        j = object()
        with lock:
            built.append((threading.get_ident(), j))
        return j

    used: Dict[int, set] = {}

    def fake_judge(judge, *_a, **_k):
        with lock:
            used.setdefault(threading.get_ident(), set()).add(id(judge))
        time.sleep(0.01)
        return {"score": 1.0, "pass": True, "rationale": "ok"}

    monkeypatch.setattr(evals, "_try_build_judge", build)
    monkeypatch.setattr(e2e_runner, "_invoke_judge", fake_judge)
    monkeypatch.setattr(evals, "_invoke_endpoint", lambda **kw: ("a", "", 200, 1))
    evals.run_eval_suite_sync(run_id="run-1")

    shared = built[0][1]  # built on the job thread before any worker
    worker_judges = {tid: ids for tid, ids in used.items()}
    assert all(len(ids) == 1 for ids in worker_judges.values())
    all_ids = set().union(*worker_judges.values())
    assert id(shared) not in all_ids
    assert len(all_ids) == len(worker_judges)  # one distinct judge per thread


# ---------------------------------------------------------------------------
# surfaces: run KPIs, case reason, MCP summary
# ---------------------------------------------------------------------------


def test_run_kpis_count_tool_errors():
    from admin.eval_handlers import run_detail_kpis

    rows = [
        {"scenario_id": "a", "passed": True, "tool_error_count": 2},
        {"scenario_id": "a", "passed": True, "tool_error_count": 0},
        {"scenario_id": "b", "passed": False, "tool_error_count": 1},
        {"scenario_id": "c", "passed": True, "tool_error_count": 0},
    ]
    k = run_detail_kpis(rows)
    assert (k["tool_errors"], k["cases_with_tool_errors"]) == (3, 2)
    # a run recorded before tool errors existed reads unknown, not 0
    assert run_detail_kpis([{"scenario_id": "a", "passed": True}])["tool_errors"] is None


def test_case_reason_names_the_failed_tool():
    from admin.testing_view import case_reason

    reason = case_reason({
        "passed": False, "errored_expected_tools": ["get_customer_summary"],
        "tool_errors": [{"tool": "get_customer_summary", "error": "returned 500"}]})
    assert reason == "Tool failed: get_customer_summary (returned 500)"
    reason = case_reason({"passed": False, "tool_errors": [
        {"tool": "get_services", "error": "boom"}, {"tool": "x", "error": "y"}]})
    assert reason == "Tool error: get_services (boom) (+1 more)"


def test_mcp_run_summary_reports_tool_errors():
    from admin_mcp.tools_test import _run_summary

    out = _run_summary({"run_id": "r", "results": [
        {"scenario_id": "a", "passed": True, "tool_error_count": 1,
         "tool_errors": [{"tool": "t", "error": "e"}]},
        {"scenario_id": "b", "passed": True, "tool_error_count": 0},
    ]}, include_results=False)
    s = out["results_summary"]
    assert s["tool_errors"] == 1
    assert s["with_tool_errors"] == [{"scenario_id": "a", "passed": True,
                                      "tool_errors": [{"tool": "t", "error": "e"}]}]
