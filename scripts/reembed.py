"""Re-embed stored vectors after changing `EMBED_PROVIDER`.

WHY YOU NEED THIS

Embeddings from different models occupy different spaces. A vector written by
Gemini and a query embedded by Azure are not comparable, and pgvector will not
tell you so — a similarity search always returns its nearest rows, or none.
So flipping `EMBED_PROVIDER` on a populated corpus does not fail loudly; the
KB starts answering from whatever happens to be nearest in a space it was
never embedded in, and user memory silently recalls nothing.

The safe order is:

    1. python -m scripts.reembed --target kb     --apply     # with the NEW provider set
    2. python -m scripts.reembed --target memory --apply
    3. leave EMBED_PROVIDER set

Run it with the NEW provider already configured in the environment: this
script embeds with whatever `core.embedding_provider` currently resolves, and
writes the result back over the old vector.

Both targets are idempotent — re-running re-embeds the same rows to the same
values — so an interrupted run is resumed by simply running it again.

Dry run by default. `--apply` is required to write.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.auth import get_service_client                     # noqa: E402
from core.embedding_provider import active_provider, embed_batch  # noqa: E402

# (table, primary key, text column, vector width)
#
# The widths are the COLUMN widths, not a provider default: ai_user_memory is
# vector(768) and ai_kb_embeddings is vector(1536). Passing the wrong one
# produces an insert error rather than silent corruption, which is the right
# failure direction, but get them right anyway.
#
# Both PKs are `id` — per the migrations that create these tables
# (supabase/migrations/20260614120000_add_user_memory.sql and
# 20260615120000_add_kb_shared_tables.sql). Earlier revisions of this script
# said `memory_id`/`embedding_id`, which exist in NO migration and made every
# run fail on the initial select (found the hard way on a client demo VM,
# 2026-08-06). tests/scripts/test_reembed_targets.py pins these against the
# migration SQL.
TARGETS = {
    "memory": ("ai_user_memory", "id", "content", 768),
    "kb": ("ai_kb_embeddings", "id", "content", 1536),
}


def _to_vector_literal(vec):
    return "[" + ",".join(str(float(x)) for x in vec) + "]"


def reembed(target: str, *, apply: bool, page_size: int = 200) -> int:
    table, pk, text_col, dim = TARGETS[target]
    client = get_service_client()
    provider = active_provider()

    print(f"== re-embedding {table} at {dim}-dim with provider={provider!r}")
    if not apply:
        print("   DRY RUN — pass --apply to write. Counting rows only.")

    done = 0
    offset = 0
    started = time.time()
    while True:
        rows = (
            client.table(table)
            .select(f"{pk},{text_col}")
            .order(pk)
            .range(offset, offset + page_size - 1)
            .execute()
            .data
            or []
        )
        if not rows:
            break

        texts = [(r.get(text_col) or "").strip() for r in rows]
        # Blank rows cannot be embedded and must not be sent: a provider may
        # reject the whole batch for one empty string, failing a page that
        # would otherwise have succeeded.
        keep = [(r, t) for r, t in zip(rows, texts) if t]
        skipped = len(rows) - len(keep)

        if keep and apply:
            vectors = embed_batch([t for _r, t in keep], dim=dim)
            for (row, _t), vec in zip(keep, vectors):
                client.table(table).update(
                    {"embedding": _to_vector_literal(vec)}
                ).eq(pk, row[pk]).execute()

        done += len(keep)
        offset += page_size
        rate = done / max(time.time() - started, 0.001)
        print(f"   {done} rows{' (dry run)' if not apply else ''}"
              f"{f', {skipped} blank skipped' if skipped else ''}"
              f"  [{rate:.1f}/s]")

    print(f"== {'re-embedded' if apply else 'would re-embed'} {done} rows in {table}")
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", required=True, choices=sorted(TARGETS))
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default is a dry run)")
    ap.add_argument("--page-size", type=int, default=200)
    args = ap.parse_args()

    print(f"provider in force: {active_provider()!r}")
    if args.apply:
        print("Writing. Retrieval against this table is INCONSISTENT until "
              "this finishes — rows already rewritten live in the new space "
              "while the rest are still in the old one.")
    reembed(args.target, apply=args.apply, page_size=args.page_size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
