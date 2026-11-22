"""Ingest the AI-agents demo knowledge base into its pgvector source.

The corpus is hand-written bilingual markdown committed in the repo
(scripts/kb_content/agents_demo/{en,ar}/*.md) about the FICTIONAL provider
named in runtime/tools/agents_demo/brand.py; `{{brand…}}` placeholders are
rendered from that constant on the way in, so swapping the company is one
edit plus a re-ingest.

Embeddings use the engine's own embedder (`runtime.embeddings.embed_texts`,
i.e. EMBED_PROVIDER — Azure `text-embedding-3-small` on the deployed stacks,
Gemini locally by default). Query-time retrieval uses the same function, so
run this with the SAME EMBED_PROVIDER as the engine that will serve it.

Rows are written in exactly the shape admin/kb_handlers.py and
the other KB ingest scripts write (ai_kb_pages / ai_kb_embeddings), into the
`agents_demo_kb` source the boot seeder creates (admin/seed_agents_demo.py).

Usage (with .env loaded, AI_SUPABASE_* pointed at the target stack):
    python scripts/ingest_agents_demo_kb.py            # reset the source, ingest all 24 pages
    python scripts/ingest_agents_demo_kb.py --dry-run  # list what would be ingested
    python scripts/ingest_agents_demo_kb.py --file scripts/kb_content/agents_demo/en/08-router-and-wifi-troubleshooting.md
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from runtime.tools.agents_demo.brand import render  # noqa: E402
from runtime.tools.agents_demo.kb import DATA_SOURCE_NAME  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("ingest_agents_demo_kb")

KB_DIR = ROOT / "scripts" / "kb_content" / "agents_demo"
LANGUAGES = ("en", "ar")


def corpus_files() -> List[Tuple[str, Path]]:
    """(language, path) for every page, English first, in file order."""
    out: List[Tuple[str, Path]] = []
    for lang in LANGUAGES:
        out.extend((lang, p) for p in sorted((KB_DIR / lang).glob("*.md")))
    return out


def page_title(text: str, fallback: str) -> str:
    m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else fallback


def _vector_literal(vec: List[float]) -> str:
    return "[" + ",".join(f"{v:.7f}" for v in vec) + "]"


def ingest_file(client, source_id: str, lang: str, path: Path) -> int:
    """Upsert one page (by path) and its chunks. Returns the chunk count."""
    from runtime.embeddings import chunk_text, embed_texts

    text = render(path.read_text(encoding="utf-8")).strip()
    title = page_title(text, path.stem)
    rel = str(path.relative_to(ROOT))
    old = (client.table("ai_kb_pages").select("id").eq("source_id", source_id)
           .eq("path", rel).execute().data or [])
    if old:
        ids = [r["id"] for r in old]
        client.table("ai_kb_embeddings").delete().in_("page_id", ids).execute()
        client.table("ai_kb_pages").delete().in_("id", ids).execute()
    page = client.table("ai_kb_pages").insert({
        "source_id": source_id, "title": title, "type": "user_guide", "language": lang,
        "content": text, "source": "local_markdown_ingest", "path": rel,
    }).execute().data[0]
    chunks = chunk_text(text, max_tokens=400, overlap_tokens=48, heading=title)
    if not chunks:
        return 0
    vectors = embed_texts([c["content"] for c in chunks])
    client.table("ai_kb_embeddings").insert([
        {"page_id": page["id"], "source_id": source_id, "content": c["content"], "heading": c["heading"],
         "slug": c["slug"], "token_count": c["token_count"], "embedding": _vector_literal(v)}
        for c, v in zip(chunks, vectors)
    ]).execute()
    return len(chunks)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", help="ingest one markdown file (upsert by path; nothing else touched)")
    ap.add_argument("--no-reset", action="store_true", help="do not wipe the source before a full ingest")
    ap.add_argument("--dry-run", action="store_true", help="list the pages and exit")
    args = ap.parse_args(argv)

    files = corpus_files()
    if args.file:
        p = Path(args.file).resolve()
        lang = "ar" if "/ar/" in str(p) else "en"
        files = [(lang, p)]
    if args.dry_run:
        for lang, p in files:
            print(f"{lang}  {p.relative_to(ROOT)}")
        print(f"{len(files)} page(s)")
        return 0

    from core.auth import get_service_client

    client = get_service_client()
    rows = (client.table("agent_data_sources").select("source_id").eq("name", DATA_SOURCE_NAME)
            .limit(1).execute().data or [])
    if not rows:
        logger.error("no agent_data_sources row named %r — boot the engine once so "
                     "admin/seed_agents_demo.py creates it", DATA_SOURCE_NAME)
        return 1
    source_id = rows[0]["source_id"]
    if not args.file and not args.no_reset:
        client.table("ai_kb_embeddings").delete().eq("source_id", source_id).execute()
        client.table("ai_kb_pages").delete().eq("source_id", source_id).execute()
        logger.info("reset source %s", DATA_SOURCE_NAME)
    total = 0
    for lang, p in files:
        n = ingest_file(client, source_id, lang, p)
        total += n
        print(f"{lang}  {p.name}: {n} chunks")
    print(f"Done: {len(files)} page(s), {total} chunk(s) into {DATA_SOURCE_NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
