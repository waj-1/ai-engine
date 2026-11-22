"""Deploy/rollback dispatch service — the single implementation behind the
portal's POST /admin/env/{environment}/deploy and /rollback routes (and,
later, any MCP ship_deploy/ship_rollback tools).

Posts a `workflow_dispatch` to GitHub Actions for the deploy-from-portal /
rollback-agent workflows. See `admin.deploy_handlers`'s module docstring
for the full pipeline context (this module only owns the *dispatch* step;
status polling and history fetching stay in `deploy_handlers.py`).

The repository (`github_repo()`), workflow constants (`WORKFLOW_FILE`, ...), the git-ref allowlist
(`_validate_git_ref`), the Azure Container App revision regex
(`_REVISION_RE`), and the service-name mapper (`_service_name`) all
still live in `admin.deploy_handlers` and are imported from there —
this module is the only importer in the other direction, so there is
no import cycle.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict

import httpx

from admin.audit import record_change
from admin.deploy_handlers import (
    dispatch_url,
    github_repo,
    rollback_dispatch_url,
    ROLLBACK_WORKFLOW_FILE,
    WORKFLOW_FILE,
    _REVISION_RE,
    _service_name,
    _validate_git_ref,
)

logger = logging.getLogger(__name__)


class DeployError(Exception):
    def __init__(self, status_code: int, detail: Any):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def _missing_token_error() -> DeployError:
    return DeployError(
        400,
        (
            "GITHUB_DEPLOY_TOKEN is not configured. Set it in .env "
            "(local) or in the agent service env vars (cloud). It must be a "
            f"GitHub fine-grained PAT with actions:write on {github_repo()}."
        ),
    )


def _missing_repo_error() -> DeployError:
    return DeployError(
        400,
        "Deploying from the portal is not configured: set GITHUB_DEPLOY_REPO "
        "(owner/name of the repository with the deploy workflows).",
    )


def _github_dispatch_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def dispatch_deploy(*, environment: str, git_ref: str, actor_user_id: str) -> Dict[str, Any]:
    """Dispatch `deploy-agent-from-portal.yaml` workflow_dispatch for
    `environment` at `git_ref`. Mirrors `dispatch_rollback`'s auth + audit
    + error shape."""
    if environment != "prod":
        raise DeployError(
            400,
            "Only 'prod' is deployable. 'staging' is an environment column, not a "
            "service (the staging service was retired). The canonical "
            "deploy is a merge to main, which ships both the control-plane and prod chat.",
        )
    _validate_git_ref(git_ref)
    if not github_repo():
        raise _missing_repo_error()

    token = os.environ.get("GITHUB_DEPLOY_TOKEN")
    if not token:
        raise _missing_token_error()

    payload: Dict[str, Any] = {
        "ref": git_ref,
        "inputs": {"target_env": environment},
    }
    headers = _github_dispatch_headers(token)

    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.post(dispatch_url(), headers=headers, json=payload)
    except httpx.HTTPError as exc:
        logger.exception("deploy-from-portal: dispatch network error")
        raise DeployError(502, f"GitHub dispatch failed: {exc}") from exc

    # GitHub returns 204 No Content on success; anything else is a config error
    # (bad token scope, missing workflow file, repo typo). Surface the body.
    if resp.status_code >= 300:
        logger.warning(
            "deploy-from-portal: dispatch returned %s: %s",
            resp.status_code, resp.text[:500],
        )
        raise DeployError(
            502,
            f"GitHub dispatch returned {resp.status_code}: {resp.text[:300]}",
        )

    record_change(
        entity_type="environment",
        entity_id=environment,
        action="DEPLOY_TRIGGERED",
        actor_user_id=actor_user_id,
        after={
            "target_env": environment,
            "git_ref": git_ref,
            "workflow": WORKFLOW_FILE,
            "repo": github_repo(),
        },
    )
    return {
        "dispatched": True,
        "environment": environment,
        "git_ref": git_ref,
        "workflow": WORKFLOW_FILE,
        "repo": github_repo(),
    }


async def dispatch_rollback(*, environment: str, target_revision: str,
                             actor_user_id: str) -> Dict[str, Any]:
    """Dispatch `rollback-agent.yaml` workflow_dispatch with the chosen
    revision name. Mirrors `dispatch_deploy`'s auth + audit + error shape."""
    if environment != "prod":
        raise DeployError(
            400,
            "Only 'prod' is deployable. 'staging' is an environment column, not a "
            "service (the staging service was retired). The canonical "
            "deploy is a merge to main, which ships both the control-plane and prod chat.",
        )
    target_revision = (target_revision or "").strip()
    if not _REVISION_RE.match(target_revision):
        raise DeployError(
            400,
            (
                f"target_revision '{target_revision}' is not a valid Azure "
                f"Container App revision name. Expected lowercase letters, "
                f"digits, and hyphens (max 63 chars). Run `az containerapp "
                f"revision list -n {_service_name(environment) or '<app>'} -g <resource group> "
                f"-o table` to see the available revision names."
            ),
        )
    if not github_repo():
        raise _missing_repo_error()

    token = os.environ.get("GITHUB_DEPLOY_TOKEN")
    if not token:
        raise _missing_token_error()

    payload: Dict[str, Any] = {
        # Rollback always dispatched from `main` — the workflow itself doesn't
        # build anything, it just shifts traffic, so the ref only matters for
        # selecting the workflow file definition.
        "ref": "main",
        "inputs": {"target_env": environment, "target_revision": target_revision},
    }
    headers = _github_dispatch_headers(token)

    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.post(rollback_dispatch_url(), headers=headers, json=payload)
    except httpx.HTTPError as exc:
        logger.exception("rollback-from-portal: dispatch network error")
        raise DeployError(502, f"GitHub dispatch failed: {exc}") from exc

    if resp.status_code >= 300:
        logger.warning(
            "rollback-from-portal: dispatch returned %s: %s",
            resp.status_code, resp.text[:500],
        )
        raise DeployError(
            502,
            f"GitHub dispatch returned {resp.status_code}: {resp.text[:300]}",
        )

    record_change(
        entity_type="environment",
        entity_id=environment,
        action="ROLLBACK_TRIGGERED",
        actor_user_id=actor_user_id,
        after={
            "target_env": environment,
            "target_revision": target_revision,
            "workflow": ROLLBACK_WORKFLOW_FILE,
            "repo": github_repo(),
        },
    )
    return {
        "dispatched": True,
        "environment": environment,
        "target_revision": target_revision,
        "workflow": ROLLBACK_WORKFLOW_FILE,
        "repo": github_repo(),
    }
