"""First-run setup: an engine with no admin account yet.

A fresh install has nobody who can sign in. Instead of shipping a default
login, the engine opens a setup page (``/admin/setup``,
``admin/first_run_handlers.py``) where the first person creates the admin
account, after proving they can read the engine's log by typing the one-time
setup code printed there.

When it is on
    Only in a build with no product plugin (``core.plugins.has_product()`` is
    false), and not when ``FIRST_RUN_SETUP`` is set to ``0``/``false``/``off``.
    A product build keeps its users in its own main database and must never
    offer to create one here, so there the whole module is inert: no database
    read, no code, no banner, and every question it is asked answers "no".

What "first run" means
    The setup row (``engine_setup``, one row in the engine's database) is not
    completed AND no ``public."user"`` row is a superuser. Both are read once
    at boot, off the event loop. A superuser found at boot (a headless install
    made one with ``ADMIN_EMAIL``/``ADMIN_PASSWORD``) completes the row, so
    setup stays closed for good even if every admin is removed later. Any
    read error means "not first run": the page fails closed.

The setup code
    Made at boot while in first run: 16 characters from a 31-symbol alphabet
    with no look-alikes (about 79 bits), shown as four groups of four. It is
    printed ONCE, in a banner on the engine's standard output, and never
    logged, stored or returned anywhere else: the database holds its SHA-256
    digest, its expiry (24 hours) and the count of wrong tries. Each boot
    replaces the code, which is also the way back from a lockout (5 wrong
    tries) or an expired code: restart the engine and read the new banner.
    The digest lives in the database so every engine process checks the same
    code and shares one count of wrong tries. Comparison is constant-time.
    The code is void the moment an admin exists: the row is completed and the
    digest removed.

One admin
    Creating the admin takes a claim on the row with a conditional UPDATE
    (``claimed_at`` empty or stale), so of two people submitting at once, in
    one process or two, exactly one creates an account. The account itself is
    made by ``core.admin_account.ensure_admin_account``, the function the
    headless init step uses.

Everything that touches the database or the auth service is synchronous and is
called through ``asyncio.to_thread``; a request path never blocks the loop.
The per-request question "is this first run?" is answered from memory.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from core import plugins
from core.admin_account import AdminAccountError, RestUserRows, ensure_admin_account

logger = logging.getLogger(__name__)

TABLE = "engine_setup"
ROW_ID = 1
ENV_FLAG = "FIRST_RUN_SETUP"

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
CODE_GROUPS, CODE_GROUP_SIZE = 4, 4
CODE_TTL = timedelta(hours=24)
MAX_ATTEMPTS = 5
#: A claim older than this was left by a process that died mid-creation.
CLAIM_STALE = timedelta(minutes=2)
#: While in first run (or after a failed boot check), how often a request may
#: trigger a fresh look at the database for an admin made somewhere else.
RECHECK_SEC = 5.0
RECHECK_AFTER_ERROR_SEC = 30.0

PASSWORD_MIN, PASSWORD_MAX = 10, 72  # the auth service hashes at most 72 bytes
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_WEAK_PASSWORDS = frozenset({
    "password", "passw0rd", "p@ssword", "p@ssw0rd", "password1", "password12", "password123",
    "password1234", "password!", "passwordpassword", "1234567890", "12345678910", "0123456789",
    "0987654321", "9876543210", "1q2w3e4r5t", "1qaz2wsx3edc", "qwertyuiop", "qwerty12345",
    "qwerty123456", "qwertyqwerty", "asdfghjkl;", "zxcvbnm123", "iloveyou12", "iloveyou123",
    "letmein123", "letmein1234", "welcome123", "welcome1234", "changeme12", "changeme123",
    "changeme1234", "administrator", "admin12345", "admin123456", "adminadmin", "adminadmin1",
    "superuser1", "superuser123", "rootrootroot", "abc1234567", "abcd123456", "abcdefghij",
    "trustno1trustno1", "secret1234", "secret12345", "supersecret", "supersecret1", "engine12345",
    "localhost123", "defaultpassword", "testtest123", "test123456", "testpassword",
})


class SetupError(Exception):
    """A setup submission that was refused. ``message`` is safe to show;
    ``status`` is the HTTP status the page answers with."""

    def __init__(self, message: str, status: int = 400, *, kind: str = "invalid") -> None:
        super().__init__(message)
        self.message, self.status, self.kind = message, status, kind


class SetupClosed(Exception):
    """Setup is not open (an admin exists, or the feature is off)."""


# --------------------------------------------------------------------------
# State (per process, in memory)
# --------------------------------------------------------------------------
# off       the feature is disabled or boot has not looked yet: never first run
# first_run no admin exists; a code was issued by this process at boot
# done      an admin exists; final for the life of the process
# unknown   the boot check could not read the database: closed, re-checked later
_state = "off"
_next_check = 0.0
_booted = False
_lock: Optional[asyncio.Lock] = None
_lock_loop: Any = None


def enabled() -> bool:
    """Whether this build offers first-run setup at all."""
    if plugins.has_product():
        return False
    return (os.getenv(ENV_FLAG) or "").strip().lower() not in ("0", "false", "no", "off")


def reset_for_tests() -> None:
    global _state, _next_check, _booted, _lock, _lock_loop
    _state, _next_check, _booted, _lock, _lock_loop = "off", 0.0, False, None, None


def _get_lock() -> asyncio.Lock:
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock, _lock_loop = asyncio.Lock(), loop
    return _lock


def _db() -> Any:
    """The engine's own database: where the setup row lives."""
    from core.auth import get_service_client

    return get_service_client()


def _main() -> Any:
    """The database and auth service that hold admin accounts."""
    from core.auth import get_main_client

    return get_main_client()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# The code
# --------------------------------------------------------------------------


def generate_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_GROUPS * CODE_GROUP_SIZE))
    return "-".join(raw[i:i + CODE_GROUP_SIZE] for i in range(0, len(raw), CODE_GROUP_SIZE))


def normalize_code(value: str) -> str:
    """What is hashed: upper case, without the dashes or spaces people type."""
    return re.sub(r"[\s-]+", "", value or "").upper()


def hash_code(value: str) -> str:
    return hashlib.sha256(("engine-setup:" + normalize_code(value)).encode("utf-8")).hexdigest()


def code_matches(candidate: str, stored_hash: Optional[str]) -> bool:
    """Constant-time. A missing digest is compared against a dummy, so "no
    code issued" takes as long as "wrong code"."""
    expected = stored_hash or hash_code("\x00none")
    ok = hmac.compare_digest(hash_code(candidate).encode("ascii"), str(expected).encode("ascii"))
    return ok and bool(stored_hash)


def _print_banner(code: str) -> None:
    """The one place the code is shown: the engine's standard output, which
    is what `docker compose logs engine` reads. Deliberately not the logging
    system, whose handlers may ship records elsewhere."""
    lines = [
        "",
        "=" * 64,
        "  FIRST-RUN SETUP: this engine has no admin account yet.",
        "",
        f"      Setup code:  {code}",
        "",
        "  Open the engine in a browser (http://localhost:8000) and enter",
        "  this code to create the admin account. It works once, expires",
        "  in 24 hours, and is replaced each time the engine starts.",
        "=" * 64,
        "",
    ]
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


# --------------------------------------------------------------------------
# Database reads and writes (synchronous: call through asyncio.to_thread)
# --------------------------------------------------------------------------


def _read_row(db: Any) -> Optional[Dict[str, Any]]:
    data = db.table(TABLE).select("*").eq("id", ROW_ID).limit(1).execute().data or []
    return data[0] if data else None


def _superuser_exists(main: Any) -> bool:
    data = (main.table("user").select("id").eq("permission->>is_superuser", "true")
            .limit(1).execute().data) or []
    return bool(data)


def _write_row(db: Any, fields: Dict[str, Any]) -> None:
    db.table(TABLE).upsert({"id": ROW_ID, **fields, "updated_at": _now().isoformat()}).execute()


def _mark_completed(db: Any, user_id: Optional[str], *, walkthrough: bool) -> None:
    """Close setup for good and void the code."""
    now = _now().isoformat()
    _write_row(db, {
        "code_hash": None, "code_expires_at": None, "claimed_at": None,
        "completed_at": now, "completed_by": user_id,
        # A headless install has no walkthrough to finish.
        "finished_at": None if walkthrough else now,
    })


def _is_open_sync() -> bool:
    """True when setup has not been completed and no superuser exists."""
    db = _db()
    row = _read_row(db)
    if row and row.get("completed_at"):
        return False
    if _superuser_exists(_main()):
        try:
            _mark_completed(db, None, walkthrough=False)
        except Exception as exc:  # noqa: BLE001 — closed either way
            logger.warning("first run: could not record that setup is complete (%s)",
                           type(exc).__name__)
        return False
    return True


def _issue_code_sync() -> str:
    code = generate_code()
    _write_row(_db(), {
        "code_hash": hash_code(code),
        "code_expires_at": (_now() + CODE_TTL).isoformat(),
        "failed_attempts": 0, "claimed_at": None,
        "completed_at": None, "completed_by": None, "finished_at": None,
    })
    return code


def _claim_sync(db: Any) -> bool:
    """Take the right to create the admin. One caller wins: the UPDATE only
    matches a row nobody holds (or whose holder died), and Postgres applies it
    to one of two concurrent callers."""
    now = _now()
    # UTC with a Z suffix: no '+' for a query string to mangle.
    stale = (now - CLAIM_STALE).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    # Two plain conditional UPDATEs (free, then stale) instead of one OR filter.
    for free in (True, False):
        query = (db.table(TABLE).update({"claimed_at": now.isoformat()})
                 .eq("id", ROW_ID).is_("completed_at", "null"))
        query = query.is_("claimed_at", "null") if free else query.lt("claimed_at", stale)
        if query.execute().data:
            return True
    return False


def _release_sync(db: Any) -> None:
    try:
        db.table(TABLE).update({"claimed_at": None}).eq("id", ROW_ID).execute()
    except Exception as exc:  # noqa: BLE001 — it goes stale by itself
        logger.warning("first run: could not release the setup claim (%s)", type(exc).__name__)


# --------------------------------------------------------------------------
# Boot and the per-request question
# --------------------------------------------------------------------------


async def on_boot() -> None:
    """Decide the state once at startup; in first run, issue and print the
    code. Never raises: a failure leaves setup closed."""
    global _state, _next_check, _booted
    if _booted:  # one code and one banner per process
        return
    _booted = True
    if not enabled():
        _state = "off"
        return
    try:
        if not await asyncio.to_thread(_is_open_sync):
            _state = "done"
            return
        code = await asyncio.to_thread(_issue_code_sync)
    except Exception as exc:  # noqa: BLE001 — fail closed
        _state, _next_check = "unknown", time.monotonic() + RECHECK_AFTER_ERROR_SEC
        logger.warning(
            "first run: could not tell whether an admin account exists (%s). The setup page "
            "stays closed; restart the engine once the database answers.", type(exc).__name__)
        return
    _state, _next_check = "first_run", time.monotonic() + RECHECK_SEC
    _print_banner(code)
    del code
    logger.info("first run: no admin account yet; the setup code is in the banner above")


async def in_first_run() -> bool:
    """Whether to send a visitor to the setup page. Answered from memory;
    while in first run it looks at the database again at most every few
    seconds (off the loop), to notice an admin created elsewhere."""
    global _state, _next_check
    if _state in ("off", "done") or not enabled():
        return False
    if time.monotonic() >= _next_check:
        was = _state
        _next_check = time.monotonic() + (RECHECK_SEC if was == "first_run" else RECHECK_AFTER_ERROR_SEC)
        try:
            still_open = await asyncio.to_thread(_is_open_sync)
        except Exception:  # noqa: BLE001 — keep the last answer
            return was == "first_run"
        if not still_open:
            _state = "done"
        # A boot that could not read the database issued no code: the page
        # stays closed until a restart issues one (and prints its banner).
    return _state == "first_run"


def state() -> str:
    return _state


# --------------------------------------------------------------------------
# Step 1: create the admin
# --------------------------------------------------------------------------


def validate_email(email: str) -> str:
    cleaned = (email or "").strip().lower()
    if not cleaned or len(cleaned) > 254 or not _EMAIL_RE.match(cleaned):
        raise SetupError("Enter a valid email address.", kind="email")
    return cleaned


def validate_password(password: str, email: str = "") -> str:
    """Refuse a password that is too short, too long or obviously weak."""
    value = password or ""
    if len(value) < PASSWORD_MIN:
        raise SetupError(f"Choose a password of at least {PASSWORD_MIN} characters.", kind="password")
    if len(value.encode("utf-8")) > PASSWORD_MAX:
        raise SetupError(f"Choose a password of at most {PASSWORD_MAX} characters.", kind="password")
    lowered = value.lower()
    local = (email or "").split("@")[0].lower()
    weak = (
        lowered in _WEAK_PASSWORDS
        or len(set(lowered)) < 5
        or lowered.isdigit()
        or (email and lowered == email.lower())
        or (len(local) >= 4 and len(lowered.replace(local, "")) < 6)
        or _is_sequence(lowered)
    )
    if weak:
        raise SetupError(
            "That password is too easy to guess. Use a longer phrase or a generated password.",
            kind="password")
    return value


def _is_sequence(text: str) -> bool:
    """abcdefghij, 9876543210, or one short chunk repeated (abcabcabcabc)."""
    steps = {ord(b) - ord(a) for a, b in zip(text, text[1:])}
    if steps in ({1}, {-1}):
        return True
    for size in (1, 2, 3, 4):
        if len(text) % size == 0 and text == text[:size] * (len(text) // size):
            return True
    return False


def _check_code_sync(db: Any, code: str) -> None:
    """Raise SetupError unless ``code`` is the current, unexpired code and the
    row is not locked. A wrong code is counted."""
    row = _read_row(db) or {}
    if row.get("completed_at"):
        raise SetupClosed()
    attempts = int(row.get("failed_attempts") or 0)
    if attempts >= MAX_ATTEMPTS:
        raise SetupError(
            "Too many wrong codes. Setup is locked. Restart the engine to get a new code, "
            "then read it from the engine's log.", 429, kind="locked")
    expires = _parse(row.get("code_expires_at"))
    matches = code_matches(code, row.get("code_hash"))
    if not row.get("code_hash") or expires is None or expires <= _now():
        raise SetupError(
            "The setup code has expired. Restart the engine to get a new one, then read it "
            "from the engine's log.", 410, kind="expired")
    if not matches:
        attempts += 1
        db.table(TABLE).update({"failed_attempts": attempts,
                                "updated_at": _now().isoformat()}).eq("id", ROW_ID).execute()
        left = MAX_ATTEMPTS - attempts
        if left <= 0:
            raise SetupError(
                "Too many wrong codes. Setup is locked. Restart the engine to get a new code, "
                "then read it from the engine's log.", 429, kind="locked")
        raise SetupError(
            f"That setup code is not right. {left} "
            f"{'try' if left == 1 else 'tries'} left before setup locks.", 403, kind="code")


def _create_admin_sync(email: str, password: str, code: str) -> Dict[str, Any]:
    db = _db()
    if not _is_open_sync():
        raise SetupClosed()
    _check_code_sync(db, code)
    if not _claim_sync(db):
        raise SetupError("Someone else is creating the admin account right now. "
                         "Reload the page in a moment.", 409, kind="busy")
    try:
        # Looked at again under the claim: the other submission may have just finished.
        main = _main()
        if _superuser_exists(main):
            raise SetupClosed()
        account = ensure_admin_account(main, RestUserRows(main), email, password)
    except AdminAccountError as exc:
        _release_sync(db)
        logger.warning("first run: the admin account could not be created (%s)", type(exc).__name__)
        raise SetupError(f"The account could not be created: {exc}", 400, kind="account") from exc
    except SetupClosed:
        _release_sync(db)
        raise
    except Exception:
        _release_sync(db)
        raise
    try:
        _mark_completed(db, account.get("user_id"), walkthrough=True)
    except Exception as exc:  # noqa: BLE001 — the admin exists, so setup is closed anyway
        logger.warning("first run: admin created but the setup row was not completed (%s)",
                       type(exc).__name__)
    return account


async def create_admin(email: str, password: str, code: str) -> Dict[str, Any]:
    """Create the first admin. Returns ``{"auth_id", "user_id", "email"}``.

    Raises :class:`SetupClosed` when setup is not open (the caller answers
    with the login page) and :class:`SetupError` for a refused submission.
    The password and the code are validated, used and dropped: neither is
    logged or returned.
    """
    global _state
    if not enabled() or _state != "first_run":
        raise SetupClosed()
    cleaned_email = validate_email(email)
    validate_password(password, cleaned_email)
    if normalize_code(password) == normalize_code(code) and normalize_code(code):
        raise SetupError("Do not use the setup code as your password.", kind="password")
    async with _get_lock():
        if _state != "first_run":
            raise SetupClosed()
        try:
            account = await asyncio.to_thread(_create_admin_sync, cleaned_email, password, code)
        except SetupClosed:
            _state = "done"
            raise
        _state = "done"
    logger.info("first run: admin account created; setup is closed")
    try:
        from admin.audit import record_change

        await asyncio.to_thread(record_change, TABLE, str(ROW_ID), "create_admin",
                                account.get("user_id"), None, {"email": cleaned_email})
    except Exception:  # noqa: BLE001 — audit is best effort
        pass
    return {**account, "email": cleaned_email}


# --------------------------------------------------------------------------
# Steps 2 and 3: the walkthrough of the admin who just set up
# --------------------------------------------------------------------------


def _walkthrough_open_sync(user_id: str) -> bool:
    row = _read_row(_db()) or {}
    return bool(row.get("completed_at")) and not row.get("finished_at") \
        and str(row.get("completed_by") or "") == str(user_id)


async def walkthrough_open(user_id: str) -> bool:
    """Whether ``user_id`` is the admin who completed setup and has not yet
    finished (or left) the remaining steps. False on any error and whenever
    the feature is off."""
    if not enabled():
        return False
    try:
        return await asyncio.to_thread(_walkthrough_open_sync, user_id)
    except Exception:  # noqa: BLE001
        return False


def _finish_sync() -> None:
    _db().table(TABLE).update({"finished_at": _now().isoformat(),
                               "updated_at": _now().isoformat()}).eq("id", ROW_ID).execute()


async def finish_walkthrough() -> None:
    try:
        await asyncio.to_thread(_finish_sync)
    except Exception as exc:  # noqa: BLE001
        logger.warning("first run: could not mark the walkthrough finished (%s)", type(exc).__name__)
