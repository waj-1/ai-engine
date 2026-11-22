"""Export / restore the agent_* config tables.

The boot-time seeders (`seed_prompts`, `sync_tools`, `seed_data_sources`,
`seed_agent_profiles`, `seed_eval_suites`) populate the registry from
code + JSON files. They DO NOT capture admin-portal edits — tool
whitelist toggles, prompt version inserts, model swaps, endpoint
changes, downstream-profile links.

This module is the bridge between portal state and a fresh DB:

    # Before `supabase db reset`:
    python -m admin.db_snapshot export > seed.json

    # After reset + boot (seeders have run + repopulated defaults):
    python -m admin.db_snapshot restore seed.json

Restore upserts on natural keys (the UNIQUE constraint of each table — see
`_NATURAL_KEYS`), so re-running the same seed.json is safe.

It also REMAPS every UUID reference before writing, which is what makes the
workflow above converge at all. `supabase db reset` drops the database; the
seeders then re-create `agent_models`, `agent_prompts`/`agent_prompt_versions`,
`agent_tools`, `agent_data_sources` and `agent_mcp_servers` with FRESH primary
keys, while seed.json's `agent_profiles` rows still point at the OLD ones. A
verbatim upsert of those rows fails closed on the FK columns
(`model_main_id`, `parent_profile_id`, `agent_prompt_versions.prompt_id`,
`agent_profile_versions.profile_id`) — `_write_row` swallows the violation, so
the profile is simply MISSING afterwards; three were lost that way on a local DB
on 2026-08-17. The array/jsonb reference columns (`tool_ids`,
`data_source_ids`, `mcp_server_ids`, `prompt_version_ids`,
`downstream_profile_ids`) carry no FK at all and fail worse than that: the row
lands looking fine while pointing at rows that no longer exist, so the profile
quietly serves with no tools, no data sources and an unresolvable prompt pin.

So `_build_id_map` resolves each referenced row by its NATURAL key — models by
(name, environment), prompts by (agent_kind, slug), prompt versions by
(agent_kind, slug, version_number), data sources / MCP servers by name, tools by
python_name, profiles by (agent_kind, slug, environment) — and rewrites the
references to the ids that exist in the TARGET database. A reference whose
natural key is absent from the target is left as-is: the row it points at is
almost always in this same payload and lands with its exported id, so the pair
stays consistent. Profiles are then written parents-first
(`_parents_first`), and `served_version_id` is deferred to the very end because
it points into `agent_profile_versions`, which restore writes AFTER profiles.

Rows are written verbatim with ONE exception: `voice_config` (on both
`agent_profiles` and inside an `agent_profile_versions.snapshot`) is flattened
through `voice/live/config.py::promote_legacy_voice_config` first. Restore runs
AFTER the migrations have applied to the fresh database, so a legacy nested
block re-imported here would be a shape no migration ever looks at again — see
`_promote_voice_config`.

The shape of seed.json:
{
  "exported_at": "2026-05-19T12:34:56Z",
  "tables": {
    "agent_models":           [...rows...],
    "agent_prompts":          [...],
    "agent_prompt_versions":  [...],
    "agent_tools":            [...],
    "agent_data_sources":     [...],
    "agent_mcp_servers":      [...],
    "agent_profiles":         [...],
    "agent_eval_suites":      [...],
    "agent_profile_versions": [...]   // promote snapshots
  }
}
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Load ai_agent/.env (mirrors main.py's lifespan behaviour) so this CLI
# works the same way the live server does — no need to `set -a; source`.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

from core.auth import get_service_client

logger = logging.getLogger(__name__)

# Tables exported / restored, in dependency order (parents first so
# foreign keys resolve on insert).
_TABLES: List[str] = [
    "agent_models",
    "agent_prompts",
    "agent_prompt_versions",
    "agent_tools",
    "agent_data_sources",
    "agent_mcp_servers",
    "agent_profiles",
    "agent_eval_suites",
    "agent_profile_versions",
]

# Natural-key fields per table — what "the same row" means across two
# databases. Used both to match on upsert and to remap references (see
# `_build_id_map`). Each tuple is the table's UNIQUE constraint in
# supabase/migrations/20260603120000_baseline_agent_schema.sql; the exception is
# agent_eval_suites, whose `name` carries no constraint but is unique in
# practice and is what the seeder keys on.
#
# The two *_versions tables key off their PARENT's id, which is itself remapped
# — that is fine and deliberate: remapping happens before matching, so by then
# the value is the target's id, not the export's.
_NATURAL_KEYS: Dict[str, Tuple[str, ...]] = {
    "agent_models": ("name", "environment"),
    "agent_prompts": ("agent_kind", "slug"),
    "agent_prompt_versions": ("prompt_id", "version_number"),
    "agent_tools": ("python_name",),
    "agent_data_sources": ("name",),
    "agent_mcp_servers": ("name",),
    "agent_profiles": ("agent_kind", "slug", "environment"),
    "agent_eval_suites": ("name",),
    "agent_profile_versions": ("profile_id", "environment", "version_number"),
}

# Primary key per table. References are remapped INTO these values, and an
# UPDATE matched by natural key must never write one (see
# `_update_by_natural_key`): the target's id is the id every remapped reference
# now points at, so overwriting it with the export's would re-break exactly what
# the remap just fixed.
_PK: Dict[str, str] = {
    "agent_models": "model_id",
    "agent_prompts": "prompt_id",
    "agent_prompt_versions": "prompt_version_id",
    "agent_tools": "tool_id",
    "agent_data_sources": "source_id",
    "agent_mcp_servers": "mcp_server_id",
    "agent_profiles": "profile_id",
    "agent_eval_suites": "suite_id",
    "agent_profile_versions": "version_id",
}

# Every column holding another table's primary key, by shape:
#   "id"       a single uuid          "id_list"  uuid[]
#   "id_map"   jsonb {name: uuid}     (agent_profiles.prompt_version_ids)
# Only four of these are real FKs (model_main_id, model_light_id,
# parent_profile_id, and the two *_versions parents) — the rest are loose uuid
# references Postgres will not complain about, which is precisely why they need
# remapping most.
_REFERENCES: Dict[str, Tuple[Tuple[str, str, str], ...]] = {
    "agent_prompt_versions": (
        ("prompt_id", "agent_prompts", "id"),
    ),
    "agent_profiles": (
        ("model_main_id", "agent_models", "id"),
        ("model_light_id", "agent_models", "id"),
        ("tool_ids", "agent_tools", "id_list"),
        ("data_source_ids", "agent_data_sources", "id_list"),
        ("mcp_server_ids", "agent_mcp_servers", "id_list"),
        ("downstream_profile_ids", "agent_profiles", "id_list"),
        ("parent_profile_id", "agent_profiles", "id"),
        ("prompt_version_ids", "agent_prompt_versions", "id_map"),
        ("served_version_id", "agent_profile_versions", "id"),
    ),
    "agent_profile_versions": (
        ("profile_id", "agent_profiles", "id"),
    ),
    "agent_eval_suites": (
        ("evaluator_model_id", "agent_models", "id"),
        ("profile_id", "agent_profiles", "id"),
    ),
}

# agent_profiles.served_version_id points into agent_profile_versions, which is
# restored AFTER agent_profiles (it holds the inverse FK). Writing it inline
# would fail the FK on a fresh database and take the WHOLE profile row down with
# it — the pin is worth less than the profile, so it is stripped on the way in
# and re-applied once the versions exist.
_DEFERRED_COLUMN = "served_version_id"


def export() -> Dict[str, Any]:
    """Pull every agent_* table and return a serialisable dict."""
    client = get_service_client()
    payload = {"exported_at": _iso_now(), "tables": {}}
    for table in _TABLES:
        try:
            rows = client.table(table).select("*").execute().data or []
            payload["tables"][table] = rows
            logger.info("exported %d rows from %s", len(rows), table)
        except Exception as exc:
            logger.warning("export: %s skipped (%s)", table, exc)
            payload["tables"][table] = []
    return payload


def restore(payload: Dict[str, Any]) -> Dict[str, int]:
    """Upsert rows from `payload` into the live DB.

    Returns per-table counts: {table: rows_inserted_or_updated}.
    Boot-time seeders should run before this; restore overlays the
    portal-edited values on top of the seeded defaults.

    The id map is built ONCE, from the target as the seeders left it, before
    anything is written. That is the correct moment: any referenced row the map
    cannot resolve is one this payload is about to insert with its exported id,
    so leaving such a reference untouched keeps it valid, whereas re-reading the
    target mid-restore would resolve some references to rows written by this
    same run and make the result depend on table order.
    """
    client = get_service_client()
    counts: Dict[str, int] = {}
    tables = payload.get("tables", {})
    id_map = _build_id_map(client, tables)
    deferred: List[Tuple[Dict[str, Any], Any]] = []
    for table in _TABLES:
        rows = tables.get(table, [])
        if not rows:
            counts[table] = 0
            continue
        counts[table] = _restore_table(
            client, table, rows, id_map=id_map, deferred=deferred)
    _apply_deferred(client, deferred, id_map)
    return counts


# --- Cross-database reference remapping ---------------------------------


def _fetch(client, table: str, columns: str = "*") -> List[Dict[str, Any]]:
    """Every row of `table` in the TARGET database, or [] if unreadable.

    An unreadable table (missing, permission, transport) must not abort the
    restore: it only costs the remap for that table, and the pre-existing
    behaviour — write the reference as exported — is what happens then anyway.
    """
    try:
        return client.table(table).select(columns).execute().data or []
    except Exception as exc:
        logger.warning("restore: could not read %s for id remapping (%s)", table, exc)
        return []


def _natural(row: Dict[str, Any], fields: Tuple[str, ...]) -> Tuple[Any, ...]:
    """The row's natural key, stringified so a JSON int and a DB int match."""
    return tuple(None if row.get(f) is None else str(row.get(f)) for f in fields)


def _map_flat(client, table: str, payload_rows: List[Dict[str, Any]]) -> Dict[str, str]:
    """{exported id: target id} for a table whose natural key is plain columns."""
    pk = _PK[table]
    fields = _NATURAL_KEYS[table]
    live = {_natural(row, fields): row.get(pk) for row in _fetch(client, table)}
    mapping: Dict[str, str] = {}
    for row in payload_rows:
        old, new = row.get(pk), live.get(_natural(row, fields))
        if old and new and old != new:
            mapping[old] = new
    return mapping


def _map_child(client, table: str, parent: str, fk: str, extra: Tuple[str, ...],
               tables: Dict[str, Any], columns: str) -> Dict[str, str]:
    """{exported id: target id} for a table keyed on its PARENT plus `extra`.

    `agent_prompt_versions` is unique on (prompt_id, version_number) and
    `agent_profile_versions` on (profile_id, environment, version_number) — ids
    that differ between the two databases, so neither side's key is comparable
    until the parent id is translated to the parent's own natural key. Doing
    that on both sides is what lets a profile's `prompt_version_ids` pin resolve
    to the freshly seeded version of the same (agent_kind, slug, version_number).
    """
    pk, parent_pk = _PK[table], _PK[parent]
    parent_fields = _NATURAL_KEYS[parent]

    def _keyed(rows, parent_rows):
        by_parent = {r.get(parent_pk): _natural(r, parent_fields) for r in parent_rows}
        out = {}
        for row in rows:
            parent_key = by_parent.get(row.get(fk))
            if parent_key is None:
                continue          # orphan / parent not in this payload
            out[parent_key + _natural(row, extra)] = row.get(pk)
        return out

    live = _keyed(_fetch(client, table, columns), _fetch(client, parent))
    exported = _keyed(tables.get(table) or [], tables.get(parent) or [])
    return {old: live[key] for key, old in exported.items()
            if old and live.get(key) and live[key] != old}


def _build_id_map(client, tables: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    """{table: {exported id: target id}} for every table referenced by another."""
    id_map: Dict[str, Dict[str, str]] = {}
    for table in ("agent_models", "agent_prompts", "agent_tools",
                  "agent_data_sources", "agent_mcp_servers", "agent_profiles"):
        id_map[table] = _map_flat(client, table, tables.get(table) or [])
    id_map["agent_prompt_versions"] = _map_child(
        client, "agent_prompt_versions", "agent_prompts", "prompt_id",
        ("version_number",), tables,
        columns="prompt_version_id,prompt_id,version_number")
    id_map["agent_profile_versions"] = _map_child(
        client, "agent_profile_versions", "agent_profiles", "profile_id",
        ("environment", "version_number"), tables,
        columns="version_id,profile_id,environment,version_number")
    stale = {t: len(m) for t, m in id_map.items() if m}
    if stale:
        logger.info("restore: remapping stale references by natural key: %s", stale)
    return id_map


def _remap_row(table: str, row: Dict[str, Any],
               id_map: Optional[Dict[str, Dict[str, str]]]) -> Dict[str, Any]:
    """Rewrite `row`'s references to ids that exist in the TARGET database.

    An id with no mapping is left exactly as exported — see the module
    docstring: it is a row this payload inserts under its own id.
    """
    specs = _REFERENCES.get(table)
    if not specs or not id_map:
        return row
    out = dict(row)
    changed = False
    for column, target, shape in specs:
        mapping = id_map.get(target) or {}
        if not mapping or column not in out:
            continue
        value = out[column]
        if shape == "id" and isinstance(value, str):
            new: Any = mapping.get(value, value)
        elif shape == "id_list" and isinstance(value, list):
            new = [mapping.get(v, v) if isinstance(v, str) else v for v in value]
        elif shape == "id_map" and isinstance(value, dict):
            new = {k: (mapping.get(v, v) if isinstance(v, str) else v)
                   for k, v in value.items()}
        else:
            continue
        if new != value:
            out[column] = new
            changed = True
    return out if changed else row


def _parents_first(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Profiles ordered so a parent is written before its children.

    `_restore_table`'s retry pass already recovers a single level of this, but
    only one: a grandchild whose parent also failed the first pass stays failed.
    Ordering is cheap and deterministic; the retry stays as the net for the
    cycles this deliberately does not resolve.
    """
    index_by_id = {row.get("profile_id"): i for i, row in enumerate(rows)
                   if row.get("profile_id")}
    ordered: List[Dict[str, Any]] = []
    placed: set = set()

    def _visit(i: int, trail: frozenset) -> None:
        if i in placed or i in trail:
            return                       # already placed, or a parent cycle
        parent_i = index_by_id.get(rows[i].get("parent_profile_id"))
        if parent_i is not None and parent_i != i:
            _visit(parent_i, trail | {i})
        if i not in placed:
            placed.add(i)
            ordered.append(rows[i])

    for i in range(len(rows)):
        _visit(i, frozenset())
    return ordered


def _apply_deferred(client, deferred: List[Tuple[Dict[str, Any], Any]],
                    id_map: Optional[Dict[str, Dict[str, str]]]) -> int:
    """Write back the `served_version_id` pins held out of the profile writes."""
    applied = 0
    for row, version_id in deferred:
        mapped = (id_map or {}).get("agent_profile_versions", {}).get(
            version_id, version_id)
        try:
            _filtered(client.table("agent_profiles").update(
                {_DEFERRED_COLUMN: mapped}), "agent_profiles", row).execute()
            applied += 1
        except Exception as exc:
            # The pinned version did not survive the restore. The profile did,
            # and an unpinned profile serves its default (dev/staging = the live
            # row, prod = latest promoted) — degraded, not broken.
            logger.warning(
                "restore: could not re-pin %s=%s on %s (%s); the profile is "
                "restored but unpinned",
                _DEFERRED_COLUMN, mapped, _row_label("agent_profiles", row), exc)
    return applied


def _promote_voice_config(table: str, row: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten a legacy `voice_config` on its way back into the DB.

    Restore is the ONE `voice_config` writer that used to bypass
    `voice/live/config.py::promote_legacy_voice_config` — it upserted whatever
    shape the export happened to hold, verbatim. That matters because of WHEN it
    runs: the documented workflow is `export -> supabase db reset -> restore`,
    so `20260817120000_unify_voice_config.sql` has already applied to an EMPTY
    database by the time these rows land. A `{"live": {...}}` row (or a
    pre-duplex turn-based one) re-imported afterwards is a shape the migration
    will never look at again — it only ever runs once per database. The read
    shim carries it for now; when that shim is deleted "one release after the
    migration" those tenants' duplex config resolves to all-defaults, and a
    pre-duplex turn-based row is worse than that today (see
    `promote_legacy_voice_config`). The shim's own docstring names "a snapshot
    restored from before the migration" as a source of exactly these rows.

    Applied to both writers of the column: the live `agent_profiles` row and the
    `voice_config` frozen inside an `agent_profile_versions.snapshot`, which is
    what prod and every served_version_id-pinned profile actually resolve from.

    The import is deliberately lazy: this module is a CLI that also runs in
    contexts (a bare `export` against a DB with no voice config at all) where
    pulling the voice package in at import time buys nothing.
    """
    def _flatten(value: Any) -> Any:
        # Non-dict / absent is left exactly as found — a malformed column is the
        # DB's problem to report, not something a restore should silently
        # rewrite (and `promote_legacy_voice_config` would raise on it).
        if value is None or isinstance(value, dict):
            from voice.live.config import promote_legacy_voice_config

            return promote_legacy_voice_config(value)
        return value

    if table == "agent_profiles" and "voice_config" in row:
        return {**row, "voice_config": _flatten(row.get("voice_config"))}
    if table == "agent_profile_versions":
        snapshot = row.get("snapshot")
        if isinstance(snapshot, dict) and "voice_config" in snapshot:
            return {
                **row,
                "snapshot": {
                    **snapshot,
                    "voice_config": _flatten(snapshot.get("voice_config")),
                },
            }
    return row


def _restore_table(client, table: str, rows: List[Dict[str, Any]],
                   id_map: Optional[Dict[str, Dict[str, str]]] = None,
                   deferred: Optional[List[Tuple[Dict[str, Any], Any]]] = None) -> int:
    """Upsert one table's rows. Returns count of rows written.

    Rows are written in two passes: a first pass that stays silent on
    failure, then a retry pass for whatever failed. This resolves
    intra-table FK ordering (e.g. agent_profiles.parent_profile_id
    pointing at another profile that hadn't been inserted yet) without
    requiring the export to be topologically sorted.

    Every row is normalised twice first: `_promote_voice_config`, so a legacy
    nested `voice_config` cannot be re-imported past the migration that exists
    to remove it, and `_remap_row`, so no reference points at a UUID the target
    database does not have. `id_map` is optional purely so the shape tests can
    drive one table without a database behind them; the CLI always passes one.
    """
    written = 0
    pending: List[Dict[str, Any]] = []
    rows = [_remap_row(table, _promote_voice_config(table, row), id_map)
            for row in rows]
    if table == "agent_profiles":
        rows = _parents_first(rows)
        if deferred is not None:
            rows = [_defer_pin(row, deferred) for row in rows]
    for row in rows:
        if _write_row(client, table, row, log=False):
            written += 1
        else:
            pending.append(row)
    for row in pending:
        if _write_row(client, table, row, log=True):
            written += 1
    return written


def _defer_pin(row: Dict[str, Any],
               deferred: List[Tuple[Dict[str, Any], Any]]) -> Dict[str, Any]:
    """Hold a profile's `served_version_id` back for `_apply_deferred`."""
    pin = row.get(_DEFERRED_COLUMN)
    if not pin:
        return row
    stripped = {k: v for k, v in row.items() if k != _DEFERRED_COLUMN}
    deferred.append((stripped, pin))
    return stripped


def _write_row(client, table: str, row: Dict[str, Any], log: bool) -> bool:
    """Upsert a single row. Returns True on success, False on failure."""
    try:
        if _row_exists(client, table, row):
            _update_by_natural_key(client, table, row)
        else:
            client.table(table).insert(row).execute()
        return True
    except Exception as exc:
        if log:
            logger.warning("restore: %s row failed (%s): %s", table, exc, _row_label(table, row))
        return False


def _filtered(q, table: str, row: Dict[str, Any]):
    """Narrow a select/update to the one row matching `row`'s natural key."""
    for field in _NATURAL_KEYS.get(table, ()):
        q = q.eq(field, row.get(field))
    return q


def _row_exists(client, table: str, row: Dict[str, Any]) -> bool:
    q = _filtered(client.table(table).select("*"), table, row)
    return bool((q.limit(1).execute().data or []))


def _update_by_natural_key(client, table: str, row: Dict[str, Any]) -> None:
    # Strip server-managed columns the update shouldn't touch, plus the primary
    # key: the matched row already IS this row, and its id is the one every
    # remapped reference now points at (see `_PK`).
    skip = {"created_at", "created_by", "updated_at", "updated_by", _PK.get(table)}
    payload = {k: v for k, v in row.items() if k not in skip}
    _filtered(client.table(table).update(payload), table, row).execute()


def _row_label(table: str, row: Dict[str, Any]) -> str:
    fields = _NATURAL_KEYS.get(table)
    if fields:
        return "/".join(str(row.get(f)) for f in fields)
    return str(row.get(_PK.get(table, ""), row.get("name", "?")))


def _iso_now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def main() -> None:
    parser = argparse.ArgumentParser(description="Export / restore agent_* tables.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("export", help="Dump current state to stdout as JSON.")
    rest = sub.add_parser("restore", help="Load JSON file into the DB.")
    rest.add_argument("path", help="Path to the JSON file produced by `export`.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.cmd == "export":
        payload = export()
        json.dump(payload, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return

    if args.cmd == "restore":
        text = Path(args.path).read_text()
        payload = json.loads(text)
        counts = restore(payload)
        for table, n in counts.items():
            print(f"  {table}: {n}")
        return


if __name__ == "__main__":
    main()
