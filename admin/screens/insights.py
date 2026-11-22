"""Conversation insights on the Overview — outcome mix, top topics, languages.

Reads ``ai_conversation_insights`` (written by the background sweep in
``core/conversation_insights.py``) through the ``ai_conversation_insights_rollup``
SQL function: one call returns counts for the current period and the one
before it, grouped by agent / outcome / topic / language / channel, so the
page never pulls rows.

Same honesty rules as the rest of the Overview:

* source down (migration not applied, DB error) → ``available: False`` and the
  template says "unavailable", never a confident zero;
* no analysed conversations yet → ``has_data: False`` and the template shows
  "Collecting data — appears after new conversations" instead of hiding;
* outcomes are labelled as an estimate (an AI reading of the transcript, or a
  hand-off action in telemetry), never as a customer-confirmed result.

Partner builds see only their own agent kinds (``overview._client_kinds``,
the same scoping the ClickHouse tiles use). Shared by ``/admin/live`` and the
admin MCP ``conversation_insights`` tool.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from core import conversation_insights as ci

logger = logging.getLogger(__name__)

# key -> (plain label, chip/bar tone, one-line meaning)
OUTCOME_META: Dict[str, tuple] = {
    "resolved": ("Resolved", "ok", "The customer got what they came for"),
    "escalated": ("Handed to a person", "info", "A hand-off action ran, or the agent passed them to the team"),
    "abandoned": ("Left before finishing", "warn", "The customer stopped replying mid-way"),
    "unknown": ("Unclear", "neutral", "Cannot be told from the conversation"),
}
OUTCOME_ORDER = ("resolved", "escalated", "abandoned", "unknown")

COLLECTING = "Collecting data — appears after new conversations"


def _rollup(client, hours: int, env: Optional[str], kinds: Sequence[str]) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"p_hours": int(hours), "p_env": env or None,
                              "p_kinds": list(kinds) if kinds else None}
    return client.rpc("ai_conversation_insights_rollup", params).execute().data or []


def _pct(part: int, whole: int) -> Optional[float]:
    return round(100.0 * part / whole, 1) if whole else None


def _trim(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return ("%d" % v) if float(v).is_integer() else ("%.1f" % v)


def _points(cur: Optional[float], prev: Optional[float], good_when: str) -> Optional[Dict[str, str]]:
    if cur is None or prev is None:
        return None
    diff = round(cur - prev, 1)
    if diff == 0:
        return {"text": "No change", "tone": "neutral"}
    if good_when == "neutral":
        return {"text": "%+.1f pts" % diff, "tone": "neutral"}
    better = (diff > 0) == (good_when == "up")
    return {"text": "%+.1f pts" % diff, "tone": "ok" if better else "bad"}


def summarize(rows: List[Dict[str, Any]], *, agent_names: Optional[Dict[str, str]] = None,
              top_n: int = 8) -> Dict[str, Any]:
    """Turn rollup rows into the Overview payload (pure — tests drive it)."""
    from admin.screens import plain_language as pl

    def n(r: Dict[str, Any]) -> int:
        try:
            return int(r.get("n") or 0)
        except (TypeError, ValueError):
            return 0

    cur = [r for r in rows if r.get("period") == "cur"]
    prev = [r for r in rows if r.get("period") == "prev"]
    total = sum(n(r) for r in cur)
    prev_total = sum(n(r) for r in prev)

    def by(rs, key):
        out: Dict[str, int] = {}
        for r in rs:
            k = r.get(key) or ""
            out[k] = out.get(k, 0) + n(r)
        return out

    oc, op = by(cur, "outcome"), by(prev, "outcome")
    outcomes = []
    for key in OUTCOME_ORDER:
        label, tone, meaning = OUTCOME_META[key]
        c = oc.get(key, 0)
        pct = _pct(c, total)
        good = {"resolved": "up", "abandoned": "down", "escalated": "neutral"}.get(key, "neutral")
        outcomes.append({
            "key": key, "label": label, "tone": tone, "meaning": meaning,
            "count": c, "pct": pct, "pct_text": _trim(pct),
            "delta": _points(pct, _pct(op.get(key, 0), prev_total), good) if prev_total else None,
        })

    # Topics are per-agent label keys; the same key (e.g. "booking") under two
    # agents is the same topic to a reader, so they merge on the plain label.
    topics_raw: Dict[str, Dict[str, Any]] = {}
    unclassified = 0
    for r in cur:
        if not r.get("topic"):
            unclassified += n(r)
            continue
        label = ci.topic_label(r.get("topic"), r.get("agent_kind"))
        t = topics_raw.setdefault(label, {"label": label, "key": r.get("topic"), "count": 0})
        t["count"] += n(r)
    classified = total - unclassified
    ranked = sorted(topics_raw.values(),
                    key=lambda t: (t["key"] == ci.OTHER_TOPIC, -t["count"], t["label"]))
    shown = [t for t in ranked if t["key"] != ci.OTHER_TOPIC][:top_n]
    rest = classified - sum(t["count"] for t in shown)
    if rest > 0:
        shown.append({"label": "Something else", "key": ci.OTHER_TOPIC, "count": rest})
    top = max((t["count"] for t in shown), default=0)
    for t in shown:
        t["pct"] = _pct(t["count"], classified)
        t["pct_text"] = _trim(t["pct"])
        t["bar"] = round(100.0 * t["count"] / top) if top else 0

    lc = by(cur, "language")
    languages = []
    for code, c in sorted(lc.items(), key=lambda kv: (-kv[1], kv[0])):
        pct = _pct(c, total)
        languages.append({"key": code or None, "label": ci.language_label(code or None),
                          "count": c, "pct": pct, "pct_text": _trim(pct)})

    ch = by(cur, "channel")

    per_agent: Dict[str, Dict[str, Any]] = {}
    for r in cur:
        kind = r.get("agent_kind") or ""
        a = per_agent.setdefault(kind, {
            "kind": kind, "name": pl.agent_label(kind or None, agent_names or {}),
            "total": 0, "outcomes": {k: 0 for k in OUTCOME_ORDER}, "_topics": {},
        })
        a["total"] += n(r)
        a["outcomes"][r.get("outcome") if r.get("outcome") in OUTCOME_META else "unknown"] += n(r)
        if r.get("topic") and r.get("topic") != ci.OTHER_TOPIC:
            lbl = ci.topic_label(r.get("topic"), kind)
            a["_topics"][lbl] = a["_topics"].get(lbl, 0) + n(r)
    agents = []
    for a in sorted(per_agent.values(), key=lambda a: -a["total"]):
        tops = sorted(a.pop("_topics").items(), key=lambda kv: -kv[1])
        a["top_topic"] = tops[0][0] if tops else None
        a["resolved_pct"] = _pct(a["outcomes"]["resolved"], a["total"])
        a["resolved_text"] = _trim(a["resolved_pct"])
        a["mix"] = [{"key": k, "label": OUTCOME_META[k][0], "tone": OUTCOME_META[k][1],
                     "count": a["outcomes"][k],
                     "pct": _pct(a["outcomes"][k], a["total"]) or 0} for k in OUTCOME_ORDER]
        agents.append(a)

    return {
        "has_data": total > 0,
        "total": total,
        "prev_total": prev_total,
        "outcomes": outcomes,
        "topics": shown,
        "topics_classified": classified,
        "topics_unclassified": unclassified,
        "languages": languages,
        "channels": {"chat": ch.get("chat", 0), "voice": ch.get("voice", 0)},
        "agents": agents,
        "by_kind": {a["kind"]: a for a in agents},
    }


def build_insights(*, env: Optional[str], hours: int, client,
                   agent_kind: Optional[str] = None,
                   agent_names: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """The Overview's insights block. ``available: False`` when the rollup
    could not be read (e.g. the migration is not applied yet)."""
    from admin.screens.overview import _client_kinds

    kinds = tuple(_client_kinds())
    if agent_kind:
        if kinds and agent_kind not in kinds:
            kinds = ("__none__",)  # a partner build never sees another's agent
        else:
            kinds = (agent_kind,)
    base = {
        "env": env, "hours": hours, "agent_kind": agent_kind or None,
        "classifier_enabled": ci.classifier_available(),
        "collecting_text": COLLECTING,
        "method": ("Outcome: a hand-off action in the conversation's telemetry, otherwise "
                   "an AI reading of the transcript (an estimate, not a customer survey). "
                   "Language: the script of the customer's own messages. Conversations are "
                   "analysed about 30 minutes after they go quiet."),
    }
    try:
        rows = _rollup(client, hours, env, kinds)
    except Exception as exc:  # noqa: BLE001 - render "unavailable", never zero
        logger.warning("overview: conversation insights unavailable (%s)", exc)
        return {**base, "available": False, "has_data": False}
    return {**base, "available": True, **summarize(rows, agent_names=agent_names)}
