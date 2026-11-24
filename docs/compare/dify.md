# WAJ AI Engine vs Dify: an open-source Dify alternative you operate over MCP

Dify is a large, mature platform for building LLM apps on a visual canvas,
with a RAG pipeline, a tool marketplace and a hosted cloud. WAJ AI Engine is a
young, smaller project (v0.1) that takes a different route: an agent is a row
of configuration, and you or your AI assistant create, test and publish it
through a portal or through the engine's own MCP server. If you want a canvas
and a wide catalogue of integrations, Dify is the better fit. If you want your
AI assistant to do the building and a test suite in front of every change,
look at WAJ AI Engine.

This page is written by the WAJ AI Engine maintainers. Statements about Dify
come from Dify's own pages, listed under [Sources](#sources-checked-2026-11-24).

## Feature table

| | WAJ AI Engine | Dify |
|---|---|---|
| Project status | Early release (v0.1) | Active since 2023. 161.8k GitHub stars. Release 1.20.0 on 2026-11-18 [1] |
| Licence | Apache-2.0 | Dify Open Source License: Apache-2.0 with added conditions [2] |
| How you build | Forms in the admin portal at `/admin`, or an AI assistant over MCP | Visual workflow canvas, a Prompt IDE, agent and chat apps [3] |
| Visual workflow builder | No | Yes [3] |
| Self-hosting | `git clone`, then `docker compose up`. Secrets are generated for you | Docker Compose from the `docker` directory, after copying `.env.example` and setting one generated token. Minimum 2 CPU cores and 4 GiB RAM [3] |
| Uses tools from MCP servers | Yes. Connect a server by address and choose which tools agents may use | Yes. Add a server by URL. "Only MCP servers with HTTP transport are supported." OAuth and custom headers are supported [4] |
| Other tool sources | Built-in knowledge search and hand-off to other agents. Everything else comes from MCP servers | Tool plugins from the Dify Marketplace, OpenAPI (Swagger) imports, workflows as tools [4] |
| Its own MCP server | Yes, at `/mcp`. It administers the platform: agents, instructions, tools, models, keys, tests | Yes, per app. Turning it on gives that app an MCP address, so a client such as Cursor or Claude can run the app as a tool [5] |
| An AI assistant can build and change agents over MCP | Yes. The same steps as the portal | We did not find this in the Dify docs. The MCP Server page describes running an app, not creating or editing one [5] |
| Tests before release | Test suites (expected words, expected tool calls, or a rubric for a judge model), runs against a draft, the same suite on another model, case-by-case comparison of two runs | Logs, a dashboard, annotations, run history and single-node debugging. Tracing integrations with Langfuse, LangSmith, Opik, W&B Weave, Arize and Phoenix. The Prompt IDE compares model performance. We did not find a test-suite feature in the docs index [3] [6] |
| Staging and production | An install can keep a Staging and a Production copy of each agent, with a promote step | Version control for workflows [6]. Separate environments: not checked |
| Voice | A WebSocket voice endpoint per agent (Gemini Live or Azure Voice Live) | Text-to-speech can be added to a chat-based app [3]. Realtime voice: not checked |
| Knowledge / RAG | Knowledge bases stored in Postgres with pgvector. Upload PDF, Word, Markdown, text and HTML | Knowledge bases with chunking and retrieval settings, a knowledge pipeline, metadata, and connections to external knowledge bases [6] |
| Model providers | Gemini, OpenAI, Azure OpenAI, Anthropic, Ollama, and any OpenAI-compatible endpoint | "Hundreds of proprietary / open-source LLMs from dozens of inference providers", including any OpenAI API-compatible model [3] |
| API for your agents | `POST /v1/agents/{slug}/chat` (server-sent events) and an OpenAI-compatible `/v1/chat/completions` | Every app has an API ("Backend-as-a-Service") [3]. We did not find an OpenAI-compatible endpoint in the docs index [6] |
| Teams | Admin accounts. API keys with Chat only or Full access | A workspace with Owner, Admin, Editor and Member roles [7]. The free self-hosted Community edition has one workspace. Several workspaces and SSO are in Dify Enterprise [8] |
| Hosted version | A public demo that resets every night | Dify Cloud, with a free Sandbox plan and paid plans [8] |

## Where Dify is the better choice

- **You want to draw the logic.** Dify has a visual canvas for workflows and
  chatflows, with nodes for knowledge retrieval, agents, tools and error
  handling [6]. WAJ AI Engine has no canvas.
- **You need ready integrations.** Dify has a marketplace of tool plugins and
  a long list of model providers [3] [4]. WAJ AI Engine supports six kinds of
  model provider, and every tool has to come from an MCP server.
- **RAG is the centre of your product.** Dify covers ingestion to retrieval,
  with chunk settings, a knowledge pipeline, metadata and external knowledge
  bases [6]. WAJ AI Engine has plain knowledge bases with document upload.
- **You want agents that run commands.** Dify's Agent works in a sandbox of
  its own where it runs commands, installs software and handles files [3].
  The WAJ AI Engine README describes no such sandbox: its agents call tools,
  search knowledge and hand off to other agents.
- **You want a hosted service or enterprise support.** Dify has Dify Cloud
  and an Enterprise edition with SSO and support [3] [8]. WAJ AI Engine has
  neither.
- **You want a proven project.** Dify has been public since 2023, has a very
  large community and ships releases often [1]. WAJ AI Engine is at v0.1.

## Where WAJ AI Engine is the better choice

- **Your AI assistant should operate the platform.** The engine's MCP server
  lets Claude, Cursor or another MCP client create an agent, write its
  instructions, attach tools, register models and run its tests. A Claude
  Code plugin in the repository adds skills for these steps.
- **You want tests in front of every change.** Test suites run against a
  draft of the instructions or against another model. Comparing two runs
  shows, case by case, what a change fixed and what it broke.
- **Your tools already live behind MCP.** Connect the server, untick the
  tools agents should never use, and attach the rest to agents.
- **You want existing OpenAI clients to work.** Point an OpenAI SDK or a chat
  app at `/v1` and use the agent's slug as the model name.
- **The licence matters to you.** See the next section.

## Licence differences

WAJ AI Engine is under the Apache License 2.0 with no added conditions.

Dify is under the Dify Open Source License, which its README describes as
"based on Apache 2.0 with additional conditions" [2] [3]. The licence file
allows commercial use, and says a commercial licence from Dify is needed in
two cases [2]:

- **Multi-tenant service.** "Unless explicitly authorized by Dify in writing,
  you may not use the Dify source code to operate a multi-tenant
  environment." One tenant is one workspace.
- **Logo and copyright.** When you use Dify's frontend, "you may not remove or
  modify the LOGO or copyright information in the Dify console or
  applications." This does not apply if you do not use the frontend.

The file also says contributors agree that the producer can adjust the
open-source agreement to be more strict or more relaxed [2].

For many internal uses these conditions change nothing. They matter if you
plan to host one install for several customers or to ship it under your own
brand. This is a summary, not legal advice: read the licence file itself.

## How to try both

**Dify.** Use Dify Cloud's free Sandbox plan [8], or self-host [3]:

```bash
git clone https://github.com/langgenius/dify.git
cd dify/docker
cp .env.example .env
openssl rand -hex 32
# set DIFY_AGENT_LOCAL_SANDBOX_AUTH_TOKEN in .env to the generated value
docker compose up -d
```

Then open `http://localhost/install`. Check Dify's README for the current
steps, because they change between releases.

**WAJ AI Engine.** See below. A fair test is to build the same small agent in
both: one set of instructions, one tool, five questions it must answer well.

## Sources, checked 2026-11-24

1. Repository metadata (stars, latest release): https://github.com/langgenius/dify and https://github.com/langgenius/dify/releases
2. Licence file: https://github.com/langgenius/dify/blob/main/LICENSE
3. README (key features, quick start, editions): https://github.com/langgenius/dify/blob/main/README.md
4. Tools, including MCP servers: https://docs.dify.ai/en/self-host/use-dify/workspace/tools
5. Publishing an app as an MCP server: https://docs.dify.ai/en/self-host/use-dify/publish/publish-mcp
6. Documentation index (the list of all pages we searched): https://docs.dify.ai/llms.txt
7. Workspace and roles: https://docs.dify.ai/en/self-host/use-dify/workspace/readme
8. Pricing and editions: https://dify.ai/pricing

## Try WAJ AI Engine

Live demo, nothing to install: [engine-demo.waj.ai](https://engine-demo.waj.ai).
It is a public demo that resets every night.

On your own machine, with Docker:

```bash
git clone https://github.com/waj-1/ai-engine.git
cd ai-engine
docker compose up
```

Open **http://localhost:8000** and follow the first-run page. No model key is
needed to start: a model served by Ollama on your machine works with no key.
The full steps are in the [README](../../README.md#quick-start).
