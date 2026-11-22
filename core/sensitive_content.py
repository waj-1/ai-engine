"""Sensitive agent kinds: content kept out of exported telemetry.

Some agents handle data that must never leave the process in a trace —
health records, dictated audio, photographed documents. A product plugin
names those agent kinds through the ``sensitive_content`` hook (see
``core/plugins.py``); a build with no plugin has none. This module is the
one place that knows

* which agent kinds are sensitive (:func:`sensitive_agent_kinds`), and
* how to keep their content out of Laminar while still exporting the trace
  skeleton (span names, timings, token usage, model ids, ``waj.*`` tags).

What else the engine does for a sensitive kind: its duplex voice calls are
never recorded (``endpoints/voice_live.py``) and its real conversations are
never sampled into live-chat evals (``admin/eval_runs_handlers.py``).

Telemetry redaction
-------------------
Content capture stays ON globally (``core._laminar_bootstrap``) because the
engine portal's Conversations / Observability screens render prompts and
tool I/O for every other agent. For sensitive traffic we redact at the span
processor instead:

1. A sensitive request opens its root span through
   ``core.observability.root_span``; when the span's ``waj.agent_kind`` is a
   sensitive kind (or the caller passes ``redact=True``) the root's trace id
   is registered here (:func:`mark_span`).
2. Code that runs outside a root span can also enter :func:`redacted_scope`;
   every span *started* inside it marks its trace.
3. :func:`install_span_redaction` wraps Laminar's span processor so every
   span of a marked trace is exported as a copy carrying only allow-listed
   attributes (:func:`redact_span`). Prompts, completions, tool arguments and
   results, ``lmnr.span.input/output``, event payloads and exception text
   never leave the process.

The redacted spans carry ``waj.phi_redacted=true`` (:data:`REDACTED_ATTR`;
the name predates this module and is kept so existing traces and filters
still match) so the portal can tell a deliberately empty span from a capture
failure.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

REDACTED_ATTR = "waj.phi_redacted"


def sensitive_agent_kinds() -> frozenset:
    """Every agent kind a plugin declares sensitive (lower-case).

    Read at call time from each plugin's ``sensitive_content`` hook
    (``sensitive_agent_kinds()``), so a test can swap the plugin list.
    """
    from core.plugins import call_hook, load_hook

    kinds: set = set()
    for declared in call_hook(load_hook("sensitive_content"), "sensitive_agent_kinds"):
        kinds.update((k or "").strip().lower() for k in (declared or ()))
    kinds.discard("")
    return frozenset(kinds)


def is_sensitive_kind(agent_kind: Optional[str]) -> bool:
    kind = (agent_kind or "").strip().lower()
    return bool(kind) and kind in sensitive_agent_kinds()


# ---------------------------------------------------------------------------
# Redacted-trace registry
# ---------------------------------------------------------------------------

# trace_id (int) -> monotonic time it was marked. Bounded + TTL'd: a voice
# call can keep one trace alive for its whole duration (max 15 min by
# default), and SSE streams end long after the handler returned, so entries
# are never removed on root-span end — they age out instead.
_TRACE_TTL_SEC = 6 * 3600
_MAX_TRACES = 50_000
_redacted_traces: "OrderedDict[int, float]" = OrderedDict()
_lock = threading.Lock()

_redacted_scope_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "waj_redacted_scope", default=False
)


def _prune_locked(now: float) -> None:
    while _redacted_traces:
        trace_id, marked_at = next(iter(_redacted_traces.items()))
        if len(_redacted_traces) > _MAX_TRACES or now - marked_at > _TRACE_TTL_SEC:
            _redacted_traces.popitem(last=False)
        else:
            break


def mark_trace(trace_id: Optional[int]) -> None:
    """Register ``trace_id`` as one to export redacted. No-op for 0/None."""
    if not trace_id:
        return
    now = time.monotonic()
    with _lock:
        _redacted_traces[trace_id] = now
        _redacted_traces.move_to_end(trace_id)
        _prune_locked(now)


def is_redacted_trace(trace_id: Optional[int]) -> bool:
    if not trace_id:
        return False
    with _lock:
        marked_at = _redacted_traces.get(trace_id)
    return marked_at is not None and time.monotonic() - marked_at <= _TRACE_TTL_SEC


def _trace_id_of(span: Any) -> Optional[int]:
    try:
        ctx = span.get_span_context()
    except Exception:
        ctx = getattr(span, "context", None)
    return getattr(ctx, "trace_id", None) if ctx is not None else None


def mark_span(span: Any) -> None:
    """Mark the trace ``span`` belongs to (best-effort)."""
    try:
        mark_trace(_trace_id_of(span))
    except Exception:  # pragma: no cover - defensive
        logger.debug("sensitive_content: could not mark span trace", exc_info=True)


def mark_current_trace() -> None:
    """Mark the trace of the currently active OTel span, if any."""
    try:
        from opentelemetry import trace as otel_trace

        mark_span(otel_trace.get_current_span())
    except Exception:  # pragma: no cover - OTel missing
        pass


@contextlib.contextmanager
def redacted_scope() -> Iterator[None]:
    """Every span started inside this block (and in tasks/threads spawned
    from it, which copy the context) belongs to a redacted trace."""
    token = _redacted_scope_var.set(True)
    try:
        mark_current_trace()
        yield
    finally:
        _redacted_scope_var.reset(token)


def enter_redacted_scope() -> None:
    """Non-context-manager form of :func:`redacted_scope` for a handler that owns
    its task's context for its whole life (the duplex voice WS handler)."""
    _redacted_scope_var.set(True)
    mark_current_trace()


def in_redacted_scope() -> bool:
    return _redacted_scope_var.get()


# ---------------------------------------------------------------------------
# Span redaction
# ---------------------------------------------------------------------------

# Attribute keys kept on a redacted span. Everything else is dropped — an
# allow-list, because instrumentors keep inventing new content keys
# (gen_ai.prompt.N.content, gen_ai.input.messages, traceloop.entity.input,
# lmnr.span.input, tool call arguments, …) and a deny-list would leak the
# next one.
_KEEP_PREFIXES = (
    "waj.",                          # our own request tags (no content)
    "lmnr.association.properties.",  # session/user/trace metadata (waj.* tags)
    "lmnr.internal.",
    "gen_ai.usage.",
    "llm.usage.",
)
_KEEP_KEYS = frozenset({
    "lmnr.span.path",
    "lmnr.span.ids_path",
    "lmnr.span.parent_path",
    "lmnr.span.parent_ids_path",
    "lmnr.span.type",
    "lmnr.span.instrumentation_source",
    "lmnr.span.sdk_version",
    "lmnr.span.language_version",
    "lmnr.span.original_type",
    "gen_ai.system",
    "gen_ai.operation.name",
    "gen_ai.request.model",
    "gen_ai.response.model",
    "gen_ai.response.id",
    "gen_ai.response.finish_reasons",
    "gen_ai.request.temperature",
    "gen_ai.request.max_tokens",
    "gen_ai.request.top_p",
    "llm.request.type",
    "llm.is_streaming",
    "llm.response.finish_reason",
    "ai.model.id",
    "traceloop.span.kind",
    "traceloop.entity.name",
    "traceloop.entity.path",
    "traceloop.workflow.name",
})
# Content-bearing keys that would otherwise match a keep-prefix.
_DROP_KEYS = frozenset({
    "lmnr.association.properties.metadata.input",
    "lmnr.association.properties.metadata.output",
})


def _keep_attr(key: str) -> bool:
    if key in _DROP_KEYS:
        return False
    return key in _KEEP_KEYS or key.startswith(_KEEP_PREFIXES)


def redact_attributes(attributes: Any) -> dict:
    out = {k: v for k, v in dict(attributes or {}).items() if _keep_attr(k)}
    out[REDACTED_ATTR] = True
    return out


def redact_span(span: Any) -> Any:
    """A copy of the ended (readable) ``span`` with content stripped.

    Keeps the skeleton (name, ids, parent, kind, timings, status code, links,
    resource, scope) plus :func:`_keep_attr` attributes; events keep their
    name and timestamp only; the status description (exception text) is
    dropped. Returns ``span`` unchanged if the copy cannot be built — but
    callers must then not export it (see :func:`install_span_redaction`).
    """
    from opentelemetry.sdk.trace import Event, ReadableSpan
    from opentelemetry.trace.status import Status

    status = getattr(span, "status", None)
    redacted_status = Status(status.status_code) if status is not None else Status()
    events = tuple(
        Event(name=e.name, attributes={}, timestamp=e.timestamp)
        for e in (getattr(span, "events", None) or ())
    )
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=redact_attributes(span.attributes),
        events=events,
        links=tuple(getattr(span, "links", None) or ()),
        kind=span.kind,
        status=redacted_status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=getattr(span, "instrumentation_scope", None),
    )


def _wrap_processor(processor: Any) -> bool:
    """Patch ``processor`` (instance-level) so marked traces export redacted."""
    if getattr(processor, "_waj_redaction_wrapped", False):
        return True
    orig_on_start = processor.on_start
    orig_on_end = processor.on_end

    def on_start(span, parent_context=None):
        if in_redacted_scope():
            mark_span(span)
        return orig_on_start(span, parent_context)

    def on_end(span):
        if is_redacted_trace(_trace_id_of(span)):
            try:
                span = redact_span(span)
            except Exception:
                # Never export an unredacted sensitive span: drop it instead.
                logger.warning("sensitive_content: span redaction failed; span dropped",
                               exc_info=True)
                return None
        return orig_on_end(span)

    processor.on_start = on_start
    processor.on_end = on_end
    processor._waj_redaction_wrapped = True
    return True


def install_span_redaction() -> bool:
    """Wrap Laminar's span processor. Idempotent; False when Laminar is not
    initialised (nothing is exported then, so nothing to redact)."""
    try:
        from lmnr.opentelemetry_lib.tracing import TracerWrapper
    except Exception:
        return False
    instance = getattr(TracerWrapper, "instance", None)
    processor = getattr(instance, "_span_processor", None) if instance else None
    if processor is None:
        return False
    try:
        return _wrap_processor(processor)
    except Exception:  # pragma: no cover - lmnr internals drift
        logger.warning("sensitive_content: could not install span redaction", exc_info=True)
        return False
