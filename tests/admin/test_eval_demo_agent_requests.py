"""Test-suite runs reach the AI-agents demo agents with the demo's own body.

`POST /agents/chat/stream` (endpoints/agents_portal.AgentsChatRequest) needs
`agent` — the demo's agent KEY — and plays a `case_id`; the eval runner built
the generic `/chat/stream` body, so every case of every suite run against a
demo agent failed 422 `body.agent: Field required`. #230 fixed the Test
drawer + MCP `chat` tool with `admin.services.test_chat`; the runner now goes
through the same builder.

The fake transport below validates every POST with the REAL request model, so
these tests fail with the production symptom (422 on every case) when the
runner sends the generic body.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

from admin import evals  # noqa: E402
from endpoints.agents_portal import AgentsChatRequest  # noqa: E402
from tests.scoped_build import full_build, use_orchard, use_agents_demo

_PROFILE = {"agent_kind": "agents_sales", "endpoint_path": "/agents/chat/stream",
            "environment": "staging"}


class _Q:
    def __init__(self, db: "_DB", name: str) -> None:
        self.db, self.name, self._update = db, name, None

    def __getattr__(self, _attr):  # select / eq / limit / order / in_ …
        return lambda *a, **k: self

    def update(self, payload):
        self._update = payload
        return self

    def execute(self):
        if self._update is not None:
            self.db.updates.setdefault(self.name, []).append(self._update)
            return type("R", (), {"data": []})()
        return type("R", (), {"data": list(self.db.rows.get(self.name, []))})()


class _DB:
    def __init__(self, scenarios: List[Dict[str, Any]]) -> None:
        self.updates: Dict[str, List[Dict[str, Any]]] = {}
        self.rows = {
            "agent_eval_runs": [{
                "run_id": "r1", "profile_id": "p1", "context": {},
                "agent_eval_suites": {"suite_id": "s1", "name": "Demo suite",
                                      "agent_kind": "agents_sales", "pass_threshold": 0.5,
                                      "scenarios": scenarios},
            }],
            "agent_profiles": [{"model_main_id": None}],
        }

    def table(self, name):
        return _Q(self, name)


class _Transport:
    """Stands in for httpx.Client: validates the body like FastAPI would."""

    def __init__(self) -> None:
        self.bodies: List[Dict[str, Any]] = []
        self.urls: List[str] = []

    def __call__(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, method, url, json=None, headers=None):  # noqa: A002
        self.urls.append(url)
        self.bodies.append(dict(json or {}))
        return _Resp(json or {}, len(self.bodies))


class _Resp:
    def __init__(self, body: Dict[str, Any], n: int) -> None:
        try:
            AgentsChatRequest(**body)
            self.status_code = 200
            self._text = (
                'data: {"type": "answer", "content": "Hello from Rami"}\n\n'
                f'data: {{"type": "state", "token": "tok-{n}"}}\n\n'
                'data: {"type": "done"}\n\n'
            )
        except Exception as exc:  # pydantic ValidationError → FastAPI's 422
            self.status_code = 422
            self._text = json.dumps({"detail": str(exc)})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_text(self):
        yield self._text


@pytest.fixture
def run_suite(monkeypatch):
    full_build(monkeypatch)

    def _run(scenarios):
        db, transport = _DB(scenarios), _Transport()
        monkeypatch.setattr(evals, "admin_service_client", lambda: db)
        monkeypatch.setattr(evals, "_resolve_profile", lambda c, pid: dict(_PROFILE))
        monkeypatch.setattr(evals, "_base_url_for", lambda c, env: "http://engine.test")
        monkeypatch.setattr(evals.httpx, "Client", transport)
        evals.run_eval_suite_sync("r1")
        final = [u for u in db.updates.get("agent_eval_runs", []) if u.get("status")][-1]
        return final, transport

    return _run


def test_demo_suite_run_sends_agent_and_case(run_suite):
    final, transport = run_suite([
        {"id": "hello", "question": "Hi, who is this?", "expected_substrings": ["Rami"]},
    ])
    body = transport.bodies[0]
    assert body["agent"] == "sales"            # the demo KEY, not the agent_kind
    assert body["case_id"]                      # a demo agent always plays a case
    assert "business_id" not in body and "agent_kind" not in body
    assert transport.urls[0].endswith("/agents/chat/stream")
    results = final["results"]
    assert [r["http_status"] for r in results] == [200]
    assert final["status"] == "passed", (final.get("error"), [(r.get("passed"), r.get("missing_substrings"), r.get("grading_note"), r.get("check_results")) for r in final["results"]])


def test_demo_multi_turn_echoes_state_and_honours_case(run_suite):
    final, transport = run_suite([{
        "id": "two-turns", "case_id": "O02",
        "turns": [{"question": "Hello", "checks": [{"type": "regex", "pattern": "Rami"}]},
                  {"question": "Yes, go on", "checks": [{"type": "no_uuid"}]}],
    }])
    first, second = transport.bodies
    assert first["case_id"] == second["case_id"] == "O02"
    assert first["session_id"] == second["session_id"]
    assert "state" not in first
    assert second["state"] == "tok-1"           # verification rides the token
    assert final["status"] == "passed", (final.get("error"), [(r.get("passed"), r.get("missing_substrings"), r.get("grading_note"), r.get("check_results")) for r in final["results"]])


def test_demo_suite_on_a_build_without_the_demo_errors_plainly(run_suite, monkeypatch):
    use_orchard(monkeypatch)
    final, transport = run_suite([{"id": "x", "question": "hi"}])
    assert transport.bodies == []
    assert final["status"] == "errored"
    assert "doesn't serve the AI-agents demo" in final["error"]


def test_generic_kinds_keep_their_body():
    body = evals._payload_for({"question": "hi"}, run_context={"business_id": "b1"})
    assert body["business_id"] == "b1" and "agent" not in body


def test_voice_eval_reaches_demo_agent_on_this_builds_ws(monkeypatch):
    """Voice suites dial the demo's WS where THIS build mounts it, with a case."""
    from admin.services.test_chat import voice_target

    use_agents_demo(monkeypatch)
    assert voice_target(_PROFILE) == {"ws_path": "/agents/voice/live", "call_case": "O01"}
    full_build(monkeypatch)
    assert voice_target(_PROFILE)["ws_path"] == "/voice/live"
    assert voice_target({"agent_kind": "business"}) is None
