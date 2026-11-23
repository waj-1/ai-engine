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
    """Check whether each agent profile's SERVED prompt (resolved through
    prompt_version_ids — not the newest version) passes
    core.prompt_validator.validate_prompt — the same gate
    admin_mcp/tools_configure.py's publish path runs before a write — plus a
    registry of config levers whose wrong value is silent
    (voice_config.barge_in_enabled, voice_config.answer_watchdog_sec,
    memory_config.enabled on a voice profile, ...  — voice_config is FLAT since
    2026-08-17; the old voice_config.live.* paths are what an unmigrated row
    still stores, not where a lever is read from). A served prompt DIFFERING
    from the repo constant is expected under DB-authoring (prompts change
    through MCP/API/DB, never a merge) and shows as `info` when the
    validator has real rules to check it against, `warning` when it does
    not (unconfirmed, not proven safe). What is still `critical` is the
    served version FAILING validation outright — the modern shape of "is my
    merged prompt fix actually live?": every scripts/seed_*.py /
    admin/seed_*.py is insert-if-missing, so once a row exists a seeder can
    NEVER push a repo edit into it, and a profile can stay pinned to a bad
    version indefinitely unless something checks for it. Findings whose summary carries
    "[INCIDENT LEVER LEFT ENGAGED]" are config levers pulled during a past
    incident and never restored — a different failure mode from "never
    configured" and worth flagging louder; summary.incident_levers_engaged
    counts them. want_diff=False by default: a prompt diff can be large and
    half Arabic, expensive to put in an LLM's context — pass true only when
    you need the actual text delta. profile/agent_kind optionally narrow the
    findings (profile matches the exact agent_profiles.slug a finding is
    about; agent_kind matches that slug's agent_kind per the check
    registry — best-effort, only as complete as PROMPT_CHECKS' coverage).
    Every fetch is scoped to ONE agent_profiles.environment — this process's
    own — and the AI project is a single DB holding every environment's rows,
    so `drifted: 0` describes that environment and nothing else. The audited
    value is returned as `audited_environment`; quote it whenever you report a
    verdict. Read-only: this reports drift, it does not fix it — publish the
    fix with `prompts action=serve` / `update_profile`."""
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
    """For every active agent profile in `env`, resolve its tool set the way
    the runtime does (pool_for_kind + AgentProfileLoader.load_active +
    resolve_mcp_tools_for_profile) and report what actually binds — the
    health check to run right after `profile_mcp_attachments action=set`, to
    confirm the wiring resolves rather than trusting the write succeeded.
    Same payload/logic as GET /admin/api/mcp-flag-parity. Degrades
    per-profile: one bad profile is reported with an error, never crashes
    the whole report. CAVEAT: an unreachable external MCP endpoint (e.g. a
    down partner sandbox) shows up as FEWER bound tools, not an error — the
    resolver swallows that failure so one flaky external MCP can't take
    down tool resolution for every profile. A low tool count on an
    external-source profile means "check the endpoint is reachable", not
    necessarily "the attachment is wrong"."""
    from admin.mcp_parity_handlers import compute_mcp_flag_parity
    with use_env(validate_env(env)):
        return compute_mcp_flag_parity(env)


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def runtime_flags() -> Dict[str, Any]:
    """Process-visible values of a small curated list of container-level
    flags THIS process was booted with — plain os.getenv reads, not routed
    through core/env_resolver.py, so they are invisible to the env_vars tool
    and /admin/env-vars (which CRUD the agent_env_vars table, a different
    read path entirely). Flags, not secrets — nothing on this list is
    sensitive, and it must stay that way; never add a secret-valued var
    here. Chiefly for VOICE_LIVE_SIM_ENABLED: endpoints/voice_live.py reads
    it with a bare os.getenv, so a voice-eval sim run against a stack
    without it set silently degrades to a real billable provider session
    instead of erroring — check this before trusting a sim run's cost
    profile. `value` is null when the var is unset on this process (not the
    same as being set to an empty string)."""
    return {"flags": [
        {"name": n, "value": os.environ.get(n), "source": "process env"}
        for n in RUNTIME_FLAG_NAMES
    ]}
