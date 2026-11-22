"""Boot-seeder isolation (main._run_boot_seeders).

Until 2026-08-26 all 14 lifespan seeders shared ONE try/except: any exception
in an early seeder silently skipped every later one with a single generic
warning — which is how `seed_voice_scenarios` (11th in the list) never ran
and the portal showed "no voice scenarios" with nothing pointing at the
seeder that actually threw. These tests pin the fix: each seeder runs in its
OWN try/except, a failure is logged WITH the seeder's name, and the loop
continues to the next seeder.

The failing-seeder tests monkeypatch each real seeder module's `run` (the
exact attribute `_run_boot_seeders` resolves via importlib), so they exercise
the real import + dispatch path — a regression back to the shared-try shape
makes them fail with the original symptom (later seeders never called).
"""
from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path
from tests.product import requires_product

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

import main as main_mod


def _seeder_modules():
    return {path: importlib.import_module(path) for path in main_mod._BOOT_SEEDERS}


@requires_product
def test_boot_seeder_registry_shape():
    """Every registered seeder module exists and exposes a callable run() —
    a typo in _BOOT_SEEDERS would otherwise only surface as a boot-time
    warning."""
    mods = _seeder_modules()
    assert len(main_mod._BOOT_SEEDERS) == 19
    for path, mod in mods.items():
        assert callable(getattr(mod, "run", None)), f"{path} has no run()"
    # The incident seeder is registered, after the ones that used to shadow it.
    assert "admin.seed_voice_scenarios" in main_mod._BOOT_SEEDERS
    # Dependency order still holds: profiles after prompts/tools/sources,
    # eval suites after profiles + models.
    order = list(main_mod._BOOT_SEEDERS)
    assert order.index("admin.seed_agent_profiles") > order.index("admin.seed_prompts")
    assert order.index("admin.seed_agent_profiles") > order.index("admin.sync_tools")
    assert order.index("admin.seed_agent_profiles") > order.index("admin.seed_data_sources")
    assert order.index("admin.seed_eval_suites") > order.index("admin.seed_agent_profiles")
    assert order.index("admin.seed_eval_suites") > order.index("admin.seed_agent_models")
    # The AI-agents demo seeder picks its brain/voice model rows.
    assert order.index("admin.seed_agents_demo") > order.index("admin.seed_agent_models")


async def test_failing_seeder_does_not_skip_later_seeders(monkeypatch, caplog):
    """Seeder N raises -> seeder N+1 (and every later one) still runs, and the
    warning names the failing seeder + the exception."""
    mods = _seeder_modules()
    ran: list = []

    def _boom():
        raise RuntimeError("boom: seeder exploded")

    for path, mod in mods.items():
        if path == "admin.seed_prompts":  # the FIRST seeder — worst case
            monkeypatch.setattr(mod, "run", _boom)
        else:
            monkeypatch.setattr(mod, "run", (lambda p: (lambda: ran.append(p)))(path))

    with caplog.at_level(logging.WARNING):
        await main_mod._run_boot_seeders()

    expected = [p for p in main_mod._BOOT_SEEDERS if p != "admin.seed_prompts"]
    assert ran == expected, "later seeders must run despite the first one failing"
    # The incident shape specifically: seed_voice_scenarios (11th) still ran.
    assert "admin.seed_voice_scenarios" in ran
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    named = [m for m in warnings if "admin.seed_prompts" in m]
    assert named, f"no warning names the failing seeder; got: {warnings}"
    assert any("boom: seeder exploded" in m for m in named)


async def test_each_failure_is_isolated_and_logged(monkeypatch, caplog):
    """Two independent failures (first and a middle seeder): both are logged
    by name, every OTHER seeder still runs exactly once."""
    mods = _seeder_modules()
    seeders = list(main_mod._BOOT_SEEDERS)
    middle = seeders[len(seeders) // 2]
    assert middle != "admin.seed_prompts" and middle != seeders[-1]
    failing = {"admin.seed_prompts", middle}
    ran: list = []

    def _boom():
        raise ValueError("nope")

    for path, mod in mods.items():
        if path in failing:
            monkeypatch.setattr(mod, "run", _boom)
        else:
            monkeypatch.setattr(mod, "run", (lambda p: (lambda: ran.append(p)))(path))

    with caplog.at_level(logging.WARNING):
        await main_mod._run_boot_seeders()

    assert ran == [p for p in main_mod._BOOT_SEEDERS if p not in failing]
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    for path in failing:
        assert any(path in m for m in warnings), f"failure of {path} not logged by name"
