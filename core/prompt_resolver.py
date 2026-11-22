"""
Prompt resolver — fetch a prompt by version id, fall back to file-system
defaults if the DB row is missing.

Agents ship their starting prompts as Python strings (runtime/prompts/*.py).
The admin portal lets ops edit prompts as data; this module is the
single read-path that bridges the two.

Resolution order:
1. If a `prompt_version_id` is supplied, fetch it from agent_prompt_versions.
2. If the fetch fails or the id is None, return the supplied `fallback`.
3. If `fallback` is also None, raise — the caller forgot to wire defaults.

Variables in DB content use Python `str.format` shape (e.g. `{locale}`).
We deliberately don't render Jinja here — keeps the path predictable and
matches today's f-string usage in agent code.
"""

from __future__ import annotations

import logging
import re
import time
from contextvars import ContextVar
from typing import Any, Dict, Optional

from core.auth import get_service_client

logger = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"@\[(tool|mcp|kb):([^\]]+)\]")


def _expand_mentions(content: str) -> str:
    """Expand operator @[tool|mcp|kb:<name>] tokens to `<name>` at resolve time.

    Pure regex — matches only the mention syntax, so a prompt with no such
    token is returned byte-identical (this ships dark: the insert-only seeder
    means running prompts carry no tokens until an operator adds one via the
    editor). Deliberately a targeted re.sub, not format_map, so it can never
    choke on `{...}` JSON/example blocks (the Phase-2 lesson).
    """
    if not content or "@[" not in content:
        return content
    return _MENTION_RE.sub(lambda m: f"`{m.group(2).strip()}`", content)


# Request-scoped prompt override.
#
# Eval runners (and the in-dev console) set this ContextVar to pin a
# specific prompt version when resolving an agent's prompts — bypassing
# the normal "use whichever version is active" lookup. Keys are
# "<agent_kind>/<prompt_key>" strings (e.g. "sales/system"), values
# are integer version_numbers. The resolver maps (agent_kind, prompt_key,
# version_number) → prompt_version_id by querying agent_prompts +
# agent_prompt_versions, caches the resulting content by version_id, and
# returns it instead of the active row's content.
#
# A ContextVar (rather than a thread-local or process global) gives us
# per-request scoping automatically inside FastAPI's async stack: setting
# it inside one request handler cannot leak into a concurrent one even
# though they share the same thread pool. The middleware that wires the
# `X-WAJ-Prompt-Override` header still clears the value in a `finally`
# block defensively.
_PROMPT_OVERRIDE: ContextVar[Optional[Dict[str, int]]] = ContextVar(
    "_PROMPT_OVERRIDE", default=None,
)


def override_slot(agent_kind: Optional[str], profile_slug: Optional[str], key: str = "system") -> str:
    """The `agent_prompts.slug` a request-scoped override of `key` names for
    this agent.

    Most kinds keep one prompt row per key (`system`). Generic `custom`
    agents all share the one kind, so each owns a row named after itself
    (`<agent slug>:system` — admin.services.custom_prompts.own_prompt_slug).
    Looking a custom agent's override up under plain `system` found no row,
    logged a warning and served the unchanged prompt, so a "test this draft"
    run graded the live prompt and reported it as the draft (2026-11-22).
    """
    if agent_kind == "custom" and profile_slug:
        return f"{profile_slug}:{key}"
    return key


def set_prompt_override(d: Optional[Dict[str, int]]) -> Any:
    """Pin specific prompt versions for the current request scope.

    `d` maps "<agent_kind>/<prompt_key>" → version_number. Returns the
    Token returned by ContextVar.set so callers may reset to the previous
    value if they want strict undo (the middleware uses a plain
    `clear_prompt_override` in a `finally` block, which is sufficient for
    HTTP requests; tests use the token for tight round-trip control).
    """
    return _PROMPT_OVERRIDE.set(d)


def clear_prompt_override() -> None:
    """Drop any override pinned for the current request scope.

    Safe to call when nothing is set — re-assigns the ContextVar to None
    so the resolver falls back to the normal active-version lookup.
    """
    _PROMPT_OVERRIDE.set(None)


def get_prompt_override() -> Optional[Dict[str, int]]:
    """Read the current override dict (or None). Exposed for tests +
    diagnostics; the resolver path uses it directly.
    """
    return _PROMPT_OVERRIDE.get()


class PromptResolver:
    _CACHE_TTL_SEC = 60.0
    _cache: Dict[str, tuple[float, str]] = {}

    @classmethod
    def invalidate(cls, prompt_version_id: Optional[str] = None) -> None:
        if prompt_version_id is None:
            cls._cache.clear()
            return
        cls._cache.pop(prompt_version_id, None)

    @classmethod
    def resolve(
        cls,
        prompt_version_id: Optional[str],
        fallback: Optional[str] = None,
        variables: Optional[Dict[str, Any]] = None,
        *,
        agent_kind: Optional[str] = None,
        prompt_key: Optional[str] = None,
    ) -> str:
        """Return the rendered prompt string.

        - `prompt_version_id` may be None (callers without a DB-backed profile
          just want the fallback).
        - `fallback` is the file-system prompt — required when no version id
          is provided or the fetch fails. If both are missing we raise.
        - `variables` are substituted via `str.format`; missing keys are
          left as literal `{key}` rather than raising, to match the legacy
          tolerance in the existing prompt code.
        - `agent_kind` + `prompt_key` are optional hints used to apply a
          request-scoped override (see `_PROMPT_OVERRIDE`). When both are
          supplied and the current override dict has an entry for
          "<agent_kind>/<prompt_key>", the resolver fetches THAT specific
          version_number instead of the active `prompt_version_id`. This
          is the hook that powers the prompt A/B feature in the admin
          portal — eval runs pin one version per parallel run via the
          `X-WAJ-Prompt-Override` header.
        """
        overridden_id = cls._lookup_override_version_id(agent_kind, prompt_key)
        if overridden_id is not None:
            # The override path uses the same cache (keyed by version_id),
            # so repeated lookups inside one eval run only hit the DB once
            # per pinned version. The override doesn't disable the
            # fallback — if the pinned version disappears we still degrade
            # gracefully to the caller's fallback string.
            prompt_version_id = overridden_id
        content = cls._fetch_or_fallback(prompt_version_id, fallback)
        if variables:
            try:
                content = content.format_map(_SafeDict(variables))
            except Exception as exc:
                logger.warning("prompt_resolver: format failed (%s); returning unformatted", exc)
        return _expand_mentions(content)

    @classmethod
    def _fetch_or_fallback(cls, prompt_version_id: Optional[str], fallback: Optional[str]) -> str:
        if not prompt_version_id:
            if fallback is None:
                raise ValueError("PromptResolver.resolve called with no prompt_version_id and no fallback")
            return fallback

        cached = cls._cache.get(prompt_version_id)
        now = time.monotonic()
        if cached is not None and (now - cached[0]) < cls._CACHE_TTL_SEC:
            return cached[1]

        try:
            client = get_service_client()
            rows = (
                client.table("agent_prompt_versions")
                .select("content")
                .eq("prompt_version_id", prompt_version_id)
                .limit(1)
                .execute()
                .data
            )
            if not rows:
                logger.warning("prompt_resolver: %s not found; using fallback", prompt_version_id)
                if fallback is None:
                    raise ValueError(f"Prompt version {prompt_version_id} missing and no fallback")
                return fallback
            content = rows[0]["content"]
            cls._cache[prompt_version_id] = (now, content)
            return content
        except Exception as exc:
            logger.warning(
                "prompt_resolver: DB fetch failed for %s (%s); using fallback",
                prompt_version_id,
                exc,
            )
            if fallback is None:
                raise
            return fallback


    @classmethod
    def resolve_by_slug(
        cls,
        slug: Optional[str],
        *,
        agent_kind: str,
        fallback: Optional[str] = None,
        variables: Optional[Dict[str, Any]] = None,
        prompt_key: Optional[str] = None,
    ) -> str:
        """Resolve a prompt by (agent_kind, slug) instead of a known version id.

        Every OTHER caller of `resolve()` already has a `prompt_version_id` —
        it comes off a DB-backed `Profile.prompt_version_ids` (see
        `core.agent_profile.AgentProfileLoader`), which the seeders populate
        by resolving each `agent_prompts` slug to its latest version at
        profile-creation time. A caller that only has the slug itself (no
        profile in scope — e.g. duplex voice's `voice_config.live.
        persona_prompt_slug`) needs that same slug -> version resolution
        done on demand instead; this is the one implementation of it.

        Looks up the `agent_prompts` row scoped to `(agent_kind, slug)` and
        takes its latest version (highest `version_number`) as the "active"
        one — the same convention the boot-time seeders already use
        (`admin/seed_agent_profiles.py`'s `_collect_prompt_version_ids`, and
        the plugins' own profile seeders); there is
        no separate `is_active` column on `agent_prompt_versions` to key off
        instead.

        Raises `LookupError` when the row genuinely doesn't exist (no
        `agent_prompts` row for the slug, or no version under it) and no
        `fallback` was given — callers that want the same "misconfigured
        tenant" semantics as a missing `prompt_version_id` should let this
        propagate rather than swallow it.

        A DB/transport error during the lookup is a DIFFERENT case and is
        **not** relabeled `LookupError`: with no `fallback`, the original
        exception propagates as itself, so a caller distinguishing
        "misconfigured tenant" from "transient failure, not the caller's
        fault" (see `endpoints/voice_live.py`'s two-tier handling of
        `_live_config_for`, which this mirrors) can tell the two apart. With
        a `fallback`, it degrades to that fallback like every other error
        path in this class.
        """
        try:
            version_id = cls._latest_version_id_for_slug(agent_kind, slug) if slug else None
        except Exception as exc:
            if fallback is None:
                raise
            logger.warning(
                "prompt_resolver: slug lookup failed for %s/%s (%s); using fallback",
                agent_kind, slug, exc,
            )
            version_id = None
        if version_id is None and fallback is None:
            raise LookupError(
                f"no agent_prompts/agent_prompt_versions row for "
                f"agent_kind={agent_kind!r} slug={slug!r}"
            )
        return cls.resolve(
            version_id,
            fallback=fallback,
            variables=variables,
            agent_kind=agent_kind,
            prompt_key=prompt_key or slug,
        )

    @classmethod
    def _latest_version_id_for_slug(cls, agent_kind: str, slug: str) -> Optional[str]:
        """(agent_kind, slug) -> latest agent_prompt_versions.prompt_version_id,
        or None if the row genuinely doesn't exist.

        Deliberately does NOT swallow DB/transport errors here (unlike
        `_lookup_override_version_id` below, which has a safe "just serve
        the active version instead" degrade path to fall back to) — there is
        no such "instead" for a caller with no `prompt_version_id` fallback
        of its own, so a query failure must propagate as itself rather than
        get relabeled "no such prompt" (`LookupError`). `resolve_by_slug`
        above is what decides, per-caller, whether to let it propagate
        further or degrade to a supplied fallback.
        """
        client = get_service_client()
        prompt_rows = (
            client.table("agent_prompts")
            .select("prompt_id")
            .eq("agent_kind", agent_kind)
            .eq("slug", slug)
            .limit(1)
            .execute()
            .data
        ) or []
        if not prompt_rows:
            return None
        version_rows = (
            client.table("agent_prompt_versions")
            .select("prompt_version_id")
            .eq("prompt_id", prompt_rows[0]["prompt_id"])
            .order("version_number", desc=True)
            .limit(1)
            .execute()
            .data
        ) or []
        return version_rows[0]["prompt_version_id"] if version_rows else None

    @classmethod
    def _lookup_override_version_id(
        cls,
        agent_kind: Optional[str],
        prompt_key: Optional[str],
    ) -> Optional[str]:
        """Map an active override entry to the concrete prompt_version_id.

        Returns None when no override is set, when the resolver caller
        didn't supply `agent_kind`/`prompt_key`, when the override has no
        matching key, or when the DB lookup fails — in every "no usable
        override" case the caller falls back to the normal active-version
        path. Lookup failures are logged at WARNING and swallowed; we'd
        rather serve the active prompt than crash the request because an
        eval-only header pointed at a deleted version.
        """
        if not agent_kind or not prompt_key:
            return None
        override = _PROMPT_OVERRIDE.get()
        if not override:
            return None
        # Override keys live in the JSON header as plain strings (the
        # resolver doesn't get to dictate the operator's JSON style),
        # version values must be ints; tolerate "7" the same as 7.
        key = f"{agent_kind}/{prompt_key}"
        raw = override.get(key)
        if raw is None:
            return None
        try:
            version_number = int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "prompt_resolver: override for %s has non-int version (%r); ignoring",
                key, raw,
            )
            return None

        try:
            client = get_service_client()
            prompt_rows = (
                client.table("agent_prompts")
                .select("prompt_id")
                .eq("agent_kind", agent_kind)
                .eq("slug", prompt_key)
                .limit(1)
                .execute()
                .data
            ) or []
            if not prompt_rows:
                logger.warning(
                    "prompt_resolver: override target prompt %s/%s missing; using active",
                    agent_kind, prompt_key,
                )
                return None
            prompt_id = prompt_rows[0]["prompt_id"]
            version_rows = (
                client.table("agent_prompt_versions")
                .select("prompt_version_id")
                .eq("prompt_id", prompt_id)
                .eq("version_number", version_number)
                .limit(1)
                .execute()
                .data
            ) or []
            if not version_rows:
                logger.warning(
                    "prompt_resolver: override v%s of %s/%s missing; using active",
                    version_number, agent_kind, prompt_key,
                )
                return None
            return version_rows[0]["prompt_version_id"]
        except Exception as exc:
            logger.warning(
                "prompt_resolver: override lookup failed for %s/%s v%s (%s); using active",
                agent_kind, prompt_key, version_number, exc,
            )
            return None


class _SafeDict(dict):
    """Dict subclass that returns `{key}` literal for missing keys.

    Used so `"hello {missing}".format_map(SafeDict({}))` returns
    `"hello {missing}"` instead of raising — matches the legacy
    tolerance baked into existing prompt files.
    """

    def __missing__(self, key: str) -> str:  # type: ignore[override]
        return "{" + key + "}"
