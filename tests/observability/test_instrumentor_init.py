"""Regression guard: every installed lmnr auto-instrumentor must init cleanly.

Incident context (2026-07-21): this service self-hosts Laminar (`lmnr`) for
LLM tracing. `Laminar.initialize()` loops over ~42 OpenTelemetry
auto-instrumentors and calls each one's `init_instrumentor()`. Critically,
lmnr's own init loop CATCHES any exception raised there, logs a single
"Error initializing instrumentor: ..." line, and moves on — a broken
instrumentor never crashes the process and never raises anywhere the app
would notice.

That silent-catch is exactly what hid a real outage: `lmnr[all]==0.7.42`
resolved (via its own floating sub-dependencies, since requirements.txt does
not pin them directly) an `opentelemetry-instrumentation-langchain` build
that imports `opentelemetry._events`, a module removed from a contemporary
`opentelemetry-api` release. Both the `langchain` and `openai` instrumentors
failed to construct, so every langchain-level span (per-tool spans, chain
spans) silently disappeared from production telemetry. Nobody noticed for
days, because the only symptom was one ERROR log line per boot, easily lost
in normal startup noise.

This test calls the exact same per-instrumentor init path lmnr's bootstrap
uses and asserts none of it raises — so a future transitive-dependency drift
that reintroduces this class of failure fails CI loudly instead of quietly
deleting telemetry. It does not call `Laminar.initialize()` itself (which
also spins up exporters and needs a project API key); `init_instrumentor`
only imports the instrumentor module and constructs the class — no network
calls, no API keys, no live LLM calls — so it is safe to run offline in CI.

The `INSTRUMENTATION_INITIALIZERS` registry is a private-ish lmnr API (not
part of its public docs), so if a future lmnr release restructures it, the
import below will fail; in that case we skip with a clear message rather
than fail confusingly on an AttributeError/ImportError unrelated to an
actual broken instrumentor. Genuine instrumentor init failures are never
skipped.
"""
from __future__ import annotations

import pytest

try:
    from lmnr.opentelemetry_lib.tracing.instruments import (
        INSTRUMENTATION_INITIALIZERS as _REGISTRY,
    )
except ImportError as exc:  # lmnr restructured its internals
    _REGISTRY = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


def _registry_or_skip():
    """Return the initializer registry, or skip if lmnr moved/removed it.

    This is the ONLY place we skip. An instrumentor that is present in the
    registry but raises during init is always a hard failure below, never
    a skip.
    """
    if _IMPORT_ERROR is not None:
        pytest.skip(
            "lmnr.opentelemetry_lib.tracing.instruments.INSTRUMENTATION_INITIALIZERS "
            f"is unavailable (lmnr internals likely restructured): "
            f"{type(_IMPORT_ERROR).__name__}: {_IMPORT_ERROR}"
        )
    return _REGISTRY


def _instrumentor_name(instrument_key) -> str:
    """`Instruments` enum members expose `.value`; tolerate a bare string too."""
    return getattr(instrument_key, "value", instrument_key)


def test_every_installed_instrumentor_initializes_without_raising():
    """Broad sweep: no instrumentor for an installed package may raise on init.

    Instrumentors for packages that are NOT installed return None from
    `init_instrumentor` (see `is_package_installed` guards in lmnr's own
    initializer classes) — that is expected and not a failure. We only flag
    instrumentors that actually raise, since a raise is what lmnr's own
    bootstrap loop would silently swallow in production.
    """
    registry = _registry_or_skip()

    failures: list[tuple[str, Exception]] = []
    for instrument_key, initializer in registry.items():
        name = _instrumentor_name(instrument_key)
        try:
            initializer.init_instrumentor(False)
        except Exception as exc:  # noqa: BLE001 - deliberately broad: this is the exact
            # exception shape lmnr's own bootstrap loop catches-and-logs. Any
            # exception here is a silent-telemetry-loss bug, regardless of type.
            failures.append((name, exc))

    if failures:
        diagnostic = "\n".join(
            f"  - {name} -> {type(exc).__name__}: {exc}" for name, exc in failures
        )
        pytest.fail(
            "One or more lmnr auto-instrumentors raised during "
            "init_instrumentor(). lmnr's own bootstrap loop swallows this "
            "exception and only logs it — meaning these instrumentors would "
            "silently stop emitting spans in production (see the 2026-07-21 "
            "langchain/openai instrumentor incident). Failing instrumentors:\n"
            f"{diagnostic}"
        )


@pytest.mark.parametrize("name", ["langchain", "google_genai", "anthropic"])
def test_critical_llm_instrumentor_initializes_successfully(name):
    """Pin the exact instrumentors that carry this fleet's LLM/tool spans.

    `langchain` is the instrumentor that actually regressed on 2026-07-21
    (per-tool and chain spans vanished). `google_genai` and `anthropic` are
    asserted alongside it because they carry the LLM-call spans the rest of
    the fleet's telemetry depends on — a silent regression in either would
    be just as invisible as the langchain one was.

    Unlike the broad sweep above, this test asserts the return value is
    non-None (not just "didn't raise"): all three packages
    (langchain / google-genai / anthropic) are hard, unconditional
    dependencies of this service per requirements.txt, so a None return here
    always means the instrumentor failed to recognize an installed package
    — itself a regression — never a legitimately-absent optional package.
    """
    registry = _registry_or_skip()

    match = None
    for instrument_key, initializer in registry.items():
        if _instrumentor_name(instrument_key) == name:
            match = initializer
            break

    if match is None:
        pytest.skip(
            f"no '{name}' entry in INSTRUMENTATION_INITIALIZERS — lmnr likely "
            "renamed/restructured this instrumentor's registry key"
        )

    try:
        result = match.init_instrumentor(False)
    except Exception as exc:  # noqa: BLE001 - see broad sweep above for rationale
        pytest.fail(
            f"'{name}' instrumentor raised during init_instrumentor() instead "
            f"of initializing: {type(exc).__name__}: {exc}\n"
            "This is the exact silent-failure shape behind the 2026-07-21 "
            "incident: lmnr's own bootstrap loop would catch this, log one "
            "ERROR line, and continue — deleting this instrumentor's spans "
            "from production telemetry without anyone noticing."
        )

    assert result is not None, (
        f"'{name}' instrumentor initializer returned None even though "
        f"'{name}' is an unconditional dependency in requirements.txt — "
        "either the package isn't actually installed in this environment "
        "or lmnr's own is_package_installed() detection regressed."
    )
