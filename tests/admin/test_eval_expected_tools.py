"""Tests for the eval runner's hard `expected_tools`/`forbidden_tools` gate
(Task 7b — eval suite gate fixes).

Background: a baseline optimization suite run showed the judge passes an
answer regardless of WHICH mechanism produced it, and `tool_count` read 0
for every scenario because the agent under test's manual tool loop only
narrates tool use via `{"type": "thought", "content":
"Using X..."}` pairs — it never emits the structured `tool`/`tool_call`/
`tool_result`/`action` events the old proxy counted. These tests cover:

  1. `_extract_executed_tools` recovering tool names from a fake SSE
     stream via BOTH extraction strategies (structured events; the
     thought-narration fallback).
  2. `expected_tools` pass/fail via `_run_scenario`.
  3. `forbidden_tools` pass/fail via `_run_scenario`.
  4. Absent fields → no assertion (back-compat, existing behavior
     unaffected).
  5. The tool assertion is a HARD gate — it fails the scenario even when
     the judge scored it a perfect 1.0.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional  # noqa: F401  (used in string annotations)

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

from admin import evals


# --------------------------------------------------------------------------- #
# 1. _extract_executed_tools — capture from a fake SSE stream
# --------------------------------------------------------------------------- #

def test_extract_executed_tools_from_structured_action_events():
    """Agents built on a tool-calling loop emit a structured
    event carrying the RAW tool id — used verbatim (canonicalized)."""
    body = (
        'data: {"type":"thought","content":"Fetching order history..."}\n\n'
        'data: {"type":"action","tool":"get_order_history","args":{}}\n\n'
        'data: {"type":"thought","content":"Retrieved order history data"}\n\n'
        'data: {"type":"answer","content":"done"}\n\n'
        'data: {"type":"done"}\n\n'
    )
    assert evals._extract_executed_tools(body) == ["order history"]


def test_extract_executed_tools_from_thought_narration_fallback():
    """An agent with a manual tool loop never emits a structured tool event — only
    `{"type": "thought", "content": "Using <display>..."}`/"Retrieved data
    from <display>" pairs. This is the actual root cause of tool_count
    reading 0 for that agent; the fallback recovers both tool
    names from that narration alone."""
    body = (
        'data: {"type":"thought","content":"Using read skill..."}\n\n'
        'data: {"type":"thought","content":"Retrieved data from read skill"}\n\n'
        'data: {"type":"thought","content":"Using ask business reporting..."}\n\n'
        'data: {"type":"thought","content":"Retrieved data from ask business reporting"}\n\n'
        'data: {"type":"answer","content":"here you go"}\n\n'
        'data: {"type":"done"}\n\n'
    )
    executed = evals._extract_executed_tools(body)
    assert executed == sorted(["ask business reporting", "read skill"])
    # The raw tool ids canonicalize to the exact same strings the fallback
    # extracted — proving expected_tools=["read_skill"] etc. would match.
    assert evals._canonical_tool_name("read_skill") in executed
    assert evals._canonical_tool_name("ask_business_reporting") in executed


def test_extract_executed_tools_no_tool_events_returns_empty():
    body = 'data: {"type":"answer","content":"hi"}\n\ndata: {"type":"done"}\n\n'
    assert evals._extract_executed_tools(body) == []


def test_extract_executed_tools_structured_takes_precedence_no_double_count():
    """An agent that emits BOTH a structured event AND thought
    narration for the same call must not be double-counted via the
    fallback — structured wins outright once any structured event exists."""
    body = (
        'data: {"type":"thought","content":"Fetching leads overview..."}\n\n'
        'data: {"type":"action","tool":"get_leads_overview","args":{}}\n\n'
        'data: {"type":"thought","content":"Retrieved leads overview data"}\n\n'
    )
    assert evals._extract_executed_tools(body) == ["leads overview"]


# --------------------------------------------------------------------------- #
# tool_count fix: thought-only transcripts now count > 0
# --------------------------------------------------------------------------- #

def test_extract_run_metrics_counts_thought_only_transcript():
    """Before this fix, tool_count read 0 for every narrating agent's scenario
    (the baseline's documented concern #2) because `_extract_run_metrics`
    only recognized structured tool/tool_call/tool_result/action events."""
    body = (
        'data: {"type":"thought","content":"Using business performance..."}\n\n'
        'data: {"type":"thought","content":"Retrieved data from business performance"}\n\n'
        'data: {"type":"answer","content":"Here is your report"}\n\n'
        'data: {"type":"done"}\n\n'
    )
    m = evals._extract_run_metrics(body)
    assert m["tool_count"] == 1
    assert m["iterations"] == 2


# --------------------------------------------------------------------------- #
# 2 & 4. expected_tools pass/fail, absent fields = no assertion
# --------------------------------------------------------------------------- #

def _fake_invoke_endpoint_factory(executed_tools, answer="An answer.", http_status=200):
    def fake_invoke(metrics=None, **_k):
        if metrics is not None:
            metrics["ttft_ms"] = 10
            metrics["usage"] = {"input_tokens": 1, "output_tokens": 1}
            metrics["runmetrics"] = {
                "iterations": (len(executed_tools) + 1) if executed_tools else 1,
                "tool_count": len(executed_tools),
            }
            metrics["executed_tools"] = executed_tools
        return (answer, "", http_status, 10)
    return fake_invoke


def test_run_scenario_expected_tools_pass_when_tool_called(monkeypatch):
    monkeypatch.setattr(
        evals, "_invoke_endpoint",
        _fake_invoke_endpoint_factory(["ask business reporting"]),
    )
    result = evals._run_scenario(
        {"id": "s1", "question": "q", "expected_tools": ["ask_business_reporting"]},
        profile_id="p", base_url="http://x", endpoint="/e",
        judge=None, pass_threshold=0.7,
    )
    assert result["passed"] is True
    assert result["tool_assertion_passed"] is True
    assert result["missing_expected_tools"] is None
    assert result["executed_tools"] == ["ask business reporting"]


def test_run_scenario_expected_tools_fail_when_tool_not_called(monkeypatch):
    monkeypatch.setattr(
        evals, "_invoke_endpoint",
        _fake_invoke_endpoint_factory([], answer="A direct answer with no delegation."),
    )
    result = evals._run_scenario(
        {"id": "s2", "question": "q", "expected_tools": ["ask_business_reporting"]},
        profile_id="p", base_url="http://x", endpoint="/e",
        judge=None, pass_threshold=0.7,
    )
    assert result["passed"] is False
    assert result["tool_assertion_passed"] is False
    assert result["missing_expected_tools"] == ["ask_business_reporting"]


def test_run_scenario_no_expected_or_forbidden_tools_is_no_assertion(monkeypatch):
    """Absent fields = no assertion — behavior identical to before this
    change for every existing scenario that doesn't set either field."""
    monkeypatch.setattr(
        evals, "_invoke_endpoint",
        _fake_invoke_endpoint_factory(["some tool"], answer="hello world"),
    )
    result = evals._run_scenario(
        {"id": "s3", "question": "q"},  # no expected_answer/substrings/tools
        profile_id="p", base_url="http://x", endpoint="/e",
        judge=None, pass_threshold=0.7,
    )
    assert result["tool_assertion_passed"] is None
    assert result["missing_expected_tools"] is None
    assert result["forbidden_tools_used"] is None
    # Smoke-only pass: non-empty 2xx body, unaffected by the tool gate.
    assert result["passed"] is True


# --------------------------------------------------------------------------- #
# 3. forbidden_tools pass/fail
# --------------------------------------------------------------------------- #

def test_run_scenario_forbidden_tools_pass_when_not_called(monkeypatch):
    monkeypatch.setattr(
        evals, "_invoke_endpoint",
        _fake_invoke_endpoint_factory([], answer="Answered without delegating."),
    )
    result = evals._run_scenario(
        {
            "id": "s4", "question": "q",
            "forbidden_tools": ["ask_business_reporting", "read_skill"],
        },
        profile_id="p", base_url="http://x", endpoint="/e",
        judge=None, pass_threshold=0.7,
    )
    assert result["passed"] is True
    assert result["tool_assertion_passed"] is True
    assert result["forbidden_tools_used"] is None


def test_run_scenario_forbidden_tools_fail_when_called(monkeypatch):
    monkeypatch.setattr(
        evals, "_invoke_endpoint",
        _fake_invoke_endpoint_factory(["read skill"], answer="Delegated anyway."),
    )
    result = evals._run_scenario(
        {
            "id": "s5", "question": "q",
            "forbidden_tools": ["ask_business_reporting", "read_skill"],
        },
        profile_id="p", base_url="http://x", endpoint="/e",
        judge=None, pass_threshold=0.7,
    )
    assert result["passed"] is False
    assert result["tool_assertion_passed"] is False
    assert result["forbidden_tools_used"] == ["read_skill"]


# --------------------------------------------------------------------------- #
# 5. Hard gate overrides a perfect judge score
# --------------------------------------------------------------------------- #

class _FakeJudge:
    """Matches the pattern in tests/admin/test_rubric_judge.py."""

    def __init__(self, payload):
        self.payload = payload

    def invoke(self, msgs):
        import json
        return type("R", (), {"content": json.dumps(self.payload)})()


def test_run_scenario_expected_tools_fail_overrides_perfect_judge_score(monkeypatch):
    monkeypatch.setattr(
        evals, "_invoke_endpoint",
        _fake_invoke_endpoint_factory([], answer="A perfectly good direct answer."),
    )
    judge = _FakeJudge({"score": 1.0, "pass": True, "rationale": "flawless"})
    result = evals._run_scenario(
        {
            "id": "s6", "question": "q", "expected_answer": "reference answer",
            "expected_tools": ["ask_business_reporting"],
        },
        profile_id="p", base_url="http://x", endpoint="/e",
        judge=judge, pass_threshold=0.7,
    )
    assert result["judge"]["score"] == 1.0
    assert result["judge"]["pass"] is True
    # The tool assertion overrides passed even though the judge loved it.
    assert result["passed"] is False
    assert result["tool_assertion_passed"] is False


# --------------------------------------------------------------------------- #
# 6. Run-level status honors tool-assertion hard-fails
#
# A run whose scenarios ALL score 1.0 from the judge — but one hard-fails
# its expected_tools assertion — must not read "passed" at the top level.
# Before this fix, `run_eval_suite_sync`'s aggregate `status` was derived
# purely from the mean judge score (`score >= pass_threshold`), which never
# looked at the per-scenario `tool_assertion_passed` field `_run_scenario`
# already persists — so a mean score of 1.0 always read "passed" even when
# every scenario "passed" only because the tool gate was ignored.
# --------------------------------------------------------------------------- #


class _FakeEvalTable:
    """Minimal fluent Supabase table fake — just enough surface for
    `run_eval_suite_sync` (select/eq/limit/update/execute)."""

    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []
        self._filters: Dict[str, Any] = {}
        self._limit: Optional[int] = None
        self._pending_update: Optional[Dict[str, Any]] = None

    def select(self, _cols: str = "*") -> "_FakeEvalTable":
        return self

    def eq(self, col: str, val: Any) -> "_FakeEvalTable":
        self._filters[col] = val
        return self

    def limit(self, n: int) -> "_FakeEvalTable":
        self._limit = n
        return self

    def update(self, payload: Dict[str, Any]) -> "_FakeEvalTable":
        self._pending_update = dict(payload)
        return self

    def _matches(self, row: Dict[str, Any]) -> bool:
        return all(row.get(k) == v for k, v in self._filters.items())

    def execute(self) -> Any:
        if self._pending_update is not None:
            updated = []
            for r in self.rows:
                if self._matches(r):
                    r.update(self._pending_update)
                    updated.append(dict(r))
            self._pending_update = None
            self._filters = {}
            return type("Result", (), {"data": updated})()
        matched = [dict(r) for r in self.rows if self._matches(r)]
        self._filters = {}
        if self._limit is not None:
            matched = matched[: self._limit]
        return type("Result", (), {"data": matched})()


class _FakeEvalClient:
    def __init__(self) -> None:
        self._tables: Dict[str, _FakeEvalTable] = {}

    def table(self, name: str) -> _FakeEvalTable:
        return self._tables.setdefault(name, _FakeEvalTable())


def test_run_eval_suite_status_fails_when_a_scenario_hard_fails_tool_assertion(monkeypatch):
    """3 scenarios, judge scores 1.0 across the board (mean score == 1.0,
    well above the 0.7 threshold), but scenario 2's expected_tools was never
    called → its tool_assertion_passed is False and its `passed` is False.
    The run-level status must read 'failed', not 'passed', and the
    per-scenario results must show exactly 1 failure (fail count reflects
    it) even though every judge score was perfect."""
    from admin import evals

    client = _FakeEvalClient()
    client.table("agent_models").rows.append(
        {"model_id": "model-1", "model_string": "test-judge-model"}
    )
    client.table("agent_profiles").rows.append({
        "profile_id": "prof-1",
        "agent_kind": "business",
        "endpoint_path": "/agent/test",
        "environment": "dev",
    })
    scenarios = [
        {"id": "s1", "question": "q1", "expected_answer": "ref1",
         "expected_tools": ["ask_business_reporting"]},
        {"id": "s2", "question": "q2", "expected_answer": "ref2",
         "expected_tools": ["ask_business_reporting"]},  # will NOT be called
        {"id": "s3", "question": "q3", "expected_answer": "ref3"},
    ]
    client.table("agent_eval_runs").rows.append({
        "run_id": "run-1",
        "suite_id": "suite-1",
        "profile_id": "prof-1",
        "status": "queued",
        "context": {},
        "agent_eval_suites": {
            "suite_id": "suite-1",
            "name": "test_suite",
            "agent_kind": "business",
            "scenarios": scenarios,
            "pass_threshold": 0.7,
            "evaluator_model_id": "model-1",
            "default_context": {},
        },
    })

    monkeypatch.setattr(evals, "get_service_client", lambda: client)
    judges_built = []
    monkeypatch.setattr(
        evals, "_try_build_judge",
        lambda _model_string, model_id=None: judges_built.append(model_id) or _FakeJudge(
            {"score": 1.0, "pass": True, "rationale": "flawless"}
        ),
    )

    def fake_invoke_endpoint(*_a, scenario=None, metrics=None, **_k):
        # s2 never calls the expected tool; s1/s3 do (s3 has no assertion).
        called = [] if scenario and scenario.get("id") == "s2" else [
            "ask business reporting"
        ]
        if metrics is not None:
            metrics["ttft_ms"] = 10
            metrics["usage"] = {"input_tokens": 1, "output_tokens": 1}
            metrics["runmetrics"] = {"iterations": 1, "tool_count": len(called)}
            metrics["executed_tools"] = called
        return ("A great answer.", "", 200, 10)

    monkeypatch.setattr(evals, "_invoke_endpoint", fake_invoke_endpoint)

    evals.run_eval_suite_sync(run_id="run-1")

    final = client.table("agent_eval_runs").rows[0]
    # Mean judge score is a perfect 1.0 — the pre-fix bug read this alone.
    assert final["score"] == 1.0
    # The run must NOT read "passed" despite the perfect score.
    assert final["status"] == "failed"
    results = final["results"]
    assert len(results) == 3
    failed = [r for r in results if not r["passed"]]
    assert len(failed) == 1
    assert failed[0]["scenario_id"] == "s2"
    assert failed[0]["tool_assertion_passed"] is False
    assert failed[0]["judge"]["score"] == 1.0  # score untouched by the gate
    # The judge is the suite's exact agent_models row, not the first row
    # that shares its model_string — including the per-thread judges the
    # runner builds when attempts run concurrently.
    assert judges_built and set(judges_built) == {"model-1"}


def test_expected_tools_match_raw_tool_ids_too(monkeypatch):
    """A scenario with per-turn `checks` reports its executed tools as raw
    ids (`lookup_customer`), not display names. The assertion compared the
    canonicalised expected name against that raw set, so such a scenario
    failed with `missing_expected_tools: ['lookup_customer']` right beside
    `executed_tools: ['lookup_customer']`."""
    monkeypatch.setattr(evals, "_invoke_endpoint",
                        _fake_invoke_endpoint_factory(["lookup_customer"]))
    result = evals._run_scenario(
        {"id": "s", "question": "q", "expected_tools": ["lookup_customer"],
         "forbidden_tools": ["open_ticket"]},
        profile_id="p", base_url="http://x", endpoint="/e",
        judge=None, pass_threshold=0.7,
    )
    assert result["missing_expected_tools"] is None
    assert result["tool_assertion_passed"] is True and result["passed"] is True

    monkeypatch.setattr(evals, "_invoke_endpoint",
                        _fake_invoke_endpoint_factory(["open_ticket", "lookup_customer"]))
    result = evals._run_scenario(
        {"id": "s", "question": "q", "expected_tools": ["lookup_customer"],
         "forbidden_tools": ["open_ticket"]},
        profile_id="p", base_url="http://x", endpoint="/e",
        judge=None, pass_threshold=0.7,
    )
    assert result["forbidden_tools_used"] == ["open_ticket"]
    assert result["tool_assertion_passed"] is False


def test_without_a_judge_the_expected_answer_is_matched_as_words(monkeypatch):
    """The suite editor's default is "Judge model: None — check expected words
    only", but a case with an expected answer was failed unread, so a suite
    written in the portal could not pass a single case."""
    monkeypatch.setattr(evals, "_invoke_endpoint", _fake_invoke_endpoint_factory(
        [], answer="You are on the  Fiber 300 plan."))
    hit = evals._run_scenario({"id": "s", "question": "q", "expected_answer": "fiber 300"},
                              profile_id="p", base_url="http://x", endpoint="/e",
                              judge=None, pass_threshold=0.7)
    assert hit["passed"] is True and "contains the expected text" in hit["grading_note"]
    miss = evals._run_scenario({"id": "s", "question": "q", "expected_answer": "Fiber 1000"},
                               profile_id="p", base_url="http://x", endpoint="/e",
                               judge=None, pass_threshold=0.7)
    assert miss["passed"] is False and "judge model" in miss["grading_note"]
