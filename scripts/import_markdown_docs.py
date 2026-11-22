#!/usr/bin/env python3
"""
Ingest locally-authored markdown docs into the AI-engine shared KB.

Reads frontmatter'd markdown files and embeds them with runtime.embeddings
(the same Gemini 1536-dim embedder the agent uses at query time), then upserts
into ai_kb_pages / ai_kb_embeddings, scoped by source_id, via the supabase client.

Usage
-----
    export AI_SUPABASE_URL=http://127.0.0.1:54421
    export AI_SUPABASE_SERVICE_KEY=sb_secret_...
    export GOOGLE_API_KEY=...
    python ai_agent/scripts/import_markdown_docs.py --dir ai_agent/scripts/kb_content/<your-docs>
    python ai_agent/scripts/import_markdown_docs.py --dir <dir> --dry-run

Each markdown file must start with flat `---`-fenced frontmatter:
    ---
    path: help-center/account/reset-password   # unique within its source, stable (upsert key)
    title: Reset your password
    type: user_guide        # one of ALLOWED_TYPES
    language: en            # one of ALLOWED_LANGS
    source: help-center     # segmentation tag (overridable with --source); must match
                             # a seeded agent_data_sources.config.source_tag
                             # (scripts/_kb_target.py)
    ---
    ## Heading
    body...
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sys
from dataclasses import dataclass

# Make the ai_agent root importable so `runtime.embeddings` resolves whether the
# script is run from ai_agent/ or from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from runtime.embeddings import embed_texts  # noqa: E402
from supabase import Client  # noqa: E402
from scripts._kb_target import ai_client, resolve_source_id  # noqa: E402

logger = logging.getLogger("import_markdown_docs")

ALLOWED_TYPES = {
    "user_guide", "feature_explanation", "navigation_help",
    "business_concept", "troubleshooting", "faq",
}
ALLOWED_LANGS = {"en", "ar", "tr", "ru"}

MIN_SECTION_CHARS = 50
MAX_SECTION_CHARS = 1500

PAGES_TABLE = "ai_kb_pages"
EMBEDDINGS_TABLE = "ai_kb_embeddings"


@dataclass
class Section:
    heading: str
    slug: str
    content: str


def _checksum(*parts: str) -> str:
    h = hashlib.md5()
    for p in parts:
        h.update((p or "").encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "section"


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a `---`-fenced flat frontmatter block from the markdown body."""
    if not text.lstrip().startswith("---"):
        raise ValueError("missing frontmatter fence ('---')")
    stripped = text.lstrip()
    parts = stripped.split("---", 2)
    if len(parts) < 3:
        raise ValueError("frontmatter fence not closed")
    fm_raw, body = parts[1], parts[2]
    meta: dict = {}
    for line in fm_raw.strip().splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, body.strip()


def validate_meta(meta: dict, where: str) -> None:
    for req in ("path", "title", "type"):
        if not meta.get(req):
            raise ValueError(f"{where}: missing frontmatter field '{req}'")
    if meta["type"] not in ALLOWED_TYPES:
        raise ValueError(f"{where}: invalid type {meta['type']!r} (allowed: {sorted(ALLOWED_TYPES)})")
    lang = meta.get("language", "en")
    if lang not in ALLOWED_LANGS:
        raise ValueError(f"{where}: invalid language {lang!r} (allowed: {sorted(ALLOWED_LANGS)})")


def split_markdown_sections(body: str, page_title: str) -> list[Section]:
    """Split markdown into sections at ATX headings; drop tiny, split long."""
    sections: list[Section] = []
    current_heading = page_title
    current: list[str] = []

    def flush() -> None:
        text = "\n".join(current).strip()
        if text:
            sections.append(Section(current_heading, _slugify(current_heading), text))

    for line in body.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush()
            current.clear()
            current_heading = m.group(2).strip() or page_title
        else:
            current.append(line)
    flush()

    out: list[Section] = []
    for s in sections:
        if len(s.content) < MIN_SECTION_CHARS:
            continue
        if len(s.content) <= MAX_SECTION_CHARS:
            out.append(s)
            continue
        buf = ""
        for p in re.split(r"\n\n+", s.content):
            if len(buf) + len(p) + 2 > MAX_SECTION_CHARS and buf:
                out.append(Section(s.heading, s.slug, buf.strip()))
                buf = p
            else:
                buf = f"{buf}\n\n{p}" if buf else p
        if buf.strip():
            out.append(Section(s.heading, s.slug, buf.strip()))

    if not out:
        full = body.strip()
        if len(full) >= MIN_SECTION_CHARS:
            out.append(Section(page_title, _slugify(page_title), full[:MAX_SECTION_CHARS]))
    return out


@dataclass
class MarkdownPage:
    path: str
    title: str
    type: str
    language: str
    source: str
    sections: list[Section]
    metadata: dict


def load_pages(root: str, source_override: str | None) -> list[MarkdownPage]:
    pages: list[MarkdownPage] = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in sorted(files):
            if not fn.endswith(".md") or fn == "TEMPLATE.md":
                continue
            full = os.path.join(dirpath, fn)
            with open(full, "r", encoding="utf-8") as fh:
                raw = fh.read()
            meta, body = parse_frontmatter(raw)
            validate_meta(meta, full)
            sections = split_markdown_sections(body, meta["title"])
            if not sections:
                logger.warning("%s -> 0 sections (skipping)", full)
                continue
            pages.append(MarkdownPage(
                path=meta["path"],
                title=meta["title"],
                type=meta["type"],
                language=meta.get("language", "en"),
                source=source_override or meta.get("source", "markdown"),
                sections=sections,
                metadata={"file": os.path.relpath(full, root)},
            ))
    return pages


def is_unchanged(client: Client, source_id: str, path: str, checksum: str) -> bool:
    existing = (client.table(PAGES_TABLE).select("id, checksum")
                .eq("source_id", source_id).eq("path", path).limit(1).execute())
    return bool(existing.data) and existing.data[0].get("checksum") == checksum


def upsert_page(client: Client, source_id: str, page: MarkdownPage) -> str | None:
    # NB: ai_kb_pages_source_path_key is a *partial* unique index
    # (WHERE path IS NOT NULL, see ai_agent/supabase/migrations/
    # 20260705140000_extend_ai_kb_for_docs.sql), so PostgREST's
    # `on_conflict="source_id,path"` can't resolve against it (Postgres
    # requires the ON CONFLICT target's predicate to match the index's
    # exactly, which the on_conflict query param has no way to express).
    # Do the upsert manually instead.
    fields = {
        "source_id": source_id,
        "path": page.path,
        "title": page.title,
        "source": page.source,
        "type": page.type,
        "language": page.language,
        "metadata": page.metadata,
    }
    existing = (client.table(PAGES_TABLE).select("id")
                .eq("source_id", source_id).eq("path", page.path).limit(1).execute())
    if existing.data:
        page_id = existing.data[0]["id"]
        client.table(PAGES_TABLE).update(fields).eq("id", page_id).execute()
        return page_id
    res = client.table(PAGES_TABLE).insert(fields).execute()
    return res.data[0]["id"] if res.data else None


def replace_embeddings(client: Client, source_id: str, page_id: str,
                       sections: list[Section], embeds: list[list[float]]) -> int:
    client.table(EMBEDDINGS_TABLE).delete().eq("page_id", page_id).execute()
    rows = [{
        "source_id": source_id,
        "page_id": page_id,
        "content": s.content,
        "heading": s.heading,
        "slug": s.slug,
        "token_count": max(1, len(s.content) // 4),
        "embedding": e,
    } for s, e in zip(sections, embeds)]
    if not rows:
        return 0
    for i in range(0, len(rows), 50):
        client.table(EMBEDDINGS_TABLE).insert(rows[i:i + 50]).execute()
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="Directory of frontmatter'd .md files")
    ap.add_argument("--source", default=None, help="Override the frontmatter `source` tag")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")

    pages = load_pages(args.dir, args.source)
    if args.limit:
        pages = pages[:args.limit]
    logger.info("loaded %d pages from %s", len(pages), args.dir)

    if args.dry_run:
        for p in pages:
            logger.info("DRY %s | type=%s | %d sections", p.path, p.type, len(p.sections))
        return 0

    client = ai_client()

    stats = {"changed": 0, "skipped": 0, "failed": 0, "sections": 0}
    source_cache: dict[str, str] = {}
    for i, page in enumerate(pages, 1):
        try:
            if page.source not in source_cache:
                source_cache[page.source] = resolve_source_id(client, page.source)
            source_id = source_cache[page.source]
            body_text = "\n\n".join(s.content for s in page.sections)
            checksum = _checksum(page.title, page.type, page.source, body_text)
            if not args.force and is_unchanged(client, source_id, page.path, checksum):
                stats["skipped"] += 1
                logger.info("[%d/%d] %s unchanged", i, len(pages), page.path)
                continue
            embeds = embed_texts([s.content for s in page.sections])
            page_id = upsert_page(client, source_id, page)
            if page_id is None:
                raise RuntimeError("page upsert returned no id")
            inserted = replace_embeddings(client, source_id, page_id, page.sections, embeds)
            client.table(PAGES_TABLE).update({"checksum": checksum}).eq("id", page_id).execute()
            stats["changed"] += 1
            stats["sections"] += inserted
            logger.info("[%d/%d] %s -> page=%s sections=%d", i, len(pages), page.path, page_id, inserted)
        except Exception as e:
            stats["failed"] += 1
            logger.error("[%d/%d] %s failed: %s", i, len(pages), page.path, e, exc_info=args.verbose)
    logger.info("done: %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
