"""Pin scripts/reembed.py's TARGETS to the schema the migrations actually create.

The script shipped with primary keys `memory_id` / `embedding_id` that exist in
NO migration — both tables declare `id uuid ... PRIMARY KEY` — so every run
failed on the initial select and had to be hand-patched in-container during the
2026-08-06 EMBED_PROVIDER=azure migration on a client demo VM. These tests
read the migration SQL itself, so a rename on either side (script or schema)
fails loudly here instead of at runtime against a live stack.
"""
import re
from pathlib import Path

from scripts.reembed import TARGETS

_REPO = Path(__file__).resolve().parents[2]
_MIGRATIONS = {
    "memory": _REPO / "supabase/migrations/20260614120000_add_user_memory.sql",
    "kb": _REPO / "supabase/migrations/20260615120000_add_kb_shared_tables.sql",
}


def _create_table_block(sql: str, table: str) -> str:
    """Return the CREATE TABLE body for `table` (up to its closing paren)."""
    m = re.search(
        rf"CREATE TABLE IF NOT EXISTS public\.{table}\s*\((.*?)\n\);",
        sql,
        re.DOTALL,
    )
    assert m, f"CREATE TABLE public.{table} not found in migration"
    return m.group(1)


def _table_sql(target: str, table: str, pk: str) -> tuple[str, bool]:
    """(CREATE TABLE body, whether ``pk`` is its primary key): from the
    migration that added the table, or, in a tree that ships a schema baseline
    instead of the history, from the baseline, where pg_dump writes the table
    without IF NOT EXISTS and the key as a separate constraint."""
    path = _MIGRATIONS[target]
    if path.exists():
        block = _create_table_block(path.read_text(), table)
        inline = re.search(rf"^\s*{pk}\s+uuid\b[^,\n]*PRIMARY KEY", block, re.MULTILINE)
        return block, bool(inline)
    for baseline in sorted((_REPO / "supabase/migrations").glob("*_baseline.sql")):
        sql = baseline.read_text()
        m = re.search(rf"CREATE TABLE public\.{table}\s*\((.*?)\n\);", sql, re.DOTALL)
        if m:
            key = re.search(rf"ALTER TABLE ONLY public\.{table}\s+ADD CONSTRAINT \w+ PRIMARY KEY \({pk}\);", sql)
            typed = re.search(rf"^\s*{pk}\s+uuid\b", m.group(1), re.MULTILINE)
            return m.group(1), bool(key and typed)
    raise AssertionError(f"CREATE TABLE public.{table} not found in any migration")


def test_targets_pk_and_columns_match_migrations():
    for target, (table, pk, text_col, dim) in TARGETS.items():
        block, pk_decl = _table_sql(target, table, pk)

        # The PK the script pages/updates by must be the declared PK column.
        assert pk_decl, (
            f"{table}: TARGETS says pk={pk!r} but the migration declares no "
            f"'{pk} uuid ... PRIMARY KEY' column"
        )

        # The text column the script embeds must exist.
        assert re.search(rf"^\s*{text_col}\s+text\b", block, re.MULTILINE), (
            f"{table}: text column {text_col!r} not in migration"
        )

        # The vector width the script passes must match the column width.
        width = re.search(r"embedding\s+(?:public\.)?vector\((\d+)\)", block)
        assert width, f"{table}: no embedding vector column in migration"
        assert int(width.group(1)) == dim, (
            f"{table}: TARGETS dim={dim} but column is vector({width.group(1)})"
        )


def test_targets_do_not_use_phantom_pk_names():
    """`memory_id` / `embedding_id` are the names the script wrongly shipped
    with — they appear in no migration as columns. Keep them out."""
    for target, (table, pk, _text, _dim) in TARGETS.items():
        assert pk not in ("memory_id", "embedding_id"), (
            f"{target}: pk {pk!r} is a phantom column — the {table} PK is 'id' "
            f"(see {_MIGRATIONS[target].name})"
        )
