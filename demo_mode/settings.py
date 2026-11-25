"""Every demo-mode setting, read from the environment at call time.

docs/deploy/demo.md lists them for operators; keep the two in step.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

_TRUE = {"1", "true", "yes", "on"}

DEFAULT_REPO_URL = "https://github.com/waj-1/ai-engine"
DEFAULT_PUBLIC_URL = "http://localhost:8000"
#: The Azure OpenAI deployments the demo uses unless told otherwise.
DEFAULT_AGENT_DEPLOYMENT = "gpt-5.4-mini"
DEFAULT_BUILDER_DEPLOYMENT = "gpt-5.5"
DEFAULT_AZURE_API_VERSION = "2025-04-01-preview"

#: The demo admin's address. `example.com` is reserved: it can never be a
#: real person's mailbox.
DEMO_EMAIL = "demo@example.com"


def enabled() -> bool:
    """``DEMO_MODE``: the whole feature. Off by default."""
    return (os.getenv("DEMO_MODE") or "").strip().lower() in _TRUE


def _int(name: str, default: int, minimum: int = 0) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def daily_turns() -> int:
    """``DEMO_DAILY_TURNS``: model turns the whole demo may run per UTC day:
    agent chat turns, builder assistant turns and test-judge calls together."""
    return _int("DEMO_DAILY_TURNS", 2000)


def assistant_per_hour() -> int:
    """``DEMO_ASSISTANT_PER_HOUR``: messages one visitor may send the builder
    assistant per hour."""
    return _int("DEMO_ASSISTANT_PER_HOUR", 30, 1)


def assistant_tools_per_hour() -> int:
    """``DEMO_ASSISTANT_TOOLS_PER_HOUR``: tool calls the builder assistant may
    make for one visitor per hour."""
    return _int("DEMO_ASSISTANT_TOOLS_PER_HOUR", 150, 1)


def assistant_tools_per_turn() -> int:
    """``DEMO_ASSISTANT_TOOLS_PER_TURN``: tool calls the builder assistant may
    make while answering one message."""
    return _int("DEMO_ASSISTANT_TOOLS_PER_TURN", 20, 1)


def max_test_cases() -> int:
    """``DEMO_MAX_TEST_CASES``: the most test cases (cases x repeats) one test
    run may grade."""
    return _int("DEMO_MAX_TEST_CASES", 20, 1)


def chat_per_hour() -> int:
    """``DEMO_CHAT_PER_HOUR``: chat turns one visitor may send per hour."""
    return _int("DEMO_CHAT_PER_HOUR", 60, 1)


def test_runs_per_hour() -> int:
    """``DEMO_TEST_RUNS_PER_HOUR``: test runs (suites, E2E, tool dry runs) per visitor per hour."""
    return _int("DEMO_TEST_RUNS_PER_HOUR", 6, 1)


def uploads_per_hour() -> int:
    """``DEMO_UPLOADS_PER_HOUR``: document uploads per visitor per hour."""
    return _int("DEMO_UPLOADS_PER_HOUR", 10, 1)


def entries_per_hour() -> int:
    """``DEMO_ENTRIES_PER_HOUR``: "Try the demo" sign-ins per address per hour."""
    return _int("DEMO_ENTRIES_PER_HOUR", 30, 1)


def ip_multiplier() -> int:
    """``DEMO_IP_MULTIPLIER``: one network address may use this many visitors' worth
    of each limit (people behind one office or phone network share an address)."""
    return _int("DEMO_IP_MULTIPLIER", 3, 1)


def client_ip_header() -> str:
    """``DEMO_CLIENT_IP_HEADER``: a request header that carries the visitor's
    address, set by a CDN in front of the demo (e.g. ``cf-connecting-ip``).
    Blank: the connection's address, as the proxy settings resolve it.
    Only set it when every request comes through that CDN."""
    return (os.getenv("DEMO_CLIENT_IP_HEADER") or "").strip().lower()


def max_output_tokens() -> int:
    """``DEMO_MAX_OUTPUT_TOKENS``: the longest answer a model may write."""
    return _int("DEMO_MAX_OUTPUT_TOKENS", 2000, 16)


def max_upload_bytes() -> int:
    """``DEMO_MAX_UPLOAD_BYTES``: the largest document a visitor may upload."""
    return _int("DEMO_MAX_UPLOAD_BYTES", 256 * 1024, 1)


def sample_keep_days() -> int:
    """``DEMO_SAMPLE_KEEP_DAYS``: how many days the nightly reset keeps the
    demo's own sample conversations (demo_mode/retention.py), so the 7-day and
    30-day reports and every "compared with the previous period" figure have
    data. 0: the reset deletes them with everything else, as it used to."""
    return min(365, _int("DEMO_SAMPLE_KEEP_DAYS", 30))


def reset_hour_utc() -> Optional[int]:
    """``DEMO_RESET_HOUR_UTC``: the hour (0-23, UTC) of the nightly reset, or
    None when set to ``off`` (an outside scheduler runs `scripts.demo_reset`)."""
    raw = (os.getenv("DEMO_RESET_HOUR_UTC") or "").strip().lower()
    if raw in ("off", "none", "no", "false", "-1"):
        return None
    if not raw:
        return 3
    try:
        hour = int(raw)
    except ValueError:
        return 3
    return hour if 0 <= hour <= 23 else 3


_FALSE = {"0", "false", "no", "off"}


def tour_enabled() -> bool:
    """``DEMO_TOUR``: the guided tour (admin/static/demo-tour.js) in demo mode.
    On by default; ``false`` switches it off without a code change. Never on
    outside demo mode."""
    if not enabled():
        return False
    return (os.getenv("DEMO_TOUR") or "").strip().lower() not in _FALSE


def reset_id(now: Optional[datetime] = None) -> str:
    """The UTC date (``YYYY-MM-DD``) of the last nightly reset: today once
    ``DEMO_RESET_HOUR_UTC`` has passed, else yesterday. The same rule as
    `demo_mode.reset.run_loop`, which resets once per UTC day at that hour.
    With the reset off (an outside scheduler), midnight UTC stands in for it.
    The tour keys its saved progress to this, so it clears with the data."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    hour = reset_hour_utc()
    if hour is None:
        hour = 0
    day = now.date()
    if now.hour < hour:
        day = day - timedelta(days=1)
    return day.isoformat()


def public_url() -> str:
    """``DEMO_PUBLIC_URL``: the demo's own address, as visitors reach it."""
    return ((os.getenv("DEMO_PUBLIC_URL") or "").strip() or DEFAULT_PUBLIC_URL).rstrip("/")


def repo_url() -> str:
    """``DEMO_REPO_URL``: where the banner sends people who want to self-host."""
    return (os.getenv("DEMO_REPO_URL") or "").strip() or DEFAULT_REPO_URL


def _str(name: str, default: str) -> str:
    return (os.getenv(name) or "").strip() or default


def agent_deployment() -> str:
    """``DEMO_AGENT_DEPLOYMENT``: the chat deployment the demo's agents use."""
    return _str("DEMO_AGENT_DEPLOYMENT", DEFAULT_AGENT_DEPLOYMENT)


def builder_deployment() -> str:
    """``DEMO_BUILDER_DEPLOYMENT``: the stronger deployment the builder
    assistant uses."""
    return _str("DEMO_BUILDER_DEPLOYMENT", DEFAULT_BUILDER_DEPLOYMENT)


def judge_deployment() -> str:
    """``DEMO_JUDGE_DEPLOYMENT``: the deployment that grades test runs
    (default: the builder's)."""
    return _str("DEMO_JUDGE_DEPLOYMENT", builder_deployment())


def model_provider() -> str:
    """``DEMO_MODEL_PROVIDER``: ``azure_openai`` (default: endpoint and key from
    ``AZURE_OPENAI_ENDPOINT`` / ``AZURE_OPENAI_API_KEY``) or ``openai`` for any
    OpenAI-compatible server at ``DEMO_MODEL_BASE_URL`` (a local Ollama, say)."""
    value = _str("DEMO_MODEL_PROVIDER", "azure_openai").lower()
    return value if value in ("azure_openai", "openai") else "azure_openai"


def model_base_url() -> str:
    """``DEMO_MODEL_BASE_URL``: the server for ``DEMO_MODEL_PROVIDER=openai``."""
    return _str("DEMO_MODEL_BASE_URL", "")


def azure_api_version() -> str:
    """``DEMO_AZURE_API_VERSION``: the Azure OpenAI API version of the demo's
    chat model rows."""
    return _str("DEMO_AZURE_API_VERSION", DEFAULT_AZURE_API_VERSION)


def reasoning_effort() -> str:
    """``DEMO_REASONING_EFFORT``: reasoning effort for the demo's models
    (``low`` keeps answers quick and cheap; ``none`` sends none)."""
    return _str("DEMO_REASONING_EFFORT", "low").lower()


# --------------------------------------------------------------------------
# Voice calls (demo_mode/voice.py). Off unless DEMO_VOICE is on.
# --------------------------------------------------------------------------

#: The variable that holds the key of the demo's Azure Voice Live resource.
#: A name of its own, so setting it unlocks the demo's voice model and nothing else.
VOICE_KEY_ENV = "DEMO_VOICE_API_KEY"
_VOICE_PROVIDERS = ("azure_voice_live", "google_genai")
_VOICE_MODEL_DEFAULTS = {"azure_voice_live": "gpt-4.1-mini",
                         "google_genai": "gemini-3.1-flash-live-preview"}
_VOICE_NAME_DEFAULTS = {"azure_voice_live": "en-US-AvaMultilingualNeural",
                        "google_genai": "Kore"}


def voice_enabled() -> bool:
    """``DEMO_VOICE``: let visitors place voice calls with the demo's agents.
    Off by default: a realtime model bills by the minute."""
    return enabled() and (os.getenv("DEMO_VOICE") or "").strip().lower() in _TRUE


def voice_daily_seconds() -> int:
    """``DEMO_VOICE_DAILY_SECONDS``: seconds of voice calls the whole demo may
    run per UTC day, all agents and all visitors together."""
    return _int("DEMO_VOICE_DAILY_SECONDS", 1800)


def voice_recording() -> bool:
    """``DEMO_VOICE_RECORDING``: keep the audio of calls with the demo's own
    agents (off by default). Every visitor shares one login, so a recording
    can be played by anyone visiting the demo until the nightly reset deletes
    it; the Voice tab says so. The Build with AI microphone and the product
    page's showcase calls are never recorded."""
    return (os.getenv("DEMO_VOICE_RECORDING") or "").strip().lower() in _TRUE


def voice_max_call_seconds() -> int:
    """``DEMO_VOICE_MAX_CALL_SECONDS``: the longest one call may last."""
    return _int("DEMO_VOICE_MAX_CALL_SECONDS", 180, 30)


def voice_max_calls() -> int:
    """``DEMO_VOICE_MAX_CALLS``: calls that may be open at the same time."""
    return _int("DEMO_VOICE_MAX_CALLS", 2, 1)


def voice_calls_per_hour() -> int:
    """``DEMO_VOICE_CALLS_PER_HOUR``: calls one visitor may place per hour."""
    return _int("DEMO_VOICE_CALLS_PER_HOUR", 6, 1)


def voice_provider() -> str:
    """``DEMO_VOICE_PROVIDER``: ``azure_voice_live`` (default: the resource at
    ``DEMO_VOICE_ENDPOINT``, key in ``DEMO_VOICE_API_KEY``) or ``google_genai``
    (Gemini Live, key in ``GOOGLE_API_KEY``)."""
    value = _str("DEMO_VOICE_PROVIDER", _VOICE_PROVIDERS[0]).lower()
    return value if value in _VOICE_PROVIDERS else _VOICE_PROVIDERS[0]


def voice_endpoint() -> str:
    """``DEMO_VOICE_ENDPOINT``: the Azure Voice Live resource, e.g.
    ``https://<resource>.services.ai.azure.com``."""
    return _str("DEMO_VOICE_ENDPOINT", "").rstrip("/")


def voice_model() -> str:
    """``DEMO_VOICE_MODEL``: the realtime model the demo's calls use."""
    return _str("DEMO_VOICE_MODEL", _VOICE_MODEL_DEFAULTS[voice_provider()])


def voice_api_version() -> str:
    """``DEMO_VOICE_API_VERSION``: the Azure Voice Live API version."""
    return _str("DEMO_VOICE_API_VERSION", "2026-04-10")


def voice_name() -> str:
    """``DEMO_VOICE_NAME``: the voice callers hear."""
    return _str("DEMO_VOICE_NAME", _VOICE_NAME_DEFAULTS[voice_provider()])


def voice_language() -> str:
    """``DEMO_VOICE_LANGUAGE``: the language of the demo's calls."""
    return _str("DEMO_VOICE_LANGUAGE", "en-US")


# --------------------------------------------------------------------------
# The showcase (demo_mode/showcase.py): the Falak Connect use-case agents,
# talked to anonymously from a product page on another site. Off unless
# DEMO_SHOWCASE is on. Its caps are its own, apart from the portal's.
# --------------------------------------------------------------------------


def showcase_enabled() -> bool:
    """``DEMO_SHOWCASE``: seed the Falak Connect use-case agents and open the
    public showcase routes (``/showcase/...``) to the origins in
    ``DEMO_SHOWCASE_ORIGINS``. Off by default."""
    return enabled() and (os.getenv("DEMO_SHOWCASE") or "").strip().lower() in _TRUE


def showcase_origins() -> list:
    """``DEMO_SHOWCASE_ORIGINS``: comma-separated origins (``https://host``)
    of the product pages that may start a showcase chat or call. The demo's
    own address (``DEMO_PUBLIC_URL``) is always allowed."""
    raw = (os.getenv("DEMO_SHOWCASE_ORIGINS") or "").split(",")
    out = [o.strip().rstrip("/").lower() for o in raw if o.strip()]
    own = public_url().lower()
    if own not in out:
        out.append(own)
    return out


def showcase_page_url() -> str:
    """``DEMO_SHOWCASE_PAGE_URL``: the product page built from these agents.
    The portal links to it ("See these agents in a real product page");
    blank hides the link."""
    return _str("DEMO_SHOWCASE_PAGE_URL", "")


def showcase_daily_turns() -> int:
    """``DEMO_SHOWCASE_DAILY_TURNS``: showcase chat turns per UTC day, every
    visitor together (apart from ``DEMO_DAILY_TURNS``)."""
    return _int("DEMO_SHOWCASE_DAILY_TURNS", 500)


def showcase_chat_per_hour() -> int:
    """``DEMO_SHOWCASE_CHAT_PER_HOUR``: showcase chat turns one visitor may
    send per hour."""
    return _int("DEMO_SHOWCASE_CHAT_PER_HOUR", 30, 1)


def showcase_voice_enabled() -> bool:
    """``DEMO_SHOWCASE_VOICE``: voice calls from the product page. Needs
    ``DEMO_VOICE`` (the realtime model) and is on with it unless set to off."""
    raw = (os.getenv("DEMO_SHOWCASE_VOICE") or "").strip().lower()
    return showcase_enabled() and voice_enabled() and raw not in ("0", "false", "no", "off")


def showcase_voice_daily_seconds() -> int:
    """``DEMO_SHOWCASE_VOICE_DAILY_SECONDS``: seconds of showcase calls per UTC
    day, every visitor together (apart from ``DEMO_VOICE_DAILY_SECONDS``)."""
    return _int("DEMO_SHOWCASE_VOICE_DAILY_SECONDS", 2700)


def showcase_voice_max_call_seconds() -> int:
    """``DEMO_SHOWCASE_VOICE_MAX_CALL_SECONDS``: the longest one showcase call may last."""
    return _int("DEMO_SHOWCASE_VOICE_MAX_CALL_SECONDS", 150, 30)


def showcase_voice_max_calls() -> int:
    """``DEMO_SHOWCASE_VOICE_MAX_CALLS``: showcase calls open at the same time."""
    return _int("DEMO_SHOWCASE_VOICE_MAX_CALLS", 3, 1)


def showcase_voice_calls_per_hour() -> int:
    """``DEMO_SHOWCASE_VOICE_CALLS_PER_HOUR``: showcase calls one visitor may
    place per hour."""
    return _int("DEMO_SHOWCASE_VOICE_CALLS_PER_HOUR", 4, 1)


# --------------------------------------------------------------------------
# Usage analytics (demo_mode/analytics.py). Off unless DEMO_POSTHOG_KEY is set.
# --------------------------------------------------------------------------

#: The PostHog cloud ingestion host used when DEMO_POSTHOG_HOST is unset.
DEFAULT_POSTHOG_HOST = "https://us.i.posthog.com"


def posthog_key() -> str:
    """``DEMO_POSTHOG_KEY``: a PostHog project API key (the public, write-only
    key a browser sends events with, not a secret). Blank: no analytics."""
    return _str("DEMO_POSTHOG_KEY", "")


def posthog_host() -> str:
    """``DEMO_POSTHOG_HOST``: where the browser sends events (PostHog cloud,
    or your own proxy in front of it)."""
    return _str("DEMO_POSTHOG_HOST", DEFAULT_POSTHOG_HOST).rstrip("/")


#: Variables that would loosen the demo's lock-down, and what demo mode makes of them.
_REMOVED_IN_DEMO = (
    "MCP_ALLOW_LOCAL_ENDPOINTS",   # tools on local/internal addresses (the SSRF guard's escape)
    "HTTP_TOOL_ALLOW_HOSTS",       # the same, per host
    "ENABLE_ADMIN_MCP",            # the admin MCP server at /mcp
    "VOICE_LIVE_SIM_ENABLED",      # the simulated voice provider
)
_FORCED_IN_DEMO = {
    "CONVERSATION_INSIGHTS": "0",  # a background sweep that calls a model on every chat (see below)
    "ENABLE_GENERIC_AGENTS": "1",  # the demo agents answer at /v1/agents/<slug>/chat
    "VOICE_RECORDING_ENABLED": "0",  # a visitor's voice is never kept (everyone shares one login)
    "TOOL_ERROR_EVENTS": "1",  # a tool server's error counts as a failed action (Overview)
}


def apply_process_lockdown(environ=None) -> list:
    """In demo mode, remove the variables that would loosen the lock-down from
    this process's environment, so every reader of them sees them unset.

    Done at import of `main` (before the admin MCP server would be mounted),
    because the compose stack's generated settings switch some of them on for
    local development and are loaded after the operator's own. Returns the
    names it changed. A no-op outside demo mode."""
    env = os.environ if environ is None else environ
    if (env.get("DEMO_MODE") or "").strip().lower() not in _TRUE:
        return []
    changed = []
    for name in _REMOVED_IN_DEMO:
        if name in env:
            del env[name]
            changed.append(name)
    for name, value in _FORCED_IN_DEMO.items():
        if env.get(name) != value:
            env[name] = value
            changed.append(name)
    # Call recording follows DEMO_VOICE_RECORDING and nothing else; which
    # calls are kept is `demo_mode.voice.may_record`.
    if (env.get("DEMO_VOICE_RECORDING") or "").strip().lower() in _TRUE:
        env["VOICE_RECORDING_ENABLED"] = "1"
    # The insights sweep runs under the demo's own rules and daily budget
    # (demo_mode/insights.py); DEMO_INSIGHTS_DAILY_CALLS=0 keeps it off.
    if (env.get("DEMO_INSIGHTS_DAILY_CALLS") or "").strip() != "0":
        env["CONVERSATION_INSIGHTS"] = "1"
    # Knowledge bases embed with the demo's own Azure OpenAI resource unless
    # the operator chose another embedder.
    if not (env.get("EMBED_PROVIDER") or "").strip() and not (env.get("EMBED_MODEL") or "").strip():
        env["EMBED_PROVIDER"] = "azure"
        changed.append("EMBED_PROVIDER")
    return changed
