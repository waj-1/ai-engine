"""Create, list and revoke portal-issued API keys.

One service for every caller that manages keys: the Settings > API keys screen
(`admin/api_keys_handlers.py`), the `api_keys` MCP tool
(`admin_mcp/tools_govern.py`) and any setup flow that needs to issue a first
key. Checking a key on a request is `core/api_keys.py`.

    row, key = create_key("Billing sync", ["chat"], created_by=user_id)

`key` is the only copy of the key that will ever exist: only its SHA-256
digest and a short display prefix are stored. Show it once and drop it.

Keys belong to the engine instance, not to an environment branch: they are
always read and written through the engine's own database
(`core.auth.get_service_client`), which is where a request's key is checked.
The portal's environment switcher does not apply to them.

Every write is recorded in the audit log by this module (so no caller can
forget to), with the key's name, prefix and scopes, and never the key or its
digest.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from admin.audit import record_change
from core import api_keys as keys

logger = logging.getLogger(__name__)

AUDIT_ENTITY = "api_keys"

#: Everything a list or a detail may show. `key_hash` is never selected by
#: this module, so it cannot reach a template, an MCP response or a log.
PUBLIC_COLUMNS = ("id, name, key_prefix, scopes, created_by, created_at, "
                  "last_used_at, expires_at, revoked_at, revoked_by")

NAME_MAX_LENGTH = 120
#: Expiry choices the portal offers: form value -> (label, days or None).
EXPIRY_CHOICES: Dict[str, Tuple[str, Optional[int]]] = {
    "": ("Never", None),
    "30": ("30 days", 30),
    "90": ("90 days", 90),
    "365": ("1 year", 365),
}
MAX_EXPIRY_DAYS = 3650


def _client():
    from core.auth import get_service_client

    return get_service_client()


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    """A row as it may leave this module: no digest, plus its `status`."""
    out = {k: v for k, v in row.items() if k != "key_hash"}
    out["scopes"] = list(out.get("scopes") or [])
    out["status"] = keys.key_status(out)
    return out


def _audit_view(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: row.get(k) for k in ("name", "key_prefix", "scopes", "expires_at", "revoked_at")}


def clean_name(name: str) -> str:
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise ValueError("give the key a name, so you can tell it apart later")
    if len(cleaned) > NAME_MAX_LENGTH:
        raise ValueError(f"the name can be at most {NAME_MAX_LENGTH} characters")
    return cleaned


def expiry_from_days(days: Optional[int]) -> Optional[datetime]:
    """An expiry `days` from now, or None for a key that does not expire."""
    if days in (None, "", 0):
        return None
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise ValueError("expires_in_days must be a whole number of days") from None
    if not 1 <= days <= MAX_EXPIRY_DAYS:
        raise ValueError(f"expires_in_days must be between 1 and {MAX_EXPIRY_DAYS}")
    return datetime.now(timezone.utc) + timedelta(days=days)


def create_key(name: str, scopes: Iterable[str], created_by: Optional[str] = None, *,
               expires_at: Optional[datetime] = None,
               client: Any = None) -> Tuple[Dict[str, Any], str]:
    """Create a key. Returns `(row, key)`; `key` is shown once and not stored.

    `scopes`: `chat` (the chat API) and/or `admin` (the chat API plus the MCP
    server). `created_by`: the id of the person or key creating it, recorded on
    the row and as the audit actor. Raises ValueError on a bad name, scope or
    an expiry in the past.
    """
    cleaned = clean_name(name)
    wanted = keys.normalize_scopes(scopes)
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise ValueError("the expiry date must be in the future")
    key = keys.generate_key()
    payload = {
        "name": cleaned,
        "key_prefix": keys.display_prefix(key),
        "key_hash": keys.hash_key(key),
        "scopes": wanted,
        "created_by": created_by,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }
    inserted = ((client or _client()).table(keys.TABLE).insert(payload).execute().data) or []
    if not inserted:
        raise RuntimeError("the API key could not be saved")
    row = _public({**payload, **inserted[0]})
    record_change(AUDIT_ENTITY, str(row["id"]), "create", created_by, after=_audit_view(row))
    logger.info("api key %s created (scopes=%s)", row["id"], ",".join(wanted))
    return row, key


def list_keys(*, include_revoked: bool = True, client: Any = None) -> List[Dict[str, Any]]:
    """Every key, newest first, each with its `status`. Never the digest."""
    found = ((client or _client()).table(keys.TABLE).select(PUBLIC_COLUMNS)
             .order("created_at", desc=True).limit(500).execute().data) or []
    out = [_public(r) for r in found]
    return out if include_revoked else [r for r in out if r["status"] != "revoked"]


def get_key(key_id: str, *, client: Any = None) -> Optional[Dict[str, Any]]:
    found = ((client or _client()).table(keys.TABLE).select(PUBLIC_COLUMNS)
             .eq("id", key_id).limit(1).execute().data) or []
    return _public(found[0]) if found else None


def revoke_key(key_id: str, revoked_by: Optional[str] = None, *,
               client: Any = None) -> Optional[Dict[str, Any]]:
    """Revoke a key: it stops working on its next request. Returns the row, or
    None when there is no such key. Revoking a revoked key changes nothing."""
    db = client or _client()
    before = get_key(key_id, client=db)
    if before is None:
        return None
    if before.get("revoked_at"):
        return before
    stamp = datetime.now(timezone.utc).isoformat()
    db.table(keys.TABLE).update({"revoked_at": stamp, "revoked_by": revoked_by}).eq("id", key_id).execute()
    after = _public({**before, "revoked_at": stamp, "revoked_by": revoked_by})
    record_change(AUDIT_ENTITY, str(key_id), "revoke", revoked_by,
                  before=_audit_view(before), after=_audit_view(after))
    logger.info("api key %s revoked", key_id)
    return after
