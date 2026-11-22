"""Laminar (lmnr-ai/lmnr) observability bootstrap.

Single entry point for the FastAPI lifespan to call. Resolves config from
env vars and calls `Laminar.initialize()`, which auto-instruments LangChain,
LangGraph, OpenAI, Anthropic, Google Gemini, and every other OpenLLMetry
provider lmnr bundles.

Pointing at a self-hosted Laminar (docker-compose) requires:
    LMNR_PROJECT_API_KEY  — generated from the local dashboard at :5667
    LMNR_BASE_URL         — e.g. http://localhost (no scheme suffix, no path)
    LMNR_HTTP_PORT        — defaults to 8001 in docker-compose.dev.yml
    LMNR_GRPC_PORT        — defaults to 8002 in docker-compose.dev.yml

Leave LMNR_BASE_URL unset to use the managed Laminar cloud at
https://api.lmnr.ai. Set LMNR_DISABLED=true to skip instrumentation entirely
(useful in unit tests so spans don't get exported to a backend nobody is
listening on).
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_initialized = False


@contextlib.contextmanager
def root_span(
    name: Optional[str],
    attributes: Optional[Dict[str, Any]] = None,
    *,
    redact: Optional[bool] = None,
):
    """Open a Laminar span as the *current* span for the duration of a block.

    This is the request-root span the agent's auto-instrumented LLM/LangChain
    child spans nest under. We need it because ``Laminar.initialize()`` only
    instruments the LLM providers — it does NOT create a per-request FastAPI
    span. Without an explicit root span there is nothing carrying the
    request-level attributes (``waj.agent_kind``, ``waj.channel.*``), so the
    trace's TOP span has no ``agent_kind`` and the Live Monitor / Observability
    views render every trace as "legacy".

    No-op (yields ``None``) when ``name`` is falsy, the SDK is missing, or
    instrumentation is disabled — so callers never have to branch on telemetry
    state and unit tests with ``LMNR_DISABLED=true`` are unaffected.

    ``redact``: the trace carries sensitive content, so every span in it is
    exported with content stripped (``core.sensitive_content``). Defaults to
    True when ``attributes["waj.agent_kind"]`` is a sensitive kind; pass
    ``redact=True`` for sensitive work tagged with another kind.
    """
    if redact is None:
        from core.sensitive_content import is_sensitive_kind

        redact = is_sensitive_kind((attributes or {}).get("waj.agent_kind"))
    if not name:
        yield None
        return
    try:
        from lmnr import Laminar
    except Exception:
        yield None
        return
    try:
        cm = Laminar.start_as_current_span(name=name)
    except Exception:
        yield None
        return
    # Enter/exit the span manually rather than via ``with`` so we can swallow
    # the contextvar detach error on exit. This span wraps an async SSE
    # generator; its OTEL context is attached via a contextvar, and the
    # generator's teardown (on ``done``/break or a client disconnect) can run
    # in a different asyncio context than where the span was entered. lmnr then
    # raises ``Token ... was created in a different Context`` during detach. The
    # span data is already recorded by that point, so the failure is pure
    # bookkeeping — swallow it so it neither spams the logs nor surfaces as an
    # un-retrieved task exception.
    try:
        span = cm.__enter__()
    except Exception:
        yield None
        return
    try:
        if redact:
            # Mark BEFORE any child span starts so the whole trace — this
            # root included — is exported redacted.
            from core.sensitive_content import REDACTED_ATTR, mark_span

            mark_span(span)
            if attributes is not None:
                attributes = {**attributes, REDACTED_ATTR: True}
        if attributes:
            # Laminar's exporter wants flat scalars; drop Nones and stringify
            # anything non-scalar so the payload is never rejected.
            flat: Dict[str, Any] = {}
            for key, value in attributes.items():
                if value is None:
                    continue
                flat[key] = value if isinstance(value, (str, int, float, bool)) else str(value)
            if flat:
                # Span attributes feed ClickHouse (the Live Monitor reads
                # `top.attributes`). The Laminar dashboard HTTP API, however,
                # STRIPS custom span attributes (it only returns `lmnr.span.*`),
                # so the Observability explorer can't see agent_kind from spans.
                # Trace METADATA *is* surfaced by that API, so set both: spans
                # for ClickHouse, metadata for the dashboard/explorer.
                try:
                    Laminar.set_span_attributes(flat)
                except Exception:
                    pass
                try:
                    Laminar.set_trace_metadata(flat)
                except Exception:
                    pass
        yield span
    finally:
        try:
            cm.__exit__(None, None, None)
        except Exception:
            pass


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def initialize_observability() -> bool:
    """Initialize Laminar once. Idempotent — safe to call from lifespan.

    Returns True if instrumentation is active after the call, False if it was
    skipped (disabled, missing api key, or the SDK refused to start).
    """
    global _initialized
    if _initialized:
        return True

    if _truthy(os.getenv("LMNR_DISABLED")):
        logger.info("Laminar instrumentation disabled via LMNR_DISABLED")
        return False

    api_key = os.getenv("LMNR_PROJECT_API_KEY")
    if not api_key:
        logger.info(
            "LMNR_PROJECT_API_KEY unset — skipping Laminar instrumentation. "
            "Start the lmnr-app docker service and grab a project key from "
            "http://localhost:5667 to enable traces."
        )
        return False

    base_url = os.getenv("LMNR_BASE_URL")
    http_port = os.getenv("LMNR_HTTP_PORT")
    grpc_port = os.getenv("LMNR_GRPC_PORT")

    kwargs: dict = {"project_api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    if http_port:
        kwargs["http_port"] = int(http_port)
    if grpc_port:
        kwargs["grpc_port"] = int(grpc_port)

    try:
        from lmnr import Laminar

        Laminar.initialize(**kwargs)
        from core.sensitive_content import install_span_redaction

        install_span_redaction()
        _initialized = True
        logger.info(
            "Laminar instrumentation active (base_url=%s, http=%s, grpc=%s)",
            base_url or "https://api.lmnr.ai",
            http_port or "default",
            grpc_port or "default",
        )
        return True
    except Exception as exc:
        logger.warning("Failed to initialize Laminar — traces disabled: %s", exc)
        return False


def shutdown_observability() -> None:
    """Flush pending spans on FastAPI shutdown. Best-effort."""
    if not _initialized:
        return
    try:
        from lmnr import Laminar

        Laminar.shutdown()
    except Exception as exc:
        logger.debug("Laminar shutdown raised (ignored): %s", exc)


# `dashboard_trace_url` / `dashboard_dataset_url` used to build
# `/project/{project_id}/...` deep-links into the Laminar web UI. They needed a
# project id, which only that UI's session-cookie API could resolve — a route set
# the deployed build no longer serves. No template ever rendered either helper, so
# they were removed with the client rather than reimplemented; the admin portal
# renders traces itself under /admin/observability.
