# WAJ AI Engine vs Flowise: an open-source Flowise alternative you operate over MCP

Flowise is a visual, drag-and-drop builder for AI agents and flows with a
large community. Its maintainers stopped development on 2026-07-29 and the
GitHub repository was archived on 2026-08-13; the Apache-2.0 code remains
available to use and fork. WAJ AI Engine is a young project (v0.1) with no
canvas: an agent is a row of configuration that you or your AI assistant
create, test and publish through a portal or through the engine's own MCP
server.

This page is written by the WAJ AI Engine maintainers. Statements about
Flowise come from Flowise's own pages, listed under
[Sources](#sources-checked-2026-11-24). The Flowise documentation was still
online when we checked, and it describes the product as it was at the last
release.

## The state of Flowise

The announcement "Future of Flowise" gives this timeline [2]:

- 2026-07-29: "Active feature development ceases immediately."
- 2026-08-13: the repository moves to public archive status. "The code will
  remain visible indefinitely, but issues and PRs will be locked." npm
  packages and Docker images "will be marked deprecated".
- 2026-08-31: "Official core team presence in Discord and GitHub will
  conclude."

The same announcement says "the Apache 2.0 licensed code is yours to keep
building on" and encourages teams to fork the repository [2]. GitHub shows the
repository as archived and read-only, and the last release is 3.1.4 from
2026-07-29 [1]. We did not check what the wind-down means for Flowise Cloud or
for Enterprise customers.

## Feature table

| | WAJ AI Engine | Flowise (as of release 3.1.4) |
|---|---|---|
| Project status | Early release (v0.1), in active development | Archived and read-only since 2026-08-13. 55.6k GitHub stars [1] [2] |
| Licence | Apache-2.0 | Apache-2.0, except the `packages/server/src/enterprise` directory and files with their own notice, which are under a commercial licence [3] |
| How you build | Forms in the admin portal at `/admin`, or an AI assistant over MCP | Visual canvas: "Build AI Agents, Visually" [4] |
| Visual workflow builder | No | Yes: Agentflow V2 and chatflows [5] |
| Self-hosting | `git clone`, then `docker compose up` | `npm install -g flowise` then `npx flowise start` (Node.js 20 or later), or Docker Compose. Deploy guides for AWS, Azure, GCP, Digital Ocean and others [4] |
| Uses tools from MCP servers | Yes. Connect a server by address and choose which tools agents may use | Yes. A Custom MCP tool on an Agent node connects to any MCP server over stdio or Streamable HTTP [6] |
| Its own MCP server | Yes, at `/mcp`. It administers the platform: agents, instructions, tools, models, keys, tests | We did not find one in the docs index. (The documentation site has an MCP server for searching the docs, which is a different thing.) [5] |
| An AI assistant can build and change agents over MCP | Yes. The same steps as the portal | We did not find this in the docs index [5] |
| Tests before release | Test suites (expected words, expected tool calls, or a rubric for a judge model), runs against a draft, the same suite on another model, case-by-case comparison of two runs | Evaluations: datasets, text, numeric and LLM-based evaluators, re-runs that create versions you can compare. "Evaluations are only available for Cloud and Enterprise plan" [7] |
| Tracing | Optional, and off in the Docker Compose stack. Saved conversations are always listed | Integrations with Langfuse, Arize, LangWatch, Lunary, Opik and Phoenix [5] |
| Voice | A WebSocket voice endpoint per agent (Gemini Live or Azure Voice Live). Run so far with a simulated provider only | Not checked |
| Knowledge / RAG | Knowledge bases stored in Postgres with pgvector | Document Stores, with upsert and document store APIs [5] |
| Model providers | Gemini, OpenAI, Azure OpenAI, Anthropic, Ollama, and any OpenAI-compatible endpoint | Many, as chat model nodes (for example ChatAnthropic, ChatOllama, Azure ChatOpenAI) [5] |
| API for your agents | `POST /v1/agents/{slug}/chat` and an OpenAI-compatible `/v1/chat/completions` | A Prediction API and other REST endpoints [5]. We did not find an OpenAI-compatible endpoint in the docs index |
| Teams | Admin accounts. API keys with Chat only or Full access | Workspaces and role-based access control, on Cloud and Enterprise plans only [8] |
| Other | | Human-in-the-loop steps, queue mode for scaling [5] |

## Where Flowise is the better choice

- **You want to build by dragging nodes.** Flowise is built around a visual
  canvas [4]. WAJ AI Engine has none.
- **You need its integrations.** Flowise has a large set of nodes for chat
  models, document loaders, vector stores and tools, and a Custom MCP tool
  that also works over stdio [5] [6]. WAJ AI Engine has six kinds of model
  provider and gets its tools from MCP servers.
- **You already run it and it does the job.** The code stays available under
  Apache-2.0 and there is a lot of community material from before the
  wind-down. Nothing forces a move.
- **You can maintain a fork.** The maintainers encourage forks [2]. A team
  with the people to own a fork gets a much more complete product than
  WAJ AI Engine is today.
- **A quick local start without Docker.** Flowise runs from npm with two
  commands [4]. WAJ AI Engine needs Docker, or a longer local Python setup.

Before choosing Flowise for a new project, plan for the fact that the
official repository no longer takes issues, pull requests or releases [1] [2].

## Where WAJ AI Engine is the better choice

- **You want a project that is still being developed.** WAJ AI Engine is
  young and small, and it is active.
- **Your AI assistant should operate the platform.** The engine's MCP server
  lets Claude, Cursor or another MCP client create an agent, write its
  instructions, attach tools, register models and run its tests.
- **You want tests in the open-source edition.** Test suites, draft testing,
  model comparison and run comparison are part of the Apache-2.0 code.
  In Flowise, Evaluations were limited to Cloud and Enterprise plans [7].
- **You prefer configuration to a canvas.** An agent is instructions, a
  model, tools and sub-agents. Changing one is an edit that you can test and
  roll back.
- **You want existing OpenAI clients to work.** Point an OpenAI SDK or a chat
  app at `/v1` and use the agent's slug as the model name.

WAJ AI Engine is not a drop-in replacement for Flowise. There is no importer
for Flowise flows. You would rebuild each flow as an agent with instructions
and tools, and move custom nodes behind an MCP server.

## Licence differences

WAJ AI Engine is under the Apache License 2.0 for the whole repository, with
no enterprise directory.

Flowise's licence file says the code is under Apache 2.0, with two
exceptions: content under `packages/server/src/enterprise`, and files with an
explicit copyright notice such as `IdentityManager.ts`, are under a commercial
licence. Third-party components keep their own licences [3]. If you fork
Flowise, check which features depend on those files. This is a summary, not
legal advice: read the licence file itself.

## How to try both

**Flowise.** The published commands are [4]:

```bash
npm install -g flowise
npx flowise start
```

Then open `http://localhost:3000`. The announcement says the npm packages and
Docker images are marked deprecated [2], so pin the version you test
(3.1.4 is the last release [1]) or build from a fork.

**WAJ AI Engine.** See below. A fair test is to rebuild one of your existing
flows as an agent and run the same five questions through both.

## Sources, checked 2026-11-24

1. Repository (archive notice, stars, releases): https://github.com/FlowiseAI/Flowise and https://github.com/FlowiseAI/Flowise/releases
2. "Future of Flowise" announcement: https://github.com/FlowiseAI/Flowise/discussions/6727 (the page https://flowiseai.com/sunset carries the same notice, with 2026-08-10 as the planned archive date)
3. Licence file: https://github.com/FlowiseAI/Flowise/blob/main/LICENSE.md
4. README (quick start, self-hosting): https://github.com/FlowiseAI/Flowise/blob/main/README.md
5. Documentation index (the list of all pages we searched): https://docs.flowiseai.com/llms.txt
6. Tools and MCP: https://docs.flowiseai.com/tutorials/tools-and-mcp
7. Evaluations: https://docs.flowiseai.com/using-flowise/evaluations
8. Workspaces: https://docs.flowiseai.com/using-flowise/workspaces

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
