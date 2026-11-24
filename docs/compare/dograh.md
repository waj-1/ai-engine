# WAJ AI Engine vs Dograh: an open-source Dograh alternative for text-first agents you operate over MCP

Dograh is an open-source platform for voice agents: phone calls, web calls, a
visual workflow builder and many speech and telephony providers. WAJ AI Engine
is a young project (v0.1) for agents that answer over a chat API first, with
voice as one more way in. Both can be driven by an AI assistant over MCP. For
phone agents, Dograh is the better fit. For text agents with test suites and
an OpenAI-compatible API, look at WAJ AI Engine.

This page is written by the WAJ AI Engine maintainers. Statements about
Dograh come from Dograh's own pages, listed under
[Sources](#sources-checked-2026-11-24).

## Feature table

| | WAJ AI Engine | Dograh |
|---|---|---|
| Project status | Early release (v0.1) | Active since 2025. 6.3k GitHub stars. Release 1.51.0 on 2026-11-04 [1] |
| Licence | Apache-2.0 | BSD 2-Clause [2] |
| Main use | Text agents behind an API, with optional voice | Voice agents for inbound and outbound calls [3] |
| How you build | Forms in the admin portal at `/admin`, or an AI assistant over MCP | Visual workflow builder with start, agent, global and end-call nodes, or a coding agent over MCP [3] |
| Visual workflow builder | No | Yes [3] |
| Self-hosting | `git clone`, then `docker compose up` | One command that downloads `docker-compose.yaml` and a start script and runs them. It starts PostgreSQL, Redis, MinIO, the API and the UI. A remote server needs at least 8 GB RAM and 4 vCPUs [3] [4] |
| Works with no API key | Yes, with a model served by Ollama on your machine | Yes. "Dograh ships with auto-generated keys and its own LLM / TTS / STT stack" [3] |
| Uses tools from MCP servers | Yes. Connect a server by address and choose which tools agents may use | Yes. An MCP tool connects a remote server over Streamable HTTP, with a tool filter per server and per node. HTTP API tools are also built in [5] |
| Its own MCP server | Yes, at `/mcp` | Yes, at `/api/v1/mcp/` [6] |
| An AI assistant can build and change agents over MCP | Yes: create agents, write instructions, attach tools, register models, store model keys, write and run tests | Yes: list agents, read their definitions, create workflows, edit nodes, search the docs. Edits are saved as a draft, and the draft is published from the Dograh UI [3] [6] |
| Tests before release | Test suites (expected words, expected tool calls, or a rubric for a judge model), runs against a draft, the same suite on another model, case-by-case comparison of two runs. Scripted voice checks | Test Audio (talk to the agent in the browser) and Test Chat (text, with edit and replay of user turns) [3]. We did not find test suites with stored cases in the docs index [7] |
| Quality checks on real conversations | Saved conversations in the portal, with the ones where an action failed flagged | A QA node scores every eligible call after it ends: tags, a 1 to 10 quality score, sentiment and a summary [8]. Integrations with Tuner and Noveum for observability and evaluation [7] |
| Voice | A WebSocket voice endpoint per agent, with a realtime model (Gemini Live or Azure Voice Live) | Core product. Speech-to-speech, or separate LLM, speech-to-text and text-to-speech providers. Interruption handling, pre-recorded audio, call transfer [1] [3] [7] |
| Telephony | None documented | Twilio, Vonage, Telnyx, Plivo, Vobiz, Cloudonix, Asterisk ARI and more. Outbound campaigns over contact lists [3] [7] |
| Text chat | Yes, the main channel: `POST /v1/agents/{slug}/chat` | A chat widget for websites, and Test Chat [3] [9] |
| Knowledge / RAG | Knowledge bases stored in Postgres with pgvector | Knowledge base files (PDF, Word, text, JSON, up to 5 MB) with Full Document or Chunked Search modes [10] |
| LLM providers | Gemini, OpenAI, Azure OpenAI, Anthropic, Ollama, and any OpenAI-compatible endpoint | OpenAI, Google AI Studio, Google Vertex AI, Azure OpenAI, AWS Bedrock, Groq, Hopper, OpenRouter, Hugging Face, MiniMax, Sarvam and Dograh-managed models, plus OpenAI-compatible endpoints such as Ollama and vLLM [11] |
| Speech providers | Gemini Live, Azure Voice Live | Text-to-speech: 16 named providers including ElevenLabs, Cartesia and Deepgram. Speech-to-text: 13 named providers including Deepgram, AssemblyAI and Speechmatics [12] |
| API for your agents | Chat API with server-sent events, and an OpenAI-compatible `/v1/chat/completions` | REST API and Python and Node SDKs for agents, runs, campaigns and telephony [3] [7]. We did not find an OpenAI-compatible endpoint in the docs index |
| Sign-in and teams | Admin accounts. API keys with Chat only or Full access | Local email and password sign-in, or Stack Auth for social login [13]. Roles and teams: not checked |
| Hosted version | A public demo that resets every night | A managed cloud with usage-based pricing [3] |

## Where Dograh is the better choice

- **The agent has to answer or place phone calls.** Dograh connects to
  Twilio, Vonage, Telnyx, Plivo and other carriers, handles inbound and
  outbound calls, runs campaigns over contact lists and can transfer a call
  to a person [3] [7]. WAJ AI Engine documents none of this.
- **You want to choose each part of the voice stack.** Dograh supports many
  speech-to-text and text-to-speech providers, local speech models through an
  OpenAI-compatible server, and speech-to-speech models [12]. WAJ AI Engine
  supports two realtime providers.
- **You want to design a call as a flow.** Dograh's builder has nodes and
  pathways for each step of a conversation [3]. WAJ AI Engine has no canvas.
- **You want every real call reviewed.** The QA node scores calls after they
  end [8]. WAJ AI Engine tests agents before release, then lists saved
  conversations and flags the ones where an action failed. Its docs describe
  no quality score for each live conversation.
- **You want a managed service.** Dograh has a cloud version [3].
- **A larger community.** Dograh has been public for over a year and has
  6.3k GitHub stars [1]. WAJ AI Engine has just been released.

## Where WAJ AI Engine is the better choice

- **The agent mostly works in text.** Each agent answers at a streaming chat
  endpoint and at an OpenAI-compatible endpoint, so OpenAI SDKs and chat apps
  that accept a custom base URL can use it.
- **You want test suites.** Write cases once, run them against a draft of the
  instructions or another model, and compare two runs case by case.
- **You want the assistant to do the whole loop over MCP.** Both projects let
  a coding agent build agents. In WAJ AI Engine the same MCP server also
  registers models, stores model keys, runs the tests and publishes an agent.
  In Dograh, publishing a draft is done in the UI [6].
- **You want teams of agents.** A parent agent can hand off to specialist
  agents through one tool per specialist.
- **You need Anthropic models.** They are on WAJ AI Engine's provider list.
  We did not find Anthropic by name on Dograh's LLM page, though OpenRouter
  and AWS Bedrock are listed there [11].

## Licence differences

Both licences are permissive and have no added conditions that we found.

- WAJ AI Engine: Apache License 2.0. It includes an explicit patent grant and
  asks you to keep the NOTICE file and state changes.
- Dograh: BSD 2-Clause, copyright Zansat Technologies Private Limited [2]. It
  is shorter and has no patent clause. You keep the copyright notice and the
  disclaimer.

Neither restricts commercial use or multi-tenant hosting. This is a summary,
not legal advice.

## How to try both

**Dograh.** Use the managed cloud, or self-host with the command from its
README [3]:

```bash
curl -o docker-compose.yaml https://raw.githubusercontent.com/dograh-hq/dograh/main/docker-compose.yaml && curl -o start_docker.sh https://raw.githubusercontent.com/dograh-hq/dograh/main/scripts/start_docker.sh && chmod +x start_docker.sh && ./start_docker.sh
```

Then open `http://localhost:3010`. Two things from the README to know before
you run it [3]: the quick start opens a public `trycloudflare.com` tunnel to
the API for telephony webhooks and leaves sign-up enabled until you set
`ENABLE_SIGNUP=false`, and anonymous usage data is collected unless you set
`ENABLE_TELEMETRY=false`.

**WAJ AI Engine.** See below. If your use case is voice, test Dograh first
and treat WAJ AI Engine's voice endpoint as unproven until you have placed a
real call on your own setup.

## Sources, checked 2026-11-24

1. Repository (description, stars, releases): https://github.com/dograh-hq/dograh and https://github.com/dograh-hq/dograh/releases
2. Licence file: https://github.com/dograh-hq/dograh/blob/main/LICENSE
3. README: https://github.com/dograh-hq/dograh/blob/main/README.md
4. Docker deployment: https://docs.dograh.com/deployment/docker
5. MCP tool for voice agents: https://docs.dograh.com/voice-agent/tools/mcp-tool
6. Dograh MCP server: https://docs.dograh.com/integrations/mcp
7. Documentation index (the list of all pages we searched): https://docs.dograh.com/llms.txt
8. QA node: https://docs.dograh.com/voice-agent/qa
9. Add to website (voice and chat widgets): https://docs.dograh.com/voice-agent/add-to-website
10. Knowledge base: https://docs.dograh.com/voice-agent/knowledge-base
11. LLM providers: https://docs.dograh.com/configurations/llm
12. Voice (text-to-speech) and transcriber (speech-to-text) providers: https://docs.dograh.com/configurations/voice and https://docs.dograh.com/configurations/transcriber
13. Authentication: https://docs.dograh.com/deployment/authentication

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
