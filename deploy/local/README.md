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

`gen_env.py` generates an admin login and `init_db.py` creates that account, so
this hand-run path goes straight to the login page. To try the **first-run
page** instead, as `docker compose up` at the repository root does, leave the
admin out:

```bash
python deploy/local/gen_env.py --no-admin     # add --force to replace an existing .env
```

`init_db.py` then creates no account, and the engine prints a one-time setup
code when it starts:

```
================================================================
  FIRST-RUN SETUP: this engine has no admin account yet.

      Setup code:  K7QM-2XHD-9RTV-4NPB
  …
```

Open http://127.0.0.1:8000: the page asks for that code, creates the admin
account, offers to store a model key and shows an API key once. Each start of
the engine prints a new code and the old one stops working; 5 wrong tries lock
the page until the next start. Once an admin exists the page is gone for good.
Set both `ADMIN_EMAIL` and `ADMIN_PASSWORD`, or neither: half a login stops
`init_db.py`. `FIRST_RUN_SETUP=0` turns the page off.

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

For a hosted provider (Gemini, OpenAI, Anthropic, Azure OpenAI), paste its key
on **Settings › Model keys** instead of setting a variable. It is stored
encrypted with the `SECRET_KEY` that `gen_env.py` wrote into `.env`, and used
from the next message. Keep that `SECRET_KEY`: without it the stored keys
cannot be read.

## Knowledge bases with a local model

Documents are made searchable by an embedding model. With no `GOOGLE_API_KEY`,
use one on this machine:

```bash
docker exec ollama ollama pull nomic-embed-text
```

and add two settings to `deploy/local/.env`, then load it again (the `set -a`
line in step 3) and start the engine:

```bash
EMBED_BASE_URL=http://127.0.0.1:11434/v1
EMBED_MODEL=nomic-embed-text
```

**Knowledge** in the portal then names the model in force, and uploads work.
Until one is set up, the same screen says what to add. Any OpenAI-compatible
server works the same way; a hosted one also needs its key in `EMBED_API_KEY`.

Pick the model before you upload. Vectors from two models cannot be compared,
so changing `EMBED_MODEL` (or adding a Gemini key and removing these two
settings) on a knowledge base that already has documents needs
`python -m scripts.reembed --target kb --apply`, run with the new settings.
The portal flags a knowledge base that was left behind.

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

## Voice

An agent can take voice calls at `WS /v1/agents/<slug>/voice`. The realtime
model that listens and speaks needs a key of its own, in `deploy/local/.env`
(or the root `.env` of the one-command stack), then restart the engine:

| Realtime model | Key |
|---|---|
| Gemini Live (seeded: **Agents › Models** lists the realtime rows) | `GOOGLE_API_KEY` |
| Azure Voice Live (add a model: provider `azure_voice_live`, extra settings `{"realtime": true, "endpoint": "https://<resource>.cognitiveservices.azure.com", "auth_env_var": "AZURE_VOICE_LIVE_API_KEY"}`) | the variable named in `auth_env_var` |

Then, on the agent: **Settings › set up voice for this agent**, tick **Duplex
voice enabled**, pick the **Realtime model**, a **Voice ID** and a **Language**,
and save. **Call** on that page places a call from the browser (the microphone
needs `localhost` or HTTPS). The brain that answers is the agent's own chat
model, so that model needs its key too.

Without a realtime key the page says which variable is missing instead of
offering the call, and the socket closes with the same sentence (code 4502).

**With no realtime key** the rest of the call path can still be tried, in
text. Start the engine with `VOICE_LIVE_SIM_ENABLED=1`, turn voice on for the
agent and pick any realtime model, then on the same machine:

```bash
python deploy/compose/voice_text_call.py support-desk "What can you help me with?"
```

Each message is delivered as if the caller had said it, and the agent's spoken
answer comes back as text. The flag lets a caller on this machine swap in a
simulated realtime model, and such a call needs no token, so leave it off on a
server other people can log in to. Voice test suites in *sim* mode use the
same flag.

A caller that is not a browser sends the token as a header, or as the first
frame:

```
{"type": "auth", "token": "<admin token>"}
```

Audio frames are raw 16-bit PCM at the rates in the server's `ready` frame;
`voice/live/protocol.py` lists every frame. Calls are capped per agent per day
(`VOICE_LIVE_DAILY_SECONDS`, default 3600 seconds), and recorded for the
Conversations screen unless `VOICE_RECORDING_ENABLED=0`. Voice checks (**Testing › Voice checks**) speak
the caller's lines with Azure Speech and need `AZURE_SPEECH_KEY`.

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
`/mcp`, and local MCP/HTTP tool endpoints. Tracing is off: the Conversations
screen lists and opens the saved chats from the database, and says that
step-by-step detail (timings, each model call) needs the tracing stack.

The first boot seeds a **Demo agent** at `/v1/agents/demo/chat`. It uses
whichever model the install can reach; with none it replies that no model is
configured and where to add one.

To start over: `docker compose -f deploy/local/docker-compose.yml down -v`,
then `python deploy/local/gen_env.py --force`.
