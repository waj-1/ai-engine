# Local stack

A generic, self-contained stack for running the engine on your own machine:
one Postgres (with pgvector), the REST layer the engine's data client speaks,
the auth service the admin login uses, and a gateway that puts them behind one
URL. No client, no tenant, and no committed secrets.

You need Docker and Python 3.11.

```bash
# 1. Secrets, generated on this machine (prints the admin login once)
python deploy/local/gen_env.py

# 2. Database, REST layer, auth, gateway
docker compose -f deploy/local/docker-compose.yml --env-file deploy/local/.env up -d

# 3. Schema and the admin user (safe to run again)
set -a; . deploy/local/.env; set +a
python deploy/local/init_db.py

# 4. The engine
python main.py            # http://127.0.0.1:8000/admin/login
```

No model key is needed to start. A fresh install has no agents, no knowledge
sources and no test suites; it has seven templates and a short list of Gemini
models, which work once `GOOGLE_API_KEY` is set. A build that adds a product
plugin may need more: the boot error names any missing variable.

## A model with no API key

A model served on this machine works with no key at all:

```bash
docker run -d --name ollama -p 127.0.0.1:11434:11434 -v ollama:/root/.ollama ollama/ollama
docker exec ollama ollama pull qwen2.5:3b
```

Then add it under **Agents › Models › New model**: provider **Ollama**, model
string `qwen2.5:3b`. Ollama is reached at `http://127.0.0.1:11434/v1` unless
`OLLAMA_BASE_URL` or the form's Base URL says otherwise. For any other
OpenAI-compatible server (vLLM, LM Studio, a hosted catalog) choose provider
**OpenAI** and fill in Base URL; a server on this machine or network needs no
key, a hosted one reads `OPENAI_API_KEY` (or the variable named in
`extra_config.api_key_env`).

A 3B model follows short instructions and calls tools most of the time; use a
larger one for anything real.

## Your first agent, in the portal

1. **Agents › Models › New model** — as above.
2. **Connections › Connect server** — the address of an MCP server that
   exposes your systems. *Discover tools* lists what it offers; untick what
   agents should never use.
3. **Agents › New agent** — pick the Blank template, name the agent and choose
   its model. Its address is made from the name: "Support desk" answers at
   `/v1/agents/support-desk/chat`.
4. On the agent: **Instructions** (write them, Save), **Tools & knowledge**
   (*Add toolset*, then *Choose actions*), **Settings** (a description, and
   Status › Active to publish).
5. **Test** on the agent's page talks to it, draft or published.
6. **Testing › Test suites › Create test suite** — choose the agent, add cases
   (a question and the words the answer must contain), *Run this suite*.

For a team, tick *make it available as a specialist* on each specialist's
Sub-agents tab, then add them on the parent's Sub-agents tab. The tab shows
each hand-off tool's name (`ask_support_desk`) for the parent's instructions.

`PORTAL_NAME` renames the portal shell.

## What the stack contains

| Service | Image | Port |
|---|---|---|
| `db` | `ghcr.io/supabase/postgres` | 127.0.0.1:54432 |
| `rest` | `postgrest/postgrest` | internal |
| `auth` | `ghcr.io/supabase/gotrue` | internal |
| `gateway` | `kong` | 127.0.0.1:54421 |

The engine reads both of its databases (`SUPABASE_*` and `AI_SUPABASE_*`) from
this one stack. The Supabase images come from GitHub's registry because Docker
Hub rate-limits anonymous pulls.

`gen_env.py` also turns on the generic agents API, the admin MCP server at
`/mcp`, and local MCP/HTTP tool endpoints. Tracing is off; the Conversations
screen needs the tracing stack and shows "temporarily unavailable" without it.

To start over: `docker compose -f deploy/local/docker-compose.yml down -v`,
then `python deploy/local/gen_env.py --force`.
