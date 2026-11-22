"""Conversation insights — outcome, topic and language per finished conversation.

The Overview wants "resolved / abandoned / escalated" and a topic + language
breakdown, but nothing on the chat path records them. Rather than touching
every agent's streaming loop, this module derives them AFTER the fact from
what is already stored, in a bounded background sweep:

* **Which conversations** — ``ai_conversation_insight_candidates`` (SQL, see
  ``supabase/migrations/20261011120000_conversation_insights.sql``): sessions
  idle for ``IDLE_MINUTES`` with at least one customer message, active in the
  last ``LOOKBACK_DAYS``, never analysed or continued since. Newest first,
  ``BATCH`` per sweep — so the 30-day backfill is just the same sweep walking
  back in time a batch at a time, never a one-shot.
* **Outcome** — ``escalated`` when telemetry shows a hand-off action ran (the
  same ``HANDOFF_TOOL_RE`` the Overview's "Handed to a person" tile uses, read
  from ClickHouse) — authoritative. Otherwise the AI classifier's judgement
  (``resolved`` / ``abandoned`` / ``escalated`` / ``unknown``) from the
  transcript. Without a model key, conservative text rules
  (``rule_outcome``) — they only say ``resolved`` / ``escalated`` on an
  explicit phrase and default to ``unknown``.
* **Language** — script heuristic on the customer's own messages (Arabic vs
  Latin letters): ``ar`` / ``en`` / ``mixed``. A Latin-script conversation
  takes the classifier's ISO code when it names another language.
* **Topic** — one key from a small per-agent label set (``TOPIC_SETS``) or
  ``other``, from ONE cheap model call per conversation that returns the
  outcome and language too. Unknown keys collapse to ``other``.
* **Environment / channel** — ``metadata.environment`` (stamped by the writer
  since this change) or the conversation's ``waj.env`` trace tag; ``voice``
  when the session has a call recording (bot-driven ``simulated`` calls are
  stored flagged and excluded from every rollup).

Runs only where ``sweep_enabled()`` (the control-plane by default) and takes a
DB lease first, so overlapping revisions or a dev box on the same DB never
double-spend. Every failure is logged and retried next sweep — the sweep must
never affect chat traffic.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

LEASE_NAME = "conversation_insights"
INSIGHTS_VERSION = 1

OUTCOMES: Tuple[str, ...] = ("resolved", "abandoned", "escalated", "unknown")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def settings() -> Dict[str, Any]:
    return {
        "model": os.getenv("CONVERSATION_INSIGHTS_MODEL", "gemini-3.5-flash-lite"),
        "batch": max(1, min(200, _int_env("CONVERSATION_INSIGHTS_BATCH", 40))),
        "interval_sec": max(60, _int_env("CONVERSATION_INSIGHTS_INTERVAL_SEC", 300)),
        "idle_minutes": max(5, _int_env("CONVERSATION_INSIGHTS_IDLE_MINUTES", 30)),
        "lookback_days": max(1, min(90, _int_env("CONVERSATION_INSIGHTS_LOOKBACK_DAYS", 30))),
        # Pause between model calls — a soft rate limit (40 calls ≈ 20s).
        "call_gap_sec": float(os.getenv("CONVERSATION_INSIGHTS_CALL_GAP_SEC", "0.5") or 0.5),
    }


def sweep_enabled() -> bool:
    """``CONVERSATION_INSIGHTS=1|0`` forces it; unset follows
    ``ENABLE_ADMIN_MCP`` — i.e. it runs on the control-plane (min=max=1) and
    not on the autoscaled chat replicas."""
    flag = (os.getenv("CONVERSATION_INSIGHTS") or "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    return (os.getenv("ENABLE_ADMIN_MCP") or "").strip().lower() in ("1", "true", "yes")


def classifier_available() -> bool:
    return bool(os.getenv("GOOGLE_API_KEY"))


# ---------------------------------------------------------------------------
# Topic label sets — small, per agent family, plain-language labels
# ---------------------------------------------------------------------------

_CUSTOMER_SERVICE = {
    "booking": "Booking an appointment",
    "reschedule_or_cancel": "Rescheduling or cancelling",
    "prices": "Prices",
    "services_info": "Services and treatments",
    "hours_location": "Opening hours and location",
    "staff": "Staff and specialists",
    "offers": "Offers and packages",
    "complaint": "Complaint",
}
_COLLECTION = {
    "payment_promise": "Promised to pay",
    "payment_made": "Already paid",
    "dispute": "Disputes the amount",
    "payment_plan": "Asked for a payment plan",
    "wrong_contact": "Wrong person or number",
    "callback_request": "Asked to be called back",
}
_SUPPORT = {
    "technical_issue": "Technical problem",
    "account_access": "Account access",
    "billing": "Billing",
    "order_status": "Order or request status",
    "how_to": "How do I…",
    "complaint": "Complaint",
}
_INQUIRIES = {
    "product_info": "Product information",
    "pricing": "Prices",
    "availability": "Availability",
    "hours_location": "Opening hours and location",
    "booking": "Booking",
}
_OUTBOUND_SALES = {
    "interested": "Interested in the offer",
    "not_interested": "Not interested",
    "price_objection": "Price concerns",
    "needs_more_info": "Wanted more details",
    "callback_request": "Asked to be called back",
    "wrong_contact": "Wrong person or number",
}
_GENERIC = {
    "booking_or_order": "Booking or ordering",
    "pricing": "Prices",
    "product_info": "Product information",
    "account_billing": "Account and billing",
    "technical_issue": "Technical problem",
    "complaint": "Complaint",
}

# The demo agents' sets. A plugin maps its own agents to label sets
# (`agent_catalog.topic_sets() -> {agent_kind: {key: label}}`).
TOPIC_SETS: Dict[str, Dict[str, str]] = {
    "agents_sales": _OUTBOUND_SALES,
    "agents_collection": _COLLECTION,
    "agents_support": _SUPPORT,
    "agents_inquiries": _INQUIRIES,
}

OTHER_TOPIC = "other"
# Offered for every agent: a large share of real conversations are a greeting
# and nothing else, and "other" would hide that.
GREETING_TOPIC = "greeting_only"
_COMMON = {GREETING_TOPIC: "Only said hello"}


def _plugin_topic_sets() -> Dict[str, Dict[str, str]]:
    from core.plugins import call_hook, load_hook

    out: Dict[str, Dict[str, str]] = {}
    for found in call_hook(load_hook("agent_catalog"), "topic_sets"):
        for kind, labels in found.items():
            out.setdefault(kind, labels)
    return out


def topic_set(agent_kind: Optional[str]) -> Dict[str, str]:
    sets = {**_plugin_topic_sets(), **TOPIC_SETS}
    return {**sets.get(agent_kind or "", _GENERIC), **_COMMON}


def topic_label(topic: Optional[str], agent_kind: Optional[str] = None) -> str:
    """Plain label for a stored topic key (any agent's set, then humanised)."""
    if not topic:
        return "Not classified"
    if topic == OTHER_TOPIC:
        return "Something else"
    labels = topic_set(agent_kind)
    if topic in labels:
        return labels[topic]
    for s in list(TOPIC_SETS.values()) + [_GENERIC, _COMMON]:
        if topic in s:
            return s[topic]
    return topic.replace("_", " ").capitalize()


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------

_ARABIC = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
_LATIN = re.compile(r"[A-Za-zÀ-ɏ]")

LANGUAGE_LABELS: Dict[str, str] = {
    "ar": "Arabic", "en": "English", "mixed": "Arabic and English",
    "fr": "French", "ur": "Urdu", "hi": "Hindi", "es": "Spanish", "de": "German",
    "tr": "Turkish", "ru": "Russian", "tl": "Filipino", "fa": "Persian",
}


def language_label(code: Optional[str]) -> str:
    if not code:
        return "Unknown"
    return LANGUAGE_LABELS.get(code, code.upper())


def detect_language(texts: Iterable[str]) -> Optional[str]:
    """``ar`` / ``en`` / ``mixed`` from the customer's own messages, by
    letter script. ``None`` when there are no letters at all (digits, emoji)."""
    ar = lat = 0
    for t in texts:
        t = t or ""
        ar += len(_ARABIC.findall(t))
        lat += len(_LATIN.findall(t))
    total = ar + lat
    if total == 0:
        return None
    if ar / total >= 0.7:
        return "ar"
    if lat / total >= 0.7:
        return "en"
    return "mixed"


# ---------------------------------------------------------------------------
# No-AI outcome rules (fallback only)
# ---------------------------------------------------------------------------

_HANDOFF_TEXT = re.compile(
    r"(connect(ing)? you (with|to) (a|our|the) (team|agent|colleague|specialist|human)"
    r"|transfer(ring)? you|hand(ed|ing)? (you|this|it) (over|off) to"
    r"|(a|our) (team member|colleague|specialist|agent) will (contact|call|reach)"
    r"|سأحول|سيتم تحويل|بحولك|راح احولك|بيتواصل معك|سيتواصل معك|أحد (الزملاء|زملائي|الموظفين))",
    re.IGNORECASE,
)
_CLOSURE_TEXT = re.compile(
    r"^\W*(thanks?|thank you|thx|great|perfect|awesome|ok(ay)? thanks?|got it|done|"
    r"شكرا|شكراً|مشكور|يعطيك العافية|تمام|ممتاز|الله يعطيك|جزاك الله)",
    re.IGNORECASE,
)


def rule_outcome(messages: Sequence[Dict[str, Any]]) -> str:
    """Conservative outcome without a model: an explicit hand-off sentence
    from the agent → escalated; the customer's last message is a thank-you →
    resolved; a single unanswered customer message → abandoned; else unknown."""
    users = [m for m in messages if m.get("role") == "user"]
    assistants = [m for m in messages if m.get("role") == "assistant"]
    if any(_HANDOFF_TEXT.search(m.get("content") or "") for m in assistants[-3:]):
        return "escalated"
    if users and _CLOSURE_TEXT.search((users[-1].get("content") or "").strip()):
        return "resolved"
    if len(users) == 1 and not assistants:
        return "abandoned"
    return "unknown"


# ---------------------------------------------------------------------------
# Transcript + classifier
# ---------------------------------------------------------------------------

def build_transcript(messages: Sequence[Dict[str, Any]], *, max_chars: int = 4000,
                     per_message: int = 400) -> str:
    """Customer/Agent lines, each capped; keeps the start and the end when
    the conversation is long (the ending decides the outcome)."""
    lines = []
    for m in messages:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        text = " ".join((m.get("content") or "").split())
        if not text:
            continue
        if len(text) > per_message:
            text = text[:per_message] + "…"
        lines.append(("Customer: " if role == "user" else "Agent: ") + text)
    out = "\n".join(lines)
    if len(out) <= max_chars:
        return out
    head, tail = [], []
    budget = max_chars // 2
    used = 0
    for ln in lines:
        if used + len(ln) > budget:
            break
        head.append(ln)
        used += len(ln) + 1
    used = 0
    for ln in reversed(lines[len(head):]):
        if used + len(ln) > budget:
            break
        tail.insert(0, ln)
        used += len(ln) + 1
    return "\n".join(head + ["[… middle of the conversation omitted …]"] + tail)


_SYSTEM = (
    "You label customer conversations with an AI agent for an analytics dashboard. "
    "Read the transcript and answer with ONE JSON object and nothing else:\n"
    '{"topic": "<one key from TOPICS>", "outcome": "<resolved|abandoned|escalated|unclear>", '
    '"language": "<ISO 639-1 code of the CUSTOMER\'s messages>"}\n'
    "Outcome rules:\n"
    "- resolved: the customer got what they came for (question answered, booking/action done) "
    "or closed politely after an answer.\n"
    "- escalated: the agent handed the customer to a person or promised that a human "
    "will follow up.\n"
    "- abandoned: the customer left mid-way — stopped replying while the agent was still "
    "asking or the need was unmet.\n"
    "- unclear: none of the above can be told from the text.\n"
    "Pick the single best topic; use \"other\" only if none fits."
)


def _parse_json(raw: str) -> Optional[Dict[str, Any]]:
    text = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


_llm_cache: Dict[str, Any] = {}


def _llm(model_string: str):
    if model_string not in _llm_cache:
        from runtime.llm_factory import build_chat_model

        _llm_cache[model_string] = build_chat_model({
            "provider": "google_genai", "model_string": model_string,
            "temperature": 0.0, "max_output_tokens": 200,
            "extra_config": {"thinking_level": "minimal"},
        })
    return _llm_cache[model_string]


def classify(agent_kind: Optional[str], transcript: str, *,
             model_string: str) -> Optional[Dict[str, Any]]:
    """One model call → ``{topic, outcome, language}`` normalised to our
    vocabularies, or ``None`` when the call fails / returns junk."""
    from langchain_core.messages import HumanMessage, SystemMessage

    from core.memory.extract import content_to_text

    labels = topic_set(agent_kind)
    topics = ", ".join(list(labels) + [OTHER_TOPIC])
    try:
        resp = _llm(model_string).invoke([
            SystemMessage(content=_SYSTEM),
            HumanMessage(content="TOPICS: %s\n\nTRANSCRIPT:\n%s" % (topics, transcript)),
        ])
    except Exception as exc:  # noqa: BLE001 - one bad call never stops a sweep
        logger.info("conversation_insights: classifier call failed (%s)", exc)
        return None
    data = _parse_json(content_to_text(getattr(resp, "content", "")))
    if not data:
        return None
    topic = str(data.get("topic") or "").strip().lower()
    outcome = str(data.get("outcome") or "").strip().lower()
    lang = str(data.get("language") or "").strip().lower()[:5]
    return {
        "topic": topic if topic in labels else OTHER_TOPIC,
        "outcome": outcome if outcome in ("resolved", "abandoned", "escalated") else "unknown",
        "language": lang if re.fullmatch(r"[a-z]{2}", lang) else None,
    }


# ---------------------------------------------------------------------------
# Telemetry (ClickHouse) — env tag + hand-off actions per session, batched
# ---------------------------------------------------------------------------

def _ch(sql: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    from core.lmnr_clickhouse import query

    return query(sql, params, timeout=20.0)


def _uuid_array(ids: Iterable[str]) -> str:
    clean = []
    for i in ids:
        try:
            clean.append("'%s'" % uuid.UUID(str(i)))
        except (ValueError, TypeError):
            continue
    return "[" + ",".join(clean) + "]"


def telemetry_signals(session_ids: Sequence[str], since: datetime) -> Dict[str, Dict[str, Any]]:
    """``{session_id: {"env": str|None, "handoff": bool}}`` for the sessions
    that have traces. Empty dict when ClickHouse is unavailable — callers then
    fall back to the classifier/rules, never fail."""
    if not session_ids:
        return {}
    from admin.screens.plain_language import HANDOFF_TOOL_RE

    hours = max(1, int((datetime.now(timezone.utc) - since).total_seconds() // 3600) + 2)
    sids = _uuid_array(session_ids)
    try:
        traces = _ch(
            "SELECT toString(id) AS trace_id,\n"
            "  simpleJSONExtractString(metadata, 'waj.session_id') AS session_id,\n"
            "  simpleJSONExtractString(metadata, 'waj.env') AS env\n"
            "FROM traces_replacing\n"
            "WHERE start_time > now() - toIntervalHour({hours:UInt32})\n"
            "  AND simpleJSONExtractString(metadata, 'waj.session_id') IN {sids:Array(String)}\n"
            "LIMIT 20000",
            {"hours": hours, "sids": sids},
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("conversation_insights: telemetry lookup skipped (%s)", exc)
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    trace_to_sid: Dict[str, str] = {}
    for r in traces:
        sid = r.get("session_id") or ""
        if not sid:
            continue
        sig = out.setdefault(sid, {"env": None, "handoff": False})
        if r.get("env") and not sig["env"]:
            sig["env"] = r["env"]
        trace_to_sid[r.get("trace_id") or ""] = sid
    if not trace_to_sid:
        return out
    err = "arrayExists(x -> x.2 IN ('exception', 'error'), events)"
    try:
        hits = _ch(
            "SELECT DISTINCT toString(trace_id) AS trace_id\n"
            "FROM spans\n"
            "WHERE start_time > now() - toIntervalHour({hours:UInt32})\n"
            "  AND toString(trace_id) IN {tids:Array(String)}\n"
            "  AND name LIKE '%.tool' AND match(lower(name), {handoff_re:String})\n"
            f"  AND NOT {err}",
            {"hours": hours, "tids": _uuid_array(trace_to_sid), "handoff_re": HANDOFF_TOOL_RE},
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("conversation_insights: hand-off lookup skipped (%s)", exc)
        return out
    for r in hits:
        sid = trace_to_sid.get(r.get("trace_id") or "")
        if sid:
            out[sid]["handoff"] = True
    return out


# ---------------------------------------------------------------------------
# Environment normalisation
# ---------------------------------------------------------------------------

def normalize_env(value: Optional[str]) -> Optional[str]:
    v = (value or "").strip().lower()
    if v in ("prod", "production"):
        return "prod"
    if v in ("staging", "stage", "dev", "development"):
        return "staging"
    return None


def writer_environment() -> str:
    """The env THIS process stamps on sessions it creates (``metadata.environment``)."""
    from core.agent_profile import _default_environment

    return _default_environment()


# ---------------------------------------------------------------------------
# Analysis of one conversation
# ---------------------------------------------------------------------------

def analyze(session: Dict[str, Any], messages: Sequence[Dict[str, Any]], *,
            signal: Optional[Dict[str, Any]] = None,
            voice: Optional[Dict[str, Any]] = None,
            model_string: Optional[str] = None) -> Dict[str, Any]:
    """The ``ai_conversation_insights`` row for one session. ``model_string``
    None = no classifier (rules only)."""
    signal = signal or {}
    kind = session.get("agent_kind")
    meta = session.get("metadata") or {}
    users = [m for m in messages if m.get("role") == "user"]
    times = [m.get("created_at") for m in messages if m.get("created_at")]

    language = detect_language(m.get("content") or "" for m in users)
    llm = None
    if model_string:
        transcript = build_transcript(messages)
        if transcript:
            llm = classify(kind, transcript, model_string=model_string)

    if signal.get("handoff"):
        outcome, source = "escalated", "telemetry"
    elif llm is not None:
        outcome, source = llm["outcome"], "llm"
    else:
        outcome, source = rule_outcome(messages), "rule"

    if llm and llm.get("language") and language in (None, "en") and llm["language"] != "ar":
        language = llm["language"]

    channel = "voice" if (voice or meta.get("channel") == "voice") else "chat"
    env = normalize_env(meta.get("environment")) or normalize_env(signal.get("env"))
    return {
        "session_id": str(session.get("session_id") or session.get("id")),
        "agent_kind": kind,
        "environment": env,
        "channel": channel,
        "outcome": outcome,
        "outcome_source": source,
        "topic": (llm or {}).get("topic"),
        "language": language,
        "user_turns": len(users),
        "message_count": len(messages),
        "is_simulated": bool((voice or {}).get("simulated")),
        "started_at": session.get("created_at") or (min(times) if times else None),
        "last_message_at": session.get("updated_at") or (max(times) if times else None),
        "classifier_model": model_string if llm is not None else None,
        "version": INSIGHTS_VERSION,
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Lease + sweep
# ---------------------------------------------------------------------------

_HOLDER = "%s:%d" % (socket.gethostname(), os.getpid())


def acquire_lease(client, ttl_sec: int) -> bool:
    """Conditional UPDATE on ``ai_background_leases``: wins when the lease
    is free/expired or already ours. Atomic per row in Postgres."""
    now = datetime.now(timezone.utc)
    until = (now + timedelta(seconds=ttl_sec)).isoformat()
    now_z = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        res = (
            client.table("ai_background_leases")
            .update({"holder": _HOLDER, "lease_until": until})
            .eq("name", LEASE_NAME)
            .or_('lease_until.lt.%s,holder.eq."%s"' % (now_z, _HOLDER))
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 - table missing until the migration runs
        logger.info("conversation_insights: lease unavailable (%s)", exc)
        return False
    return bool(res.data)


def _record_run(client, result: Dict[str, Any]) -> None:
    try:
        client.table("ai_background_leases").update({
            "last_run_at": datetime.now(timezone.utc).isoformat(),
            "last_result": result,
        }).eq("name", LEASE_NAME).execute()
    except Exception:  # noqa: BLE001
        pass


def _messages_for(client, session_ids: Sequence[str]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {sid: [] for sid in session_ids}
    if not session_ids:
        return out
    rows = (
        client.table("ai_chat_messages")
        .select("session_id, role, content, created_at")
        .in_("session_id", list(session_ids))
        .order("created_at")
        .limit(20000)
        .execute().data or []
    )
    for r in rows:
        out.setdefault(str(r.get("session_id")), []).append(r)
    return out


def _voice_for(client, session_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    if not session_ids:
        return {}
    try:
        rows = (
            client.table("ai_voice_recordings")
            .select("session_id, simulated")
            .in_("session_id", list(session_ids))
            .limit(5000).execute().data or []
        )
    except Exception:  # noqa: BLE001 - recordings are optional
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        sid = str(r.get("session_id"))
        cur = out.setdefault(sid, {"simulated": True})
        # A session is simulated only if every call in it was.
        cur["simulated"] = cur["simulated"] and bool(r.get("simulated"))
    return out


def _parse_ts(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def sweep_once(client=None, *, limit: Optional[int] = None,
               use_lease: bool = True) -> Dict[str, Any]:
    """Analyse one batch of finished conversations. Returns a small summary
    (also stored on the lease row as ``last_result``)."""
    from core.auth import get_service_client

    cfg = settings()
    client = client or get_service_client()
    if use_lease and not acquire_lease(client, ttl_sec=cfg["interval_sec"] * 2):
        return {"ran": False, "reason": "lease held elsewhere or table missing"}

    started = time.monotonic()
    try:
        cands = client.rpc("ai_conversation_insight_candidates", {
            "p_idle_minutes": cfg["idle_minutes"],
            "p_lookback_days": cfg["lookback_days"],
            "p_limit": limit or cfg["batch"],
            "p_retry_unclassified": classifier_available(),
        }).execute().data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("conversation_insights: candidate query failed (%s)", exc)
        return {"ran": False, "reason": "candidate query failed: %s" % exc}

    if not cands:
        result = {"ran": True, "analysed": 0, "classified": 0, "pending": False}
        _record_run(client, result)
        return result

    sids = [str(c["session_id"]) for c in cands]
    msgs = _messages_for(client, sids)
    voice = _voice_for(client, sids)
    oldest = min((_parse_ts(c.get("created_at")) for c in cands
                  if _parse_ts(c.get("created_at"))), default=datetime.now(timezone.utc))
    signals = telemetry_signals(sids, oldest)
    model = cfg["model"] if classifier_available() else None

    rows, classified = [], 0
    for c in cands:
        sid = str(c["session_id"])
        if model and rows:
            time.sleep(cfg["call_gap_sec"])
        row = analyze(c, msgs.get(sid) or [], signal=signals.get(sid),
                      voice=voice.get(sid),
                      model_string=None if (voice.get(sid) or {}).get("simulated") else model)
        classified += 1 if row["classifier_model"] else 0
        rows.append(row)
    try:
        client.table("ai_conversation_insights").upsert(rows, on_conflict="session_id").execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("conversation_insights: write failed (%s)", exc)
        return {"ran": False, "reason": "write failed: %s" % exc}
    result = {
        "ran": True, "analysed": len(rows), "classified": classified,
        "telemetry_sessions": len(signals), "classifier": model,
        "pending": len(cands) >= (limit or cfg["batch"]),
        "seconds": round(time.monotonic() - started, 1),
    }
    _record_run(client, result)
    logger.info("conversation_insights: %s", result)
    return result


async def run_loop() -> None:
    """Background loop started from ``main.lifespan`` when ``sweep_enabled()``.
    First sweep after a short delay (boot seeders, cold caches); then every
    ``interval_sec``. A backlog (a batch came back full) re-sweeps after a
    minute so the 30-day backfill finishes in hours, not days — still one
    bounded batch at a time."""
    cfg = settings()
    await asyncio.sleep(90)
    while True:
        try:
            result = await asyncio.to_thread(sweep_once)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never let the loop die
            logger.exception("conversation_insights: sweep crashed")
            result = {}
        await asyncio.sleep(60 if result.get("pending") else cfg["interval_sec"])
