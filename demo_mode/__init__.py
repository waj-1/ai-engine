"""Demo mode: run the engine as a public, one-click demo.

Everything here is OFF unless ``DEMO_MODE`` is set to a true value, and every
setting is read at call time, so a normal install (and the test suite) is
unchanged. ``docs/deploy/demo.md`` describes what it does and every setting.

    settings     every DEMO_* setting, with its default
    policy       which routes a visitor may not reach, and the refusal
    gate         the ASGI middleware that applies the policy and the caps
    limits       per-visitor rate limits and the global daily turn cap
    voice        voice calls: the switch, the caps and the seeded voice setup
    account      the demo admin user and the "Try the demo" sign-in
    northwind    the fictional Northwind tool server, served in-process
    content      the demo's agents, documents, tests and conversations
    seed         builds the Northwind demo (idempotent)
    reset        wipes what visitors made and seeds again (nightly)
    sample_traffic  the demo's own conversations around the clock, capped
    insights     "What happened in conversations", sample conversations only, capped
    prices       list prices for the demo's model rows
"""
from __future__ import annotations

from demo_mode.settings import enabled  # noqa: F401
