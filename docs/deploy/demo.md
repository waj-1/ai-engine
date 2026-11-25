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
3. **Build with AI.** A side panel on every page, where the visitor talks to the
   *Builder assistant*. It opens by itself on the first visit when the guided tour is
   off; with the tour on, the tour's Welcome comes first and the panel stays closed
   until the visitor asks for it. On a phone the panel is a full-screen sheet, so it
   does not open by itself there either: a small hint by the **Build with AI** button
   points at it instead. It does the work with the engine's own
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
   | Support desk | Northwind's tools (`check_outage`, `get_order_status`, `create_ticket`, `escalate_to_human`, `list_plans`) and the knowledge base, hands billing questions to the Billing helper and a customer who asks for a person to the support team |
   | Billing helper | Specialist sub-agent of the Support desk |
   | Sales assistant | Tools and knowledge base |
   | IT helpdesk | Instructions only |
   | Builder assistant | The assistant in the Build with AI panel |

   Categories *Support*, *Sales* and *Internal tools*; four knowledge bases
   (*Northwind help center*, *plans and pricing*, *billing and refunds* and the staff
   *IT handbook*, nine documents in all), each attached to the agents that need it;
   three playbooks under Knowledge › Skills; a five-case test suite for each of the
   four agents, with a schedule for each; three voice checks for the Support desk
   (where voice is on); the channels *Northwind website* (a visitor's own chats in
   the portal arrive on it) and *Sample traffic* (the demo's own conversations, next
   section); and, only on an install with no tracing store, a few stored example
   conversations (no model is called to make them; with tracing on Conversations and
   Overview read traces, so a stored chat would be on no report and none is seeded).

   After seeding, the engine runs its own **test round** in the background
   (`demo_mode/warmup.py`): each agent's suite once, then the same suite on the agents'
   model and on the builder's model, one after another. A few minutes after a start
   or a reset, Testing shows real results, a run history, the schedules' last run and
   a model comparison per agent. These are real model calls (about 110 of the day's
   turns) and are skipped when runs already exist. `DEMO_WARMUP=off` turns the round
   off.

## Sample traffic: reports that are never empty

A visitor who opens Overview at any hour should find recent activity on every report.
So the demo talks to its own agents all day (`demo_mode/sample_traffic.py`): short
scripted customer conversations, one to four messages each, from a pool of about forty
across the four agents. Only the customer's lines are scripted. Every turn is a real
one, posted to the agent's own chat address the way a website does it, answered by the
real model with the real tools and knowledge bases, traced and saved like a visitor's.
Nothing is written to the trace store by hand and no number is made up.

The pool is written against the demo's own facts (the outages in Riverside and Old
Town, orders NW-1042 and NW-1077, the plans, the billing dates, the staff IT handbook)
and includes, on purpose and labelled in the code: conversations that end in a ticket;
billing questions the Support desk hands to the Billing helper; a customer who asks for
a person (`escalate_to_human`, which is what Overview counts as *Handed to a person*); a
malformed order number, which the order tool refuses (a real *failed action*, so *Needs
attention* is not always 0); an order that does not exist (a normal answer, not a
failure); and two in Arabic.

- **How a visitor can tell.** The conversations arrive on their own channel, *Sample
  traffic* (Connections › Channels describes it; Conversations can be filtered by it),
  and Overview says so in one line under its status. They are counted on every report
  like any conversation. Test runs and the builder assistant stay out, as before.
- **When.** A burst of `DEMO_SAMPLE_BURST` conversations after a new install's first
  start and after each nightly reset, so nothing is empty in the first minutes. Then
  one conversation (two on about four ticks in ten) every 20 to 40 minutes during
  Northwind's support hours, 08:00 to 20:00 on its clock (`DEMO_SAMPLE_UTC_OFFSET`),
  and one at half that pace at night, so the hourly charts have a shape.
- **Budget.** At most `DEMO_SAMPLE_DAILY_TURNS` turns a day (default 200; the default
  pace uses about 80), and each is also one of `DEMO_DAILY_TURNS`. The generator stops
  for the day once `DEMO_SAMPLE_STOP_AT_PERCENT` (default 60) of the daily turns are
  used by anyone: when visitors are busy it goes quiet first.
- **Several engine processes, restarts.** Each tick is claimed in the database first
  (one process per 20-minute slot), and so is the start burst (once a UTC day per
  database), so a second process or a restart does not double the traffic.
- **With tracing off** it runs the same; the conversations are saved and listed under
  Conversations from the database.
- `DEMO_SAMPLE_TRAFFIC=off` switches it off. The test round then holds five
  conversations once, as it used to.

**What happened in conversations** (Overview) is filled from these conversations: the
engine's insights sweep reads each one about 30 minutes after it goes quiet and makes
one short model call (the agents' model, at most 400 output tokens) for its topic,
outcome and language. On the demo it reads the sample conversations only, never a
visitor's chat, a test run or the builder assistant, at most `DEMO_INSIGHTS_DAILY_CALLS`
a day (default 100), each also one of the daily turns (`demo_mode/insights.py`).
`DEMO_INSIGHTS_DAILY_CALLS=0` switches it off.

**What it costs**, at Azure's list prices for a Global Standard `gpt-5.4-mini`
deployment on 2026-11-24 ($0.75 per million input tokens, $4.50 per million output
tokens). Measured on a rehearsal that day: a sample turn used 1,983 input and 159
output tokens on average (a turn is often two model calls, one that picks a tool and
one that answers, and a hand-over to the Billing helper is a third), and an insights
call 329 input and 73 output tokens. The default pace is about 54 conversations a day,
so about 81 turns and 54 insights calls (135 of the day's 2,000 turns): roughly 178,000
input and 17,000 output tokens a day, about $0.21 a day or $6.40 a month. At the full
default budgets (200 turns and 100 insights calls) it is about 430,000 input and 39,000
output tokens a day, $0.50 a day or $15 a month.

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

The demo's own work comes out of the same daily turns: the test round (about 110), the
sample traffic (`DEMO_SAMPLE_DAILY_TURNS`, which stops by itself once
`DEMO_SAMPLE_STOP_AT_PERCENT` of the day is used) and the insights calls
(`DEMO_INSIGHTS_DAILY_CALLS`).

## Voice calls

Off by default: the voice socket (`WS /v1/agents/<slug>/voice`) is refused, and an
agent's Voice tab says so. A realtime model bills by the minute, so turn it on only
with the caps below in mind.

With `DEMO_VOICE=true` and a realtime model set up, the seed adds a model row named
"Demo voice model" and turns voice on for the Support desk, the Sales assistant and the
IT helpdesk (the Billing helper is a specialist callers do not reach), so a visitor can
open an agent's Voice tab and press **Call** with nothing to set up. The banner and the
entry page link to the Support desk's (`/admin/demo/call`). An agent a visitor makes
(from a template, as a draft copy or with Build with AI) starts with the same voice
settings, and its Test agent panel links to its call. Such an agent is also added to the
*Northwind website* channel's agents: the portal is served from that channel's address,
so without it the agent's Test chat and Call were refused. Visitors can turn voice off, or
change it, on the Voice tab.

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

### Talking to the builder assistant

With voice on, the Build with AI panel gets a microphone button. Pressing it opens a
call with the builder assistant (`WS /admin/demo/assistant/voice`,
`demo_mode/assistant_voice.py`). It is the engine's shared voice handler with the
builder behind it: the "Demo voice model" hears the visitor and speaks, and each
request goes to the builder assistant itself, with the same tools, allow-list and step
limit as a typed message, in the panel's conversation, so typed and spoken turns share
one history. The panel shows the call's words, and the steps and full answer of each
spoken request as they come; the voice says a holding line when a request goes out and
then only the first sentence of the answer. While a call is open, a link in the panel
(a step's "Open ...", a link in an answer) opens in a new tab, so following it does not
end the call; on a phone that tab shows the page with the panel closed. Once the call is
over, links behave as before.

There is nothing to set up beyond `DEMO_VOICE` and the realtime model above: the call's
voice settings are in code, not on a row a visitor can edit. Each call is a demo call:
it counts against all four caps above, and the Support desk's calls and the builder's
share the same lines and the same day's seconds. Each spoken request is also a builder
message: it counts against `DEMO_ASSISTANT_PER_HOUR`, takes a turn from
`DEMO_DAILY_TURNS` and its tool calls count against the two tool limits. Only the
signed-in visitor can call, and only from the demo's own page. A builder turn may take
up to 150 seconds (a test run is waited for), within the call's length limit.

Calls are not recorded in demo mode unless `DEMO_VOICE_RECORDING=true`. With it on,
the audio of a call with one of the portal's own agents is kept and shown on the
conversation's **Call audio** tab. Every visitor shares one login, so anyone visiting
the demo can play it until the nightly reset deletes it; the Voice tab's note says
so. The Build with AI microphone and the product page's showcase calls are never
recorded, whatever the setting. The
transcript is saved like a chat and is wiped by the nightly reset. The answers come
from the agent's chat model; those turns are not taken from `DEMO_DAILY_TURNS`, they
are bounded by the day's voice seconds. The plain `/voice/live` socket, Settings ›
Voice budgets and running voice checks stay locked.

## The product-page showcase

The demo shows how agents are built. The showcase shows what a finished product made
from such agents looks like: a product page on another site runs a live chat and voice
demo of four agents of the fictional provider Falak Connect, and the portal links to that
page ("See these agents in a real product page"). Off unless `DEMO_SHOWCASE` is on.

The four agents are ordinary portal agents, seeded with the rest of the demo (so the
nightly reset brings them back) and read-only in the portal, so a visitor cannot break the
page that runs them:

| Use case | Agent | Knowledge |
|---|---|---|
| Overdue bill (outbound call) | `falak-collections` | the case file only |
| Upgrade offer (outbound call) | `falak-offers` | the case file only |
| Customer support | `falak-support` | Falak Connect help center |
| Plans and pricing | `falak-plans` | Falak Connect help center |

Each is instructions, the demo's agent model, no tools, and a voice setup with one Azure
neural voice per language and accent (`voice_by_language`, keyed `en-US`, `en-GB`,
`en-AU`, `en-IN`, `ar-SA`, `ar-AE`, `ar-EG`, `ar-JO`, then `en` and `ar`). The page picks a
use case (three per agent) and the engine hands that case's file to the agent for the
conversation: who the customer is, their record, the answers to the identity check, what
the agent may do and the reference of each action. The cases are the fixtures in
`runtime/tools/agents_demo/data`; the story cards' wording is `demo_mode/showcase_stories.py`.

The public entry is narrow (`demo_mode/showcase_routes.py`):

    GET  /showcase/api/catalog          the four agents; what voice and chat can do now
    GET  /showcase/api/{key}/cases      one agent's story cards
    POST /showcase/api/chat             one chat turn (server-sent events)
    WS   /showcase/api/{key}/voice      a call; first frame {"type": "auth", "token": "showcase"}

The two reads are public data. A chat turn or a call needs an `Origin` from
`DEMO_SHOWCASE_ORIGINS` (or the demo's own address), one of the four agents and one of
its curated cases, and room under the showcase's own caps, which are apart from the
portal's:

- chat: `DEMO_SHOWCASE_DAILY_TURNS` turns per UTC day for everyone, and
  `DEMO_SHOWCASE_CHAT_PER_HOUR` per visitor;
- voice: `DEMO_SHOWCASE_VOICE_DAILY_SECONDS` seconds per UTC day for everyone,
  `DEMO_SHOWCASE_VOICE_MAX_CALL_SECONDS` per call, `DEMO_SHOWCASE_VOICE_MAX_CALLS` at
  once, and `DEMO_SHOWCASE_VOICE_CALLS_PER_HOUR` per visitor.

A browser cannot fake its `Origin`; any other client can, so no cap depends on it. The
day's totals, the call length and the calls at once hold whatever address a caller
claims; only the per-visitor limits key on the address. To make those hold too, trust the
address only from your CDN: set `FORWARDED_ALLOW_IPS` to the CDN's ranges and leave
`DEMO_CLIENT_IP_HEADER` blank, so a caller who reaches the server directly is counted by
their real address.

A refusal says why, with a code first: `[budget]`, `[busy]`, `[rate]`, `[length]` or
`[off]` (a 429 with `{"detail", "code"}` for chat, a close 4429 with that reason for a
call), so the page can show its own sentence and offer chat when voice is busy or used up.

The seed also adds a channel, "Product page showcase", with the page's origins and these
four agents, so the engine's CORS and its voice Origin check admit the page; set
`ALLOWED_ORIGINS` to include the page's origins as well, so CORS never depends on a row.

## Usage analytics

Off by default. With `DEMO_POSTHOG_KEY` set (and demo mode on), every demo page loads
`admin/static/demo-analytics.js`, which sends events from the visitor's browser to
your PostHog project. The key is the project's public API key: it can only send
events, so it is not a secret. Nothing is loaded and no request goes to PostHog
when the key is blank. The code is `demo_mode/analytics.py` and that script.

Everyone shares one demo login, so the events are anonymous. Each browser gets a
random id from PostHog, kept in a first-party cookie and local storage.

What is sent:

- event names, all starting `engine_demo_`, plus `$pageview` and `$pageleave`;
- on every event `app: "engine_demo"` and `env`: `public` on the demo's own
  address (`DEMO_PUBLIC_URL`), `local` anywhere else;
- the page's section (agents, an agent's tab, knowledge, testing and so on) and its
  path with every id replaced by `:id`; of the query string only `tab`, `env`,
  `show` and the `utm_*` campaign keys;
- for Build with AI: that the panel opened, that a message was sent (typed, or the
  number of the suggestion chip), and when the answer ends the names of the tools
  and actions it used, how many steps ran or failed, how long it took (rounded),
  and whether the answer was the daily cap or the rate limit;
- for the test chat: that a message was sent and answered, whether the agent was
  one of the seeded ones or one a visitor made, and whether a cap answered;
- that an agent was made (from a template, a copy or the assistant), a test run was
  started (and how it ended), a form was saved (its route, such as
  `/admin/agents/:id/instructions`), a voice call started and ended (its length in
  5-second steps and why it ended);
- that a locked screen or a cap was hit, and which one;
- that "Try the demo" was clicked, and clicks on links that leave the demo:
  GitHub, docs, the product page or "other";
- with the guided tour on, its step ids, how a step was done and the tool names of
  its AI turns;
- what PostHog's browser library adds to every event: browser, device, screen
  size, the referring site, and the country it derives from the address.

What is never sent: anything a visitor types (prompts, chat messages, names of
agents, knowledge bases or files), any text from the page (answers, step labels,
page titles), the rest of the query string, or the id of a run, agent or
conversation. There is no session replay, no autocapture of clicks or inputs, no
heatmaps and no surveys, whatever the PostHog project's own settings say.

The script reads a copy of the streams the panel and the test chat already
receive and watches the voice sockets open and close; it does not change what
those pages do. When your product's site on the parent domain (the demo at
`demo.example.com`, the site at `example.com`) uses the same PostHog project, the
two share the visitor id through PostHog's cookie on the parent domain, so a path
from that site into the demo reads as one visitor.

## The guided tour

With `DEMO_TOUR` on (the default in demo mode), a first visit opens a **Welcome**
window that asks what the visitor wants to see first, then a checklist of 17 steps in
four tracks: Build an agent, Prove it works, Wire it into my stack and Operate it.
Each step has a coach card on the real screen (**Show me**), a **Let AI do it** that
hands the step to the Builder assistant, and a "Same step, in code" box with the MCP
call, curl or Python behind the click. A step ticks however it is done: with the card,
by the assistant or by hand. The four Wire in steps about code show the code in one
card, since API keys are off in the demo. The steps that change an agent (tools,
sub-agents, promote) work only on an agent the visitor made, so the shared sample
agents stay as they are.

It is plain JavaScript and CSS (`admin/static/demo-tour.js`, `demo-tour-steps.js`,
`demo-tour.css`), loaded only when both `DEMO_MODE` and `DEMO_TOUR` are on. Nothing is
stored on the server and there are no tables or routes for it: progress is kept in the
visitor's browser (`localStorage`, else `sessionStorage`, else memory). The browser
keys it to the demo's data: a new id each time the demo is wiped and seeded again (it
is derived from the Builder assistant's rows, which every reset makes anew), so it
clears with the nightly reset, with `scripts.demo_reset` and with an outside
scheduler alike. If that id cannot be read, the date of the last scheduled reset
stands in for it.

`DEMO_TOUR=false` switches the tour off without a code change; the demo then behaves
as it did before the tour, with Build with AI opening on the first visit.
`manual_tests/demo_tour_track_a.js` walks the Build track end to end with screenshots.

## The nightly reset

Every day at `DEMO_RESET_HOUR_UTC` the engine deletes everything visitors could make
or change (agents and their versions and instructions, knowledge bases and documents,
tool servers and HTTP tools, skills, test suites, runs and fixtures, conversations
including assistant chats, traces (and the trace store itself, where the demo has one), memories, channels, categories, background jobs,
the activity log), runs the engine's own seeders and seeds the Northwind demo again. The one
thing it keeps is the demo's own sample conversations, for `DEMO_SAMPLE_KEEP_DAYS` days,
and only those it can prove a visitor did not touch (see the Tracing section); nothing
a visitor typed, made or said survives.
It keeps the templates (back on their first instructions version), the model rows,
the demo admin and the day's usage count. With
several engine processes exactly one runs it each day.

By hand, or from an outside scheduler (set `DEMO_RESET_HOUR_UTC=off` then), inside the
engine's container. Where the install's settings live in the state volume (the compose
stack and the one-host layout below), `with_env.sh` loads them first:

```bash
sh deploy/compose/with_env.sh python -m scripts.demo_reset           # wipe and seed again
sh deploy/compose/with_env.sh python -m scripts.demo_reset --seed    # only add what is missing
```

(On a host that sets every engine variable on the container itself, `python -m
scripts.demo_reset` alone.) A reset run this way does not hold the sample traffic's
opening burst: that belongs to the engine process, and its next tick fills the reports
again within one interval.

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
| `DEMO_VOICE_RECORDING` | off | `true` keeps the audio of calls with the portal's own agents until the nightly reset. Anyone visiting the demo can play it. |
| `DEMO_SHOWCASE` | off | The product-page showcase (four use-case agents and `/showcase/...`). |
| `DEMO_SHOWCASE_ORIGINS` | (none) | Comma-separated origins of the product pages that may chat and call. |
| `DEMO_SHOWCASE_PAGE_URL` | (none) | The product page; the portal's banner and the use-case agents link to it. |
| `DEMO_SHOWCASE_DAILY_TURNS` | 500 | Showcase chat turns per UTC day, every visitor together. |
| `DEMO_SHOWCASE_CHAT_PER_HOUR` | 30 | Showcase chat turns per visitor per hour. |
| `DEMO_SHOWCASE_VOICE` | on | Showcase calls (needs `DEMO_VOICE`); `off` keeps chat only. |
| `DEMO_SHOWCASE_VOICE_DAILY_SECONDS` | 2700 | Seconds of showcase calls per UTC day. |
| `DEMO_SHOWCASE_VOICE_MAX_CALL_SECONDS` | 150 | The longest one showcase call may last (30 at least). |
| `DEMO_SHOWCASE_VOICE_MAX_CALLS` | 3 | Showcase calls open at the same time. |
| `DEMO_SHOWCASE_VOICE_CALLS_PER_HOUR` | 4 | Showcase calls per visitor per hour. |
| `DEMO_RESET_HOUR_UTC` | 3 | The hour of the nightly reset, or `off` when an outside scheduler runs `scripts.demo_reset`. |
| `DEMO_WARMUP` | on | The demo's own test round after a seed or a reset: every agent's suite and a model comparison. `off` skips it. |
| `DEMO_SAMPLE_TRAFFIC` | on | The demo's own conversations around the clock (see "Sample traffic"). `off` stops them. |
| `DEMO_SAMPLE_DAILY_TURNS` | 200 | The most turns a UTC day the sample traffic may hold. Each is also one of `DEMO_DAILY_TURNS`. |
| `DEMO_SAMPLE_STOP_AT_PERCENT` | 60 | The sample traffic stops for the day once this share of `DEMO_DAILY_TURNS` is used, so the rest is kept for visitors. |
| `DEMO_SAMPLE_MIN_GAP_SEC`, `DEMO_SAMPLE_MAX_GAP_SEC` | 1200, 2400 | The wait between two ticks in support hours (twice that outside them). The minimum is also the slot one process may claim. Small values are for a rehearsal only. |
| `DEMO_SAMPLE_BURST` | 8 | Conversations held at once after a new install's first start and after each reset. 0: none. |
| `DEMO_SAMPLE_TURN_DELAY_SEC` | 20 | The longest pause between two messages of one conversation. |
| `DEMO_SAMPLE_UTC_OFFSET` | 0 | Northwind's clock, in hours from UTC: its support hours (08:00 to 20:00) are when the traffic is denser. |
| `DEMO_SAMPLE_KEEP_DAYS` | 30 | Days the nightly reset keeps the demo's own sample conversations, so the 7 and 30-day views have data. 0: delete everything every night. Needs a trace store. |
| `DEMO_INSIGHTS_DAILY_CALLS` | 100 | Model calls a day for "What happened in conversations", sample conversations only. Each is also one of `DEMO_DAILY_TURNS`. 0 switches the sweep off. |
| `DEMO_INSIGHTS_IDLE_MINUTES` | 30 | How long a conversation must be quiet before it is read (1 at least). |
| `DEMO_INSIGHTS_INTERVAL_SEC` | 300 | Seconds between two sweeps (20 at least). |
| `DEMO_POSTHOG_KEY` | (none) | A PostHog project API key. Set, the demo sends anonymous usage events (see "Usage analytics"). Blank: nothing is loaded or sent. |
| `DEMO_POSTHOG_HOST` | `https://us.i.posthog.com` | Where browsers send those events: the PostHog cloud host of your project's region (`https://eu.i.posthog.com` for EU), or a proxy in front of it. |
| `DEMO_TOUR` | on | The guided tour (above). `false` switches it off; outside demo mode there is none. |

Demo mode also sets, in its own process: `ENABLE_GENERIC_AGENTS=1`,
`CONVERSATION_INSIGHTS=1` under the demo's own rules and daily budget (0 when
`DEMO_INSIGHTS_DAILY_CALLS=0`), `TOOL_ERROR_EVENTS=1` (a tool server's error result is
recorded as a failed action; off by default on a normal install),
`VOICE_RECORDING_ENABLED=0` (a visitor's voice is not kept, unless `DEMO_VOICE_RECORDING=true`), `EMBED_PROVIDER=azure`
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

Voice calls (the Support desk's **Call** button and the Build with AI microphone) are
off unless you turn them on. `deploy/compose/demo.yml` passes every `DEMO_VOICE*`
setting in the table above through to the engine, empty when unset (an empty value
takes the default, and an empty key counts as no key). With an Azure Voice Live
resource:

```bash
export DEMO_VOICE=true DEMO_VOICE_PROVIDER=azure_voice_live
export DEMO_VOICE_ENDPOINT=https://<resource>.services.ai.azure.com
export DEMO_VOICE_API_KEY=<key>
docker compose -f docker-compose.yml -f deploy/compose/demo.yml up -d
```

The caps (`DEMO_VOICE_DAILY_SECONDS`, `DEMO_VOICE_MAX_CALL_SECONDS`,
`DEMO_VOICE_MAX_CALLS`, `DEMO_VOICE_CALLS_PER_HOUR`) are read the same way. A changed
voice setting needs `up -d` again, which recreates the engine.

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

### Tracing (optional): Overview and Conversations with real numbers

Without a tracing store the demo says "Tracing is off" on Overview and lists
conversations from the database. To give it one, add three containers to the same host
and set five variables on the `setup` step. Nothing else changes, no port is published
and there is no browser step: the setup step makes the store's secrets and the engine
stores its own ingest key before it starts.

On `setup`: `STACK_TRACING=1`, `STACK_LMNR_URL=http://127.0.0.1`,
`STACK_LMNR_HTTP_PORT=8000`, `STACK_LMNR_GRPC_PORT=8001`,
`STACK_CLICKHOUSE_URL=http://127.0.0.1:8123`, and
`STACK_CLICKHOUSE_MEMORY_MB` (about three quarters of the ClickHouse container's
memory: ClickHouse sizes itself from the host's memory unless told). It then also writes
`tracing.env` and `clickhouse/*.xml` into the state volume, puts the ingest key and both
addresses in `engine.env` in place of `LMNR_DISABLED`, and adds `CREATE DATABASE lmnr`
to `db-init.sql`: Laminar's own tables live in a second database on the same Postgres.

**7. `clickhouse`** (the store every Overview and Conversations screen reads)

- Image: `clickhouse/clickhouse-server:26.5.6.64-alpine`.
- Command: `bash -c 'cp /run/engine/clickhouse/users.xml /etc/clickhouse-server/users.d/lmnr.xml && cp /run/engine/clickhouse/memory.xml /run/engine/clickhouse/system-logs.xml /etc/clickhouse-server/config.d/ && set -a && . /run/engine/tracing.env && set +a && exec /entrypoint.sh'`
- Volumes: `state` (read-only), and an empty directory at `/var/lib/clickhouse`.
- Size: 0.5 vCPU and 1 GB (about 230 MB in use with the ceiling set, 700 MB without).

**8. `lmnr-app`** (Laminar's ingest server: the engine exports spans to it)

- Image: `ghcr.io/lmnr-ai/app-server:v0.2.1`.
- Command: `sh -c 'set -a && . /run/engine/tracing.env && set +a && cd /app-server && exec ./app-server'`
- Env: `PORT=8000`, `GRPC_PORT=8001`, `ENVIRONMENT=LITE`. It also listens on 8002.
- Volume: `state` (read-only). It exits until the database is up: restart policy Always.
- Size: 0.25 vCPU and 0.3 GB (it uses about 60 MB). It logs "Failed to publish spans for Quickwit indexing"
  on every batch: the search index and the query engine of Laminar's own UI are left
  out on purpose, and the spans are stored all the same.

**9. `lmnr-migrate`** (Laminar's frontend image, used only as the migration runner)

- Image: `ghcr.io/lmnr-ai/frontend:v0.2.1`. It creates Laminar's Postgres tables,
  downloads a public model price list (it needs outbound internet for that) and applies
  the ClickHouse schema, which takes a few minutes.
- Command: start `node server.js`, wait for the line `ClickHouse schema applied
  successfully`, stop the server, create the ClickHouse table `engine_schema_ready`,
  then idle (`tail -f /dev/null`) so the restart policy does not run it again.
  The engine waits for that table (`deploy/compose/tracing_init.py`, up to
  `TRACING_WAIT_SEC`, default 900), stores its ingest key and only then starts, so the
  first conversations are traced. If the store never comes up the engine starts
  anyway and says so in its log.
- Env: `PORT=5667`, `HOSTNAME=127.0.0.1`, `NEXTAUTH_URL` and `NEXT_PUBLIC_URL`
  `http://127.0.0.1:5667`, `BACKEND_URL=http://127.0.0.1:8000`,
  `BACKEND_RT_URL=http://127.0.0.1:8002`, `ENVIRONMENT=LITE`.
- Laminar's own UI has no login in this mode. That is why it listens on loopback only
  and is stopped as soon as the schema is applied. Never publish 5667, 8000 or 8123.
- Size: 0.1 vCPU and 0.5 GB (up to 300 MB while it migrates, a few MB after; the
  migrations wait on ClickHouse, they do not need CPU).

Use Laminar **v0.2.1** with this engine (the release this was run on). The portal reads
the tables `spans`, `traces_replacing` and `deduped_content`, and other releases do not
fill all three:

| Laminar | What the portal shows |
|---|---|
| v0.1.46 and earlier | Numbers and steps, but no message content: there is no `deduped_content` table, so a conversation shows the agent's replies without the customer's messages |
| v0.2.1 | Everything (v0.2.0 has the same tables going by its migrations; not run) |
| v0.2.2 and later | Nothing: traces are written to `traces_agg` / `traces_static` and message content to `unique_content`; `traces_replacing` stays empty, so Overview and Conversations read zero |

Tokens and cost. One model call is exported as two spans on OpenAI and Azure OpenAI
models (the LangChain wrapper's and the SDK's own), and Laminar's per-trace totals add
both. The portal counts each call once when it reads (`core/llm_calls.py`), so Overview,
the technical tiles and a conversation's detail show what the provider reported; nothing
has to be changed in the store, and rows written before this are read correctly too.
Cost is priced by Laminar from the model name the provider reports, with the price list
`lmnr-migrate` downloads: on 2026-11-24 it held Azure's list prices for both demo models
(`gpt-5.4-mini` $0.75 / $4.50 and `gpt-5.5` $5.00 / $30.00 per million input / output
tokens, Global Standard). The demo's model rows carry the same rates
(`demo_mode/prices.py`, with the source and date) for the screens that price from the
row: Settings › Models and test runs. A deployment under another name gets no rate
there rather than a guess.

What tracing stores, and who sees it: every chat turn's model input and output
(the agent's instructions included), each tool call's arguments and result, token
counts and timings. Every visitor of the demo can read all of it on Conversations,
the same way they can already read every saved conversation and every agent's
instructions. The nightly reset empties the trace store together with the database
(`demo_mode/trace_store.py`). To keep message content out of the store, set
`LMNR_TRACE_CONTENT=false` on the engine: the numbers stay, the texts are not exported.


#### What the nightly reset keeps (`DEMO_SAMPLE_KEEP_DAYS`)

Emptying the whole trace store every night meant "Last 7 days", "Last 30 days", the
trends and every "compared with the previous period" figure only ever held data since
03:00. The demo's own sample conversations (above, "Sample traffic") are scripted lines
answered by the seeded agents, with nothing a visitor wrote in them, so the reset keeps
those for `DEMO_SAMPLE_KEEP_DAYS` days (default 30) and deletes everything else exactly
as before. `DEMO_SAMPLE_KEEP_DAYS=0` is the old behaviour: nothing is kept. It is read
by the engine; `aci_spec_tracing.py` passes it through when it is set. Only with a trace
store: without one the reset deletes every conversation, as it always did.

The rule is "delete unless positively identified" (`demo_mode/retention.py`). A
conversation is kept only when all of this holds, and it is checked again every night:

1. **Every turn carries the generator's own mark.** The generator sends, on its
   in-process requests only, a secret made when the engine process started (never
   stored, logged or sent anywhere), and the chat route records a matching one as the
   trace attribute `waj.demo.sample`. The channel is not the test: "Sample traffic" is
   resolved from the request's `Origin`, which anyone holding the demo's token can send
   from outside a browser (checked: such a chat lists under "Sample traffic", carries no
   mark and is deleted).
2. **The agents were exactly as seeded.** Visitors may edit the Northwind agents, their
   instructions and their knowledge bases, and a sample conversation held after such an
   edit could repeat a visitor's text. Before and after each conversation the generator
   compares a fingerprint of the four agents (the engine's own full snapshot of each:
   fields, model, instructions text, tools, tool servers, playbooks, hand-over targets,
   plus the pages and chunks of their knowledge bases) with the one taken right after
   the seed. A conversation held while they differ is not marked. So from a visitor's
   first edit of a seeded agent until the next reset, that day's sample conversations
   are not kept; a visitor's own new agents and knowledge bases change nothing.
3. **The saved conversation is the script and nothing else**: its customer messages are
   one script's lines in order, with no other kind of message.
4. **The trace store agrees**: every trace of that conversation is marked, there is one
   per customer message, all inside the window. A visitor who posts into a sample
   conversation adds an unmarked trace and a line that is not in the script, and the
   whole conversation is deleted.

Deleted every night, as before: visitors' chats (the portal's test chat, the API, a
forged "Sample traffic" origin), the builder assistant's conversations and its test
chats, test runs with their cases and judge calls, voice calls and recordings, the
insights sweep's own model calls, memories, the activity log, and every agent, knowledge
base and tool a visitor made or changed. Sample conversations older than the window go
too. If the trace store does not answer, a delete does not pass its check or a table
cannot be read, nothing is kept that night.

Where the kept rows are: ClickHouse `spans` and `traces_replacing` (rows of kept traces
only) and `deduped_content` (message text is stored once per hash and shared between
spans, so after the spans are deleted every text no remaining span refers to is deleted:
what stays is exactly the kept conversations' text); Laminar's `traces` rows of the same
traces; and in the engine's database the kept conversations' `ai_chat_sessions`,
`ai_chat_messages`, `agent_traces` and `ai_conversation_insights` rows. ClickHouse
deletes are mutations that normally finish in the background: each is run with
`mutations_sync = 2` (it returns when it is done), and the reset then asks the store
whether any row outside the kept traces is left or any mutation is still running. If so
it empties every table instead. The engine's log has one line with the row counts before
and after (`demo reset: trace store: kept N sample trace(s) ...`).

Size. Measured on a rehearsal (2026-11-25): a sample turn is about 6 spans and takes
about 20 KB uncompressed in ClickHouse (10 KB on disk) and under 1 KB in each Postgres
database. Thirty days at the default pace (about 80 turns a day) is about 50 MB
uncompressed; at the full budget (200 a day) about 120 MB, and under 10 MB in Postgres.
The nightly check reads one row per trace (6,000 at the full budget). That is well
inside a 1 GB ClickHouse container with the memory ceiling above.

A new deployment still starts empty. The volumes here are empty directories that live
as long as the container group, so deleting and creating the group (every new image, on
Azure Container Instances) loses the kept days and the reports fill again from that
moment. Keeping them across deployments needs storage that outlives the group. On Azure
Container Instances the only kind is an Azure Files share mounted over SMB, and neither
store is meant to run on one: Postgres needs to own and `chmod` its data directory and
ClickHouse's MergeTree uses hard links, which SMB shares do not give (not tried here).
The options that would work are a host with real disks (a VM or a managed-disk volume
for `pgdata` and `chdata`), or an export of the kept rows to a share before a deployment
and an import after it. Neither is built.

What the agents are compared with is recorded by a reset, and by the first start of a
new install. An engine started on a database that already holds the demo but no such
record (an image from before this change, restarted in place) keeps nothing until its
first reset has run.
