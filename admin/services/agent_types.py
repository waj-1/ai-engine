"""User-defined agent categories (`agent_types`).

A category is a row, never an `agent_kind` and never code: it groups, filters
and reports on agents ("Support", "Sales", "Internal tools") and has no effect
on how an agent runs. The runtime never reads it. `agent_kind` still names the
code that runs a profile, and a generic agent is still resolved by
`(custom, slug, environment)`.

One implementation behind the portal's Agents › Categories screen
(`admin/agent_type_handlers.py`), the category pickers in the New agent wizard
and an agent's Settings tab, and the `agent_types` admin MCP tool.

Decisions worth knowing before changing this:

- **Not part of a promote snapshot.** `runtime.compiler._SHALLOW_FIELDS` does
  not list `agent_type_id`, so a rename, a recolour or a move between
  categories shows at once and needs no re-promote, and restoring an older
  version of an agent leaves its category alone.
- **A category belongs to the agent, not to one environment's row.** A
  promoted agent is two `agent_profiles` rows with the same
  `(agent_kind, slug)`. `set_profile_type` writes every such row in the
  database it is talking to, so the staging and live lists never disagree.
  Where each environment has its own database, promote carries the category
  across by slug (`carry_to`).
- **Deleting a category never deletes an agent.** Its agents become
  uncategorised. The foreign key is `ON DELETE SET NULL`; `delete_type` also
  clears the column itself first, so the result does not depend on the
  constraint being there.
- **A fresh install has no categories**, and nothing here seeds any.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from admin.audit import record_change

logger = logging.getLogger(__name__)

TABLE = "agent_types"
PK = "agent_type_id"

# The fixed palette: colour KEYS, mapped to chip styles by the portal
# (`.chip-cat-<key>` in admin/static/portal.css). Also the CHECK constraint in
# supabase/migrations/20261123040000_agent_types.sql; keep the three in step.
PALETTE: tuple = ("slate", "teal", "blue", "violet", "pink", "red", "amber", "green")
DEFAULT_COLOR = "slate"

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
MAX_NAME = 60
MAX_DESCRIPTION = 280

# What `update_type` may change. The slug is deliberately not here: it is the
# handle assistants and snapshots use, so it stays put when the name changes.
UPDATABLE_FIELDS = ("display_name", "description", "color", "sort_order")


class AgentTypeError(ValueError):
    """A category request that cannot be carried out as asked."""


class AgentTypeNotFoundError(AgentTypeError):
    pass


def _rows(resp: Any) -> List[Dict[str, Any]]:
    return getattr(resp, "data", None) or []


def _client(client: Any = None) -> Any:
    if client is not None:
        return client
    from core.auth import admin_service_client

    return admin_service_client()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(name: str) -> str:
    """ "Internal tools" -> `internal-tools`. A name with no Latin letters or
    digits (an Arabic name, say) gives "", and the caller picks a fallback."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:48].strip("-")
    if slug and not slug[0].isalpha():
        slug = f"c-{slug}"[:48].strip("-")
    return slug


def _clean_name(value: Any) -> str:
    name = " ".join(str(value or "").split())
    if not name:
        raise AgentTypeError("a category needs a name")
    if len(name) > MAX_NAME:
        raise AgentTypeError(f"the name is too long (at most {MAX_NAME} characters)")
    return name


def _clean_description(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if len(text) > MAX_DESCRIPTION:
        raise AgentTypeError(
            f"the description is too long (at most {MAX_DESCRIPTION} characters)")
    return text or None


def _clean_color(value: Any) -> str:
    color = str(value or "").strip().lower() or DEFAULT_COLOR
    if color not in PALETTE:
        raise AgentTypeError(
            f"colour must be one of: {', '.join(PALETTE)} (got {value!r})")
    return color


def _clean_sort_order(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise AgentTypeError("sort_order must be a whole number") from None


def _all(client: Any) -> List[Dict[str, Any]]:
    rows = _rows(client.table(TABLE).select("*").execute())
    return sorted(rows, key=lambda r: (r.get("sort_order") or 0,
                                       (r.get("display_name") or "").lower()))


def agent_counts(client: Any = None, *, kinds: Optional[set] = None) -> Dict[str, int]:
    """{agent_type_id: number of AGENTS in it}.

    An agent promoted to a second environment is two rows; it is counted
    once, by `(agent_kind, slug)`. Templates are not agents and cannot carry
    a category. `kinds` narrows the count to those agent kinds (a scoped
    build's Agents list shows only its own).
    """
    client = _client(client)
    rows = _rows(client.table("agent_profiles")
                 .select("profile_id, agent_kind, slug, status, agent_type_id").execute())
    seen: Dict[str, set] = {}
    for r in rows:
        type_id = r.get("agent_type_id")
        if not type_id or r.get("status") == "template":
            continue
        if kinds is not None and r.get("agent_kind") not in kinds:
            continue
        seen.setdefault(type_id, set()).add(
            (r.get("agent_kind"), r.get("slug") or r.get("profile_id")))
    return {type_id: len(agents) for type_id, agents in seen.items()}


def list_types(client: Any = None, *, with_counts: bool = True,
               kinds: Optional[set] = None) -> List[Dict[str, Any]]:
    """Every category in display order, each with `agent_count`."""
    client = _client(client)
    rows = [dict(r) for r in _all(client)]
    if with_counts:
        counts = agent_counts(client, kinds=kinds)
        for r in rows:
            r["agent_count"] = counts.get(r.get(PK), 0)
    return rows


def types_by_id(client: Any = None) -> Dict[str, Dict[str, Any]]:
    """{agent_type_id: row}, for labelling agents. Never raises: a database
    that has not had the migration yet simply has no categories."""
    try:
        return {r[PK]: r for r in list_types(client, with_counts=False) if r.get(PK)}
    except Exception as exc:  # noqa: BLE001 — a chip must not 500 a list
        logger.warning("agent_types: lookup failed (%s)", exc)
        return {}


def get_type(ref: str, client: Any = None) -> Dict[str, Any]:
    """One category by its slug or its id. Raises AgentTypeNotFoundError."""
    client = _client(client)
    ref = (ref or "").strip()
    if not ref:
        raise AgentTypeNotFoundError("no category given")
    column = PK if _UUID_RE.match(ref) else "slug"
    value = ref if column == PK else ref.lower()
    found = _rows(client.table(TABLE).select("*").eq(column, value).limit(1).execute())
    if not found:
        raise AgentTypeNotFoundError(f"no category {ref!r}")
    return found[0]


def create_type(*, display_name: str, slug: str = "", description: str = "",
                color: str = "", sort_order: Optional[int] = None,
                actor_user_id: Optional[str], client: Any = None) -> Dict[str, Any]:
    """Add a category. The slug is made from the name when none is given; a
    new category goes to the end of the list unless `sort_order` places it."""
    client = _client(client)
    name = _clean_name(display_name)
    slug = (slug or "").strip().lower() or slugify(name)
    existing = _all(client)
    taken = {r.get("slug") for r in existing}
    if not slug:
        n = 1
        while f"category-{n}" in taken:
            n += 1
        slug = f"category-{n}"
    if not _SLUG_RE.match(slug):
        raise AgentTypeError(
            "the slug must start with a letter and use only lowercase letters, "
            "digits and hyphens (at most 48 characters)")
    if slug in taken:
        raise AgentTypeError(f"a category with the slug {slug!r} already exists")
    if name.lower() in {(r.get("display_name") or "").lower() for r in existing}:
        raise AgentTypeError(f"a category named {name!r} already exists")
    row: Dict[str, Any] = {
        # Made here rather than left to the column default, so the caller has
        # the id without depending on what the insert echoes back.
        PK: str(uuid.uuid4()),
        "slug": slug,
        "display_name": name,
        "description": _clean_description(description),
        "color": _clean_color(color),
        "sort_order": (_clean_sort_order(sort_order) if sort_order is not None
                       else max([r.get("sort_order") or 0 for r in existing] + [-1]) + 1),
        "created_by": actor_user_id,
        "updated_by": actor_user_id,
    }
    inserted = _rows(client.table(TABLE).insert(row).execute())
    out = inserted[0] if inserted else row
    record_change(entity_type=TABLE, entity_id=out.get(PK),
                  action="create", actor_user_id=actor_user_id, after=out)
    return out


def update_type(ref: str, changes: Dict[str, Any], *,
                actor_user_id: Optional[str], client: Any = None) -> Dict[str, Any]:
    """Rename, re-describe, recolour or move a category. Only the keys in
    `changes` are touched; agents keep pointing at the same row."""
    client = _client(client)
    unknown = set(changes) - set(UPDATABLE_FIELDS)
    if unknown:
        raise AgentTypeError(
            f"cannot change {', '.join(sorted(unknown))} "
            f"(only {', '.join(UPDATABLE_FIELDS)}; the slug is fixed)")
    before = get_type(ref, client)
    payload: Dict[str, Any] = {}
    if "display_name" in changes:
        name = _clean_name(changes["display_name"])
        clash = [r for r in _all(client)
                 if (r.get("display_name") or "").lower() == name.lower()
                 and r.get(PK) != before.get(PK)]
        if clash:
            raise AgentTypeError(f"a category named {name!r} already exists")
        payload["display_name"] = name
    if "description" in changes:
        payload["description"] = _clean_description(changes["description"])
    if "color" in changes:
        payload["color"] = _clean_color(changes["color"])
    if "sort_order" in changes:
        payload["sort_order"] = _clean_sort_order(changes["sort_order"])
    if not payload:
        raise AgentTypeError("nothing to change")
    payload["updated_by"] = actor_user_id
    payload["updated_at"] = _now()
    client.table(TABLE).update(payload).eq(PK, before[PK]).execute()
    after = {**before, **payload}
    record_change(entity_type=TABLE, entity_id=str(before[PK]), action="update",
                  actor_user_id=actor_user_id, before=before, after=after)
    return after


def reorder_types(ordered_refs: List[str], *, actor_user_id: Optional[str],
                  client: Any = None) -> List[Dict[str, Any]]:
    """Put the categories in the given order (slugs or ids). Any category
    left out keeps its place after the ones named."""
    client = _client(client)
    rows = _all(client)
    by_id = {r[PK]: r for r in rows}
    ordered_ids: List[str] = []
    for ref in ordered_refs:
        type_id = get_type(ref, client)[PK]
        if type_id in ordered_ids:
            raise AgentTypeError(f"{ref!r} is listed twice")
        ordered_ids.append(type_id)
    ordered_ids += [r[PK] for r in rows if r[PK] not in ordered_ids]
    before = [by_id[i].get("slug") for i in by_id]
    for position, type_id in enumerate(ordered_ids):
        if (by_id[type_id].get("sort_order")) != position:
            client.table(TABLE).update(
                {"sort_order": position, "updated_by": actor_user_id,
                 "updated_at": _now()}).eq(PK, type_id).execute()
    after = [by_id[i].get("slug") for i in ordered_ids]
    if after != before:
        # entity_id is a uuid column; a reorder is about the whole list.
        record_change(entity_type=TABLE, entity_id=None, action="reorder",
                      actor_user_id=actor_user_id,
                      before={"order": before}, after={"order": after})
    return list_types(client)


def move_type(ref: str, direction: str, *, actor_user_id: Optional[str],
              client: Any = None) -> List[Dict[str, Any]]:
    """Move one category one place `up` or `down` the list."""
    client = _client(client)
    if direction not in ("up", "down"):
        raise AgentTypeError("direction must be 'up' or 'down'")
    target = get_type(ref, client)[PK]
    ids = [r[PK] for r in _all(client)]
    i = ids.index(target)
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
    return reorder_types(ids, actor_user_id=actor_user_id, client=client)


def delete_type(ref: str, *, actor_user_id: Optional[str],
                client: Any = None) -> Dict[str, Any]:
    """Delete a category. Its agents stay, uncategorised. Returns the deleted
    row and `agents_uncategorised`, the number of agents that were in it."""
    client = _client(client)
    before = get_type(ref, client)
    type_id = before[PK]
    affected = agent_counts(client).get(type_id, 0)
    # The foreign key is ON DELETE SET NULL; clearing the column first makes
    # the outcome the same on a database where that constraint is missing.
    client.table("agent_profiles").update({"agent_type_id": None}) \
        .eq("agent_type_id", type_id).execute()
    client.table(TABLE).delete().eq(PK, type_id).execute()
    record_change(entity_type=TABLE, entity_id=str(type_id), action="delete",
                  actor_user_id=actor_user_id,
                  before={**before, "agents_uncategorised": affected})
    return {"deleted": before, "agents_uncategorised": affected}


# --------------------------------------------------------------------------
# The profile's reference
# --------------------------------------------------------------------------


def resolve_type_id(ref: Optional[str], client: Any = None) -> Optional[str]:
    """A category slug or id -> its id; blank / "none" -> None (uncategorised).
    Raises AgentTypeNotFoundError for a category that does not exist."""
    ref = (ref or "").strip()
    if not ref or ref.lower() in ("none", "uncategorised", "uncategorized"):
        return None
    return get_type(ref, client)[PK]


def sync_profile_siblings(client: Any, profile: Dict[str, Any],
                          type_id: Optional[str]) -> None:
    """Give every row of the same agent (`agent_kind`, `slug`) this category.

    A promoted agent has one row per environment; the category is about the
    agent, so all of them move together and no re-promote is needed.
    """
    kind, slug = profile.get("agent_kind"), profile.get("slug")
    if not (kind and slug):
        return
    client.table("agent_profiles").update({"agent_type_id": type_id}) \
        .eq("agent_kind", kind).eq("slug", slug).execute()


def set_profile_type(profile_id: str, ref: Optional[str], *,
                     actor_user_id: Optional[str], client: Any = None) -> Dict[str, Any]:
    """Put an agent in a category (slug or id), or take it out (blank).

    Goes through `update_profile_row`, so it gets the same template
    protection, audit trail and sibling sync as every other profile edit.
    """
    from admin.services.profiles import update_profile_row

    type_id = resolve_type_id(ref, client)
    return update_profile_row(profile_id=profile_id,
                              payload={"agent_type_id": type_id},
                              actor_user_id=actor_user_id)


def carry_to(source_client: Any, target_client: Any, type_id: Optional[str], *,
             actor_user_id: Optional[str] = None) -> Optional[str]:
    """The id, in `target_client`'s database, of the category `type_id` names
    in `source_client`'s. Used by promote.

    Same database: the id is already right. Two databases (one per
    environment): the category is matched by slug and created in the target
    when it is not there yet, so a promoted agent keeps its category. Any
    failure leaves the promoted agent uncategorised; a category is never a
    reason to fail a promote.
    """
    if not type_id:
        return None
    if target_client is source_client:
        return type_id
    try:
        src = _rows(source_client.table(TABLE).select("*").eq(PK, type_id).limit(1).execute())
        if not src:
            return None
        src = src[0]
        found = _rows(target_client.table(TABLE).select(PK)
                      .eq("slug", src["slug"]).limit(1).execute())
        if found:
            return found[0][PK]
        row = {k: src.get(k) for k in ("slug", "display_name", "description",
                                       "color", "sort_order")}
        row[PK] = str(uuid.uuid4())
        row["created_by"] = row["updated_by"] = actor_user_id
        target_client.table(TABLE).insert(row).execute()
        return row[PK]
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_types: could not carry category %s across (%s); "
                       "the promoted agent is left uncategorised", type_id, exc)
        return None
