"""Model provider keys stored in the database, encrypted with ``SECRET_KEY``.

A provider key (Gemini, OpenAI, Anthropic, Azure OpenAI) can come from two
places. The portal's **Settings › Model keys** screen stores one in the
``provider_keys`` table, encrypted; the environment holds one in a variable
such as ``GOOGLE_API_KEY``. A stored key wins; with none stored, the variable
is used exactly as before this module existed.

Encryption
----------
``SECRET_KEY`` is a server secret from the environment. It may be any string
of 16 characters or more; a Fernet key is derived from it (HKDF-SHA256), so
it does not have to be a Fernet key itself. To rotate it, put the new secret
first in a comma-separated list: ``SECRET_KEY=new,old``. The first entry
encrypts, every entry decrypts; saving a key again re-encrypts it with the
first.

With no ``SECRET_KEY`` nothing is ever stored (``encrypt`` raises
``SecretKeyMissing``; there is no plain-text fallback) and nothing is ever
read: the runtime does not query the table at all, so a deployment that keeps
its keys in the environment behaves exactly as it always did.

Reading without blocking
------------------------
The Supabase client is synchronous, and a chat model is built inside a
request. So the runtime never reads the table on demand: it keeps a decrypted
snapshot in memory for ``CACHE_TTL_SEC`` (60s) and ``stored_key`` only looks
at that snapshot.

* The engine loads it once at boot (``main.py`` lifespan, in a thread).
* A read that finds the snapshot stale inside a running event loop returns
  what it has and refreshes in a worker thread.
* A read outside an event loop (a boot seeder, a script, a worker thread)
  refreshes inline.
* A write through ``admin/services/provider_keys.py`` reloads it at once, so
  the process that saved a key uses it on the next request. Other replicas
  catch up within the TTL.

Nothing here logs a key or its ciphertext.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TABLE = "provider_keys"
SECRET_KEY_ENV = "SECRET_KEY"
MIN_SECRET_LENGTH = 16
CACHE_TTL_SEC = 60.0
#: The name of a provider's one unnamed key.
DEFAULT_NAME = ""

#: Providers a key can be stored for: slug → (label, the environment variable
#: that holds the same key). The slugs are ``agent_models.provider`` values.
PROVIDERS: Dict[str, Tuple[str, str]] = {
    "google_genai": ("Google Gemini", "GOOGLE_API_KEY"),
    "openai": ("OpenAI", "OPENAI_API_KEY"),
    "anthropic": ("Anthropic", "ANTHROPIC_API_KEY"),
    "azure_openai": ("Azure OpenAI", "AZURE_OPENAI_API_KEY"),
}
_ALIASES = {"google": "google_genai"}
_ENV_TO_PROVIDER = {env: slug for slug, (_label, env) in PROVIDERS.items()}


class SecretKeyMissing(RuntimeError):
    """``SECRET_KEY`` is unset or too short, so nothing can be encrypted."""


class DecryptError(RuntimeError):
    """A stored key cannot be decrypted with this server's ``SECRET_KEY``."""


def canonical_provider(provider: Optional[str]) -> str:
    slug = (provider or "").strip().lower()
    return _ALIASES.get(slug, slug)


def env_var_for(provider: Optional[str]) -> Optional[str]:
    entry = PROVIDERS.get(canonical_provider(provider))
    return entry[1] if entry else None


# --------------------------------------------------------------------------
# Encryption
# --------------------------------------------------------------------------


def _secrets() -> List[str]:
    return [s.strip() for s in (os.getenv(SECRET_KEY_ENV) or "").split(",") if s.strip()]


def secret_key_problem() -> Optional[str]:
    """Why keys cannot be stored on this server, or None when they can."""
    secrets = _secrets()
    if not secrets:
        return (f"{SECRET_KEY_ENV} is not set on this server, so a key cannot be "
                f"encrypted and will not be stored. Set {SECRET_KEY_ENV} to a long "
                f"random string in the engine's environment and restart it.")
    if any(len(s) < MIN_SECRET_LENGTH for s in secrets):
        return (f"{SECRET_KEY_ENV} is too short to protect a key: every entry needs "
                f"at least {MIN_SECRET_LENGTH} characters.")
    return None


def encryption_available() -> bool:
    return secret_key_problem() is None


def _derive(secret: str) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=b"provider-keys/v1").derive(secret.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


def _cipher():
    problem = secret_key_problem()
    if problem:
        raise SecretKeyMissing(problem)
    from cryptography.fernet import Fernet, MultiFernet

    return MultiFernet([Fernet(_derive(s)) for s in _secrets()])


def encrypt(value: str) -> str:
    """Ciphertext for ``value`` under the first ``SECRET_KEY`` entry."""
    return _cipher().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """The key behind ``ciphertext``; any ``SECRET_KEY`` entry may open it."""
    from cryptography.fernet import InvalidToken

    cipher = _cipher()
    try:
        return cipher.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError):
        # Deliberately carries no part of the ciphertext or the cause.
        raise DecryptError(
            f"this key was encrypted with a different {SECRET_KEY_ENV}; "
            f"enter it again") from None


def hint_for(value: str) -> str:
    """The last four characters, for telling two keys apart. A key too short
    for four characters to be a small part of it gets no hint."""
    value = value.strip()
    return value[-4:] if len(value) >= 12 else ""


# --------------------------------------------------------------------------
# The decrypted snapshot
# --------------------------------------------------------------------------

_lock = threading.Lock()
_snapshot: Dict[Tuple[str, str], str] = {}
_token: str = ""
_loaded_at: Optional[float] = None
_refreshing = False
_warned = False


def _environment() -> str:
    from core.agent_profile import _default_environment

    return _default_environment()


def _fetch_rows() -> List[Dict[str, Any]]:
    from core.auth import get_service_client

    result = (get_service_client().table(TABLE)
              .select("provider, name, ciphertext, updated_at")
              .eq("environment", _environment()).execute())
    data = getattr(result, "data", None)
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def refresh() -> None:
    """Read and decrypt this environment's keys now. Blocking: call it from a
    thread, never inline in a request. Never raises."""
    global _snapshot, _token, _loaded_at, _warned
    if not encryption_available():
        with _lock:
            _snapshot, _token, _loaded_at = {}, "", time.monotonic()
        return
    fresh: Dict[Tuple[str, str], str] = {}
    stamps: List[str] = []
    try:
        for row in _fetch_rows():
            provider = canonical_provider(row.get("provider"))
            name = str(row.get("name") or "")
            try:
                fresh[(provider, name)] = decrypt(str(row.get("ciphertext") or ""))
            except DecryptError:
                if not _warned:
                    logger.warning(
                        "provider keys: the stored %s key cannot be decrypted with this "
                        "server's %s; using the environment variable instead",
                        provider, SECRET_KEY_ENV)
                    _warned = True
                continue
            stamps.append(f"{provider}/{name}/{row.get('updated_at')}")
    except Exception as exc:  # noqa: BLE001 - a missing table must not break chat
        if not _warned:
            logger.warning("provider keys: could not read stored keys (%s); using "
                           "environment variables", type(exc).__name__)
        _warned = True
        with _lock:
            # Keep what was loaded before; try again after the TTL.
            _loaded_at = time.monotonic()
        return
    token = hashlib.sha256("|".join(sorted(stamps)).encode("utf-8")).hexdigest()[:16] if stamps else ""
    with _lock:
        _snapshot, _token, _loaded_at = fresh, token, time.monotonic()


async def arefresh() -> None:
    """``refresh`` off the event loop."""
    await asyncio.to_thread(refresh)


def invalidate() -> None:
    """Forget the snapshot; the next read loads it again."""
    global _snapshot, _token, _loaded_at
    with _lock:
        _snapshot, _token, _loaded_at = {}, "", None


def _refresh_in_background() -> None:
    global _refreshing
    with _lock:
        if _refreshing:
            return
        _refreshing = True

    def _run() -> None:
        global _refreshing
        try:
            refresh()
        finally:
            _refreshing = False

    threading.Thread(target=_run, name="provider-keys-refresh", daemon=True).start()


def _ensure_loaded() -> None:
    loaded_at = _loaded_at
    if loaded_at is not None and (time.monotonic() - loaded_at) < CACHE_TTL_SEC:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        refresh()
        return
    # On the event loop: never block. Serve what is in memory and reload aside.
    _refresh_in_background()


def stored_key(provider: Optional[str], name: str = DEFAULT_NAME) -> Optional[str]:
    """The key stored in the portal for ``provider`` (and credential ``name``),
    or None. Reads the in-memory snapshot only."""
    if not encryption_available():
        return None
    _ensure_loaded()
    return _snapshot.get((canonical_provider(provider), (name or "").strip()))


def resolve_key(provider: Optional[str], env_var: Optional[str],
                name: str = DEFAULT_NAME) -> str:
    """The key to use: the stored one, else the environment variable, else ''."""
    stored = stored_key(provider, name)
    if stored:
        return stored
    return os.getenv(env_var, "") if env_var else ""


def env_or_stored(env_var: str) -> str:
    """For code that reads a provider variable by name (embeddings, voice):
    the provider's stored default key when there is one, else the variable."""
    provider = _ENV_TO_PROVIDER.get(env_var)
    if provider:
        return resolve_key(provider, env_var)
    return os.getenv(env_var, "")


def has_key(provider: Optional[str]) -> bool:
    """Whether ``provider`` has a default key, stored or in the environment."""
    return bool(resolve_key(provider, env_var_for(provider)))


def cache_token() -> str:
    """Changes whenever the set of stored keys changes; '' with none stored.
    Part of the chat-model cache key, so a replaced key builds a new client."""
    if not encryption_available():
        return ""
    _ensure_loaded()
    return _token
