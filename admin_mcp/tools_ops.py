"""Ops-domain MCP tools: drift check, MCP-binding parity, and process-visible
runtime flags — three operator capabilities that existed only as CLI/internal
code before this module, surfaced over MCP so an operator working from
Claude Code doesn't have to open a browser or shell into a container.

- `drift_check` wraps `scripts.check_profile_drift.collect_drift()` — the
  sanctioned way to answer "is my merged prompt/config fix actually live?"
  (CLAUDE.md "Merging a prompt or profile change does NOT change a live
  stack" — the repo's documented #2 gotcha).
- `mcp_flag_parity` wraps `admin.mcp_parity_handlers.compute_mcp_flag_parity`
  — the same logic `/admin/api/mcp-flag-parity` uses — the one health check
  proving a profile's MCP wiring resolves the way the runtime resolves it.
- `runtime_flags` reports a small curated list of process-visible container
  flags (never secrets) that neither the `env_vars` tool nor
  `/admin/env-vars` can see, because they're read by application code via a
  bare `os.getenv` rather than through `core/env_resolver.py`.

All three are read-only — no audit trail, no mutations, matching
`tools_observe.py`'s convention. The same drift + runtime-flags data backs
the `/admin/drift` portal screen (`admin/drift_handlers.py`).
"""
from __future__ import annotations

import dataclasses
import os
from typing import Any, Dict, List

from admin_mcp._common import READ_ONLY, Env, err, tool_errors, use_env, validate_env
from admin_mcp.server import mcp
from core.deployment import scope_env_vars

# Small curated list of container-level flags whose value is safe to report
# over MCP (flags, not secrets — see runtime_flags' own docstring for why
# this list must never grow a secret-valued var). Kept in sync with
# admin/drift_handlers.py's RUNTIME_FLAG_NAMES, which renders the same block
# on the /admin/drift screen.
RUNTIME_FLAG_NAMES: List[str] = [
    "VOICE_LIVE_SIM_ENABLED",
    "ENABLE_ADMIN_MCP",
    "ENVIRONMENT",
    *scope_env_vars(),  # what selects a deployment scope (core/deployment.py)
    "EMBED_PROVIDER",
    "EMBED_MODEL",  # the OpenAI-compatible embedder's model
]


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def drift_check(want_diff: bool = False, profile: str = "",
                agent_kind: str = "") -> Dict[str, Any]:
    """Check that each agent profile's SERVED prompt and key settings are
    healthy. For every profile it validates the prompt version that is
    actually served (not the newest one), and it checks settings whose wrong
    value fails silently, such as voice_config.barge_in_enabled,
    voice_config.answer_watchdog_sec and memory_config.enabled on a voice
    profile. Not part of every build.

    Severities: critical means the served prompt fails validation. A served
    prompt that differs from the copy in the engine's code is expected: it is
    info when it could be validated and warning when it could not. A summary
    containing "[INCIDENT LEVER LEFT ENGAGED]" marks a setting that was
    changed during an incident and never restored;
    summary.incident_levers_engaged counts them.

    Only ONE environment is checked: the one this engine runs as. It is
    returned as audited_environment. Quote it whenever you report a result,
    because "drifted: 0" says nothing about other environments.

    profile (an exact profile slug) and agent_kind narrow the findings; the
    agent_kind filter is best effort. want_diff=true adds the prompt text
    difference, which can be large. Read-only: fix a finding with
    prompts(action="serve") or update_profile."""
    # Maintainer notes (kept out of the description clients read):
    # Wraps scripts.check_profile_drift.collect_drift (same gate as the prompts
    # tool's publish path: core.prompt_validator.validate_prompt). Seeders are
    # insert-if-missing, so once a row exists a repo edit never reaches it and a
    # profile can stay pinned to a bad version unless something checks. Under
    # DB-authoring a served prompt differing from the repo constant is normal.
    # voice_config levers are read from the flat shape; old voice_config.live.*
    # paths are only what an unmigrated row still stores. The agent_kind filter
    # is only as complete as PROMPT_CHECKS' coverage. Every fetch is scoped to
    # one agent_profiles.environment (this process's own) although one database
    # holds every environment's rows.
    from core.plugins import optional_import

    # The drift checker carries a registry of this deployment's own profiles
    # and prompts, so it ships only with builds that have them.
    _drift = optional_import("scripts.check_profile_drift")
    if _drift is None:
        return err("the config drift check is not part of this build")
    DRIFT_SEVERITIES, PROMPT_CHECKS, collect_drift = (
        _drift.DRIFT_SEVERITIES, _drift.PROMPT_CHECKS, _drift.collect_drift)

    findings = collect_drift(want_diff=want_diff)

    # Run metadata, not a profile finding: lift it out of the list so it neither
    # counts as drift nor gets filtered away by `profile`/`agent_kind`. Reported
    # as its own top-level key because a caller acting on "drifted: 0" has to
    # know which environment's rows that zero describes.
    audited_environment = next(
        (f.summary for f in findings if f.kind == "scope"), None)
    findings = [f for f in findings if f.kind != "scope"]

    if profile:
        findings = [f for f in findings
                   if f.target == profile or f.target.startswith(f"{profile}.")]
    if agent_kind:
        slug_to_kind = {c.profile_slug: c.agent_kind for c in PROMPT_CHECKS}
        findings = [f for f in findings
                   if slug_to_kind.get(f.target.split(".", 1)[0]) == agent_kind]

    ok = sum(1 for f in findings if f.severity == "ok")
    # DRIFT_SEVERITIES (critical + warning) — NOT "everything that isn't ok".
    # An "info" finding (an operator-owned lever like progress_after_sec, or
    # — since Part 3 — a validated prompt that legitimately diverges from
    # its repo constant under DB-authoring) is visibility, not drift, and
    # counting it as drifted here would contradict the CLI's own exit code
    # for the identical findings.
    drifted = sum(1 for f in findings if f.severity in DRIFT_SEVERITIES)
    incident_levers_engaged = sum(
        1 for f in findings if "[INCIDENT LEVER LEFT ENGAGED]" in f.summary)

    return {
        "audited_environment": audited_environment,
        "summary": {"ok": ok, "drifted": drifted, "total": len(findings),
                    "incident_levers_engaged": incident_levers_engaged},
        "findings": [dataclasses.asdict(f) for f in findings],
    }


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def mcp_flag_parity(env: Env = "staging") -> Dict[str, Any]:
    """For every active agent profile in env, resolve its tools the way the
    runtime does and report what actually binds. Run it right after
    profile_mcp_attachments(action="set") to confirm the change works. A
    profile that fails is reported with its error; the rest of the report
    still comes back.

    Caution: an external MCP endpoint that cannot be reached shows up as
    FEWER bound tools, not as an error. A low tool count on a profile with an
    external MCP server means "check that the endpoint is reachable", not
    necessarily "the attachment is wrong"."""
    # Maintainer notes (kept out of the description clients read):
    # Same payload and logic as GET /admin/api/mcp-flag-parity (pool_for_kind +
    # AgentProfileLoader.load_active + resolve_mcp_tools_for_profile). The
    # resolver swallows an unreachable external endpoint so one flaky MCP cannot
    # take down tool resolution for every profile.
    from admin.mcp_parity_handlers import compute_mcp_flag_parity
    with use_env(validate_env(env)):
        return compute_mcp_flag_parity(env)


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def runtime_flags() -> Dict[str, Any]:
    """Show a short, fixed list of settings this engine process was started
    with, such as VOICE_LIVE_SIM_ENABLED, ENABLE_ADMIN_MCP, ENVIRONMENT,
    EMBED_PROVIDER and EMBED_MODEL. They are plain process environment
    values, which the env_vars tool cannot see. None of them is a secret.
    value is null when the variable is not set, which is not the same as an
    empty string. Check VOICE_LIVE_SIM_ENABLED before trusting the cost of a
    simulated voice eval: without it the run silently makes a real, billable
    provider call. Returns {flags: [{name, value, source}]}."""
    # Maintainer notes (kept out of the description clients read):
    # Plain os.getenv reads of RUNTIME_FLAG_NAMES, not routed through
    # core/env_resolver.py (so invisible to env_vars and /admin/env-vars, which
    # manage the agent_env_vars table). Flags only: never add a secret-valued
    # variable to the list. endpoints/voice_live.py reads
    # VOICE_LIVE_SIM_ENABLED with a bare os.getenv.
    return {"flags": [
        {"name": n, "value": os.environ.get(n), "source": "process env"}
        for n in RUNTIME_FLAG_NAMES
    ]}
