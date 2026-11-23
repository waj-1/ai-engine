"""Profile update/fork service — the single implementation behind both the
portal's POST /admin/profiles/{id} and /admin/agents/{id}/fork, and the MCP
configure_profile / fork_profile tools.

Form parsing (checkbox coercion, JSON decoding, voice-config assembly)
stays in the callers — this module only ever sees an already-normalized
payload dict, so it can be driven identically from an HTML form or an MCP
tool call.
"""
from __future__ import annotations

import copy
import logging
import uuid
from typing import Any, Dict, List, Optional

from admin.audit import record_change

logger = logging.getLogger(__name__)


def deep_merge_voice_config(existing: Optional[Dict[str, Any]],
                            patch: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge `patch` onto `existing` for the `voice_config`
    JSONB column, WITHOUT clobbering sibling keys the way a plain
    `{**existing, **patch}` (or a bare column replace) would.

    Rules, applied key by key at every nesting level:
      - `patch[key] is None` DELETES that key from the result (explicit
        null-delete — the only way to remove a lever via the patch path;
        omitting a key from the patch simply leaves it untouched).
      - `patch[key]` is a dict AND `existing[key]` is also a dict: merged
        recursively (so e.g. a patch touching only `voice_by_language` leaves
        every sibling key like `barge_in_enabled` or `agent_profile_slug`
        intact).
      - Anything else (scalar, list, or a dict replacing a non-dict / a
        non-dict replacing a dict): REPLACES that key wholesale — lists and
        scalars are never merged element-wise.

    Pure function: never mutates `existing` or `patch`; returns a new dict.
    This is the MCP-tool analogue of what the voice cockpit's writer
    (`admin/screens/agent_voice.py::voice_config_from_form` — the single
    portal writer of the column) does for its known form fields — except
    this is a generic, arbitrary-shape merge
    suited to `update_profile(voice_config_patch=...)` setting a single
    lever (e.g. `{"barge_in_enabled": true}`) without an agent having to
    first read, then resend, the entire column.

    SHAPE-AGNOSTIC, which since the 2026-08-17 unification is a hazard worth
    naming here rather than in the caller alone: `voice_config` is FLAT, and
    this function will happily merge in a nested `{"live": {...}}` patch. Doing
    so does not "also" set the flat keys — it HIDES them, because the read shim
    (`voice/live/config.py::_voice_block`) resolves a nested block alone and
    ignores the top level. Patch flat keys; a nested patch is a config outage.
    """
    result: Dict[str, Any] = copy.deepcopy(existing) if existing else {}
    for key, val in patch.items():
        if val is None:
            result.pop(key, None)
        elif isinstance(val, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge_voice_config(result[key], val)
        else:
            result[key] = copy.deepcopy(val) if isinstance(val, (dict, list)) else val
    return result


class ProfileNotFoundError(Exception):
    pass


class TemplateProtectedError(Exception):
    """Raised when a caller tries to edit or promote a status='template' row.
    Templates are a protected catalog — fork them instead."""


class DualActiveProfileError(Exception):
    """Raised when an update would leave two `status='active'` rows for the
    same (agent_kind, environment) pair.

    `core.agent_profile.AgentProfileLoader` resolves the serving row with
    `.eq("status", "active")...limit(1)` and no deterministic ORDER BY, so
    a second active row for the same (agent_kind, environment) makes it
    non-deterministic which one the runtime actually serves. Deactivate
    (or archive) the conflicting row before activating a new one."""


# Status values a caller may set via update_profile_row. 'template' is
# deliberately excluded — that status is exclusively seeder-managed (see
# admin/seed_templates.py); an operator forks a template into a new row
# rather than ever promoting one in place via this path.
# The generic, configuration-only agent kind (runtime/agents/custom.py). A
# literal rather than an import: this module must stay importable without the
# runtime package's model dependencies.
_CUSTOM_AGENT_KIND = "custom"

ALLOWED_STATUS_VALUES = {"active", "draft", "archived", "specialist"}

# Payload keys `update_profile_row` accepts. Anything else raises
# ValueError — this is the one behavior change over the pre-extraction
# handler, which trusted whatever admin/screens/agents.py's form parsing
# produced.
ALLOWED_PROFILE_FIELDS = {
    "display_name",
    "description",
    "environment",
    "status",
    "model_main_id",
    "model_light_id",
    "endpoint_path",
    "is_active",
    "delegatable",
    "prompt_version_ids",
    "voice_config",
    # Written by the voice cockpit's brain panel
    # (POST /admin/agents/{id}/voice/brain) onto the SPECIALIST row — the
    # only portal editor `memory_config` has. The panel merges the `enabled`
    # toggle onto the existing dict so sibling keys (retrieval, window, …)
    # survive; see admin/screens/agent_voice.py::brain_update_from_form.
    "memory_config",
    "tool_ids",
    "data_source_ids",
    "mcp_server_ids",
    "downstream_profile_ids",
    # The agent's user-defined category (admin/services/agent_types.py), or
    # None for uncategorised. Organisational only: the runtime never reads it.
    "agent_type_id",
}

# Fields copied verbatim from the source row into a fork. Anything not in
# this list (profile_id, created_at, updated_at, created_by, updated_by,
# status, environment, display_name) is intentionally overridden or left
# for Postgres to default. Single source of truth — profile_fork_handlers
# imports this rather than redefining it.
_FORK_COPY_FIELDS = (
    "agent_kind",
    "slug",
    "description",
    "model_main_id",
    "model_light_id",
    "tool_ids",
    "data_source_ids",
    "mcp_server_ids",
    "downstream_profile_ids",
    "parent_profile_id",
    "endpoint_path",
    "prompt_version_ids",
    # Explicit since the voice-config unification (spec D11). It was already
    # being copied — but only through the `untouched` verbatim fallback below,
    # which exists to catch columns nobody listed and logs a WARNING every time
    # it fires. So every fork of a voice-configured profile emitted a warning
    # about a field that was in fact meant to be copied, training operators to
    # ignore the one signal that says "a column was added and nobody decided
    # about it". Copying it is right: a fork is a config experiment, and a fork
    # whose duplex voice silently reverted to `enabled: false` would look like
    # the voice edge had broken.
    "voice_config",
    "escalation_policy",
    "additional_agent_kinds",
    "is_active",
    # A copy of an agent starts in the same category as the original.
    "agent_type_id",
)


def _safe_list(execute_result: Any) -> List[Dict[str, Any]]:
    return execute_result.data or []


def invalidate_profile_loader(agent_kind: str, environment: str) -> None:
    """Drop the cached AgentProfileLoader entry for `(agent_kind, environment)`.

    Thin wrapper around `AgentProfileLoader.invalidate` so callers (this
    module, and later MCP configure-tools) don't need to import
    `core.agent_profile` directly.
    """
    from core.agent_profile import AgentProfileLoader

    AgentProfileLoader.invalidate(agent_kind, environment)


def update_profile_row(*, profile_id: str, payload: Dict[str, Any],
                        actor_user_id: str) -> Dict[str, Any]:
    """Apply a validated field update to an `agent_profiles` row.

    `payload` must already be normalized (form-parsing / JSON decoding /
    checkbox coercion is the caller's job — see admin/router.py's
    `profile_update` for the HTTP form flavor). Validates the payload's
    keys against `ALLOWED_PROFILE_FIELDS`, does the before/after lookup,
    invalidates the profile-loader cache for both the row's original
    environment and (if changed) its new one, and audits the change.

    Raises `ValueError` on an unknown payload key or a `status` outside
    `ALLOWED_STATUS_VALUES`, `ProfileNotFoundError` if `profile_id` doesn't
    exist, `TemplateProtectedError` if the target row is a status='template'
    row, and `DualActiveProfileError` if setting `status='active'` would
    create a second active row for the same (agent_kind, environment).
    Returns the merged after-dict.
    """
    unknown = set(payload) - ALLOWED_PROFILE_FIELDS
    if unknown:
        raise ValueError(
            f"unknown profile field(s): {', '.join(sorted(unknown))}"
        )

    if "status" in payload and payload["status"] not in ALLOWED_STATUS_VALUES:
        raise ValueError(
            f"status must be one of {sorted(ALLOWED_STATUS_VALUES)}, got "
            f"{payload['status']!r} ('template' is seeder-managed and can't "
            "be set here — fork a template instead via templates(action='use'))"
        )

    from core.auth import admin_service_client

    client = admin_service_client()
    before_rows = _safe_list(
        client.table("agent_profiles").select("*").eq("profile_id", profile_id).execute()
    )
    if not before_rows:
        raise ProfileNotFoundError(f"profile not found: {profile_id}")
    before = before_rows[0]
    if before.get("status") == "template":
        raise TemplateProtectedError(
            "templates are read-only — fork one with templates(action='use')"
        )

    # Dual-active guard: only relevant when this update is the transition
    # INTO 'active' (an already-active row saving unrelated fields shouldn't
    # re-check itself). Scoped to the row's target (agent_kind, environment)
    # — agent_kind is immutable (not in ALLOWED_PROFILE_FIELDS), environment
    # may be changing in this same payload, so use the post-update value.
    #
    # Generic (`custom`) agents are the exception to both halves of this. Every
    # other kind has ONE serving profile, picked by `load_active`; custom
    # agents are many per kind, each addressed by its own slug, so several are
    # live at once and there is no conflict to guard. And nothing else flips a
    # custom agent's `is_active` (a fork starts inactive, and the portal's
    # Settings tab only writes `status`), so without syncing it here a custom
    # agent set to Active stayed unservable for good — found on a fresh stack
    # 2026-11-22: status='active', is_active=false, /v1/agents/{slug}/chat 404.
    is_custom = before.get("agent_kind") == _CUSTOM_AGENT_KIND
    if is_custom and "status" in payload and "is_active" not in payload:
        payload["is_active"] = payload["status"] in ("active", "specialist")
    if (not is_custom and payload.get("status") == "active"
            and before.get("status") != "active"):
        target_env = payload.get("environment") or before.get("environment")
        conflict_rows = _safe_list(
            client.table("agent_profiles").select("*")
            .eq("agent_kind", before["agent_kind"])
            .eq("environment", target_env)
            .eq("status", "active")
            .eq("is_active", True)
            .execute()
        )
        conflict = next(
            (r for r in conflict_rows if r.get("profile_id") != profile_id), None
        )
        if conflict:
            raise DualActiveProfileError(
                f"{before['agent_kind']!r}/{target_env!r} already has an "
                f"active profile: {conflict.get('slug') or conflict.get('profile_id')!r} "
                "— deactivate or archive it before activating this one"
            )

    # A category must exist before an agent is put in it. Checked here, not
    # left to the foreign key, so the caller gets a sentence instead of a
    # constraint name (and so it holds where the constraint is missing).
    if payload.get("agent_type_id"):
        found = _safe_list(
            client.table("agent_types").select("agent_type_id")
            .eq("agent_type_id", payload["agent_type_id"]).limit(1).execute()
        )
        if not found:
            raise ValueError(f"no category with id {payload['agent_type_id']!r}")

    client.table("agent_profiles").update(payload).eq("profile_id", profile_id).execute()
    if "agent_type_id" in payload:
        # The category is about the agent, not one environment's row: its
        # promoted sibling follows at once, with no re-promote.
        from admin.services.agent_types import sync_profile_siblings

        sync_profile_siblings(client, before, payload["agent_type_id"])
    invalidate_profile_loader(before["agent_kind"], before["environment"])
    if payload.get("environment") and payload["environment"] != before["environment"]:
        invalidate_profile_loader(before["agent_kind"], payload["environment"])

    after = {**before, **payload}
    record_change(
        entity_type="agent_profiles",
        entity_id=profile_id,
        action="update",
        actor_user_id=actor_user_id,
        before=before,
        after=after,
    )
    return after


# Per-profile junction tables a fork must carry: (table, the id column besides
# profile_id, the extra columns copied verbatim).
_FORK_JUNCTIONS = (
    ("agent_profile_mcp_servers", "mcp_server_id", ("tool_subset", "position")),
    ("agent_profile_skills", "skill_id", ("position",)),
)


def _copy_profile_attachments(client, src_profile_id: str,
                              new_profile_id: str) -> Dict[str, Any]:
    """Copy `src_profile_id`'s toolset (MCP server/bundle) and skill
    attachments onto `new_profile_id`. Upserts on the junction primary key so
    a boot-seeder pass that attached the new row first can't fail the fork —
    the original's own attachment (and its tool_subset) wins. Returns
    `{"copied_mcp_servers": n, "copied_skills": n}`."""
    out: Dict[str, Any] = {}
    for table, id_col, extra_cols in _FORK_JUNCTIONS:
        rows = _safe_list(
            client.table(table).select("*").eq("profile_id", src_profile_id).execute()
        )
        copies = []
        for r in rows:
            if not r.get(id_col):
                continue
            row = {"profile_id": new_profile_id, id_col: r[id_col]}
            for c in extra_cols:
                if c in r:
                    row[c] = copy.deepcopy(r[c])
            copies.append(row)
        if copies:
            client.table(table).upsert(
                copies, on_conflict=f"profile_id,{id_col}"
            ).execute()
        key = "copied_mcp_servers" if table == "agent_profile_mcp_servers" else "copied_skills"
        out[key] = len(copies)
    return out


def fork_profile_row(*, profile_id: str, actor_user_id: str,
                     display_name: str | None = None, slug: str | None = None,
                     status: str = "draft", exact_slug: bool = False) -> Dict[str, Any]:
    """Clone a profile row into a staging fork for iteration.

    The fork always lands in `staging` (the live, editable env), so the
    loader's `status='active'` filter hides it from the runtime until an
    operator promotes it. By default it lands in `status="draft"` with a
    "(fork)"-suffixed display name and a slug uniqueness suffix; callers
    (e.g. the templates "use" flow) may pass `display_name`/`slug`/`status`
    to control those instead. Slug is suffixed (whether the caller's
    own or the source's) to avoid the `(agent_kind, slug, environment)`
    unique constraint colliding with the source row — UNLESS `exact_slug`
    is set with an explicit `slug`, in which case that slug is used
    verbatim (the caller deliberately chose a stable name — e.g. a
    delegation specialist whose `ask_<slug>` tool name must be
    predictable). A genuine collision then surfaces as a DB
    unique-constraint error rather than being silently renamed.

    Raises `ProfileNotFoundError` if `profile_id` doesn't exist. Returns
    the inserted row (including its new `profile_id`) so callers can build
    a redirect / response around it.
    """
    from core.auth import admin_service_client

    client = admin_service_client()
    rows = _safe_list(
        client.table("agent_profiles").select("*").eq("profile_id", profile_id).execute()
    )
    if not rows:
        raise ProfileNotFoundError(f"profile not found: {profile_id}")
    src = rows[0]

    new_profile_id = str(uuid.uuid4())
    new_row: Dict[str, Any] = {"profile_id": new_profile_id}
    for f in _FORK_COPY_FIELDS:
        if f in src and src[f] is not None:
            # Deep-copied: lists are uuid[] but the jsonb columns
            # (prompt_version_ids, voice_config, escalation_policy) are
            # dicts, and a shared dict would let a later in-process edit of
            # the copy's pins rewrite the original's.
            new_row[f] = copy.deepcopy(src[f])

    # Forced overrides — these win over anything copied from src.
    new_row["environment"] = "staging"
    new_row["status"] = status
    # A fork of a delegatable specialist must land inert: delegation is a
    # capability grant (exposes the fork to its parent as an ask_<slug>
    # tool), and copying it verbatim would let a fork of an untested draft
    # silently inherit live delegation on insert. Explicit here (rather
    # than relying on the "untouched" verbatim-copy fallback below) so it
    # can never be re-introduced by a future column added to that fallback.
    new_row["delegatable"] = False
    if display_name:
        new_row["display_name"] = display_name
    else:
        new_row["display_name"] = (
            f"{src.get('display_name') or src.get('agent_kind')} (fork)"
        )
    if exact_slug and slug:
        # Deliberate, stable slug (e.g. a delegation specialist). Use it
        # verbatim; the DB unique constraint fails loudly on a real collision.
        new_row["slug"] = slug
    else:
        base_slug = slug or new_row.get("slug")
        if base_slug:
            new_row["slug"] = f"{base_slug}-fork-{new_profile_id[:8]}"
    new_row["created_by"] = actor_user_id
    new_row["updated_by"] = actor_user_id

    # Warn loud (but don't fail) if a NOT NULL column on the source row
    # wasn't in our copy list — better to surface the gap than to silently
    # drop config and let the operator discover it later.
    untouched = [
        k for k, v in src.items()
        if v is not None
        and k not in new_row
        and k not in {"created_at", "updated_at"}
    ]
    if untouched:
        logger.warning(
            "fork_profile_row: source profile %s had fields %s not in copy "
            "list; copying verbatim", profile_id, untouched,
        )
        for k in untouched:
            new_row[k] = copy.deepcopy(src[k])

    inserted = client.table("agent_profiles").insert(new_row).execute()
    inserted_row = _safe_list(inserted)
    result = inserted_row[0] if inserted_row else new_row

    # Toolsets and skills are NOT columns on the row (knowledge and sub-agents
    # are — `data_source_ids` / `downstream_profile_ids`, copied above). They
    # live in the junction tables, keyed by profile_id, so a row-only copy
    # left the fork with 0 actions — or, for a kind the boot seeder attaches
    # (`seed_builtin_mcps._attach_profiles`), with the kind's bundle at a
    # subset rebuilt from `tool_ids` instead of the original's own choice.
    _copy_profile_attachments(client, profile_id, new_profile_id)

    invalidate_profile_loader(src["agent_kind"], src["environment"])
    invalidate_profile_loader(src["agent_kind"], "staging")

    record_change(
        entity_type="agent_profiles",
        entity_id=new_profile_id,
        action="fork",
        actor_user_id=actor_user_id,
        before=src,
        after=new_row,
    )
    return result
