# WAJ AI Engine

**Self-hosted chat agents you build from a portal or over MCP, not from code.**

<!-- overview-video -->
[![WAJ AI Engine in action](docs/media/overview/engine-overview.webp)](docs/media/overview/engine-overview.mp4)

▶ [Watch with sound](docs/media/overview/engine-overview.mp4) (35 s) · [Video tour of every section](docs/partner/README.md#watch-it-first)
<!-- /overview-video -->

An agent here is a row of configuration: instructions, a model, tools from the
MCP servers you connect, and optionally other agents it can hand off to. You
create and change agents in the admin portal at `/admin`, or by asking your AI
assistant through the engine's own MCP server at `/mcp`. Each published agent
answers at `POST /v1/agents/{slug}/chat`. Test suites let you measure a change
to the instructions or the model before it reaches anyone.

This is an early release. What is in this README works today on a fresh
database; what does not yet is listed under [Roadmap](#roadmap).

## Quick start

You need Docker.

```bash
git clone https://github.com/waj-1/ai-engine.git
cd ai-engine
docker compose up
```

Open **http://localhost:8000**. There is no default login. The first-run page
has you do three things:

1. **Create the admin account.** It asks for the one-time setup code printed in
   the engine's log, so nobody else can claim a fresh install:
   ```bash
   docker compose logs engine
   ```
   Each start of the engine prints a new code; five wrong tries lock the page
   until the next start (`docker compose restart engine`).
2. **Add a model key** (optional). Paste a provider's key and test it, or skip
   this and use a local model.
3. **Copy your API key.** It is shown once, with a ready-to-paste `curl` call
   and the MCP client settings.

Once the admin account exists the first-run page is gone for good. For an
install with no browser step, set both `ADMIN_EMAIL` and `ADMIN_PASSWORD`
before the first `docker compose up`: that account is created at start and the
first-run page never appears.

That one command builds the engine and starts it with everything it needs:
Postgres with pgvector, a REST layer, an auth service and a gateway. The
install's secrets are generated inside a Docker volume, not written to your
disk, and only the engine (port 8000) and the gateway (port 54421) are
published, on this machine only. `docker compose down` stops it and keeps your
data; `docker compose down -v` deletes the data and the secrets, so the next
`up` is a new install.

No model key is needed to start. A fresh install has one agent, **Demo agent**,
plus seven templates and a short list of Gemini models; it has no knowledge
sources and no test suites. The demo agent answers at `/v1/agents/demo/chat`
with whichever model the install can use. With none yet, it replies that no
model is configured and where to add one; add a model and it answers, with no
change to the agent. It is seeded once: edit, archive or rename it and it
stays that way (`SEED_DEMO_AGENT=false` never seeds it).

**Add a model key.** Open **Settings › Model keys**, paste your provider's key
(Google Gemini, OpenAI, Anthropic or Azure OpenAI) and click **Test**. The key
is encrypted with this install's `SECRET_KEY` before it is saved, is never
shown again, and is used from the next message. You can instead put
`GOOGLE_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` or
`AZURE_OPENAI_API_KEY` in a `.env` file next to `docker-compose.yml`; a key
saved in the portal wins.

To run the engine with Python instead of in a container (for development),
see [deploy/local/README.md](deploy/local/README.md).

### A model with no API key

A model served on your own machine works with no key at all:

```bash
ollama pull qwen2.5:3b
```

Add it under **Agents › Models › New model**: provider **Ollama**, model
string `qwen2.5:3b`. The engine's container reaches Ollama on your machine at
`host.docker.internal:11434`, so Ollama has to listen on an address containers
can reach, not only on `127.0.0.1`: start it with `OLLAMA_HOST=0.0.0.0`, or, on
Linux with Ollama in Docker, publish it on the Docker bridge address
(`-p 172.17.0.1:11434:11434`). For any other OpenAI-compatible server (vLLM, LM Studio,
a hosted catalog) choose provider **OpenAI** and fill in **Base URL**. A small
model follows short instructions and calls tools most of the time; use a larger
one for anything real.

### Knowledge bases with no API key

Documents are made searchable by an embedding model. Without a Gemini key, use
one on your machine:

```bash
ollama pull nomic-embed-text
echo "EMBED_MODEL=nomic-embed-text" >> .env
docker compose up -d
```

Any OpenAI-compatible server works (`EMBED_BASE_URL`, `EMBED_MODEL`). The
**Knowledge** screen names the model in use, or says what to set when there is
none. Choose the model before uploading: changing it later means re-embedding
every document (`python -m scripts.reembed --target kb --apply`), and the
screen marks a knowledge base that needs it. Attach a knowledge base on an
agent's **Tools & knowledge** tab and the agent gets a `search_knowledge` tool.
A small chat model does not always call it; in our run a 1.5B model answered
without searching and a 3B model searched and answered from the document.

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

To keep many agents tidy, group them into your own **categories** (Support,
Sales, Internal tools) under **Agents › Categories**, then choose the category
in the New agent window or on an agent's **Settings** tab. The Agents list
shows and filters by it. A category only organises the list: it does not
change how an agent runs. A fresh install has none.

The same steps work over MCP; see [Connect your AI assistant](#connect-your-ai-assistant).

## Talk to an agent

The chat API takes an API key. Use the key from first-run
setup, or open **Settings › API keys**, create a key with **Chat only** access
and copy it. It is shown once, and only
a hash of it is stored.

```bash
export AI_ENGINE_API_KEY=wae_...

curl -N http://localhost:8000/v1/agents/support-desk/chat \
  -H "Authorization: Bearer $AI_ENGINE_API_KEY" -H "Content-Type: application/json" \
  -d '{"message": "What can you help me with?"}'
```

A new install already has an agent to try: replace `support-desk` with `demo`.

Each key has its own conversations: one key cannot read or continue another's,
and `GET /ai/sessions` with the key lists them.
Revoke a key on the same screen and it stops working on its next request.

The reply streams as server-sent events: a `session` event, the answer in
`answer` chunks, then `usage` and `done`. To continue the conversation, send the
`session` event's `id` back as `session_id` with the next message.
Conversations are saved in the database.

### By voice

An agent can take calls at `WS /v1/agents/<slug>/voice`. On the agent, open
**Settings › set up voice for this agent**, tick **Duplex voice enabled**, pick
a realtime model, a voice and a language, and save; **Call** on that page
places a call from your browser. The realtime model needs its own key:
`GOOGLE_API_KEY` for Gemini Live, or the variable named on an Azure Voice Live
model. Without it the page and the socket say which key to set. A caller that
is not a browser sends an API key, as an `Authorization` header or as a first
frame `{"type": "auth", "token": "…"}`. Calls are capped per agent per day
(`VOICE_LIVE_DAILY_SECONDS`).

What we have run: the whole call path with a simulated realtime provider (the
question arrives as if spoken, the agent's own model answers, the spoken answer
comes back as text). We have not yet placed a call with real audio against
these agents in this tree, so speech recognition, synthesis and interruption
with a live provider are unverified here. To run the same check:

```bash
VOICE_LIVE_SIM_ENABLED=1 docker compose up -d
docker compose exec -T engine sh deploy/compose/with_env.sh \
  python deploy/compose/voice_text_call.py support-desk "What are your office hours?"
```

Leave `VOICE_LIVE_SIM_ENABLED` unset on a shared server: with it, a call from
the engine's own machine needs no key.

## Connect your AI assistant

The admin MCP server at `/mcp` takes an API key with **Full access** (a Chat
only key is refused there). Create one under **Settings › API keys**. Add it to Claude, Cursor or any MCP
client:

```json
{
  "mcpServers": {
    "waj-ai-engine": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer wae_..." }
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
| Group agents into categories | `agent_types(action="create", display_name=…)`, then `update_profile(profile_id=…, agent_type="support")` |
| Store a model key | `provider_keys(action="set", provider=…, value=…)` |

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
OpenAI-compatible endpoint. Each takes its key from **Settings › Model keys**
or from the environment; a local server needs none. The local test runs used a
small model served by Ollama; the other providers are wired in but have not all
been run against the test suites in this tree.

## Configuration

`docker compose up` generates what the stack needs and turns on the chat API
and the MCP server. `.env.example` lists every variable the engine reads. A few
worth knowing:

| Variable | Purpose |
|---|---|
| `ENABLE_GENERIC_AGENTS` | Serve `/v1/agents/{slug}/chat` (off by default) |
| `ENABLE_ADMIN_MCP` | Serve the admin MCP server at `/mcp` (off by default) |
| `PORTAL_NAME` | The name shown in the portal (default "WAJ AI Engine") |
| `ALLOWED_ORIGINS` | Browser origins allowed to call the API |
| `SECRET_KEY` | Encrypts model keys saved in the portal. `docker compose up` generates it. Keep it: without it saved keys cannot be read. Rotate with `SECRET_KEY=new,old` |
| `GOOGLE_API_KEY`, `OPENAI_API_KEY`, … | Optional. Model provider keys from the environment, used when none is saved in the portal |

The engine talks to its database through the REST and auth services that
`docker-compose.yml` starts, not over a direct Postgres connection, so it needs
that stack (or a Supabase project) rather than a bare `DATABASE_URL`.

Tracing is optional and off in this stack. Without it, the portal's
**Conversations** screen lists and opens the saved chats (messages, tokens,
response time, model) and says that step-by-step detail (timings, each model
call) needs the tracing stack. **Overview** shows a "tracing is off" state
instead of live statistics.

## Run the tests

The suite needs Python 3.11 and no running services, only placeholder settings
so the engine can import (`make develop` creates `venv/`):

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

Not done yet:

- Deploy guides for more targets. Today there is one, for the target we have
  run: [Docker Compose on one machine](docs/deploy/compose.md), with backup,
  restore and upgrade. Serving the engine to other machines behind a TLS
  proxy, cloud container services and Kubernetes are not tested yet.
- A call with real audio against a portal-built agent (see "By voice").
- A local embedding model's retrieval quality has only been spot-checked.

Known limits: team hand-offs were unreliable with a 3B local model and have not
yet been re-run with a stronger one.

## Contributing and security

Issues and pull requests are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).
Please report vulnerabilities privately as described in
[SECURITY.md](SECURITY.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
