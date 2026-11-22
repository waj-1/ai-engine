"""Store, remove, list and test model provider keys.

The one place the portal (Settings › Model keys), the admin MCP
(``provider_keys`` tool) and any later caller, such as a first-run page, go
through to change a stored key::

    from admin.services import provider_keys
    provider_keys.set_key("openai", "sk-…", actor=user_id)
    provider_keys.key_status()

Every function here is synchronous and talks to the database (and ``check_key``
to the provider): call them through ``asyncio.to_thread`` from an async route.

Nothing here returns, logs or audits a key or its ciphertext. A stored key
leaves this module only as its hint (the last four characters).
Encryption, and how the runtime reads keys, is ``core/provider_keys.py``.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from core import provider_keys as store

logger = logging.getLogger(__name__)

TABLE = store.TABLE
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_PUBLIC_COLUMNS = "key_id, environment, provider, name, hint, created_by, created_at, updated_by, updated_at"


class ProviderKeyError(ValueError):
    """A request that cannot be carried out; the message is safe to show."""


def _client(env: str, client: Any = None) -> Any:
    if client is not None:
        return client
    from core.auth import CrossEnvNotConfiguredError, get_service_client, get_service_client_for_env

    try:
        return get_service_client_for_env(env)
    except CrossEnvNotConfiguredError:
        return get_service_client()


def _env(env: Optional[str]) -> str:
    return env or store._environment()


def _provider(provider: str) -> str:
    slug = store.canonical_provider(provider)
    if slug not in store.PROVIDERS:
        raise ProviderKeyError(
            f"unknown provider '{provider}'. A key can be stored for: "
            f"{', '.join(sorted(store.PROVIDERS))}.")
    return slug


def _name(name: Optional[str]) -> str:
    value = (name or "").strip()
    if value and not _NAME_RE.match(value):
        raise ProviderKeyError(
            "a credential name is 1 to 64 letters, digits, dots, dashes or "
            "underscores, starting with a letter or digit")
    return value


def _actor_uuid(actor: Optional[str]) -> Optional[str]:
    try:
        return str(uuid.UUID(str(actor)))
    except (ValueError, TypeError):
        return None


def _rows(client: Any, env: str, columns: str = _PUBLIC_COLUMNS) -> List[Dict[str, Any]]:
    data = client.table(TABLE).select(columns).eq("environment", env).execute().data
    return [r for r in (data or []) if isinstance(r, dict)]


def _find(client: Any, env: str, provider: str, name: str) -> Optional[Dict[str, Any]]:
    for row in _rows(client, env):
        if store.canonical_provider(row.get("provider")) == provider and (row.get("name") or "") == name:
            return row
    return None


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    """A row as it may leave this module: never its ciphertext."""
    return {k: row.get(k) for k in ("key_id", "environment", "provider", "name", "hint",
                                    "created_by", "created_at", "updated_by", "updated_at")}


def _after_write() -> None:
    """This process uses the change on its next request."""
    store.refresh()
    try:
        from runtime.llm_factory import clear_llm_cache

        clear_llm_cache()
    except Exception:  # noqa: BLE001 - the cache token already covers it
        pass


def _audit(key_id: Optional[str], action: str, actor: Optional[str],
           before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]]) -> None:
    from admin.audit import record_change

    record_change(entity_type=TABLE, entity_id=key_id, action=action,
                  actor_user_id=_actor_uuid(actor) or actor, before=before, after=after)


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------


def set_key(provider: str, value: str, actor: Optional[str], name: Optional[str] = None,
            *, env: Optional[str] = None, client: Any = None) -> Dict[str, Any]:
    """Store (or replace) ``provider``'s key, encrypted. ``name`` stores it as
    a named credential beside the provider's default key.

    Returns the stored row without the key: provider, name, hint, who and
    when, plus ``replaced``. Raises ``core.provider_keys.SecretKeyMissing``
    when the server has no usable ``SECRET_KEY`` (nothing is written) and
    ``ProviderKeyError`` for a bad provider, name or value.
    """
    slug, cred, env = _provider(provider), _name(name), _env(env)
    key = (value or "").strip()
    if not key:
        raise ProviderKeyError("the key is empty")
    if len(key) < 8 or len(key) > 4096 or any(ch.isspace() for ch in key):
        raise ProviderKeyError(
            "that does not look like a key: it must be 8 to 4096 characters with no spaces or line breaks")
    ciphertext = store.encrypt(key)  # raises SecretKeyMissing before any write
    db = _client(env, client)
    before = _find(db, env, slug, cred)
    now = datetime.now(timezone.utc).isoformat()
    who = _actor_uuid(actor)
    payload = {"ciphertext": ciphertext, "hint": store.hint_for(key),
               "updated_by": who, "updated_at": now}
    if before:
        db.table(TABLE).update(payload).eq("key_id", before["key_id"]).execute()
        key_id = before["key_id"]
        row = {**before, **payload}
    else:
        inserted = db.table(TABLE).insert({
            "environment": env, "provider": slug, "name": cred,
            "created_by": who, "created_at": now, **payload,
        }).execute().data or [{}]
        row = {"environment": env, "provider": slug, "name": cred, "created_by": who,
               "created_at": now, **payload, **(inserted[0] if isinstance(inserted[0], dict) else {})}
        key_id = row.get("key_id")
    public = _public(row)
    _audit(key_id, "replace" if before else "set", actor,
           _public(before) if before else None, public)
    _after_write()
    return {**public, "replaced": bool(before)}


def remove_key(provider: str, actor: Optional[str], name: Optional[str] = None,
               *, env: Optional[str] = None, client: Any = None) -> bool:
    """Delete a stored key. False when there was none. The provider goes back
    to its environment variable, if one is set."""
    slug, cred, env = _provider(provider), _name(name), _env(env)
    db = _client(env, client)
    before = _find(db, env, slug, cred)
    if not before:
        return False
    db.table(TABLE).delete().eq("key_id", before["key_id"]).execute()
    _audit(before["key_id"], "delete", actor, _public(before), None)
    _after_write()
    return True


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------


def key_status(*, env: Optional[str] = None, client: Any = None) -> Dict[str, Any]:
    """Where each provider's key comes from. Never includes a key.

    ``{"can_store", "problem", "environment", "providers": [...]}``; each
    provider has ``source`` (``portal`` | ``environment`` | ``none``),
    ``env_var``/``env_set``, its ``default`` stored key (hint, who, when,
    ``readable``) or None, and its named ``credentials``.
    """
    env = _env(env)
    problem = store.secret_key_problem()
    rows: List[Dict[str, Any]] = []
    load_error: Optional[str] = None
    try:
        rows = _rows(_client(env, client), env, _PUBLIC_COLUMNS + ", ciphertext")
    except Exception as exc:  # noqa: BLE001 - e.g. the table is not migrated yet
        logger.warning("provider keys: status read failed (%s)", type(exc).__name__)
        load_error = "The stored keys could not be read from the database."

    def entry(row: Dict[str, Any]) -> Dict[str, Any]:
        readable = False
        if not problem:
            try:
                store.decrypt(str(row.get("ciphertext") or ""))
                readable = True
            except store.DecryptError:
                readable = False
        return {**_public(row), "readable": readable}

    providers = []
    for slug, (label, env_var) in store.PROVIDERS.items():
        mine = [entry(r) for r in rows if store.canonical_provider(r.get("provider")) == slug]
        default = next((r for r in mine if not (r.get("name") or "")), None)
        env_set = bool(os.getenv(env_var))
        source = "portal" if default and default["readable"] else ("environment" if env_set else "none")
        providers.append({
            "provider": slug, "label": label, "env_var": env_var, "env_set": env_set,
            "source": source, "default": default,
            "credentials": sorted((r for r in mine if r.get("name")), key=lambda r: r["name"]),
        })
    return {"can_store": problem is None, "problem": problem, "environment": env,
            "load_error": load_error, "providers": providers}


def provider_sources(*, env: Optional[str] = None, client: Any = None) -> Dict[str, str]:
    """``{provider slug: "portal" | "environment" | "none"}`` for the model form."""
    out = {p["provider"]: p["source"] for p in key_status(env=env, client=client)["providers"]}
    out["google"] = out.get("google_genai", "none")
    return out


# --------------------------------------------------------------------------
# Test
# --------------------------------------------------------------------------

_TIMEOUT_SEC = 15.0


def _http_get(url: str, headers: Dict[str, str]) -> Any:
    import httpx

    return httpx.get(url, headers=headers, timeout=_TIMEOUT_SEC, follow_redirects=False)


def _scrub(text: str, key: str) -> str:
    """Provider error text with the key (or a long piece of it) taken out."""
    out = str(text or "")
    if key:
        out = out.replace(key, "[key hidden]")
        for size in (24, 16, 12):
            if len(key) > size + 4:
                out = out.replace(key[:size], "[key hidden]").replace(key[-size:], "[key hidden]")
    return " ".join(out.split())[:300]


def _model_extra(client: Any, env: str, provider: str, credential: str) -> Dict[str, Any]:
    """The extra_config of a model row that uses this provider/credential."""
    try:
        rows = client.table("agent_models").select("provider, extra_config, environment, is_active").execute().data or []
    except Exception:  # noqa: BLE001
        return {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        extra = row.get("extra_config") or {}
        if store.canonical_provider(row.get("provider")) != provider or not isinstance(extra, dict):
            continue
        if (extra.get("credential") or "") == credential and (row.get("environment") or env) == env:
            return extra
    return {}


def _probe(provider: str, key: str, extra: Dict[str, Any], credential: str) -> Optional[tuple]:
    """(url, headers) of one free, read-only call that needs a valid key."""
    if provider == "google_genai":
        return ("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
                {"x-goog-api-key": key})
    if provider == "anthropic":
        return ("https://api.anthropic.com/v1/models?limit=1",
                {"x-api-key": key, "anthropic-version": "2023-06-01"})
    if provider == "openai":
        base = str(extra.get("base_url") or "").strip().rstrip("/")
        if credential and not base:
            return None
        return ((base or "https://api.openai.com/v1") + "/models", {"Authorization": f"Bearer {key}"})
    if provider == "azure_openai":
        endpoint = str(extra.get("endpoint") or os.getenv("AZURE_OPENAI_ENDPOINT") or "").strip().rstrip("/")
        if not endpoint:
            return None
        return (f"{endpoint}/openai/models?api-version=2024-10-21", {"api-key": key})
    return None


def check_key(provider: str, name: Optional[str] = None, *, env: Optional[str] = None,
             client: Any = None, http_get: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    """Make one free call (list models) with the key in use for ``provider``.

    ``{"ok", "source", "detail"}``. ``detail`` is the provider's own error on
    failure, with the key removed from it. Blocking (network).
    """
    slug, cred, env = _provider(provider), _name(name), _env(env)
    db = _client(env, client)
    label, env_var = store.PROVIDERS[slug]
    key, source = "", "none"
    try:
        rows = _rows(db, env, "provider, name, ciphertext")
    except Exception:  # noqa: BLE001
        rows = []
    stored = next((r for r in rows if store.canonical_provider(r.get("provider")) == slug
                   and (r.get("name") or "") == cred), None)
    if stored:
        try:
            key, source = store.decrypt(str(stored.get("ciphertext") or "")), "portal"
        except (store.DecryptError, store.SecretKeyMissing) as exc:
            if cred:
                return {"ok": False, "source": "portal", "detail": str(exc)}
    if not key and not cred:
        key, source = os.getenv(env_var, ""), "environment"
    if not key:
        return {"ok": False, "source": "none",
                "detail": f"No key is set for {label}" + (f" under the name '{cred}'." if cred else ".")}
    probe = _probe(slug, key, _model_extra(db, env, slug, cred), cred)
    if probe is None:
        if slug == "azure_openai":
            detail = ("There is no Azure OpenAI endpoint to call: set AZURE_OPENAI_ENDPOINT, or "
                      "add a model with an `endpoint` in its extra settings.")
        else:
            detail = (f"No model uses the credential '{cred}' with a base_url yet, so there is "
                      f"no endpoint to call. Add `credential` and `base_url` to a model first.")
        return {"ok": False, "source": source, "detail": detail}
    url, headers = probe
    try:
        response = (http_get or _http_get)(url, headers)
    except Exception as exc:  # noqa: BLE001 - network errors are the answer here
        return {"ok": False, "source": source,
                "detail": _scrub(f"Could not reach {label}: {type(exc).__name__}: {exc}", key)}
    status = getattr(response, "status_code", 0)
    if 200 <= status < 300:
        return {"ok": True, "source": source, "detail": f"{label} accepted the key."}
    message = ""
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        message = (error.get("message") if isinstance(error, dict) else error) or ""
    except Exception:  # noqa: BLE001 - not JSON
        message = getattr(response, "text", "") or ""
    return {"ok": False, "source": source,
            "detail": _scrub(f"{label} answered {status}: {message or 'no message'}", key)}
