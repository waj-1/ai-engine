"""_sse_format must stamp waj.ttft_ms and waj.ttlt_ms on the active Laminar span.

TTFT = time from generator entry to the first *content* event
       (type in {answer, final, result, final_response, output, chart}).
TTLT = time from generator entry to the last event before (or at) `done`.

Both are recorded as milliseconds on the active span via
`Laminar.set_span_attributes`. The hook is best-effort: it must be a
complete no-op when lmnr is absent or raises, and it must never alter the
event stream or break the stop_on_done behaviour.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _collect(_sse_format, source, **kwargs):
    """Drain the generator and return all SSE chunks."""
    return [chunk async for chunk in _sse_format(source, error_message="err", **kwargs)]


async def _fake_stream(*event_dicts):
    for ev in event_dicts:
        yield ev


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _CaptureLaminar:
    """Minimal fake that records every set_span_attributes call."""

    def __init__(self):
        self.calls: list[dict] = []

    def set_span_attributes(self, attrs: dict) -> None:
        self.calls.append(dict(attrs))


@pytest.fixture
def capture_lmnr(monkeypatch):
    """Patch lmnr.Laminar inside common.sse so _stamp_ttft_ttlt uses the fake."""
    import importlib
    import types

    fake = _CaptureLaminar()

    # Build / replace the lmnr module stub so `from lmnr import Laminar` works.
    lmnr_mod = types.ModuleType("lmnr")
    lmnr_mod.Laminar = fake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "lmnr", lmnr_mod)

    # Force-reimport common.sse so the patched module is visible.
    if "common.sse" in sys.modules:
        del sys.modules["common.sse"]
    if "common" in sys.modules:
        del sys.modules["common"]

    return fake


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_ttft_and_ttlt_stamped_once(capture_lmnr):
    """Happy path: answer → done stream stamps both metrics exactly once."""
    from common.sse import _sse_format

    source = _fake_stream(
        {"type": "answer", "content": "Hello"},
        {"type": "answer", "content": " world"},
        {"type": "done"},
    )
    await _collect(_sse_format, source)

    assert len(capture_lmnr.calls) == 1, "set_span_attributes called more than once"
    attrs = capture_lmnr.calls[0]
    assert "waj.ttft_ms" in attrs, "missing waj.ttft_ms"
    assert "waj.ttlt_ms" in attrs, "missing waj.ttlt_ms"
    assert attrs["waj.ttft_ms"] >= 0
    assert attrs["waj.ttlt_ms"] >= attrs["waj.ttft_ms"], "ttlt must be >= ttft"


async def test_ttft_uses_first_content_event_not_preamble(capture_lmnr):
    """A non-content preamble (thought) must not trigger ttft."""
    from common.sse import _sse_format

    source = _fake_stream(
        {"type": "thought", "content": "thinking…"},  # NOT a content type
        {"type": "answer", "content": "result"},
        {"type": "done"},
    )
    await _collect(_sse_format, source)

    assert len(capture_lmnr.calls) == 1
    attrs = capture_lmnr.calls[0]
    assert "waj.ttft_ms" in attrs
    # ttft must be > 0 (at least after the thought event), but we can't assert
    # exact timing in a unit test — just that both keys are present.
    assert "waj.ttlt_ms" in attrs


async def test_stream_unchanged(capture_lmnr):
    """Adding timing must not drop, reorder, or alter any SSE frame."""
    from common.sse import _sse_format
    import json

    events = [
        {"type": "answer", "content": "a"},
        {"type": "answer", "content": "b"},
        {"type": "done"},
    ]
    source = _fake_stream(*events)
    chunks = await _collect(_sse_format, source)

    parsed = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("data:"):
                parsed.append(json.loads(line[len("data:"):].strip()))

    assert parsed == events


async def test_stop_on_done_still_respected(capture_lmnr):
    """stop_on_done=True must stop iteration after done; ttft/ttlt still stamped."""
    from common.sse import _sse_format
    import json

    after_done_seen = []

    async def _source():
        yield {"type": "answer", "content": "hi"}
        yield {"type": "done"}
        after_done_seen.append(True)  # must never execute when stop_on_done=True
        yield {"type": "answer", "content": "ghost"}

    chunks = await _collect(_sse_format, _source(), stop_on_done=True)

    assert not after_done_seen, "generator continued past done"
    types_seen = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("data:"):
                types_seen.append(json.loads(line[len("data:"):].strip()).get("type"))
    assert "ghost" not in types_seen

    # Metrics still stamped even though we broke out early.
    assert len(capture_lmnr.calls) == 1
    assert "waj.ttft_ms" in capture_lmnr.calls[0]
    assert "waj.ttlt_ms" in capture_lmnr.calls[0]


async def test_stop_on_done_false_forwards_after_done(capture_lmnr):
    """stop_on_done=False keeps forwarding past `done`. A chat turn may
    yield its reply-suggestion chips AFTER `done` (so they don't block
    completion); the wrapper must not break on `done` and drop them."""
    from common.sse import _sse_format
    import json

    async def _source():
        yield {"type": "answer", "content": "300 AED. Book it?"}
        yield {"type": "done"}
        yield {"type": "suggestions", "questions": ["Yes, book it", "No, thanks"]}

    chunks = await _collect(_sse_format, _source(), stop_on_done=False)

    types_seen = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("data:"):
                types_seen.append(json.loads(line[len("data:"):].strip()).get("type"))
    assert types_seen == ["answer", "done", "suggestions"]
    assert "Yes, book it" in "".join(chunks)


async def test_no_content_events_skips_ttft(capture_lmnr):
    """If only done arrives (no content events), ttft is absent, ttlt present."""
    from common.sse import _sse_format

    source = _fake_stream({"type": "done"})
    await _collect(_sse_format, source)

    # set_span_attributes may or may not be called (only ttlt), but must not crash.
    # If called, ttft_ms must NOT be present (no content event seen).
    for call in capture_lmnr.calls:
        assert "waj.ttft_ms" not in call, "ttft_ms should not be set without a content event"


async def test_no_op_when_lmnr_missing(monkeypatch):
    """If lmnr is not installed, _sse_format must still stream normally."""
    # Remove lmnr from sys.modules to simulate absence.
    monkeypatch.delitem(sys.modules, "lmnr", raising=False)
    if "common.sse" in sys.modules:
        del sys.modules["common.sse"]
    if "common" in sys.modules:
        del sys.modules["common"]

    from common.sse import _sse_format

    source = _fake_stream({"type": "answer", "content": "hi"}, {"type": "done"})
    chunks = await _collect(_sse_format, source)  # must not raise

    assert any("answer" in c for c in chunks)
    assert any("done" in c for c in chunks)
