"""Record + people sources for the portal's global search.

Every function here is ONE source: it takes a ``SearchCtx`` and returns result
rows (``{label, type, group, context, href, icon, tech?, _rank}``). Sources
are run in parallel by ``admin.search`` with a deadline; one that raises or
runs late is skipped and reported as a quiet note — never an error page.

Scoping rules every source follows:

* **Environment** — AI-project reads go through ``admin.router.client_for(env)``
  (the selected STAGING/PRODUCTION branch on the control-plane), tables with an
  ``environment`` column filter on it, and ClickHouse filters on ``waj.env``.
* **Partner builds** — rows are narrowed to the client's agent kinds
  (``core.deployment.scope_kinds``) and the main-database sources
  (the people sources a plugin adds) are switched off: a
  partner build never sees another client's data.
* **PII** — people are shown masked (``admin.search_pii``), only people who
  have at least one conversation in the selected environment are returned,
  and nothing here logs the query text.

Where the people data really lives (all verified against the schemas):

==========================  ================================================
Source                      Link to a conversation
==========================  ================================================
AI ``ai_chat_sessions``     id (= session id), title, user_id, business_id,
                            and the record ids a plugin's sessions carry in
                            metadata (``session_record_keys``)
main ``business``           business_name → ``ai_chat_sessions.business_id``
                            (a plugin's people source; names on rows here)
ClickHouse traces           trace id / session id prefixes
==========================  ================================================
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from admin import search_pii as pii
from admin.search_registry import (
    AGENT_TABS, GROUP_AGENTS, GROUP_BUILD, GROUP_PEOPLE, GROUP_SETTINGS,
    GROUP_TESTING, agent_tab_href,
)

logger = logging.getLogger(__name__)

CAP = 6            # rows per record category
SCAN = 25          # rows fetched before the in-process AND filter


@dataclass
class SearchCtx:
    q: str                      # the query as typed (scope prefix removed)
    ql: str                     # lower-cased
    tokens: List[str]           # lower-cased words
    env: str                    # "staging" | "prod"
    partner: bool
    kinds: Optional[frozenset]  # partner client's agent kinds, else None
    client_key: Optional[str]


# ---------------------------------------------------------------------------
# Seams (patched in tests)
# ---------------------------------------------------------------------------


def _ai(env: str):
    from admin.router import client_for
    return client_for(env)


def _main():
    from admin.router import get_main_client
    return get_main_client()


def _ch(sql: str, params: Dict[str, Any], timeout: float) -> List[Dict[str, Any]]:
    from core.lmnr_clickhouse import query
    return query(sql, params, timeout=timeout)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAFE = re.compile(r"[^\w\s@.+'-]", re.UNICODE)


def _needle(token: str) -> str:
    """A PostgREST-safe ilike needle: no filter-syntax characters."""
    return _SAFE.sub("", token).replace('"', "").strip()[:60]


def _or_ilike(cols: Sequence[str], tokens: Iterable[str]) -> str:
    parts = []
    for t in tokens:
        n = _needle(t)
        if len(n) < 2:
            continue
        parts.extend(f'{c}.ilike."%{n}%"' for c in cols)
    return ",".join(parts)


def _rows(res: Any) -> List[Dict[str, Any]]:
    data = getattr(res, "data", None)
    return data if isinstance(data, list) else []


def rank_label(label: str, ctx: SearchCtx) -> int:
    """0 exact · 1 prefix · 2 word-prefix · 3 substring · 4 all words ·
    6 matched on another field (description, keyword, technical name)."""
    lab = (label or "").lower()
    q = ctx.ql
    if not lab:
        return 6
    if lab == q:
        return 0
    if lab.startswith(q):
        return 1
    if any(w.startswith(q) for w in re.split(r"[\s›·/_-]+", lab)):
        return 2
    if q in lab:
        return 3
    if ctx.tokens and all(t in lab for t in ctx.tokens):
        return 4
    return 6


def _all_tokens_in(ctx: SearchCtx, *fields: Any) -> bool:
    hay = " ".join(str(f or "") for f in fields).lower()
    return all(t in hay for t in ctx.tokens)


def _first_line(text: Optional[str], n: int = 100) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _kind_ok(ctx: SearchCtx, kind: Optional[str], extra: Optional[Iterable[str]] = None) -> bool:
    if ctx.kinds is None:
        return True
    if kind in ctx.kinds:
        return True
    return bool(set(extra or ()) & ctx.kinds)


def kind_label(kind: Optional[str]) -> str:
    if not kind:
        return "Agent"
    return kind.replace("_", " ").strip().capitalize()


def when(ts: Optional[str]) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    days = (datetime.now(timezone.utc).date() - dt.date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    return dt.strftime("%-d %b %Y") if dt.year != datetime.now(timezone.utc).year else dt.strftime("%-d %b")


def _row(label: str, type_: str, group: str, href: str, icon: str, context: str = "",
         rank: int = 6, tech: str = "", person: bool = False) -> Dict[str, Any]:
    r = {"label": label, "type": type_, "group": group, "href": href, "icon": icon,
         "context": context, "_rank": rank}
    if tech:
        r["tech"] = tech
    if person:
        r["person"] = True
    return r


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def _agent_tab_split(ctx: SearchCtx):
    """If some query words name an agent tab ("prompt", "voice"…), return
    (tab, remaining words) so "support prompt" pairs the agent and the tab."""
    if len(ctx.tokens) < 2:
        return None, ctx.tokens
    for tab in AGENT_TABS:
        names = {tab.label.lower(), *tab.keywords}
        used = [t for t in ctx.tokens if any(t == n or (len(t) > 3 and n.startswith(t)) for n in names)]
        if used and len(used) < len(ctx.tokens):
            return tab, [t for t in ctx.tokens if t not in used]
    return None, ctx.tokens


def agents(ctx: SearchCtx) -> List[Dict[str, Any]]:
    tab, words = _agent_tab_split(ctx)
    flt = _or_ilike(("display_name", "slug", "description"), words)
    if not flt:
        return []
    rows = _rows(
        _ai(ctx.env).table("agent_profiles")
        .select("profile_id, display_name, slug, description, agent_kind, status, environment")
        .eq("environment", ctx.env).or_(flt).limit(SCAN).execute()
    )
    from demo_mode import protect as demo_protect

    out = []
    for r in demo_protect.visible(rows):
        if not _kind_ok(ctx, r.get("agent_kind")):
            continue
        hay = (r.get("display_name"), r.get("slug"), r.get("description"), r.get("agent_kind"))
        if not all(w in " ".join(str(h or "") for h in hay).lower() for w in words):
            continue
        name = r.get("display_name") or r.get("slug") or kind_label(r.get("agent_kind"))
        pid = r.get("profile_id")
        status = (r.get("status") or "").replace("_", " ")
        ctx_line = " · ".join(x for x in (_first_line(r.get("description"), 80),
                                          status.capitalize() if status and status != "active" else "") if x)
        sub = SearchCtx(" ".join(words), " ".join(words), list(words), ctx.env, ctx.partner,
                        ctx.kinds, ctx.client_key)
        rank = rank_label(name, sub)
        if tab is not None:
            out.append(_row(f"{name} › {tab.label}", "Agent tab", GROUP_AGENTS,
                            agent_tab_href(tab, pid, ctx.env), "agents",
                            ctx_line or kind_label(r.get("agent_kind")), min(rank, 3),
                            r.get("slug") or ""))
        else:
            out.append(_row(name, "Agent", GROUP_AGENTS,
                            f"/admin/agents?env={ctx.env}&profile_id={pid}", "agents",
                            ctx_line or kind_label(r.get("agent_kind")), rank,
                            r.get("slug") or ""))
    out.sort(key=lambda x: (x["_rank"], x["label"].lower()))
    return out[:CAP]


def models(ctx: SearchCtx) -> List[Dict[str, Any]]:
    flt = _or_ilike(("name", "model_string", "provider"), ctx.tokens)
    if not flt:
        return []
    rows = _rows(
        _ai(ctx.env).table("agent_models")
        .select("model_id, name, model_string, provider, environment, is_active")
        .eq("environment", ctx.env).or_(flt).limit(SCAN).execute()
    )
    out = []
    for r in rows:
        if not _all_tokens_in(ctx, r.get("name"), r.get("model_string"), r.get("provider")):
            continue
        name = r.get("name") or r.get("model_string") or "Model"
        out.append(_row(name, "Model", GROUP_AGENTS, f"/admin/agent-models/{r.get('model_id')}?env={ctx.env}",
                        "model", " · ".join(x for x in (r.get("provider"), r.get("model_string")) if x),
                        rank_label(name, ctx)))
    return out[:CAP]


def tools(ctx: SearchCtx) -> List[Dict[str, Any]]:
    flt = _or_ilike(("display_name", "python_name", "description"), ctx.tokens)
    if not flt:
        return []
    rows = _rows(
        _ai(ctx.env).table("agent_tools")
        .select("tool_id, display_name, python_name, description, agent_kind, additional_agent_kinds, is_deprecated")
        .or_(flt).limit(SCAN).execute()
    )
    out = []
    for r in rows:
        if not _kind_ok(ctx, r.get("agent_kind"), r.get("additional_agent_kinds")):
            continue
        if not _all_tokens_in(ctx, r.get("display_name"), r.get("python_name"), r.get("description")):
            continue
        name = r.get("display_name") or r.get("python_name") or "Tool"
        rank = min(rank_label(name, ctx), rank_label(r.get("python_name") or "", ctx))
        if r.get("is_deprecated"):
            rank += 1
        out.append(_row(name, "Tool", GROUP_BUILD, f"/admin/tools/{r.get('tool_id')}?env={ctx.env}", "tools",
                        _first_line(r.get("description"), 90) or "Tool", rank,
                        r.get("python_name") or ""))
    out.sort(key=lambda x: (x["_rank"], x["label"].lower()))
    return out[:CAP]


def mcp_servers(ctx: SearchCtx) -> List[Dict[str, Any]]:
    flt = _or_ilike(("name", "usage_guidance"), ctx.tokens)
    if not flt:
        return []
    client = _ai(ctx.env)
    rows = _rows(
        client.table("agent_mcp_servers")
        .select("mcp_server_id, name, source, usage_guidance, is_builtin")
        .or_(flt).limit(SCAN).execute()
    )
    if ctx.kinds is not None and rows:
        # Partner build: only toolsets attached to the client's own agents.
        profs = _rows(client.table("agent_profiles").select("profile_id, agent_kind, mcp_server_ids").execute())
        vis = [p for p in profs if p.get("agent_kind") in ctx.kinds]
        allowed = {sid for p in vis for sid in (p.get("mcp_server_ids") or [])}
        vis_ids = [p["profile_id"] for p in vis if p.get("profile_id")]
        if vis_ids:
            atts = _rows(client.table("agent_profile_mcp_servers").select("mcp_server_id")
                         .in_("profile_id", vis_ids).execute())
            allowed |= {a.get("mcp_server_id") for a in atts}
        rows = [r for r in rows if r.get("mcp_server_id") in allowed]
    out = []
    for r in rows:
        if not _all_tokens_in(ctx, r.get("name"), r.get("usage_guidance")):
            continue
        name = r.get("name") or "Toolset"
        src = "Built-in" if r.get("is_builtin") else ("External server" if r.get("source") == "external" else "Toolset")
        out.append(_row(name, "Toolset", GROUP_BUILD,
                        f"/admin/agent-mcp-servers/{r.get('mcp_server_id')}?env={ctx.env}", "connections",
                        " · ".join(x for x in (src, _first_line(r.get("usage_guidance"), 70)) if x),
                        rank_label(name, ctx)))
    return out[:CAP]


def knowledge_bases(ctx: SearchCtx) -> List[Dict[str, Any]]:
    flt = _or_ilike(("name",), ctx.tokens)
    if not flt:
        return []
    rows = _rows(
        _ai(ctx.env).table("agent_data_sources")
        .select("source_id, name, kind, agent_kind, additional_agent_kinds, is_active, last_refreshed_at")
        .or_(flt).limit(SCAN).execute()
    )
    out = []
    for r in rows:
        if not _kind_ok(ctx, r.get("agent_kind"), r.get("additional_agent_kinds")):
            continue
        if not _all_tokens_in(ctx, r.get("name")):
            continue
        name = r.get("name") or "Knowledge base"
        refreshed = when(r.get("last_refreshed_at"))
        out.append(_row(name, "Knowledge base", GROUP_BUILD, f"/admin/kb/{r.get('source_id')}?env={ctx.env}",
                        "knowledge",
                        " · ".join(x for x in (kind_label(r.get("agent_kind")),
                                               f"updated {refreshed}" if refreshed else "") if x),
                        rank_label(name, ctx)))
    return out[:CAP]


def skills(ctx: SearchCtx) -> List[Dict[str, Any]]:
    flt = _or_ilike(("display_name", "slug", "description"), ctx.tokens)
    if not flt:
        return []
    rows = _rows(
        _ai(ctx.env).table("agent_skills")
        .select("skill_id, display_name, slug, description, agent_kind, status")
        .or_(flt).limit(SCAN).execute()
    )
    out = []
    for r in rows:
        if not _kind_ok(ctx, r.get("agent_kind")):
            continue
        if not _all_tokens_in(ctx, r.get("display_name"), r.get("slug"), r.get("description")):
            continue
        name = r.get("display_name") or r.get("slug") or "Skill"
        out.append(_row(name, "Skill", GROUP_BUILD, f"/admin/skills/{r.get('skill_id')}?env={ctx.env}", "knowledge",
                        _first_line(r.get("description"), 90), rank_label(name, ctx), r.get("slug") or ""))
    return out[:CAP]


def channels(ctx: SearchCtx) -> List[Dict[str, Any]]:
    flt = _or_ilike(("name", "slug", "description"), ctx.tokens)
    if not flt:
        return []
    rows = _rows(
        _ai(ctx.env).table("agent_channels")
        .select("channel_id, name, slug, kind, description, allowed_agent_kinds, is_active")
        .or_(flt).limit(SCAN).execute()
    )
    if ctx.partner:
        from admin.channel_handlers import visible_channels
        rows = visible_channels(rows)
    out = []
    for r in rows:
        if not _all_tokens_in(ctx, r.get("name"), r.get("slug"), r.get("description")):
            continue
        name = r.get("name") or r.get("slug") or "Channel"
        out.append(_row(name, "Channel", GROUP_BUILD, f"/admin/channels/{r.get('channel_id')}?env={ctx.env}",
                        "channels", _first_line(r.get("description"), 90) or kind_label(r.get("kind")),
                        rank_label(name, ctx), r.get("slug") or ""))
    return out[:CAP]


def testing(ctx: SearchCtx) -> List[Dict[str, Any]]:
    """Test suites (+ their latest runs), model comparisons, voice checks,
    and a test run by its exact id."""
    client = _ai(ctx.env)
    out: List[Dict[str, Any]] = []
    flt = _or_ilike(("name",), ctx.tokens)
    suites: List[Dict[str, Any]] = []
    if flt:
        suites = [
            r for r in _rows(client.table("agent_eval_suites")
                             .select("suite_id, name, agent_kind, is_active")
                             .or_(flt).limit(SCAN).execute())
            if _kind_ok(ctx, r.get("agent_kind")) and _all_tokens_in(ctx, r.get("name"))
        ]
        suites.sort(key=lambda r: rank_label(r.get("name") or "", ctx))
        suites = suites[:CAP]
        for r in suites:
            name = r.get("name") or "Test suite"
            out.append(_row(name, "Test suite", GROUP_TESTING,
                            f"/admin/evals/suites/{r.get('suite_id')}?env={ctx.env}", "testing",
                            kind_label(r.get("agent_kind")), rank_label(name, ctx)))
        names = {r["suite_id"]: r.get("name") or "Test suite" for r in suites if r.get("suite_id")}
        if names:
            runs = _rows(client.table("agent_eval_runs")
                         .select("run_id, suite_id, status, score, created_at")
                         .in_("suite_id", list(names)).order("created_at", desc=True).limit(4).execute())
            for run in runs:
                out.append(_run_row(run, names.get(run.get("suite_id"), "Test suite"), ctx, 7))
        bake = [r for r in _rows(client.table("agent_bakeoffs").select("bakeoff_id, name, status")
                                 .or_(flt).limit(SCAN).execute())
                if _all_tokens_in(ctx, r.get("name"))]
        if ctx.kinds is None:  # bake-offs carry no kind; partner builds skip them
            for r in bake[:3]:
                name = r.get("name") or "Model comparison"
                out.append(_row(name, "Model comparison", GROUP_TESTING,
                                f"/admin/evals/bakeoffs/{r.get('bakeoff_id')}?env={ctx.env}", "testing",
                                (r.get("status") or "").capitalize(), rank_label(name, ctx)))
        vflt = _or_ilike(("title", "slug"), ctx.tokens)
        for r in _rows(client.table("ai_voice_scenarios").select("scenario_id, slug, title, agent_kind, locale")
                       .or_(vflt).limit(SCAN).execute())[:SCAN]:
            if not _kind_ok(ctx, r.get("agent_kind")) or not _all_tokens_in(ctx, r.get("title"), r.get("slug")):
                continue
            name = r.get("title") or r.get("slug") or "Voice check"
            out.append(_row(name, "Voice check", GROUP_TESTING, f"/admin/voice-scenarios?env={ctx.env}",
                            "voice", " · ".join(x for x in (kind_label(r.get("agent_kind")), r.get("locale")) if x),
                            rank_label(name, ctx), r.get("slug") or ""))
    if pii.is_uuid(ctx.q):
        for run in _rows(client.table("agent_eval_runs").select("run_id, suite_id, status, score, created_at")
                         .eq("run_id", ctx.q.strip()).limit(1).execute()):
            out.append(_run_row(run, "Test run", ctx, 0))
    return out


def _run_row(run: Dict[str, Any], suite_name: str, ctx: SearchCtx, rank: int) -> Dict[str, Any]:
    score = run.get("score")
    try:
        score_txt = f"score {round(float(score) * 100) if float(score) <= 1 else round(float(score))}%"
    except (TypeError, ValueError):
        score_txt = ""
    status = str(run.get("status") or "").replace("_", " ")
    bits = [x for x in (status.capitalize(), score_txt, when(run.get("created_at"))) if x]
    return _row(f"Run · {suite_name}", "Test run", GROUP_TESTING,
                f"/admin/evals/runs/{run.get('run_id')}?env={ctx.env}", "testing", " · ".join(bits), rank)


def team(ctx: SearchCtx) -> List[Dict[str, Any]]:
    """Portal team members (superusers) by email or phone."""
    if len(ctx.q) < 3:
        return []
    q = _main().table("user").select("id, email, phone_number, country_code, permission").eq("is_deleted", False)
    if pii.is_phone(ctx.q):
        q = q.in_("phone_number", pii.phone_candidates(ctx.q))
    else:
        n = _needle(ctx.q)
        if len(n) < 3:
            return []
        q = q.ilike("email", f"%{n}%")
    out = []
    for r in _rows(q.limit(SCAN).execute()):
        if not (r.get("permission") or {}).get("is_superuser"):
            continue
        label = pii.mask_email(r.get("email")) or pii.mask_phone(r.get("phone_number"), r.get("country_code"))
        out.append(_row(label or "Team member", "Team member", GROUP_SETTINGS, "/admin/users", "users",
                        "Portal admin", 3, person=True))
    return out[:CAP]


# ---------------------------------------------------------------------------
# Conversations & people
# ---------------------------------------------------------------------------

_SESSION_COLS = "id, title, agent_kind, business_id, user_id, message_count, metadata, created_at"
_biz_cache: Dict[str, tuple] = {}
_biz_lock = threading.Lock()
_BIZ_TTL = 300.0


def _business_names(ctx: SearchCtx, ids: Iterable[Optional[str]]) -> Dict[str, str]:
    """business_id → name from the main DB (cached 5 min). Never on a partner
    build — business names are WAJ data."""
    ids = {i for i in ids if i}
    if not ids or ctx.partner:
        return {}
    now = time.monotonic()
    out: Dict[str, str] = {}
    missing = []
    with _biz_lock:
        for i in ids:
            hit = _biz_cache.get(i)
            if hit and now - hit[1] < _BIZ_TTL:
                out[i] = hit[0]
            else:
                missing.append(i)
    if missing:
        try:
            rows = _rows(_main().table("business").select("id, business_name").in_("id", missing).execute())
        except Exception as exc:  # noqa: BLE001 — names are decoration only
            logger.info("global-search: business names unavailable (%s)", type(exc).__name__)
            rows = []
        with _biz_lock:
            for r in rows:
                if r.get("id"):
                    _biz_cache[r["id"]] = (r.get("business_name") or "", now)
                    out[r["id"]] = r.get("business_name") or ""
    return out


def _sessions(ctx: SearchCtx, column: str, values: Sequence[str], limit: int = 20) -> List[Dict[str, Any]]:
    values = [v for v in dict.fromkeys(values) if v]
    if not values:
        return []
    q = (_ai(ctx.env).table("ai_chat_sessions").select(_SESSION_COLS)
         .in_(column, values[:50]).is_("deleted_at", "null"))
    if ctx.kinds is not None:
        q = q.in_("agent_kind", sorted(ctx.kinds))
    return _rows(q.order("created_at", desc=True).limit(limit).execute())


def _session_title(s: Dict[str, Any]) -> str:
    t = (s.get("title") or "").strip()
    if t and t.lower() not in ("new chat", "untitled"):
        return t
    return f"{kind_label(s.get('agent_kind'))} conversation"


def session_href(ctx: SearchCtx, sid: str) -> str:
    return f"/admin/sessions/{sid}?env={ctx.env}"


def session_record_keys() -> List[str]:
    """Session-metadata keys holding an external record id a conversation is
    about, searchable by exact value: each plugin's ``search`` hook,
    ``session_record_keys()``."""
    from core.plugins import call_hook, load_hook

    keys: List[str] = []
    for extra in call_hook(load_hook("search"), "session_record_keys"):
        keys.extend(k for k in (extra or ()) if k not in keys)
    return keys


def customer_record_links() -> List[tuple]:
    """``(customers column, session metadata key, person label)`` triples: a
    customer whose column holds a record id is linked to the sessions whose
    metadata key carries it, and is listed under that label. Each plugin's
    ``search`` hook, ``customer_record_links()``."""
    from core.plugins import call_hook, load_hook

    links: List[tuple] = []
    for extra in call_hook(load_hook("search"), "customer_record_links"):
        links.extend(tuple(link) for link in (extra or ()))
    return links


def _session_row(ctx: SearchCtx, s: Dict[str, Any], biz: Dict[str, str], rank: int) -> Dict[str, Any]:
    n = s.get("message_count") or 0
    bits = [kind_label(s.get("agent_kind")), biz.get(s.get("business_id") or "", ""),
            when(s.get("created_at")), f"{n} message{'s' if n != 1 else ''}" if n else ""]
    return _row(_session_title(s), "Conversation", GROUP_PEOPLE, session_href(ctx, s["id"]),
                "conversations", " · ".join(b for b in bits if b), rank, pii.mask_id(s.get("id")))


def conversations(ctx: SearchCtx) -> List[Dict[str, Any]]:
    """Conversations by session id, title, or a linked record id."""
    client = _ai(ctx.env)
    found: Dict[str, tuple] = {}

    def add(rows: List[Dict[str, Any]], rank: int) -> None:
        for s in rows:
            if s.get("id") and _kind_ok(ctx, s.get("agent_kind")) and s["id"] not in found:
                found[s["id"]] = (s, rank)

    base = lambda: client.table("ai_chat_sessions").select(_SESSION_COLS).is_("deleted_at", "null")  # noqa: E731
    if pii.is_uuid(ctx.q):
        add(_rows(base().eq("id", ctx.q.strip().lower()).limit(1).execute()), 0)
    if pii.looks_like_record_id(ctx.q) and not pii.is_phone(ctx.q):
        v = ctx.q.strip()
        for key in session_record_keys():
            add(_rows(base().eq(f"metadata->>{key}", v).order("created_at", desc=True).limit(CAP).execute()), 1)
    words = [_needle(t) for t in ctx.tokens if len(_needle(t)) >= 3]
    if words and not pii.is_phone(ctx.q) and not pii.is_uuid(ctx.q):
        q = base()
        for w in words[:4]:
            q = q.ilike("title", f"%{w}%")
        if ctx.kinds is not None:
            q = q.in_("agent_kind", sorted(ctx.kinds))
        add(_rows(q.order("created_at", desc=True).limit(CAP).execute()), 5)
    biz = _business_names(ctx, (s.get("business_id") for s, _ in found.values()))
    out = []
    for s, rank in found.values():
        r = rank if rank < 5 else rank_label(_session_title(s), ctx) + 2
        out.append(_session_row(ctx, s, biz, r))
    return out[:CAP]


def traces(ctx: SearchCtx) -> List[Dict[str, Any]]:
    """Trace / session id prefixes in ClickHouse telemetry (last 30 days)."""
    if not pii.is_id_fragment(ctx.q):
        return []
    needle = ctx.q.strip().lower()
    params: Dict[str, Any] = {"q": needle, "env": ctx.env}
    where = [
        "start_time > now() - toIntervalDay(30)",
        "(startsWith(lower(toString(id)), {q:String}) OR startsWith(lower(toString(session_id)), {q:String}))",
        "(simpleJSONExtractString(metadata, 'waj.env') = {env:String} "
        "OR simpleJSONExtractString(metadata, 'waj.env') = '')",
    ]
    if ctx.kinds is not None:
        params["kinds"] = "['" + "','".join(sorted(ctx.kinds)) + "']"
        where.append("has({kinds:Array(String)}, simpleJSONExtractString(metadata, 'waj.agent_kind'))")
    sql = (
        "SELECT toString(id) AS id, toString(session_id) AS session_id, toString(start_time) AS start_time, "
        "simpleJSONExtractString(metadata, 'waj.agent_kind') AS agent_kind, top_span_name "
        "FROM traces_replacing WHERE " + " AND ".join(where) + " ORDER BY start_time DESC LIMIT 12"
    )
    seen = set()
    out = []
    for r in _ch(sql, params, 2.5):
        tid = r.get("id")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        by_session = (r.get("session_id") or "").lower().startswith(needle)
        bits = [kind_label(r.get("agent_kind") or None), when(r.get("start_time")),
                "session match" if by_session else "trace match"]
        out.append(_row(f"Conversation {pii.mask_id(r.get('session_id') or tid)}", "Conversation", GROUP_PEOPLE,
                        f"/admin/observability?env={ctx.env}#trace={tid}", "conversations",
                        " · ".join(b for b in bits if b), 1, pii.mask_id(tid)))
        if len(out) >= CAP:
            break
    return out


def _person_row(ctx: SearchCtx, *, kind: str, name: Optional[str], phone: Optional[str],
                cc: Optional[str], email: Optional[str], sessions: List[Dict[str, Any]],
                biz: Dict[str, str], rank: int) -> Optional[Dict[str, Any]]:
    if not sessions:
        return None  # people are only shown when they have a conversation here
    latest = sessions[0]
    label = pii.mask_name(name) or pii.mask_phone(phone, cc) or pii.mask_email(email) or kind
    n = len(sessions)
    bits = [pii.mask_phone(phone, cc) if name else "",
            pii.mask_email(email) if (email and not name and not phone) else "",
            f"{n} conversation{'s' if n != 1 else ''}" if n < 20 else "20+ conversations",
            biz.get(latest.get("business_id") or "", ""),
            f"last chat {when(latest.get('created_at'))}" if latest.get("created_at") else ""]
    return _row(label, kind, GROUP_PEOPLE, session_href(ctx, latest["id"]), "person" if kind != "Patient" else "patient",
                " · ".join(b for b in bits if b), rank, person=True)


def _name_rank(ctx: SearchCtx, name: Optional[str]) -> int:
    return rank_label(name or "", ctx) + 1
