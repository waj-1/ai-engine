"""GET /admin/drift — read-only drift-check screen.

Renders `scripts.check_profile_drift.collect_drift()` findings: whether each
agent profile's SERVED prompt (resolved through `prompt_version_ids`, not
merely the newest version) passes `core.prompt_validator.validate_prompt` —
the same gate the publish path runs before a write — plus a small registry
of config levers whose wrong value is silent. A served prompt DIFFERING from
the repo constant is no longer, by itself, a `critical`/`warning` finding:
prompts are authored in the DB now, so divergence is expected and shows as
`info` (or `warning` when nothing is registered to confirm it's safe). That
module's own docstring says this was "deliberately factored [...] so an
/admin screen or a cron alert can consume it without scraping stdout" — this
is that screen. See CLAUDE.md "Merging a prompt or profile change does NOT
change a live stack" (the repo's documented #2 gotcha) and
docs/admin-portal/04-govern.md#drift-check.

Also surfaces the same curated "runtime flags" block as
`admin_mcp/tools_ops.py`'s `runtime_flags` MCP tool (kept here rather than
importing that module, to avoid pulling the MCP server package into the
portal's import graph for one constant list) — process-visible
container-level flags like `VOICE_LIVE_SIM_ENABLED` that neither the
`agent_env_vars`-backed `/admin/env-vars` screen nor the `env_vars` MCP tool
can see, because application code reads them with a bare `os.getenv` rather
than through `core/env_resolver.py`.

Read-only, superuser-gated. NEVER writes and has no "fix drift" action —
fixing drift is a publish action (a new `agent_prompt_versions` row +
repointing `prompt_version_ids`, or an `update_profile` config edit), which
belongs to the MCP-only publish flow, not this screen.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

from core.deployment import scope_env_vars

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

from admin.auth import require_superuser
from admin.router import router, templates

# Kept in sync with admin_mcp/tools_ops.py's RUNTIME_FLAG_NAMES — small
# curated list of container-level flags whose value is safe to show an
# operator (no secrets). See CLAUDE.md "Config levers that are silent when
# wrong" for why VOICE_LIVE_SIM_ENABLED in particular earns a dedicated
# panel: a voice-eval sim run against a stack without it set silently
# degrades to a real billable provider session instead of erroring.
RUNTIME_FLAG_NAMES: List[str] = [
    "VOICE_LIVE_SIM_ENABLED",
    "ENABLE_ADMIN_MCP",
    "ENVIRONMENT",
    *scope_env_vars(),  # what selects a deployment scope (core/deployment.py)
    "EMBED_PROVIDER",
    "EMBED_MODEL",  # the OpenAI-compatible embedder's model
]


def _runtime_flags() -> List[Dict[str, Any]]:
    return [{"name": n, "value": os.environ.get(n), "source": "process env"}
            for n in RUNTIME_FLAG_NAMES]


@router.get("/drift", response_class=HTMLResponse)
async def drift_screen(
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    from core.plugins import optional_import

    # The drift checker carries a registry of this deployment's own profiles
    # and prompts, so it ships only with builds that have them.
    drift = optional_import("scripts.check_profile_drift")
    DRIFT_SEVERITIES = getattr(drift, "DRIFT_SEVERITIES", ("critical", "warning"))

    def collect_drift(**kwargs):
        if drift is None:
            raise RuntimeError("the config drift check is not part of this build")
        return drift.collect_drift(**kwargs)

    collect_error = None
    try:
        # want_diff=True: unlike the MCP tool (default off — a prompt diff
        # can be large and half-Arabic, costly to put in an LLM's context),
        # a browser render has no such cost, so the screen always shows the
        # full delta for a drifted prompt.
        findings = collect_drift(want_diff=True)
    except Exception as exc:  # noqa: BLE001 — render the page with the error, don't 500.
        findings = []
        collect_error = f"{type(exc).__name__}: {exc}"

    # The scope finding is metadata about the run, not a finding about a
    # profile: pull it out so it neither inflates the drift count nor sits
    # buried in the table. "0 drifted" means nothing unless the reader can see
    # WHICH environment's rows it is about.
    audited_environment = next(
        (f.summary for f in findings if f.kind == "scope"), None)
    findings = [f for f in findings if f.kind != "scope"]
    from admin.screens.govern import group_drift

    ok_count = sum(1 for f in findings if f.severity == "ok")
    # DRIFT_SEVERITIES (critical + warning) — NOT "everything that isn't ok".
    # An "info" finding (an operator-owned lever, or — since Part 3 — a
    # validated prompt that legitimately diverges from its repo constant
    # under DB-authoring) is visibility, not drift, matching the CLI's own
    # exit code for the identical findings.
    drift_count = sum(1 for f in findings if f.severity in DRIFT_SEVERITIES)

    return templates.TemplateResponse(
        request, "drift.html",
        {
            "ctx": ctx,
            "active": "drift",
            # collect_drift() is not env-branched (single AI-project DB,
            # whatever core.auth.get_service_client() resolves for this
            # process) — showing the staging/prod switcher here would imply
            # a control this screen doesn't have.
            "hide_env_switcher": True,
            "audited_environment": audited_environment,
            "findings": findings,
            "groups": group_drift(findings),
            "severity_counts": {sev: sum(1 for f in findings if f.severity == sev)
                                for sev in ("critical", "warning", "info", "ok")},
            "ok_count": ok_count,
            "drift_count": drift_count,
            "collect_error": collect_error,
            "runtime_flags": _runtime_flags(),
        },
    )


@router.get("/diagnostics/runtime", response_class=HTMLResponse)
async def diagnostics_runtime(
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    """Settings › Diagnostics › Runtime environment — the "what is this
    process bound to" card that used to open the Promote tab."""
    from admin._runtime_env import collect_runtime_env

    return templates.TemplateResponse(
        request, "diagnostics_runtime.html",
        {"ctx": ctx, "active": "drift", "hide_env_switcher": True,
         "runtime_env": collect_runtime_env(), "runtime_flags": _runtime_flags()},
    )
