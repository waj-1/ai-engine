"""Boot seeder for the demo agent: one generic (`custom`) agent, slug `demo`.

A new install has something to talk to and to copy:
`POST /v1/agents/demo/chat` answers, and the agent is in the portal's Agents
list with its instructions open for reading.

What it is: an ordinary custom agent. Short instructions that explain what
this engine is and how to build an agent, no tools, no knowledge. It is not
the fictional `/agents` demo portal (admin/seed_agents_demo.py), which is four
product-shaped agents with simulated tools and stays separate.

Seeded ONCE, and never touched again
------------------------------------
Every seeder here is insert-if-missing, so an operator's edits survive a
restart. That is not enough for this agent: insert-if-missing would bring it
back after it was deleted, and would seed a second one after its address was
changed (a renamed slug no longer matches). So the seeder writes a marker the
first time it runs to completion, and does nothing at all once the marker
exists:

    agent_env_vars  (environment, name = DEMO_AGENT_SEEDED)

It is a row in the settings table rather than a table of its own because it
is one flag, and because an operator can see it under Settings > Environment
and delete it to have the demo agent seeded again on the next boot.

An agent that already answers at `demo` when the seeder first runs is the
operator's own: it is left alone and the marker is written.

The model
---------
The agent is pinned to the Automatic model row (core.model_resolver), which
names no model: each request runs on the first model this install can use.
With none (a fresh install with no key and no local model) the agent still
exists and a chat returns a plain message saying where to add a model. Once a
model is added it answers, with no edit to the agent. Pinning a real model at
seed time would have frozen whichever one existed at first boot, often one
the install has no key for.

Where it seeds
--------------
`SEED_DEMO_AGENT=true|false` decides when set. Unset, it seeds on a build
with no product plugin that serves the generic agents API
(`ENABLE_GENERIC_AGENTS`), and nowhere else: a product build has its own
agents and does not want a stranger in its list.

Never raises: seeding must not gate live traffic. Runs after
seed_agent_models (the log line names the model the agent would use now).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from core.auth import get_service_client

logger = logging.getLogger(__name__)

DEMO_SLUG = "demo"
DEMO_KIND = "custom"
SEEDED_MARKER = "DEMO_AGENT_SEEDED"
_NOTES = "Seeded by admin/seed_demo_agent.py"

DEMO_DISPLAY_NAME = "Demo agent"
DEMO_DESCRIPTION = (
    "A demo, seeded on first boot. Talk to it, read its instructions and copy them. "
    "It has no tools and no knowledge. Archive it when you no longer need it."
)

DEMO_INSTRUCTIONS = """\
You are the demo agent of an open-source engine for building AI agents. You were created \
automatically when this install first started, so that a new user has something to talk to. \
Say plainly that you are a demo whenever someone asks who or what you are.

## What you can explain
- What this engine is: a server that runs AI agents. An agent is configuration, not code: \
instructions, a model, tools and knowledge. Each agent has an address of its own, \
`/v1/agents/<its address>/chat`, and replies as a stream of events.
- How to build an agent in the portal:
  1. Agents > Models > New model. A cloud model needs its provider key. A model served on the \
user's own machine (Ollama, or any OpenAI-compatible server) needs none.
  2. Connections > Connect server, to give agents tools from an MCP server.
  3. Agents > New agent. Pick a template (Blank is the plainest), name it, choose its model.
  4. On the agent: write its Instructions, attach tools and knowledge, then set Status to \
Active in Settings.
  5. Test, on the agent's page, talks to it.
- That the same steps work over the admin MCP server at `/mcp`.
- That you are a good thing to copy: your instructions are on your Instructions tab.

## How to answer
- Be brief and concrete. Use short steps when explaining how to do something.
- You have no tools and no knowledge base. You cannot look anything up, see this install's \
agents or settings, or take any action. If asked to, say so and point to where in the portal \
it is done.
- If you do not know something about this engine, say so. Do not invent features, menu names \
or commands.
"""


def enabled() -> bool:
    """Whether this build seeds the demo agent (read at call time)."""
    raw = (os.getenv("SEED_DEMO_AGENT") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    from core.plugins import has_product
    from endpoints.agents_v1 import generic_agents_enabled

    return generic_agents_enabled() and not has_product()


def is_demo_profile(profile: Optional[Dict[str, Any]]) -> bool:
    """True for the row the seeder made (by address; the portal labels it)."""
    p = profile or {}
    return p.get("agent_kind") == DEMO_KIND and p.get("slug") == DEMO_SLUG


def _rows(resp: Any) -> list:
    return getattr(resp, "data", None) or []


def _already_seeded(client, env: str) -> bool:
    return bool(_rows(
        client.table("agent_env_vars").select("var_id")
        .eq("environment", env).eq("name", SEEDED_MARKER).limit(1).execute()))


def _mark_seeded(client, env: str) -> None:
    client.table("agent_env_vars").insert({
        "environment": env,
        "name": SEEDED_MARKER,
        "value": "1",
        "is_secret": False,
        "source": "db",
        "description": "The demo agent was seeded once and is never seeded again. "
                       "Delete this row to have it seeded on the next start.",
    }).execute()


def ensure_auto_model(client, env: str) -> str:
    """The Automatic model row's id for `env`, inserting it if missing."""
    from core.model_resolver import AUTO_MODEL_NAME, AUTO_MODEL_STRING, AUTO_PROVIDER

    found = _rows(client.table("agent_models").select("model_id")
                  .eq("name", AUTO_MODEL_NAME).eq("environment", env).limit(1).execute())
    if found:
        return found[0]["model_id"]
    return _rows(client.table("agent_models").insert({
        "name": AUTO_MODEL_NAME,
        "provider": AUTO_PROVIDER,
        "model_string": AUTO_MODEL_STRING,
        "environment": env,
        "is_active": True,
        "extra_config": {"auto": True},
    }).execute())[0]["model_id"]


def _ensure_instructions(client) -> str:
    """Version 1 of the agent's own prompt (`demo:system`); returns its id.
    An existing version is never touched."""
    from admin.services.custom_prompts import own_prompt_slug

    slug = own_prompt_slug(DEMO_SLUG)
    prompts = _rows(client.table("agent_prompts").select("prompt_id")
                    .eq("agent_kind", DEMO_KIND).eq("slug", slug).limit(1).execute())
    if prompts:
        prompt_id = prompts[0]["prompt_id"]
    else:
        prompt_id = _rows(client.table("agent_prompts").insert({
            "agent_kind": DEMO_KIND, "slug": slug,
            "description": f"Instructions for {DEMO_DISPLAY_NAME}",
        }).execute())[0]["prompt_id"]
    versions = _rows(client.table("agent_prompt_versions").select("prompt_version_id")
                     .eq("prompt_id", prompt_id).order("version_number", desc=True)
                     .limit(1).execute())
    if versions:
        return versions[0]["prompt_version_id"]
    return _rows(client.table("agent_prompt_versions").insert({
        "prompt_id": prompt_id, "version_number": 1,
        "content": DEMO_INSTRUCTIONS, "notes": _NOTES,
    }).execute())[0]["prompt_version_id"]


def run() -> Dict[str, Any]:
    """Seed the demo agent once. Never raises."""
    try:
        if not enabled():
            return {"seeded": False, "reason": "off"}
        from core.agent_profile import AgentProfileLoader, _default_environment
        from core.model_resolver import load_usable_model

        client = get_service_client()
        env = _default_environment()
        if _already_seeded(client, env):
            return {"seeded": False, "reason": "already seeded"}
        # Any status: an archived or renamed-back `demo` is still the operator's.
        taken = _rows(client.table("agent_profiles").select("profile_id")
                      .eq("agent_kind", DEMO_KIND).eq("slug", DEMO_SLUG)
                      .eq("environment", env).limit(1).execute())
        if taken:
            _mark_seeded(client, env)
            return {"seeded": False, "reason": "an agent already answers at demo"}

        model_id = ensure_auto_model(client, env)
        version_id = _ensure_instructions(client)
        client.table("agent_profiles").insert({
            "agent_kind": DEMO_KIND,
            "slug": DEMO_SLUG,
            "display_name": DEMO_DISPLAY_NAME,
            "description": DEMO_DESCRIPTION,
            "environment": env,
            "endpoint_path": f"/v1/agents/{DEMO_SLUG}/chat",
            "model_main_id": model_id,
            "prompt_version_ids": {"system": version_id},
            "tool_ids": [],
            "data_source_ids": [],
            "mcp_server_ids": [],
            "memory_config": {"enabled": False},
            "is_active": True,
            "status": "active",
        }).execute()
        _mark_seeded(client, env)
        AgentProfileLoader.invalidate(DEMO_KIND, env)
        usable = load_usable_model(env, client)
        logger.info(
            "seed_demo_agent: created the demo agent (%s). %s", env,
            ("It will answer with %s." % usable.get("name")) if usable
            else "No usable model yet: it will say so until one is added.")
        return {"seeded": True, "model": (usable or {}).get("name")}
    except Exception as exc:  # seeding must never gate live traffic
        logger.warning("seed_demo_agent: skipped (%s)", exc)
        return {"seeded": False, "reason": "error"}
