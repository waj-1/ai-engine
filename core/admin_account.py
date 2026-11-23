"""Create the engine's admin account: an auth user plus its superuser row.

An admin is two things (see ``admin/auth.resolve_superuser``): a user in the
auth service, and a ``public."user"`` row pointing at it with
``permission.is_superuser = true``. This module is the one place that makes
both, for every caller:

* the init step of a headless install (``deploy/_init_db_common.py``), which
  writes the row over its direct Postgres connection, and
* the first-run page (``admin/services/first_run.py``), which writes it
  through the REST layer like the rest of the running engine.

The two differ only in how the row is written, so that part is passed in as
``rows``: an object with ``find(auth_id)``, ``update(auth_id, fields)`` and
``insert(fields)``. Everything else (creating the auth user, falling back to
an existing one, what the row holds) is shared.

Everything here blocks (HTTP to the auth service, the database): call it
through ``asyncio.to_thread`` from an async route. Nothing here logs or
returns a password.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

SUPERUSER_PERMISSION: Dict[str, Any] = {"is_superuser": True}
ADMIN_ROLE = "admin"


class AdminAccountError(RuntimeError):
    """The account could not be made. The message is what the auth service or
    the database answered; it never contains the password."""


class RestUserRows:
    """``public."user"`` through the REST layer (a service-role client)."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def find(self, auth_id: str) -> Optional[str]:
        data = (self._client.table("user").select("id").eq("auth_id", auth_id)
                .limit(1).execute().data) or []
        return str(data[0]["id"]) if data else None

    def update(self, auth_id: str, fields: Dict[str, Any]) -> None:
        self._client.table("user").update(fields).eq("auth_id", auth_id).execute()

    def insert(self, fields: Dict[str, Any]) -> Optional[str]:
        data = self._client.table("user").insert(fields).execute().data or []
        return str(data[0]["id"]) if data and data[0].get("id") else None


def superuser_fields(auth_id: str, email: str) -> Dict[str, Any]:
    """What an admin's ``public."user"`` row holds."""
    return {"auth_id": auth_id, "email": email,
            "permission": dict(SUPERUSER_PERMISSION), "role": ADMIN_ROLE}


def _existing_auth_user(auth_client: Any, email: str) -> Optional[str]:
    page = auth_client.auth.admin.list_users()
    users = page if isinstance(page, list) else getattr(page, "users", [])
    wanted = email.strip().lower()
    for user in users:
        if (getattr(user, "email", None) or "").strip().lower() == wanted:
            return str(user.id)
    return None


def ensure_admin_account(auth_client: Any, rows: Any, email: str, password: str, *,
                         log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Make ``email`` an admin with ``password``. Idempotent.

    Creates the auth user (already confirmed, so it can sign in at once); when
    the auth service refuses because the address is already registered, the
    existing user's password is reset instead. Then inserts or updates the
    superuser row. Returns ``{"auth_id", "user_id", "created"}``; ``created``
    is False when an existing auth user was reused.

    Raises :class:`AdminAccountError` when no auth user could be created or
    found (a rejected email or password, the auth service down) or the row
    could not be written. An auth user created by this call is removed again
    if its row cannot be written, so a retry starts clean.
    """
    say = log or (lambda _msg: None)
    auth_id: Optional[str] = None
    created = False
    try:
        made = auth_client.auth.admin.create_user(
            {"email": email, "password": password, "email_confirm": True})
        auth_id, created = str(made.user.id), True
        say(f"  ✓ GoTrue user created ({auth_id})")
    except Exception as exc:  # noqa: BLE001 — already exists, rejected, or transient
        say(f"  GoTrue create said: {exc}; looking up existing user…")
        reason = str(exc) or type(exc).__name__
        try:
            auth_id = _existing_auth_user(auth_client, email)
            if auth_id:
                auth_client.auth.admin.update_user_by_id(auth_id, {"password": password})
                say(f"  ↻ reset password for existing GoTrue user ({auth_id})")
        except Exception as exc2:  # noqa: BLE001
            say(f"  ! could not resolve GoTrue user: {exc2}")
            auth_id = None
        if not auth_id:
            raise AdminAccountError(reason) from exc

    fields = superuser_fields(auth_id, email)
    try:
        user_id = rows.find(auth_id)
        if user_id:
            rows.update(auth_id, {k: v for k, v in fields.items() if k != "auth_id"})
            say("  ↻ updated superuser user row.")
        else:
            user_id = rows.insert(fields) or rows.find(auth_id)
            say("  ✓ inserted superuser user row.")
    except Exception as exc:  # noqa: BLE001
        if created:
            try:
                auth_client.auth.admin.delete_user(auth_id)
            except Exception:  # noqa: BLE001 — best effort; the next run reuses it
                pass
        raise AdminAccountError(f"the admin's user row could not be written: {exc}") from exc
    return {"auth_id": auth_id, "user_id": user_id, "created": created}
