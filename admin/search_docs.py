"""Full-text index of the portal's docs for global search.

Two corpora:

* **Help** — the partner guides in ``docs/partner/*.md`` (``/admin/help``),
  visible on every build.
* **Developer docs** — ``docs/admin-portal/*.md`` (``/admin/docs``), staff
  only: never indexed on a partner build (the route is gated there too).

Each page is split at its headings into sections; a hit deep-links to the
heading's anchor. Anchors use the same slug rule as ``templates/docs.html``
(which assigns the ids client-side after rendering the markdown), so
``heading_anchor`` and the JS ``slugify`` there must stay in step —
``test_heading_anchor_rule_matches_docs_template`` pins the JS source.

The index is rebuilt when a file's mtime changes, so editing a guide needs
no restart.
"""
from __future__ import annotations

import re
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

_HEADING = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class DocSection:
    corpus: str          # "help" | "dev"
    page_slug: str
    page_title: str
    heading: str         # "" for the page's lead text
    anchor: str          # "" for the page itself
    level: int
    text: str            # plain body text of the section
    href: str


def strip_inline_md(text: str) -> str:
    """Plain text of an inline-markdown string (as the browser's textContent
    would read the rendered HTML)."""
    t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = t.replace("`", "")
    t = re.sub(r"(\*\*|__|\*)", "", t)
    return t


def heading_anchor(text: str) -> str:
    """GitHub-style heading slug. Mirrors ``slugify`` in templates/docs.html:
    lower-case, keep only letters / numbers / combining marks (Unicode L, N,
    M — so Arabic headings keep their text), underscore, whitespace and
    hyphen, then collapse whitespace runs to one hyphen."""
    t = strip_inline_md(text).strip().lower()
    t = "".join(c for c in t if c in "_-" or c.isspace()
                or unicodedata.category(c)[0] in "LNM")
    return re.sub(r"\s+", "-", t)


def _plain(md: str) -> str:
    t = strip_inline_md(md)
    t = re.sub(r"^\s*>\s?", "", t, flags=re.M)          # blockquotes
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.M)        # bullets
    t = re.sub(r"^\s*\d+\.\s+", "", t, flags=re.M)        # numbered
    t = re.sub(r"^\s*\|?[-:\s|]+\|[-:\s|]*$", "", t, flags=re.M)  # table rules
    t = t.replace("|", " ")
    return re.sub(r"\s+", " ", t).strip()


def split_sections(markdown: str, *, corpus: str, page_slug: str, page_title: str,
                   base_href: str) -> List[DocSection]:
    sections: List[DocSection] = []
    seen: Dict[str, int] = {}
    cur_heading, cur_anchor, cur_level = "", "", 0
    buf: List[str] = []
    in_fence = False

    def flush() -> None:
        body = _plain("\n".join(buf))
        if cur_heading or body or cur_level == 1:
            href = base_href + (f"#{cur_anchor}" if cur_anchor else "")
            sections.append(DocSection(corpus, page_slug, page_title, cur_heading,
                                       cur_anchor, cur_level, body, href))

    for line in markdown.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            buf.append(line.strip("`~"))
            continue
        m = None if in_fence else _HEADING.match(line)
        if not m:
            buf.append(line)
            continue
        flush()
        buf = []
        level = len(m.group(1))
        heading = strip_inline_md(m.group(2)).strip()
        anchor = heading_anchor(m.group(2))
        # Duplicate slugs get -1, -2 … in document order (same as the JS).
        if anchor in seen:
            seen[anchor] += 1
            anchor = f"{anchor}-{seen[anchor]}"
        else:
            seen[anchor] = 0
        if level == 1:
            # The H1 is the page title: its section IS the page (no anchor).
            cur_heading, cur_anchor, cur_level = "", "", 1
        else:
            cur_heading, cur_anchor, cur_level = heading, anchor, level
    flush()
    return sections


# ---------------------------------------------------------------------------
# Corpora + cache
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_cache: Dict[str, Tuple[Tuple, List[DocSection]]] = {}


def _help_pages() -> List[Dict[str, str]]:
    from admin.router import help_pages
    return help_pages()


def _help_dir() -> Path:
    from admin.router import HELP_DIR
    return HELP_DIR


def _dev_pages() -> List[Dict[str, str]]:
    from admin.router import DOC_PAGES
    return list(DOC_PAGES)


def _dev_dir() -> Path:
    from admin.router import _DOCS_DIR
    return _DOCS_DIR


def _build(corpus: str) -> List[DocSection]:
    if corpus == "help":
        pages, root, base = _help_pages(), _help_dir(), "/admin/help"
    else:
        pages, root, base = _dev_pages(), _dev_dir(), "/admin/docs"
    stamp = []
    for p in pages:
        try:
            stamp.append((p["file"], (root / p["file"]).stat().st_mtime))
        except OSError:
            stamp.append((p["file"], None))
    key = tuple(stamp)
    with _lock:
        hit = _cache.get(corpus)
        if hit and hit[0] == key:
            return hit[1]
    sections: List[DocSection] = []
    for p in pages:
        try:
            md = (root / p["file"]).read_text(encoding="utf-8")
        except OSError:
            continue
        href = base + ("" if p["slug"] == "index" else f"/{p['slug']}")
        sections.extend(split_sections(md, corpus=corpus, page_slug=p["slug"],
                                       page_title=p["title"], base_href=href))
    with _lock:
        _cache[corpus] = (key, sections)
    return sections


def corpora(include_dev: bool) -> List[DocSection]:
    out = _build("help")
    if include_dev:
        out = out + _build("dev")
    return out


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def snippet(text: str, tokens: List[str], width: int = 120) -> str:
    """A ~width-char window of ``text`` around the first token hit."""
    low = text.lower()
    pos = min((i for i in (low.find(t) for t in tokens) if i >= 0), default=-1)
    if pos < 0:
        return text[:width].rstrip() + ("…" if len(text) > width else "")
    start = max(0, pos - width // 3)
    # Start on a word boundary.
    if start > 0:
        sp = text.rfind(" ", 0, start)
        start = sp + 1 if sp >= 0 else start
    end = min(len(text), start + width)
    if end < len(text):
        sp = text.rfind(" ", start, end)
        end = sp if sp > start else end
    return ("…" if start > 0 else "") + text[start:end].strip() + ("…" if end < len(text) else "")


def search_docs(tokens: List[str], *, include_dev: bool, limit: int = 8) -> List[Dict]:
    """Rank doc sections whose heading/title/body contain EVERY token.

    Rank (lower is better): 10 all tokens in the heading, 11 in the page
    title (page-level row), 13 heading+body, 15 body only — a docs hit always
    sorts below a screen/record whose own name matches.
    """
    if not tokens:
        return []
    hits: List[Tuple[float, int, Dict]] = []
    for i, s in enumerate(corpora(include_dev)):
        head = s.heading.lower()
        title = s.page_title.lower()
        body = s.text.lower()
        if not all(t in head or t in title or t in body for t in tokens):
            continue
        if s.heading and all(t in head for t in tokens):
            rank = 10
        elif not s.heading and all(t in title for t in tokens):
            rank = 11
        elif any(t in head for t in tokens):
            rank = 13
        else:
            rank = 15
        if s.heading and head.startswith(" ".join(tokens)):
            rank -= 0.5  # the heading starts with the query: best doc hit
        is_help = s.corpus == "help"
        label = s.heading or s.page_title
        context = (f"{s.page_title} · " if s.heading else "") + snippet(s.text, tokens)
        hits.append((rank, i, {
            "label": label,
            "type": "Help" if is_help else "Developer doc",
            "context": context,
            "href": s.href,
            "icon": "help" if is_help else "docs",
            "_rank": rank + (0 if is_help else 1),  # Help first on ties
        }))
    hits.sort(key=lambda h: (h[0], h[1]))
    # One row per page section; cap per page so one long page can't flood.
    out: List[Dict] = []
    per_page: Dict[str, int] = {}
    for _, _, row in hits:
        page = row["href"].split("#")[0]
        if per_page.get(page, 0) >= 3:
            continue
        per_page[page] = per_page.get(page, 0) + 1
        out.append(row)
        if len(out) >= limit:
            break
    return out


def page_titles(include_dev: bool) -> List[Tuple[str, str, str]]:
    """(title, href, corpus) for every page — used by tests and the registry
    coverage check."""
    out = []
    for s in corpora(include_dev):
        if not s.heading:
            out.append((s.page_title, s.href, s.corpus))
    return out
