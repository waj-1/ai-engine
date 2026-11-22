"""Plain-language helpers shared by the Overview and Conversations screens.

The Operate screens are read by partners (non-engineers), so engineering names
— ``agent_kind`` slugs, tool ``python_name``s, exception class names — are
turned into words here, in ONE place, and the raw names are kept only for the
"Technical" views. Everything is best-effort: a lookup that fails falls back
to a humanised version of the slug, never to an error.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def humanize_slug(slug: Optional[str]) -> str:
    """``send_payment_link`` → ``Send payment link``; ``agents_collection`` →
    ``Collection``. Used when no display name is configured."""
    if not slug:
        return "Unknown"
    s = str(slug)
    for prefix in ("agents_",):
        if s.startswith(prefix) and len(s) > len(prefix):
            s = s[len(prefix):]
    s = re.sub(r"[._\-]+", " ", s).strip()
    return (s[:1].upper() + s[1:]) if s else "Unknown"


def tool_base_name(span_or_tool_name: Optional[str]) -> str:
    """A tool span is named ``<tool>.tool``; strip the suffix."""
    name = (span_or_tool_name or "").strip()
    return name[: -len(".tool")] if name.endswith(".tool") else name


def load_tool_display_names(env: Optional[str] = None) -> Dict[str, str]:
    """``python_name -> display_name`` from ``agent_tools``. ``{}`` on failure."""
    try:
        if env:
            from admin.router import client_for
            client = client_for(env)
        else:
            from core.auth import admin_service_client
            client = admin_service_client()
        rows = (
            client.table("agent_tools").select("python_name, display_name")
            .limit(2000).execute().data or []
        )
    except Exception as exc:  # noqa: BLE001 - names are cosmetic
        logger.debug("tool display names unavailable (%s)", exc)
        return {}
    out: Dict[str, str] = {}
    for r in rows:
        py = (r.get("python_name") or "").strip()
        disp = (r.get("display_name") or "").strip()
        if py and disp and py not in out:
            out[py] = disp
    return out


def action_label(tool_name: Optional[str], names: Optional[Dict[str, str]] = None) -> str:
    """What a partner reads for a tool call: its configured display name, or a
    humanised python name ("Send payment link")."""
    base = tool_base_name(tool_name)
    if names and base in names:
        return names[base]
    return humanize_slug(base)


def load_agent_display_names(env: Optional[str]) -> Dict[str, str]:
    """``agent_kind -> display name`` for the env's active profiles.

    Prefers the root profile (no parent) of each kind; falls back to the other
    env's profiles so a kind that only exists on one side still reads as a
    name. ``{}`` on failure — callers then humanise the slug."""
    out: Dict[str, str] = {}
    try:
        from admin.router import client_for
        envs = [e for e in (env, "prod", "staging") if e]
        seen_envs: List[str] = []
        for e in envs:
            if e in seen_envs:
                continue
            seen_envs.append(e)
            rows = (
                client_for(e).table("agent_profiles")
                .select("agent_kind, display_name, parent_profile_id, is_active")
                .eq("environment", e).eq("is_active", True)
                .execute().data or []
            )
            # Root profiles first so a sub-agent's name never wins the kind.
            rows.sort(key=lambda r: 1 if r.get("parent_profile_id") else 0)
            for r in rows:
                kind = r.get("agent_kind")
                name = (r.get("display_name") or "").strip()
                if kind and name and kind not in out:
                    out[kind] = name
    except Exception as exc:  # noqa: BLE001 - names are cosmetic
        logger.debug("agent display names unavailable (%s)", exc)
    return out


def agent_label(kind: Optional[str], names: Optional[Dict[str, str]] = None) -> str:
    if kind and names and kind in names:
        return names[kind]
    if not kind:
        return "Unlabelled agent"
    return humanize_slug(kind)


def fetch_business_names(ids: Iterable[Optional[str]]) -> Dict[str, str]:
    """``business_id -> business name`` from the MAIN project's ``business``
    table (the AI project cannot see it). ``{}`` on any failure."""
    wanted = sorted({str(i) for i in ids if i})
    if not wanted:
        return {}
    try:
        from core.auth import get_main_client
        rows = (
            get_main_client().table("business")
            .select("id, business_name")
            .in_("id", wanted[:200])
            .execute().data or []
        )
    except Exception as exc:  # noqa: BLE001 - names are cosmetic
        logger.debug("business names unavailable (%s)", exc)
        return {}
    return {
        str(r["id"]): (r.get("business_name") or "").strip()
        for r in rows if r.get("id") and (r.get("business_name") or "").strip()
    }


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------

# Tools that hand the conversation to a person. Matched case-insensitively on
# the tool name. The SAME pattern runs in ClickHouse (``match()``, RE2) and in
# Python, so keep it RE2-compatible (no look-arounds).
HANDOFF_TOOL_RE = r"(handoff|hand_off|escalat|transfer_to_(human|agent)|human_agent|live_agent)"
_HANDOFF = re.compile(HANDOFF_TOOL_RE, re.IGNORECASE)


def is_handoff_tool(name: Optional[str]) -> bool:
    return bool(name) and bool(_HANDOFF.search(tool_base_name(name)))


# Outcome keys → (label, chip variant). Labels are deliberately modest: the
# data says whether an action failed or a hand-off tool ran — it does NOT say
# whether the customer's problem was solved, so nothing here claims
# "resolved".
OUTCOMES: Dict[str, tuple] = {
    "attention": ("Needs attention", "err"),
    "handoff": ("Handed to a person", "info"),
    "ok": ("No problems", "ok"),
}


def outcome_for(*, failed_actions: int, has_error: bool, handoffs: int,
                slow: bool = False) -> str:
    """``slow``: a turn ran past ``observability.SLOW_TURN_SEC`` — the caller
    most likely gave up waiting, so it needs a look even without an error."""
    if failed_actions or has_error or slow:
        return "attention"
    if handoffs:
        return "handoff"
    return "ok"


# ---------------------------------------------------------------------------
# Failure reasons
# ---------------------------------------------------------------------------

_REASONS = (
    (re.compile(r"time ?out|timed out|deadline exceeded|readtimeout", re.I),
     "the service did not respond in time"),
    (re.compile(r"connect(ion)?(error| refused| reset)|unreachable|name or service not known|"
                r"getaddrinfo|network is unreachable|no route to host", re.I),
     "the service could not be reached"),
    (re.compile(r"\b(401|403)\b|unauthori[sz]ed|forbidden|permission denied|access denied|"
                r"invalid api key|not allowed", re.I),
     "access was refused"),
    (re.compile(r"\b429\b|rate.?limit|quota|resource.?exhausted|too many requests", re.I),
     "a usage limit was reached"),
    (re.compile(r"\b404\b|not found|does not exist|no such", re.I),
     "the record could not be found"),
    (re.compile(r"\b5\d\d\b|internal server error|bad gateway|service unavailable|"
                r"gateway timeout", re.I),
     "the service returned an error"),
    (re.compile(r"validation|invalid|missing required|required field|valueerror|"
                r"typeerror|keyerror|bad request|\b400\b|\b422\b", re.I),
     "some details were missing or invalid"),
)


def plain_failure_reason(*texts: Optional[str]) -> str:
    """One short phrase for why something failed, from an exception type /
    message / tool output. Never echoes the raw text (that goes to the
    Technical view)."""
    blob = " ".join(t for t in texts if t)
    for pattern, phrase in _REASONS:
        if pattern.search(blob):
            return phrase
    return "something unexpected went wrong"


def exception_from_event_attrs(raw: Any) -> Dict[str, str]:
    """``{"type", "message"}`` from an OTel exception event's attribute blob
    (a JSON string on the ``spans.events`` tuple)."""
    attrs: Dict[str, Any] = {}
    if isinstance(raw, dict):
        attrs = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                attrs = parsed
        except Exception:  # noqa: BLE001
            attrs = {}
    return {
        "type": str(attrs.get("exception.type") or "").strip(),
        "message": str(attrs.get("exception.message") or "").strip(),
    }


def failure_sentence(label: str, reason: str, *, is_action: bool = True) -> str:
    """"Send payment link failed — the service did not respond in time"."""
    if is_action:
        return "%s failed — %s" % (label, reason)
    return "The agent ran into a problem — %s" % reason
