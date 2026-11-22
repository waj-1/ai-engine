"""Bootstrap Laminar at import time, BEFORE any LangChain import.

Why this exists as a separate module: Laminar's auto-instrumentation
patches LangChain / LangGraph / LLM-provider classes at the moment
`Laminar.initialize()` runs. Classes that were already imported into the
process get monkey-patched in place, but only their methods that exist
*and* are referenced through the class — descriptors bound to instances
of the original class before patching still call the original code.

In practice this means: if `initialize()` runs in FastAPI's lifespan,
which fires after uvicorn has finished evaluating `main.py`'s module
body, every `from langchain... import X` and `from langgraph... import
Y` has already executed and bound *unpatched* references in the import
graph. The patches still apply to fresh imports, but agent code holding
the old references emits no spans.

The fix is to make sure `Laminar.initialize()` is the *first* thing that
touches `lmnr.*`/`langchain.*` in the process. The cleanest way to do
that is to put it in its own module and `import` it at the very top of
`main.py`, before any other agent / framework / FastAPI import. Python's
module cache guarantees it runs exactly once and finishes before the
next import line.

Failure modes (DB unreachable, missing key, bad URL) are logged but
never raise — the agent must still start when telemetry isn't
configured.
"""
from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

# Load .env before reading LMNR_* so a local dev workflow doesn't have to
# `export` the keys. main.py also calls load_dotenv() later — that call
# is idempotent.
load_dotenv()

logger = logging.getLogger(__name__)


def _truthy(value):
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


_initialized = False
_init_error: str = ""
_content_capture: bool = False


# ---------------------------------------------------------------------------
# Content capture (prompts / completions / tool args / tool results).
#
# The OpenLLMetry instrumentors that lmnr bundles (LangChain, LangGraph,
# OpenAI, Anthropic, Gemini, …) gate message *content* behind Traceloop's
# `TRACELOOP_TRACE_CONTENT` switch. It defaults to "true" in the upstream
# instrumentors, BUT:
#
#   * several deployments disable it for privacy / span-size reasons, and
#   * once disabled in a parent process / shared base image it sticks,
#
# which is exactly the "prompts/answers/tool I/O aren't traced" symptom the
# operator reported. The robust fix is to force the flag ON at import time,
# BEFORE `Laminar.initialize()` activates the instrumentors — the
# instrumentors read the env var at instrumentation time, so setting it
# afterwards is a no-op.
#
# We set BOTH the Traceloop name (`TRACELOOP_TRACE_CONTENT`, honoured by the
# OpenLLMetry instrumentors lmnr re-exports) and the lmnr-native alias
# (`LMNR_TRACE_CONTENT`, accepted by newer lmnr SDKs) so we're covered across
# SDK versions. This is a GLOBAL flag — no per-call span attribute plumbing is
# needed once it's on. An explicit `LMNR_TRACE_CONTENT=false` /
# `TRACELOOP_TRACE_CONTENT=false` from the operator still wins (we only force
# it when the operator hasn't explicitly opted out).
# ---------------------------------------------------------------------------
def _enable_content_capture() -> bool:
    """Force prompt/completion/tool-I/O capture ON unless explicitly disabled.

    Returns True when content capture is effectively ON after this call.
    Must run before `Laminar.initialize()` so the instrumentors pick it up.
    """
    explicit = os.getenv("TRACELOOP_TRACE_CONTENT")
    if explicit is None:
        explicit = os.getenv("LMNR_TRACE_CONTENT")

    if explicit is not None and not _truthy(explicit):
        # Operator explicitly opted out — respect it.
        os.environ["TRACELOOP_TRACE_CONTENT"] = "false"
        os.environ["LMNR_TRACE_CONTENT"] = "false"
        return False

    # Default + explicit-true path: force both names ON.
    os.environ["TRACELOOP_TRACE_CONTENT"] = "true"
    os.environ["LMNR_TRACE_CONTENT"] = "true"
    return True


def is_initialized() -> bool:
    """Public read of bootstrap state. The /admin/lmnr/diagnose endpoint
    uses this so the operator can confirm import-time setup ran before
    any LangChain class was imported."""
    return _initialized


def last_error() -> str:
    """Last bootstrap error (empty string when init succeeded or the key
    was simply unset). Surfaced by the diagnose endpoint."""
    return _init_error


def content_capture_enabled() -> bool:
    """Whether prompt/completion/tool-I/O content capture is ON.

    Surfaced by /admin/lmnr/diagnose so the operator can confirm at a glance
    that traces will carry message content (and not just token counts)."""
    return _content_capture


def _patch_contextvar_detach() -> None:
    """Make lmnr's context detach tolerant of cross-context async-gen teardown.

    lmnr auto-instruments async generators (the LLM stream wrappers). When such
    a generator is abandoned — e.g. the SSE framing breaks on the ``done`` event
    or the HTTP client disconnects — lmnr's ``_cleanup_span`` runs in a different
    asyncio context than where the span was entered and calls
    ``ContextVar.reset(token)``, raising ``ValueError: Token was created in a
    different Context``. That surfaces as a noisy "Task exception was never
    retrieved". The span data is already recorded, so the detach is pure
    bookkeeping — wrap it to swallow that one error. No-op on lmnr version drift.
    """
    try:
        from lmnr.opentelemetry_lib.tracing import context as _ctx

        cls = _ctx.IsolatedContextVarsRuntimeContext
        if getattr(cls, "_waj_detach_patched", False):
            return
        _orig_detach = cls.detach

        def _safe_detach(self, token):
            try:
                _orig_detach(self, token)
            except ValueError:
                pass  # token from a different async context (async-gen teardown)

        cls.detach = _safe_detach
        cls._waj_detach_patched = True
        logger.info("Patched lmnr context detach to tolerate async-gen teardown")
    except Exception as exc:  # pragma: no cover - defensive across lmnr versions
        logger.debug("Could not patch lmnr context detach: %s", exc)


def _initialize() -> bool:
    global _initialized, _init_error, _content_capture
    if _initialized:
        return True

    if _truthy(os.getenv("LMNR_DISABLED")):
        _init_error = "disabled via LMNR_DISABLED env var"
        logger.info("Laminar instrumentation disabled via LMNR_DISABLED")
        return False

    # Flip on content capture BEFORE the instrumentors are activated by
    # Laminar.initialize() below — they read TRACELOOP_TRACE_CONTENT at
    # instrumentation time, so this must precede the import + initialize call.
    _content_capture = _enable_content_capture()

    api_key = os.getenv("LMNR_PROJECT_API_KEY")
    if not api_key:
        _init_error = "LMNR_PROJECT_API_KEY env var is unset"
        logger.info(
            "LMNR_PROJECT_API_KEY unset — Laminar bootstrap skipped. "
            "Set the key in .env to export spans; without them the "
            "/admin observability screens have nothing to read."
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
        # The import itself triggers OpenLLMetry's auto-instrumentor
        # registration. Calling .initialize() then activates each
        # instrumentor against whatever provider SDKs are already on
        # sys.path. This must complete before any `from langchain...`
        # in the agent code so the monkey-patches apply to the freshly-
        # imported classes.
        from lmnr import Laminar

        Laminar.initialize(**kwargs)
        _patch_contextvar_detach()
        # Content capture stays on for every agent, but the traces of
        # sensitive agent kinds are exported with inputs/outputs stripped —
        # see core.sensitive_content.
        from core.sensitive_content import install_span_redaction

        if not install_span_redaction():
            logger.warning(
                "sensitive-content span redaction NOT installed — sensitive "
                "traces would carry content; check lmnr TracerWrapper internals"
            )
        _initialized = True
        logger.info(
            "Laminar instrumentation active at import time "
            "(base_url=%s, http=%s, grpc=%s, content_capture=%s)",
            base_url or "https://api.lmnr.ai",
            http_port or "default",
            grpc_port or "default",
            _content_capture,
        )
        return True
    except Exception as exc:
        _init_error = f"{type(exc).__name__}: {exc}"
        logger.warning("Laminar bootstrap failed — traces disabled: %s", exc)
        return False


# Run on import.
_initialize()
