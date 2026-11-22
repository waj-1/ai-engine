"""The unhandled-exception handler must not leak internals to callers.

`main.general_exception_handler` gated on `ENVIRONMENT == "production"`.
Read off the running services, the live values are:

  * `ai-engine`         -> `production`  — prod chat, matched
  * `ai-engine-control` -> `dev`         — did NOT match
  * the client demo VMs    -> `staging`     — did NOT match

So it protected the prod chat service and nothing else, and the two that
slipped through are both internet-facing — the control-plane
(aiengine.staging.example) being the one service holding the staging *and* prod
Supabase service keys.

Widening the match wouldn't fix it: the control-plane's env is legitimately
`dev` (it's the cross-env admin portal), so no ENVIRONMENT value separates
"laptop" from "deployed service". The handler now fails closed and requires
an explicit `EXPOSE_ERROR_DETAILS` opt-in; the traceback still goes to the
server log unconditionally.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

_SECRET = "connection refused to postgres://user:hunter2@10.0.0.5:5432/waj"

_SANITIZED = "An internal error occurred. Please try again later."


def _invoke_handler(monkeypatch, *, environment=None, expose=None):
    """Call the real handler with env applied, return the parsed body."""
    import main

    for name, value in (("ENVIRONMENT", environment), ("EXPOSE_ERROR_DETAILS", expose)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    response = asyncio.run(
        main.general_exception_handler(None, RuntimeError(_SECRET))
    )
    assert response.status_code == 500
    return json.loads(response.body)


# Every value any real deployment actually sets, plus the historical one.
@pytest.mark.parametrize(
    "environment",
    ["prod", "production", "staging", "dev", "development", "PROD", "canary-eu", None],
)
def test_no_environment_value_leaks_exception_details(monkeypatch, environment):
    """Default is closed for EVERY environment — including `dev`, which the
    internet-facing control-plane portal sets."""
    body = _invoke_handler(monkeypatch, environment=environment)

    assert "details" not in body["error"], (
        f"ENVIRONMENT={environment!r} leaked internals to the client"
    )
    assert _SECRET not in json.dumps(body)
    assert body["error"] == {"code": 500, "message": _SANITIZED}


@pytest.mark.parametrize("flag", ["1", "true", "TRUE", "yes", "on"])
def test_explicit_opt_in_shows_details(monkeypatch, flag):
    """A developer who asks for detail on their own box still gets it."""
    body = _invoke_handler(monkeypatch, environment="development", expose=flag)

    assert body["error"]["details"] == _SECRET


@pytest.mark.parametrize("flag", ["", "0", "false", "no", "off", "maybe"])
def test_opt_in_requires_an_affirmative_value(monkeypatch, flag):
    """Anything that isn't an affirmative opt-in stays sanitized."""
    body = _invoke_handler(monkeypatch, environment="development", expose=flag)

    assert "details" not in body["error"]


def test_opt_in_does_not_override_a_production_deployment_by_accident(monkeypatch):
    """The flag is the ONLY switch — documenting that setting it in prod is a
    deliberate operator act, not something ENVIRONMENT can silently undo."""
    body = _invoke_handler(monkeypatch, environment="prod", expose="true")

    assert body["error"]["details"] == _SECRET
