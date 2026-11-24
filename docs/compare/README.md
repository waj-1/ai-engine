# WAJ AI Engine compared with Dify, Flowise and Dograh

These pages compare WAJ AI Engine with three other open-source projects people
look at for building AI agents. They are written by the WAJ AI Engine
maintainers, so read them as one side's view. Every statement about another
project links to that project's own pages, checked on 2026-11-24. If something
here is wrong or out of date, please open an issue.

WAJ AI Engine is an early release (v0.1). Dify, Flowise and Dograh have been
public for longer and have far more users, contributors and integrations.

- [WAJ AI Engine vs Dify](dify.md)
- [WAJ AI Engine vs Flowise](flowise.md)
- [WAJ AI Engine vs Dograh](dograh.md)

## Summary

| | WAJ AI Engine | Dify | Flowise | Dograh |
|---|---|---|---|---|
| What it is | Self-hosted agent platform. Agents are configuration. | LLM app development platform: workflows, RAG, agents | Visual builder for AI agents and flows | Voice agent platform with telephony |
| Project status | Early release (v0.1) | Active. 161.8k GitHub stars, release 1.20.0 on 2026-11-18 | Archived. The repository became read-only on 2026-08-13. Last release 3.1.4 on 2026-07-29 | Active. 6.3k GitHub stars, release 1.51.0 on 2026-11-04 |
| Licence | Apache-2.0 | Dify Open Source License: Apache-2.0 with added conditions (multi-tenant use, logo) | Apache-2.0, with the `enterprise` directory under a commercial licence | BSD 2-Clause |
| How you build | Forms in an admin portal, or an AI assistant over MCP | Visual workflow canvas, plus agent and chat apps | Visual canvas | Visual workflow builder, or a coding agent over MCP |
| Self-hosting | `docker compose up` | Docker Compose | npm or Docker Compose | One script that runs Docker Compose |
| Uses tools from MCP servers | Yes | Yes (HTTP transport) | Yes (Custom MCP: stdio and Streamable HTTP) | Yes (Streamable HTTP) |
| Has its own MCP server | Yes, for administering the platform | Yes, per app: an MCP client can run the app | We did not find one in the docs | Yes, for working in your workspace |
| An AI assistant can build and change agents over MCP | Yes: agents, instructions, tools, models, tests | We did not find this in the docs | We did not find this in the docs | Yes: read agents, create workflows, save edits as drafts. Publishing is done in the UI |
| Tests before release | Test suites with judge rubrics, draft testing, model comparison, run comparison | Logs, annotations and tracing integrations. We did not find a test-suite feature in the docs index | Evaluations with datasets and evaluators, on Cloud and Enterprise plans only | Test Audio and Test Chat, and a QA node that scores real calls after they end |
| Voice | WebSocket voice endpoint (Gemini Live, Azure Voice Live). | Text-to-speech can be added to chat apps. Realtime voice: not checked | Not checked | Core product: phone calls, web calls, many speech providers |
| Knowledge / RAG | Knowledge bases on pgvector | Knowledge bases, a knowledge pipeline, external knowledge bases | Document Stores | Knowledge base files, full-document or chunked search |
| Model providers | Gemini, OpenAI, Azure OpenAI, Anthropic, Ollama, any OpenAI-compatible endpoint | "Hundreds" of models from "dozens" of providers | Many, through chat model nodes | 11 named LLM providers plus OpenAI-compatible endpoints |
| OpenAI-compatible API for your agents | Yes: `/v1/chat/completions`, `/v1/models` | We did not find one in the docs index | We did not find one in the docs index | We did not find one in the docs index |
| Teams | Admin accounts, API keys with two access levels | Workspace with four roles. One workspace in the Community edition | Workspaces and roles on Cloud and Enterprise plans | Not checked |
| Hosted version | Public demo only | Dify Cloud | Not checked after the wind-down | Dograh cloud |

Sources for each cell are listed at the bottom of the page for that project.

## Which should you pick

**Pick Dify** when you want a visual workflow canvas, a large marketplace of
tools and model providers, a full RAG pipeline, and a project with a very
large community and a company behind it. It does much more than WAJ AI Engine
does today. Read its licence first if you plan to offer it to several
customers from one install or to remove its branding.

**Think carefully about Flowise for new work.** Its maintainers stopped
development on 2026-07-29 and archived the repository on 2026-08-13. The
Apache-2.0 code stays available and they encourage forks. It is a reasonable
choice if you already run it, or if you are ready to maintain a fork. If you
are looking for a replacement, Dify is the closest of these projects in
shape, because it also has a visual canvas. WAJ AI Engine has none.

**Pick Dograh** when the job is phone calls. It has telephony integrations
(Twilio, Vonage, Telnyx, Plivo and others), outbound campaigns, call transfer
and a long list of speech providers. WAJ AI Engine has a voice endpoint, but
voice is not its centre and it documents no telephony integration.

**Pick WAJ AI Engine** when you want all of these together:

- Agents defined as configuration (instructions, a model, tools, sub-agents),
  with no canvas to draw.
- Tools that come from MCP servers you already run.
- An AI assistant such as Claude or Cursor doing the building and testing
  through the engine's own MCP server.
- Test suites in front of each change to the instructions or the model.
- Plain Apache-2.0 with no added conditions.

Accept that it is young: fewer integrations, a small community, no visual
builder, and some paths (live voice audio, several model providers) that have
not been run end to end in the public tree. The
[README](../../README.md) says which.

## Sources, checked 2026-11-24

The full source list for each column is at the bottom of its page. The main
ones:

- Dify: [repository](https://github.com/langgenius/dify), [licence](https://github.com/langgenius/dify/blob/main/LICENSE), [docs index](https://docs.dify.ai/llms.txt), [pricing and editions](https://dify.ai/pricing)
- Flowise: [repository](https://github.com/FlowiseAI/Flowise), [licence](https://github.com/FlowiseAI/Flowise/blob/main/LICENSE.md), ["Future of Flowise"](https://github.com/FlowiseAI/Flowise/discussions/6727), [docs index](https://docs.flowiseai.com/llms.txt)
- Dograh: [repository](https://github.com/dograh-hq/dograh), [licence](https://github.com/dograh-hq/dograh/blob/main/LICENSE), [docs index](https://docs.dograh.com/llms.txt)
- WAJ AI Engine: the [README](../../README.md) and the [portal guides](../partner/README.md) in this repository

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
