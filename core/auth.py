"""
Authentication and authorization module for AI Agent.
Handles JWT validation and creates token-aware Supabase clients for RLS enforcement.
"""

import os
import threading
import time
import jwt
from contextvars import ContextVar
from typing import Optional, Dict, Any
from fastapi import Header, HTTPException
from supabase import create_client, Client, ClientOptions
import logging

logger = logging.getLogger(__name__)


class TokenValidationError(Exception):
    """Raised when JWT token validation fails"""
    pass


def validate_token(token: str) -> Dict[str, Any]:
    """
    Validate JWT token and extract user information.
    
    Args:
        token: JWT token string
        
    Returns:
        Dict containing decoded token payload
        
    Raises:
        TokenValidationError: If token is invalid or expired
    """
    try:
        # Decode without verification (Supabase will verify on API calls)
        # We just need to extract user_id and other claims
        decoded = jwt.decode(
            token,
            options={"verify_signature": False}  # Supabase validates on its end
        )
        
        # Basic validation
        if 'sub' not in decoded:
            raise TokenValidationError("Token missing 'sub' claim")
        
        logger.info(f"Token validated for user: {decoded.get('sub')}")
        return decoded
        
    except jwt.ExpiredSignatureError:
        raise TokenValidationError("Token has expired")
    except jwt.InvalidTokenError as e:
        raise TokenValidationError(f"Invalid token: {str(e)}")
    except Exception as e:
        raise TokenValidationError(f"Token validation error: {str(e)}")


# ---------------------------------------------------------------------------
# Signing-key resolution for validate_token_verified
# ---------------------------------------------------------------------------
# The main project signs user JWTs one of two ways, and the engine must accept
# both:
#   - ES256 (asymmetric signing keys): what newer Supabase CLI stacks issue by
#     default, and what hosted projects issue once migrated. Checked against
#     the project's PUBLIC keys, published at <SUPABASE_URL>/auth/v1/.well-known/jwks.json.
#   - HS256 (legacy shared secret): hosted prod today, the self-hosted demo stacks
#     (deploy/local among them), scripts/mint_mcp_token.py and the test fixtures. Checked
#     against SUPABASE_JWT_SECRET.
# The token's header names its algorithm, but the header is attacker-written:
# it only picks WHICH configured key to try, from an allow-list, and each
# branch hands jwt.decode exactly one algorithm. Never pass both algorithms to
# one decode — that is how an HS256 token "signed" with the public key would
# verify (algorithm confusion).
_SUPPORTED_ALGORITHMS = ("ES256", "HS256")
_JWKS_TIMEOUT_SEC = 3
# Keys are kept fresh by a BACKGROUND refresher (`refresh_jwks_cache`, run by
# main.lifespan every `JWKS_REFRESH_INTERVAL_SEC`), so the request path reads
# a warm cache and never waits on a fetch in steady state. It only fetches
# itself when the cache is cold (before the first refresh lands), when a
# token names a `kid` the cache doesn't hold (a key rotation), or when the
# keys are older than `_JWKS_MAX_AGE_SEC` because the refresher stopped —
# so a revoked key can't stay trusted forever just because refreshing died.
JWKS_REFRESH_INTERVAL_SEC = 600
_JWKS_MAX_AGE_SEC = 3600
# An unknown `kid` forces a JWKS refetch (that is how a key rotation is picked
# up). Bound it, or a stream of tokens with random kids turns every request
# into an outbound fetch.
_JWKS_FORCED_REFRESH_MIN_SEC = 60
# After a failed fetch, requests needing one (cold or too-old cache) fail
# closed straight away until this passes, instead of each retrying the
# `_JWKS_TIMEOUT_SEC` fetch: during a JWKS outage a stream of ES256 tokens
# would otherwise tie up one worker thread per request for the whole timeout.
_JWKS_FAILED_FETCH_RETRY_SEC = 30

# url -> {"keys": {kid: PyJWK}, "fetched_at": monotonic seconds}. Our own cache
# rather than PyJWKClient's: PyJWKClient CLEARS its cached set when a fetch
# fails, so one network blip during a refresh would throw away good keys. Here
# a failed refresh keeps the last good set.
_jwks_state: Dict[str, Dict[str, Any]] = {}
_jwks_last_forced_refresh: Dict[str, float] = {}
_jwks_last_failed_fetch: Dict[str, float] = {}
# `_jwks_lock` guards the dicts above and is only held for a lookup/store.
# `_jwks_fetch_lock` lets one fetch run at a time (background refresher and
# request-triggered fetches alike), so an older fetch can never finish after
# a newer one and overwrite its keys. A request with a warm cache never
# touches it, so it never waits behind a slow fetch.
_jwks_lock = threading.Lock()
_jwks_fetch_lock = threading.Lock()


class AuthConfigError(TokenValidationError):
    """Verified auth is misconfigured; every verified request fails closed."""


def _allowed_algorithms() -> tuple[str, ...]:
    """Algorithms accepted for verified auth (`SUPABASE_JWT_ALGORITHMS`,
    default both). Lets a deploy drop HS256 once its legacy secret is revoked.

    A set value is taken literally or not at all: any entry outside
    `_SUPPORTED_ALGORITHMS` (a typo like "ES265", or "RS256") raises
    `AuthConfigError`, so verified auth fails closed. Dropping the bad entry,
    or falling back to the defaults, would re-enable exactly the algorithm the
    operator was trying to turn off."""
    raw = os.getenv("SUPABASE_JWT_ALGORITHMS")
    if raw is None or not raw.strip():
        return _SUPPORTED_ALGORITHMS
    entries = [p.strip().upper() for p in raw.split(",") if p.strip()]
    unsupported = [e for e in entries if e not in _SUPPORTED_ALGORITHMS]
    if unsupported:
        raise AuthConfigError(
            f"SUPABASE_JWT_ALGORITHMS has unsupported value(s) {unsupported}; "
            f"allowed: {list(_SUPPORTED_ALGORITHMS)}. Refusing all verified auth "
            "until it is fixed."
        )
    return tuple(dict.fromkeys(entries))


def _jwks_url() -> Optional[str]:
    explicit = os.getenv("SUPABASE_JWKS_URL")
    if explicit:
        return explicit
    base = os.getenv("SUPABASE_URL")
    return f"{base.rstrip('/')}/auth/v1/.well-known/jwks.json" if base else None


def _fetch_jwks(url: str) -> Dict[str, Any]:
    """kid -> PyJWK for the ES256 signing keys at `url`. Blocking; raises
    TokenValidationError when unreachable or unparseable."""
    import json
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=_JWKS_TIMEOUT_SEC) as resp:
            data = json.load(resp)
        keyset = jwt.PyJWKSet.from_dict(data)
    except (urllib.error.URLError, TimeoutError, ValueError, jwt.PyJWTError) as e:
        raise TokenValidationError(f"Could not load signing keys: {e}")
    return {k.key_id: k for k in keyset.keys if k.key_id}


def _cached_jwks(url: str) -> Optional[Dict[str, Any]]:
    with _jwks_lock:
        return _jwks_state.get(url)


def _is_stale(state: Optional[Dict[str, Any]]) -> bool:
    return state is None or time.monotonic() - state["fetched_at"] > _JWKS_MAX_AGE_SEC


def _refetch_jwks(url: str) -> Dict[str, Any]:
    """Fetch the JWKS and store it; return the new keys. Caller must hold
    `_jwks_fetch_lock`. A failure is recorded (for
    `_JWKS_FAILED_FETCH_RETRY_SEC`) and re-raised; the cached keys are kept."""
    try:
        keys = _fetch_jwks(url)
    except TokenValidationError:
        with _jwks_lock:
            _jwks_last_failed_fetch[url] = time.monotonic()
        raise
    with _jwks_lock:
        _jwks_state[url] = {"keys": keys, "fetched_at": time.monotonic()}
        _jwks_last_failed_fetch.pop(url, None)
    return keys


def _reset_jwks_cache() -> None:
    """Drop cached keys and refetch timestamps (tests; safe any time)."""
    with _jwks_lock:
        _jwks_state.clear()
        _jwks_last_forced_refresh.clear()
        _jwks_last_failed_fetch.clear()


def _es256_key(kid: Optional[str]) -> Any:
    """The main project's public key for `kid`, from the cached JWKS.

    Fetches only when the cache is cold, too old, or lacks `kid` (rate-limited)
    — see `JWKS_REFRESH_INTERVAL_SEC`. Callers in async code run this whole
    check off the event loop, so even those fetches never block other requests.
    """
    url = _jwks_url()
    if not url:
        raise TokenValidationError(
            "Token is ES256-signed but no JWKS is configured — set SUPABASE_URL "
            "(or SUPABASE_JWKS_URL) to the main Supabase project."
        )
    if not kid:
        raise TokenValidationError("ES256 token is missing its 'kid' header")
    state = _cached_jwks(url)
    if _is_stale(state):
        with _jwks_fetch_lock:
            # Re-check: the request we queued behind may have just fetched.
            state = _cached_jwks(url)
            if _is_stale(state):
                with _jwks_lock:
                    failed_at = _jwks_last_failed_fetch.get(url)
                if failed_at is not None and time.monotonic() - failed_at < _JWKS_FAILED_FETCH_RETRY_SEC:
                    raise TokenValidationError(
                        "Signing keys are unavailable (a recent JWKS fetch failed)"
                    )
                # Unreachable / unparseable JWKS here raises: fail closed, never fall back.
                _refetch_jwks(url)
                state = _cached_jwks(url)
    match = state["keys"].get(kid)
    if match is None:
        now = time.monotonic()
        with _jwks_lock:
            last = _jwks_last_forced_refresh.get(url)
            may_refresh = last is None or now - last >= _JWKS_FORCED_REFRESH_MIN_SEC
            if may_refresh:
                _jwks_last_forced_refresh[url] = now
        if may_refresh:  # only then wait for the fetch lock
            with _jwks_fetch_lock:
                match = _refetch_jwks(url).get(kid)
    if match is None:
        raise TokenValidationError("Token signed with an unknown key (kid)")
    if match.algorithm_name != "ES256":
        raise TokenValidationError("Token 'kid' does not name an ES256 key")
    return match.key


def refresh_jwks_cache() -> bool:
    """Fetch the JWKS and replace the cached keys; True when it did.

    Blocking — call it off the event loop (main.lifespan runs it at boot and
    then every `JWKS_REFRESH_INTERVAL_SEC`). Never raises: on failure the last
    good keys stay cached and the next refresh tries again.
    """
    try:
        algorithms = _allowed_algorithms()
    except AuthConfigError as e:
        logger.error("JWKS refresh skipped: %s", e)
        return False
    url = _jwks_url()
    if not url or "ES256" not in algorithms:
        return False
    try:
        with _jwks_fetch_lock:
            _refetch_jwks(url)
        return True
    except Exception as e:
        logger.warning("JWKS refresh failed (%s): %s — keeping the last good keys", url, e)
        return False


# Kept for callers of the first version of this module.
warm_jwks_cache = refresh_jwks_cache


def validate_token_verified(token: str, audience: str = "authenticated") -> Dict[str, Any]:
    """Signature-VERIFIED JWT decode for admin-portal and MCP auth.

    Unlike `validate_token` (which trusts the payload and lets Supabase
    enforce auth on data calls), this path is the trust boundary itself:
    the admin/MCP surface acts with service-role credentials, so a forged
    `sub` here means superuser bypass.

    Accepts ES256 (checked against the main project's JWKS) and HS256
    (checked against SUPABASE_JWT_SECRET) — see the block comment above.
    Fails closed: an algorithm outside the allow-list, a missing key source,
    or an unreachable JWKS is a rejection, never an unverified decode.

    `audience` is "authenticated" for every first-party session; the WAJ MCP
    server passes "waj-mcp", the audience Supabase's access-token hook gives
    OAuth-client tokens (public_mcp/auth.py) — which is also why those tokens
    fail every other verified endpoint here.

    Blocking when it has to fetch the JWKS (cold cache, rotated key, or keys
    older than `_JWKS_MAX_AGE_SEC`; each bounded by `_JWKS_TIMEOUT_SEC`); a warm
    cache is pure CPU. async callers run it via `asyncio.to_thread`, and
    `main.lifespan` keeps the cache warm in the background.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as e:
        raise TokenValidationError(f"Invalid token: {str(e)}")
    alg = header.get("alg")
    if alg not in _allowed_algorithms():
        raise TokenValidationError(f"Invalid token: algorithm {alg!r} is not allowed")

    if alg == "ES256":
        key = _es256_key(header.get("kid"))
    else:
        key = os.getenv("SUPABASE_JWT_SECRET")
        if not key:
            raise TokenValidationError(
                "SUPABASE_JWT_SECRET is not configured — refusing unverified "
                "admin authentication. Set it to the main Supabase project's "
                "JWT secret."
            )
    try:
        decoded = jwt.decode(
            token,
            key,
            algorithms=[alg],
            audience=audience,
            options={"require": ["exp", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise TokenValidationError("Token has expired")
    except jwt.InvalidTokenError as e:
        raise TokenValidationError(f"Invalid token: {str(e)}")
    except Exception as e:
        raise TokenValidationError(f"Token validation error: {str(e)}")
    if "sub" not in decoded:
        raise TokenValidationError("Token missing 'sub' claim")
    return decoded


class SignInError(Exception):
    """Raised when email/password sign-in fails (bad credentials, auth
    service unreachable, or no session returned)."""


def sign_in_with_password(email: str, password: str) -> Dict[str, str]:
    """Exchange an email + password for a Supabase session via GoTrue.

    Uses the anon-key client (the only key allowed to call the auth
    endpoints). Returns ``{access_token, refresh_token}`` on success.
    The access_token is the same JWT the admin portal stores in its
    cookie and validates through `validate_token` + the superuser check
    — so this is purely a different way to OBTAIN the token, not a new
    trust path.

    Raises SignInError on bad credentials or any auth failure, with a
    message safe to surface to the operator (no internal detail leak).
    """
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_anon_key = os.getenv("SUPABASE_ANON_KEY")
    if not supabase_url or not supabase_anon_key:
        raise SignInError("Auth is not configured (missing SUPABASE_URL / SUPABASE_ANON_KEY).")

    # A fresh stateless client — no session persistence, no shared header.
    client = create_client(
        supabase_url,
        supabase_anon_key,
        options=ClientOptions(persist_session=False),
    )
    try:
        result = client.auth.sign_in_with_password({"email": email, "password": password})
    except Exception as exc:  # GoTrue raises AuthApiError for bad creds
        logger.info("admin sign-in failed for %s: %s", email, exc)
        raise SignInError("Invalid email or password.") from exc

    session = getattr(result, "session", None)
    access_token = getattr(session, "access_token", None) if session else None
    if not access_token:
        raise SignInError("Sign-in returned no session. Check the account exists and is confirmed.")
    refresh_token = getattr(session, "refresh_token", "") or ""
    return {"access_token": access_token, "refresh_token": refresh_token}


def get_supabase_client(user_token: str) -> Client:
    """
    Create Supabase client with user's JWT token for RLS enforcement.
    
    This is the PRIMARY method for database access. It ensures all operations
    respect Row Level Security policies by using the user's token.
    
    Args:
        user_token: User's JWT authentication token
        
    Returns:
        Configured Supabase client with user context
        
    Raises:
        ValueError: If required environment variables are missing
    """
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_anon_key = os.getenv("SUPABASE_ANON_KEY")
    
    if not supabase_url or not supabase_anon_key:
        raise ValueError("Missing SUPABASE_URL or SUPABASE_ANON_KEY environment variables")
    
    # Create client with user's token in Authorization header
    # This ensures RLS policies are enforced
    # persist_session=False avoids storage requirement for stateless API
    client = create_client(
        supabase_url,
        supabase_anon_key,
        options=ClientOptions(
            headers={"Authorization": f"Bearer {user_token}"},
            persist_session=False
        )
    )
    
    logger.debug(f"Created Supabase client with user token")
    return client


def get_service_client() -> Client:
    """Service-role client for the AI project (BYPASSES RLS).

    Targets the standalone AI Supabase project (AI_SUPABASE_*). Default
    `public` schema — the agent's tables now live there, not in `wajagents`.
    Use only for system/admin operations; the app enforces business scoping.
    """
    supabase_url = os.getenv("AI_SUPABASE_URL")
    service_key = os.getenv("AI_SUPABASE_SERVICE_KEY")

    if not supabase_url or not service_key:
        raise ValueError("Missing AI_SUPABASE_URL or AI_SUPABASE_SERVICE_KEY environment variables")

    client = create_client(
        supabase_url, service_key,
        options=ClientOptions(persist_session=False),
    )

    logger.warning("Created AI-project Supabase client with SERVICE ROLE (bypasses RLS)")
    return client


def get_main_client() -> Client:
    """Service-role client for the MAIN project (auth + business).

    Used only where the AI engine must touch the main project directly —
    today that is the admin portal's superuser check against public.user.
    Reads SUPABASE_URL / SUPABASE_SERVICE_KEY. Default `public` schema.
    """
    supabase_url = os.getenv("SUPABASE_URL")
    service_key = os.getenv("SUPABASE_SERVICE_KEY")

    if not supabase_url or not service_key:
        raise ValueError("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY environment variables")

    return create_client(
        supabase_url, service_key,
        options=ClientOptions(persist_session=False),
    )


class CrossEnvNotConfiguredError(RuntimeError):
    """Raised when a cross-environment Supabase client is requested but
    the matching `<ENV>_AI_SUPABASE_URL` / `<ENV>_AI_SUPABASE_SERVICE_KEY`
    env vars are not configured.

    Distinct from `ValueError` so the promote handler can surface a
    targeted UI message ("set the STAGING_SUPABASE_URL env var") instead
    of the generic 5xx the regular get_service_client raises.
    """


def get_service_client_for_env(environment: str) -> Client:
    """Return a service-role Supabase client for `environment`.

    Used by the cross-environment promote flow so the dev portal can
    write a profile row + version snapshot directly into the staging
    or production Supabase project. The runtime in each environment
    still talks to its own local DB via `get_service_client()` — this
    helper exists solely for the dev → (staging|prod) write path.

    Configuration (each env independently):
      - dev:     AI_SUPABASE_URL / AI_SUPABASE_SERVICE_KEY (current env)
      - staging: STAGING_AI_SUPABASE_URL / STAGING_AI_SUPABASE_SERVICE_KEY
      - prod:    PROD_AI_SUPABASE_URL / PROD_AI_SUPABASE_SERVICE_KEY

    If the env vars for a non-current target aren't set, raises
    `CrossEnvNotConfiguredError`. The promote handler converts that
    into a 400 with a hint instead of a 5xx.

    If `environment` is the same as the runtime's own ENVIRONMENT
    (e.g. promoting "dev → dev" or running this from within staging),
    falls through to `get_service_client()` so the call is a no-op
    local write.
    """
    env_norm = environment.lower()
    if env_norm in ("development", "dev"):
        env_norm = "dev"
    elif env_norm in ("production", "prod"):
        env_norm = "prod"

    current = (os.getenv("ENVIRONMENT") or "dev").lower()
    if current in ("development", "dev"):
        current = "dev"
    elif current in ("production", "prod"):
        current = "prod"

    if env_norm == current:
        return get_service_client()

    prefix = env_norm.upper()
    url = os.getenv(f"{prefix}_AI_SUPABASE_URL")
    key = (
        os.getenv(f"{prefix}_AI_SUPABASE_SERVICE_KEY")
        or os.getenv(f"{prefix}_AI_SUPABASE_SERVICE_ROLE_KEY")
    )
    if not url or not key:
        raise CrossEnvNotConfiguredError(
            f"Cross-environment AI Supabase client for '{env_norm}' is not "
            f"configured. Set {prefix}_AI_SUPABASE_URL and "
            f"{prefix}_AI_SUPABASE_SERVICE_KEY so promotes can write to the "
            f"remote AI project."
        )

    client = create_client(
        url, key,
        options=ClientOptions(persist_session=False),
    )
    logger.warning(
        "Created cross-env AI Supabase client (env=%s, url=%s) — BYPASSES RLS on the remote project",
        env_norm, url,
    )
    return client


# ---------------------------------------------------------------------------
# Request-scoped admin environment (the /admin env switcher)
# ---------------------------------------------------------------------------
# The admin portal is ONE control-plane that can read/write BOTH the staging
# and prod AI-Supabase branches via the `?env=` switcher. Rather than thread
# `env` through every context-builder signature, a middleware
# (`main.persist_admin_env`) resolves the selected env once per request and
# parks it here; env-scoped admin handlers call `admin_service_client()` to
# get the client for the *selected* branch. Mirrors the `_PROMPT_OVERRIDE` /
# `_MODEL_OVERRIDE` ContextVar pattern. Background jobs (eval/e2e/KB-ingest
# runners) run on daemon threads that do NOT inherit this ContextVar, so the
# job spawn captures the env and re-sets it inside the thread.
_ADMIN_ENV: ContextVar[Optional[str]] = ContextVar("_ADMIN_ENV", default=None)


def set_admin_env(env: Optional[str]) -> Any:
    """Pin the admin env for the current request/thread scope. Returns the
    reset token (pass to `reset_admin_env`)."""
    return _ADMIN_ENV.set(env if env in ("staging", "prod") else None)


def reset_admin_env(token: Any) -> None:
    try:
        _ADMIN_ENV.reset(token)
    except Exception:
        _ADMIN_ENV.set(None)


def get_admin_env() -> Optional[str]:
    """The admin env selected for this request scope, or None (→ local branch)."""
    return _ADMIN_ENV.get()


def admin_service_client() -> Client:
    """Service client for the admin-selected env branch.

    Reads the request-scoped `_ADMIN_ENV`; when set to a cross-env value the
    control-plane serves that branch, otherwise (or when cross-env vars are
    unconfigured on a per-env service) it safely falls back to the local
    branch. This is the env-aware replacement for `get_service_client()` in
    every env-scoped admin read/write handler.
    """
    env = _ADMIN_ENV.get()
    if not env:
        return get_service_client()
    try:
        return get_service_client_for_env(env)
    except CrossEnvNotConfiguredError:
        return get_service_client()


def get_user_id_from_token(token: str) -> str:
    """
    Extract user ID from JWT token.
    
    Args:
        token: JWT token string
        
    Returns:
        User ID (UUID) from token's 'sub' claim
        
    Raises:
        TokenValidationError: If token is invalid or missing user ID
    """
    decoded = validate_token(token)
    user_id = decoded.get('sub')
    
    if not user_id:
        raise TokenValidationError("Token missing user ID")
    
    return user_id


def require_bearer(authorization: Optional[str]) -> str:
    """Pull the raw token out of an Authorization header, or 401."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header. Expected format: Bearer <token>",
        )
    return authorization[len("Bearer "):]


def authenticate(authorization: Optional[str]) -> tuple[str, str]:
    """Validate Bearer token and return (raw_token, user_id)."""
    user_token = require_bearer(authorization)
    try:
        user_data = validate_token(user_token)
    except TokenValidationError as e:
        logger.warning(f"Token validation failed: {e}")
        raise HTTPException(status_code=401, detail=str(e))
    return user_token, user_data.get("sub", "")


def authenticate_verified(authorization: Optional[str]) -> tuple[str, str]:
    """Like `authenticate()` but SIGNATURE-VERIFIES the JWT (ES256 against the
    main project's JWKS, or HS256 against SUPABASE_JWT_SECRET) before trusting
    its claims. Returns (raw_token, user_id).

    Required on any path where the derived user_id scopes a SERVICE-ROLE
    (RLS-bypassing) query — e.g. the `/ai/sessions` chat-history endpoints,
    which read/mutate `ai_chat_*` rows via `get_service_client()` filtered only
    by `user_id`. On those paths the unverified `validate_token()` would let a
    forged `sub` read, rename, or delete another user's transcripts, because the
    service client never re-checks the token. Normal RLS-scoped paths
    (`get_supabase_client(token)`) may keep using `authenticate()`, since
    Supabase verifies the token itself on the data call.

    Fails closed (401) when the token's key source is unconfigured — see
    `validate_token_verified`.
    """
    user_token = require_bearer(authorization)
    try:
        user_data = validate_token_verified(user_token)
    except TokenValidationError as e:
        logger.warning(f"Verified token validation failed: {e}")
        raise HTTPException(status_code=401, detail=str(e))
    return user_token, user_data.get("sub", "")


def authenticate_optional(authorization: str | None) -> tuple[str | None, str]:
    """Like authenticate() but never raises: anonymous callers get (None, "")."""
    if not authorization:
        return None, ""
    try:
        return authenticate(authorization)
    except Exception:
        return None, ""


def authenticate_dep(authorization: str = Header(None)) -> tuple[str, str]:
    """FastAPI `Depends(...)`-friendly wrapper around `authenticate`."""
    return authenticate(authorization)


def get_user_metadata_from_token(token: str) -> Dict[str, Any]:
    """
    Extract user metadata from JWT token.
    
    Args:
        token: JWT token string
        
    Returns:
        Dict containing user metadata (email, role, etc.)
    """
    decoded = validate_token(token)
    
    return {
        'user_id': decoded.get('sub'),
        'email': decoded.get('email'),
        'role': decoded.get('role'),
        'user_metadata': decoded.get('user_metadata', {}),
        'app_metadata': decoded.get('app_metadata', {}),
    }

