"""sse_response must open a Laminar ROOT span that wraps the whole stream.

Regression for the observability bug where the Live Monitor showed
"ClickHouse unavailable" and Observability rendered every trace as "legacy":
`Laminar.initialize()` only instruments the LLM providers — it never creates a
per-request FastAPI span — and the endpoints called `tag_active_span(...)`
BEFORE the agent (and its auto-instrumented spans) started, so the
`waj.agent_kind` / `waj.channel.*` attributes never landed on the trace's TOP
span.

The fix wraps the streaming body in `core.observability.root_span(...)`. These
tests prove (a) the span opens with the right name + attributes and (b) the
agent generator actually iterates *inside* that span context — i.e. the LLM
child spans it spawns will nest under it and inherit the trace.
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


class _FakeLaminar:
    """Tracks the current span so we can assert child work nests correctly."""

    def __init__(self):
        self.current = None
        self.opened: list[str] = []
        self.attrs: dict[int, dict] = {}

    def start_as_current_span(self, name):
        span = {"name": name}

        @contextlib.contextmanager
        def _cm():
            prev = self.current
            self.current = span
            self.opened.append(name)
            try:
                yield span
            finally:
                self.current = prev

        return _cm()

    def set_span_attributes(self, attrs):
        if self.current is not None:
            self.attrs.setdefault(id(self.current), {}).update(attrs)


async def _drain(resp):
    return [chunk async for chunk in resp.body_iterator]


@pytest.fixture
def fake_lmnr(monkeypatch):
    import lmnr

    fake = _FakeLaminar()
    monkeypatch.setattr(lmnr, "Laminar", fake)
    return fake


async def test_root_span_wraps_stream_and_carries_agent_kind(fake_lmnr):
    from common.sse import sse_response

    seen = {}

    async def _agent():
        # Capture the active span at the moment the agent runs — proves the
        # agent (and any LLM spans it creates) execute INSIDE the root span.
        seen["current"] = fake_lmnr.current
        yield {"type": "thought", "content": "hi"}
        yield {"type": "done"}

    resp = sse_response(
        lambda: _agent(),
        trace_name="sample_agent.agent",
        trace_attributes={"waj.agent_kind": "business", "waj.channel.slug": "web"},
    )
    chunks = await _drain(resp)

    # The stream still produced its SSE frames.
    assert any("thought" in c for c in chunks)
    assert any("done" in c for c in chunks)

    # A single root span named for the agent was opened.
    assert fake_lmnr.opened == ["sample_agent.agent"]

    # The agent ran while that span was current (child spans would nest).
    assert seen["current"] is not None
    assert seen["current"]["name"] == "sample_agent.agent"

    # agent_kind + channel landed ON the root span.
    span_attrs = fake_lmnr.attrs[id(seen["current"])]
    assert span_attrs["waj.agent_kind"] == "business"
    assert span_attrs["waj.channel.slug"] == "web"


async def test_no_trace_name_opens_no_span_but_still_streams(fake_lmnr):
    from common.sse import sse_response

    async def _agent():
        yield {"type": "done"}

    resp = sse_response(lambda: _agent())  # no trace_name
    chunks = await _drain(resp)

    assert any("done" in c for c in chunks)
    assert fake_lmnr.opened == []  # nullcontext path — no span opened


def _error_event_from_chunks(chunks):
    import json

    for c in chunks:
        for line in c.splitlines():
            if line.startswith("data:"):
                evt = json.loads(line[len("data:"):].strip())
                if evt.get("type") == "error":
                    return evt
    return None


async def test_factory_failure_exposes_detail_outside_production(fake_lmnr, monkeypatch):
    """The admin 'Test agent' widget hits the same SSE path. Outside production
    the operator must see the actual exception (e.g. a missing agent_profile),
    not just the generic message — that opacity is exactly what blocked
    diagnosing the local Test-agent 500."""
    monkeypatch.setenv("ENVIRONMENT", "development")
    from common.sse import sse_response

    def _boom():
        raise RuntimeError("no active agent_profiles row for kind=business")

    resp = sse_response(_boom, trace_name="sample_agent.agent")
    evt = _error_event_from_chunks(await _drain(resp))

    assert evt is not None
    assert "no active agent_profiles row" in evt["message"]
    assert "RuntimeError" in evt["message"]
    assert "no active agent_profiles row" in evt["detail"]


async def test_midstream_failure_hidden_in_production(fake_lmnr, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    from common.sse import sse_response

    async def _agent():
        yield {"type": "thought", "content": "hi"}
        raise RuntimeError("secret internal detail")

    resp = sse_response(lambda: _agent(), trace_name="sample_agent.agent")
    evt = _error_event_from_chunks(await _drain(resp))

    assert evt is not None
    assert "secret internal detail" not in evt["message"]
    assert "detail" not in evt  # production never leaks the exception text
    assert evt["message"] == "An error occurred while processing your request. Please try again."
