"""Tests for the ops-domain tools: drift_check, mcp_flag_parity,
runtime_flags. Each wraps existing internal/CLI logic (collect_drift,
compute_mcp_flag_parity, os.getenv) rather than owning its own DB access, so
these tests monkeypatch that logic at its SOURCE module — the tools import
it lazily inside the function body, so patching the source module's
attribute is picked up on the next call (same convention as
tests/admin_mcp/test_tools_observe.py)."""
from __future__ import annotations

import admin_mcp.tools_ops as ops


# ---------------------------------------------------------------------------
# drift_check
# ---------------------------------------------------------------------------


def test_drift_check_shape_and_summary(monkeypatch):
    from scripts.check_profile_drift import Finding
    import scripts.check_profile_drift as drift_mod

    findings = [
        Finding("critical", "prompt", "orchard_default",
                "served system differs from repo (100 chars live vs 90 repo, +10)"),
        Finding("warning", "config",
                "orchard_default.voice_config.live.answer_watchdog_sec",
                "is 0, expected 4.0  [INCIDENT LEVER LEFT ENGAGED]"),
        Finding("ok", "prompt", "orchard_voice", "system matches repo constant"),
    ]
    monkeypatch.setattr(drift_mod, "collect_drift", lambda **kw: findings)

    out = ops.drift_check()

    assert out["summary"] == {"ok": 1, "drifted": 2, "total": 3,
                              "incident_levers_engaged": 1}
    assert len(out["findings"]) == 3
    assert out["findings"][0]["target"] == "orchard_default"
    assert out["findings"][0]["severity"] == "critical"


def test_drift_check_reports_which_environment_it_audited(monkeypatch):
    """`drifted: 0` describes ONE agent_profiles.environment out of the single
    shared AI DB — a caller acting on it has to know which. The scope finding
    is lifted out of `findings` so it neither counts as drift nor gets dropped
    by the profile/agent_kind narrows."""
    from scripts.check_profile_drift import Finding
    import scripts.check_profile_drift as drift_mod

    findings = [
        Finding("info", "scope", "environment",
                "audited environment: prod (from explicit)"),
        Finding("ok", "prompt", "orchard_voice", "system matches repo constant"),
    ]
    monkeypatch.setattr(drift_mod, "collect_drift", lambda **kw: findings)

    out = ops.drift_check()

    assert out["audited_environment"] == "audited environment: prod (from explicit)"
    assert out["summary"] == {"ok": 1, "drifted": 0, "total": 1,
                              "incident_levers_engaged": 0}
    assert [f["kind"] for f in out["findings"]] == ["prompt"]

    # …and it survives a narrow that would otherwise filter it away.
    assert ops.drift_check(profile="orchard_voice")["audited_environment"]


def test_drift_check_defaults_want_diff_off(monkeypatch):
    import scripts.check_profile_drift as drift_mod
    captured = {}

    def fake_collect(**kw):
        captured.update(kw)
        return []

    monkeypatch.setattr(drift_mod, "collect_drift", fake_collect)

    ops.drift_check()
    assert captured == {"want_diff": False}

    ops.drift_check(want_diff=True)
    assert captured == {"want_diff": True}


def test_drift_check_filters_by_profile(monkeypatch):
    from scripts.check_profile_drift import Finding
    import scripts.check_profile_drift as drift_mod

    findings = [
        Finding("ok", "prompt", "orchard_default", "..."),
        Finding("ok", "config", "orchard_default.voice_config.live.barge_in_enabled", "..."),
        Finding("ok", "prompt", "orchard_voice", "..."),
    ]
    monkeypatch.setattr(drift_mod, "collect_drift", lambda **kw: findings)

    out = ops.drift_check(profile="orchard_default")
    targets = sorted(f["target"] for f in out["findings"])
    assert targets == ["orchard_default",
                       "orchard_default.voice_config.live.barge_in_enabled"]


def test_drift_check_incident_lever_count_ignores_non_engaged_findings(monkeypatch):
    from scripts.check_profile_drift import Finding
    import scripts.check_profile_drift as drift_mod

    findings = [
        Finding("warning", "config", "orchard_voice.memory_config.enabled",
                "is True, expected False"),  # not an incident lever
    ]
    monkeypatch.setattr(drift_mod, "collect_drift", lambda **kw: findings)

    out = ops.drift_check()
    assert out["summary"]["incident_levers_engaged"] == 0
    assert out["summary"]["drifted"] == 1


def test_drift_check_info_findings_are_not_counted_as_drifted(monkeypatch):
    """Part 3: a validated prompt that legitimately diverges from its repo
    constant under DB-authoring is reported `info`, and an `info` finding is
    visibility, not drift — same DRIFT_SEVERITIES the CLI's own exit code
    uses. Counting it as drifted here would contradict the CLI for the
    identical findings and defeat the whole point of the reframe."""
    from scripts.check_profile_drift import Finding
    import scripts.check_profile_drift as drift_mod

    findings = [
        Finding("info", "prompt", "orchard_default",
                "served system differs from repo — informational under "
                "DB-authoring"),
        Finding("ok", "prompt", "orchard_voice", "system matches repo constant"),
    ]
    monkeypatch.setattr(drift_mod, "collect_drift", lambda **kw: findings)

    out = ops.drift_check()
    assert out["summary"] == {"ok": 1, "drifted": 0, "total": 2,
                              "incident_levers_engaged": 0}


# ---------------------------------------------------------------------------
# mcp_flag_parity
# ---------------------------------------------------------------------------


def test_mcp_flag_parity_delegates_to_shared_handler(monkeypatch):
    import admin.mcp_parity_handlers as h

    captured = {}

    def fake_compute(env):
        captured["env"] = env
        return {"env": env, "total": 1, "errors": 0, "results": []}

    monkeypatch.setattr(h, "compute_mcp_flag_parity", fake_compute)

    out = ops.mcp_flag_parity(env="prod")

    assert captured == {"env": "prod"}
    assert out == {"env": "prod", "total": 1, "errors": 0, "results": []}


def test_mcp_flag_parity_sets_admin_env_for_the_call(monkeypatch):
    import admin.mcp_parity_handlers as h
    from core.auth import _ADMIN_ENV

    seen = {}

    def fake_compute(env):
        seen["ctx_env"] = _ADMIN_ENV.get()
        return {"env": env}

    monkeypatch.setattr(h, "compute_mcp_flag_parity", fake_compute)

    ops.mcp_flag_parity(env="prod")
    assert seen["ctx_env"] == "prod"
    # Context is unwound after the call — no leakage into later calls.
    assert _ADMIN_ENV.get() is None


def test_mcp_flag_parity_rejects_bad_env():
    out = ops.mcp_flag_parity(env="dev")
    assert "error" in out


# ---------------------------------------------------------------------------
# runtime_flags
# ---------------------------------------------------------------------------


def test_runtime_flags_reports_process_env(monkeypatch):
    monkeypatch.setenv("VOICE_LIVE_SIM_ENABLED", "1")
    monkeypatch.delenv("EMBED_PROVIDER", raising=False)

    out = ops.runtime_flags()
    by_name = {f["name"]: f for f in out["flags"]}

    assert by_name["VOICE_LIVE_SIM_ENABLED"] == {
        "name": "VOICE_LIVE_SIM_ENABLED", "value": "1", "source": "process env"}
    assert by_name["EMBED_PROVIDER"]["value"] is None
    assert by_name["EMBED_PROVIDER"]["source"] == "process env"
    assert set(by_name) == set(ops.RUNTIME_FLAG_NAMES)


def test_runtime_flags_unset_var_is_null_not_missing(monkeypatch):
    monkeypatch.delenv("EMBED_PROVIDER", raising=False)
    out = ops.runtime_flags()
    by_name = {f["name"]: f for f in out["flags"]}
    assert "EMBED_PROVIDER" in by_name
    assert by_name["EMBED_PROVIDER"]["value"] is None


def test_runtime_flags_list_never_carries_a_secret_looking_name():
    # Guard against someone extending the curated list with a secret-valued
    # var — these are reported unredacted by design (flags, not secrets).
    for name in ops.RUNTIME_FLAG_NAMES:
        upper = name.upper()
        assert not any(bad in upper for bad in ("SECRET", "KEY", "TOKEN", "PASSWORD"))
