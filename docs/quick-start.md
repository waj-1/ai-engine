# Quick start, in full

The README has the three-line version. This page walks through the first run
and explains what the stack starts. For upgrades, backups, restore and
troubleshooting see [deploy/compose.md](deploy/compose.md).

You need Docker.

```bash
git clone https://github.com/waj-1/ai-engine.git
cd ai-engine
docker compose up
```

The first start builds the engine image, which takes a few minutes. Then open
**http://localhost:8000**.

## The first-run page

There is no default login. The first-run page has you do three things:

1. **Create the admin account.** It asks for the one-time setup code printed in
   the engine's log, so nobody else can claim a fresh install:
   ```bash
   docker compose logs engine
   ```
   Each start of the engine prints a new code. Five wrong tries lock the page
   until the next start (`docker compose restart engine`).
2. **Add a model key** (optional). Paste a provider's key and test it, or skip
   this and use a [local model](local-models.md).
3. **Copy your API key.** It is shown once, with a ready-to-paste `curl` call
   and the MCP client settings.

Once the admin account exists the first-run page is gone for good. The portal
has its own guide to this page:
[Set up a new install](partner/01-getting-started/04-set-up-a-new-install.md).

## Install with no browser step

Set both `ADMIN_EMAIL` and `ADMIN_PASSWORD` before the first `docker compose up`.
That account is created at start and the first-run page never appears.

```bash
ADMIN_EMAIL=you@example.com ADMIN_PASSWORD='a long passphrase' docker compose up -d
```

Setting only one of the two stops the stack with a message saying so.

## What the stack starts

That one command builds the engine and starts it with everything it needs:
Postgres with pgvector, a REST layer, an auth service and a gateway.

- **Ports.** Only the engine (port 8000) and the gateway (port 54421) are
  published, on this machine only. The database is not published.
  `ENGINE_PORT` and `GATEWAY_PORT` change them.
- **Secrets.** The install's secrets are generated inside a Docker volume, not
  written to your disk. That volume holds `SECRET_KEY`, which encrypts model
  keys saved in the portal.
- **Stop.** `docker compose down` stops the stack and keeps your data.
- **Start over.** `docker compose down -v` deletes the data and the secrets, so
  the next `up` is a new install.

The engine talks to its database through the REST and auth services that
`docker-compose.yml` starts, not over a direct Postgres connection. It needs
that stack (or a Supabase project) rather than a bare `DATABASE_URL`.

Tracing is optional and off in this stack. Without it, the portal's
**Conversations** screen lists and opens the saved chats (messages, tokens,
response time, model) and says that step-by-step detail (timings, each model
call) needs the tracing stack. **Overview** shows a "tracing is off" state
instead of live statistics.

## What a fresh install contains

No model key is needed to start. A fresh install has one agent, **Demo agent**,
plus seven templates and a short list of Gemini models. It has no knowledge
sources and no test suites.

The demo agent answers at `/v1/agents/demo/chat` with whichever model the
install can use. With none yet, it replies that no model is configured and
where to add one. Add a model and it answers, with no change to the agent. It
is seeded once: edit, archive or rename it and it stays that way.
`SEED_DEMO_AGENT=false` never seeds it.

## Add a model key

Open **Settings › Model keys**, paste your provider's key (Google Gemini,
OpenAI, Anthropic or Azure OpenAI) and click **Test**. The key is encrypted
with this install's `SECRET_KEY` before it is saved, is never shown again, and
is used from the next message.

You can instead put `GOOGLE_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` or
`AZURE_OPENAI_API_KEY` in a `.env` file next to `docker-compose.yml`. A key
saved in the portal wins.

To use a model with no key at all, see [local-models.md](local-models.md).

## Supported model providers

Chat models: Google Gemini, OpenAI, Azure OpenAI, Anthropic, Ollama, and any
OpenAI-compatible endpoint. Each takes its key from **Settings › Model keys**
or from the environment. A local server needs none. The local test runs used a
small model served by Ollama. The other providers are wired in but have not all
been run against the test suites in this tree.

## Configuration

`docker compose up` generates what the stack needs and turns on the chat API
and the MCP server. `.env.example` lists every variable the engine reads. A few
worth knowing:

| Variable | Purpose |
|---|---|
| `ENABLE_GENERIC_AGENTS` | Serve `/v1/agents/{slug}/chat` and the OpenAI-compatible `/v1/chat/completions` and `/v1/models` (off by default) |
| `ENABLE_ADMIN_MCP` | Serve the admin MCP server at `/mcp` (off by default) |
| `PORTAL_NAME` | The name shown in the portal (default "WAJ AI Engine") |
| `ALLOWED_ORIGINS` | Browser origins allowed to call the API |
| `SECRET_KEY` | Encrypts model keys saved in the portal. `docker compose up` generates it. Keep it: without it saved keys cannot be read. Rotate with `SECRET_KEY=new,old` |
| `GOOGLE_API_KEY`, `OPENAI_API_KEY`, … | Optional. Model provider keys from the environment, used when none is saved in the portal |

More settings for the Compose stack are in
[deploy/compose.md](deploy/compose.md#settings).

## Next

- Build an agent: [Create a new agent](partner/02-agents/07-new-agent.md)
- Let your AI assistant build it: [connect-your-assistant.md](connect-your-assistant.md)
- Run the engine with Python instead of a container:
  [deploy/local/README.md](../deploy/local/README.md)
