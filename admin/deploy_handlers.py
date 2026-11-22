"""Deploy-from-portal trigger + status polling.

Posts a `workflow_dispatch` to GitHub Actions for
`.github/workflows/deploy-agent-from-portal.yaml` in the repository named by
`GITHUB_DEPLOY_REPO` (default: the plugin's `deployment.deploy_repo()`; none
without a plugin, and then the deploy buttons say it isn't set up). The
workflow builds and rolls the prod chat service on Azure Container Apps by
running `az containerapp update` in `ACA_RESOURCE_GROUP` — the only
deployable target (single-DB main-only topology; there is no staging
service, `dev` is local-only and has no Container App).

NOTE: this deploys the agent IMAGE only — it does NOT apply Supabase
migrations. Schema changes ship via the separate `.github/workflows/
deploy-ai-agent-db.yaml` pipeline (push to main migrates the one AI-Supabase
DB; single-DB main-only topology, no staging branch). Trigger a portal
deploy only after the matching migrations have landed through that
pipeline, or the new image will run against an un-migrated database.

The portal does NOT run the Azure CLI or psql itself — it only dispatches
the workflow and audits the dispatch. The workflow run is the source of
truth for what actually deployed.

Requires the `GITHUB_DEPLOY_TOKEN` env var (a fine-grained PAT with
`actions:read` + `actions:write` on this repo). If unset the trigger
handler returns 400 with a hint; the status fetcher returns an empty
list + logs (the UI falls through to a "configure GITHUB_DEPLOY_TOKEN"
hint).

This module also exposes:

  - `_fetch_recent_deploy_runs()`  — lists recent runs of the deploy and
    rollback workflows, used by the history table and polling JSON.
  - `_fetch_run_logs_summary()`    — fetches one run + its jobs/steps for
    in-progress detail (current step, fail step + error).
  - POST `/env/{environment}/rollback` — workflow_dispatch the rollback
    workflow with an Azure Container App revision name.
  - GET  `/deploy-runs.json`       — same payload as the history table,
    JSON, for the 5s polling JS.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx
from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from admin.auth import require_superuser
from admin.router import router

logger = logging.getLogger(__name__)

WORKFLOW_FILE = "deploy-agent-from-portal.yaml"
ROLLBACK_WORKFLOW_FILE = "rollback-agent.yaml"


def github_repo() -> str:
    """`owner/name` of the repository whose workflows deploy this engine:
    `GITHUB_DEPLOY_REPO`, else the plugin's `deployment.deploy_repo()`, else
    "" (deploying from the portal is not set up). Read per call."""
    repo = (os.environ.get("GITHUB_DEPLOY_REPO") or "").strip()
    if repo:
        return repo
    from core.plugins import call_hook, load_hook

    found = [r for r in call_hook(load_hook("deployment"), "deploy_repo") if r]
    return found[-1] if found else ""


def dispatch_url() -> str:
    return f"https://api.github.com/repos/{github_repo()}/actions/workflows/{WORKFLOW_FILE}/dispatches"


def rollback_dispatch_url() -> str:
    return f"https://api.github.com/repos/{github_repo()}/actions/workflows/{ROLLBACK_WORKFLOW_FILE}/dispatches"

# Azure Container App revision names (e.g. <app>--0000093):
# lowercase letters, digits, and hyphens, 1-63 chars. This regex is
# intentionally permissive — it exists to stop shell escapes / SQL-style
# payloads from being dispatched into the rollback workflow, not to fully
# validate the "<app>--<suffix>" shape (rollback-agent.yaml's own
# `^<app>--[a-z0-9-]+$` check does that).
_REVISION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

# Refs the deploy button is allowed to dispatch. `main` is the only branch
# that ships to Azure Container Apps now (single-DB main-only topology —
# `production` is a no-op tracking branch, see DEPLOY.md); release tags
# (`v1.2.3`) are the recommended way to pin a known-good build. Anything
# else (feature branches, arbitrary SHAs) is rejected so a compromised
# admin session can't deploy unreviewed code to prod.
_ALLOWED_BRANCHES = frozenset({"main"})
_ALLOWED_TAG_RE = re.compile(r"^v\d+(\.\d+){0,2}([-+][0-9A-Za-z.-]+)?$")

def _deployment_default(fn: str) -> str:
    """The last non-empty answer of the plugins' ``deployment.<fn>()``, or ""."""
    from core.plugins import call_hook, load_hook

    found = [r for r in call_hook(load_hook("deployment"), fn) if r]
    return found[-1] if found else ""


def _service_name(environment: str) -> str:
    """The Azure Container App a deploy target runs as: `ACA_APP_NAME`, else
    the plugin's `deployment.container_app()`, else "" (not set up). One app
    for every target: `staging` is an environment column value, not a
    deployable service (the staging service was retired), and `dev` is
    local-only, so every value maps to the prod app. Read per call."""
    return ((os.environ.get("ACA_APP_NAME") or "").strip()
            or _deployment_default("container_app"))


def _resource_group() -> str:
    """The Container App's resource group: `ACA_RESOURCE_GROUP`, else the
    plugin's `deployment.resource_group()`, else "" (not set up)."""
    return ((os.environ.get("ACA_RESOURCE_GROUP") or "").strip()
            or _deployment_default("resource_group"))


def _validate_git_ref(git_ref: str) -> None:
    """Raise HTTP 400 if `git_ref` isn't on the deploy allowlist."""
    if git_ref in _ALLOWED_BRANCHES:
        return
    if _ALLOWED_TAG_RE.match(git_ref):
        return
    raise HTTPException(
        status_code=400,
        detail=(
            f"git_ref '{git_ref}' is not allowed. Allowed values: "
            f"{sorted(_ALLOWED_BRANCHES)} or a release tag like 'v1.2.3'. "
            f"Land your change on main first, then trigger deploy from there."
        ),
    )


@router.post("/env/{environment}/deploy")
async def env_deploy(
    request: Request,
    environment: str,
    git_ref: str = Form("main"),
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Thin wrapper — see `admin.services.deploys.dispatch_deploy` for the
    validation, GitHub dispatch, and audit logic shared with any future MCP
    ship tool."""
    from admin.services.deploys import DeployError, dispatch_deploy

    try:
        await dispatch_deploy(environment=environment, git_ref=git_ref,
                               actor_user_id=ctx["user_id"])
    except DeployError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    # Redirect to the History tab so the operator can immediately see the
    # newly-dispatched run appear in the deploy table.
    return RedirectResponse(url="/admin/deployments?tab=history", status_code=302)


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


@router.post("/env/{environment}/rollback")
async def env_rollback(
    request: Request,
    environment: str,
    target_revision: str = Form(...),
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Thin wrapper — see `admin.services.deploys.dispatch_rollback` for the
    validation, GitHub dispatch, and audit logic shared with any future MCP
    ship tool."""
    from admin.services.deploys import DeployError, dispatch_rollback

    try:
        await dispatch_rollback(environment=environment, target_revision=target_revision,
                                 actor_user_id=ctx["user_id"])
    except DeployError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    # Redirect to the History tab so the operator can see the rollback run appear.
    return RedirectResponse(url="/admin/deployments?tab=history", status_code=302)


# ---------------------------------------------------------------------------
# Status fetching (GitHub Actions REST)
# ---------------------------------------------------------------------------


def _github_headers() -> Optional[Dict[str, str]]:
    """Common headers for GH API reads. Returns None if no token is set —
    callers should log + fall through to an empty result."""
    token = os.environ.get("GITHUB_DEPLOY_TOKEN")
    if not token:
        return None
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _parse_dt(iso: Optional[str]) -> Optional[_dt.datetime]:
    if not iso:
        return None
    try:
        # GitHub returns 'Z'-suffixed ISO timestamps; normalize to +00:00.
        return _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def _shape_run(raw: Dict[str, Any], workflow_label: str) -> Dict[str, Any]:
    """Project a GH workflow-run blob into the dict the template + JSON
    polling consume. Keeps the surface tiny so we can swap to a richer
    fetcher later (e.g. Azure Container Apps diagnostic logs) without
    rewriting the UI."""
    created = _parse_dt(raw.get("created_at"))
    updated = _parse_dt(raw.get("updated_at"))
    duration_s: Optional[int] = None
    if created and updated and raw.get("status") == "completed":
        duration_s = max(0, int((updated - created).total_seconds()))

    # Cheap target_env extraction: GitHub returns `display_title` and a
    # `name` of the workflow but does NOT echo the dispatch `inputs` on
    # /runs unless we follow `jobs_url`. To stay one HTTP call per list we
    # parse the run name (workflow includes target_env in display_title
    # when invoked via dispatch) — falling back to None when unclear.
    inputs = raw.get("display_title") or ""
    target_env: Optional[str] = None
    if "prod" in inputs.lower():
        target_env = "prod"
    elif "staging" in inputs.lower():
        target_env = "staging"

    return {
        "run_id": raw.get("id"),
        "workflow_label": workflow_label,
        "status": raw.get("status"),
        "conclusion": raw.get("conclusion"),
        "head_branch": raw.get("head_branch"),
        "head_sha": raw.get("head_sha"),
        "head_sha_short": (raw.get("head_sha") or "")[:7],
        "actor": (raw.get("actor") or {}).get("login"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "duration_s": duration_s,
        "html_url": raw.get("html_url"),
        "run_attempt": raw.get("run_attempt"),
        "target_env": target_env,
        "event": raw.get("event"),
    }


async def _fetch_recent_deploy_runs(*, limit: int = 20,
                                    errors: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """GET /repos/{owner}/{repo}/actions/workflows/{workflow_file}/runs
    for *both* the deploy and rollback workflows, merge by created_at desc,
    truncate to `limit`. On missing token or any HTTP error: log and return
    [] so the page still renders. Pass an `errors` list to collect a plain
    reason for each failure (the history table shows it instead of an empty
    "no runs" that reads like a clean history).
    """
    if not github_repo():
        if errors is not None:
            errors.append("Deploy history isn't set up here: no deploy repository "
                          "(GITHUB_DEPLOY_REPO) is configured.")
        return []
    headers = _github_headers()
    if headers is None:
        logger.info("deploy status: GITHUB_DEPLOY_TOKEN not set, skipping fetch")
        if errors is not None:
            errors.append("Deploy history isn't set up here: the portal has no GitHub deploy "
                          "token (GITHUB_DEPLOY_TOKEN).")
        return []

    workflows = [
        (WORKFLOW_FILE, "deploy"),
        (ROLLBACK_WORKFLOW_FILE, "rollback"),
    ]
    all_runs: List[Dict[str, Any]] = []
    per_workflow = max(5, limit)
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            for wf_file, label in workflows:
                url = (
                    f"https://api.github.com/repos/{github_repo()}/actions/workflows/"
                    f"{wf_file}/runs?per_page={per_workflow}"
                )
                try:
                    resp = await http.get(url, headers=headers)
                except httpx.HTTPError as exc:
                    logger.warning("deploy status: %s fetch failed: %s", wf_file, exc)
                    if errors is not None:
                        errors.append("GitHub could not be reached.")
                    continue
                if resp.status_code == 404:
                    # Workflow file not committed yet — fine, skip silently.
                    continue
                if resp.status_code >= 300:
                    logger.warning(
                        "deploy status: %s returned %s: %s",
                        wf_file, resp.status_code, resp.text[:200],
                    )
                    if errors is not None:
                        errors.append(f"GitHub answered {resp.status_code} — check the deploy token's access.")
                    continue
                data = resp.json() or {}
                for raw in (data.get("workflow_runs") or []):
                    all_runs.append(_shape_run(raw, label))
    except Exception:
        logger.exception("deploy status: unexpected error fetching runs")
        if errors is not None:
            errors.append("Deploy history could not be loaded.")
        return []

    all_runs.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return all_runs[:limit]


async def _fetch_run_logs_summary(run_id: int) -> Dict[str, Any]:
    """GET /repos/{owner}/{repo}/actions/runs/{run_id} + its /jobs for
    richer detail: per-job timings, the current step when in_progress,
    and the failing step + name when conclusion=='failure'.

    Returns `{}` on any error so callers can render a graceful fallback.
    """
    headers = _github_headers()
    if headers is None or not github_repo():
        return {}

    base = f"https://api.github.com/repos/{github_repo()}/actions/runs/{run_id}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            run_resp = await http.get(base, headers=headers)
            if run_resp.status_code >= 300:
                logger.warning(
                    "deploy logs: run %s returned %s",
                    run_id, run_resp.status_code,
                )
                return {}
            run = run_resp.json() or {}
            jobs_resp = await http.get(base + "/jobs", headers=headers)
            jobs = (jobs_resp.json() or {}).get("jobs", []) if jobs_resp.status_code < 300 else []
    except httpx.HTTPError as exc:
        logger.warning("deploy logs: network error for run %s: %s", run_id, exc)
        return {}
    except Exception:
        logger.exception("deploy logs: unexpected error for run %s", run_id)
        return {}

    current_step: Optional[Dict[str, Any]] = None
    fail_step: Optional[Dict[str, Any]] = None
    job_summaries: List[Dict[str, Any]] = []
    for job in jobs:
        started = _parse_dt(job.get("started_at"))
        completed = _parse_dt(job.get("completed_at"))
        duration_s: Optional[int] = None
        if started and completed:
            duration_s = max(0, int((completed - started).total_seconds()))
        job_summaries.append({
            "name": job.get("name"),
            "status": job.get("status"),
            "conclusion": job.get("conclusion"),
            "duration_s": duration_s,
            "html_url": job.get("html_url"),
        })
        for step in (job.get("steps") or []):
            if step.get("status") == "in_progress" and current_step is None:
                current_step = {"job": job.get("name"), "name": step.get("name")}
            if step.get("conclusion") == "failure" and fail_step is None:
                fail_step = {
                    "job": job.get("name"),
                    "name": step.get("name"),
                    "number": step.get("number"),
                }

    return {
        "run_id": run_id,
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "current_step": current_step,
        "fail_step": fail_step,
        "jobs": job_summaries,
        "html_url": run.get("html_url"),
    }


# ---------------------------------------------------------------------------
# JSON endpoint for polling JS
# ---------------------------------------------------------------------------


@router.get("/deploy-runs.json")
async def deploy_runs_json(
    env: Optional[str] = Query(None, pattern=r"^(staging|prod)$"),
    limit: int = Query(20, ge=1, le=50),
    ctx: dict = Depends(require_superuser),
) -> JSONResponse:
    """Same payload the history table renders, as JSON, for the polling
    JS on `deployments.html`. `Cache-Control: no-store` so a stale tab
    can't show "completed" forever after a real failure."""
    errors: List[str] = []
    runs = await _fetch_recent_deploy_runs(limit=limit, errors=errors)
    if env:
        # The deploy workflow encodes target_env in `display_title` (so
        # `_shape_run` already extracted it). Match either that or, for
        # push-triggered deploys that don't go through the portal, the
        # head_branch convention: main→prod (single-DB main-only).
        def _matches(r: Dict[str, Any]) -> bool:
            if r.get("target_env") == env:
                return True
            # Single-DB main-only: prod ships from main; staging is not deployed.
            if env == "prod" and r.get("head_branch") == "main":
                return True
            return False
        runs = [r for r in runs if _matches(r)]
    payload: Dict[str, Any] = {"runs": runs, "env": env,
                               "fetched_at": _dt.datetime.utcnow().isoformat() + "Z"}
    if errors and not runs:
        payload["error"] = errors[0]
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Recent Container App revisions (rollback picker)
# ---------------------------------------------------------------------------

_ARM = "https://management.azure.com"
# Revisions built before the voice-config flattening migration
# (20260817120000_unify_voice_config) cannot read the live schema — see the
# ROLLBACK FLOOR note in .github/workflows/rollback-agent.yaml. The picker
# shows them but won't let you choose them.
_ROLLBACK_FLOOR = os.environ.get("ROLLBACK_FLOOR", "2026-08-17T12:00:00+00:00")
_ARM_API_VERSION = "2024-03-01"


def _arm_token() -> str:
    """ARM bearer token via DefaultAzureCredential (the app's managed
    identity on Azure; `az login` locally). Raises on failure."""
    from azure.identity import DefaultAzureCredential

    cred = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    return cred.get_token(f"{_ARM}/.default").token


async def _fetch_revisions(environment: str, *, limit: int = 10) -> Dict[str, Any]:
    """Recent revisions of the deploy target's Container App, newest first.

    Read-only ARM call (`Microsoft.App/containerApps/{app}/revisions`). Needs
    `AZURE_SUBSCRIPTION_ID`, the app and its resource group (`_service_name`,
    `_resource_group`: env, else the plugin's defaults) and an identity
    allowed to read the app. Any failure comes
    back as `{"revisions": [], "error": "<plain reason>"}` — the rollback form
    then falls back to typing a revision name.
    """
    sub = (os.environ.get("AZURE_SUBSCRIPTION_ID") or "").strip()
    if not sub:
        return {"revisions": [], "error": "The list of recent versions isn't available here "
                                          "(no Azure subscription is configured)."}
    rg = _resource_group()
    app = _service_name(environment)
    if not (rg and app):
        return {"revisions": [], "error": "The list of recent versions isn't available here "
                                          "(ACA_APP_NAME and ACA_RESOURCE_GROUP are not configured)."}
    url = (f"{_ARM}/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.App/"
           f"containerApps/{app}/revisions?api-version={_ARM_API_VERSION}")
    try:
        import asyncio

        token = await asyncio.wait_for(asyncio.to_thread(_arm_token), timeout=15)
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.get(url, headers={"Authorization": f"Bearer {token}"})
    except Exception as exc:  # noqa: BLE001 — credential / network failures are expected off-Azure
        logger.info("revisions: fetch failed (%s)", exc)
        return {"revisions": [], "error": "Couldn't load recent versions from Azure."}
    if resp.status_code >= 300:
        logger.warning("revisions: ARM returned %s: %s", resp.status_code, resp.text[:200])
        return {"revisions": [], "error": f"Azure answered {resp.status_code} when listing versions."}
    out: List[Dict[str, Any]] = []
    for raw in (resp.json() or {}).get("value") or []:
        props = raw.get("properties") or {}
        image = ""
        containers = ((props.get("template") or {}).get("containers") or [])
        if containers:
            image = (containers[0].get("image") or "").rsplit(":", 1)[-1]
        out.append({
            "name": raw.get("name"),
            "created_at": props.get("createdTime"),
            "active": bool(props.get("active")),
            "traffic": props.get("trafficWeight") or 0,
            "state": props.get("runningState"),
            "image_tag": image[:7],
            "below_floor": bool(props.get("createdTime")) and str(props.get("createdTime")) < _ROLLBACK_FLOOR,
        })
    out = [r for r in out if r.get("name") and _REVISION_RE.match(r["name"])]
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return {"revisions": out[:limit], "app": app}


@router.get("/deploy-revisions.json")
async def deploy_revisions_json(
    env: str = Query("prod", pattern=r"^(staging|prod)$"),
    ctx: dict = Depends(require_superuser),
) -> JSONResponse:
    """Recent revisions for the rollback picker on /admin/deployments."""
    data = await _fetch_revisions(env)
    return JSONResponse(data, headers={"Cache-Control": "no-store"})
