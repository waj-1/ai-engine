"""
Agent profile loader.

A `Profile` is the data-plane representation of an agent's runtime config:
model, prompts, tool whitelist, data sources, MCP servers, escalation policy.

Each agent kind's main profile is `<kind>_default` per environment (a
generic `custom` agent is told apart by slug instead).
At request time, agent code calls `AgentProfileLoader.load_active(...)`.

Failure semantics (all environments):

The DB is treated as the source of truth in every environment. A
missing or unreachable Supabase used to silently fall back to
file-system defaults in dev — that hid genuine configuration bugs
("why isn't my admin-portal change taking effect?") until they showed
up in staging. Now:

- **DB unreachable** (transport error, service-key bad, etc.): raises
  `ProfileLoadError`. The agent factory propagates it; FastAPI surfaces
  a 503 so the developer sees the failure immediately instead of
  silently serving the file-system prompt.
- **No active row** for `(agent_kind, environment)`: raises
  `MissingProfileError`. Fix by running the seeders (`make seed`) or
  promoting from a working env.
- **No snapshot in non-dev** for an existing row: raises
  `MissingSnapshotError`. Fix by re-clicking promote so the runtime
  has pinned config to read.

The only place fallback still applies is the in-process `invalidate()`
flow when a test or admin override explicitly sets `is_fallback=True`.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.auth import get_service_client

logger = logging.getLogger(__name__)


def _default_environment() -> str:
    env = os.getenv("ENVIRONMENT", "development").lower()
    if env in ("production", "prod"):
        return "prod"
    if env in ("staging", "stage"):
        return "staging"
    # 'dev' was retired — the engine manages staging + production only.
    # development / dev / unset all collapse to the live, editable staging env.
    return "staging"


@dataclass(frozen=True)
class Profile:
    """Runtime view of an agent_profiles row.

    `is_fallback=True` is the sentinel for "no DB row found; agent code
    should keep using its hardcoded prompts and full tool list".
    """

    profile_id: Optional[str]
    agent_kind: str
    slug: str
    environment: str
    model_main: Optional[Dict[str, Any]] = None
    model_light: Optional[Dict[str, Any]] = None
    prompt_version_ids: Dict[str, str] = field(default_factory=dict)
    # Fully-resolved prompt contents, keyed by role (e.g. "system").
    # Populated from compile-on-promote snapshots in non-dev so the
    # PromptResolver never needs to hit the target DB's
    # agent_prompt_versions table. Empty in dev → resolver falls through
    # to the live row + file-system fallback.
    resolved_prompts: Dict[str, str] = field(default_factory=dict)
    # The promote-frozen realtime model row (snapshot["voice_model"]), or None
    # in dev/staging where the live row is read instead.
    voice_model: Optional[Dict[str, Any]] = None
    tool_python_names: List[str] = field(default_factory=list)
    data_source_ids: List[str] = field(default_factory=list)
    mcp_server_ids: List[str] = field(default_factory=list)
    # Resolved MCP attachments (agent_profile_mcp_servers joined with servers
    # and bundle membership), ordered by position. Shape documented in
    # runtime/tools/_mcp_resolver.py. Loaded in dev; snapshot-fed in prod
    # (compiler extension lands in Phase 1a — until then snapshots yield []).
    mcp_attachments: List[Dict[str, Any]] = field(default_factory=list)
    # Resolved platform-skill attachments (agent_profile_skills joined with
    # skills + latest version + resources). SKILL_ENTRY shape documented in
    # core/skills.py. Live-joined in staging; snapshot-fed in prod (schema v5).
    # Tenant skills are NOT here — they resolve per request in
    # core.skills.resolve_skills_for_request.
    skill_attachments: List[Dict[str, Any]] = field(default_factory=list)
    # Delegatable downstream profiles (delegation targets), slimmed to the
    # descriptor the runtime needs to build ask_<slug> tools. Live-joined in
    # staging; snapshot-fed in prod (schema v6). Auxiliary sub-agents
    # (delegatable=false) never appear here.
    downstream_delegates: List[Dict[str, Any]] = field(default_factory=list)
    escalation_policy: Dict[str, Any] = field(default_factory=dict)
    voice_config: Dict[str, Any] = field(default_factory=dict)
    memory_config: Dict[str, Any] = field(default_factory=dict)
    is_fallback: bool = False


FALLBACK_PROFILE = Profile(
    profile_id=None,
    agent_kind="",
    slug="fallback",
    environment="dev",
    is_fallback=True,
)


# ---------------------------------------------------------------------------
# Precompiled prod config registry (the GCS "middle path")
#
# On the PROD service, `main.lifespan` loads an immutable, precompiled bundle
# from GCS (see runtime/prod_bundle.py) into this registry at boot. The prod
# read path in `_load_from_db` then serves config from here with no config-DB
# resolution. Maps `agent_kind -> {"row": <agent_profiles row>, "snapshot":
# <snapshot>}` — both, because `_profile_from_snapshot(row, snapshot, env)`
# needs both.
#
# Empty until a bundle is pushed, so the prod read path stays byte-identical
# to the DB-snapshot path until cutover (safe + reversible). Staging/dev never
# populate this — they serve live, editable rows.
_PROD_BUNDLE: Dict[str, Dict[str, Any]] = {}


def set_prod_bundle(profiles: Dict[str, Any]) -> None:
    """Install the precompiled prod config registry (boot loader / tests).

    Replaces the whole registry with `profiles` (a copy is taken so the
    caller can't mutate the live registry afterwards).
    """
    global _PROD_BUNDLE
    _PROD_BUNDLE = dict(profiles or {})


def clear_prod_bundle() -> None:
    """Drop the prod config registry (tests / rollback → DB-snapshot path)."""
    _PROD_BUNDLE.clear()


class MissingSnapshotError(RuntimeError):
    """Raised in staging/prod when the DB is reachable but no pinned
    snapshot exists for `(agent_kind, environment)`.

    The runtime treats this as fatal at boot: serving a non-dev request
    from an unpinned profile would defeat compile-on-promote. Operators
    fix it by clicking promote in dev once.
    """


class MissingProfileError(RuntimeError):
    """Raised when the DB is reachable but no active profile row exists
    for `(agent_kind, environment)`. Fix by running the seeders or
    promoting a profile into this env.
    """


class ProfileLoadError(RuntimeError):
    """Raised when the DB itself is unreachable (transport error, bad
    service key, schema missing). Caller should surface a 503 — the
    agent has no defaults to fall back on now that file-system prompts
    are no longer the source of truth.
    """


class AgentProfileLoader:
    """Process-local cache around the agent_profiles table.

    Every admin save / fork / promote path calls `invalidate()`, so same-
    process edits are instant regardless of TTL. The TTL only bounds how
    long a *cross-process* change (e.g. a control-plane promote) takes to
    propagate to a chat service. We default it to 300s (overridable via
    `AGENT_PROFILE_CACHE_TTL_SEC`) so the hot path isn't re-reading the
    profile — and rebuilding the LLM — every 60s under sustained traffic.
    """

    _CACHE_TTL_SEC = float(os.getenv("AGENT_PROFILE_CACHE_TTL_SEC", "300"))
    _cache: Dict[str, tuple[float, Profile]] = {}

    @classmethod
    def _cache_key(cls, agent_kind: str, environment: str) -> str:
        return f"{agent_kind}::{environment}"

    @classmethod
    def invalidate(cls, agent_kind: Optional[str] = None, environment: Optional[str] = None) -> None:
        """Drop entries from the in-process cache.

        With no args, clears everything. With both args, clears the
        kind-keyed `load_active` slot AND every id-keyed (`load_by_id`) or
        slug-keyed (`load_delegate`) entry whose cached Profile matches this
        (agent_kind, environment) — delegates share their parent's agent_kind
        but are cached under a separate slot, so a kind-only pop would
        otherwise leave them stale until TTL, breaking the "same-process edits
        are instant" invariant for delegates. The cache is tiny, so an O(n)
        scan is fine.
        """
        if agent_kind is None or environment is None:
            cls._cache.clear()
            return
        cls._cache.pop(cls._cache_key(agent_kind, environment), None)
        stale_keys = [
            key for key, (_, profile) in cls._cache.items()
            if profile.agent_kind == agent_kind and profile.environment == environment
        ]
        for key in stale_keys:
            cls._cache.pop(key, None)

    @classmethod
    def load_active(cls, agent_kind: str, environment: Optional[str] = None) -> Profile:
        """Resolve the active profile for `(agent_kind, environment)`.

        Returns `FALLBACK_PROFILE` on any failure — agent code MUST handle
        that case by using its file-system defaults.
        """
        env_value = environment or _default_environment()
        key = cls._cache_key(agent_kind, env_value)

        now = time.monotonic()
        cached = cls._cache.get(key)
        if cached is not None and (now - cached[0]) < cls._CACHE_TTL_SEC:
            return cached[1]

        try:
            profile = cls._load_from_db(agent_kind, env_value)
        except (MissingSnapshotError, MissingProfileError):
            # Loud, deliberate failure — re-raise without caching so the
            # next call also fails until an operator runs the seeders or
            # promotes a profile. Caching a fallback here would mask the
            # configuration bug; that's exactly what we removed.
            raise
        except Exception as exc:
            # DB unreachable / transport error / bad service key. We no
            # longer fall back silently in any environment — silent
            # fallback in dev was hiding "my admin-portal change didn't
            # take effect" bugs until they surfaced in staging. Raise a
            # typed error so the FastAPI layer can return 503.
            logger.error(
                "agent_profile: DB load failed for %s/%s (%s); refusing to "
                "serve from file-system defaults",
                agent_kind, env_value, exc,
            )
            raise ProfileLoadError(
                f"Failed to load agent_profile for {agent_kind}/{env_value}: {exc}"
            ) from exc

        cls._cache[key] = (now, profile)
        return profile

    @classmethod
    def load_version(cls, version_id: str, environment: Optional[str] = None) -> Profile:
        """Build a Profile from a specific agent_profile_versions snapshot.

        Used by eval/bake-off requests that pin a version via
        X-WAJ-Profile-Version. Reuses the same snapshot rehydration the prod
        compile-on-promote path uses. Not cached (per-request, arbitrary
        version). Raises MissingSnapshotError if the version is unknown."""
        from runtime.compiler import load_snapshot_by_version

        entry = load_snapshot_by_version(version_id)
        if not entry or not entry.get("snapshot"):
            raise MissingSnapshotError(
                f"no agent_profile_versions snapshot for version_id={version_id}"
            )
        client = get_service_client()
        prof_rows = (
            client.table("agent_profiles").select("*")
            .eq("profile_id", entry["profile_id"]).limit(1)
            .execute().data or []
        )
        row = prof_rows[0] if prof_rows else {
            "profile_id": entry["profile_id"],
            "agent_kind": entry["snapshot"].get("agent_kind"),
            "slug": entry["snapshot"].get("slug"),
        }
        return cls._profile_from_snapshot(
            row, entry["snapshot"], environment or entry["environment"]
        )

    @classmethod
    def load_pinned(cls, profile_id: str, agent_kind: str) -> Optional[Profile]:
        """The profile an operator pinned for this request (X-WAJ-Profile-Id —
        the admin Test drawer / MCP chat), built the way ITS OWN environment
        builds it: live in dev/staging (a draft copy lands in staging), the
        served/promoted snapshot in prod.

        Returns None — the caller then serves its usual profile — when the
        pin can't be honoured: unknown/inactive id, a template row, or a
        profile of a DIFFERENT agent_kind than the endpoint being called (a
        pin swaps config within a kind, never which agent class or tenant
        answers). Logged WARNING, never raised: a stale pin must not take a
        test chat down. Not cached — per-request, operator-only.
        """
        try:
            client = get_service_client()
            rows = (
                client.table("agent_profiles").select("*")
                .eq("profile_id", profile_id)
                .limit(1).execute().data
            ) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent_profile: pinned profile %s lookup failed (%s); "
                           "serving the default", profile_id, exc)
            return None
        row = rows[0] if rows else None
        # An inactive row is refused, except a draft: a fork starts inactive
        # (admin/services/profiles.py), and a draft is what a pin is for. Without
        # this a new agent's Test chat and Call answered 404 until it was
        # published (found on the public demo, 2026-11-25).
        if row and not row.get("is_active") and row.get("status") != "draft":
            row = None
        if not row or row.get("status") == "template" \
                or str(row.get("agent_kind")) != str(agent_kind):
            logger.warning(
                "agent_profile: pinned profile %s not usable for kind=%s "
                "(found kind=%s status=%s); serving the default",
                profile_id, agent_kind, (row or {}).get("agent_kind"),
                (row or {}).get("status"),
            )
            return None
        try:
            return cls._build_profile(client, row, row.get("environment") or _default_environment())
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent_profile: pinned profile %s failed to build (%s); "
                           "serving the default", profile_id, exc)
            return None

    @classmethod
    def load_by_id(cls, profile_id: str, environment: str) -> Profile:
        """Load a specific profile row by id — the delegation path.

        Unlike load_active this does not filter status='active' (delegates
        use status='specialist' so they can never shadow load_active), only
        is_active=True. Non-dev environments read the latest promoted
        snapshot for the id; dev/staging build from the live row.
        """
        key = cls._cache_key(f"id:{profile_id}", environment)
        now = time.monotonic()
        cached = cls._cache.get(key)
        if cached is not None and (now - cached[0]) < cls._CACHE_TTL_SEC:
            return cached[1]
        client = get_service_client()
        rows = (
            client.table("agent_profiles").select("*")
            .eq("profile_id", profile_id).eq("is_active", True)
            .limit(1).execute().data
        )
        if not rows:
            raise MissingProfileError(f"delegate profile {profile_id} not found/inactive")
        row = rows[0]
        profile = cls._build_profile(client, row, environment)
        cls._cache[key] = (now, profile)
        return profile

    @classmethod
    def load_delegate(cls, agent_kind: str, slug: str, environment: str) -> Profile:
        """Resolve a delegation specialist by (agent_kind, slug, environment).

        The delegation path resolves by slug, NOT by the frozen descriptor's
        profile_id, so it survives compile-on-promote across environments:
        promoting a parent freezes the delegate's *staging* profile_id into the
        snapshot, but the promote of the delegate itself mints a *new*
        target-env profile_id — a by-id lookup in prod would find no prod
        snapshot and fail. `(agent_kind, slug, environment)` is unique (the
        agent_profiles table constraint), so the slug reliably picks out the
        target-env specialist row regardless of the id remap or promote order.

        Like load_by_id this does not filter status='active' (a delegate is
        status='specialist'), only is_active=True. Non-dev reads the promoted
        snapshot; dev/staging builds live.
        """
        key = cls._cache_key(f"delegate:{agent_kind}:{slug}", environment)
        now = time.monotonic()
        cached = cls._cache.get(key)
        if cached is not None and (now - cached[0]) < cls._CACHE_TTL_SEC:
            return cached[1]
        client = get_service_client()
        rows = (
            client.table("agent_profiles").select("*")
            .eq("agent_kind", agent_kind).eq("slug", slug)
            .eq("environment", environment).eq("is_active", True)
            .limit(1).execute().data
        )
        if not rows:
            raise MissingProfileError(
                f"delegate {agent_kind}/{slug} not found/inactive in {environment}")
        row = rows[0]
        profile = cls._build_profile(client, row, environment)
        cls._cache[key] = (now, profile)
        return profile

    @classmethod
    def _load_from_db(cls, agent_kind: str, environment: str) -> Profile:
        client = get_service_client()
        # Filter status='active' so admin-created `draft` forks (see
        # POST /admin/agents/{id}/fork) stay invisible to the runtime
        # until an operator explicitly promotes them. Older DBs without
        # the status column (pre-20260520160000) fall back to is_active
        # alone via the .eq filter being a no-op on missing columns —
        # PostgREST treats unknown columns as a query error, so the
        # migration is a prerequisite for this filter to ship.
        rows = (
            client.table("agent_profiles")
            .select("*")
            .eq("agent_kind", agent_kind)
            .eq("environment", environment)
            .eq("is_active", True)
            .eq("status", "active")
            .limit(1)
            .execute()
            .data
        )
        if not rows:
            # No active row in any environment is now fatal. In dev this
            # means "run the seeders or the lifespan import failed"; in
            # staging/prod it means "promote from dev". The error message
            # tells the operator which case they're in.
            hint = (
                "promote a profile from staging"
                if environment == "prod"
                else "run `make seed` (or restart the agent service so lifespan "
                "seeders fire)"
            )
            raise MissingProfileError(
                f"no active agent_profiles row for kind={agent_kind} env={environment}; {hint}"
            )
        row = rows[0]
        return cls._build_profile(client, row, environment)

    @classmethod
    def _build_profile(cls, client, row: Dict[str, Any], environment: str) -> Profile:
        """Shared row -> Profile construction for `_load_from_db` and
        `load_by_id`.

        Contains the snapshot-vs-live branch, verbatim from the original
        `_load_from_db` body: non-dev environments read a compiled
        `agent_profile_versions` snapshot (or the precompiled prod bundle);
        dev/staging build the Profile from the live row via table joins.
        """
        # Served-version pin: an operator has pinned a specific promoted
        # snapshot to serve in this env (rollback / hold). Honored in EVERY
        # env and by both _load_from_db and load_by_id. NULL (the default for
        # every row today) skips this branch entirely → byte-for-byte the
        # unchanged resolution below.
        served_vid = row.get("served_version_id")
        if served_vid:
            from runtime.compiler import load_snapshot_by_version
            entry = load_snapshot_by_version(served_vid)
            if entry and entry.get("snapshot"):
                if entry.get("profile_id") == row.get("profile_id"):
                    return cls._profile_from_snapshot(row, entry["snapshot"], environment)
                # Ownership mismatch: the pinned version belongs to a
                # DIFFERENT profile (manual DB edit / future setter bug).
                # Serving it would silently hand back the wrong profile's
                # config on the hot chat path — treat this exactly like a
                # dangling pin and fall through to the default resolution
                # below instead of raising.
                logger.warning(
                    "agent_profile: served_version_id=%s for %s/%s belongs to "
                    "profile_id=%s, not the requested profile_id=%s; serving "
                    "default", served_vid, row.get("agent_kind"), environment,
                    entry.get("profile_id"), row.get("profile_id"),
                )
            else:
                logger.warning(
                    "agent_profile: served_version_id=%s for %s/%s not found; "
                    "serving default", served_vid, row.get("agent_kind"), environment,
                )

        # Compile-on-promote: in PRODUCTION the runtime reads a fully
        # self-contained snapshot. The snapshot carries the resolved
        # model row, prompt contents, and tool python_names — so the
        # target Supabase doesn't need duplicate agent_models /
        # agent_tools / agent_prompt_versions data for the agent to
        # serve. A missing snapshot is a refusal-to-serve: silently
        # falling through to the live row defeats the purpose of
        # compile-on-promote.
        # Staging serves the live, editable rows (edits apply immediately);
        # only production is snapshot-pinned via compile-on-promote.
        if environment == "prod":
            # Precompiled bundle first: if the GCS bundle loaded at boot has
            # this kind, serve it from the in-memory registry with zero
            # config-DB resolution. Prefer the registry's own frozen row for
            # a truer zero-DB path; fall back to the row already in scope if
            # the bundle entry somehow omits it. When the kind is NOT in the
            # registry (empty bundle / pre-cutover / staging-style single-DB),
            # fall through to the unchanged DB-snapshot path below so nothing
            # breaks and prod stays byte-identical to today.
            #
            # Bundle entries are keyed by agent_kind and hold only that
            # kind's ACTIVE profile — but `load_by_id` (the delegation path)
            # can be asked to build a *different* profile_id of the same
            # agent_kind (a `status='specialist'` delegate row). Serving the
            # bundle entry in that case would silently hand back the
            # PARENT's profile instead of the delegate's, so only use the
            # entry when it actually represents THIS row (same profile_id);
            # otherwise fall through to the normal snapshot/DB path below.
            entry = _PROD_BUNDLE.get(row.get("agent_kind"))
            if entry is not None:
                entry_row = entry.get("row") or {}
                if entry_row.get("profile_id") == row.get("profile_id"):
                    bundle_snapshot = entry.get("snapshot")
                    if bundle_snapshot:
                        return cls._profile_from_snapshot(
                            entry_row or row, bundle_snapshot, environment,
                        )
                else:
                    logger.debug(
                        "agent_profile: prod bundle entry for kind=%s belongs to "
                        "profile_id=%s, not the requested profile_id=%s; falling "
                        "through to the DB snapshot path (delegate/specialist row?)",
                        row.get("agent_kind"), entry_row.get("profile_id"),
                        row.get("profile_id"),
                    )

            from runtime.compiler import load_active_snapshot
            snapshot = load_active_snapshot(row["profile_id"], environment)
            if not snapshot:
                raise MissingSnapshotError(
                    f"no agent_profile_versions snapshot for profile_id={row['profile_id']} "
                    f"env={environment}; re-run promote so the runtime can pin its config"
                )
            return cls._profile_from_snapshot(row, snapshot, environment)

        # Dev path: live row + table lookups (matches today's behaviour).
        model_main = cls._fetch_model(client, row.get("model_main_id"), environment)

        tool_ids = row.get("tool_ids") or []
        tool_python_names: List[str] = []
        if tool_ids:
            tool_rows = (
                client.table("agent_tools")
                .select("python_name")
                .in_("tool_id", tool_ids)
                .execute()
                .data
                or []
            )
            tool_python_names = [t["python_name"] for t in tool_rows if t.get("python_name")]

        return Profile(
            profile_id=row.get("profile_id"),
            agent_kind=row["agent_kind"],
            slug=row["slug"],
            environment=row["environment"],
            model_main=model_main,
            # Light model retired: loader ignores model_light_id (the column
            # is vestigial). Auxiliary calls fall back to the main LLM.
            model_light=None,
            prompt_version_ids=row.get("prompt_version_ids") or {},
            tool_python_names=tool_python_names,
            data_source_ids=[str(x) for x in (row.get("data_source_ids") or [])],
            mcp_server_ids=[str(x) for x in (row.get("mcp_server_ids") or [])],
            mcp_attachments=_fetch_mcp_attachments(client, row.get("profile_id")),
            skill_attachments=_fetch_skill_attachments_safe(client, row.get("profile_id")),
            downstream_delegates=_fetch_downstream_delegates(
                client, row.get("downstream_profile_ids")),
            escalation_policy=row.get("escalation_policy") or {},
            voice_config=row.get("voice_config") or {},
            memory_config=row.get("memory_config") or {},
            is_fallback=False,
        )

    @classmethod
    def _profile_from_snapshot(
        cls, row: Dict[str, Any], snapshot: Dict[str, Any], environment: str,
    ) -> Profile:
        """Build a Profile entirely from a self-contained snapshot.

        Used in non-dev environments where the snapshot carries
        denormalized model rows, tool python_names, and prompt contents
        so the loader needs zero further DB lookups. This is what makes
        cross-env promotion to a Supabase project without replicated
        models/tools/prompts rows actually work.

        The fields `resolved_prompts` and `tool_python_names` are taken
        straight from the snapshot. `prompt_version_ids` is kept too so
        PromptResolver can still cache by id if a runtime cares.
        """
        return Profile(
            profile_id=row.get("profile_id"),
            agent_kind=snapshot.get("agent_kind") or row["agent_kind"],
            slug=snapshot.get("slug") or row["slug"],
            environment=environment,
            model_main=snapshot.get("model_main"),
            # Light model retired: ignore any model_light a legacy snapshot
            # still carries. Auxiliary calls fall back to the main LLM.
            model_light=None,
            prompt_version_ids=snapshot.get("prompt_version_ids") or {},
            resolved_prompts=snapshot.get("prompt_contents") or {},
            voice_model=snapshot.get("voice_model") or None,
            tool_python_names=list(snapshot.get("tool_python_names") or []),
            data_source_ids=[
                str(x) for x in (snapshot.get("data_source_ids") or [])
            ],
            mcp_server_ids=[
                str(x) for x in (snapshot.get("mcp_server_ids") or [])
            ],
            mcp_attachments=list(snapshot.get("mcp_attachments") or []),
            skill_attachments=list(snapshot.get("skill_attachments") or []),
            downstream_delegates=list(snapshot.get("downstream_delegates") or []),
            escalation_policy=snapshot.get("escalation_policy") or {},
            voice_config=snapshot.get("voice_config") or {},
            memory_config=snapshot.get("memory_config") or {},
            is_fallback=False,
        )

    @staticmethod
    def _fetch_model(
        client, model_id: Optional[str], environment: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Resolve a model row, preferring an env-scoped sibling if one
        exists.

        With `agent_models.environment` (added in 20260519200000) the
        same logical model name can carry per-env tuning knobs. The
        profile pins `model_main_id` at promote time, but if an admin
        later "promotes" the model itself (creates an env-specific
        sibling), that sibling should win for callers in that env.
        Falls back to the pinned row when no env-specific sibling
        exists.
        """
        if not model_id:
            return None
        base = (
            client.table("agent_models")
            .select("*")
            .eq("model_id", model_id)
            .limit(1)
            .execute()
            .data
        )
        if not base:
            return None
        row = base[0]
        if not environment or row.get("environment") == environment:
            return row
        # Look for a same-named sibling tagged for this environment.
        siblings = (
            client.table("agent_models")
            .select("*")
            .eq("name", row.get("name"))
            .eq("environment", environment)
            .eq("is_active", True)
            .limit(1)
            .execute()
            .data
        )
        return siblings[0] if siblings else row

    @staticmethod
    def fetch_model(
        client, model_id: Optional[str], environment: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Public alias for `_fetch_model`, for callers outside this module
        (the live-voice config resolver) that need the same env-scoped
        sibling behaviour."""
        return AgentProfileLoader._fetch_model(client, model_id, environment)


def _fetch_skill_attachments_safe(client, profile_id: str) -> List[Dict[str, Any]]:
    """Deferred import of core.skills (mirrors the compiler's late import
    pattern) — skills lookups must degrade to [] like MCP attachments do."""
    try:
        from core.skills import fetch_skill_attachments
        return fetch_skill_attachments(client, profile_id)
    except Exception as exc:  # pragma: no cover — import failure only
        logger.warning("agent_profile: skill attachment lookup failed for %s (%s)",
                       profile_id, exc)
        return []


def _fetch_downstream_delegates(client, downstream_ids) -> List[Dict[str, Any]]:
    """downstream_profile_ids -> ordered delegate descriptors.

    Only delegatable+active rows qualify; any failure degrades to [] (same
    posture as the MCP/skill attachment fetchers)."""
    ids = [str(x) for x in (downstream_ids or []) if x]
    if not ids:
        return []
    try:
        rows = (
            client.table("agent_profiles")
            .select("profile_id, slug, display_name, description, agent_kind, delegatable, is_active")
            .in_("profile_id", ids).execute().data or []
        )
        by_id = {r["profile_id"]: r for r in rows
                 if r.get("delegatable") and r.get("is_active", True)}
        return [
            {"profile_id": r["profile_id"], "slug": r.get("slug"),
             "display_name": r.get("display_name"), "description": r.get("description"),
             "agent_kind": r.get("agent_kind")}
            for r in (by_id[i] for i in ids if i in by_id)
        ]
    except Exception as exc:  # pragma: no cover — transport failure only
        logger.warning("agent_profile: delegate lookup failed (%s)", exc)
        return []


def _fetch_mcp_attachments(client, profile_id: str) -> List[Dict[str, Any]]:
    """agent_profile_mcp_servers -> denormalized attachment dicts.

    Any failure returns [] (logged): tool resolution must degrade, never
    crash an agent turn (same posture as runtime/tools/_mcp_resolver.py).
    """
    try:
        att_rows = (
            client.table("agent_profile_mcp_servers").select("*")
            .eq("profile_id", profile_id).execute().data or []
        )
        if not att_rows:
            return []
        server_ids = [r["mcp_server_id"] for r in att_rows]
        server_rows = (
            client.table("agent_mcp_servers").select("*")
            .in_("mcp_server_id", server_ids).eq("is_active", True).execute().data or []
        )
        servers = {s["mcp_server_id"]: s for s in server_rows}

        bundle_ids = [sid for sid, s in servers.items() if s.get("source") == "bundle"]
        members_by_server: Dict[str, List[Dict[str, Any]]] = {}
        if bundle_ids:
            member_rows = (
                client.table("agent_mcp_tools").select("*")
                .in_("mcp_server_id", bundle_ids).execute().data or []
            )
            tool_ids = [m["tool_id"] for m in member_rows]
            tool_rows = (
                client.table("agent_tools").select("*")
                .in_("tool_id", tool_ids).eq("is_deprecated", False).execute().data or []
            ) if tool_ids else []
            tools_by_id = {t["tool_id"]: t for t in tool_rows}
            for m in sorted(member_rows, key=lambda r: r.get("position") or 0):
                tool = tools_by_id.get(m["tool_id"])
                if tool is None:
                    continue  # deprecated or vanished
                members_by_server.setdefault(m["mcp_server_id"], []).append({
                    "tool": tool,
                    "context_fields": m.get("context_fields"),
                    "position": m.get("position") or 0,
                })

        attachments: List[Dict[str, Any]] = []
        for att in sorted(att_rows, key=lambda r: r.get("position") or 0):
            server = servers.get(att["mcp_server_id"])
            if server is None:
                continue  # inactive or deleted
            attachments.append({
                "mcp_server": server,
                "tool_subset": att.get("tool_subset"),
                "position": att.get("position") or 0,
                "member_tools": members_by_server.get(att["mcp_server_id"], []),
            })
        return attachments
    except Exception as exc:
        logger.warning("agent_profile: mcp attachment lookup failed for %s (%s)", profile_id, exc)
        return []


def external_mcp_endpoint_for_kind(
    agent_kind: str, *, env_var: str, default: str,
    environment: Optional[str] = None,
) -> str:
    """Resolve an agent kind's external MCP endpoint from portal-managed data.

    Reads the active profile's attached `source='external'` MCP row endpoint
    (from mcp_attachments — frozen in the prod snapshot, so no live join in the
    request path). Falls back to the env var, then `default`, on any miss or
    error — so with no attached row this is byte-identical to a plain
    `os.getenv(env_var, default)` (behavior-preserving).
    """
    try:
        profile = AgentProfileLoader.load_active(agent_kind, environment)
        for att in (profile.mcp_attachments or []):
            server = att.get("mcp_server") or {}
            if server.get("source") == "external" and server.get("endpoint"):
                return server["endpoint"]
    except Exception as exc:
        logger.warning(
            "external_mcp_endpoint_for_kind(%s): profile/attachment lookup failed "
            "(%s); using env fallback", agent_kind, exc,
        )
    return os.getenv(env_var, default)
