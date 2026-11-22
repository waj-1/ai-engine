"""Surface the agent process's ACTUAL runtime environment.

The admin portal is mounted in the same process as the agent (see
main.py), so `os.environ` here IS what the agent reads. This module
collects the load-bearing runtime vars — which Supabase the agent is
bound to, which environment it resolves to, the Laminar + deploy config
— so the Environments screen can answer "did my dev config apply?" by
showing which DB the running agent actually talks to.

Secrets are never returned in full: keys/tokens collapse to a set/unset
flag; connection URLs show host-only so the operator can identify the
project without leaking embedded credentials.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse


def _host_only(url: Optional[str]) -> str:
    """Show scheme://host[:port] — drop any path / embedded credentials.
    A Supabase project URL's host is the project ref, which is not a
    secret; the service key (passed separately) is."""
    if not url:
        return ""
    try:
        p = urlparse(url)
        if p.scheme and p.netloc:
            # netloc may carry user:pass@host — strip the userinfo.
            host = p.netloc.rsplit("@", 1)[-1]
            return f"{p.scheme}://{host}"
    except Exception:
        pass
    return "(unparseable)"


def _flag(name: str) -> bool:
    return bool((os.getenv(name) or "").strip())


def _resolved_environment() -> str:
    raw = (os.getenv("ENVIRONMENT", "development") or "").lower()
    if raw in ("production", "prod"):
        return "prod"
    if raw in ("staging", "stage"):
        return "staging"
    return "staging"  # dev retired — default to the live staging env


def collect_runtime_env() -> Dict[str, Any]:
    """Return a masked, render-ready view of the process runtime env."""
    resolved = _resolved_environment()
    primary_url = _host_only(os.getenv("SUPABASE_URL"))

    # Cross-env promotion targets — only the ones actually configured.
    cross: List[Dict[str, Any]] = []
    for env_name, prefix in (("staging", "STAGING"), ("prod", "PROD")):
        # The real cross-env vars are the AI-prefixed ones (what
        # get_service_client_for_env reads / what the control-plane binds);
        # the un-prefixed names are accepted as a legacy fallback. Checking
        # only the legacy names made this card warn "not configured" on a
        # fully configured control-plane.
        url = os.getenv(f"{prefix}_AI_SUPABASE_URL") or os.getenv(f"{prefix}_SUPABASE_URL")
        if url:
            cross.append({
                "environment": env_name,
                "url": _host_only(url),
                "service_key_set": (
                    _flag(f"{prefix}_AI_SUPABASE_SERVICE_KEY")
                    or _flag(f"{prefix}_SUPABASE_SERVICE_KEY")
                ),
            })

    return {
        "resolved_environment": resolved,
        "raw_environment": os.getenv("ENVIRONMENT", "(unset → development)"),
        "supabase": {
            "url": primary_url or "(unset)",
            "service_key_set": _flag("SUPABASE_SERVICE_KEY"),
            "anon_key_set": _flag("SUPABASE_ANON_KEY"),
        },
        "cross_env_targets": cross,
        "observability": {
            "lmnr_base_url": _host_only(os.getenv("LMNR_BASE_URL")) or "(default api.lmnr.ai)",
            "lmnr_api_key_set": _flag("LMNR_PROJECT_API_KEY"),
            # The read side of every observability screen.
            "lmnr_clickhouse_url": _host_only(os.getenv("LMNR_CLICKHOUSE_URL")) or "(unset)",
        },
        "deploy": {
            "github_token_set": _flag("GITHUB_DEPLOY_TOKEN"),
            "repo_set": bool(_deploy_repo()),
        },
    }


def _deploy_repo() -> str:
    from admin.deploy_handlers import github_repo

    return github_repo()
