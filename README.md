# WAJ AI Engine

**Self-hosted chat agents you build from a portal or over MCP, not from code.**

An agent here is a row of configuration: instructions, a model, tools from the
MCP servers you connect, and optionally other agents it can hand off to. You
create and change agents in the admin portal at `/admin`, or by asking your AI
assistant through the engine's own MCP server at `/mcp`. Each published agent
answers at `POST /v1/agents/{slug}/chat`. Test suites let you measure a change
to the instructions or the model before it reaches anyone.

This is an early release. What is in this README works today on a fresh
database; what does not yet is listed under [Roadmap](#roadmap).

## Run it locally

You need Docker and Python 3.11. The engine itself runs with Python; Docker
runs the database stack it talks to (Postgres with pgvector, a REST layer, an
auth service and a gateway).

```bash
git clone https://github.com/waj-1/ai-engine.git
cd ai-engine
python3.11 -m venv venv && ./venv/bin/pip install -r requirements.lock

# 1. Secrets, generated on this machine (prints the admin login once)
./venv/bin/python deploy/local/gen_env.py

# 2. Database, REST layer, auth, gateway
docker compose -f deploy/local/docker-compose.yml --env-file deploy/local/.env up -d

# 3. Schema and the admin user (safe to run again)
set -a; . deploy/local/.env; set +a
./venv/bin/python deploy/local/init_db.py

# 4. The engine
./venv/bin/python main.py      # http://127.0.0.1:8000/admin/login
```

Log in with the email and password that step 1 printed. No model key is needed
to start. A fresh install has no agents, no knowledge sources and no test
suites: it has seven templates and a short list of Gemini models, which work
once `GOOGLE_API_KEY` is set.

More detail, including how to start over, is in
[deploy/local/README.md](deploy/local/README.md).

### A model with no API key

A model served on your own machine works with no key at all:

```bash
docker run -d --name ollama -p 127.0.0.1:11434:11434 -v ollama:/root/.ollama ollama/ollama
docker exec ollama ollama pull qwen2.5:3b
```

Add it under **Agents › Models › New model**: provider **Ollama**, model
string `qwen2.5:3b`. For any other OpenAI-compatible server (vLLM, LM Studio,
a hosted catalog) choose provider **OpenAI** and fill in **Base URL**. A small
model follows short instructions and calls tools most of the time; use a larger
one for anything real.

## Build an agent

In the portal:

1. **Agents › Models › New model**, as above.
2. **Connections › Connect server**: the address of an MCP server that exposes
   your systems. *Discover tools* lists what it offers; untick what agents
   should never use.
3. **Agents › New agent**: pick a template (Blank is the plainest), name the
   agent and choose its model. Its address is made from the name: "Support
   desk" answers at `/v1/agents/support-desk/chat`.
4. On the agent: **Instructions** (write them, Save), **Tools & knowledge**
   (*Add toolset*, then *Choose actions*), **Settings** (a description, and
   Status › Active to publish).
5. **Test**, on the agent's page, talks to it, draft or published.

For a team, tick *make it available as a specialist* on each specialist's
**Sub-agents** tab, then add them on the parent's Sub-agents tab. The parent
gets one hand-off tool per specialist (`ask_support_desk`), which its
instructions can name.

The same steps work over MCP; see [Connect your AI assistant](#connect-your-ai-assistant).

## Talk to an agent

The chat API is off unless `ENABLE_GENERIC_AGENTS=1`; `deploy/local/gen_env.py`
turns it on. For now it takes the admin user's access token (portal-issued API
keys are on the roadmap). On the local stack you get one from the auth service:

```bash
set -a; . deploy/local/.env; set +a
TOKEN=$(curl -s "http://127.0.0.1:54421/auth/v1/token?grant_type=password" \
  -H "apikey: $SUPABASE_ANON_KEY" -H "Content-Type: application/json" \
  -d "{\"email\": \"$ADMIN_EMAIL\", \"password\": \"$ADMIN_PASSWORD\"}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

curl -N http://127.0.0.1:8000/v1/agents/support-desk/chat \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"message": "What can you help me with?"}'
```

The reply streams as server-sent events: a `session` event, the answer in
`answer` chunks, then `usage` and `done`. To continue the conversation, send the
`session` event's `id` back as `session_id` with the next message.
Conversations are saved in the database.

## Connect your AI assistant

The admin MCP server at `/mcp` is on when `ENABLE_ADMIN_MCP=1` (also set by
`gen_env.py`) and takes the same token. Add it to Claude, Cursor or any MCP
client:

```json
{
  "mcpServers": {
    "waj-ai-engine": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

Building an agent this way is a handful of calls:

| Step | MCP tool |
|---|---|
| Register a model | `models(action="create", …)` |
| Connect your systems | `mcp_servers(action="create", source="external", endpoint=…)` |
| Create the agent | `templates(action="use", template_id=…, display_name=…)` |
| Give it a model, publish it, build a team | `update_profile(…)` |
| Write its instructions | `prompts(action="create_version", profile_id=…, content=…)` |
| Choose its tools | `profile_mcp_attachments(action="set", …)` |

## Test a change before it ships

From **Testing** in the portal, or over MCP:

| | In the portal | Over MCP |
|---|---|---|
| **Test suites** | Write cases: a question, the words or tool calls the answer must contain, or a rubric for a judge model | `eval_suites` |
| **Runs** | Run a suite against any agent, draft or published | `trigger_eval_run`, `eval_runs` |
| **Test a draft** | Run a suite against a draft of the instructions without serving it | `trigger_eval_run` with `prompt_overrides` |
| **Compare models** | Run the same suite on another model | `trigger_eval_run` with `model_id`, `bakeoffs` |
| **Compare runs** | Tick two runs and see, case by case, what a change fixed and what it broke | `eval_runs` |

## Supported model providers

Chat models: Google Gemini, OpenAI, Azure OpenAI, Anthropic, Ollama, and any
OpenAI-compatible endpoint. Each reads its key from the environment
(`GOOGLE_API_KEY`, `OPENAI_API_KEY`, `AZURE_OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`); a local server needs none. The local test runs used a
small model served by Ollama; the other providers are wired in but have not all
been run against the test suites in this tree.

## Configuration

`deploy/local/gen_env.py` writes everything the local stack needs to
`deploy/local/.env`. `.env.example` lists every variable the engine reads. A few
worth knowing:

| Variable | Purpose |
|---|---|
| `ENABLE_GENERIC_AGENTS` | Serve `/v1/agents/{slug}/chat` (off by default) |
| `ENABLE_ADMIN_MCP` | Serve the admin MCP server at `/mcp` (off by default) |
| `PORTAL_NAME` | The name shown in the portal (default "WAJ Admin") |
| `ALLOWED_ORIGINS` | Browser origins allowed to call the API |
| `GOOGLE_API_KEY`, `OPENAI_API_KEY`, … | Model provider keys |

The engine talks to its database through the REST and auth services in
`deploy/local/`, not over a direct Postgres connection, so it needs that stack
(or a Supabase project) rather than a bare `DATABASE_URL`.

Tracing is optional and off on the local stack. Without it, conversations are
still saved, but the portal's **Conversations** screen shows "temporarily
unavailable".

## Run the tests

The suite needs no running services, only placeholder settings so the engine
can import:

```bash
ENVIRONMENT=development \
SUPABASE_URL=http://127.0.0.1:59998 SUPABASE_ANON_KEY=test-anon \
SUPABASE_SERVICE_KEY=test-service SUPABASE_JWT_SECRET=test-jwt-secret \
AI_SUPABASE_URL=http://127.0.0.1:59999 AI_SUPABASE_ANON_KEY=test-ai-anon \
AI_SUPABASE_SERVICE_KEY=test-ai-service \
GOOGLE_API_KEY=test-google OPENAI_API_KEY=test-openai \
ALLOWED_ORIGINS="*" \
./venv/bin/python -m pytest tests/ -q
```

Run it in a shell where `deploy/local/.env` is not loaded:
`ENABLE_GENERIC_AGENTS`, `ENABLE_ADMIN_MCP` and `AI_DATABASE_URL` must be unset.

## Roadmap

Not built yet, in roughly the order we plan to build them:

- `docker compose up` at the repo root that runs the engine and its database
  together, with nothing else to install.
- A first-run page: create the admin account with a one-time setup code
  printed in the engine log, add a model key, copy an API key.
- API keys issued from the portal (shown once, revocable), accepted by the
  chat API and `/mcp` in place of the admin token.
- A demo agent seeded on first boot.
- Model keys entered in the portal and stored encrypted, instead of
  environment variables.
- Knowledge bases without a cloud embedding key (a local embedding option).
  Knowledge search works today with a Gemini or Azure OpenAI embedding key,
  but was not part of the local test runs.
- Voice for agents built in the portal. The engine has a realtime voice edge,
  but it is not yet wired to these agents.
- User-defined agent categories, managed from the portal and MCP.
- The Conversations screen reading saved chats when the tracing stack is not
  running.
- Deploy guides, written only for targets we have tested.

Known limits: team hand-offs were unreliable with a 3B local model and have not
yet been re-run with a stronger one.

## Contributing and security

Issues and pull requests are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).
Please report vulnerabilities privately as described in
[SECURITY.md](SECURITY.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
