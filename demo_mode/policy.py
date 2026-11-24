"""What a demo visitor may not reach, and which requests are rate limited.

Every visitor of the public demo is signed in as the same admin user, so the
superuser check protects nothing here: these rules are the boundary. They are
applied to every HTTP request and WebSocket handshake by `demo_mode.gate`,
before any route runs, so a screen that is hidden from the navigation is also
refused when its URL is typed or posted to (GET and POST alike).

Paths match on a segment boundary: ``/admin/users`` blocks ``/admin/users``
and ``/admin/users/<id>/superuser`` but not ``/admin/users-guide``.
"""
from __future__ import annotations

import re
from typing import Mapping, Optional, Tuple

# Refused whatever the method: screens that show or change secrets, people,
# the install itself, or run arbitrary engine work.
BLOCKED_PREFIXES: Tuple[str, ...] = (
    "/admin/setup",                # the first-run page never appears
    "/admin/model-keys",           # Settings > Model keys
    "/admin/api-keys",             # Settings > API keys (no key can be minted)
    "/admin/env-vars",             # Settings > Environment variables
    "/admin/users",                # Settings > Team & access, admin changes
    "/admin/voice-budgets",        # Settings > Voice budgets
    "/admin/drift",                # Settings > Diagnostics ...
    "/admin/diagnostics",          # ... runtime environment (connection details)
    "/admin/lmnr",                 # ... telemetry check
    "/admin/tests",                # ... engineering tests (runs the test suite)
    "/admin/api/mcp-flag-parity",  # ... a deployment probe
    "/admin/docs",                 # Developer docs and the API reference
    "/admin/deployments",          # code deploy targets
    "/admin/deploy-revisions.json",
    "/admin/deploy-runs.json",
    "/admin/demo/reset",           # reserved: never a visitor action
    "/mcp",                        # the admin MCP server
    "/agents",                     # the anonymous sample-agents portal
    "/voice/live",                 # the plain voice socket (a call names its agent, below)
)

# Refused for anything but GET/HEAD: the page may be read, not changed.
READ_ONLY_PREFIXES: Tuple[str, ...] = (
    "/admin/audit",                 # the activity log (and its write probe)
    "/admin/agent-models",          # model rows (provider, endpoint, cost)
    "/admin/voice-scenarios",       # running a voice check calls a realtime model
    "/admin/eval-runs/schedules",   # scheduled runs spend model calls unattended
    "/admin/evals/bakeoffs",        # a model comparison calls several models
)

# Regular expressions, matched against the whole path, any method.
_BLOCKED_PATTERNS = (
    re.compile(r"^/admin/env/[^/]+/(deploy|rollback)$"),     # code deploys
    re.compile(r"^/admin/jobs/?$"),                           # the background jobs list
    re.compile(r"^/admin/prompts/[^/]+/optimize$"),           # an extra model call per click
)

# A voice call with one of the demo's agents: refused unless ``DEMO_VOICE`` is
# on, and then capped by `demo_mode.voice` (call length, calls at once, the
# day's seconds).
_VOICE_CALL = re.compile(r"^/v1/agents/[^/]+/voice$")

#: Requests counted against a visitor's limits: (bucket, method, path pattern).
_LIMITED = (
    ("chat", "POST", re.compile(r"^/v1/agents/[^/]+/chat$")),
    ("tests", "POST", re.compile(r"^/admin/evals/run$")),
    ("tests", "POST", re.compile(r"^/admin/eval-runs/trigger$")),
    ("tests", "POST", re.compile(r"^/admin/agents/[^/]+/e2e/run$")),
    ("tests", "POST", re.compile(r"^/admin/prompts/[^/]+/compare$")),
    ("tests", "POST", re.compile(r"^/admin/tools/dry-run$")),
    ("uploads", "POST", re.compile(r"^/admin/kb/[^/]+/upload$")),
    ("uploads", "POST", re.compile(r"^/admin/skills/[^/]+/versions/[^/]+/resources/create$")),
    ("entry", "POST", re.compile(r"^/admin/demo/enter$")),
    ("assistant", "POST", re.compile(r"^/admin/demo/assistant/chat$")),
)

#: Buckets that spend model calls: refused once the day's turns are used up.
MODEL_BUCKETS = frozenset({"chat", "tests", "assistant"})

#: Buckets whose route streams server-sent events: a refusal is a streamed
#: answer the chat panel shows, not an error page.
STREAM_BUCKETS = frozenset({"chat", "assistant"})

# Settings and search entries hidden in demo mode (admin/nav.py keys).
HIDDEN_SETTINGS = frozenset({
    "users", "api_keys", "voice_budgets", "model_keys", "variables", "diagnostics", "docs",
})


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def refusal(method: str, path: str, query: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Why a demo visitor may not make this request, or None when they may."""
    p = path.rstrip("/") or "/"
    m = (method or "GET").upper()
    for prefix in BLOCKED_PREFIXES:
        if _under(p, prefix):
            return "This part of the engine is switched off in the public demo."
    for pattern in _BLOCKED_PATTERNS:
        if pattern.match(p):
            return "This part of the engine is switched off in the public demo."
    if _VOICE_CALL.match(p) and not voice_on():
        return "Voice calls are switched off in the public demo."
    if m not in ("GET", "HEAD"):
        for prefix in READ_ONLY_PREFIXES:
            if _under(p, prefix):
                return "This screen is read-only in the public demo."
    # Settings > Environment variables is a tab of the Releases page.
    if p == "/admin/env" and (query or {}).get("tab") == "variables":
        return "This part of the engine is switched off in the public demo."
    return None


def limited_bucket(method: str, path: str) -> Optional[str]:
    """The rate-limit bucket this request counts against, or None."""
    p = path.rstrip("/") or "/"
    m = (method or "GET").upper()
    for bucket, want, pattern in _LIMITED:
        if m == want and pattern.match(p):
            return bucket
    return None


SHARED_KB_CONFIG = {"table": "ai_kb_embeddings", "pages_table": "ai_kb_pages"}


def demo_on() -> bool:
    from demo_mode import settings

    return settings.enabled()


def voice_on() -> bool:
    from demo_mode import settings

    return settings.voice_enabled()


def tool_endpoint_problem(endpoint: str, auth_env_var: Optional[str] = None) -> Optional[str]:
    """In demo mode, why a visitor may not save a tool server or HTTP tool
    at `endpoint`: a local, private or internal address (the demo's own
    Northwind server excepted), or naming a process secret as its credential.
    None when they may, and always None outside demo mode."""
    if not demo_on():
        return None
    if (auth_env_var or "").strip():
        return "Credentials from the server's environment cannot be used in the public demo."
    from runtime.tools._http_adapter import _classify_endpoint

    try:
        verdict = _classify_endpoint(endpoint or "")
    except Exception:  # noqa: BLE001 — a malformed address is refused
        verdict = "blocked"
    if verdict == "blocked":
        return ("The public demo cannot reach local or internal addresses. "
                "Use a public https address, or the built-in Northwind tools.")
    return None


def clamp_kb_payload(payload: dict) -> Optional[str]:
    """In demo mode, keep a knowledge base on the built-in tables: a visitor
    may not point one at another table (its rows would be shown on the
    Knowledge screen). Changes `payload` in place; returns why it cannot be
    saved, or None."""
    if not demo_on():
        return None
    if "kind" in payload and payload.get("kind") != "pgvector":
        return "Knowledge bases in the public demo are built-in (pgvector) ones."
    if "config" in payload:
        config = dict(payload.get("config") or {})
        config.update(SHARED_KB_CONFIG)
        payload["config"] = config
    return None


def href_allowed(href: str) -> bool:
    """For navigation and search: may this link be offered to a visitor?"""
    raw = href or ""
    path, _, qs = raw.partition("?")
    query = dict(part.split("=", 1) for part in qs.split("&") if "=" in part)
    return refusal("GET", path, query) is None
