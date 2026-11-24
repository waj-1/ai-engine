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


def ensure_categories(client) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for cat in content.CATEGORIES:
        found = _first(client.table("agent_types").select("agent_type_id")
                       .eq("slug", cat["slug"]).limit(1).execute())
        if not found:
            found = _first(client.table("agent_types").insert(dict(cat)).execute())
        out[cat["slug"]] = found["agent_type_id"]
    return out


def ensure_knowledge(client) -> str:
    found = _first(client.table("agent_data_sources").select("source_id")
                   .eq("name", content.KB_NAME).limit(1).execute())
    if found:
        source_id = found["source_id"]
    else:
        source_id = _first(client.table("agent_data_sources").insert({
            "name": content.KB_NAME,
            "kind": "pgvector",
            "agent_kind": "custom",
            "config": {"table": "ai_kb_embeddings", "pages_table": "ai_kb_pages",
                       "description": "Northwind's customer help pages (fictional)."},
            "is_active": True,
        }).execute())["source_id"]
    new_pages: Dict[str, Any] = {}
    texts: Dict[str, str] = {}
    titles: Dict[str, str] = {}
    for doc in content.DOCUMENTS:
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


def ensure_agents(client, env: str, models: Dict[str, str], categories: Dict[str, str],
                  kb_id: str, server_id: str) -> Dict[str, str]:
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
            "data_source_ids": [kb_id] if spec.get("knowledge") else [],
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


def ensure_suite(client, profile_id: str, model_id: str) -> str:
    found = _first(client.table("agent_eval_suites").select("suite_id")
                   .eq("name", content.SUITE_NAME).limit(1).execute())
    if found:
        return found["suite_id"]
    return _first(client.table("agent_eval_suites").insert({
        "name": content.SUITE_NAME,
        "agent_kind": "custom",
        "profile_id": profile_id,
        "scenarios": content.TEST_CASES,
        "pass_threshold": 0.7,
        "evaluator_model_id": model_id,
        "runner_kind": "chat",
        "default_context": {},
        "is_active": True,
    }).execute())["suite_id"]


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
    kb_id = ensure_knowledge(client)
    server_id = ensure_tool_server(client)
    agents = ensure_agents(client, env, models, categories, kb_id, server_id)
    suite_id = ensure_suite(client, agents[content.SUPPORT_SLUG], models["judge"])
    channel_id = ensure_channel(client)
    conversations = ensure_conversations(client, env)
    try:
        from core.agent_profile import AgentProfileLoader

        AgentProfileLoader.invalidate("custom", env)
    except Exception:  # noqa: BLE001 — the cache expires by itself
        pass
    return {"environment": env, "models": models, "agents": agents, "suite_id": suite_id,
            "knowledge_id": kb_id, "tool_server_id": server_id, "channel_id": channel_id,
            "conversations_added": conversations}
