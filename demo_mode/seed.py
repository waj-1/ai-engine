"""Build the Northwind Internet demo (demo_mode/content.py). Idempotent.

Every piece is looked up by its natural key and inserted only when missing,
so running it twice, or on every start, adds nothing the second time and
leaves a visitor's edits alone until the nightly reset (demo_mode/reset.py)
wipes them and calls this again.

What it makes, all in this install's own environment (staging on a default
install):

* model rows for Azure OpenAI (`DEMO_AGENT_DEPLOYMENT` for the agents,
  `DEMO_BUILDER_DEPLOYMENT` for the builder assistant, `DEMO_JUDGE_DEPLOYMENT`
  for the test judge; endpoint and key from `AZURE_OPENAI_*`), with the
  demo's output cap;
* the categories Support, Sales and Internal tools;
* the knowledge base "Northwind help center" with its two documents, embedded
  when an embedding model is set up (the documents are stored either way);
* the "Northwind tools" MCP server row pointing at this engine's own
  in-process tool server (demo_mode/northwind.py);
* the agents Support desk (tools, knowledge, and the Billing helper as its
  specialist), Billing helper, Sales assistant and IT helpdesk, each with
  its own instructions, and the Builder assistant (demo_mode/assistant.py);
* with ``DEMO_VOICE`` on and its model set up, the realtime model row and
  voice turned on for the three agents a caller can reach (demo_mode/voice.py);
  agents a visitor makes get the same voice (admin/services/profiles.py);
* a five-case test suite for the Support desk;
* the channel "Northwind website", allowed to talk to the demo agents only;
* a handful of stored example conversations (no model is called).

Blocking (supabase-py): call through `asyncio.to_thread` from async code.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from demo_mode import content, settings

logger = logging.getLogger(__name__)

MODEL_NAME = "Demo model (Azure OpenAI)"
SEED_MARK = "demo_mode.seed"


def _client():
    from core.auth import get_service_client

    return get_service_client()


def _env() -> str:
    from core.agent_profile import _default_environment

    return _default_environment()


def _rows(resp: Any) -> List[Dict[str, Any]]:
    return list(getattr(resp, "data", None) or [])


def _first(resp: Any) -> Optional[Dict[str, Any]]:
    rows = _rows(resp)
    return rows[0] if rows else None


# --------------------------------------------------------------------------
# Pieces
# --------------------------------------------------------------------------


def _model_row(role: str, deployment: str, env: str) -> Dict[str, Any]:
    provider = settings.model_provider()
    extra: Dict[str, Any] = {"seeded_by": SEED_MARK, "demo_role": role,
                             # The gpt-5 family rejects a temperature.
                             "omit_temperature": True, "pricing_mode": "manual"}
    effort = settings.reasoning_effort()
    if effort and effort != "none":
        extra["reasoning_effort"] = effort
    if provider == "azure_openai":
        # The model string is the deployment name; the endpoint and key come
        # from AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY at call time.
        extra["api_version"] = settings.azure_api_version()
    else:
        extra["base_url"] = settings.model_base_url()
        extra.pop("reasoning_effort", None)
        extra.pop("omit_temperature", None)
    from demo_mode import prices

    extra.update(prices.manual_rates(provider, deployment))  # list prices, where known
    return {
        "name": MODEL_NAMES[role],
        "provider": provider,
        "model_string": deployment,
        "temperature": 0.3,
        "max_output_tokens": settings.max_output_tokens(),
        "environment": env,
        "is_active": True,
        "extra_config": extra,
    }


#: The demo's model rows, by role.
MODEL_NAMES = {
    "agent": "Demo agents model",
    "builder": "Demo builder model",
    "judge": "Demo test judge model",
}


def ensure_models(client, env: str) -> Dict[str, str]:
    """{role: model_id} for the agents, the builder assistant and the test
    judge. The judge shares the builder's row when they name one deployment.
    A row that exists is brought in line with the current settings (these
    rows are the demo's, never a visitor's: Settings › Models is read-only)."""
    wanted = {"agent": settings.agent_deployment(), "builder": settings.builder_deployment()}
    if settings.judge_deployment() != wanted["builder"]:
        wanted["judge"] = settings.judge_deployment()
    ids: Dict[str, str] = {}
    for role, deployment in wanted.items():
        row = _model_row(role, deployment, env)
        found = _first(client.table("agent_models").select("model_id")
                       .eq("name", row["name"]).eq("environment", env).limit(1).execute())
        if found:
            client.table("agent_models").update(
                {k: row[k] for k in ("provider", "model_string", "max_output_tokens",
                                     "extra_config", "is_active")}
            ).eq("model_id", found["model_id"]).execute()
            ids[role] = found["model_id"]
        else:
            ids[role] = _first(client.table("agent_models").insert(row).execute())["model_id"]
    ids.setdefault("judge", ids["builder"])
    return ids


def ensure_voice(client, env: str, agents: Dict[str, str]) -> Optional[str]:
    """The demo's realtime model row, and voice turned on for the seeded
    agents a caller can reach (Support desk, Sales assistant, IT helpdesk;
    the banner's call goes to the Support desk), so a visitor can press Call
    on any of them without setting anything up. Nothing
    unless ``DEMO_VOICE`` is on and its model is set up (`demo_mode.voice`).
    The model row follows the settings, like the chat rows. The agent's voice
    settings are written only while it has none, so a visitor's change stays
    until the nightly reset. Returns the model id."""
    from demo_mode import voice

    if not voice.ready():
        return None
    row = voice.voice_model_row(env)
    found = _first(client.table("agent_models").select("model_id")
                   .eq("name", row["name"]).eq("environment", env).limit(1).execute())
    if found:
        client.table("agent_models").update(
            {k: row[k] for k in ("provider", "model_string", "extra_config", "is_active")}
        ).eq("model_id", found["model_id"]).execute()
        model_id = found["model_id"]
    else:
        model_id = _first(client.table("agent_models").insert(row).execute())["model_id"]
    # The agents the portal may call (the website channel's, ensure_channel):
    # the Builder assistant has its own microphone, and the Billing helper is
    # a specialist the Support desk hands work to, not one a caller reaches.
    callable_slugs = {a["slug"] for a in content.AGENTS if a["status"] == "active"}
    for slug, profile_id in agents.items():
        if slug not in callable_slugs or not profile_id:
            continue
        current = _first(client.table("agent_profiles").select("voice_config")
                         .eq("profile_id", profile_id).limit(1).execute()) or {}
        if not current.get("voice_config"):
            client.table("agent_profiles").update(
                {"voice_config": voice.voice_config(model_id)}
            ).eq("profile_id", profile_id).execute()
    return model_id


def ensure_categories(client) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for cat in content.CATEGORIES:
        found = _first(client.table("agent_types").select("agent_type_id")
                       .eq("slug", cat["slug"]).limit(1).execute())
        if not found:
            found = _first(client.table("agent_types").insert(dict(cat)).execute())
        out[cat["slug"]] = found["agent_type_id"]
    return out


def ensure_knowledge(client) -> Dict[str, str]:
    """{key: source_id} for every knowledge base in `content.KNOWLEDGE_BASES`,
    with any missing document added and made searchable."""
    return {key: _ensure_knowledge_base(client, spec)
            for key, spec in content.KNOWLEDGE_BASES.items()}


def _ensure_knowledge_base(client, spec: Dict[str, Any]) -> str:
    found = _first(client.table("agent_data_sources").select("source_id")
                   .eq("name", spec["name"]).limit(1).execute())
    if found:
        source_id = found["source_id"]
    else:
        source_id = _first(client.table("agent_data_sources").insert({
            "name": spec["name"],
            "kind": "pgvector",
            "agent_kind": "custom",
            "config": {"table": "ai_kb_embeddings", "pages_table": "ai_kb_pages",
                       "description": spec["description"]},
            "is_active": True,
        }).execute())["source_id"]
    new_pages: Dict[str, Any] = {}
    texts: Dict[str, str] = {}
    titles: Dict[str, str] = {}
    for doc in spec["documents"]:
        page = _first(client.table("ai_kb_pages").select("id")
                      .eq("source_id", source_id).eq("title", doc["title"]).limit(1).execute())
        if page:
            continue
        page = _first(client.table("ai_kb_pages").insert({
            "source_id": source_id, "title": doc["title"], "type": "user_guide",
            "language": "en", "content": doc["content"], "source": "admin_upload",
            "path": doc["filename"],
        }).execute())
        new_pages[doc["filename"]] = page["id"]
        texts[doc["filename"]] = doc["content"]
        titles[doc["filename"]] = doc["title"]
    if new_pages:
        _embed(source_id, new_pages, texts, titles)
    return source_id


def _embed(source_id: str, page_ids: Dict[str, Any], texts: Dict[str, str],
           titles: Dict[str, str]) -> None:
    """Make the new pages searchable, the way an upload does. Without an
    embedding model the pages stay stored but unsearchable, and the log says so."""
    from admin import kb_handlers

    status = kb_handlers.embedding_status()
    if not status.get("ready"):
        logger.warning("demo seed: knowledge base stored but not searchable: %s", status.get("problem"))
        return
    kb_handlers._kb_ingest_worker(
        source_id=source_id, table_name="ai_kb_embeddings", page_ids_by_filename=page_ids,
        file_contents_by_filename=texts, page_titles_by_filename=titles,
        page_type="user_guide", language="en")


def ensure_tool_server(client) -> str:
    from demo_mode import northwind

    endpoint = northwind.northwind_endpoint()
    found = _first(client.table("agent_mcp_servers").select("mcp_server_id, endpoint")
                   .eq("name", northwind.SERVER_NAME).limit(1).execute())
    if found:
        if found.get("endpoint") != endpoint:  # DEMO_PUBLIC_URL changed
            client.table("agent_mcp_servers").update({"endpoint": endpoint}) \
                .eq("mcp_server_id", found["mcp_server_id"]).execute()
        return found["mcp_server_id"]
    return _first(client.table("agent_mcp_servers").insert({
        "name": northwind.SERVER_NAME,
        "source": "external",
        "transport": "http",
        "endpoint": endpoint,
        "is_active": True,
        "usage_guidance": "Northwind Internet's tools: outages, orders, tickets and plans (fictional).",
    }).execute())["mcp_server_id"]


def _ensure_instructions(client, slug: str, display_name: str, text: str) -> str:
    from admin.services.custom_prompts import own_prompt_slug

    prompt_slug = own_prompt_slug(slug)
    prompt = _first(client.table("agent_prompts").select("prompt_id")
                    .eq("agent_kind", "custom").eq("slug", prompt_slug).limit(1).execute())
    if not prompt:
        prompt = _first(client.table("agent_prompts").insert({
            "agent_kind": "custom", "slug": prompt_slug,
            "description": f"Instructions for {display_name}",
        }).execute())
    version = _first(client.table("agent_prompt_versions").select("prompt_version_id")
                     .eq("prompt_id", prompt["prompt_id"]).order("version_number", desc=True)
                     .limit(1).execute())
    if version:
        return version["prompt_version_id"]
    return _first(client.table("agent_prompt_versions").insert({
        "prompt_id": prompt["prompt_id"], "version_number": 1, "content": text,
        "notes": "Seeded by demo_mode/seed.py",
    }).execute())["prompt_version_id"]


def _converge(client, profile_id: str, spec: Dict[str, Any], models: Dict[str, str]) -> None:
    """A code-owned agent (the read-only Builder assistant) follows the code:
    its instructions get a new version when the text changed, and its model
    and status are put back."""
    from admin.services.custom_prompts import own_prompt_slug

    prompt = _first(client.table("agent_prompts").select("prompt_id")
                    .eq("agent_kind", "custom").eq("slug", own_prompt_slug(spec["slug"]))
                    .limit(1).execute())
    pin = None
    if prompt:
        latest = _first(client.table("agent_prompt_versions")
                        .select("prompt_version_id, version_number, content")
                        .eq("prompt_id", prompt["prompt_id"]).order("version_number", desc=True)
                        .limit(1).execute())
        if latest and latest.get("content") == spec["instructions"]:
            pin = latest["prompt_version_id"]
        else:
            pin = _first(client.table("agent_prompt_versions").insert({
                "prompt_id": prompt["prompt_id"],
                "version_number": int((latest or {}).get("version_number") or 0) + 1,
                "content": spec["instructions"], "notes": "Seeded by demo_mode/seed.py",
            }).execute())["prompt_version_id"]
    patch: Dict[str, Any] = {"model_main_id": models[spec.get("model", "agent")],
                             "status": spec["status"], "is_active": True}
    if pin:
        patch["prompt_version_ids"] = {"system": pin}
    client.table("agent_profiles").update(patch).eq("profile_id", profile_id).execute()


def _knowledge_ids(spec: Dict[str, Any], kb_ids: Dict[str, str]) -> List[str]:
    """The knowledge bases an agent spec names (`knowledge`: a list of keys;
    True means the help center)."""
    wanted = spec.get("knowledge") or []
    if wanted is True:
        wanted = ["help"]
    return [kb_ids[k] for k in wanted if k in kb_ids]


def ensure_agents(client, env: str, models: Dict[str, str], categories: Dict[str, str],
                  kb_ids: Dict[str, str], server_id: str) -> Dict[str, str]:
    """{slug: profile_id}. Specialists first, so a hand-off can name them."""
    ids: Dict[str, str] = {}
    ordered = sorted(content.AGENTS, key=lambda a: 0 if a.get("delegatable") else 1)
    for spec in ordered:
        slug = spec["slug"]
        found = _first(client.table("agent_profiles").select("profile_id")
                       .eq("agent_kind", "custom").eq("slug", slug).eq("environment", env)
                       .limit(1).execute())
        if found:
            ids[slug] = found["profile_id"]
            if spec.get("code_owned"):
                _converge(client, found["profile_id"], spec, models)
            continue
        version_id = _ensure_instructions(client, slug, spec["display_name"], spec["instructions"])
        row = {
            "agent_kind": "custom",
            "slug": slug,
            "display_name": spec["display_name"],
            "description": spec["description"],
            "environment": env,
            "endpoint_path": f"/v1/agents/{slug}/chat",
            "model_main_id": models[spec.get("model", "agent")],
            "prompt_version_ids": {"system": version_id},
            "tool_ids": [],
            "data_source_ids": _knowledge_ids(spec, kb_ids),
            "mcp_server_ids": [server_id] if spec.get("tools") else [],
            "downstream_profile_ids": [ids[s] for s in spec.get("sub_agents", []) if s in ids],
            "delegatable": bool(spec.get("delegatable")),
            "memory_config": {"enabled": False},
            "is_active": True,
            "status": spec["status"],
            "agent_type_id": categories.get(spec["category"]),
        }
        ids[slug] = _first(client.table("agent_profiles").insert(row).execute())["profile_id"]
        if spec.get("tools"):
            client.table("agent_profile_mcp_servers").insert({
                "profile_id": ids[slug], "mcp_server_id": server_id, "position": 0,
            }).execute()
    return ids


def ensure_suites(client, agents: Dict[str, str], model_id: str) -> Dict[str, str]:
    """{agent slug: suite_id}: one test suite per demo agent."""
    out: Dict[str, str] = {}
    for spec in content.SUITES:
        profile_id = agents.get(spec["agent"])
        if not profile_id:
            continue
        found = _first(client.table("agent_eval_suites").select("suite_id")
                       .eq("name", spec["name"]).limit(1).execute())
        if found:
            out[spec["agent"]] = found["suite_id"]
            continue
        out[spec["agent"]] = _first(client.table("agent_eval_suites").insert({
            "name": spec["name"],
            "agent_kind": "custom",
            "profile_id": profile_id,
            "scenarios": spec["cases"],
            "pass_threshold": 0.7,
            "evaluator_model_id": model_id,
            "runner_kind": "chat",
            "default_context": {},
            "is_active": True,
        }).execute())["suite_id"]
    return out


def ensure_skills(client, agents: Dict[str, str]) -> Dict[str, str]:
    """{slug: skill_id}: the demo's playbooks, each attached to its agents."""
    out: Dict[str, str] = {}
    for spec in content.SKILLS:
        found = _first(client.table("agent_skills").select("skill_id")
                       .eq("slug", spec["slug"]).limit(1).execute())
        if found:
            out[spec["slug"]] = found["skill_id"]
            continue
        skill_id = _first(client.table("agent_skills").insert({
            "slug": spec["slug"], "display_name": spec["display_name"],
            "description": spec["description"], "agent_kind": "custom",
            "always_on": False, "is_active": True, "status": "active",
        }).execute())["skill_id"]
        client.table("agent_skill_versions").insert({
            "skill_id": skill_id, "version_number": 1, "content": spec["content"],
            "notes": "Seeded by demo_mode/seed.py",
        }).execute()
        for position, slug in enumerate(spec.get("agents", [])):
            if agents.get(slug):
                client.table("agent_profile_skills").insert({
                    "profile_id": agents[slug], "skill_id": skill_id, "position": position,
                }).execute()
        out[spec["slug"]] = skill_id
    return out


def ensure_schedules(client, agents: Dict[str, str], suites: Dict[str, str]) -> Dict[str, str]:
    """{agent slug: schedule_id}: the scheduled runs shown on Testing >
    Schedules. `demo_mode.warmup` runs them; nothing else fires them."""
    from datetime import datetime, timedelta, timezone

    out: Dict[str, str] = {}
    hour = settings.reset_hour_utc()
    now = datetime.now(timezone.utc)
    for spec in content.SCHEDULES:
        slug = spec["agent"]
        if not (agents.get(slug) and suites.get(slug)):
            continue
        found = _first(client.table("agent_eval_schedules").select("schedule_id")
                       .eq("name", spec["name"]).limit(1).execute())
        if found:
            out[slug] = found["schedule_id"]
            continue
        nxt = now.replace(hour=hour if hour is not None else 3, minute=10, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        if spec["cadence"] == "weekly":
            nxt += timedelta(days=6)
        out[slug] = _first(client.table("agent_eval_schedules").insert({
            "name": spec["name"], "suite_id": suites[slug], "profile_id": agents[slug],
            "agent_kind": "custom", "source": "suite", "cadence": spec["cadence"],
            "coverage": {}, "is_enabled": True, "next_run_at": nxt.isoformat(),
        }).execute())["schedule_id"]
    return out


def ensure_voice_checks(client) -> int:
    """The Support desk's voice checks (Testing > Voice checks), only where
    the demo's voice is on. Returns how many were added."""
    from demo_mode import voice

    if not voice.ready():
        return 0
    have = {r["slug"] for r in _rows(client.table("ai_voice_scenarios").select("slug").execute())}
    added = 0
    for spec in content.VOICE_CHECKS:
        if spec["slug"] in have:
            continue
        client.table("ai_voice_scenarios").insert({
            "slug": spec["slug"], "agent_kind": "custom", "title": spec["title"],
            "ws_path": f"/v1/agents/{voice.CALL_SLUG}/voice", "locale": "en",
            "steps": spec["steps"], "notes": spec["notes"], "enabled": True,
        }).execute()
        added += 1
    return added


def ensure_channel(client) -> str:
    spec = content.CHANNEL
    found = _first(client.table("agent_channels").select("channel_id")
                   .eq("slug", spec["slug"]).limit(1).execute())
    if found:
        return found["channel_id"]
    public = [a["slug"] for a in content.AGENTS if a["status"] == "active"]
    return _first(client.table("agent_channels").insert({
        **spec,
        "is_active": True,
        "allowed_origins": [settings.public_url()],
        "allowed_agent_kinds": [f"custom:{slug}" for slug in public],
        "auth_required": True,
        "rate_limit_rpm": 20,
        "config": {},
    }).execute())["channel_id"]


def allow_on_website(client, slug: Optional[str]) -> None:
    """Let the portal's Test and Call reach an agent a visitor made. The
    portal is served from the website channel's own address, so a chat or
    call from its pages is checked against that channel's agents; without
    this a visitor's own agent answers 403. Demo mode only; a channel with
    no agent list allows every agent already."""
    if not (settings.enabled() and slug):
        return
    own = _first(client.table("agent_channels").select("channel_id, allowed_agent_kinds")
                 .eq("slug", content.CHANNEL["slug"]).limit(1).execute())
    kinds = list((own or {}).get("allowed_agent_kinds") or [])
    entry = f"custom:{slug}"
    if not kinds or entry in kinds:
        return
    client.table("agent_channels").update({"allowed_agent_kinds": kinds + [entry]}) \
        .eq("channel_id", own["channel_id"]).execute()
    try:
        from core import channels

        channels.invalidate()
    except Exception:  # noqa: BLE001 — the cache expires by itself
        pass


def ensure_sample_channel(client) -> str:
    """The channel the demo's own sample conversations arrive on
    (demo_mode/sample_traffic.py): it may talk to every demo agent a customer
    or a member of staff could reach, the billing specialist included."""
    spec = content.SAMPLE_CHANNEL
    found = _first(client.table("agent_channels").select("channel_id")
                   .eq("slug", spec["slug"]).limit(1).execute())
    if found:
        return found["channel_id"]
    slugs = [a["slug"] for a in content.AGENTS if not a.get("code_owned")]
    return _first(client.table("agent_channels").insert({
        **spec,
        "is_active": True,
        "allowed_origins": [content.SAMPLE_ORIGIN],
        "allowed_agent_kinds": [f"custom:{slug}" for slug in slugs],
        "auth_required": True,
        "rate_limit_rpm": 20,
        "config": {},
    }).execute())["channel_id"]


def stored_conversations_wanted() -> bool:
    """The stored example chats are for an install with no tracing store,
    where Conversations lists saved chats. With tracing on, Conversations and
    Overview read traces only: a stored chat with no trace would be counted on
    no report and found by no link, so none is seeded (the demo's sample
    conversations, which are real and traced, take their place)."""
    from core.lmnr_clickhouse import tracing_configured

    return not tracing_configured()


def ensure_conversations(client, env: str) -> int:
    """Stored chats; a chat that is already there (same title and agent) is skipped."""
    made = 0
    start = datetime.now(timezone.utc) - timedelta(hours=len(content.CONVERSATIONS) * 3)
    for n, convo in enumerate(content.CONVERSATIONS):
        exists = [r for r in _rows(client.table("ai_chat_sessions").select("id, metadata")
                                   .eq("title", convo["title"]).eq("agent_kind", "custom").execute())
                  if (r.get("metadata") or {}).get("agent_slug") == convo["agent"]]
        if exists:
            continue
        at = start + timedelta(hours=3 * n)
        session = _first(client.table("ai_chat_sessions").insert({
            "title": convo["title"],
            "agent_kind": "custom",
            "message_count": len(convo["turns"]),
            "metadata": {"agent_slug": convo["agent"], "source": "api", "session_type": "custom",
                         "environment": env, "seeded_by": SEED_MARK},
            "created_at": at.isoformat(),
            "updated_at": (at + timedelta(minutes=len(convo["turns"]))).isoformat(),
        }).execute())
        for i, (role, text) in enumerate(convo["turns"]):
            client.table("ai_chat_messages").insert({
                "session_id": session["id"], "role": role, "content": text,
                "metadata": {}, "created_at": (at + timedelta(minutes=i)).isoformat(),
            }).execute()
        made += 1
    return made


# --------------------------------------------------------------------------
# The whole demo
# --------------------------------------------------------------------------


def seed(client=None) -> Dict[str, Any]:
    """Make whatever part of the demo is missing. Raises on a database error."""
    client = client or _client()
    env = _env()
    models = ensure_models(client, env)
    categories = ensure_categories(client)
    kb_ids = ensure_knowledge(client)
    server_id = ensure_tool_server(client)
    agents = ensure_agents(client, env, models, categories, kb_ids, server_id)
    voice_model_id = ensure_voice(client, env, agents)
    suites = ensure_suites(client, agents, models["judge"])
    extras: Dict[str, Any] = {}
    # Sample content for the other screens. Each is optional: a table this
    # install does not have must not stop the demo from starting.
    for key, make in (("skills", lambda: ensure_skills(client, agents)),
                      ("schedules", lambda: ensure_schedules(client, agents, suites)),
                      ("voice_checks_added", lambda: ensure_voice_checks(client))):
        try:
            extras[key] = make()
        except Exception as exc:  # noqa: BLE001
            logger.warning("demo seed: %s not seeded (%s: %s)", key, type(exc).__name__, exc)
    channel_id = ensure_channel(client)
    extras["sample_channel_id"] = ensure_sample_channel(client)
    conversations = ensure_conversations(client, env) if stored_conversations_wanted() else 0
    # The product-page showcase (demo_mode/showcase.py), only with DEMO_SHOWCASE on.
    try:
        from demo_mode import showcase

        extras["showcase"] = showcase.ensure(client, env, models, voice_model_id)
    except Exception as exc:  # noqa: BLE001 — the rest of the demo must still start
        logger.warning("demo seed: showcase not seeded (%s: %s)", type(exc).__name__, exc)
    try:
        from core.agent_profile import AgentProfileLoader

        AgentProfileLoader.invalidate("custom", env)
    except Exception:  # noqa: BLE001 — the cache expires by itself
        pass
    try:
        from core import channels

        channels.invalidate()  # the sample channel is used within seconds
    except Exception:  # noqa: BLE001 — the cache expires by itself
        pass
    return {"environment": env, "models": models, "agents": agents,
            "suite_id": suites.get(content.SUPPORT_SLUG), "suites": suites,
            "knowledge_id": kb_ids.get("help"), "knowledge_ids": kb_ids,
            "tool_server_id": server_id, "channel_id": channel_id,
            "conversations_added": conversations, "voice_model_id": voice_model_id, **extras}
