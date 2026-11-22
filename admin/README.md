# Admin portal

Configuration, testing and observability for the engine's agents, mounted at
`/admin/*` of the FastAPI service. The full screen-by-screen reference is in
`docs/admin-portal/` (also rendered in the portal at `/admin/docs`); the user
guides are in `docs/partner/` (rendered at `/admin/help`).

## How it fits together

```
┌─────────────────────────────────────────────────────────────────┐
│ Engine (FastAPI, main.py)                                       │
│                                                                 │
│  /admin/*                — this portal (Jinja + HTMX)           │
│  /mcp                    — admin MCP server (ENABLE_ADMIN_MCP)  │
│  /v1/agents/{slug}/chat  — generic (custom) agents              │
│  plugin routes           — a deployment's own agents            │
│                                                                 │
│  core/                                                          │
│    agent_profile.py      — DB row → Profile dataclass + cache   │
│    prompt_resolver.py    — prompt_version_id → text + cache     │
│    tool_registry.py      — Python tools ↔ DB row mirror         │
│    trace_recorder.py     — per-request telemetry → agent_traces │
└────────────────┬────────────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────────────┐
│ AI Supabase project (supabase/)                                 │
│  agent_profiles · agent_profile_versions · agent_prompts        │
│  agent_prompt_versions · agent_models · agent_tools             │
│  agent_mcp_servers · agent_data_sources · agent_channels        │
│  agent_eval_suites · agent_eval_runs · agent_audit_log          │
│  ai_chat_sessions · ai_chat_messages · …                        │
└─────────────────────────────────────────────────────────────────┘
                 │
                 ▼ (optional)
┌─────────────────────────────────────────────────────────────────┐
│ Laminar tracing: spans written over OTLP, read from ClickHouse  │
│ by every Conversations / Overview screen                        │
└─────────────────────────────────────────────────────────────────┘
```

`admin/router.py` is the route table (including `DOC_PAGES`, the developer docs
the portal renders). `admin/seed_*.py` are the boot-time seeders: they are
insert-if-missing, so they never overwrite an edit made in the portal.

## Running it locally

The quickest path is the self-contained stack in `deploy/local/README.md`: it
starts the database, auth and REST services, creates a superuser, and serves the
portal at `http://localhost:8000/admin`.

## Authentication

The portal is **superuser only**: a signature-verified Supabase token whose
`user` row has `permission.is_superuser`. The admin MCP server uses the same
check (`admin.auth.resolve_superuser`).

## Changing a prompt without a deploy

1. Open the agent (`/admin/agents`) and its **Instructions** tab.
2. Edit and save. The save creates a new prompt version and points the agent's
   `system` slot at it.
3. The runtime picks it up when the profile cache expires (60 seconds by
   default), or at once after a restart.
4. Promote the agent to production from Settings › Releases when it is ready.

Every step is logged in `agent_audit_log` and shown under **Activity**.

## Evals

Suites live in `agent_eval_suites`; runs in `agent_eval_runs`. Each scenario is
sent to the agent through its real endpoint and scored by checks and an LLM
judge. A suite can gate promotion. See `docs/admin-portal/03-test.md`.
