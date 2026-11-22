"""Regression tests for the e2e runner (admin/e2e_runner.py).

Covers two fixes:

* BUG A (evals-2): the smoke check must accept ANY 2xx (matching evals.py and
  eval_runs_handlers.py), not strictly ``== 200``. A 201/202/204 with a
  non-empty body smoke-passes; a 5xx does not.
* BUG B (evals-3): when scenarios from several active suites are flattened into
  one run, each scenario must be graded against its OWN suite's
  ``pass_threshold`` rather than ``suites[0]``'s threshold for everyone.

These drive the real (synchronous, dict-based) API: ``_run_one_scenario`` uses
``httpx.Client`` and returns a dict; ``run_e2e`` resolves a profile + active
suites through ``get_service_client``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

from admin import e2e_runner


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


class _FakeSyncClient:
    """Stand-in for ``httpx.Client`` (sync context manager)."""

    def __init__(self, resp: _FakeResp):
        self._resp = resp

    def __call__(self, *a, **k):  # httpx.Client(timeout=...)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, *a, **k):
        return self._resp


class _Builder:
    def __init__(self, table: str, store: Dict[str, Any]):
        self.table = table
        self.store = store
        self._op = "select"
        self._payload = None

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def execute(self):
        if self._op == "update":
            self.store.setdefault("updates", []).append(self._payload)
            return type("R", (), {"data": []})()
        if self._op == "insert":
            return type("R", (), {"data": [{}]})()
        return type("R", (), {"data": self.store["tables"].get(self.table, [])})()


class _FakeClient:
    def __init__(self, store: Dict[str, Any]):
        self.store = store

    def table(self, name: str):
        return _Builder(name, self.store)


# --------------------------------------------------------------------------- #
# BUG A — smoke check accepts any 2xx
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", [200, 201, 202, 204])
def test_smoke_ok_accepts_any_2xx(monkeypatch, status):
    monkeypatch.setattr(
        e2e_runner.httpx, "Client", _FakeSyncClient(_FakeResp(status, '{"reply": "hi"}'))
    )
    out = e2e_runner._run_one_scenario(
        "http://agent.local", "/chat", {"id": "s1", "question": "hi"},
        judge=None, pass_threshold=0.7,
    )
    assert out["passed"] is True
    assert 200 <= out["http_status"] < 300


def test_smoke_fails_on_5xx(monkeypatch):
    monkeypatch.setattr(
        e2e_runner.httpx, "Client", _FakeSyncClient(_FakeResp(500, "internal error"))
    )
    out = e2e_runner._run_one_scenario(
        "http://agent.local", "/chat", {"id": "s1", "question": "hi"},
        judge=None, pass_threshold=0.7,
    )
    assert out["passed"] is False


def test_smoke_fails_on_empty_2xx_body(monkeypatch):
    """A 2xx with an empty body is NOT a smoke pass."""
    monkeypatch.setattr(
        e2e_runner.httpx, "Client", _FakeSyncClient(_FakeResp(200, "   "))
    )
    out = e2e_runner._run_one_scenario(
        "http://agent.local", "/chat", {"id": "s1", "question": "hi"},
        judge=None, pass_threshold=0.7,
    )
    assert out["passed"] is False


# --------------------------------------------------------------------------- #
# BUG B — each suite's scenarios graded against its OWN threshold
# --------------------------------------------------------------------------- #
def test_each_suite_uses_its_own_pass_threshold(monkeypatch):
    """Two active suites, thresholds 0.6 and 0.9, fixed judge score 0.75:
    the lenient suite's scenario passes, the strict suite's fails. If every
    scenario were graded against suites[0]'s 0.6 threshold, both would pass."""
    suite_lenient = {
        "suite_id": "s-lenient", "agent_kind": "booking", "is_active": True,
        "pass_threshold": 0.6,
        "scenarios": [{"id": "lenient-scn", "question": "q", "expected_answer": "a"}],
    }
    suite_strict = {
        "suite_id": "s-strict", "agent_kind": "booking", "is_active": True,
        "pass_threshold": 0.9,
        "scenarios": [{"id": "strict-scn", "question": "q", "expected_answer": "a"}],
    }
    store: Dict[str, Any] = {
        "tables": {
            "agent_profiles": [
                {"profile_id": "p1", "agent_kind": "booking", "endpoint_path": "/chat"}
            ],
            "agent_eval_suites": [suite_lenient, suite_strict],
        },
        "updates": [],
    }

    monkeypatch.setattr(e2e_runner, "get_service_client", lambda: _FakeClient(store))
    monkeypatch.setattr(e2e_runner, "_try_build_judge", lambda model, **_kw: object())
    # The judge always scores 0.75; pass is computed against the threshold it
    # is HANDED (so this proves each scenario gets its own suite's threshold).
    monkeypatch.setattr(
        e2e_runner, "_invoke_judge",
        lambda judge, q, e, body, threshold: {
            "score": 0.75, "pass": 0.75 >= threshold, "rationale": "stub",
        },
    )
    monkeypatch.setattr(
        e2e_runner.httpx, "Client", _FakeSyncClient(_FakeResp(200, '{"reply": "ok"}'))
    )

    out = e2e_runner.run_e2e(profile_id="p1", environment="dev")

    final = store["updates"][-1]
    results: List[Dict[str, Any]] = final["results"]
    by_id = {r["id"]: r for r in results}
    assert by_id["lenient-scn"]["passed"] is True   # 0.75 >= 0.6
    assert by_id["strict-scn"]["passed"] is False    # 0.75 <  0.9
    assert final["pass_count"] == 1
    assert final["fail_count"] == 1


def test_judge_is_built_from_the_suites_exact_model_row(monkeypatch):
    """model_string is not unique in agent_models, so the run hands the
    suite's evaluator_model_id down to the judge builder."""
    store: Dict[str, Any] = {
        "tables": {
            "agent_profiles": [
                {"profile_id": "p1", "agent_kind": "booking", "endpoint_path": "/chat"}
            ],
            "agent_eval_suites": [{
                "suite_id": "s1", "agent_kind": "booking", "is_active": True,
                "evaluator_model_id": "m-2", "scenarios": [],
            }],
            "agent_models": [{"model_id": "m-2", "model_string": "gpt-5-mini"}],
        },
        "updates": [],
    }
    built = []
    monkeypatch.setattr(e2e_runner, "get_service_client", lambda: _FakeClient(store))
    monkeypatch.setattr(e2e_runner, "_try_build_judge",
                        lambda model, model_id=None: built.append((model, model_id)))
    e2e_runner.run_e2e(profile_id="p1", environment="dev")
    assert built == [("gpt-5-mini", "m-2")]


def test_try_build_judge_routes_claude_to_direct_anthropic(monkeypatch):
    """A claude-* evaluator must build a Claude judge, not a Gemini one — passing
    a claude id to ChatGoogleGenerativeAI silently scored every scenario 0.0.

    The route is the DIRECT Anthropic API since 2026-08-16 (it was
    Anthropic-on-Vertex, whose GCP ADC requirement does not exist on Azure
    compute, so the branch had silently stopped producing a judge at all).
    `location: "global"` is deliberately gone — that was a Vertex-only knob."""
    import runtime.llm_factory as lf
    cap = {}
    monkeypatch.setattr(lf, "_build_anthropic", lambda m: cap.update(m) or "CLAUDE_JUDGE")
    judge = e2e_runner._try_build_judge("claude-sonnet-4-6")
    assert judge == "CLAUDE_JUDGE"
    assert cap["model_string"] == "claude-sonnet-4-6"
    assert cap["max_output_tokens"] == 4096
    assert "location" not in cap["extra_config"]
    assert not cap["extra_config"].get("omit_temperature")  # Sonnet keeps temperature


def test_try_build_judge_does_not_use_a_vertex_builder(monkeypatch):
    """Guards the regression that would reintroduce the GCP dependency: if the
    Claude branch reached for a Vertex builder again it would raise ImportError,
    get swallowed by the broad `except`, and silently return None (smoke-only) —
    the exact failure this repoint fixed."""
    import runtime.llm_factory as lf
    assert not hasattr(lf, "_build_anthropic_vertex")
    monkeypatch.setattr(lf, "_build_anthropic", lambda m: "CLAUDE_JUDGE")
    assert e2e_runner._try_build_judge("claude-sonnet-4-6") == "CLAUDE_JUDGE"


def test_try_build_judge_opus_omits_temperature(monkeypatch):
    import runtime.llm_factory as lf
    cap = {}
    monkeypatch.setattr(lf, "_build_anthropic", lambda m: cap.update(m) or "J")
    e2e_runner._try_build_judge("claude-opus-4-8")
    assert cap["extra_config"]["omit_temperature"] is True


def test_try_build_judge_returns_none_when_claude_builder_raises(monkeypatch):
    """Any build failure must degrade to smoke-only, never propagate into the
    run.

    NB this mocks the builder into raising — see the test below for the REAL
    missing-key case, whose premise this one does not actually establish."""
    import runtime.llm_factory as lf

    def _boom(_m):
        raise RuntimeError("no credential")

    monkeypatch.setattr(lf, "_build_anthropic", _boom)
    assert e2e_runner._try_build_judge("claude-sonnet-4-6") is None


def test_try_build_judge_is_none_when_anthropic_api_key_is_actually_unset(monkeypatch):
    """The real case, with the REAL builder.

    `ChatAnthropic` constructs happily with `api_key=''` (that is the field's
    own default) and 401s on every call, so an unset ANTHROPIC_API_KEY used to
    yield a non-None judge whose every invoke failed — caught by
    `_run_one_scenario` as `{'score': 0.5, 'pass': False, 'rationale':
    'judge_error: …'}`. A red run scored 0.5, after a ~30s failed API
    round-trip per scenario, instead of the documented smoke-only degrade.
    ANTHROPIC_API_KEY is set nowhere in this repo — no workflow, not in
    DEPLOY.md, commented out in .env.example — so this was what the first
    post-merge claude-evaluator run would have done."""
    import sys as _sys
    import types as _types

    built = []
    fake = _types.ModuleType("langchain_anthropic")
    fake.ChatAnthropic = lambda **kw: built.append(kw) or "CLAUDE_JUDGE"
    monkeypatch.setitem(_sys.modules, "langchain_anthropic", fake)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert e2e_runner._try_build_judge("claude-sonnet-4-6") is None
    assert built == [], "a keyless judge must not be constructed at all"

    # …and with a key present the same path still produces one.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert e2e_runner._try_build_judge("claude-sonnet-4-6") == "CLAUDE_JUDGE"


def test_try_build_judge_gemini_path_unchanged(monkeypatch):
    import sys as _sys, types as _types
    monkeypatch.setenv("GOOGLE_API_KEY", "x")
    sent = {}
    fake = _types.ModuleType("langchain_google_genai")
    fake.ChatGoogleGenerativeAI = lambda **kw: sent.update(kw) or "GEMINI_JUDGE"
    monkeypatch.setitem(_sys.modules, "langchain_google_genai", fake)
    assert e2e_runner._try_build_judge("gemini-3-flash-preview") == "GEMINI_JUDGE"
    assert sent["model"] == "gemini-3-flash-preview"
