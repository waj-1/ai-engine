"""Telemetry redaction for sensitive agent kinds (core.sensitive_content).

Content capture stays ON globally; spans of a sensitive trace must reach the
exporter with prompts / completions / tool I/O stripped but the skeleton
(name, timings, token usage, model, waj.* tags) intact. Other traces must be
untouched. The sensitive kind here ("records") is test data: which kinds are
sensitive is a plugin's declaration, not the engine's.
"""
import sys
import types

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from core import sensitive_content


CONTENT_ATTRS = {
    "gen_ai.prompt.0.role": "user",
    "gen_ai.prompt.0.content": "Account holder Jane Doe, card ending 4242",
    "gen_ai.completion.0.content": "Your balance is 120",
    "gen_ai.input.messages": "[...]",
    "lmnr.span.input": '{"q": "jane doe balance"}',
    "lmnr.span.output": '{"record": "Account ..."}',
    "traceloop.entity.input": "{...}",
    "traceloop.entity.output": "{...}",
    "gen_ai.tool.call.arguments": '{"holder": "Jane"}',
}
SKELETON_ATTRS = {
    "gen_ai.system": "gemini",
    "gen_ai.request.model": "gemini-3.5-flash",
    "gen_ai.usage.input_tokens": 1200,
    "gen_ai.usage.output_tokens": 80,
    "llm.usage.total_tokens": 1280,
    "lmnr.span.path": ("records.agent", "ChatGoogleGenerativeAI.chat"),
    "waj.agent_kind": "records",
    "lmnr.association.properties.session_id": "s-1",
}


@pytest.fixture
def provider():
    """An SDK provider whose ONLY processor is a wrapped exporting processor —
    the same position LaminarSpanProcessor occupies in production."""
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    sensitive_content._wrap_processor(processor)
    tp = TracerProvider()
    tp.add_span_processor(processor)
    yield tp, exporter
    tp.shutdown()


def test_redact_attributes_allowlist():
    out = sensitive_content.redact_attributes({**CONTENT_ATTRS, **SKELETON_ATTRS})
    for key in CONTENT_ATTRS:
        assert key not in out, key
    for key in SKELETON_ATTRS:
        assert key in out, key
    assert out[sensitive_content.REDACTED_ATTR] is True


def test_marked_trace_spans_are_exported_redacted(provider):
    tp, exporter = provider
    tracer = tp.get_tracer("t")
    with tracer.start_as_current_span("records.agent") as root:
        sensitive_content.mark_span(root)
        with tracer.start_as_current_span("llm") as child:
            for k, v in {**CONTENT_ATTRS, **SKELETON_ATTRS}.items():
                child.set_attribute(k, v)
            child.add_event("gen_ai.content.prompt", {"gen_ai.prompt": "Jane Doe"})
            try:
                raise ValueError("store said: Jane Doe not found")
            except ValueError as exc:
                child.record_exception(exc)

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert set(spans) == {"records.agent", "llm"}
    llm = spans["llm"]
    for key in CONTENT_ATTRS:
        assert key not in llm.attributes, key
    assert llm.attributes["gen_ai.usage.input_tokens"] == 1200
    assert llm.attributes["gen_ai.request.model"] == "gemini-3.5-flash"
    assert llm.attributes[sensitive_content.REDACTED_ATTR] is True
    # Events keep their names (timeline) but no payload — incl. exception text.
    assert [e.name for e in llm.events] == ["gen_ai.content.prompt", "exception"]
    assert all(not e.attributes for e in llm.events)
    # Skeleton preserved: same ids, parentage and timings.
    assert llm.parent.span_id == spans["records.agent"].context.span_id
    assert llm.end_time >= llm.start_time > 0


def test_unmarked_trace_is_untouched(provider):
    tp, exporter = provider
    tracer = tp.get_tracer("t")
    with tracer.start_as_current_span("support.agent"):
        with tracer.start_as_current_span("llm") as child:
            for k, v in CONTENT_ATTRS.items():
                child.set_attribute(k, v)
    llm = {s.name: s for s in exporter.get_finished_spans()}["llm"]
    for key, value in CONTENT_ATTRS.items():
        assert llm.attributes[key] == value
    assert sensitive_content.REDACTED_ATTR not in llm.attributes


def test_redacted_scope_marks_spans_started_inside_it(provider):
    tp, exporter = provider
    tracer = tp.get_tracer("t")
    with sensitive_content.redacted_scope():
        # No root span at all (e.g. a provider call outside any root_span).
        with tracer.start_as_current_span("realtime.connect") as span:
            span.set_attribute("gen_ai.prompt.0.content", "Jane Doe")
    assert not sensitive_content.in_redacted_scope()
    span = exporter.get_finished_spans()[0]
    assert "gen_ai.prompt.0.content" not in span.attributes


def test_redaction_failure_drops_the_span(provider, monkeypatch):
    tp, exporter = provider
    tracer = tp.get_tracer("t")

    def _boom(span):
        raise RuntimeError("redaction bug")

    monkeypatch.setattr(sensitive_content, "redact_span", _boom)
    with tracer.start_as_current_span("records.agent") as root:
        sensitive_content.mark_span(root)
        root.set_attribute("gen_ai.prompt.0.content", "Jane Doe")
    assert exporter.get_finished_spans() == ()


def test_root_span_marks_sensitive_kinds_and_explicit_redact(monkeypatch):
    """core.observability.root_span marks the trace for a sensitive
    agent_kind (and for redact=True), not for other kinds."""
    from core import observability

    monkeypatch.setattr(sensitive_content, "sensitive_agent_kinds",
                        lambda: frozenset({"records", "records_review"}))

    tp = TracerProvider()
    tracer = tp.get_tracer("t")
    seen = {}

    class _Laminar:
        @staticmethod
        def start_as_current_span(name):
            return tracer.start_as_current_span(name)

        @staticmethod
        def set_span_attributes(attrs):
            seen.setdefault("attrs", []).append(dict(attrs))

        @staticmethod
        def set_trace_metadata(attrs):
            pass

    monkeypatch.setitem(sys.modules, "lmnr", types.SimpleNamespace(Laminar=_Laminar))

    def _trace_of(span):
        return span.get_span_context().trace_id

    with observability.root_span("records.agent", {"waj.agent_kind": "records"}) as span:
        assert sensitive_content.is_redacted_trace(_trace_of(span))
    assert seen["attrs"][-1][sensitive_content.REDACTED_ATTR] is True
    with observability.root_span("records_review.agent", {"waj.agent_kind": "records_review"}) as span:
        assert sensitive_content.is_redacted_trace(_trace_of(span))
    with observability.root_span("records.transcribe", {"waj.agent_kind": "voice"}, redact=True) as span:
        assert sensitive_content.is_redacted_trace(_trace_of(span))
    with observability.root_span("support.agent", {"waj.agent_kind": "support"}) as span:
        assert not sensitive_content.is_redacted_trace(_trace_of(span))


def test_install_span_redaction_wraps_laminar_processor_when_initialised():
    """When Laminar is up (as in production), its processor carries the wrap."""
    try:
        from lmnr.opentelemetry_lib.tracing import TracerWrapper
    except Exception:  # pragma: no cover
        pytest.skip("lmnr not importable")
    instance = getattr(TracerWrapper, "instance", None)
    if instance is None:
        assert sensitive_content.install_span_redaction() is False
        return
    assert sensitive_content.install_span_redaction() is True
    assert getattr(instance._span_processor, "_waj_redaction_wrapped", False)


def test_no_plugin_means_no_sensitive_kinds(monkeypatch):
    """The engine names no sensitive kind itself: with no plugin, none."""
    import engine_plugins

    monkeypatch.setattr(engine_plugins, "PLUGINS", ())
    assert sensitive_content.sensitive_agent_kinds() == frozenset()
    assert not sensitive_content.is_sensitive_kind("anything")
    assert not sensitive_content.is_sensitive_kind(None)


def test_plugin_hook_declares_sensitive_kinds(monkeypatch):
    """A plugin's `sensitive_content.sensitive_agent_kinds()` is what counts,
    normalized to lower case."""
    import types

    hook = types.SimpleNamespace(sensitive_agent_kinds=lambda: ("Records", " ledger "))
    import core.plugins as plugins

    monkeypatch.setattr(plugins, "load_hook", lambda name: [hook] if name == "sensitive_content" else [])
    assert sensitive_content.sensitive_agent_kinds() == frozenset({"records", "ledger"})
    assert sensitive_content.is_sensitive_kind("RECORDS")
    assert not sensitive_content.is_sensitive_kind("business")
