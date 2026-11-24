"""Per-parameter descriptions for every admin MCP tool.

Hosts render a tool's JSON schema to the calling model, and a bare
parameter name ("kind", "source", "environment") is ambiguous across ~50
tools. A parameter declared with `Annotated[..., Field(description=...)]`
(the shared aliases in `_common` — `Env`, `RowEnv`, `TraceEnv`, `Confirm`
— and every parameter of the newer tools) already carries its description
in the schema. Everything else gets one here, applied once at server build
time by `apply_param_descriptions`, so each meaning is written ONCE
(`GENERIC`) and only genuinely tool-specific meanings are overridden
(`OVERRIDES`). A description already present on the schema always wins.

`tests/admin_mcp/test_param_descriptions.py` fails if any registered tool
parameter is left without a description — a new parameter must be
described here (or with Field) before it ships.
"""
from __future__ import annotations

import inspect
from typing import Dict, Tuple

# One meaning everywhere it appears.
GENERIC: Dict[str, str] = {
    "profile_id": "Agent profile UUID (list_profiles / get_profile).",
    "agent_kind": "Agent kind, e.g. custom (a generic agent) or one of this deployment's own.",
    "confirm": "false (default) = dry run, nothing changes; true = execute.",
    "limit": "Maximum number of rows to return.",
    "offset": "Skip this many rows first (paging).",
    "is_active": "Active flag (false hides/disables the row without deleting it).",
    "business_id": "Business (tenant) UUID the row belongs to.",
    "suite_id": "Eval suite UUID (eval_suites action=list).",
    "display_name": "Human-readable name shown in the portal.",
    "notes": "Free-text notes stored with the version.",
    "hours": "Look-back window in hours (clamped to 1-8760).",
    "auth_env_var": "Name of the env var holding the auth token/secret "
                    "(the name only, never the value).",
    "version_id": "Version UUID.",
    "model_id": "Model UUID (models action=list).",
    "temperature": "Sampling temperature override.",
    "max_output_tokens": "Max output tokens override.",
    "timeout_seconds": "Request timeout in seconds (1-300).",
    "usage_guidance": "Free-text guidance rendered to the model next to "
                      "the tool(s) when bound.",
    "context_fields": "JSON object of context or credential fields that "
                      "the server adds to tool calls.",
    "repeats": "Run each scenario this many times (1-20) for consistency.",
    "lookback_days": "live_chats: how many days of real chats to sample from.",
    "sample_size": "live_chats: how many chats to sample.",
    "judge_model": "live_chats: model string of the LLM judge.",
    "expectations": "live_chats: rubric the judge grades against.",
    "user_id": "User id (UUID). memory also accepts an email.",
    "run_id": "Run UUID.",
    "tool_subset": "Only these tool names from the MCP (omit = all).",
    "trace_id": "Trace id (traces action=search).",
    "span_id": "Span id within the trace (traces action=get).",
    "session_id": "Chat session id (as returned by chat or sent by the "
                  "client).",
    "prompt_id": "Prompt UUID (prompts action=list).",
    "prompt_key": "Prompt slot on the profile, e.g. system, voice_persona.",
    "skill_id": "Skill UUID (skills action=list).",
    "template_id": "Template UUID (templates action=list).",
    "mcp_server_id": "MCP server UUID (mcp_servers action=list).",
    "tool_id": "Tool UUID (tools action=list).",
    "source_id": "KB source UUID (kb_sources action=list).",
    "schedule_id": "Eval schedule UUID (eval_schedules action=list).",
    "bakeoff_id": "Bake-off UUID (bakeoffs action=list).",
    "channel_id": "Channel UUID (channels action=list).",
    "fixture_id": "Test fixture UUID (test_fixtures action=list).",
    "job_id": "Job UUID returned by a tool that starts a background job.",
    "memory_id": "Memory UUID (memory action=list).",
    "var_id": "Variable UUID (env_vars action=list).",
    "target_id": "Deployment target UUID (deployment_targets action=list).",
    "audit_id": "Audit entry UUID (audit action=list).",
    "entity_type": "Only entries about this entity type, e.g. agent_profiles.",
    "channel": "Only traffic from this channel slug.",
    "channel_slug": "Only traces from this channel slug.",
    "search_text": "Free-text match over trace input/output.",
    "tool": "Only traces that called this tool (by name).",
    "include_background": "Include background/diagnostic traces (default off).",
    "q": "Search text.",
    "locale": "Language code (en, ar, ...).",
    "model_main_id": "Main chat model (model UUID).",
    "model_light_id": "Light model. Stored, but not used at run time.",
    "endpoint_path": "Route the agent is served on, e.g. /chat/stream.",
    "delegatable": "Expose this profile to its parent as an ask_<slug> "
                   "delegation tool.",
    "prompt_version_ids": "REPLACES the prompt pins: {prompt_key: "
                          "prompt_version_id}. Prefer prompts action=serve "
                          "for one slot.",
    "voice_config": "FULL REPLACE of the flat voice_config column (wipes every "
                    "key not sent). Prefer voice_config_patch.",
    "voice_config_patch": "Deep-merge into the current voice_config (null "
                          "deletes a key). Never send a nested 'live' block.",
    "tool_ids": "REPLACES the profile's tool list (use profile_attachments "
                "to add/remove one).",
    "data_source_ids": "REPLACES the profile's KB source list (use "
                       "profile_attachments to add/remove one).",
    "mcp_server_ids": "REPLACES the older MCP label list. Display only: "
                      "tools are bound from profile_attachments kind=mcp.",
    "downstream_profile_ids": "REPLACES the sub-agent list (use "
                              "profile_attachments kind=downstream).",
    "profile_a": "First profile of the pair (or use ids=[a, b]).",
    "profile_b": "Second profile of the pair (or use ids=[a, b]).",
    "version_a": "First prompt version NUMBER (or use ids=[uuid_a, uuid_b]).",
    "version_b": "Second prompt version NUMBER (or use ids=[uuid_a, uuid_b]).",
    "constant": "Dotted path of a repo prompt constant under runtime.prompts, "
                "e.g. runtime.prompts.agents_demo.SUPPORT_SYSTEM_PROMPT.",
    "override_validation": "Write even though prompt validation failed "
                           "(needs confirm=true; audited).",
    "provider": "LLM provider id, e.g. google_genai, openai, anthropic, azure_openai.",
    "model_string": "Provider model name, e.g. gemini-3-pro.",
    "extra_config": "JSON object of provider-specific settings.",
    "daily_seconds": "Daily duplex-voice allowance in audio seconds.",
    "note": "Free-text note.",
    "python_name": "Tool name the model sees (lowercase + underscores).",
    "category": "Tool category label.",
    "scope": "Tool scope: read | write (default read).",
    "additional_agent_kinds": "Extra agent kinds the tool may bind to.",
    "endpoint_url": "Absolute http(s) URL the HTTP tool calls.",
    "http_method": "HTTP method: GET | POST | PUT | PATCH | DELETE (default POST).",
    "argument_schema": "JSON schema (type object) of the tool's arguments.",
    "args": "dry_run: sample arguments to call the tool with.",
    "transport": "MCP transport for an external server: stdio | sse | http (default http).",
    "endpoint": "External MCP endpoint URL.",
    "member_tool_ids": "Bundle membership: tool ids in order "
                       "(REPLACES the bundle's members).",
    "refresh_cadence": "How often the source is refreshed (free text).",
    "freshness_sla_hours": "Alert when the source is older than this.",
    "filename": "File name of the uploaded document.",
    "page_type": "KB page type label, e.g. user_guide.",
    "language": "Language code of the content.",
    "attachments": "Full attachment list [{mcp_server_id, tool_subset?}]. "
                   "REPLACES the current set.",
    "as_subagent_of": "Wire the new agent in as a sub-agent of this profile.",
    "always_on": "Inject the skill every turn instead of on demand.",
    "linked_data_source_ids": "KB sources the skill links to.",
    "skill_ids": "Full skill list in order. REPLACES the current set.",
    "prompt_overrides": "Variant: {prompt_key: version_number} to run with.",
    "top_p": "Nucleus-sampling override.",
    "thinking_level": "Reasoning-effort override (minimal|low|medium|high).",
    "thinking_budget": "Reasoning token budget override.",
    "variant_label": "Label shown for this variant in comparisons.",
    "profile_version_id": "Pin the run to this saved profile version "
                          "(must belong to profile_id).",
    "voice_mode": "voice_live suites: sim (simulated, free) | live (real, "
                  "billable provider call) | '' = suite default.",
    "run_b": "Second run of a pair (prefer run_ids=[a, b, ...]).",
    "days": "Look-back window in days.",
    "run_ids": "Run UUIDs, in order (compare: baseline first, latest last).",
    "variants": "Bake-off variants: [{label?, profile_version_id?, "
                "model_id?, gen_config?}] (2+).",
    "suite_ids": "Eval suites every variant runs.",
    "runner_kind": "Scoring path: chat | voice_live, or a runner a plugin adds.",
    "pass_threshold": "Score (0-1) a run must reach to pass the gate.",
    "evaluator_model_id": "Model UUID of the judge model.",
    "scenarios": "Scenario objects (question, expected, ...).",
    "default_context": "JSON stored as the suite's default run context.",
    "clear_profile": "Unbind the suite from its profile.",
    "cadence": "manual | daily | weekly | monthly.",
    "is_enabled": "Whether the schedule fires automatically.",
    "suite": "Which pytest selection to run.",
    "ws_path": "WebSocket path the scenario calls (default /voice/live).",
    "steps": "Ordered scenario steps (utterances, checks).",
    "enabled": "Whether the scenario is active.",
    "target": "ws(s):// URL of a remote deployment to call instead of this "
              "engine.",
    "headers": "Per-run HTTP/WS headers (never stored).",
    "git_ref": "Git ref (branch/sha) to deploy.",
    "target_revision": "Revision name of the deployed service to roll back to.",
    "label": "Target label, unique per environment.",
    "base_url": "Service base URL, e.g. https://engine.example.com.",
    "is_default": "Make this the default target for its environment.",
    "is_secret": "Mask the value in output and audit.",
    "reveal": "Return the real (unmasked) value. Audited.",
    "include_series": "Include the per-bucket time series (large).",
    "superusers_only": "Only users with the superuser flag.",
    "grant": "true = grant superuser, false = revoke.",
    "pinned": "true = pin, false = unpin.",
    "allowed_origins": "Browser origins allowed to call (http(s)://host[:port]).",
    "allowed_agent_kinds": "Agents this channel may reach: an agent kind, or "
                           "custom:<slug> for one generic agent.",
    "default_profile_id": "Profile served when the request names none.",
    "auth_required": "Require a user JWT.",
    "rate_limit_rpm": "Requests per minute allowed (1-3600).",
    "want_diff": "Include the prompt text difference (can be large).",
    "profile": "Only findings for this profile slug.",
    "fields": "Only these columns of the row.",
}

# (tool, parameter) -> description, where the generic meaning doesn't fit.
OVERRIDES: Dict[Tuple[str, str], str] = {
    ("update_profile", "profile_id"): "Profile to update.",
    ("update_profile", "display_name"): "New display name.",
    ("update_profile", "description"): "New description.",
    ("update_profile", "status"): "active | draft | archived | specialist "
                                  "('template' refused).",
    ("update_profile", "is_active"): "Serve-able flag on the row.",
    ("update_profile", "environment"): "DEPRECATED alias of row_environment.",
    ("deployment_targets", "environment"): "DEPRECATED alias of row_environment.",
    ("env_vars", "environment"): "DEPRECATED alias of row_environment.",
    ("live_analytics", "environment"): "DEPRECATED alias of trace_env.",
    ("caching_analytics", "environment"): "DEPRECATED alias of trace_env.",
    ("deploy", "environment"): "Service to deploy. Only prod can be deployed "
                               "here.",
    ("rollback", "environment"): "Service to roll back. Only prod.",
    ("list_deploy_runs", "environment"): "Only runs that targeted this "
                                         "service ('' = all).",
    ("list_profiles", "status"): "Only profiles with this status ('' = all).",
    ("live_analytics", "status"): "success | error | '' (both).",
    ("traces", "status"): "success | error | '' (both).",
    ("skills", "status"): "active | draft | archived (archived also sets "
                          "is_active=false).",
    ("jobs", "status"): "Only jobs in this status (queued, running, ...).",
    ("get_profile", "slug"): "With agent_kind: this exact profile slug "
                             "(any status).",
    ("prompts", "slug"): "publish_from_repo: prompt slug to find the prompt "
                         "by (with profile_id).",
    ("templates", "slug"): "Slug for the new agent (default: derived).",
    ("skills", "slug"): "Skill slug (lowercase, hyphens).",
    ("voice_scenarios", "slug"): "Scenario slug.",
    ("channels", "slug"): "Channel slug (lowercase letters/digits/_-).",
    ("templates", "display_name"): "Name of the new agent made from the template.",
    ("tools", "display_name"): "Human-readable tool name.",
    ("skills", "display_name"): "Skill name shown in the catalog.",
    ("tools", "description"): "What the tool does. This is the text the model reads.",
    ("skills", "description"): "One-line catalog description of the skill.",
    ("deployment_targets", "description"): "Free-text description.",
    ("env_vars", "description"): "Free-text description.",
    ("channels", "description"): "Free-text description.",
    ("test_fixtures", "description"): "Free-text description.",
    ("prompts", "content"): "create_version: the full prompt text.",
    ("kb_sources", "content"): "upload_document: the document text.",
    ("skills", "content"): "Skill body (create/create_version) or resource "
                           "file text (add_resource).",
    ("memory", "content"): "Memory text (add/edit).",
    ("models", "name"): "Model registry name (unique per environment).",
    ("mcp_servers", "name"): "MCP server name (unique).",
    ("kb_sources", "name"): "KB source name.",
    ("bakeoffs", "name"): "Bake-off name.",
    ("eval_suites", "name"): "Suite name (sync_from_repo: the name of the "
                             "suite definition that ships with the build).",
    ("eval_schedules", "name"): "Schedule name.",
    ("env_vars", "name"): "Variable name, e.g. FEATURE_X.",
    ("channels", "name"): "Channel name.",
    ("test_fixtures", "name"): "Fixture name (lowercase, underscores).",
    ("kb_sources", "kind"): "Source kind: pgvector | ms_drive | gitbook | supabase_table | http.",
    ("channels", "kind"): "Channel kind (public_api, embed, other, or one a plugin adds).",
    ("test_fixtures", "kind"): "jwt | uuid | string | json | secret.",
    ("jobs", "kind"): "Only jobs of this kind (kb_upload, e2e, ...).",
    ("mcp_servers", "source"): "bundle (group of registered tools) | external "
                               "(remote MCP endpoint).",
    ("trigger_eval_run", "source"): "suite (grade suite scenarios) | "
                                    "live_chats (judge sampled real chats).",
    ("eval_schedules", "source"): "suite | live_chats.",
    ("env_vars", "source"): "Where the value comes from (default db).",
    ("kb_sources", "config"): "Source config JSON (pgvector: table, "
                              "pages_table, chunking, embed_model).",
    ("memory", "config"): "set_config: the nested memory settings object.",
    ("channels", "config"): "Channel config JSON.",
    ("env_vars", "value"): "Variable value ('' on update clears it).",
    ("test_fixtures", "value"): "Fixture value (stored; masked for jwt/secret).",
    ("eval_runs", "run_id"): "Run UUID (get), or the first run of a pair.",
    ("pytest_runs", "run_id"): "Test run UUID.",
    ("e2e_runs", "run_id"): "E2E run UUID.",
    ("kb_sources", "title"): "upload_document: page title (default filename).",
    ("voice_scenarios", "title"): "Scenario title.",
    ("profile_versions", "version_id"): "Profile version UUID (profile_versions action=list).",
    ("prompts", "version_id"): "Prompt version UUID (get / serve).",
    ("profile_versions", "notes"): "save: notes stored with the version.",
    ("prompts", "notes"): "Notes for the new version (publish_from_repo "
                          "default: carry the served version's notes).",
    ("skills", "notes"): "create_version: notes for the new version.",
    ("voice_scenarios", "notes"): "Free-text notes.",
    ("models", "model_id"): "Model UUID.",
    ("trigger_eval_run", "model_id"): "Variant: run with this model instead.",
    ("trigger_eval_run", "temperature"): "Variant: temperature override.",
    ("trigger_eval_run", "max_output_tokens"): "Variant: max output tokens.",
    ("trigger_eval_run", "repeats"): "Run each scenario this many times "
                                     "(1-20).",
    ("trigger_eval_run", "concurrency"): "Scenario attempts run at once "
                                         "(1-8; 0 = suite default, else 4). "
                                         "voice_live suites always run 1.",
    ("bakeoffs", "repeats"): "Repeats per variant x suite (1-20).",
    ("models", "target_env"): "promote: environment to clone the model into.",
    ("models", "is_active"): "Whether the model is selectable.",
    ("models", "temperature"): "Default temperature.",
    ("models", "max_output_tokens"): "Default max output tokens.",
    ("tools", "agent_kind"): "Agent kind the tool belongs to.",
    ("tools", "timeout_seconds"): "HTTP timeout in seconds (1-300).",
    ("mcp_servers", "timeout_seconds"): "Timeout in seconds.",
    ("tools", "auth_env_var"): "Env var holding the bearer token sent to "
                               "endpoint_url (name only).",
    ("mcp_servers", "auth_env_var"): "Env var holding the MCP endpoint's "
                                     "token (name only).",
    ("deployment_targets", "auth_env_var"): "Env var holding the target's "
                                            "auth token (name only).",
    ("kb_sources", "agent_kind"): "Agent kind the source belongs to.",
    ("skills", "agent_kind"): "Agent kind the skill serves (list: filter).",
    ("list_profiles", "agent_kind"): "Only this agent kind.",
    ("get_profile", "agent_kind"): "Look up by agent_kind (+ slug) instead "
                                   "of profile_id.",
    ("drift_check", "agent_kind"): "Only findings for this agent kind.",
    ("trigger_eval_run", "agent_kind"): "live_chats: agent kind to sample.",
    ("eval_suites", "agent_kind"): "Agent kind (derived from profile_id "
                                   "when that is given).",
    ("eval_schedules", "agent_kind"): "Agent kind (derived from profile_id "
                                      "when given).",
    ("voice_scenarios", "agent_kind"): "Agent kind the scenario calls.",
    ("memory", "agent_kind"): "Agent whose memory/config to use: a kind, or `custom:<slug>` for one custom agent (each keeps its own memories).",
    ("live_analytics", "agent_kind"): "Only this agent kind.",
    ("caching_analytics", "agent_kind"): "Only this agent kind.",
    ("traces", "agent_kind"): "Only traces of this agent kind.",
    ("voice_scenarios", "locale"): "Caller language for the scenario.",
    ("memory", "business_id"): "add: tenant id the memory belongs to, where the build's plugin has tenants (optional).",
    ("skills", "business_id"): "Tenant skill: owning business ('' = platform).",
    ("channels", "business_id"): "Tenant channel kinds: the business the channel belongs to.",
    ("voice_budgets", "business_id"): "Tenant business UUID, or any other tenant id (mapped to a stable UUID).",
    ("users", "user_id"): "set_superuser: the user to change.",
    ("users", "limit"): "list: maximum users to return.",
    ("memory", "limit"): "list: maximum memories to return (1-200).",
    ("jobs", "limit"): "Maximum jobs to return.",
    ("audit", "limit"): "Rows per page.",
    ("bakeoffs", "suite_id"): "get: focus the matrix on this suite.",
    ("eval_runs", "suite_id"): "Only runs of this suite (list/report/matrix).",
    ("eval_runs", "profile_id"): "list: only runs of this profile.",
    ("eval_runs", "limit"): "list/matrix: maximum runs.",
    ("bakeoffs", "profile_id"): "create: the profile every variant tests.",
    ("trigger_eval_run", "business_id"): "Business to run the scenarios as.",
    ("eval_schedules", "business_id"): "Business to run as.",
    ("traces", "session_id"): "session / audio: the chat session to look up.",
    ("traces", "hours"): "search: look-back window in hours (1-8760).",
    ("mcp_servers", "is_active"): "update: active flag.",
    ("kb_sources", "is_active"): "Active flag.",
    ("skills", "is_active"): "Active flag (archive forces false).",
    ("eval_suites", "is_active"): "Active flag (false = soft delete).",
    ("channels", "is_active"): "Active flag.",
    ("deployment_targets", "is_active"): "Active flag.",
}


def describe(tool: str, param: str) -> str:
    return OVERRIDES.get((tool, param)) or GENERIC.get(param) or ""


def apply_param_descriptions(server) -> None:
    """Fill in a description on every tool parameter schema that lacks one,
    and strip the docstring indentation from every tool description.
    Idempotent. `action` gets its enum spelled out and a pointer to the
    tool description, where each action's required arguments are listed."""
    for tool in server._tool_manager.list_tools():
        # A docstring keeps its source indentation; clients should not see it.
        tool.description = inspect.cleandoc(tool.description or "")
        props = (tool.parameters or {}).get("properties") or {}
        for name, spec in props.items():
            if spec.get("description"):
                continue
            if name == "action":
                choices = spec.get("enum") or []
                spec["description"] = (
                    "Operation to run" + (f": {' | '.join(choices)}" if choices else "")
                    + ". The tool description lists what each action needs.")
                continue
            text = describe(tool.name, name)
            if text:
                spec["description"] = text
