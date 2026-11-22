"""Boot seeder for the generic AI-agents demo portal (four agent kinds).

Runs on a full build (engine.company.example — the landing page links there) and on
a deployment scope that owns these kinds (core/deployment.py); a no-op on
every other scoped build. Self-contained on purpose: the shared seeders
(seed_prompts / sync_tools / seed_agent_profiles / seed_builtin_mcps) skip
every scope's kinds on a full build, so this one seeds its own:

  1. agent_tools rows for the four kinds' python tools (code-owned metadata,
     refreshed every boot like sync_tools does — never deprecates anything);
  2. the `agents_demo_kb` pgvector data source (empty until
     scripts/ingest_agents_demo_kb.py runs);
  3. agent_prompts + version 1 of each `system` / `voice_persona` prompt —
     insert-if-missing; an existing version is never touched;
  4. one active profile per kind (`<kind>_default`) — insert-if-missing,
     never updated afterwards (operator edits survive every boot);
  5. the OUTBOUND kinds' built-in tool bundle, attached to their profile.
     The inbound kinds (support, inquiries) are prompt-only — case file +
     facts sheet in the prompt, no tools — so they get no tool ids and no
     bundle, and the seeder never re-attaches one (that would undo
     scripts/agents_demo_inbound_prompt_only.py on the next boot).

Never raises: seeding must not gate live traffic. Runs after
seed_agent_models (it needs the model rows).

Model choices at seed time (repointable in /admin afterwards):
  * brain — the in-region Azure GPT-5 Mini row when its key is configured
    (latency-tuned for the voice delegate), else the engine's default main
    model row;
  * voice — the Gemini Live realtime row (the pre-fetched-context mode and
    its barge-in values were tuned on it), else any realtime row;
    no realtime row → the profile is seeded with voice `enabled: false`
    (the portal then offers text only).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from core.auth import get_service_client

logger = logging.getLogger(__name__)

_NOTES = "Seeded by admin/seed_agents_demo.py"

# Edge settings for a pre-fetched-context call (outbound, and the prompt-only
# inbound agents) — the values tuned live on the first outbound voice demo:
# Gemini Live stops reporting `interrupted` once it finishes generating, so the
# endpoint's own onset detector does the barge-in, and the greeting guard is
# short because callers answer over a long opening ("yes, speaking").
DIRECT_MODE_VOICE_KEYS: Dict[str, Any] = {
    "prefetched_context": True,
    "local_barge_in_sec": 0.6,
    "local_barge_in_dbfs": -28.0,
    "greeting_barge_in_guard_sec": 4.0,
    # Echo/noise in the opening second cut the greeting dead on ~40% of
    # calls (Gemini cancels it server-side); the caller is not heard until
    # the greeting has played out.
    "greeting_input_mute_sec": 12.0,
}

# The anonymous-caller posture (same mechanism as the public `sales` voice
# kind): the endpoint's per-IP connect limiter, these concurrency caps, the
# per-IP daily audio budget and a short session ceiling.
ANONYMOUS_VOICE_KEYS: Dict[str, Any] = {
    "max_session_sec": 300,
    "max_concurrent_sessions": 2,
    "max_concurrent_per_ip": 4,
    "handshake_charge_sec": 30,
}

def voice_config_for(agent_key: str, realtime_model_id: Optional[str]) -> Dict[str, Any]:
    from runtime.prompts.agents_demo import GREETING_TRIGGERS, PERSONA_SLUG
    from runtime.tools.agents_demo.brand import BRAND
    from runtime.tools.agents_demo.data import AGENTS

    agent = AGENTS[agent_key]
    cfg: Dict[str, Any] = {
        "enabled": bool(realtime_model_id),
        "persona_prompt_slug": PERSONA_SLUG,
        "greeting_trigger": GREETING_TRIGGERS[agent_key],
        "voice_id": agent.voice_id,
        "phrase_list": [BRAND["name_en"], BRAND["short_en"], BRAND["name_ar"], BRAND["short_ar"]],
        "stall_recovery_sec": 15,
        **ANONYMOUS_VOICE_KEYS,
    }
    if realtime_model_id:
        cfg["model_id"] = realtime_model_id
    # Every demo agent answers from pre-fetched context: outbound with direct
    # action tools, inbound (support, inquiries) from the prompt alone — the
    # delegated ask_agent path cost 2-4s per answer for a simulation with
    # nothing real behind its tools.
    cfg.update(DIRECT_MODE_VOICE_KEYS)
    return cfg


def is_prompt_only(agent_key: str) -> bool:
    """The inbound demo agents carry no tools at all."""
    from runtime.tools.agents_demo.data import AGENTS

    return AGENTS[agent_key].direction == "inbound"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

_WRITE_TOOLS = {
    "support_request_refund", "support_book_technician", "support_change_service", "support_open_ticket",
    "support_handoff_to_human", "inquiry_capture_lead", "inquiry_book_callback",
    "collection_send_payment_link", "collection_set_up_payment_plan", "collection_record_promise",
    "collection_log_note", "collection_escalate", "offer_send_link", "offer_book_followup",
    "offer_log_outcome",
}


def tool_pools() -> Dict[str, List[Any]]:
    """{agent_kind: [tool, …]} in each kind's pool order (domain tools, then KB)."""
    from runtime.tools.agents_demo import collection, inquiries, kb, sales, support
    from runtime.tools.agents_demo.data import AGENTS

    ctx = {"locale": "en"}
    domain = {
        "support": support.create_support_tools(ctx),
        "inquiries": inquiries.create_inquiries_tools(ctx),
        "collection": collection.create_collection_tools(ctx),
        "sales": sales.create_sales_tools(ctx),
    }
    kb_tool = kb.make_kb_tool(None)
    return {AGENTS[k].kind: [*tools, kb_tool] for k, tools in domain.items()}


def tool_rows() -> Dict[str, Dict[str, Any]]:
    """{python_name: agent_tools payload} — the shared KB tool lists every kind."""
    from runtime.tools.agents_demo.kb import KB_TOOL_NAME

    rows: Dict[str, Dict[str, Any]] = {}
    for kind, tools in tool_pools().items():
        for tool in tools:
            name = tool.name
            if name in rows:
                extra = rows[name]["additional_agent_kinds"]
                if kind != rows[name]["agent_kind"] and kind not in extra:
                    extra.append(kind)
                continue
            schema: Dict[str, Any] = {}
            args = getattr(tool, "args_schema", None)
            if args is not None and hasattr(args, "model_json_schema"):
                try:
                    schema = args.model_json_schema()
                except Exception:  # noqa: BLE001
                    schema = {}
            rows[name] = {
                "python_name": name,
                "display_name": name.replace("_", " ").title(),
                "description": (tool.description or "")[:500],
                "category": "agents_demo",
                "scope": "write" if name in _WRITE_TOOLS else "read",
                "source_file": ("runtime/tools/agents_demo/kb.py" if name == KB_TOOL_NAME
                                else f"runtime/tools/agents_demo/{kind.split('_', 1)[1]}.py"),
                "argument_schema": schema,
                "agent_kind": kind,
                "additional_agent_kinds": [],
                "is_deprecated": False,
                "kind": "python",
            }
    return rows


def _sync_tools(client) -> None:
    wanted = tool_rows()
    existing = (client.table("agent_tools").select("python_name")
                .in_("python_name", list(wanted)).execute().data or [])
    have = {r["python_name"] for r in existing}
    for name, payload in wanted.items():
        if name not in have:
            try:
                client.table("agent_tools").insert(payload).execute()
                continue
            except Exception as exc:
                # Staging, prod and the control-plane share one AI database and
                # boot together: a sibling can insert the row between our select
                # and insert. Lost the race = the row exists, so update it.
                if "23505" not in str(exc):
                    raise
        client.table("agent_tools").update(payload).eq("python_name", name).execute()


def _tool_ids(client, kind: str) -> List[str]:
    pool = [t.name for t in tool_pools()[kind]]
    rows = (client.table("agent_tools").select("tool_id, python_name")
            .in_("python_name", pool).eq("is_deprecated", False).execute().data or [])
    by_name = {r["python_name"]: r["tool_id"] for r in rows}
    return [by_name[n] for n in pool if n in by_name]


# ---------------------------------------------------------------------------
# KB source, prompts, models
# ---------------------------------------------------------------------------

def _ensure_kb_source(client) -> Optional[str]:
    from runtime.tools.agents_demo.data import AGENT_KINDS
    from runtime.tools.agents_demo.kb import DATA_SOURCE_NAME

    rows = (client.table("agent_data_sources").select("source_id")
            .eq("name", DATA_SOURCE_NAME).limit(1).execute().data or [])
    if rows:
        return rows[0]["source_id"]
    row = client.table("agent_data_sources").insert({
        "name": DATA_SOURCE_NAME,
        "kind": "pgvector",
        "config": {"table": "ai_kb_embeddings", "pages_table": "ai_kb_pages"},
        "agent_kind": AGENT_KINDS[0],
        "additional_agent_kinds": list(AGENT_KINDS[1:]),
        "is_active": True,
    }).execute()
    logger.info("seed_agents_demo: created KB source %s", DATA_SOURCE_NAME)
    return row.data[0]["source_id"]


def _ensure_prompts(client) -> Dict[str, Dict[str, str]]:
    """{agent_kind: {slug: latest prompt_version_id}} — inserts what is missing."""
    from runtime.prompts.agents_demo import prompt_specs

    out: Dict[str, Dict[str, str]] = {}
    for kind, slug, description, content in prompt_specs():
        rows = (client.table("agent_prompts").select("prompt_id")
                .eq("agent_kind", kind).eq("slug", slug).limit(1).execute().data or [])
        if rows:
            prompt_id = rows[0]["prompt_id"]
        else:
            prompt_id = client.table("agent_prompts").insert({
                "agent_kind": kind, "slug": slug, "description": description,
            }).execute().data[0]["prompt_id"]
        versions = (client.table("agent_prompt_versions").select("prompt_version_id")
                    .eq("prompt_id", prompt_id).order("version_number", desc=True)
                    .limit(1).execute().data or [])
        if versions:
            vid = versions[0]["prompt_version_id"]
        else:
            vid = client.table("agent_prompt_versions").insert({
                "prompt_id": prompt_id, "version_number": 1, "content": content, "notes": _NOTES,
            }).execute().data[0]["prompt_version_id"]
        out.setdefault(kind, {})[slug] = vid
    return out


def _models(client, env: str) -> List[Dict[str, Any]]:
    q = client.table("agent_models").select("model_id, name, model_string, provider, extra_config, is_active, environment")
    rows = q.eq("is_active", True).execute().data or []
    scoped = [r for r in rows if (r.get("environment") or env) == env]
    return scoped or rows


def pick_brain_model(rows: List[Dict[str, Any]]) -> Optional[str]:
    from core.config import AGENT_CONFIG

    if os.getenv("AZURE_OPENAI_API_KEY"):
        azure = next((r for r in rows if r.get("provider") == "azure_openai"
                      and r.get("model_string") == "gpt-5-mini"), None)
        if azure:
            return azure["model_id"]
    chat = [r for r in rows if not (r.get("extra_config") or {}).get("realtime")]
    preferred = AGENT_CONFIG.main_model
    # Prefer the plain row over one tuned for another agent's purpose (its
    # name carries the purpose in parentheses, e.g. "… (<purpose>)").
    hit = next((r for r in chat if r.get("model_string") == preferred
                and "(" not in (r.get("name") or "")), None)
    hit = hit or next((r for r in chat if r.get("model_string") == preferred), None)
    return (hit or (chat[0] if chat else {})).get("model_id")


def _all_active_models(client) -> List[Dict[str, Any]]:
    q = client.table("agent_models").select("model_id, name, model_string, provider, extra_config, is_active, environment")
    return q.eq("is_active", True).execute().data or []


# The realtime model the outbound personas and barge-in timings were tuned on.
PREFERRED_REALTIME = "gemini-3.1-flash-live-preview"


def pick_realtime_model(rows: List[Dict[str, Any]]) -> Optional[str]:
    realtime = [r for r in rows if (r.get("extra_config") or {}).get("realtime")]
    preferred = next((r for r in realtime if r.get("model_string") == PREFERRED_REALTIME), None)
    if preferred:
        return preferred["model_id"]
    gemini = next((r for r in realtime if r.get("provider") == "google_genai"), None)
    return (gemini or (realtime[0] if realtime else {})).get("model_id")


# ---------------------------------------------------------------------------
# Profiles + bundles
# ---------------------------------------------------------------------------

def profile_slug(kind: str) -> str:
    return f"{kind}_default"


def _ensure_profile(client, env: str, agent_key: str, *, model_id: str, realtime_id: Optional[str],
                    prompt_versions: Dict[str, str], tool_ids: List[str], source_id: Optional[str]) -> None:
    from runtime.tools.agents_demo.data import AGENTS

    agent = AGENTS[agent_key]
    rows = (client.table("agent_profiles").select("profile_id")
            .eq("agent_kind", agent.kind).eq("slug", profile_slug(agent.kind))
            .eq("environment", env).limit(1).execute().data or [])
    if rows:
        return
    titles = {"support": "Customer support", "inquiries": "Business inquiries",
              "collection": "Outbound collection", "sales": "Outbound sales"}
    client.table("agent_profiles").insert({
        "agent_kind": agent.kind,
        "slug": profile_slug(agent.kind),
        "display_name": f"{agent.persona_en} — {titles[agent_key]} (AI agents demo)",
        "description": "Generic AI-agents demo portal (/agents) — fictional company, simulated tools.",
        "environment": env,
        "endpoint_path": "/agents/chat/stream",
        "model_main_id": model_id,
        "model_light_id": model_id,
        "prompt_version_ids": prompt_versions,
        "tool_ids": tool_ids,
        "data_source_ids": [source_id] if source_id else [],
        "mcp_server_ids": [],
        # Memory's read path awaits an embedding before the model call — off
        # for a voice-first demo with anonymous, one-off callers.
        "memory_config": {"enabled": False},
        "voice_config": voice_config_for(agent_key, realtime_id),
        "is_active": True,
        "status": "active",
    }).execute()
    logger.info("seed_agents_demo: created profile %s/%s (%s)", agent.kind, profile_slug(agent.kind), env)


def _ensure_bundle(client, kind: str) -> None:
    from admin.builtin_mcp_membership import ordered_membership
    from admin.seed_builtin_mcps import _attach_profiles, _reconcile_membership, _upsert_builtin_bundle

    pool = [t.name for t in tool_pools()[kind]]
    rows = (client.table("agent_tools").select("tool_id, python_name, kind")
            .in_("python_name", pool).eq("is_deprecated", False).execute().data or [])
    if not rows:
        return
    mcp_id = _upsert_builtin_bundle(client, kind)
    _reconcile_membership(client, mcp_id, ordered_membership(pool, rows))
    _attach_profiles(client, kind, mcp_id)


def run() -> None:
    """Seed the four demo agents if absent. Never raises."""
    try:
        from core.deployment import serves_agents_portal

        if not serves_agents_portal():
            return
        if not (os.getenv("GOOGLE_API_KEY") or os.getenv("AZURE_OPENAI_API_KEY")):
            # The demo's brain is a Gemini or Azure OpenAI row (pick_brain_model).
            # Seeded without either key, a fresh install got four "Active"
            # agents in its list that could not answer (2026-11-22). Insert-only,
            # so they appear on the first boot after a key is set.
            logger.info("seed_agents_demo: no GOOGLE_API_KEY or AZURE_OPENAI_API_KEY — "
                        "demo agents not seeded")
            return
        from core.agent_profile import _default_environment
        from runtime.tools.agents_demo.data import AGENTS

        client = get_service_client()
        env = _default_environment()
        _sync_tools(client)
        source_id = _ensure_kb_source(client)
        prompts = _ensure_prompts(client)
        models = _models(client, env)
        brain = pick_brain_model(models)
        # Realtime rows are not env-scoped in practice: prod's live voice runs
        # on staging-tagged rows (there is no prod realtime row), so scoping
        # would seed voice OFF on prod.
        realtime = pick_realtime_model(_all_active_models(client))
        if not brain:
            logger.warning("seed_agents_demo: no chat model row in %s yet — profiles not seeded "
                           "(boot again after seed_agent_models)", env)
        for key, agent in AGENTS.items():
            prompt_only = is_prompt_only(key)
            if brain:
                _ensure_profile(client, env, key, model_id=brain, realtime_id=realtime,
                                prompt_versions=prompts.get(agent.kind, {}),
                                tool_ids=[] if prompt_only else _tool_ids(client, agent.kind),
                                source_id=source_id)
            if not prompt_only:
                _ensure_bundle(client, agent.kind)
    except Exception as exc:  # seeding must never gate live traffic
        logger.warning("seed_agents_demo: skipped (%s)", exc)
