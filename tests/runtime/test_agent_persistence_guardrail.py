"""Fence: once an agent is migrated onto common.chat_session.run_turn it must
NOT persist chat itself, emit its own `done`, OR write its own telemetry row —
the orchestrator owns all three (chat_persistence, the terminal `done`, and the
agent_traces telemetry via record_turn_telemetry). An agent that keeps calling
`record_trace` after migration double-inserts into agent_traces, silently
double-counting the KPIs the dashboards read. Newly-added agents are guarded by
default (not on the allowlist). Remove an agent from the allowlist in the same
PR that migrates it — stripping chat_persistence, `done`, AND record_trace."""
import re
from pathlib import Path

from core.plugins import optional_import

AGENTS_DIR = Path(__file__).resolve().parents[2] / "runtime" / "agents"

# Agents not yet fully on the run_turn orchestrator. SHRINK this as each
# migration PR lands; the goal is only genuinely-exempt agents remain. The
# engine's own agents are all migrated; a product build's exempt agents are
# listed in tests/product_fixtures.py (shipped with the product only).
_product = optional_import("tests.product_fixtures")
ALLOWLIST = set(getattr(_product, "PERSISTENCE_GUARDRAIL_ALLOWLIST", ()) if _product else ())


def _agent_files():
    return [p for p in AGENTS_DIR.glob("*.py") if p.name != "__init__.py"]


def test_migrated_agents_do_not_touch_persistence_or_emit_done():
    offenders = []
    for p in _agent_files():
        if p.name in ALLOWLIST:
            continue
        src = p.read_text()
        if "chat_persistence" in src:
            offenders.append(f"{p.name}: imports chat_persistence")
        if re.search(r'["\']type["\']\s*:\s*["\']done["\']', src):
            offenders.append(f"{p.name}: emits its own done event")
        if "record_trace" in src or "trace_recorder" in src:
            offenders.append(f"{p.name}: writes its own agent_traces row "
                             "(record_trace) — run_turn owns telemetry")
    assert not offenders, "Migrated agents must use run_turn:\n" + "\n".join(offenders)


def test_allowlist_only_names_real_agents():
    names = {p.name for p in _agent_files()}
    stale = ALLOWLIST - names
    assert not stale, f"Allowlist names non-existent agents: {stale}"
