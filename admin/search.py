"""Global search (Ctrl-K palette) for the admin portal.

``POST /admin/search.json`` (the portal; the query travels in the body so it
never lands in an access log) and ``GET /admin/search.json?q=&env=`` (older
callers and the admin MCP ``search`` tool) return grouped, ranked rows::

    {"results": [{group, type, label, context, sublabel, href, icon, tech?, person?}],
     "notes":   [{source, message}],      # sources skipped this time
     "scope":   null | "actions" | "people" | "docs"}

Where rows come from:

* **Screens, tabs, agent tabs, settings, actions** — ``admin/search_registry.py``,
  generated from the nav IA in ``admin/nav.py``; matched in-process, so they
  work with every database down.
* **Docs** — full text of the Help guides (and, off partner builds, the
  developer docs), deep-linked to the heading: ``admin/search_docs.py``.
* **Records and people** — agents, models, tools, toolsets, knowledge bases,
  skills, channels, tests, team, conversations, customers/patients/leads:
  ``admin/search_sources.py``. Each source runs in parallel under a deadline;
  one that fails or runs late is skipped with a quiet note.

Scope prefixes: ``>`` actions, ``@`` people & conversations, ``?`` docs.

PII: people are masked (``admin/search_pii.py``) and the query text is never
logged — the access log filter installed below redacts ``q=`` on GET.
"""
from __future__ import annotations

import asyncio
import concurrent.futures as cf
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import Body, Depends, Query
from fastapi.responses import JSONResponse

from admin import search_pii as pii
from admin import search_registry as reg
from admin import search_sources as src
from admin.auth import require_superuser
from admin.router import router

logger = logging.getLogger(__name__)

MAX_RESULTS = 40
DEADLINE_SEC = 3.0
GROUP_CAPS: Dict[str, int] = {
    reg.GROUP_ACTIONS: 5, reg.GROUP_SCREENS: 7, reg.GROUP_AGENTS: 6, reg.GROUP_PEOPLE: 8,
    reg.GROUP_BUILD: 8, reg.GROUP_TESTING: 6, reg.GROUP_DOCS: 6, reg.GROUP_SETTINGS: 6,
}
SCOPES = {">": "actions", "@": "people", "?": "docs"}

Source = Tuple[str, str, Callable[[src.SearchCtx], List[Dict[str, Any]]]]

from core.plugins import call_hook as _call_hook, load_hook as _load_hook  # noqa: E402

RECORD_SOURCES: Tuple[Source, ...] = (
    ("agents", "Agents", src.agents),
    ("models", "Models", src.models),
    ("tools", "Tools", src.tools),
    ("toolsets", "Toolsets", src.mcp_servers),
    ("knowledge", "Knowledge bases", src.knowledge_bases),
    ("skills", "Skills", src.skills),
    ("channels", "Channels", src.channels),
    ("testing", "Tests", src.testing),
    ("team", "Team", src.team),
)
PEOPLE_SOURCES: Tuple[Source, ...] = (
    ("conversations", "Conversations", src.conversations),
    ("telemetry", "Conversation telemetry", src.traces),
    # A plugin's own people sources (hook "search", `people_sources()`), e.g. a product
    # database's customers and businesses. The engine has no business table of
    # its own, so a build without a plugin neither searches nor mentions one.
    *(s for found in _call_hook(_load_hook("search"), "people_sources") for s in found),
)

_pool = cf.ThreadPoolExecutor(max_workers=16, thread_name_prefix=pii.SEARCH_THREAD_PREFIX)
pii.install_access_log_filter()


# ---------------------------------------------------------------------------
# Query parsing + context
# ---------------------------------------------------------------------------


def parse(query: Optional[str]) -> Tuple[Optional[str], str]:
    """Split a scope prefix off the query: ("people", "sara") for "@sara"."""
    q = (query or "").strip()
    if q[:1] in SCOPES:
        return SCOPES[q[:1]], q[1:].strip()
    return None, q


def _ctx(q: str, env: str) -> src.SearchCtx:
    from core.deployment import active_scope, scope_kinds
    from admin.nav import partner_build

    try:
        client = active_scope()
    except ValueError:
        client = None
    ql = q.lower()
    tokens = [t for t in re.split(r"\s+", ql) if t]
    return src.SearchCtx(q=q, ql=ql, tokens=tokens, env=env, partner=partner_build(),
                         kinds=scope_kinds(client) if client else None,
                         client_key=client.key if client else None)


# ---------------------------------------------------------------------------
# Static registry matching
# ---------------------------------------------------------------------------


def _rank_entry(e: reg.Entry, ctx: src.SearchCtx) -> Optional[int]:
    label_rank = src.rank_label(e.label, ctx)
    if label_rank <= 4:
        return label_rank
    kws = [k.lower() for k in e.keywords]
    if any(k == ctx.ql for k in kws):
        return 4
    if any(k.startswith(ctx.ql) for k in kws):
        return 5
    hay = " ".join([e.label.lower()] + kws)
    if ctx.tokens and all(t in hay for t in ctx.tokens):
        return 6 if any(ctx.ql in k for k in kws) else 7
    # "testing runs": one word names the entry, the other its section
    # ("In Testing") — a weak match, below everything above.
    if ctx.tokens and all(t in f"{hay} {e.context.lower()}" for t in ctx.tokens):
        return 8
    return None


def match_registry(ctx: src.SearchCtx, *, actions_only: bool = False) -> List[Dict[str, Any]]:
    out = []
    for e in reg.visible_entries():
        if actions_only and e.type != "Action":
            continue
        rank = _rank_entry(e, ctx) if ctx.q else 5
        if rank is None:
            continue
        # Agent-tab shortcuts are generic; let real screens win ties.
        if e.type == "Agent tab":
            rank += 1
        out.append({"label": e.label, "type": e.type, "group": e.group,
                    "href": e.href.replace("{env}", ctx.env), "icon": e.icon,
                    "context": e.context, "_rank": rank})
    return out


def _docs(ctx: src.SearchCtx, limit: int) -> List[Dict[str, Any]]:
    from admin import search_docs

    from demo_mode import settings as demo_settings

    # Developer docs are refused in the public demo (demo_mode/policy.py).
    include_dev = not ctx.partner and not demo_settings.enabled()
    if not ctx.q:
        rows = []
        for title, href, corpus in search_docs.page_titles(include_dev=False):
            if href == "/admin/help":
                continue
            rows.append({"label": title, "type": "Help", "context": "Help guide",
                         "href": href, "icon": "help", "_rank": 5})
    else:
        rows = search_docs.search_docs(ctx.tokens, include_dev=include_dev, limit=limit)
    for r in rows:
        r["group"] = reg.GROUP_DOCS
    return rows


# ---------------------------------------------------------------------------
# Parallel sources
# ---------------------------------------------------------------------------


def run_sources(ctx: src.SearchCtx, sources: Tuple[Source, ...],
                deadline: float = DEADLINE_SEC) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Run ``sources`` in parallel; return (rows, notes). A source that
    raises or misses the deadline is skipped with a note. Never logs the
    query — only the source name and the exception type."""
    futures = {_pool.submit(fn, ctx): (key, label) for key, label, fn in sources}
    done, pending = cf.wait(futures, timeout=deadline)
    rows: List[Dict[str, Any]] = []
    notes: List[Dict[str, str]] = []
    for fut in done:
        key, label = futures[fut]
        try:
            rows.extend(fut.result() or [])
        except Exception as exc:  # noqa: BLE001 — fail soft per source
            logger.info("global-search: source %s failed (%s)", key, type(exc).__name__)
            notes.append({"source": key, "message": f"{label} couldn't be searched just now."})
    for fut in pending:
        key, label = futures[fut]
        fut.cancel()
        logger.info("global-search: source %s timed out", key)
        notes.append({"source": key, "message": f"{label} took too long and were skipped."})
    notes.sort(key=lambda n: n["source"])
    return rows, notes


def _wants_people(ctx: src.SearchCtx, scoped: bool) -> bool:
    if scoped:
        return len(ctx.q) >= 2
    return (len(ctx.q) >= 3 or pii.is_phone(ctx.q) or pii.is_id_fragment(ctx.q))


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def _merge(rows: List[Dict[str, Any]], *, scoped: bool = False) -> List[Dict[str, Any]]:
    """Dedupe by href, cap per group, order groups by their best hit.
    A scoped search (``>``/``@``/``?``) shows one group, so it gets room."""
    seen: set = set()
    by_group: Dict[str, List[Dict[str, Any]]] = {}
    for r in sorted(rows, key=lambda r: (r.get("_rank", 9), r["label"].lower())):
        key = (r["href"], r["label"])
        if key in seen:
            continue
        seen.add(key)
        group = r.get("group") or reg.GROUP_SCREENS
        bucket = by_group.setdefault(group, [])
        if len(bucket) < (15 if scoped else GROUP_CAPS.get(group, 6)):
            bucket.append(r)
    order = sorted(by_group, key=lambda g: (by_group[g][0].get("_rank", 9),
                                            reg.GROUP_ORDER.index(g) if g in reg.GROUP_ORDER else 99))
    out: List[Dict[str, Any]] = []
    for g in order:
        out.extend(by_group[g])
    out = out[:MAX_RESULTS]
    for r in out:
        r.pop("_rank", None)
        r.setdefault("context", "")
        r["sublabel"] = r["context"]  # pre-redesign field name (admin MCP)
    return out


def search_payload(query: Optional[str], env: str) -> Dict[str, Any]:
    scope, q = parse(query)
    if env not in ("staging", "prod"):
        env = "staging"
    ctx = _ctx(q, env)
    payload: Dict[str, Any] = {"results": [], "notes": [], "scope": scope}
    if not q and scope is None:
        return payload

    rows: List[Dict[str, Any]] = []
    sources: Tuple[Source, ...] = ()
    if scope is None:
        rows += match_registry(ctx)
        rows += _docs(ctx, limit=6)
        if len(q) >= 2:
            sources += RECORD_SOURCES
        if _wants_people(ctx, scoped=False):
            sources += PEOPLE_SOURCES
    elif scope == "actions":
        rows += match_registry(ctx, actions_only=True)
    elif scope == "docs":
        rows += _docs(ctx, limit=12)
    elif scope == "people" and _wants_people(ctx, scoped=True):
        sources += PEOPLE_SOURCES

    if sources:
        found, notes = run_sources(ctx, sources)
        rows += found
        payload["notes"] = notes
    payload["results"] = _merge(rows, scoped=scope is not None)
    return payload


def search(query: str, env: str) -> List[Dict[str, Any]]:
    """Flat result list (the admin MCP ``search`` tool's contract)."""
    return search_payload(query, env)["results"]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/search.json")
async def search_json(
    q: str = Query("", description="Search query"),
    env: str = Query("staging", pattern=r"^(staging|prod)$"),
    _ctx_: dict = Depends(require_superuser),
) -> JSONResponse:
    """Global-search results (GET; the portal itself POSTs)."""
    return JSONResponse(await asyncio.to_thread(search_payload, q, env))


@router.post("/search.json")
async def search_json_post(
    body: Dict[str, Any] = Body(default_factory=dict),
    _ctx_: dict = Depends(require_superuser),
) -> JSONResponse:
    """Global-search results. The query rides in the JSON body so a phone
    number or a name never appears in a URL or an access log."""
    q = str(body.get("q") or "")[:200]
    env = str(body.get("env") or "staging")
    if env not in ("staging", "prod"):
        env = "staging"
    return JSONResponse(await asyncio.to_thread(search_payload, q, env))
