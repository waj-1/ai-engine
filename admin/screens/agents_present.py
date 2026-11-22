"""Plain-language presentation helpers for the Agents area.

The Agents screens are read by partners (non-engineers), so engineering
identifiers — endpoint paths, `__builtin__:<kind>` bundle names, agent_kind
slugs, UUIDs — are turned into words here and shown only as a muted
"technical name". Nothing in this module reads or writes the database; the
loaders in admin/screens/agents.py and admin/screens/agents_list.py call it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from voice.live.config import promote_legacy_voice_config


# --------------------------------------------------------------------------
# Channel — who talks to the agent, derived from its endpoint and voice block
# --------------------------------------------------------------------------

# (key, label, plain description). `key` drives the list filter.
_CHAT = ("chat", "Chat", "People chat with it in an app or on a website")
_API = ("api", "API", "Called by another system")
_DELEGATED = ("delegated", "Via another agent", "Only runs when another agent hands it work")
_NONE = ("none", "Not connected", "No channel — nothing calls this agent yet")
_VOICE = ("voice", "Voice", "Answers phone or in-app voice calls")


def endpoint_channels() -> Tuple[Tuple[str, str, str, str], ...]:
    """(path fragment, key, label, description) for the endpoints a plugin
    names (`agent_catalog.endpoint_channels()`), checked before chat/API."""
    from admin.agent_kinds import catalog_tuple

    return catalog_tuple("endpoint_channels")


def channel_filter_options() -> List[Tuple[str, str]]:
    """(key, label) for the Agents list's channel filter."""
    opts = [("chat", "Chat"), ("voice", "Voice")]
    api_also = [label.lower() for _f, key, label, _d in endpoint_channels() if key == "api"]
    for _frag, key, label, _desc in endpoint_channels():
        if key != "api" and all(k != key for k, _ in opts):
            opts.append((key, label))
    opts.append(("api", " / ".join(["API", *api_also])))
    opts.append(("delegated", "Via another agent"))
    return opts


def voice_block(profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The profile's flat voice block, or {} for a missing/malformed column."""
    stored = (profile or {}).get("voice_config")
    if stored is not None and not isinstance(stored, dict):
        return {}
    try:
        return promote_legacy_voice_config(stored) or {}
    except Exception:  # noqa: BLE001 — presentation must never 500 a page
        return {}


def has_voice(profile: Optional[Dict[str, Any]]) -> bool:
    """True when the profile itself carries a duplex voice setup."""
    vc = voice_block(profile)
    return bool(vc.get("model_id") or vc.get("agent_profile_slug")
                or vc.get("persona_prompt_slug"))


def channels_for(profile: Dict[str, Any], *, answers_voice_for: Optional[str] = None
                 ) -> List[Dict[str, str]]:
    """Every channel the agent is reached through, most important first.

    ``answers_voice_for`` is the display name of a parent whose voice calls
    this profile answers (the voice "brain"), when there is one.
    """
    path = (profile.get("endpoint_path") or "").strip()
    out: List[Tuple[str, str, str]] = []
    if path:
        named = next((c for c in endpoint_channels() if c[0] in path), None)
        if named is not None:
            out.append(named[1:])
        elif "chat" in path:
            out.append(_CHAT)
        else:
            out.append(_API)
    if not path and profile.get("agent_kind") == "custom" and profile.get("slug") \
            and profile.get("status") in ("active", "specialist"):
        # A generic agent stores no address: it is served by name. Shown only
        # while the API is switched on, or the chip would promise a route that
        # answers 404.
        from endpoints.agents_v1 import generic_agents_enabled

        if generic_agents_enabled():
            out.append(("api", "API", f"Called at /v1/agents/{profile['slug']}/chat"))
    if has_voice(profile) or answers_voice_for:
        out.append(_VOICE)
    if not out:
        if profile.get("parent_profile_id") or profile.get("delegatable") \
                or (profile.get("status") == "specialist"):
            out.append(_DELEGATED)
        else:
            out.append(_NONE)
    return [{"key": k, "label": label, "description": desc} for k, label, desc in out]


# --------------------------------------------------------------------------
# Status / type
# --------------------------------------------------------------------------

STATUS_LABELS = {
    "active": ("Active", "ok"),
    "specialist": ("Specialist", "ok"),
    "draft": ("Draft", "warn"),
    "archived": ("Archived", "neutral"),
    "template": ("Template", "info"),
}


def status_info(status: Optional[str]) -> Dict[str, str]:
    s = status or "active"
    label, variant = STATUS_LABELS.get(s, (s.capitalize(), "neutral"))
    # The list's Status filter has three buckets; a specialist is live.
    bucket = {"specialist": "active", "template": "template"}.get(s, s)
    return {"key": s, "label": label, "variant": variant, "bucket": bucket}


# --------------------------------------------------------------------------
# Toolset (MCP server) names
# --------------------------------------------------------------------------

_BUILTIN_PREFIX = "__builtin__:"


def humanize_slug(value: str) -> str:
    words = value.replace("-", " ").replace("_", " ").split()
    if not words:
        return value
    text = " ".join(words)
    return text[0].upper() + text[1:]


def toolset_label(server: Optional[Dict[str, Any]]) -> str:
    """`__builtin__:business` → "Business tools"; `support-mock` → "Support mock"."""
    name = (server or {}).get("name") or ""
    if name.startswith(_BUILTIN_PREFIX):
        return humanize_slug(name[len(_BUILTIN_PREFIX):]) + " tools"
    return humanize_slug(name) if name else "Unnamed toolset"


def toolset_description(server: Optional[Dict[str, Any]]) -> str:
    s = server or {}
    guidance = (s.get("usage_guidance") or "").strip()
    if guidance:
        return guidance
    if s.get("source") == "bundle":
        return "Built-in actions that ship with the engine."
    return "Actions served by an external system, discovered when the agent connects."


def action_group(name: str) -> str:
    """Bucket an action for the action picker."""
    n = (name or "").lower()
    if n.startswith(("propose_", "create_", "update_", "delete_", "send_", "book_",
                     "cancel_", "set_", "add_", "remove_", "escalate")):
        return "Make changes"
    if n.startswith(("get_", "list_", "search_", "find_", "lookup_", "fetch_",
                     "read_", "query_", "count_", "check_")):
        return "Read data"
    return "Other"


ACTION_GROUP_ORDER = ("Read data", "Make changes", "Other")


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def relative_time(value: Any, now: Optional[datetime] = None) -> str:
    """"3 days ago", "just now"… — empty string when unknown."""
    dt = _parse_ts(value)
    if dt is None:
        return ""
    now = now or datetime.now(timezone.utc)
    secs = int((now - dt).total_seconds())
    if secs < 60:
        return "just now"
    for unit, size in (("year", 31536000), ("month", 2592000), ("week", 604800),
                       ("day", 86400), ("hour", 3600), ("minute", 60)):
        if secs >= size:
            n = secs // size
            return f"{n} {unit}{'' if n == 1 else 's'} ago"
    return "just now"


def short_date(value: Any) -> str:
    dt = _parse_ts(value)
    return dt.strftime("%Y-%m-%d") if dt else ""


# --------------------------------------------------------------------------
# Counts
# --------------------------------------------------------------------------


def plural(n: int, word: str, plural_word: Optional[str] = None) -> str:
    return f"{n} {word if n == 1 else (plural_word or word + 's')}"


def can_use_summary(tools: Optional[int], knowledge: int, subagents: int,
                    external: bool = False) -> str:
    parts: List[str] = []
    if tools is None:
        parts.append("tools unknown")
    else:
        parts.append(plural(tools, "action") + (" + external" if external else ""))
    if knowledge:
        parts.append(plural(knowledge, "knowledge source"))
    if subagents:
        parts.append(plural(subagents, "sub-agent"))
    return " · ".join(parts)


def first_lines(text: Optional[str], n: int = 3, width: int = 220) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    out = " ".join(lines[:n])
    return out if len(out) <= width else out[: width - 1].rstrip() + "…"


def unique(items: Iterable[str]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for i in items:
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out
