"""
Superuser-only gate for the admin portal.

The portal mounts under /admin/*. Every endpoint depends on
`require_superuser`, which:
 1. Reads the cookie (set by /admin/login) or the Authorization header.
 2. Validates the Supabase JWT via core.auth, with signature verification.
 3. Checks the user row's permission->>'is_superuser' flag.

The token-and-permission check itself lives in `resolve_superuser`, a
token-only function shared with the MCP server's auth middleware so both
entry points enforce the exact same trust boundary.

Failure modes return a 403 with a stable JSON body so the HTMX frontend
can swap in an error message instead of redirecting mid-form.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Dict, Optional, Tuple

from fastapi import Cookie, Depends, Header, HTTPException, Request

from core.auth import (
    TokenValidationError,
    get_main_client,
    require_bearer,
    validate_token_verified,
)

logger = logging.getLogger(__name__)

ADMIN_TOKEN_COOKIE = "waj_admin_token"

# --- superuser-lookup cache -------------------------------------------------
# resolve_superuser runs on EVERY admin-portal request and EVERY MCP message
# (initialize, tools/list, every tools/call), and each call otherwise makes a
# fresh main-Supabase `user` round-trip. An agent driving the MCP fires dozens
# of calls per session, so that round-trip dominates latency. We cache only
# the DB lookup — keyed by the token's `sub` (auth id) — for a short TTL.
#
# The JWT itself is re-verified (signature + `exp`) on every call below, so a
# forged or expired token is never accepted from cache; only the "is this
# user a superuser" fact is memoized. Revoking superuser therefore takes
# effect within ADMIN_AUTH_CACHE_TTL_SECONDS (default 60; set 0 to disable).
# Successes only are cached — a 403/404 is re-checked every time.
_SUPERUSER_CACHE: Dict[str, Tuple[float, Dict[str, str]]] = {}
_SUPERUSER_CACHE_LOCK = threading.Lock()


def _cache_ttl() -> float:
    """Read the TTL at call time so ops/tests can tune it without a reimport."""
    try:
        return float(os.getenv("ADMIN_AUTH_CACHE_TTL_SECONDS", "60"))
    except (TypeError, ValueError):
        return 60.0


def reset_superuser_cache() -> None:
    """Drop all cached superuser lookups (used by tests; safe any time)."""
    with _SUPERUSER_CACHE_LOCK:
        _SUPERUSER_CACHE.clear()


def _extract_token(
    authorization: Optional[str],
    cookie_token: Optional[str],
) -> str:
    """Return the raw JWT. Prefers Authorization header over cookie."""
    if authorization and authorization.startswith("Bearer "):
        return require_bearer(authorization)
    if cookie_token:
        return cookie_token
    raise HTTPException(
        status_code=401,
        detail="Admin portal requires authentication. Sign in at /admin/login.",
    )


def resolve_superuser(token: str) -> dict:
    """Validate a JWT (signature-verified) and require a superuser row.

    Shared trust boundary for the admin portal (via require_superuser)
    and the MCP server (via admin_mcp.auth). Returns
    {user_token, user_id, email}; raises HTTPException 401/403/500.
    """
    try:
        decoded = validate_token_verified(token)
    except TokenValidationError as exc:
        raise HTTPException(status_code=401, detail=str(exc))

    auth_id = decoded.get("sub")
    if not auth_id:
        raise HTTPException(status_code=401, detail="Token missing sub claim")

    # Cache hit: token already verified above (signature + exp), so it's safe
    # to reuse the memoized superuser fact. `user_token` is always the CURRENT
    # request's token, never a cached one.
    ttl = _cache_ttl()
    if ttl > 0:
        now = time.monotonic()
        with _SUPERUSER_CACHE_LOCK:
            hit = _SUPERUSER_CACHE.get(auth_id)
        if hit and hit[0] > now:
            resolved = hit[1]
            return {
                "user_token": token,
                "user_id": resolved["user_id"],
                "email": resolved["email"],
            }

    try:
        client = get_main_client()
        rows = (
            client.table("user")
            .select("id, permission, email")
            .eq("auth_id", auth_id)
            .limit(1)
            .execute()
            .data
        )
    except Exception as exc:
        logger.error("admin auth: user lookup failed: %s", exc)
        raise HTTPException(status_code=500, detail="auth check failed") from exc

    if not rows:
        raise HTTPException(status_code=403, detail="No user record found")
    user_row = rows[0]
    perm = user_row.get("permission") or {}
    if not perm.get("is_superuser"):
        raise HTTPException(
            status_code=403,
            detail="Admin portal is restricted to superusers.",
        )

    resolved = {"user_id": user_row["id"], "email": user_row.get("email", "")}
    if ttl > 0:
        with _SUPERUSER_CACHE_LOCK:
            _SUPERUSER_CACHE[auth_id] = (time.monotonic() + ttl, resolved)
    return {"user_token": token, **resolved}


def require_superuser(
    request: Request,
    authorization: Optional[str] = Header(None),
    waj_admin_token: Optional[str] = Cookie(None),
) -> dict:
    """FastAPI dependency that returns the authenticated superuser context.

    Returns a small dict `{user_token, user_id, email}` for handlers to use
    when recording audit entries.
    """
    token = _extract_token(authorization, waj_admin_token)
    ctx = resolve_superuser(token)
    request.state.admin_user_id = ctx["user_id"]
    request.state.admin_email = ctx["email"]
    request.state.admin_token = token
    return ctx
