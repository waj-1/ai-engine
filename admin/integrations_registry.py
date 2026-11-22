"""Static catalogue of every external system the WAJ agents touch.

This is metadata only — no secrets. Reachability is checked at render
time by the integrations handler using a 30s in-process cache.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class Integration:
    name: str
    scope: str          # "READ" | "WRITE" | "READ·WRITE"
    location: str       # "CLOUD" | "ON-PREM"
    description: str
    env_vars: List[str]  # which env vars carry credentials (for the "Rotate" button)
    health_url: Optional[str] = None  # public-ish endpoint to ping (None = no live check)


INTEGRATIONS: List[Integration] = [
    Integration("Supabase",     "READ·WRITE", "CLOUD",
                "Primary Postgres for agent state, audit, traces.",
                ["SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_KEY"],
                health_url=None),
    Integration("Google AI (Gemini)", "READ", "CLOUD",
                "LLM provider for every agent.",
                ["GOOGLE_API_KEY"], health_url="https://generativelanguage.googleapis.com/"),
    Integration("GitBook",      "READ", "CLOUD",
                "Documentation source for knowledge-base retrieval.",
                ["GITBOOK_API_KEY"], health_url=None),
    Integration("Resend",       "WRITE", "CLOUD",
                "Transactional email provider.",
                ["RESEND_API_KEY"], health_url=None),
    Integration("Twilio",       "WRITE", "CLOUD",
                "WhatsApp and SMS delivery.",
                ["TWILIO_ACCOUNT", "TWILIO_AUTH"], health_url=None),
    Integration("Stripe Connect", "READ·WRITE", "CLOUD",
                "Payments rail for booking flows.",
                ["STRIPE_CONNECT_API_KEY", "STRIPE_CONNECT_WEBHOOK_SECRET"], health_url=None),
    Integration("Sentry",       "WRITE", "CLOUD",
                "Error monitoring for the agent service.",
                ["SENTRY_DSN"], health_url=None),
]


# Scoped build: the WAJ catalog above (GitBook, Twilio, Stripe, Resend,
# Sentry, plus the plugin's own) is irrelevant — a single-client engine
# touches only these platform deps. A client's own MCP (e.g. its
# transactions MCP) is surfaced separately as a *configurable* MCP server
# (agent_mcp_servers), not a static card, so an operator can repoint it from
# the UI. Client-agnostic on purpose: every scoped deployment runs on the
# same Supabase + Gemini + Laminar stack.
SCOPED_BUILD_INTEGRATIONS: List[Integration] = [
    Integration("AI Supabase", "READ·WRITE", "CLOUD",
                "Postgres for the agent profile, knowledge base, memory, chat sessions and traces.",
                ["AI_SUPABASE_URL", "AI_SUPABASE_ANON_KEY", "AI_SUPABASE_SERVICE_KEY"],
                health_url=None),
    Integration("Google AI (Gemini)", "READ", "CLOUD",
                "LLM provider — chat, title/suggestions, and document embeddings.",
                ["GOOGLE_API_KEY"], health_url="https://generativelanguage.googleapis.com/"),
    Integration("Laminar", "WRITE", "CLOUD",
                "Self-hosted observability — traces, tokens, latency and cost.",
                ["LMNR_PROJECT_API_KEY", "LMNR_BASE_URL"], health_url=None),
]


def visible_integrations() -> List[Integration]:
    """The integrations to show on the MCP & Tools screen. Any scoped build
     gets the slim platform-deps list; everyone else gets the
    full catalog. An older gate checked for one scope by name and fell
    through to the full catalog on every other scope."""
    try:
        from core.deployment import scoped_build
        if scoped_build():
            return SCOPED_BUILD_INTEGRATIONS
    except Exception:
        pass
    return INTEGRATIONS[:1] + plugin_integrations() + INTEGRATIONS[1:]


def plugin_integrations() -> List[Integration]:
    """Cards a product plugin adds for the systems its own agents use: each
    plugin's ``integrations`` hook, ``integrations()`` -> list of
    ``Integration``. Shown right after Supabase, in plugin order."""
    from core.plugins import call_hook, load_hook

    cards: List[Integration] = []
    for extra in call_hook(load_hook("integrations"), "integrations"):
        cards.extend(extra or ())
    return cards
