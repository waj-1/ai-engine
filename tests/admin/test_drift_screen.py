"""Tests for GET /admin/drift (the drift-check portal screen).

Mirrors tests/admin/test_mcp_parity_endpoint.py's pattern: a minimal FastAPI
host with just `admin.router.router`, the superuser dependency overridden (or
not, for the gate test), and `scripts.check_profile_drift.collect_drift`
monkeypatched at its SOURCE module — `admin/drift_handlers.py` imports it
lazily inside the route function, so patching the source module's attribute
is picked up on the next call.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List

from fastapi import FastAPI
from fastapi.testclient import TestClient

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


def _app() -> TestClient:
    from admin.router import router
    from admin import auth

    app = FastAPI()
    app.dependency_overrides[auth.require_superuser] = lambda: {
        "user_id": "u", "email": "e", "user_token": "t"}
    app.include_router(router)
    return TestClient(app)


def _findings(monkeypatch, findings: List):
    import scripts.check_profile_drift as drift_mod
    monkeypatch.setattr(drift_mod, "collect_drift", lambda **kw: findings)


def test_drift_screen_requires_superuser():
    from admin.router import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    r = client.get("/admin/drift")
    assert r.status_code in (401, 403)


def test_drift_screen_renders_ok_and_drifted(monkeypatch):
    from scripts.check_profile_drift import Finding

    _findings(monkeypatch, [
        Finding("critical", "prompt", "orchard_default",
                "served system differs from repo (100 chars live vs 90 repo, +10)",
                detail="a merged fix is not live"),
        Finding("critical", "config", "orchard_default.voice_config.barge_in_enabled",
                "is False, expected True  [INCIDENT LEVER LEFT ENGAGED]",
                detail="callers cannot interrupt at all"),
        Finding("ok", "prompt", "orchard_voice", "system matches repo constant"),
    ])

    r = _app().get("/admin/drift")
    assert r.status_code == 200
    assert "Drift Check" in r.text
    assert "orchard_default" in r.text
    assert "INCIDENT LEVER LEFT ENGAGED" in r.text
    # Drifted finding's detail is shown (expanded, not hidden behind a click).
    assert "callers cannot interrupt at all" in r.text
    # ok finding still listed but without its (nonexistent) detail text leaking.
    assert "orchard_voice" in r.text


def test_drift_screen_renders_prompt_diff_for_drifted_prompt(monkeypatch):
    from scripts.check_profile_drift import Finding

    _findings(monkeypatch, [
        Finding("critical", "prompt", "orchard_default", "served differs from repo",
                detail="why", diff=["-old line", "+new line"]),
    ])

    r = _app().get("/admin/drift")
    assert r.status_code == 200
    assert "new line" in r.text


def test_drift_screen_no_drift_shows_zero_chip(monkeypatch):
    from scripts.check_profile_drift import Finding

    _findings(monkeypatch, [
        Finding("ok", "prompt", "orchard_default", "system matches repo constant"),
        Finding("ok", "config", "orchard_voice.memory_config.enabled", "= False"),
    ])

    r = _app().get("/admin/drift")
    assert r.status_code == 200
    assert "0 drifted" in r.text


def test_drift_screen_info_prompt_finding_does_not_count_as_drift(monkeypatch):
    """Part 3: a validated prompt that legitimately diverges from its repo
    constant under DB-authoring is `info`, and `info` is visibility, not
    drift — the screen's chip must agree with the CLI's own exit code for
    the identical findings, not count every non-`ok` finding as drifted."""
    from scripts.check_profile_drift import Finding

    _findings(monkeypatch, [
        Finding("info", "prompt", "orchard_default",
                "served system differs from repo constant "
                "(150 chars live vs 100 repo, +50) — informational under "
                "DB-authoring: the served version passed validation"),
    ])

    r = _app().get("/admin/drift")
    assert r.status_code == 200
    assert "0 drifted" in r.text
    assert "informational under DB-authoring" in r.text


def test_drift_screen_info_finding_gets_an_info_chip_not_a_warning_chip(monkeypatch):
    """An 'info' finding must never render the same warning-orange chip as
    an actual drift finding — the pre-Part-3 template's severity branch was
    ok / critical / else(warning), which silently mislabelled every 'info'
    finding (e.g. the pre-existing progress_after_sec lever) as a warning."""
    from scripts.check_profile_drift import Finding

    _findings(monkeypatch, [
        Finding("info", "config", "orchard_default.voice_config.progress_after_sec",
                "is 2.5, code default 4.0", detail="hidden unless drifted",
                diff=["-old", "+new"]),
    ])

    r = _app().get("/admin/drift")
    assert r.status_code == 200
    assert 'class="chip chip-ghost' in r.text
    assert 'class="chip chip-warn' not in r.text
    # The diff still renders (matches the CLI's unconditional diff loop)...
    assert "+new" in r.text
    # ...but the `why` detail — gated the same as the CLI's `why:` line — does
    # not, since that gate is only critical/warning.
    assert "hidden unless drifted" not in r.text


def test_drift_screen_handles_collect_error_without_500(monkeypatch):
    import scripts.check_profile_drift as drift_mod

    def _boom(**kw):
        raise RuntimeError("no reachable AI project")

    monkeypatch.setattr(drift_mod, "collect_drift", _boom)

    r = _app().get("/admin/drift")
    assert r.status_code == 200
    assert "could not run" in r.text
    assert "no reachable AI project" in r.text


def test_drift_screen_shows_runtime_flags(monkeypatch):
    from scripts.check_profile_drift import Finding

    _findings(monkeypatch, [])
    monkeypatch.setenv("VOICE_LIVE_SIM_ENABLED", "1")
    monkeypatch.delenv("EMBED_PROVIDER", raising=False)

    r = _app().get("/admin/drift")
    assert r.status_code == 200
    assert "VOICE_LIVE_SIM_ENABLED" in r.text
    assert "EMBED_PROVIDER" in r.text
    # Unset flags render as a distinguishable "unset" chip, not a blank cell.
    assert "unset" in r.text
