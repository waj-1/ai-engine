"""Portal-issued API keys: the key format, its digest and request authentication.

A key lets a program call the engine without a signed-in operator's token:

    Authorization: Bearer wae_<43 url-safe characters>

Where it is accepted, and with which scope:

    POST /v1/agents/{slug}/chat   `chat` or `admin`   (endpoints/agents_v1.py)
    /mcp (the admin MCP server)   `admin` only        (admin_mcp/auth.py)

Keys are created, listed and revoked in `admin/services/api_keys.py` (the
portal's Settings > API keys screen and the `api_keys` MCP tool call it). This
module is the part the request path needs, so the runtime does not import the
portal to check a key.

Security properties, each pinned by tests/core/test_api_keys.py:

- **Only a digest is stored.** A key is 32 bytes from `secrets` (256 bits), so
  a plain SHA-256 is enough: there is nothing to brute-force, and no server
  secret whose rotation would invalidate every key. The key itself exists in
  exactly one place after creation: wherever the person who created it put it.
- **Lookup is by digest**, an indexed equality on `api_keys.key_hash`, and the
  row that comes back is compared again with `hmac.compare_digest`. The
  database never sees the key, and no code path compares key text byte by byte.
- **One answer for every bad key.** Malformed, unknown, revoked and expired all
  get the same 401 with the same body, so a caller learns nothing about which
  prefixes exist or which keys once worked. A valid key that lacks the scope a
  route needs gets a 403: only the key's holder can see that.
- **Nothing here logs a key, a digest or a prefix.** Log lines name the key by
  its row id at most.
- **`last_used_at` never slows a request.** It is written from a background
  thread, at most once a minute per key and process.

The identity a key acts as is the key itself: `user_id` is the key's row id.
Chat sessions are scoped by `user_id` (core/chat_persistence.py), so two keys
cannot read or continue each other's conversations, and a session a key
started stays attributable to that key after the key is revoked.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Set, Tuple

from fastapi import HTTPException

logger = logging.getLogger(__name__)

TABLE = "api_keys"

#: Every key starts with this, so one is easy to recognise in a config file and
#: easy for a secret scanner to find in a commit.
KEY_PREFIX = "wae_"
_SECRET_BYTES = 32
#: Characters of the key kept for display (the marker plus 8 of the 43 random
#: characters: enough to tell keys apart, far too little to help guess one).
DISPLAY_PREFIX_LENGTH = len(KEY_PREFIX) + 8
_KEY_RE = re.compile(r"^wae_[A-Za-z0-9_-]{43}$")

SCOPE_CHAT = "chat"
SCOPE_ADMIN = "admin"
#: scope -> what it allows, in the order the portal lists them.
SCOPES: Dict[str, str] = {
    SCOPE_CHAT: "Talk to agents through the chat API.",
    SCOPE_ADMIN: "Everything the chat scope allows, plus full control of the engine over the MCP server.",
}

INVALID_KEY_DETAIL = "Invalid API key"
MISSING_SCOPE_DETAIL = "This API key does not have the scope this request needs"

#: Columns read back for a key. `key_hash` is deliberately not in the list the
#: management code uses (see admin/services/api_keys.py); it is selected here
#: only to be compared.
_AUTH_COLUMNS = "id, name, key_hash, scopes, expires_at, revoked_at"

#: Seconds between two `last_used_at` writes for the same key from this process.
LAST_USED_WRITE_INTERVAL = 60.0
_last_used_written: Dict[str, float] = {}
_last_used_lock = threading.Lock()
_background: Set["asyncio.Task[None]"] = set()


def looks_like_api_key(token: Optional[str]) -> bool:
    """Whether a bearer token should take the API-key path.

    Decided on the marker alone, so a damaged key is answered as a bad key and
    never handed to the token verifier (whose error would say something else).
    """
    return bool(token) and token.startswith(KEY_PREFIX)


def generate_key() -> str:
    """A new key: the marker plus 32 random bytes, url-safe."""
    return KEY_PREFIX + secrets.token_urlsafe(_SECRET_BYTES)


def hash_key(key: str) -> str:
    """The digest stored for a key and looked up on every request."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def display_prefix(key: str) -> str:
    return key[:DISPLAY_PREFIX_LENGTH]


def normalize_scopes(scopes: Iterable[str]) -> list:
    """Validate and order a scope list. Raises ValueError on an unknown or
    empty one, so a key can never be stored with a scope nothing checks."""
    wanted = {str(s).strip().lower() for s in (scopes or []) if str(s).strip()}
    unknown = sorted(wanted - set(SCOPES))
    if unknown:
        raise ValueError(f"unknown scope(s): {', '.join(unknown)} (use: {', '.join(SCOPES)})")
    if not wanted:
        raise ValueError(f"choose at least one scope ({', '.join(SCOPES)})")
    return [s for s in SCOPES if s in wanted]


def scope_allows(granted: Iterable[str], required: str) -> bool:
    """`admin` includes `chat`; nothing includes `admin`."""
    have = set(granted or [])
    if required == SCOPE_CHAT:
        return bool(have & {SCOPE_CHAT, SCOPE_ADMIN})
    return required in have


def parse_timestamp(value: Any) -> Optional[datetime]:
    """A timestamptz as PostgREST returns it, as an aware datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def key_status(row: Dict[str, Any], now: Optional[datetime] = None) -> str:
    """`revoked`, `expired` or `active`. An unreadable expiry counts as expired:
    a key whose end date cannot be checked is not let in."""
    if row.get("revoked_at"):
        return "revoked"
    if row.get("expires_at"):
        expires = parse_timestamp(row["expires_at"])
        if expires is None or expires <= (now or datetime.now(timezone.utc)):
            return "expired"
    return "active"


def _invalid() -> HTTPException:
    return HTTPException(status_code=401, detail=INVALID_KEY_DETAIL)


def _client():
    from core.auth import get_service_client

    return get_service_client()


def verify_key(token: str) -> Dict[str, Any]:
    """Resolve a presented key to its row, or raise 401.

    Synchronous (one supabase-py round trip): call it through
    `authenticate`, never inline in an async handler.
    """
    if not token or not _KEY_RE.match(token):
        raise _invalid()
    digest = hash_key(token)
    try:
        found = (_client().table(TABLE).select(_AUTH_COLUMNS)
                 .eq("key_hash", digest).limit(1).execute().data) or []
    except Exception as exc:  # noqa: BLE001
        # The exception's class only: a client error can carry the request URL,
        # and the URL of this request carries the digest.
        logger.error("api key check: lookup failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Could not check the API key. Try again.") from None
    row = found[0] if found else None
    stored = str((row or {}).get("key_hash") or "")
    # Compared in constant time even though the database matched it already:
    # the decision to let a caller in must not rest on a filter alone.
    if not row or not hmac.compare_digest(stored.encode(), digest.encode()):
        raise _invalid()
    if key_status(row) != "active":
        raise _invalid()
    return row


def caller_context(row: Dict[str, Any], token: str) -> Dict[str, Any]:
    """The caller dict a route receives: the same shape `resolve_superuser`
    returns, plus what tells a key apart from a person."""
    key_id = str(row["id"])
    return {
        "user_token": token,
        # The key IS the identity: sessions, memory and audit rows it produces
        # are scoped to its row id (see the module docstring).
        "user_id": key_id,
        "email": "",
        "auth": "api_key",
        "api_key_id": key_id,
        "api_key_name": row.get("name") or "",
        "scopes": list(row.get("scopes") or []),
    }


def touch_last_used(key_id: str) -> None:
    """Write `last_used_at = now()` for one key. Best effort, never raises."""
    try:
        (_client().table(TABLE)
         .update({"last_used_at": datetime.now(timezone.utc).isoformat()})
         .eq("id", key_id).execute())
    except Exception as exc:  # noqa: BLE001
        logger.warning("api key %s: last_used_at not recorded (%s)", key_id, type(exc).__name__)


def _due_for_touch(key_id: str) -> bool:
    now = time.monotonic()
    with _last_used_lock:
        last = _last_used_written.get(key_id)
        if last is not None and now - last < LAST_USED_WRITE_INTERVAL:
            return False
        _last_used_written[key_id] = now
        return True


def reset_last_used_throttle() -> None:
    """Forget which keys were written recently (tests; safe any time)."""
    with _last_used_lock:
        _last_used_written.clear()


def record_use(key_id: str) -> Optional["asyncio.Task[None]"]:
    """Schedule the `last_used_at` write off the request path, throttled.

    Returns the background task when one was started (so a test can await it),
    None when this key was written recently.
    """
    if not _due_for_touch(key_id):
        return None
    task = asyncio.get_running_loop().create_task(asyncio.to_thread(touch_last_used, key_id))
    # The loop keeps only a weak reference to a task: hold it until it is done.
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


async def authenticate(token: str, required_scope: str) -> Dict[str, Any]:
    """Authenticate a presented API key for a route that needs `required_scope`.

    Returns the caller context (`caller_context`). Raises HTTPException 401 for
    any key that is not a live one, 403 for a live key without the scope, 503
    when the key store cannot be reached.
    """
    row = await asyncio.to_thread(verify_key, token)
    if not scope_allows(row.get("scopes") or [], required_scope):
        raise HTTPException(status_code=403, detail=MISSING_SCOPE_DETAIL)
    record_use(str(row["id"]))
    return caller_context(row, token)


def split_bearer(authorization: Optional[str]) -> Tuple[bool, str]:
    """(is a bearer header, the token) for an Authorization header value."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return False, ""
    return True, authorization.split(" ", 1)[1].strip()
