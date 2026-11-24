# Demo mode: a public, one-click demo of the engine

`DEMO_MODE=true` turns an install into a public demo anyone can open without
installing anything or making an account: one click signs them in, an AI assistant
in the portal builds and tests agents for them, the screens that hold secrets are
switched off, every visitor is rate limited, and everything they made is wiped every
night. It is off by default and changes nothing on a normal install.

The code is in `demo_mode/` (its module docstring lists the parts).

## What a visitor sees

1. **One click in.** `/`, `/admin` and the sign-in page show a **Try the demo**
   button and nothing else: no first-run page, no form. It opens on Agents, in
   staging. It signs the visitor in as
   the demo admin `demo@example.com` (`example.com` is reserved, so it is no one's
   mailbox), with a session of its own. Its password is derived from `SECRET_KEY`
   and never typed, shown or stored.
2. **A banner** on every portal page: this is a public demo, anyone can see what you
   type, it resets every night, and a link to the repository to self-host it.
3. **Build with AI.** A side panel on every page, open on the first visit, where the
   visitor talks to the *Builder assistant*. It does the work with the engine's own
   admin tools: creates an agent from a template, writes its instructions, attaches
   the Northwind tools and knowledge base, adds a specialist sub-agent, creates a test
   suite and runs it, chats with an agent, compares runs and explains a failed case.
   Each tool call shows as a step in plain words with a link to what it made
   ("Created Outage helper from a template · Open Outage helper"). Starter prompts are
   shown as chips. After a test run it waits for the result (up to two minutes) and
   reports the score and the failing cases. The conversation survives moving between
   pages, even mid-turn. The Builder assistant itself is read-only and hidden from
   the Agents list, pickers, the Conversations filter and search.
4. **The Northwind demo.** A fictional home internet provider, *Northwind Internet*:

   | Agent | |
   |---|---|
   | Support desk | Northwind's tools (`check_outage`, `get_order_status`, `create_ticket`, `list_plans`) and the knowledge base, hands billing questions to the Billing helper |
   | Billing helper | Specialist sub-agent of the Support desk |
   | Sales assistant | Tools and knowledge base |
   | IT helpdesk | Instructions only |
   | Builder assistant | The assistant in the Build with AI panel |

   Categories *Support*, *Sales* and *Internal tools*; the knowledge base *Northwind
   help center* (two documents); a five-case test suite for the Support desk; the
   channel *Northwind website*; and a few stored example conversations (no model is
   called to make them).

## The builder assistant

`demo_mode/assistant.py`. A normal custom agent, `builder-assistant`, seeded with its
own instructions and the builder model; visitors can open it on Agents. The panel
posts to `POST /admin/demo/assistant/chat`, which runs it with one addition: the admin
MCP server's tool functions (`admin_mcp/`) called **in process**, as the visitor's
demo session. The public admin MCP endpoint `/mcp` is off in demo mode.

Only these tools are bound, each narrowed to the listed actions and to an allow-list
of arguments (any other argument is dropped before the tool runs, so `env` is always
staging and no environment variable can be named as a credential):

| Tool | Actions |
|---|---|
| `list_profiles`, `get_profile`, `update_profile`, `fork_profile` | (no endpoint, voice or environment changes) |
| `templates`, `agent_types`, `profile_attachments`, `mentionables`, `search`, `chat` | all |
| `prompts` | get, list, list_versions, create_version, serve, diff |
| `kb_sources` | list, get, create, update, upload_document (built-in tables, upload size cap) |
| `mcp_servers` | list, get, members, discover, create, update: only the built-in Northwind server |
| `models` | list, get |
| `eval_suites` | list, get, create, update (at most 10 cases, chat runner) |
| `trigger_eval_run`, `eval_runs` | suite runs only, at most 2 repeats |
| `promote_profile` | staging to production, without `force` |
| `wait_for_test_run` (the demo's own) | waits up to two minutes for a run, returns its score and failing cases |

Not available: model keys, API keys, users, environment variables, deploys, rollbacks
and deployment targets, runtime flags, background jobs, the audit log, memories,
skills, bake-offs, voice checks and the rest. `check_call` refuses any call outside the
list even when the model asks for it.

## What is locked

`demo_mode/policy.py`, applied by `demo_mode/gate.py` (an ASGI middleware, so
WebSocket handshakes too) to every request before any route runs. Refused for GET and
POST, and hidden from the sidebar, Settings and search:

- Settings › Model keys, API keys (no key can be minted), Environment variables,
  Team & access, Voice budgets
- Diagnostics: drift check, runtime environment, telemetry check, engineering tests,
  the MCP flag probe
- Developer docs and the API reference
- Deployment targets, deploy runs, code deploy and rollback
- The background jobs list, prompt optimisation, the first-run page
- `/mcp` (the admin MCP server), `/agents` (the sample-agents portal), voice calls
  (unless `DEMO_VOICE` is on, see "Voice calls" below)

Read-only (GET works, changes are refused): Models, scheduled test runs, model
comparisons, voice checks, Activity.

Usable: agents (create, edit, instructions, attachments, versions, promote staging →
production), knowledge bases and uploads, tests, conversations, channels, connections.

Tools: a tool server or HTTP tool at a local, private or internal address is refused
when it is saved and when it is called, and nothing widens that
(`MCP_ALLOW_LOCAL_ENDPOINTS` and `HTTP_TOOL_ALLOW_HOSTS` are removed from the process
in demo mode). No process secret (`auth_env_var`) is ever sent to any address. The
Northwind server at `/demo/northwind/mcp` is the one exception: the engine calls it in
process (`httpx.ASGITransport`), never over the network. Knowledge bases stay on the
built-in tables.

The public chat API (`/v1/agents/<slug>/chat`) accepts only the visitor's demo session
(no API key can be minted), so it serves the portal's test chat and the assistant.

## Caps

Per visitor: a visitor is their session and their network address, both counted (the
address with `DEMO_IP_MULTIPLIER` times the room), in sliding one-hour windows kept in
memory. Over a limit, a chat or the assistant answers "You are going a little fast",
other requests get HTTP 429.

The whole demo: `DEMO_DAILY_TURNS` model turns per UTC day, counted in the database
(`demo_usage_daily`, shared by every engine process). Agent chat turns, assistant
turns and test-judge calls (one per graded case, taken when a run starts) all count.
Once it is used up, chats and the assistant answer that the demo has reached today's
limit and that the engine can be self-hosted for free; no model is called.

Every model answer is capped at `DEMO_MAX_OUTPUT_TOKENS`, whatever its model row says.

## Voice calls

Off by default: the voice socket (`WS /v1/agents/<slug>/voice`) is refused, and an
agent's Voice tab says so. A realtime model bills by the minute, so turn it on only
with the caps below in mind.

With `DEMO_VOICE=true` and a realtime model set up, the seed adds a model row named
"Demo voice model" and turns voice on for the Support desk, so a visitor can open its
Voice tab and press **Call** with nothing to set up. The banner and the entry page link
to it (`/admin/demo/call`). Visitors can turn voice on for their own agents too, with
that model.

The realtime model, one of:

- `DEMO_VOICE_PROVIDER=azure_voice_live` (the default): an Azure AI Foundry resource
  that serves Voice Live. Set `DEMO_VOICE_ENDPOINT` to its address and
  `DEMO_VOICE_API_KEY` to its key. The key has a name of its own, so it unlocks this
  one model and nothing else.
- `DEMO_VOICE_PROVIDER=google_genai`: Gemini Live, with `GOOGLE_API_KEY`. Note that
  this key also makes every Gemini chat model row usable by visitors, so prefer a key
  made for the demo, with its own quota.

Every call runs under `demo_mode/voice.py`, whatever the agent's own voice settings
say (a visitor can edit those):

- one call lasts at most `DEMO_VOICE_MAX_CALL_SECONDS`;
- at most `DEMO_VOICE_MAX_CALLS` calls are open at the same time;
- the whole demo has `DEMO_VOICE_DAILY_SECONDS` seconds of calls per UTC day, every
  agent and visitor together, counted in the database next to the turn cap (in memory
  when the database cannot be reached, so the cap never switches off);
- one visitor may place `DEMO_VOICE_CALLS_PER_HOUR` calls an hour.

A call is charged every 15 seconds while it runs and for its last part when it ends.
When the day's seconds run out, running calls end and new ones are refused, each with
a sentence the Call button shows. The first three caps do not depend on the visitor's
address, so they hold for a caller who reaches the server without the CDN. The engine's
own voice budget (`VOICE_LIVE_DAILY_SECONDS`) is per agent and still applies; it does
not bound the demo, because a visitor can make agents.

Calls are never recorded in demo mode (`VOICE_RECORDING_ENABLED` is forced off). The
transcript is saved like a chat and is wiped by the nightly reset. The answers come
from the agent's chat model; those turns are not taken from `DEMO_DAILY_TURNS`, they
are bounded by the day's voice seconds. The plain `/voice/live` socket, Settings ›
Voice budgets and running voice checks stay locked.

## The nightly reset

Every day at `DEMO_RESET_HOUR_UTC` the engine deletes everything visitors could make
or change (agents and their versions and instructions, knowledge bases and documents,
tool servers and HTTP tools, skills, test suites, runs and fixtures, conversations
including assistant chats, traces, memories, channels, categories, background jobs,
the activity log), runs the engine's own seeders and seeds the Northwind demo again.
It keeps the templates (back on their first instructions version), the model rows,
the demo admin and the day's usage count. With
several engine processes exactly one runs it each day.

By hand, or from an outside scheduler (set `DEMO_RESET_HOUR_UTC=off` then), inside the
engine's container:

```bash
python -m scripts.demo_reset           # wipe and seed again
python -m scripts.demo_reset --seed    # only add what is missing
```

At every start the engine also seeds whatever part of the demo is missing, so a fresh
database becomes the demo on the first `up`.

## Settings

All read at call time. Only `DEMO_MODE` and the model settings matter to most hosts.

| Setting | Default | What it does |
|---|---|---|
| `DEMO_MODE` | off | The whole feature. |
| `DEMO_PUBLIC_URL` | `http://localhost:8000` | The demo's own address. The Northwind server row is stored under it. |
| `DEMO_REPO_URL` | the project's GitHub repository | Where the banner sends people who want to self-host. |
| `DEMO_MODEL_PROVIDER` | `azure_openai` | `azure_openai` (endpoint and key from `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_API_KEY`), or `openai` for any OpenAI-compatible server at `DEMO_MODEL_BASE_URL` (a local Ollama, say). |
| `DEMO_MODEL_BASE_URL` | | The server for `DEMO_MODEL_PROVIDER=openai`, e.g. `http://host.docker.internal:11434/v1`. |
| `DEMO_AGENT_DEPLOYMENT` | `gpt-5.4-mini` | The model (Azure deployment name) of the demo's agents and of new agents made from templates. |
| `DEMO_BUILDER_DEPLOYMENT` | `gpt-5.5` | The builder assistant's model. |
| `DEMO_JUDGE_DEPLOYMENT` | the builder's | The model that grades test runs. |
| `DEMO_AZURE_API_VERSION` | `2025-04-01-preview` | Azure OpenAI API version of the demo's model rows. |
| `DEMO_REASONING_EFFORT` | `low` | Reasoning effort sent to the models (`none` sends none). |
| `DEMO_DAILY_TURNS` | 2000 | Model turns per UTC day for the whole demo. |
| `DEMO_CHAT_PER_HOUR` | 60 | Agent chat turns per visitor per hour (the portal test chat, the assistant's test chats, a test run's cases). |
| `DEMO_ASSISTANT_PER_HOUR` | 30 | Messages to the builder assistant per visitor per hour. |
| `DEMO_ASSISTANT_TOOLS_PER_HOUR` | 150 | The assistant's tool calls per visitor per hour. |
| `DEMO_ASSISTANT_TOOLS_PER_TURN` | 20 | The assistant's tool calls per message. |
| `DEMO_TEST_RUNS_PER_HOUR` | 6 | Test runs, E2E runs, prompt comparisons and tool dry runs per visitor per hour. |
| `DEMO_MAX_TEST_CASES` | 20 | The most cases (cases × repeats) one test run may grade. |
| `DEMO_UPLOADS_PER_HOUR` | 10 | Document uploads per visitor per hour. |
| `DEMO_MAX_UPLOAD_BYTES` | 262144 (256 KB) | The largest document a visitor may upload (portal or assistant). |
| `DEMO_ENTRIES_PER_HOUR` | 30 | "Try the demo" sign-ins per visitor per hour. |
| `DEMO_IP_MULTIPLIER` | 3 | One network address may use this many visitors' worth of each limit. |
| `DEMO_CLIENT_IP_HEADER` | | A header that carries the visitor's address, set by a CDN in front of the demo (`cf-connecting-ip` behind Cloudflare). Blank: the connection's address, as the proxy settings resolve it. Set it only when every request comes through that CDN. |
| `DEMO_MAX_OUTPUT_TOKENS` | 2000 | The longest answer any model may write (reasoning included). |
| `DEMO_VOICE` | off | Let visitors place voice calls. Needs a realtime model (next rows). |
| `DEMO_VOICE_PROVIDER` | `azure_voice_live` | `azure_voice_live` or `google_genai` (Gemini Live, key in `GOOGLE_API_KEY`). |
| `DEMO_VOICE_ENDPOINT` | | The Azure Voice Live resource, e.g. `https://<resource>.services.ai.azure.com`. |
| `DEMO_VOICE_API_KEY` | | The key of that resource. A secret: set it the way you set `AZURE_OPENAI_API_KEY`. |
| `DEMO_VOICE_MODEL` | `gpt-4.1-mini` (Azure), `gemini-3.1-flash-live-preview` (Gemini) | The realtime model. |
| `DEMO_VOICE_API_VERSION` | `2026-04-10` | Azure Voice Live API version. |
| `DEMO_VOICE_NAME` | `en-US-AvaMultilingualNeural` (Azure), `Kore` (Gemini) | The voice callers hear. |
| `DEMO_VOICE_LANGUAGE` | `en-US` | The language of the seeded agent's calls. |
| `DEMO_VOICE_DAILY_SECONDS` | 1800 | Seconds of calls per UTC day for the whole demo. |
| `DEMO_VOICE_MAX_CALL_SECONDS` | 180 | The longest one call may last (30 at least). |
| `DEMO_VOICE_MAX_CALLS` | 2 | Calls open at the same time. |
| `DEMO_VOICE_CALLS_PER_HOUR` | 6 | Calls per visitor per hour. |
| `DEMO_RESET_HOUR_UTC` | 3 | The hour of the nightly reset, or `off` when an outside scheduler runs `scripts.demo_reset`. |

Demo mode also sets, in its own process: `ENABLE_GENERIC_AGENTS=1`,
`CONVERSATION_INSIGHTS=0` (a background model call per chat),
`VOICE_RECORDING_ENABLED=0` (a visitor's voice is never kept), `EMBED_PROVIDER=azure`
unless another embedder is set (`AZURE_EMBED_DEPLOYMENT`, default
`text-embedding-3-small`), and removes `ENABLE_ADMIN_MCP`, `MCP_ALLOW_LOCAL_ENDPOINTS`,
`HTTP_TOOL_ALLOW_HOSTS` and `VOICE_LIVE_SIM_ENABLED`. It does not seed the sample-agents
portal, the generic starter agent or the environment-variables mirror.

## Run it with docker compose

On the normal stack (Postgres, REST layer, auth service, gateway, engine), with the
override file `deploy/compose/demo.yml`:

```bash
export AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com/
export AZURE_OPENAI_API_KEY=<key>
docker compose -f docker-compose.yml -f deploy/compose/demo.yml up -d
```

Open `http://localhost:8000` and click **Try the demo**. The Azure resource needs three
deployments, named as in the settings above (or set the `DEMO_*_DEPLOYMENT` names): a
small chat model for the agents, a stronger one for the assistant and the judge, and
`text-embedding-3-small` for the knowledge base.

Without Azure, any OpenAI-compatible server works, for example Ollama on the host:

```bash
DEMO_MODEL_PROVIDER=openai DEMO_MODEL_BASE_URL=http://host.docker.internal:11434/v1 \
DEMO_AGENT_DEPLOYMENT=qwen2.5:7b DEMO_BUILDER_DEPLOYMENT=qwen2.5:7b \
EMBED_BASE_URL=http://host.docker.internal:11434/v1 EMBED_MODEL=nomic-embed-text \
docker compose -f docker-compose.yml -f deploy/compose/demo.yml up -d
```

(The assistant drives many tools and needs a capable model: a small local model will
stumble.) To start over: `docker compose down -v`.

## Hosting it

The engine image is the normal one. A host provides:

- The engine's usual settings: `SUPABASE_URL`, `SUPABASE_ANON_KEY`,
  `SUPABASE_SERVICE_KEY`, `SUPABASE_JWT_SECRET`, the same four `AI_SUPABASE_*` (one
  database serves both), `SECRET_KEY`, `ENABLE_GENERIC_AGENTS=1`, `PORT`, and the
  proxy settings (`FORWARDED_ALLOW_IPS`, so per-visitor limits see the visitor's
  address, not the proxy's).
- `DEMO_MODE=true` and `DEMO_PUBLIC_URL=https://<the demo's address>`.
- `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_API_KEY` (a secret), plus the deployment
  names if they differ from the defaults.
- A database with the engine's migrations applied, `demo_usage` among them; the auth
  service with sign-ups off (the demo admin is created through its admin API).
- Health check: `GET /health`.
- One replica is enough; more work (the daily cap and the reset are shared through the
  database, per-visitor limits are per replica). Do not run it with the admin MCP
  endpoint or any `MCP_ALLOW_LOCAL_ENDPOINTS` override: demo mode removes them anyway.
- The nightly reset needs nothing more (the engine runs it at `DEMO_RESET_HOUR_UTC`);
  a host that scales to zero overnight should set `DEMO_RESET_HOUR_UTC=off` and run
  `python -m scripts.demo_reset` from a scheduled job instead.

## One host, no compose (e.g. Azure Container Instances)

The same stack runs where every container shares one network namespace (they reach
each other on `127.0.0.1`), nothing orders their start and no file from this
repository can be mounted. Three things make that work:

- `deploy/compose/setup.py` takes the addresses from `STACK_GATEWAY_URL`,
  `STACK_DB_HOST`, `STACK_REST_UPSTREAM` and `STACK_AUTH_UPSTREAM` (defaults: the
  compose service names), and also writes `kong.yml` (the gateway config, with this
  install's keys and those upstreams) and `db-init.sql` (the database's first-start
  scripts in one file) into the state volume, so the gateway and the database read them from there.
- `deploy/compose/start_engine.sh` starts the engine in any order: it waits for the
  setup step's `engine.env`, runs the init step (which waits up to `INIT_WAIT_SEC`,
  default 300, for the auth service and the REST layer, then applies the schema) and
  execs uvicorn. It exits non-zero on a failure, so the platform restarts it. The
  engine seeds the demo itself as it starts (demo account, Northwind demo).
- Ports must not collide on the shared host: Postgres 5432, REST 3000, auth 9999,
  gateway **54321** (`KONG_PROXY_LISTEN=0.0.0.0:54321`, `KONG_ADMIN_LISTEN=off`; its
  default 8000/8001 would collide), engine **8080** (the image's default; also a port
  Cloudflare proxies, which 8000 is not).

Volumes: `state` (mounted at `/run/engine` in every container), `pg-data` (the
database) and `initdb` (an empty directory at `/docker-entrypoint-initdb.d` in the
database container). All three can be empty directories that live as long as the
group: a restart of the whole group is then a new install (new secrets, empty database)
that seeds the demo again, which suits a demo that resets every night. `state` holds
the install's secrets: mount it only into these containers.

Only the engine's port is published. Images and tags are the ones in
`docker-compose.yml`.

**1. `setup`, an init container** (runs once, before the others start)

- Image: the engine image. Command: `python deploy/compose/setup.py`.
- Env: `STACK_GATEWAY_URL=http://127.0.0.1:54321`, `STACK_DB_HOST=127.0.0.1:5432`,
  `STACK_REST_UPSTREAM=http://127.0.0.1:3000`, `STACK_AUTH_UPSTREAM=http://127.0.0.1:9999`.
- Volume: `state` at `/run/engine`, writable. It must be able to write there; in
  compose it runs as root for that reason (the image's own user is `appuser`).

**2. `db`**

- Image: `ghcr.io/supabase/postgres:15.8.1.060`.
- Command: `bash -c 'cp /run/engine/db-init.sql /docker-entrypoint-initdb.d/01-init.sql && set -a && . /run/engine/stack.env && set +a && exec docker-entrypoint.sh postgres -c listen_addresses=*'`
- Env: `POSTGRES_HOST=/var/run/postgresql`, `POSTGRES_DB=postgres`,
  `POSTGRES_USER=postgres`, `POSTGRES_PORT=5432`, `JWT_EXP=3600`.
- Volumes: `state` at `/run/engine` (read-only), `initdb` at
  `/docker-entrypoint-initdb.d`, `pg-data` at `/var/lib/postgresql/data`.

**3. `rest`**

- Image: `postgrest/postgrest:v12.2.3`. Command: `/bin/postgrest /run/engine/postgrest.conf` (the image sets no `PATH`, and a platform that replaces the entrypoint may not add one).
- Volume: `state` at `/run/engine` (read-only). Retries the database by itself.

**4. `auth`**

- Image: `ghcr.io/supabase/gotrue:v2.158.1`.
- Command: `sh -c 'set -a && . /run/engine/stack.env && set +a && exec auth'`
- Env: `GOTRUE_API_HOST=0.0.0.0`, `GOTRUE_API_PORT=9999`,
  `API_EXTERNAL_URL=http://127.0.0.1:54321`, `GOTRUE_DB_DRIVER=postgres`,
  `GOTRUE_SITE_URL=<the demo's public address>`, `GOTRUE_URI_ALLOW_LIST=*`,
  `GOTRUE_DISABLE_SIGNUP=true`, `GOTRUE_JWT_ADMIN_ROLES=service_role`,
  `GOTRUE_JWT_AUD=authenticated`, `GOTRUE_JWT_DEFAULT_GROUP_NAME=authenticated`,
  `GOTRUE_JWT_EXP=3600`, `GOTRUE_EXTERNAL_EMAIL_ENABLED=true`,
  `GOTRUE_MAILER_AUTOCONFIRM=true`.
- Volume: `state` at `/run/engine` (read-only). It exits while the database is not up
  yet: give the group a restart policy of Always.

**5. `gateway`**

- Image: `kong:2.8.1`.
- Command: `bash -c 'set -a && . /run/engine/stack.env && set +a && exec /docker-entrypoint.sh kong docker-start'`
- Env: `KONG_DATABASE=off`, `KONG_DECLARATIVE_CONFIG=/run/engine/kong.yml`,
  `KONG_PROXY_LISTEN=0.0.0.0:54321`, `KONG_ADMIN_LISTEN=off`,
  `KONG_DNS_ORDER=LAST,A,CNAME`, `KONG_PLUGINS=request-transformer,cors,key-auth,acl`,
  `KONG_NGINX_PROXY_PROXY_BUFFER_SIZE=160k`, `KONG_NGINX_PROXY_PROXY_BUFFERS=64 160k`.
- Volume: `state` at `/run/engine` (read-only).

**6. `engine`**

- Image: the engine image. Command: `sh deploy/compose/start_engine.sh`.
- Port: **8080**, published (the only one).
- Env: `PORT=8080`, `INIT_WAIT_SEC=300`, `DEMO_MODE=true`,
  `DEMO_PUBLIC_URL=https://<the demo's public address>`,
  `DEMO_CLIENT_IP_HEADER=cf-connecting-ip` (behind Cloudflare),
  `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY` (secret), and any other `DEMO_*`
  setting above. Everything else (database, auth, `SECRET_KEY`) comes from the state
  volume.
- Volume: `state` at `/run/engine` (read-only).
- Health check: `GET /health` on port 8080. Allow a few minutes on the first start
  (the init step waits for the database and applies the schema).
- Size: 1 vCPU and 2 GB for the engine; 0.5 vCPU and 1 GB for the database; the other
  containers are small (0.25 vCPU and 0.5 GB each).

The nightly reset runs inside the engine at `DEMO_RESET_HOUR_UTC`; nothing else is
needed.
