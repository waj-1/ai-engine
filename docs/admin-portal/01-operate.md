# Operate

**Docs › Operate**

Watch agents in production: the **Overview** (outcomes per period, needs
attention, technical details) and **Conversations** (one row per conversation +
a chat-style viewer), plus the telemetry self-test. All routes require a
superuser session.

**Environment default.** Every Operate screen — Overview, Conversations (page and
JSON panes) and Telemetry check — resolves the environment with
`router.py:current_env_live`, so with no `?env=` and no `waj_admin_env` cookie they
all open on **production** (real customer traffic). Config screens keep the
**staging** default (`current_env`) because edits are meant to land on staging and
be promoted. Before the 2026-10 revamp Overview used one dependency and
Conversations/Diagnose the other, so one session showed PRODUCTION on the Overview
and silently switched to STAGING on Conversations. An explicit selection (query
param or cookie) wins everywhere; a scoped single-environment build always uses its own env.

Running evals on a cadence against real prod traffic moved to
[Test → Evals (Production monitoring)](./03-test.md#production-monitoring); on-demand
and suite-scheduled runs stay on
[Test → Evals (Suites & Runs)](./03-test.md#suites--runs).

- [Overview](#overview)
- [Conversations](#conversations)
- [Background jobs](#background-jobs) (Settings › Diagnostics)
- [Telemetry diagnose](#telemetry-diagnose)

---

## Overview

Sidebar **Overview** · `GET /admin/live?period=24h|7d|30d` · `live.html` ·
`router.py:live_monitor()` · `admin/screens/overview.py:build_overview()` ·
plain-language helpers in `admin/screens/plain_language.py`

Answers "how are my agents doing?" for one **period** (last 24 hours / 7 days /
30 days — the segmented picker at the top drives the whole page) compared with
the period before it.

**With tracing off.** On an install with no tracing store
(`core.lmnr_clickhouse.tracing_configured()` is False: `LMNR_DISABLED` set and no
`LMNR_CLICKHOUSE_URL`) the page does not report an outage. The status strip
says *Tracing is off on this install, so live statistics are not collected.
Conversations are saved: open Conversations to read them*; the **Conversations**
tile shows the count of saved conversations (`ai_chat_sessions`) and links to
the list, and the other four tiles read *Needs tracing*. In place of the
per-agent table and **Needs attention** one card says what turning the tracing
stack on adds (per-agent numbers, the Needs attention list, response times,
token and cost charts, step-by-step detail) and links to Conversations.
**Technical details** carries the same note, and the charts' notice reads
*Tracing is off on this install*. These sections are not rebuilt on Postgres.
No ClickHouse query is sent (`core.lmnr_clickhouse.query` raises `kind="off"`
at once and records no failure). What happened in conversations and Voice
calls have their own sources and are unchanged.

1. **Status strip** — the single place the page says whether its numbers are
   live: *all agents running normally* / *N problems found* / *some statistics are
   delayed* (failure list unavailable) / *live statistics are temporarily
   unavailable — conversations are still being recorded*. Staff builds get a
   **Details** disclosure with the classified ClickHouse cause and a link to the
   Telemetry check; partner builds don't.
2. **Outcome tiles** (each with a vs-previous-period delta): **Conversations**
   (distinct `waj.session_id`, a session-less trace counts as its own
   conversation) · **Needs attention** (conversations with a failed action or an
   error event; share in points) · **Handed to a person** (a hand-off/escalation
   tool ran — `plain_language.HANDOFF_TOOL_RE`) · **Failed actions** (tool spans
   with an exception event, of all actions) · **Avg response time** (mean trace
   duration). These tiles never claim "resolved" — telemetry can't tell
   whether a customer's problem was solved; that estimate lives in the
   conversation-insights section below, labelled as one. A tile whose source failed shows "—" and
   *Unavailable right now* — never 0. When ClickHouse is down, Conversations may
   show the `ai_chat_sessions` count instead, **labelled "All environments — from
   the conversation log"** because that table has no environment column.
3. **How each agent is doing** — per agent (display name from the env's active
   profile, `agent_kind` muted underneath): conversations (+delta), needs
   attention, handed to a person, failed actions of actions, avg response, a
   sparkline (conversations per hour/day), and a link into Conversations
   pre-filtered by agent and period.
4. **Needs attention** — failing spans of the period grouped by **cause**
   (`overview.group_failures`): "*Send payment link failed — the service did not
   respond in time* ×7", with conversations affected, agents, last seen, the raw
   tool/exception under a muted technical line (staff builds), and **Open
   latest** (deep link `#trace=`). Per trace, tool failures win; a trace with no
   failing tool contributes one "the agent ran into a problem" entry, so each
   failure counts once. Top 5 shown, the rest behind *Show N more*.
5. **What happened in conversations** (`admin/screens/insights.py`) — three
   cards: **How conversations ended** (a stacked bar + counts for *Resolved*,
   *Handed to a person*, *Left before finishing*, *Unclear*, each with the
   change in points vs the previous period), **Top topics** (top 8 labels,
   merged across agents, plus *Something else*), and **Customer language**.
   With more than one agent, a per-agent table adds the outcome mix, resolved %
   and top topic; the agents table above gains a **Resolved** column. Its own
   source — the `ai_conversation_insights` table via the
   `ai_conversation_insights_rollup` SQL function — so it renders while
   ClickHouse is down. Unreadable → *unavailable — unknown, not zero*; nothing
   analysed yet → **"Collecting data — appears after new conversations"** (never
   hidden). Partner builds see only their own agents.

   **How rows get there.** `core/conversation_insights.py` runs a background
   sweep (on the control-plane — `ENABLE_ADMIN_MCP=1` — unless
   `CONVERSATION_INSIGHTS=1|0` overrides; one process at a time via a lease
   row in `ai_background_leases`). Every 5 minutes it takes up to 40
   conversations that have been quiet for 30 minutes (newest first, last 30
   days — so the backfill is the same sweep walking back in batches; a full
   batch re-sweeps after a minute) and stores, per conversation:
   - **outcome** — *escalated* when a hand-off action ran (ClickHouse spans,
     the same `HANDOFF_TOOL_RE` as the tile above; `outcome_source=telemetry`),
     else the AI classifier's reading of the transcript (`llm`), else
     conservative text rules (`rule` — a thank-you → resolved, a hand-off
     sentence → escalated, otherwise unclear);
   - **topic** — one key from a small per-agent label set
     (`TOPIC_SETS`, plus *Only said hello* and *other*), from one
     `gemini-3.5-flash-lite` call per conversation (`CONVERSATION_INSIGHTS_MODEL`;
     skipped without `GOOGLE_API_KEY`, retried every 6 h once a key exists);
   - **language** — the script of the customer's own messages (`ar` / `en` /
     `mixed`; a Latin-script conversation takes the classifier's ISO code);
   - **environment** — `ai_chat_sessions.metadata.environment` (stamped by the
     writer since this change) or the trace's `waj.env`; **channel** — `voice`
     when the session has a call recording or `metadata.channel=voice`.
     Bot-driven (`simulated`) calls are stored flagged and never counted.

   Tunables: `CONVERSATION_INSIGHTS_BATCH` (40), `…_INTERVAL_SEC` (300),
   `…_IDLE_MINUTES` (30), `…_LOOKBACK_DAYS` (30), `…_CALL_GAP_SEC` (0.5). The
   lease row's `last_run_at` / `last_result` say what the last sweep did. MCP:
   `conversation_insights`.
5. **Voice calls** (only when there is voice traffic) — calls, **minutes used**
   from `ai_voice_usage_daily` (all environments; vs previous period), **today vs
   the daily allowance** (`ai_voice_budgets` or the engine default), time to first
   reply (EOT → first audio p50, seconds) and interruptions.
6. **Technical details** (collapsed `<details>`, open state remembered per
   browser) — every engineering number the dashboard had, for the period:
   tool-call success, latency P95, token spend, TTFT/TTLT p50, cache hit rate,
   cache savings, sessions (marked *all envs* when it is the `agent_traces`
   fallback), the duplex-voice stage latencies, a per-agent technical rollup
   (traces, error %, tokens, cost, p50/p95) and the **14 charts** with their
   own filter bar + CSV export. The charts fetch only when the section is first
   opened; an empty window or a dead source collapses the grid into **one**
   empty state instead of 14 "No data" boxes.

No conversations in the period → one empty state ("No conversations in the last
7 days — Test an agent →").

### Technical details — engineering reference



The rest of this section documents the engineering tiles and charts that now
sit under **Technical details**, and the environment rules shared by every
Operate screen.

**Exception — scoped builds.** On a single-environment deployment (a deployment
scope a plugin defines, see [Overview → Scoped builds](./README.md#scoped-partner-builds))
every screen follows the box's own `ENVIRONMENT` instead, and the env switcher is
hidden entirely (`core.deployment.single_env_deployment`). Such a box is not a
control-plane: it runs one engine against one AI Supabase, so the "other"
environment has zero profiles and zero traces by construction, and a prod default
would render an empty Live Monitor.

There the box's env is **authoritative, not a default**: it beats `?env=` and the
`waj_admin_env` cookie both, and no cookie is written. With the switcher hidden there
would otherwise be no way to undo a stale `prod` cookie. If you need to inspect such a
box's "other" environment, query its AI Supabase directly — the portal will not switch.

**Shows**
- **Eight KPI tiles**: sessions in last 24h · tool-call success % (target ≥95%) ·
  avg P95 latency (target ≤1500ms) · token spend in 24h (USD) · **Time-to-first-token
  p50** (ms) · **Time-to-last-token p50** (ms) · **Cache hit rate 24h** (cache-read
  tokens ÷ total input tokens) · **Cache savings 24h** (USD saved vs. the full
  input price had those tokens not been served from cache, priced with the primary
  active model's rate). The first six tiles source from ClickHouse via
  `load_live_insights` / `_live_kpis`, with TTFT/TTLT read from the ClickHouse span
  attributes `waj.ttft_ms` / `waj.ttlt_ms` (stamped by `common/sse.py` on every
  streaming request root span); the two cache tiles are aggregated separately from
  **ClickHouse** via the same `load_analytics` call that feeds the chart
  grid below, off the `gen_ai.usage.cache_read_input_tokens` span attribute (see
  [Traces — explorer](#conversations) and [Data Model](./data-model.md) for where
  that attribute comes from). All tiles are **scoped to the selected environment**
  (`waj.env`, prod strict / staging admits untagged) so switching STAGING/PRODUCTION
  changes the numbers — matching the charts below. `sessions_24h` is the env-scoped
  count of distinct `waj.session_id` from ClickHouse `traces_replacing` across **all
  agent kinds** (background kinds included — total session activity; only the diagnose
  self-test span is dropped); it falls back to a **global** count from the lean
  `agent_traces` index only when ClickHouse is unavailable (that index has no env
  column, so the fallback can't differ per env).
- **Analytics chart grid** (thirteen uPlot charts, re-rendered whenever the filter
  bar changes): volume over time · tokens in/out · cost (USD) · latency p50/p95 ·
  error-rate % · avg agentic iterations · volume by agent (stacked) · cost share
  by agent (bar) · latency histogram · top tools by call volume (bar) ·
  **Time to first/last token (ms)** (p50 and p95 series for TTFT and TTLT over the
  selected window) · **Cache: cached vs uncached tokens** (stacked volume of
  cache-read vs. non-cached input tokens over the window) · **Cache hit rate by
  agent** (%, per agent kind). A notice banner replaces the grid when the
  analytics source is unavailable.
- **Duplex voice card** (last 24h): stage latencies for the realtime voice edge
  (`WS /voice/live`) — voice sessions · assistant turns · **end-of-turn → first
  audio** p50/p95 (responsiveness; targets ≤800 / ≤1500ms) · **answer latency**
  p50/p95 (user question → first spoken `ask_agent` result) · **delegated turn**
  p50/p95 (the brain's `run_turn` wall time) · **Brain LLM p50** (the LLM
  share of the delegated turn, `waj.voice.live.stage.llm_ms` off the
  `voice.agent_turn` root span — read it against the delegated-turn tile: the
  gap is history/memory/prefetch/tool time plus inter-round bookkeeping,
  broken down per-trace by the other `stage.*` attributes in the span detail
  pane. The llm stage is filled by brains that time their own model calls
  (the `custom` agent and the `/agents` demo agents do), so a window whose
  calls were served by a brain that does not shows "—" on this tile: expected,
  not breakage. The number is a slight upper bound on model time: its timed
  window includes the delegate's per-sentence rendering and WS sends, ~tens
  of ms) · `ask_agent` call count ·
  barge-ins. Sourced from the `waj.voice.live.*` **span attributes** the voice
  edge emits per turn / tool call / session (`voice/live/metrics.py` — point
  spans `voice.live.turn` / `voice.live.tool` / `voice.live.session`, plus the
  delegate's `voice.agent_turn` root span for the stage attributes),
  aggregated env-scoped in ClickHouse by
  `observability.py:load_voice_live_insights()`. Stamps are server-side (they
  exclude browser playback), and Gemini-provider turns carry no EOT metric
  (no end-of-turn signal in its API). Renders a notice when ClickHouse is down
  and a hint when there's no voice traffic in the window. These point spans are
  excluded from the generic latency tile / trace rollups above so their ~0ms
  span durations can't skew them.
- **Active profiles** table: agent kind, display name, endpoint, tool / data-source /
  sub-agent counts, last updated, status chip — scoped to the active env. Each row
  carries **per-agent insight columns** (volume, error-rate %, tokens in/out, cost,
  p50, p95) from a 24h ClickHouse rollup keyed by agent kind.
- **Recent error traces**: the last few errored traces in the window, deep-linked
  into the [Traces explorer](#conversations) (from
  `load_live_insights`; graceful "unavailable" notice when ClickHouse is down).
  A trace counts as an error iff **any of its spans carries an OTel
  `exception`/`error` event** — aggregated per trace in the trace-list SQL.
  The `traces_replacing.status` column is `''` on every row the live stack
  writes (like `spans.status`), so reading it rendered this card as a
  permanent "No error traces in this window." even while the chart grid's
  Err% (which always derived from events) showed errors on the same page.
  When the *recent-trace* read specifically fails, the card says
  **"Error traces are unavailable"** — it never shows the affirmative
  "No error traces in this window." off a query that did not run.

**Filter bar** — four controls that re-fetch and redraw all charts client-side
without a page reload: **time range** (1h / 6h / 24h / 7d / 30d), **agent kind**,
**channel**, **status** (all / success / error).

**Data endpoint** — `GET /admin/live/analytics.json` (superuser-gated; params
`hours`, `agent_kind`, `channel`, `status`, `environment` — the charts pass the
Overview's env via `data-env`) returns the full aggregated payload
(time-series, breakdowns, per-agent rollups, totals). Aggregation runs as
read-only SQL against **ClickHouse** (`core/lmnr_clickhouse.py` →
`admin/screens/live_analytics.py:load_analytics`): `traces_replacing FINAL`
joined to `spans` for `waj.agent_kind`/`waj.channel.slug`, error-rate from OTel
`exception` events, agentic iterations from LLM-span counts, cost from the
precomputed `total_cost`, TTFT/TTLT p50/p95 from the `waj.ttft_ms` /
`waj.ttlt_ms` span attributes via `simpleJSONExtractString`, and cache-read tokens
(cache hit rate, cache savings, the two cache charts) from
`gen_ai.usage.cache_read_input_tokens` on LLM spans (`span_type = 1`). Best-effort —
`ok:false` + a notice when ClickHouse is unreachable, so the page never errors.

**Actions** — apply filter-bar controls to narrow the charts · click a profile row
to open it in [Build → Agents](./02-configure.md#agent-profiles) · open
any error trace in the [Traces explorer](#conversations).

**Backing data** — `agent_profiles` (active profiles table); **ClickHouse**
(`traces_replacing`, `spans`) for everything else: the KPI tiles + recent error
traces (`load_live_insights`) and the chart grid + per-agent insight columns
(`load_analytics`).

`load_live_insights` issues five independent reads — the 1h trace rollup, the 24h
trace rollup, TTFT/TTLT percentiles, the distinct-session count, and the
tool-volume aggregate — and each degrades on its own. That matters: it used to
bail out before the TTFT and session queries whenever the trace-list read failed,
so five tiles showed "—" while their numbers were sitting in ClickHouse.

**Reading a partial outage.** `lmnr_ok` is an OR across those five reads — one
hiccup must not blank the whole page — so it stays `true` while a single read is
dead. `load_live_insights` therefore also returns **`sources`** (one bool per
read: `traces_recent`, `traces_24h`, `sessions`, `ttft`, `top_tools`) plus
`degraded`, and everything rendering off it consults them:

- A tile whose source failed shows **—**, never `0`. The one that bit hardest was
  **Token spend 24h**: with the 1000-row 24h read timed out the token sums are 0
  and price to a confident `$0.00`, indistinguishable from an idle day.
- A warning banner above the tiles **names the stale reads** ("Telemetry
  PARTIALLY unavailable — … 24h traces (token spend)"), so a half-real page is
  never silent.
- **The banner names the cause.** Every ClickHouse failure is classified in
  `core/lmnr_clickhouse.py` as *unreachable* (network/timeout — check
  `LMNR_CLICKHOUSE_URL` and the `lmnr-server` VM), *auth* (login refused —
  check `LMNR_CLICKHOUSE_USER` / `LMNR_CLICKHOUSE_PASSWORD`) or *rejected*
  (ClickHouse is up and refused the SQL — a query bug, not connectivity), and
  the banner quotes the ClickHouse error. Before 2026-09-29 every failure read
  "could not be reached", and a month of rejected trace-list SQL (`Code: 10`)
  was chased as a network problem.
- Extending this is one entry in `_LIVE_SOURCE_LABELS`
  (`admin/screens/observability.py`) plus a `_src_ok(...)` guard on the tile in
  `admin/router.py:_live_kpis`.

## Background jobs

Settings › Diagnostics › **Background jobs** · `GET /admin/jobs` · `jobs.html` (+ `_tab_jobs.html`) · `jobs_handlers.py:jobs_list()` / `build_jobs_context()`

Moved off the Overview in the 2026-10 revamp (background admin work is not
monitoring). `GET /admin/live?tab=jobs` 307-redirects here with filters carried over.

A tracker for fire-and-forget admin work — eval runs, KB ingestion, deploy dispatches,
env promotions, tool dry-runs.

**Shows** — filters (kind, status, limit); a newest-first table (started, kind chip,
label, status chip, duration, actor, truncated error, actions). While any job is in
flight `jobs-poll.js` watches the in-flight `admin_jobs` rows via
`GET /admin/jobs/{job_id}.json` and reloads the page **once** when one settles — this
replaced the old `<meta http-equiv="refresh">` that yanked the operator to the top of
the page every 5s. Click a row to expand full job id, timestamps, full error and raw
metadata.

**Actions** — filter / clear · **Cancel** a queued or running job
(`POST /admin/jobs/{job_id}/cancel`); the worker stops at its next checkpoint
(cooperative cancellation — already-finished work is preserved).

**Backing table** — `admin_jobs`.

> The `/admin/jobs/{id}.json` and `…/cancel` sub-routes are unchanged.

---

## Conversations

Sidebar **Conversations** · `GET /admin/observability` (same page:
`/admin/observability/explorer`) · `observability_explorer.html` ·
`observability_explorer_handlers.py:render_conversations()` ·
`admin/screens/conversations.py:build_conversations_context()`

Answers "what happened in this conversation?" and "what went wrong?".

**Filters** (one row above the panes): period, agent (display names), channel,
**outcome** (`?outcome=attention|handoff|ok`), search. **More filters** (collapsed):
action used (tool), technical status (`?status=success|error`, kept for old links),
*One row per agent reply* (`?group=none`, the old flat trace list) and *Include
background activity* (`?background=1`). **Export CSV** exports the visible rows.

**List — one row per conversation** (`group_traces_by_session` +
`conversations.enrich_groups`): agent display name · channel (or *Voice call*) ·
time (*Today 09:14*, UTC) · business name (main-project `business`, via the
session's `business_id`) · the customer's **first message** (`ai_chat_messages`)
or the session title · message count · failed actions · an **outcome chip**
(*Needs attention* / *Handed to a person* / *No problems*) · *Voice m:ss* and
*Test call* chips. Outcome counts come from the trace-list query's per-trace
`tool_calls` / `tool_errors` / `handoffs` aggregates. A turn slower than
`SLOW_TURN_SEC` (60 s) also makes its conversation **Needs attention** (the eval
runner and most callers give up at 120 s), and the viewer's failure card says how
long it took. Every agent endpoint opens a tagged root span (agent, channel, env,
session); an endpoint that does not shows up as an untagged `LangGraph.workflow`
row. Enrichment is best-effort —
a failed lookup falls back to plainer text, never hides a row.

**Viewer — two panes by default** (list ~380px + conversation). Header: business
or first message, agent · channel · started · duration · messages, outcome chip,
**Copy link**. Tabs:

- **Conversation** — chat bubbles (customer right, agent left, `09:14`
  timestamps, markdown). **Action chips** sit above the reply they fed, collapsed
  by default ("✓ Look up balance", "✕ Send payment link failed", "→ hand-off");
  expanding shows a plain sentence and a nested *Show technical details* with the
  raw tool name, error and sent/received JSON. Actions of **every turn** of the
  conversation are shown (`observability._session_tool_spans`, ±2 days around the
  selected trace, attached to the first reply persisted after each call);
  persisted `tool_calls` metadata still wins. When anything failed a red
  **summary card** at the top states it in words, quotes what the agent said
  next, and offers *Jump to message*. A completed call recording is **pinned**
  above the transcript. Token counts, cache badges, heat colours and raw model
  output are not shown here.
- **Call audio** — every recording of the conversation (unchanged loader).
- **Technical** — the old Details (trace metrics, linked chat session, tools and
  error steps), the **Steps** waterfall + **Step detail** (former middle/right
  panes) and per-reply token counts / raw model output.

**Not found vs unavailable.** Every pane payload carries `found` /
`unavailable` (`observability._trace_found`): a bad or expired link reads *"We
couldn't find this conversation"* (and a malformed id is never reported as an
outage, even though ClickHouse rejects it); a ClickHouse failure reads
*"Conversation details are temporarily unavailable"*. The Details payload's
`trace` is `null` when not found — never a zero-filled record.

**Responsive.** Below 1280px the list and the conversation share the width: the
list shows until a conversation is opened, then *← All conversations* returns.

When the list itself can't be read, one status strip says *Conversations are
temporarily unavailable — they are still being recorded* (staff Details show the
ClickHouse cause). The engineering reference below still applies to the list
query, filters and JSON endpoints.

### With tracing off: the saved chats

`admin/screens/saved_chats.py` · used only when
`core.lmnr_clickhouse.tracing_configured()` is False (`LMNR_DISABLED` set and no
`LMNR_CLICKHOUSE_URL`: the local compose stack). With a tracing store configured
nothing below runs and the screen is exactly as described above.

Every conversation is saved in Postgres whether or not tracing runs, so the
screen is built from those rows instead of reporting an outage:

- **List** (`saved_chats.build_list_context`) — one row per `ai_chat_sessions`
  row that was created or active in the period (soft-deleted ones excluded),
  newest activity first, 100 at most: the agent (for a generic agent, the
  display name of the agent whose slug the session saved as
  `metadata.agent_slug`), the channel where the session recorded one
  (`metadata.channel_slug`), else where it came from (`metadata.source`, e.g.
  *API*), started / last activity, the message count (counted from
  `ai_chat_messages`; the stored `message_count` column has no writer), the
  customer's first message, and an outcome chip: *Needs attention* when a
  reply failed (`agent_traces.status = 'error'`), else *No problems*.
- **Filters** — period, agent, channel (only sessions that recorded one match),
  outcome, and **Search**, which looks in message text and the session title
  (case-insensitive, the words taken literally). **More filters** is hidden:
  action used, technical status, one row per reply and background activity are
  questions only the tracing store can answer.
- **A note, not a banner** — a neutral strip above the list: *Tracing is off on
  this install, so this list comes from the saved conversations. Step-by-step
  detail (timings, each model call) needs the tracing stack.*
- **Opening one** — the id in the URL hash and in the pane endpoints is the
  chat **session id** (`#trace=<session id>`); `observability_explorer_handlers`
  sends each pane to the saved-chats loader. **Conversation**: the messages in
  order (`observability._persisted_conversation`, the function the
  tracing-backed transcript reads). An action chip appears where the reply's
  metadata carries tool calls; a reply saved by `common.chat_session.run_turn`
  carries the NAMES of the tools it ran (`metadata.tools_used`), shown as a
  neutral chip that says the result and the sent/received data need tracing.
  **Technical**: totals for the whole conversation summed from its
  `agent_traces` rows (tokens in/out, cache, cost, total response time, replies,
  failed replies, model) and, under *Per reply*, each reply's tokens, response
  time and model (the turn rows paired with the replies in order; a failed
  turn's row is skipped because it wrote no reply). **Steps** shows one plain
  sentence: step-by-step detail is recorded by the tracing stack, which is off.
  **Call audio** lists the session's recordings as before.
- **Access.** The same superuser gate, and the same content: the transcript is
  the saved messages and nothing else, for every agent kind. A kind whose
  telemetry is exported redacted (`core/sensitive_content.py`) is shown by the
  tracing-backed screen from these same rows, so nothing appears here that
  does not appear there.
- `/admin/sessions/{id}` and `/admin/observability/sessions/{id}` open the saved
  chat directly.

### Engineering reference — list query and panes



Sidebar **Conversations** (renamed from Traces) · `GET /admin/observability` · `observability_explorer.html` · `observability_explorer_handlers.py`

A **3-pane trace browser** (the "Traces" destination in the nav), rendered
entirely from ClickHouse. Pane content hydrates from JSON endpoints via
client-side hash routing (`#trace=…&span=…`), so a URL deep-links to an exact
span.

**Left pane — trace list**
- Filters: time range (1h/24h/7d/30d), status (any/success/error), channel, agent
  kind, tool, and free-text search. The **status filter (and the per-row status
  dot, the session-group error dot, and the CSV export's status column) is
  events-derived**: a trace is an error iff any of its spans carries an OTel
  `exception`/`error` event, aggregated per trace inside the list query — the
  `traces_replacing.status` column is `''` on every live row, so filtering on it
  made Error return nothing and Success return everything.
- **The list is scoped to the selected environment and source-filtered in
  ClickHouse** (`traces_replacing`), not narrowed client-side: it returns the recent
  traces whose `waj.env` matches the env switcher (env-less traces are admitted too,
  never silently hidden) with the **background / non-conversational kinds excluded**.
  This is what keeps a high-volume background workload (a plugin's one-shot,
  non-chat agent kind, for example) from flooding the newest-first page and burying
  genuine chat sessions. Untagged bare-LLM traces are excluded by top-span signature
  as well. **Voice-live metric point spans** (`voice.live.*`, emitted by the duplex
  voice edge) are excluded the same way: they are instantaneous ~0ms markers whose
  measurements live in span *attributes* (rolled up by the Dashboard's Duplex-voice
  card), so as trace rows they'd bury real chats under sub-millisecond entries after
  any voice session. Tick **Show background** to include them, or pick a background
  kind in the agent filter to see only those. Since 2026-10-14 the same background class
  also covers (one shared list, `observability.background_trace_clauses()`, used by
  the Overview rollups too): **untagged** traces whose top span is the admin MCP's
  own tool call (`ResponseStreamWriter`) or a bare LLM / embedding call
  (`gemini.generate_content[_stream]`, `ChatGoogleGenerativeAI.chat`,
  `openai.embeddings`), and **test traffic**: the eval runner's `eval_suite::*`
  traces and every agent request carrying `waj.eval.run_id`. Before that, 62 of
  the newest 100 prod rows were MCP calls, all shown as *Unlabelled agent*. Test
  traffic shown with Show background gets the *Test call* chip; each eval case
  also links to its trace from the [run detail](./03-test.md#eval-run-detail).
  The **diagnose self-test** span
  (`lmnr_diagnose_test_span`, emitted by [Telemetry diagnose](#telemetry-diagnose)) is
  excluded **unconditionally** — even with Show background on — so those empty
  self-test traces never clutter the list. Free-text search is pushed down too
  (top span name, metadata, session id, trace id). There is no second source to
  fall back on: when ClickHouse is unreachable the list is empty **with a
  notice**, never silently.
- Newest-first rows: agent-kind chip, timestamp, status dot, top span name, channel
  chip, duration · token count. The **duration and token values are heat-shaded
  green → red** relative to the heaviest trace in the current window, so the slowest
  / most expensive traces stand out at a glance.
- **Group by session** (the default view) — buckets the list into one collapsible
  group per chat session (a conversation), since each trace is a single turn.
  **Clicking a group header opens the conversation's Transcript** (via its newest
  turn); the **caret** alone expands/collapses the turn list. The header shows the
  session id, agent chip, turn count, error dot, latest timestamp, and summed
  duration/tokens; the first three groups render expanded. Conversational traces
  without a session id (legacy turns) stay visible as standalone rows. Untick
  the toggle for the flat newest-first list. Grouping reads `waj.session_id`
  off the trace metadata, so it adds no extra telemetry calls and composes with
  all other filters.

**Middle pane — span tree**
- Hierarchical spans for the selected trace, each with a duration bar coloured by
  type (LLM / tool / error) and a loading skeleton while fetching.
- Every span row shows its **wall-clock time** and (for LLM steps) **token in/out**
  as text on the right. When the telemetry backend omits a span's duration it is
  derived from the span's start/end timestamps, so no row reads "—". Both the time
  and token values are **heat-shaded green → red** relative to the heaviest span in
  the trace, surfacing the slowest step and biggest token sink.

**Right pane — detail (four tabs: Transcript · Spans · Details · Audio)**
- **Transcript tab** *(default)* — the conversation. The user/assistant turns come
  from the **persisted `ai_chat_messages`** for the trace's linked chat session (the
  reliable record; this is why the standalone Sessions page was retired), and this
  trace's **tool calls** + the raw **model output** are merged in from the spans and
  attached to the assistant turn (each in a collapsible block). The conversation is
  read from the env **the trace was recorded in** (`waj.env` on its root span) —
  not the portal's selected env — so a prod trace shows its full prod conversation
  even while browsing with the staging env cookie. The trace carries the **raw**
  `waj.session_id`, but `ai_chat_messages` is keyed on the **coerced** id
  (`chat_persistence.coerce_session_id` maps non-UUID client ids → a deterministic
  UUIDv5); the reader applies the same coercion, so conversational agents with
  non-UUID session ids resolve their persisted transcript instead of falling back. A
  plugin can render a structured result kept in a message's metadata into the bubble
  (the `portal_display` hook's `message_markdown`). Falls back to
  a span-reconstructed transcript (with a banner) when no chat session is linked — the
  banner is **informational for one-shot / background kinds** (voice, and any kind a
  plugin lists in `portal_display.non_chat_agent_kinds()`: "no chat transcript by design") and diagnostic for conversational
  agents. Message content is rendered as clean text — Gemini `parts[]` / provider
  message shapes are flattened, not dumped as raw JSON. Each assistant turn's **token
  count is heat-shaded green → red** relative to the heaviest turn in the conversation,
  and an assistant turn with any cache-read tokens is badged **`⚡ N cached`** next to
  its token count.
- **Spans tab** — span header (name, time, duration, model), token stats (including
  a per-step **Cache read** count for LLM spans), expandable tool I/O or LLM I/O,
  events list, and an attributes table grouped by prefix (`gen_ai.tool.*`,
  `gen_ai.*`, `lmnr.*`, other) with show-full toggles. Selecting a span in the
  middle pane switches here automatically.
- **Details tab** — trace metrics (status, duration, tokens in/out, cost, tool-call
  count, **Cache read / Cache write / Cache hit** — cache-read and cache-creation
  tokens summed across this trace's LLM spans, and hit % of total input tokens for
  the turn), the linked chat-session metadata (agent kind, business, branch, locale,
  message count, title, session id), a per-tool calls/errors rollup, and any error
  spans. Consolidates the numbers the retired Sessions page used to show.
- **Audio tab** — **listen to the conversation's voice calls**, real and simulated
  alike. Every `/voice/live` call is captured at the websocket edge
  (`voice/live/recorder.py`, on by default via `VOICE_RECORDING_ENABLED`) and stored
  as one stereo WAV — **left channel = caller, right channel = assistant**,
  timeline-aligned (provider audio arrives far faster than realtime, so chunks are
  placed at their wall-clock arrival offsets; audio a barge-in flushed unheard is cut
  the way the caller experienced it). A voice call shows up in the list as N
  `voice.agent_turn` traces under one session; this tab lists **every recording of
  the selected trace's conversation** (one per call — each websocket connection mints
  a `call_id`, stamped on the call's spans as `waj.voice.call_id`), newest first, and
  badges the one belonging to the selected trace. **Simulated conversations stand
  out**: every bot-driven call (a text-only `?provider=simulated` run, or a
  live-mode eval/scenario run, which self-declares with `?simRun=1`) is stamped
  `waj.voice.simulated` on its spans, so its traces and session group carry a
  **`sim` chip in the trace list** and its recording a `sim` badge here — real
  caller sessions never blend with harness runs. Live-mode runs stream real audio
  and record like any call; a **text-only sim run carries no wire audio and is
  deliberately not synthesized** — its transcript is the eval record, and its
  metadata-only row renders an explanatory empty state instead of a player.
  Recordings are capped per call (`VOICE_RECORDING_MAX_SEC`, default 15
  min) and pruned after `VOICE_RECORDING_RETENTION_DAYS` (default 30) — a rolling
  window, not an archive. A call that carried no audible audio shows an explanatory
  empty state rather than a broken player. **Calls to a sensitive agent kind are
  never recorded**, whatever `VOICE_RECORDING_ENABLED` says.
- **Sensitive agent kinds are redacted.** A plugin can mark agent kinds as
  sensitive (the `sensitive_content` hook, read by `core/sensitive_content.py`; a
  build with no plugin has none). Every span of such a kind's trace is exported to
  Laminar with prompts, completions, tool arguments/results, event payloads and
  error text **stripped** — only the skeleton (span tree, timings, model, token
  usage, `waj.*` tags) is kept, and each span carries `waj.phi_redacted=true`.
  Content capture stays on for every other agent. So for such a conversation the
  Transcript tab shows the persisted chat turns (from `ai_chat_messages`) and tool
  *names* with empty I/O; span detail shows no LLM/tool I/O. That is by design, not
  a capture failure.
- **JSON rendering**: every tool/LLM I/O block is robustly pretty-printed — nested /
  doubly-serialized JSON is unwrapped and literal `\n` / `\"` escapes decoded — with
  long values wrapped and a hover **Copy** button.

**Actions** — apply filters · select trace/span (updates the URL hash) · toggle
Transcript/Spans/Details/Audio · **Copy** any block · **Export CSV** of the filtered
trace list · jump to [Telemetry diagnose](#telemetry-diagnose).

> The **tool filter** resolves in one ClickHouse query for the whole page. If that
> query fails it shows the list **unnarrowed** rather than empty — an empty result
> set would read as "no trace called this tool", which is indistinguishable from a
> real answer. Unnarrowed is only *visibly* wrong if it is announced, so the page
> also renders a warning banner naming the tool and a red
> **"tool filter NOT applied — list is unnarrowed"** marker beside the picker
> (which still shows the tool as selected). Treat that list as unfiltered:
> silently, it reads as "every trace in this window called that tool".

> Clicking a span while ClickHouse is unreachable reports the **telemetry
> unavailable** notice, not "Span not found" — the span list comes back empty in
> both cases, and calling an outage "not found" points the operator at a data-loss
> hunt during a connectivity problem while the tree beside them still shows the
> span they clicked.

**JSON endpoints** — `…/explorer/trace/{id}/spans.json`,
`…/explorer/span/{trace}/{span}.json`, `…/explorer/trace/{id}/transcript.json`,
`…/explorer/trace/{id}/overview.json`, `…/explorer/trace/{id}/audio.json`
(in-memory LRU cached; transcript + overview + audio take the current `env` so they
read that environment's persisted data). Audio bytes stream from
`…/explorer/recording/{recording_id}.wav` (superuser-gated like every explorer
endpoint; `?env=` selects the branch DB the recording was found in).

**Backing tables** — ClickHouse `traces_replacing FINAL` (per-trace aggregate) for
the env-scoped/background-filtered trace list, `spans` for the span tree and detail,
and **`deduped_content`** for LLM prompt/completion text — `spans.input` /
`spans.output` are empty for every LLM span, so the messages are recovered by
array-joining the span's `input_message_hashes` / `output_message_hashes` against
that table (tool spans keep their payload inline). Plus `agent_traces` for the lean
bridge; `ai_chat_sessions` / `ai_chat_messages` for the conversation spine and
Details; `ai_voice_recordings` / `ai_voice_recording_chunks` for the Audio tab.

> Legacy `GET /admin/observability/{trace_id}` 302-redirects into the explorer with `#trace=…`.

> **Sessions retired.** The standalone Sessions browser duplicated the Transcript
> tab. `GET /admin/observability/sessions` and `/admin/sessions` now 302-redirect to
> the explorer; `…/sessions/{id}` resolves the session to its trace and deep-links
> the Transcript (`#trace=…`).

---

## Telemetry diagnose

Settings › Diagnostics › **Telemetry check** · `GET /admin/lmnr/diagnose` · `lmnr_diagnose.html` · `router.py:lmnr_diagnose()`

A self-test that explains the common "no traces in the portal" puzzle, and it
covers **both ends** of the pipeline so the answer is never ambiguous:

- **Write side** — bootstrap status (`Laminar.initialize()`, API key, last error),
  the SDK/dashboard URLs, content-capture status, and a live test-span emit with an
  **Open it in Conversations** link (Technical view; that trace is excluded from
  the list, so the link is the only way to see it). The link is **disabled with a
  reason** while the read side is down — it used to open a trace that could not
  load.
- **Read side** — a live **ClickHouse** probe: reachable or not, the configured URL,
  and a span count for the last 24h. Every observability screen reads ClickHouse
  and nothing else, so this IS the "can the portal show me traces?" answer. A
  reachable ClickHouse holding **zero recent spans** is called out separately —
  that's a write-side failure, not a read one.

No DB writes. (This page used to report a Laminar *project id*; a project is a
web-UI concept with no ClickHouse analogue, and resolving one needed the
session-cookie API the deployed build doesn't serve.)
