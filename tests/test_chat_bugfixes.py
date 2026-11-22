"""Regression tests for three pre-existing bugs surfaced during the
standalone-AI-engine verification:

1. Non-UUID session ids silently failed to persist (``ai_chat_sessions.id`` is
   ``uuid`` but some agents' request schemas accept arbitrary strings).
2. ``root_span`` raised ``Token created in a different Context`` during async
   SSE generator teardown.
3. (verified live) the business tool loop could exhaust without an answer.
"""
import asyncio
import sys
import types
import uuid
from unittest.mock import MagicMock

import core.chat_persistence as cp
from core import observability
from core.chat_persistence import coerce_session_id


# --- Bug 1: session-id coercion ---------------------------------------------

def test_coerce_passes_through_valid_uuid():
    u = str(uuid.uuid4())
    assert coerce_session_id(u) == u


def test_coerce_maps_non_uuid_deterministically():
    a = coerce_session_id("clean-sales-1")
    b = coerce_session_id("clean-sales-1")
    assert a == b                      # stable across turns
    uuid.UUID(a)                       # is a valid uuid
    assert a != "clean-sales-1"
    assert coerce_session_id("other-session") != a   # distinct input -> distinct id


def test_coerce_none_passthrough():
    assert coerce_session_id(None) is None


def test_ensure_session_writes_a_uuid_for_non_uuid_id(monkeypatch):
    captured = {}

    class _Q:
        def schema(self, *_): return self
        def table(self, *_): return self
        def upsert(self, row, **_k): captured["row"] = row; return self
        def execute(self): return None

    monkeypatch.setattr(cp, "get_service_client", lambda: _Q())
    asyncio.run(cp.ensure_session("clean-sales-1", business_id="biz", title="t"))
    # The id actually written must be a valid uuid (would otherwise be rejected
    # by the uuid column), and stable w.r.t. the coercion helper.
    uuid.UUID(captured["row"]["id"])
    assert captured["row"]["id"] == coerce_session_id("clean-sales-1")


# --- Bug 2: root_span tolerates a cross-context detach error ------------------

def test_root_span_swallows_contextvar_detach_error(monkeypatch):
    class _CM:
        def __enter__(self):
            return "span"

        def __exit__(self, *_a):
            raise ValueError("Token <x> was created in a different Context")

    class _Laminar:
        @staticmethod
        def start_as_current_span(name):
            return _CM()

        @staticmethod
        def set_span_attributes(flat):
            pass

        @staticmethod
        def set_trace_metadata(flat):
            pass

    fake = types.ModuleType("lmnr")
    fake.Laminar = _Laminar
    monkeypatch.setitem(sys.modules, "lmnr", fake)

    # Entering AND exiting the context manager must not raise, even though the
    # underlying span __exit__ throws the contextvar detach error.
    with observability.root_span("sample_agent.agent", {"waj.agent_kind": "sample_agent"}) as span:
        assert span == "span"
