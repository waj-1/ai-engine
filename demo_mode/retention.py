"""What the nightly reset keeps: the demo's own sample conversations, for a while.

The reset exists for privacy: everything a visitor typed is gone each night.
It used to do that by deleting every conversation and emptying the trace
store, so "Last 7 days", "Last 30 days", the trends and every "compared with
the previous period" figure only ever held data since 03:00. The demo's own
sample traffic (demo_mode/sample_traffic.py) is scripted text answered by the
seeded agents and holds nothing a visitor wrote, so the reset now keeps it for
``DEMO_SAMPLE_KEEP_DAYS`` (default 30; 0 is the old behaviour) and deletes
everything else as before.

The rule is: delete unless positively identified. A conversation is kept only
when ALL of this holds, checked again at every reset:

1. **Every turn carries the generator's in-process mark.** The generator sends
   a secret made when the process started (`_SECRET`: never stored, logged or
   sent outside the process) on its own in-process requests; the chat route
   turns a matching header into the trace attribute ``waj.demo.sample``
   (`trace_mark`). The "Sample traffic" channel is NOT the test: it is
   resolved from the ``Origin`` header, which anyone with the demo's token can
   send from outside a browser.
2. **The agents were exactly as seeded.** Visitors may edit the Northwind
   agents, their instructions and their knowledge bases, and a sample
   conversation held after such an edit could repeat a visitor's text. Before
   and after each conversation the generator compares a fingerprint of the
   four agents (the engine's own fully-denormalized snapshot of each,
   `runtime.compiler.build_snapshot`, plus their knowledge bases' pages and
   chunks) with the one taken right after the seed (`write_baseline`). Only a
   conversation held on unchanged agents is marked at all, and its session is
   stamped with the fingerprint afterwards (`PRISTINE_KEY`).
3. **The saved conversation is the script and nothing else**: its session
   names a script of the pool, its customer messages are that script's lines
   in order, and it has no other kind of message (`valid_sessions`).
4. **The trace store agrees**: every trace of that session is marked, there is
   one per customer message, and all are inside the window (`kept`). A visitor
   who posts into a sample conversation adds an unmarked trace or a line that
   is not in the script, and the whole conversation goes.

Anything else is deleted: visitors' chats, the builder assistant's turns and
its test chats, test runs and their judge calls, voice calls, the insights
sweep's own model calls, sample conversations held on edited agents, sample
conversations older than the window, and any trace the checks cannot read.
On any failure (the trace store is down, a query fails) nothing is kept.

Only where the demo has a trace store of its own (`trace_store.has_own_store`):
without one there is no mark to check, and the reset deletes everything.

What is not noticed: a change to a seeded agent that is made and exactly
undone between two checks. The engine's profile cache is dropped before each
sample conversation, so such a change has to happen and be undone within the
seconds one conversation takes to leave a trace of itself in a kept one.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, NamedTuple, Optional, Set, Tuple

from demo_mode import settings

logger = logging.getLogger(__name__)

#: The header the generator sends on its own in-process requests, and the
#: trace attribute a matching one becomes.
MARK_HEADER = "X-Demo-Sample"
MARK_ATTR = "waj.demo.sample"
#: This process's mark. A new one at every start; never written anywhere.
_SECRET = secrets.token_urlsafe(32)

#: Session metadata: the fingerprint the agents had during the conversation
#: (written by the generator), and "an earlier reset checked and kept this".
PRISTINE_KEY = "sample_pristine"
VERIFIED_KEY = "sample_verified"
#: Where the fingerprint taken after the seed is kept (a row only the engine
#: writes: no portal screen or assistant tool reaches this table).
BASELINE_NAME = "demo_sample_baseline"

_DELETE_CHUNK = 100


def keep_days() -> int:
    return settings.sample_keep_days()


def active() -> bool:
    """True when the reset keeps sample conversations on this install."""
    from demo_mode import trace_store

    return keep_days() > 0 and trace_store.has_own_store()


# --------------------------------------------------------------------------
# 1. The in-process mark
# --------------------------------------------------------------------------


def _script_ids() -> FrozenSet[str]:
    from demo_mode import sample_traffic

    return frozenset(s["id"] for s in sample_traffic.SCRIPTS)


def mark_headers(script_id: str) -> Dict[str, str]:
    """The header that marks one of the generator's own requests."""
    return {MARK_HEADER: f"{_SECRET}.{script_id}"}


def trace_mark(headers: Mapping[str, str]) -> Dict[str, str]:
    """``{waj.demo.sample: <script id>}`` for a request that carries this
    process's mark, ``{}`` for every other request. Never raises."""
    try:
        raw = headers.get(MARK_HEADER) or ""
    except Exception:  # noqa: BLE001
        return {}
    secret, _, script_id = raw.partition(".")
    if not secret or not hmac.compare_digest(secret.encode(), _SECRET.encode()):
        return {}
    return {MARK_ATTR: script_id} if script_id in _script_ids() else {}


# --------------------------------------------------------------------------
# 2. Were the agents exactly as seeded?
# --------------------------------------------------------------------------


#: Row bookkeeping, left out of the fingerprint: the seed rewrites its model
#: rows at every start (same content, a new ``updated_at``), and who or when
#: says nothing about what an agent runs on. Any real edit changes a column
#: that IS hashed.
_BOOKKEEPING = frozenset({"created_at", "updated_at", "created_by", "updated_by"})


def _canonical(value: Any) -> Any:
    """`value` with every list of rows in one order, so two reads of the same
    data hash the same whatever order the database returned them in."""
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
                if k not in _BOOKKEEPING}
    if isinstance(value, (list, tuple)):
        items = [_canonical(v) for v in value]
        if any(isinstance(v, dict) for v in items):
            return sorted(items, key=lambda v: json.dumps(v, sort_keys=True, default=str))
        return items
    return value


def _digest(value: Any) -> str:
    text = json.dumps(_canonical(value), sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint(client=None, env: Optional[str] = None) -> Optional[str]:
    """A hash of everything the sample conversations' agents run on: for each
    agent the engine's own snapshot of it (its fields, model, instructions
    text, tools, knowledge-base rows, tool servers, playbooks with their
    bodies, hand-over targets) and the pages and chunks of the knowledge bases
    they search. None when it cannot be read whole (an agent is missing, a
    query failed): nothing is "as seeded" then."""
    from demo_mode import sample_traffic, seed
    from runtime.compiler import build_snapshot

    try:
        client = client or seed._client()
        env = env or seed._env()
        slugs = sorted({s["agent"] for s in sample_traffic.SCRIPTS})
        rows = seed._rows(client.table("agent_profiles")
                          .select("profile_id, slug, status, is_active, served_version_id")
                          .eq("agent_kind", "custom").eq("environment", env)
                          .in_("slug", slugs).execute())
        if sorted(r["slug"] for r in rows) != slugs:
            return None
        agents: List[Dict[str, Any]] = []
        sources: Set[str] = set()
        for row in sorted(rows, key=lambda r: r["slug"]):
            snapshot = build_snapshot(client, row["profile_id"])
            sources.update(str(x) for x in (snapshot.get("data_source_ids") or []))
            agents.append({"row": row, "snapshot": snapshot})
        knowledge: Dict[str, Any] = {}
        if sources:
            ids = sorted(sources)
            knowledge["pages"] = seed._rows(
                client.table("ai_kb_pages").select("id, source_id, title, type, language, content, source")
                .in_("source_id", ids).execute())
            knowledge["chunks"] = seed._rows(
                client.table("ai_kb_embeddings").select("id, source_id, page_id, content, heading, slug")
                .in_("source_id", ids).execute())
        return _digest({"agents": agents, "knowledge": knowledge})
    except Exception as exc:  # noqa: BLE001 — unreadable is "not as seeded"
        logger.warning("demo retention: fingerprint not taken (%s)", type(exc).__name__)
        return None


def write_baseline(client=None) -> Optional[str]:
    """Record the agents' fingerprint as "as seeded". Called right after a
    seed that built the demo from nothing: the reset, and a new install's
    first start. Returns it (None when it could not be taken or stored)."""
    from demo_mode import seed

    if not active():
        return None
    try:
        client = client or seed._client()
        value = fingerprint(client)
        row = {"name": BASELINE_NAME, "holder": "demo",
               "last_run_at": datetime.now(timezone.utc).isoformat(),
               "last_result": {"fingerprint": value}}
        found = seed._first(client.table("ai_background_leases").select("name")
                            .eq("name", BASELINE_NAME).limit(1).execute())
        if found:
            client.table("ai_background_leases").update(row).eq("name", BASELINE_NAME).execute()
        else:
            client.table("ai_background_leases").insert(row).execute()
        return value
    except Exception as exc:  # noqa: BLE001 — no baseline: nothing is kept
        logger.warning("demo retention: baseline not stored (%s)", type(exc).__name__)
        return None


def read_baseline(client=None) -> Optional[str]:
    from demo_mode import seed

    try:
        client = client or seed._client()
        row = seed._first(client.table("ai_background_leases").select("last_result")
                          .eq("name", BASELINE_NAME).limit(1).execute())
        value = ((row or {}).get("last_result") or {}).get("fingerprint")
        return value if isinstance(value, str) and value else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("demo retention: baseline not read (%s)", type(exc).__name__)
        return None


def pristine_now(*, fresh_profiles: bool = False) -> Optional[str]:
    """The baseline fingerprint when the agents are exactly as seeded right
    now, else None. With `fresh_profiles`, this process's cached agent
    profiles are dropped first, so the conversation about to be held loads
    them from the database this check just read. Blocking."""
    if not active():
        return None
    baseline = read_baseline()
    if not baseline:
        return None
    if fresh_profiles:
        try:
            from core.agent_profile import AgentProfileLoader
            from demo_mode import seed

            AgentProfileLoader.invalidate("custom", seed._env())
        except Exception:  # noqa: BLE001 — no fresh load, no mark
            return None
    return baseline if fingerprint() == baseline else None


def stamp_session(session_id: str, baseline: str) -> None:
    """Write `PRISTINE_KEY` on a finished sample conversation's session. Never raises."""
    from demo_mode import seed

    try:
        client = seed._client()
        row = seed._first(client.table("ai_chat_sessions").select("metadata")
                          .eq("id", session_id).limit(1).execute())
        if row is None:
            return
        meta = dict(row.get("metadata") or {})
        meta[PRISTINE_KEY] = baseline
        client.table("ai_chat_sessions").update({"metadata": meta}).eq("id", session_id).execute()
    except Exception as exc:  # noqa: BLE001 — unstamped: the reset deletes it
        logger.warning("demo retention: session not stamped (%s)", type(exc).__name__)


# --------------------------------------------------------------------------
# 3 and 4. At the reset: which conversations are kept
# --------------------------------------------------------------------------


class Plan(NamedTuple):
    """What a reset keeps: session ids, their trace ids, and the sessions to
    stamp `VERIFIED_KEY` on once both stores have kept them."""
    sessions: FrozenSet[str] = frozenset()
    traces: FrozenSet[str] = frozenset()
    to_verify: FrozenSet[str] = frozenset()


NOTHING = Plan()


def _when(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _chunks(items: Iterable[str], size: int = _DELETE_CHUNK) -> Iterable[List[str]]:
    items = list(items)
    for start in range(0, len(items), size):
        yield items[start:start + size]


def valid_sessions(client, baseline: Optional[str], cutoff: datetime) -> Dict[str, Dict[str, Any]]:
    """The saved conversations that are a script and nothing else, held on
    agents that were as seeded, inside the window:
    ``{session id: {"turns": customer messages, "verified": bool}}``."""
    from demo_mode import insights, sample_traffic, seed

    scripts = {s["id"]: s for s in sample_traffic.SCRIPTS}
    candidates: Dict[str, Dict[str, Any]] = {}
    for row in seed._rows(client.table("ai_chat_sessions")
                          .select("id, metadata, created_at, deleted_at").execute()):
        meta = row.get("metadata") or {}
        script = scripts.get(meta.get(insights.SAMPLE_KEY)) if isinstance(meta, dict) else None
        if script is None or row.get("deleted_at") or meta.get("agent_slug") != script["agent"]:
            continue
        verified = meta.get(VERIFIED_KEY) is True
        if not verified and not (baseline and meta.get(PRISTINE_KEY) == baseline):
            continue
        created = _when(row.get("created_at"))
        if created is None or created < cutoff:
            continue
        candidates[str(row["id"])] = {"script": script, "verified": verified}
    if not candidates:
        return {}
    said: Dict[str, List[Tuple[str, str, str]]] = {}
    for chunk in _chunks(sorted(candidates)):
        for m in seed._rows(client.table("ai_chat_messages")
                            .select("session_id, role, content, created_at")
                            .in_("session_id", chunk).execute()):
            said.setdefault(str(m["session_id"]), []).append(
                (str(m.get("created_at") or ""), str(m.get("role") or ""), m.get("content") or ""))
    out: Dict[str, Dict[str, Any]] = {}
    for sid, info in candidates.items():
        messages = sorted(said.get(sid, []))
        if any(role not in ("user", "assistant") for _at, role, _text in messages):
            continue
        lines = [text for _at, role, text in messages if role == "user"]
        script_lines = list(info["script"]["messages"])
        if lines and lines == script_lines[:len(lines)]:
            out[sid] = {"turns": len(lines), "verified": info["verified"]}
    return out


def kept(sessions: Mapping[str, Mapping[str, Any]], traces: Iterable[Mapping[str, Any]],
         cutoff: datetime) -> Tuple[Set[str], Set[str]]:
    """The conversations both stores agree on, and their traces (pure).

    `traces` is the trace store's index: one row a trace with ``trace_id``,
    ``session_id`` ('' when the trace names none), ``marked`` and ``started``
    (epoch seconds). A session is kept when it is one of `sessions`, every
    trace that names it is marked and inside the window, and there is exactly
    one trace per customer message."""
    by_session: Dict[str, List[Mapping[str, Any]]] = {}
    for row in traces:
        sid = str(row.get("session_id") or "")
        if sid:
            by_session.setdefault(sid, []).append(row)
    floor = cutoff.timestamp()
    keep_sessions: Set[str] = set()
    keep_traces: Set[str] = set()
    for sid, info in sessions.items():
        rows = by_session.get(sid) or []
        if len(rows) != int(info["turns"]):
            continue
        try:
            fine = all(int(r.get("marked") or 0) == 1 and float(r.get("started") or 0) >= floor
                       for r in rows)
        except (TypeError, ValueError):
            fine = False
        if not fine:
            continue
        keep_sessions.add(sid)
        keep_traces.update(str(r["trace_id"]) for r in rows)
    return keep_sessions, keep_traces


def plan(client, now: Optional[datetime] = None) -> Plan:
    """Decide what this reset keeps. Reads only. `NOTHING` when keeping is
    off, when there is no trace store, and on any failure."""
    if not active():
        return NOTHING
    from demo_mode import trace_store

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=keep_days())
    try:
        sessions = valid_sessions(client, read_baseline(client), cutoff)
        if not sessions:
            return NOTHING
        index = trace_store.trace_index()
        if index is None:
            return NOTHING
        keep_sessions, keep_traces = kept(sessions, index, cutoff)
    except Exception as exc:  # noqa: BLE001 — cannot tell: keep nothing
        logger.warning("demo retention: nothing kept, the check failed (%s)", type(exc).__name__)
        return NOTHING
    return Plan(frozenset(keep_sessions), frozenset(keep_traces),
                frozenset(s for s in keep_sessions if not sessions[s]["verified"]))


def mark_verified(client, session_ids: Iterable[str]) -> None:
    """Stamp `VERIFIED_KEY` on the sessions this reset checked and kept, so
    the next resets (whose baseline is a new one) keep them too. A session
    that is not stamped is deleted by the next reset. Never raises."""
    from demo_mode import seed

    for sid in session_ids:
        try:
            row = seed._first(client.table("ai_chat_sessions").select("metadata")
                              .eq("id", sid).limit(1).execute())
            if row is None:
                continue
            meta = dict(row.get("metadata") or {})
            meta[VERIFIED_KEY] = True
            client.table("ai_chat_sessions").update({"metadata": meta}).eq("id", sid).execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning("demo retention: session not marked as checked (%s)", type(exc).__name__)


def delete_except(client, table: str, pk: str, session_column: str, keep: FrozenSet[str]) -> int:
    """Delete every row of `table` whose session is not one of `keep` (a row
    with no session goes too). Returns the rows removed. Raises when the
    table cannot be read: the caller then clears it whole."""
    from demo_mode import seed

    columns = pk if pk == session_column else f"{pk}, {session_column}"
    doomed = [str(r[pk]) for r in seed._rows(client.table(table).select(columns).execute())
              if str(r.get(session_column) or "") not in keep]
    for chunk in _chunks(doomed):
        client.table(table).delete().in_(pk, chunk).execute()
    return len(doomed)
