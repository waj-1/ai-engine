# Test

**Docs › Test**

Prove an agent works before promoting it: LLM-judge evaluator suites (with A/B and an
optimizer), scheduled / on-demand / real-traffic eval runs, persisted profile-version
bake-offs, end-to-end and pytest runs, and a fixtures vault for test inputs.

- [Evals](#evals) — the Testing area's tabs
  - [Overview](#overview) (one health card per agent: Ready / Needs work, score vs target, change, trend)
  - [Suites & Runs](#suites--runs) (Test suites tab · Runs tab · the Run tests drawer)
    - [Suites section](#suites-section) (list · suite editor · CSV import)
    - [Suite editor](#suite-editor)
    - [Runs section](#runs-section) (run now · trend · recent runs)
    - [Variant overrides on the run trigger](#variant-overrides-on-the-run-trigger)
  - [Production monitoring](#production-monitoring) (Schedules tab: prod-chat schedules bound to a profile · indicators · trend · recent scheduled runs)
  - [Bake-offs](#bake-offs) (Model comparison tab: persisted N-variant × M-suite comparison · recommendation · matrix · charts)
  - [Display conventions](#display-conventions) (percent, points, one status vocabulary, "—" for unknown)
- [Eval run detail](#eval-run-detail)
- [Eval run compare](#eval-run-compare)
- [Tests — pytest viewer](#tests--pytest-viewer)
- [Test Fixtures](#test-fixtures)

---

## Evals

`/admin/evals` · `/admin/evals/suites` · `/admin/evals/runs` · `/admin/evals/bakeoffs` (+ `/bakeoffs/{id}`) · `/admin/evals/scheduling`

Sidebar **Testing**. The Testing section tabs (`admin/nav.py:SECTION_TABS["testing"]`,

**Generic agents and plain builds.** Where the generic agents API is on, every
agent picker here (create suite, run, model comparison, schedules) offers
generic (`custom`) agents; templates are never offered. On a build with no
product plugin the product "Agent type" lists are empty, so a suite is always
made for a chosen agent and no product suites are seeded. A run of a generic
agent's suite pins the profile under test (`X-WAJ-Profile-Id`, as the Test
panel does), so a **draft** can be tested before it is published. With no
deployment target registered, runs call the engine itself.

**Judge model.** Any model the engine can call works as a judge: a Gemini,
Claude, OpenAI, Azure OpenAI or OpenAI-compatible (base URL) row, a local
Ollama model, or the Automatic row. `admin/e2e_runner._build_judge` builds the
suite's chosen `agent_models` row through `runtime.llm_factory.build_chat_model`,
the factory agents use (Claude and Gemini rows keep their name-based builders).
Before 2026-11-23 only Gemini, Claude and Azure rows could be built, so a local
judge fell through to the Gemini builder ("no Gemini key set") and graded
nothing.

**No judge model.** A suite whose Judge model is "None — check expected words
only", or whose judge cannot be built (no key, an unknown model), grades each
case with what code can check:

- the reply contains the expected answer, or every word of it (case and
  spacing ignored) → passed;
- otherwise, a case with expected/forbidden tools, `checks` or `reply_checks`
  is graded by those, and its note says the expected answer was not judged;
- otherwise the case is **Not judged** (`ungraded: true`, `score: null`). It is
  left out of the run's score, shows a neutral **Not judged** chip with the
  reason (no judge set, or why the chosen one could not be built), the run page
  counts it in a warning callout and a **Not judged** filter, and a run where
  no case could be judged ends `errored` with "No case could be judged: …". It
  used to be failed at 0%, which read "Scored 0%, below the pass mark" on a
  correct answer worded differently.

A model comparison needs a judge only when some case needs judging: a suite
graded entirely by `checks`, tool assertions or required substrings runs
without one. The pre-flight (`admin/services/eval_scorers.suite_judge_problem`,
used by `start_suite_run` and `create_bakeoff`) refuses a judged suite with no
judge, or one whose provider the engine cannot call. A refused run started from
the Run tests / Start run panel renders a portal page ("This run can't start")
with a link back to the panel; API callers still get the JSON 400.

**Business picker.** The run panels' and schedule form's **Business** field,
and the suite's default business, appear only when a plugin names it
(`agent_catalog` hook `business_field_label()`, `admin/eval_context.py`). A
build with no plugin has no tenants and shows no such field.

**Case names.** A case's display name is its `name` (the suite page's **Name**
field, a `name` key in the case JSON, a CSV `name` column, or an MCP
`eval_suites` scenario's `name`), else its question
(`admin/eval_handlers.case_display_name`). The `id` stays a slug, made from the
name when one is given, else the question.

**Durations** on the run page, run compare and model comparison read "850 ms",
"8.4 s", "1 min 26 s" (`admin/testing_view.ms`).

rendered by `_base.html` from the request path) are:

| Tab | Route | Template | What it answers |
|---|---|---|---|
| **Overview** | `/admin/evals` | `evals_overview.html` | Is each agent good enough? |
| **Test suites** | `/admin/evals/suites` | `evals_list.html` | What do we test, and how did each suite do last time? |
| **Runs** | `/admin/evals/runs` | `evals_runs.html` (+ `_tab_eval_runs.html`) | Every run; compare two of them |
| **Model comparison** | `/admin/evals/bakeoffs` | `evals_bakeoffs.html` | Which model / version / settings is best? |
| **Voice checks** | `/admin/voice-scenarios` | `voice_scenarios.html` | Do scripted live calls behave? |
| **Schedules** | `/admin/evals/scheduling` | `evals_scheduling.html` | How do real conversations score over time? |
| **Engineering** | `/admin/tests` | `tests.html` | pytest + agent E2E runs (staff-only, hidden on partner builds) |

Every screen leads with **results** and keeps forms behind a button: **Run tests**
(the `_run_dialog.html` drawer — same `POST /admin/eval-runs/trigger` contract as the
old always-open "Run now" form), **Create test suite**, **New comparison**, **New
schedule** and **New voice check** each open a side drawer (`<dialog class="drawer">`).
A rejected bake-off submit re-renders the page with the drawer re-opened on its error.

**Old URLs keep working.** `/admin/evals?suite_id=…` (or any other old filter, or
`?tab=runs`) 307s to `/admin/evals/runs` with the same query; `?tab=suites` 307s to
`/admin/evals/suites`; `GET /admin/eval-runs` 307s to `/admin/evals/runs`;
`/admin/datasets*` 302 to `/admin/evals/suites`. A run trigger now lands on the new
run's page (`/admin/evals/runs/{run_id}`, live progress) instead of the list.
`_evals_tabs.html` is an empty include kept for the templates that still include it.

**Agents that are testable.** On an unscoped build the agent pickers
(Run tests, Runs filter, Model comparison) add every agent kind that already has a
suite bound to it, so any agent with a suite can be run and baked off here
(`_load_picker_data`'s `allowed_kinds`). Names are resolved from **all** profiles; an id
that doesn't resolve shows as "Unknown agent (b089a338)" with a **Missing here**
chip and its Run button disabled.

### Overview

`GET /admin/evals` · `evals_overview.html` · `router.py:evals_list()` · `eval_runs_handlers.py:build_testing_overview_context()`

One card per agent that has a test suite. Each suite's **latest comparable run**
(finished + scored, not a bake-off variant or a model/prompt/version override —
`_comparable()`) gives the verdict: **Ready** when every tested suite meets its target,
**Needs work** when any falls short, **Not tested** when nothing has run, **Agent missing
here** when the bound profile doesn't exist in this environment. The card shows the
score of the weakest failing suite (or the most recent one), its target, the change
since that suite's previous comparable run, a sparkline of the agent's last 10 scores,
"N of M tested suites meet the target", the latest real-conversation (scheduled) score,
and **See N failures** (→ the run filtered to failed cases) / **Run tests** (→ the Runs
tab with the drawer pre-filled via `?run_profile=&run_suite=`). The summary line counts
agents meeting target, failing cases in the latest runs and untested agents; suites not
linked to any agent are counted underneath.

**Voice checks count toward the same score** (`admin/voice_checks.py`). A voice check
(`ai_voice_scenarios`) targets an agent *kind*; its calls are answered by that kind's
active top-level profile in the selected environment, so its results land on that
agent's card as one **Voice checks** row: score = share of the agent's enabled checks
whose latest scored call passed, target **100%** (they are pass/fail acceptance
scripts), change = against each check's previous call. That row takes part in the
verdict like any suite (an agent with passing text suites and a failing voice check is
**Needs work**), and an agent with only voice checks gets a card too. Every card with
more than one suite (or any voice row) lists its suites with a **Voice** / **Text** chip
and score / target — `runner_kind: voice_live` suites are labelled Voice as well; past
six rows the not-run tail folds into "N more test suites". **Run voice checks** opens
the Voice checks tab. Partner builds only see their own agents' checks
(`scope_to_build`).

### Suites & Runs

`GET /admin/evals/suites` · `evals_list.html` · `router.py:evals_suites()` — and `GET /admin/evals/runs` · `evals_runs.html` (+ `_tab_eval_runs.html`, `_run_dialog.html`) · `router.py:evals_runs()`. Both use `eval_runs_handlers.py:build_evals_page_context()`.

The old single page is split into two tabs: **Test suites** (define suites, each bound
to a profile, see each one's last result, Run it) and **Runs** (filterable list,
compare, score trend, and the Run tests drawer). Recurring schedules — for both the legacy suite-sourced
kind and the current prod-chat kind — are managed entirely from the
[Production monitoring](#production-monitoring) tab now; a suite still shows its own
targeting schedule (cadence + enabled) as a read-only column here.

**Demo agents.** A suite run against an AI-agents demo profile (`agents_*`) posts
the demo's own body — `agent` + `case_id`, echoing the `state` token between turns —
built by `admin/services/test_chat.py` (`own_body_shape`), the same builder as the
Test drawer and the MCP `chat` tool; the generic body 422'd every case. A scenario's
`case_id` (else the suite's `default_context.case_id`, else the agent's first case)
picks the case. Live-chat replays and per-profile E2E runs use the same builder, and a
`voice_live` suite dials the build's own demo WS (`/agents/voice/live` on the agents
build) with `callCase`. On a build that doesn't serve the demo the run errors with the
reason instead of sending anything.

#### Suites section

A suite is a set of scenarios (input + expected) run against **one bound profile** and
scored. A suite passes when its score clears `pass_threshold` — this is the gate that
blocks promotion (see [Deploy](./05-deploy.md)).

**Shows** — a suites table: humanised name (the raw slug as a muted technical name;
"Paused" chip when inactive), **Agent** (name, linked to `/admin/agents?profile_id=`;
"Not linked · <agent type>" when unbound; **Missing here** when the id doesn't
resolve), case count, **last result** (status chip + when, linked to the run — the
latest *comparable* run, see [Overview](#overview)), **score vs target in percent**
(`target 85%`, never `0.85`), the **change** since the previous comparable run
(`−3 pts`), schedule cadence (read-only; managed on
[Production monitoring](#production-monitoring)), and a per-row **Run** button that
opens the Run tests drawer pre-filled with that suite and its agent. While runs are queued/running, the status poller
reloads the page **once** when a watched run settles. (`build_evals_page_context()`
decorates each suite with `_judge_model` / `_last_run` / `_categories` / `_schedules` /
`_profile`.)

**Actions / popups**
- **Create test suite** (drawer → `POST /admin/evals/suites`; the target is entered
  as a percent and posted as the 0–1 `pass_threshold`): name, **Agent** (profile) picker
  (optional — binds the suite to one profile; picking one **derives `agent_kind`** from
  it via `_sync_agent_kind` and disables the agent-kind select client-side so a stale
  value can't be submitted alongside it — the server re-derives it either way and never
  trusts the client), else **Agent kind** picked directly (legacy/API-style, unbound
  suite), threshold, evaluator (judge) model (**required for comparison runs** — see
  [Mandatory judge](#mandatory-judge)), active flag. `runner_kind` defaults to `chat`
  and isn't exposed on this form — set it in the suite editor's Advanced section, from
  a seeder, or with the MCP `eval_suites` tool.
- **Edit suite** — click the suite name → the [suite editor](#suite-editor) (scenarios,
  threshold, evaluator, and the **same Profile picker** — rebinding re-derives
  `agent_kind`; clearing it back to "— none —" **nulls `profile_id`** without touching
  whatever `agent_kind` the suite already carried). Trigger runs from the
  [Run now](#runs-section) form.

#### Suite editor
`GET /admin/evals/suites/{suite_id}` · `eval_suite_edit.html` · `eval_handlers.py:eval_suite_edit()`

**No longer a dead end (B3).** The page leads with the latest comparable run (status,
score vs target, change, "See what failed") and a **Run this suite** button (a drawer
that posts to `/admin/eval-runs/trigger` with the suite and its agent; tenant, repeat
each case, voice mode and model under Advanced; `?run` opens it), then **Run history**
(last 8 runs with result, score, change and kind — standard / scheduled / model
comparison · variant). Then the editable **Cases** table — a plain `.table` now, not
Tabulator (whose virtual rows also kept off-screen inputs out of the DOM), with the
**Preview** toggle on its own line above each textarea instead of floating over the
text — plus **Add a case** and **Import cases from a CSV file** disclosures, then
**Settings** (name, agent, **target score in percent** — posted as the 0–1
`pass_threshold` — judge model, active) and an **Advanced** disclosure holding the
runner kind (with a plain description of each), the default tenant/language and the
context record ids, and the extra-context JSON. One **Save suite**
at the bottom. The agent picker always includes the suite's **current** agent (a
suite whose agent is outside the picker's list used to render "none", and a plain
Save unlinked it). A
malformed suite id is a 404 page. All field names / POST contracts are unchanged.

- **Settings**: name, pass threshold, evaluator model, active flag, and **Profile**
  (optional — same derive-`agent_kind`-on-save / null-on-clear behavior as the create
  dialog above; the caption under the picker reads "agent_kind ({{ current kind }}) is
  derived from the bound profile on save").
- **Default trigger context**: tenant (`business_id`) picker + context record ids
  (branch, customer, …), locale, extra JSON — merged into every scenario at run time.
- **Scenarios table**: inline-editable name (blank = shown by its question; the id
  is listed under it), category, question, expected answer
  (question and expected are markdown textareas with an **Edit/Preview** toggle),
  with per-row delete and a details view of extra keys (e.g. `expected_substrings`,
  `rubric`, `expected_tools`).
- **Scenario rubric** — an optional `rubric` field on each scenario row holds a list
  of `{id, criterion, weight?}` objects. When present the judge scores each criterion
  independently and returns a weighted-mean `overall_score` (0–1). When absent, the
  judge falls back to a single "matches expected_answer" criterion (back-compat). The
  `rubric` value lives in the scenario `jsonb` column — no schema migration needed.
- **Add scenario** form and a **CSV import** card. The plain fields (id, category,
  question, expected answer) cover a `chat` scenario; a collapsed
  **"Scenario JSON (advanced)"** textarea merges arbitrary JSON onto the new row
  (the plain fields win on key collision) — it's the only portal path to author a
  `voice_live` scenario's `caller` block (`persona`/`goal`/`facts`/`max_turns`) plus
  `limits`/`checks`/`expected_proposals`, none of which the plain fields express.

**CSV import** — `POST /admin/evals/suites/{suite_id}/import`
Appends rows (no dedup). Recognised columns: id, category, question, expected_answer,
expected_substrings (`;`-separated), locale, business_id, branch_id, customer_id and
the other context record ids the suite editor offers. Missing ids are slugged from the question; redirects back
with an "imported N" chip.

**Backing tables** — `agent_eval_suites`, `agent_eval_runs`, `agent_models`,
`agent_profiles`.

#### Eval runner internals

`evals.py:_invoke_endpoint()` / `_extract_answer_chunks()` · `e2e_runner.py:_invoke_judge()`

How a scenario is actually executed and scored against a live agent endpoint:

- **Concurrency** (`_resolve_concurrency` / `_dispatch_attempts`) — scenario attempts
  (scenarios × repeats) run on a bounded thread pool, **4 at a time** by default.
  Set it per run (**Cases at a time** in the run dialog's Advanced section, MCP
  `trigger_eval_run concurrency=`), per suite (`default_context.concurrency`), or
  per deployment (`EVAL_CONCURRENCY`); run beats suite beats env, clamped to 1–8.
  `voice_live` suites always run one at a time (per-IP anonymous voice caps, billable
  live calls, the daily audio budget). Results are stored in scenario-then-attempt
  order whatever order they finished in; a multi-turn scenario is one task, so its
  turns stay sequential. Cancel is polled before each attempt **starts**: in-flight
  attempts finish and are kept, nothing new starts. Each attempt runs in its own
  `contextvars.copy_context()`, so its Laminar span is still a child of the suite span
  and the admin env the operator picked still applies; each worker thread builds its
  own judge. Pick **1** for a suite whose scenarios write to the same test customer
  and must not overlap.
- **Tool-result errors** (`_tool_result_error` / `_extract_tool_calls`) — a failing
  tool usually doesn't raise: it returns `{"error": "<fn> returned 500",
  "details": …}`, `{"ok": false}` or `{"status": "error"}`, and the agent carries on.
  The runner reads every tool **result** (`observation` / `tool_result` SSE events,
  or a runner's own `tools` list) and
  records error-shaped ones per attempt as `tool_errors` (`[{tool, error, turn?}]`,
  error clipped to 160 chars) and `tool_error_count`; tools that came back empty
  (`[]`, `{}`, `{"data": []}`) go in `empty_tool_results` (informational). Detection:
  a non-empty `error` key up to a few dicts deep, `ok`/`success: false`, `isError:
  true`, a top-level `status` of error/failed, `Error: …` text, and the same keys
  matched textually when the agent clipped the result mid-JSON. Lists are not walked
  (a payment row with `status: failed` is data). **Policy:** an `expected_tools`
  entry whose every result errored fails the tool assertion (`errored_expected_tools`,
  "Used, but it only returned errors"); a retry that succeeded clears it. Any other
  tool error is shown (a chip on the case, a **Tool errors** list per attempt, the
  run's **Tool errors** total under Technical details, MCP `eval_runs`
  `results_summary.tool_errors` / `with_tool_errors`) but does not fail the scenario —
  an agent that says "I can't confirm that right now" after a 500 is following its
  prompt, and the judge sees the result in the trace. Set `fail_on_tool_error: true`
  on a scenario or in the suite's default context to fail on any tool error. An agent
  that emits no tool-result events gives the runner nothing to detect.

- **Streaming answer capture** — the agent endpoints return `text/event-stream`
  (chunked, keep-alive). The runner reads the response with `httpx`'s streaming
  API and **drains the socket to EOF**, accumulating every byte before parsing.
  `_extract_answer_chunks()` then concatenates the `content` of **every**
  answer/content-bearing SSE frame (the reply streams token-by-token across many
  `answer` chunks) and skips non-answer frames (`session`, `title`, `thought`,
  `proposal`, `done`, keep-alive `:` comments, `[DONE]`). This replaced a
  buffered `.text` read that intermittently captured only a partial body and
  scored correct answers `0` (the flaky empty-`actual` zeros).
- **Per-scenario metrics captured** — after the stream drains, the runner records
  in each scenario's `results` dict (jsonb, no schema change):
  - `iterations` — count of assistant/LLM turns in the SSE transcript.
  - `tool_count` — count of tool-call events in the SSE transcript.
  - `ttft_ms` — milliseconds from request start to the first content chunk.
  - `ttlt_ms` (`total_ms`) — milliseconds from request start to the final `done`
    event (time to last token).
- **HTTP timeout 120s** — raised from 60s. Grounded scenarios can legitimately
  call several reporting tools and synthesize a long answer; the ceiling is a
  safety net, not a workaround for a hang (the CSV→cloud-storage hang is fixed
  separately, below).
- **CSV export gated off in dev/local** — `shared/cloud_storage.py`
  `generate_csv_export_gcs()` short-circuits when the environment is
  local/dev/test or the backend has no storage target configured
  (`CLOUD_STORAGE_BACKEND` defaults to **`azure_blob`**, which needs
  `AZURE_STORAGE_ACCOUNT_URL`). Override with `GCS_EXPORT_ENABLED=1/0` — that var
  and `GCS_UPLOAD_TIMEOUT_SECONDS` keep their historical `GCS_`-prefixed names,
  as does the `generate_csv_export_gcs` function itself. In the gated cases it
  returns the report rows **without** uploading (`storage: "skipped"`), so
  grounded tool queries no longer hang on a credential prompt or an
  after-fork deadlock. When enabled, the upload runs on a worker thread under a
  `GCS_UPLOAD_TIMEOUT_SECONDS` (default 8s) wall-clock timeout and degrades to
  `storage: "timeout"` rather than blocking the request.
- **Grounding fixture** — a suite whose agent calls data tools should run against a
  tenant that has rich data, so tool calls return real results. The suite
  `default_context` carries `business_id` / `businessId` (and any access flags the
  agent's tools read) so the agent authorizes and scopes its tools correctly. These suite/context values
  live in the `agent_eval_suites` rows (AI DB runtime state), not in the repo.
- **Scoring dispatch by `runner_kind`** — a scenario's scorer is resolved through
  a small registry keyed by the suite's `runner_kind` (default `chat`, no entry —
  every chat suite takes exactly the substring+judge path above, unchanged) or an
  explicit per-scenario `assert.type` override
  (`admin.services.eval_scorers.SCORERS`). A plugin may add runner kinds with their
  own invocation and scorer (the `eval_runners` hook); invocation for a scorer-only
  kind stays the same `_invoke_endpoint` call, which tolerates a plain-JSON response
  body, falling back to the raw body when no SSE `answer` frames are found. See
  [Seeded suites](#seeded-suites).
- **Profile-version pin forwarding** — when a run's `profile_version_id` is set
  (a [bake-off](#bake-offs) variant, or a Run-now
  [Profile-version override](#variant-overrides-on-the-run-trigger)),
  `_invoke_endpoint` sends it as `X-WAJ-Profile-Version` alongside any model/prompt
  override headers, so the agent process serves that immutable snapshot for the
  request instead of the live profile.

#### Seeded suites

The engine ships no suite of its own. A deployment's plugin may seed suites at boot
(the `eval_suite_seeds` hook: `eval_suite_specs()`, read by
`admin/seed_eval_suites.py`, insert-only on `name`). A spec names the suite,
`agent_kind`, `pass_threshold` and either inline `scenarios` or a `scenarios_file`
under `evals/scenarios/`; optionally an `evaluator` (bind an LLM judge),
`runner_kind`, `default_context`, `first_message` (mark every scenario as a session's
first message) and `profile` (`{slug, environment}`: bind to that named profile; the
suite is skipped until it exists). On a scoped build, a seeded `voice_live` suite's
`default_context.voice.ws_path` is rewritten to the scope's mount
(`<portal_prefix>/voice/live`). A plugin may also add runner kinds (`eval_runners`)
and check types (`eval_checks`); a build without them reports "unknown check type"
for a check it does not have.

A seeded judged suite gets its judge from, in order: the main model of a profile
whose kind the plugin names (`judge_profile_kinds()`), if `_try_build_judge` can build
it; any active Gemini model row in the environment; any active Claude model row. With
none, the suite seeds without `evaluator_model_id` and grades by substring only.

#### Building a suite that discriminates

A suite of easy "did it call the tool?" scenarios scores **1.0 for every model** and
tells you nothing. To rank models (or prompts) you need *hard tasks + strict rubrics +
a working judge*: mix multi-step tasks that weak models botch (a percentage change, a
filtered average, a breakdown by category) with fabrication traps that models fall for
differently (confirm a figure that doesn't exist, project the future, name a record
that isn't there), and grade every scenario with a `rubric` for partial credit. A
suite built this way spread `mean_judge_score` across ~0.53–0.68 from the cheapest
Flash model to the strongest Claude model (per-scenario 0.0–1.0). For an agent that
must answer in the caller's language, lead each rubric with a language criterion
(e.g. `in_arabic`). A **Claude** judge grades more strictly than Gemini; either
provider works as the evaluator.

> A **Claude evaluator needs `ANTHROPIC_API_KEY` set on the engine.** Without it
> `_try_build_judge` returns `None` and the suite degrades to **smoke-only**
> (substring checks), which is the intended behaviour: `ChatAnthropic` constructs
> happily with an empty key and 401s per call, so the builder refuses to build one.
> If a Claude-evaluator run comes back all-`0.5` with `judge_error:` rationales, that
> is the key, not the agent.

Each judged suite uses the LLM judge **plus** the always-on substring check. Suites
run from [`/admin/evals`](#evals), record runs in `agent_eval_runs` and emit a
Laminar trace per scenario. Mark scenarios `is_first_message` when the run should
persist a chat session + turn for the Observability transcript.

#### Voice eval suites (`runner_kind: voice_live`)

`admin/voice_eval_driver.py` (the caller loop) · `admin/voice_eval_runner.py`
(the per-scenario executor) · `admin/services/evals.py:_voice_live_guard()`

A `chat` suite grades one question-answer pair. A `voice_live` suite grades a
whole **conversation**: instead of `_invoke_endpoint` POSTing one scenario at
the profile's `endpoint_path`, `run_eval_suite_sync` dispatches to
`run_voice_scenario_sync`, which drives a goal-driven LLM caller over a real
`WS /voice/live` session (the same duplex edge described in
[Architecture → Duplex voice edge](./00-architecture.md#duplex-voice-edge))
and judges the resulting transcript.

- **The `caller` block** — each scenario carries a `caller` object instead of
  (or alongside) `question`/`expected_answer`:
  - `persona` — a short character sketch (who's calling, tone, language).
  - `goal` — what the caller is trying to accomplish this call; the caller
    LLM keeps talking until the goal is met or it can't be, then hangs up
    (`[HANGUP]`).
  - `facts` — a flat `{key: value}` map the caller may reveal when asked
    (an IBAN, a TID, …) but must never invent beyond.
  - `max_turns` — hard cap on caller utterances (1–20, clamped); the caller
    wraps up before it runs out.
  - `opening` (optional) — a fixed first utterance that skips the caller LLM
    for turn 0, for deterministic call openings.
- **Sim vs live mode, and the cost warning** — `default_context.voice.mode`
  on the suite (or a per-run `voice_mode` override — see
  [Runs section](#runs-section) below) picks the transport:
  - **`sim`** (default) — the `simulated` realtime provider (see
    [Architecture → Duplex voice edge](./00-architecture.md#duplex-voice-edge))
    stands in for the real speech provider: no audio synthesis, no per-minute
    provider bill, just the text turns through the same state machine, tool
    delegation, and `to_speakable()` transform a real call would hit.
  - **`live`** — a REAL realtime-provider call: caller utterances are
    synthesized with the same Azure Speech TTS the turn-based edge uses and
    streamed at real-time pace, so a live voice suite run **costs real
    per-minute provider spend**, same as a production call. Only use it
    deliberately.
- **Every harness call is badged `sim` in observability** — the eval driver
  and the scenario runner tag their connections as simulated (the sim
  provider implies it for `mode: "sim"`; live mode self-declares with
  `?simRun=1`, honoured only from a loopback peer), so their sessions carry
  a `sim` chip in the [traces explorer](./01-operate.md#conversations)
  and their call recordings (real audio in live mode; a metadata-only row
  for text-only sim) are badged apart from real caller sessions in its
  Audio tab. A run pointed at a **remote** target (`VOICE_SCENARIO_TARGET`
  / an explicit `target`) is not loopback on the receiving engine, so its
  sessions arrive unbadged there.
- **Live is manual-only** — `_voice_live_guard()` in
  `admin/services/evals.py` refuses `mode: "live"` on anything that isn't a
  human clicking Run now: **scheduled** runs and **bake-off** runs always
  fall back to `sim` for a `voice_live` suite, regardless of what the suite's
  `default_context.voice.mode` says, raising a clear error instead of
  silently billing on an unattended cadence.
- **Triggering voice suites over MCP** — the admin MCP `trigger_eval_run` tool
  takes the same `voice_mode` (`sim`\|`live`\|blank) the portal's "Run now"
  form does: blank defers to the suite's `default_context.voice.mode`
  (itself defaulting to `sim`), an explicit value overrides it for that one
  run. `trigger_eval_run` calling `start_suite_run` directly IS the
  "human clicking Run now" case `_voice_live_guard` allows through with
  `mode: "live"` — the guard only blocks `live` reaching it via a
  `schedule_id`/`bakeoff_id`-linked spawn, not via this tool. Pick `live`
  deliberately over MCP the same as in the browser: it's a real, per-minute
  billable provider call. See [MCP → Test tools](./06-mcp.md#test--admin_mcptools_testpy).
- **`checks` are hard gates** — an optional per-scenario `checks` array (same
  `http_check` shape as [Voice scenarios](#voice-scenarios--live-call-acceptance)'s
  steps: re-read backing state over HTTP and regex the body) runs after
  hangup. A failing check fails the scenario **regardless of the judge
  score** — the same "assert what happened, not how it was phrased" pattern
  as the scripted harness, applied to a judged conversation.
- **`limits` are per-scenario metric hard gates**, evaluated against the
  driver's per-turn timing (`admin/voice_eval_runner.py:_check_limits`):
  ```json
  "limits": {"max_answer_ms": 12000, "max_latency_ms": 15000, "max_turn_ms": 30000,
             "require_turn_end": true, "require_complete_utterances": true,
             "min_played_audio_ratio": 0.8}
  ```
  `max_answer_ms` caps the **answer** latency per turn (below),
  `max_latency_ms` caps time-to-first-assistant-reply of ANY kind,
  `max_turn_ms` caps the whole turn (send → `turn_end`), and
  `require_turn_end` fails any turn where the driver never saw a
  `turn_end` frame.

  **Answer latency vs first-reply latency.** On a tool turn the first
  assistant transcript is usually the out-of-band **holding line** ("still
  working on that…") the endpoint speaks at `progress_after_sec` — it
  arrives on schedule regardless of how slow the real answer is, so a
  first-reply metric pegs there and is structurally blind to a slow
  delegate. The endpoint now stamps out-of-band speech on the wire
  (`transcript` frames carry `"out_of_band": true`; the key is absent on
  normal answer frames, so old clients are unaffected), and the driver
  records both numbers per turn:

  | per-turn field | measures |
  |---|---|
  | `answer_ms` | caller utterance → first **non**-out-of-band assistant transcript — the real answer. This is what `results[].latency_ms`/`ttft_ms` aggregate, and what `max_answer_ms` gates. |
  | `first_reply_ms` (legacy alias `latency_ms`) | caller utterance → first assistant transcript of any kind — on tool turns, the holding line. Kept for continuity; `max_latency_ms` still reads it. |
  | `holding_line` | whether an out-of-band line preceded the answer this turn. `scorer_detail.holding_line_rate` is the fraction of turns where it did — how often callers sat through filler. |

  Gate new suites on `max_answer_ms`; keep `max_latency_ms` only for
  back-compat with existing suite rows (a suite may carry
  `max_answer_ms: 12000` alongside a legacy `max_latency_ms: 20000`).
  Note the seeder is insert-only on suite name — an already-seeded stack
  keeps its old `limits` until the row is deleted and re-seeded.

  The last two are the **truncation gates**, and they exist because a rubric
  judge cannot do this job: a reply that was cut off still contains the
  answer, so a `no_truncation` criterion reads the fragment and scores it a
  pass. There are two ways a caller loses the end of an answer, and one
  limit for each:

  | limit | breach `kind` | catches |
  |---|---|---|
  | `require_complete_utterances` | `incomplete_utterance` | the reply's **text** stops mid-utterance — the service stopped generating. Audio and transcript stop together, so no audio measurement can see it; the unfinished sentence is the only evidence on the wire. Verdict is "does the turn end on sentence-final punctuation" (Latin, Arabic and CJK terminators; a trailing colon counts, a trailing `،` does not). Opt-in per scenario — a scenario that asks for a bare list or a spelled-out IBAN readback legitimately ends without punctuation. |
  | `min_played_audio_ratio` | `min_played_audio_ratio` | the text is complete but the **sound** never reached the caller, because a playback flush discarded audio the model believes it delivered. Ratio is `(audio sent for this turn − audio a flush threw away unheard) / audio sent`; a turn that sent no audio is skipped, never a false `0.0`. |

  Both are fed by the driver behaving like the real client: it keeps the same
  playback cursor `static/shared/voice-live.js` does and reports its `played`
  offset on every flush. Without that the engine's
  flush → `played` → `conversation.item.truncate` path is unreachable from an
  eval, which is why the 2026-08-06 mid-word truncation could not have been
  caught here before.

  Any breach fails the scenario **independent of the
  judge verdict**, same hard-gate semantics as `checks` above, and is
  recorded as `scorer_detail.limit_breaches` (a list of
  `{turn, kind, value, limit}`; empty when limits are set and clean, the
  key is absent entirely when the scenario has no `limits`). Limits are
  only evaluated on a **completed** transcript — a skipped/errored
  scenario already fails for its own reason. All fields are optional
  and independent; a scenario can set any subset.
- **Reading a run in the portal** — every `voice_live` attempt's
  `scorer_detail` renders inline on
  [Eval run detail](#eval-run-detail) (`admin/eval_handlers.py`'s
  `group_results_by_scenario()` carries the full result row — `scorer_detail`
  included — into each attempt; the template renders it only when the key is
  present, so a plain `chat` run's attempts are unaffected), not just a
  flattened pass/fail:
  - **Transcript** — the full turn-by-turn conversation, caller/assistant
    bubbles right/left, `dir="auto"` on each line so Arabic renders RTL
    (most caller-loop transcripts are Arabic).
  - **Per-turn timing** — a collapsible table of `turn` / `latency_ms` /
    `turn_ms` / `turn_end_seen`, with the max latency called out right in
    the collapsed summary so a slow run is visible without expanding it.
  - **Limit breaches** — rendered as red chips (`turn N · kind: value (limit
    L)`), and — because a `limit_breaches` fail can otherwise look like a
    pass (the judge reason may read as a perfectly good answer) — a
    `N limit breach(es)` chip also appears on the attempt's own collapsed
    summary line, next to the pass/fail chip, so the fail explains itself
    before you expand anything.
  - **Checks** — the `http_check` results table (pass/fail, URL, a
    truncated response-body head).
  - **Proposals** — `proposals` surfaced during the call and any
    `missing_proposals` the `expected_proposals` gate caught, when present.
  - **Meta line** — mode (`sim`/`live`), turn count, termination reason,
    audio bytes received, and target.
  - Anything in `scorer_detail` not explicitly rendered above (e.g. a field
    added by a future scorer change) is still reachable via a collapsed
    **Raw scorer detail (JSON)** fallback at the bottom of the attempt —
    nothing is silently hidden.
- **Reading a limit-breach failure** — start from the breach chip: it names
  the turn and the metric (`turn 2 · max_latency_ms: 16000 (limit 15000)`).
  Cross-reference that turn number against the per-turn timing table and the
  transcript (turns are 1-indexed against caller utterances) to see what was
  actually said before assuming the agent regressed — one slow tool call on
  a single turn is a different bug than every turn creeping up, and the
  judge's reason (which grades prose, not timing) won't tell you which.
- **An evaluator model is required** — `voice_live` suites error out at
  trigger time with no evaluator model configured (same as any judged suite;
  see [Mandatory judge](#mandatory-judge)), because the **same** model plays
  **two roles**: it's the LLM **judge** grading the transcript, and it's also
  the **caller** brain deciding what to say next each turn — one evaluator
  pick drives both. A per-scenario `caller.model` field exists in the schema
  to point the caller at a *different* model than the judge, but it is not
  wired in v1: every caller today runs on the suite's evaluator model
  regardless of what `caller.model` says.
- **Sim mode needs a greeting trigger** — the `simulated` provider treats the
  **first** text turn it receives as the endpoint's own greeting trigger
  (`voice_config.greeting_trigger` on the profile) and replies with a
  scripted greeting instead of routing it to the caller's opening line. A
  profile with no `greeting_trigger` configured has the caller's real first
  utterance swallowed as a fake "greeting" turn — set one before running
  any `voice_live` suite in sim mode against a profile.
- **`VOICE_LIVE_SIM_ENABLED` is control-plane-only** — the `simulated`
  provider slug can only be requested (`?provider=simulated` on the WS URL)
  when the deploy set `VOICE_LIVE_SIM_ENABLED=1` **and** the connection is
  loopback **and** the handshake carries no forwarding header (see
  [Architecture → Duplex voice edge](./00-architecture.md#duplex-voice-edge)).
  Set it on the instance that serves the admin portal (where eval runs
  connect to their own replica) — **never** on the public chat instance: denial there is silent by design (falls through to the profile's
  real provider) so the query param can't be used to probe what a deploy
  supports. The real posture: `websocket.client.host` alone is NOT trusted
  as proof of loopback, because this app runs behind uvicorn with
  `--proxy-headers --forwarded-allow-ips="*"` (Dockerfile) so a real
  edge proxy can report genuine client IPs — but that same setting makes
  uvicorn's ProxyHeadersMiddleware rewrite `scope["client"]` from a
  client-supplied `X-Forwarded-For`, on WebSocket scopes too. A remote
  caller sending `X-Forwarded-For: 127.0.0.1` would otherwise sail through
  the loopback check. The gate closes that: any connection that arrives
  with `X-Forwarded-For`/`X-Forwarded-Host`/`X-Forwarded-Proto`/`Forwarded`
  set — i.e. anything that came through a proxy, real or spoofed — is
  denied, by design. Only a connection that reaches uvicorn directly (the
  eval driver's `ws://127.0.0.1:$PORT`) passes.
- **vs. [Voice scenarios](#voice-scenarios--live-call-acceptance)** — the two
  voice-testing surfaces cover different things and are not a replacement
  for one another: **Voice scenarios** (`/admin/voice-scenarios`) is a
  scripted, deterministic smoke/acceptance harness — fixed `say` /
  `expect_transcript` / `expect_frame` / `http_check` / `wait` steps,
  pass/fail on exact matches, cheap to run on every deploy. **`voice_live`
  eval suites** are judged, goal-driven **conversations** — a caller LLM
  improvises its way toward a goal across several turns and an LLM judge
  grades the transcript against a rubric, closer to how a real call
  actually goes (distractions, follow-up questions, mid-flow tangents).
  Use Voice scenarios to catch a broken greeting or a dead tool path fast;
  use `voice_live` suites to catch an agent that loses the thread of a real
  conversation.

- **`expected_proposals` hard gate** — a scenario may list proposal types that
  must have actually been surfaced as `proposal` frames during the call. A
  missing type forces `passed=false` regardless of judge score (recorded as
  `scorer_detail.missing_proposals`), because prose can say "queued for your
  review" while nothing reached the screen. Surfaced proposals are also rendered
  into the judged transcript as `[PROPOSAL SURFACED: ...]` lines so rubric
  criteria can reference them, and stored verbatim in `scorer_detail.proposals`.
- **Auth** — a voice kind that requires first-frame auth takes it from
  `default_context.voice.auth` plus the operator's JWT (the driver sends the
  same `auth` frame the app does). The trusted sim path (`VOICE_LIVE_SIM_ENABLED`
  + direct loopback + `?provider=simulated`) and header-signature-verified callers
  are exempt where the kind allows it (see [Architecture → Duplex voice
  edge](./00-architecture.md#duplex-voice-edge)). A **live**-mode run of such a
  kind must put a verified bearer in `default_context.voice.headers`
  (`{"Authorization": "Bearer <JWT>"}`).
- **`ws_path`** — `default_context.voice.ws_path` names the socket the driver
  dials (`/voice/live`; `<portal_prefix>/voice/live` on a scoped build, where the
  duplex voice router mounts under the scope's prefix).

#### Checks-graded scenarios (text + voice)

A scenario can be graded by **deterministic checks**
(`admin/services/eval_checks.py`) instead of, or as well as, a judge:

- A scenario carries `turns: [{question|say, checks: [...]}]` — played in one
  chat session (chat runner, `_run_turns`) or as a scripted caller (voice
  driver, `caller.script`; no caller LLM). A scenario with `turns` and no
  `expected_answer` passes iff every check passes; checks are also a hard
  gate on judged scenarios (`results[].checks`, `checks_passed`). A single-turn
  scenario may carry a top-level `checks` list.
- Engine check types: `no_uuid` (no record id in the answer), `regex` /
  `not_regex`, `no_question` (optionally about a pattern), `asks` (the turn asks
  a clarifying question), `tools_any` (at least one of these tools ran),
  `no_proposal` (this turn created no card), `no_saved_claim` (never says a
  change is saved or done) and `screen_backed` ("it's on your screen" only when
  the screen got a table, a chart or a card). A plugin may add check types (the
  `eval_checks` hook: `check_types()` plus the `facts(business_id, user_token)`
  they read), for example a check that compares a figure in the answer with a
  direct database query. A build without that plugin reports
  "unknown check type" for it, and the check fails.
- Payload placeholders resolved at run time: `{d+N}` (today + N days),
  `{next:thursday}` (the next such weekday), and record-name lookups such as
  `{staff:<name>}` resolved with the caller's token.

#### Runs section

`GET /admin/evals/runs` · `_tab_eval_runs.html` + `_run_dialog.html` (included by `evals_runs.html`) · `eval_runs_handlers.py:build_eval_runs_context()`

The on-demand + comparison hub — distinct from the [Suites section](#suites-section)
(which *defines* suites), from [Production monitoring](#production-monitoring) (which
now owns **all** recurring/scheduled eval coverage), and from
[eval run detail](#eval-run-detail) (which inspects one run). Fires one-off runs and
shows every recent run, filterable, with a trend chart and multi-run compare.

**Shows / actions**
- **Run tests** (drawer; was the always-open "Run now" form — same field names and
  POST) — fire an eval run on demand (`POST /admin/eval-runs/trigger`), choosing
  the suite/profile and (optionally) sourcing scenarios from recorded chat sessions.
  **Sensitive agent kinds are never offered as a live-chats source.** A plugin may
  mark kinds whose real conversations hold sensitive data (`sensitive_agent_kinds()`,
  `core/sensitive_content.py`); a live-chats run or schedule that still targets one
  ends `errored` without reading a single session. Evaluate those agents with
  authored suites.
  For **live-chats (real-data) runs the judge model and an *expectations* field are
  required** (the expectations textarea is pre-filled with a sensible default the
  operator edits) — a real-data run is never started ungraded. The expectations are
  fed into the reference-free quality judge so it scores against what the operator
  defined, not just generic helpfulness; they're stored on the run's `context`. The
  judge model is picked by display name and resolved to its `model_string` before the
  judge is built. The **variant-override expander** (suite mode) also carries an
  optional **Profile version** picker — see
  [Variant overrides](#variant-overrides-on-the-run-trigger) below, plus a **Voice
  mode** select (`sim` / `live` / suite default) that only takes effect for a
  [`runner_kind: voice_live` suite](#voice-eval-suites-runner_kind-voice_live) — pick
  `live` deliberately, it's a real billable provider call. **Calibrate
  expectations for reference-free grading:** the judge has no ground truth, so it can't
  verify tool-grounded numbers — phrase expectations around relevance / tool-grounding /
  refusals / language, not "are these figures real" (the same agent scored 0.21 under a
  "must not fabricate numbers" rubric vs 0.76 under a tool-grounding rubric).
- **Trend chart** — score / pass-rate over time, one coloured series per **prompt
  version** so prompt-A/B comparisons are visual. Filter by suite, metric (score vs
  pass-rate) and window (7d/30d/90d/1y); the suite's `pass_threshold` is drawn as a
  dashed reference line when the filtered set shares one. Fed by
  `GET /admin/eval-runs/report.json?suite_id=&days=` (`eval-runs-chart.js`, Chart.js).
  Prompt version is derived from each run's trigger `context` (`prompt_version` →
  `override` when a `prompt_override` was used → `baseline`).
- **Runs table** (first on the page) — a filterable list (test suite / agent / result /
  since, plus source and agent type under "More filters"; server-side via query
  params). Columns: test suite (linked to [eval run detail](#eval-run-detail); the
  variant label or model override as a sub-line), agent, result, **score vs target**
  (percent, coloured by pass/fail against the target), **change** vs the previous
  comparable run of the same suite + agent (`previous_run_map()`), cases passed
  (`45 / 57`), finished (relative, absolute on hover). Engineering columns (prompt
  version, gen-config) moved to the run page's Technical details. In-flight rows
  update **in place** (status chip / score / pass-rate) via
  `GET /admin/eval-runs/status.json` polled by `eval-runs-poll.js` — no full-page reload
  or scroll jump; polling stops once nothing is queued/running and pauses while the tab
  is hidden. Tick ≥2 rows → **Compare selected** (disabled — and visibly so — until two
  are ticked; see [Eval run compare](#eval-run-compare)).

**Sub-views & polling** — `GET /admin/eval-runs/status.json` (list-level in-place
poll); `GET /admin/eval-runs/{run_id}/status.json` (per-run poll used by the detail
page); `GET /admin/eval-runs/report.json` (trend data). All are superuser-gated JSON.

**Backing tables** — `agent_eval_runs`, `agent_eval_suites`, `agent_profiles`,
`agent_profile_versions` (when a run pins `profile_version_id`), and
`ai_chat_sessions` / `ai_chat_messages` (for real-history evals). Recurring-schedule
CRUD reads/writes `agent_eval_schedules` from the
[Production monitoring](#production-monitoring) tab, not here.

> Legacy `GET /admin/eval-runs` 307-redirects to `/admin/evals/runs` (filters carried
> over); the `/admin/eval-runs/*` `.json` + POST sub-routes are unchanged, except that
> `POST /admin/eval-runs/trigger` now redirects to the new run's page.

#### Variant overrides on the run trigger

The **Run now** trigger dialog (and the schedule trigger) exposes a **"Variant
override"** expander. Fields:

| Field | Stored key | Description |
|-------|-----------|-------------|
| Model | `model_override` → `model_string` | Pick any active model to substitute for the profile's default model. The override object carries `provider`, `model_string`, `temperature`, `extra_config`, and optionally `max_output_tokens` — built by `_build_model_override()`; there is no `model_id` in the wire shape. |
| **Profile version** | `profile_version_id` | Pin the run to one promoted `agent_profile_versions` snapshot (tools/prompt/KB/MCP/model, all frozen at promote time) instead of the live profile — e.g. reproduce a [bake-off](#bake-offs) variant's exact config outside the bake-off flow. Forwarded to the agent as `X-WAJ-Profile-Version` (see [Architecture → Profile-version pinning](./00-architecture.md#request-scoped-profile-version-pin-x-waj-profile-version)); recorded on both `context` and the dedicated `agent_eval_runs.profile_version_id` column. |
| Prompt version pins | `prompt_overrides_json` | Run one or more prompts at a specific version NUMBER instead of the pinned one, as JSON: `{"<agent_kind>/<prompt slug>": 3}`, or the short `{"system": 3}` for the selected agent's own instructions (a generic agent's prompt is `<slug>:system`). Checked before the run is queued (`admin.services.evals.resolve_prompt_overrides`): an entry that names no prompt or no such version is refused with a 400, since the runtime treats an override it cannot find as "no override" and the run would grade the live prompt under the variant's label. |
| Temperature | `model_override` → `temperature` | Override the model temperature for this run. |
| Max output tokens | `model_override` → `max_output_tokens` | Override the max-output-token ceiling. |
| Top-p | `model_override` → `extra_config.top_p` | Override top-p sampling (stored in `extra_config`). |
| Thinking level / budget | `model_override` → `extra_config.thinking_level` \| `extra_config.thinking_budget` | Override Gemini reasoning depth **per variant** — `minimal`/`low`/`medium`/`high` (Gemini 3+) or an integer budget (`0` = off, Gemini 2.5). Lets you A/B reasoning depth on cost/latency/score. Level wins over budget; a budget-only variant drops any inherited level so the budget applies. Shown in the **Gen config** column as `think <level>` / `think <N>t`. |
| Variant label | `variant_label` | Free-text label recorded in the run `context` (e.g. `"gemini-flash-v3"`) for trend-chart legend and compare-column headers. |
| Cases at a time | `concurrency` | How many scenario attempts run at once (blank = the suite's `default_context.concurrency`, else `EVAL_CONCURRENCY`, else 4; clamped 1–8; `voice_live` always 1). Recorded in `context.concurrency`. |
| Attempts per scenario | `repeats` | How many times to run **each** scenario (1 / 3 / 5 / 10). `>1` enables **pass^k** (consistency): the compare matrix reports the fraction of scenarios that pass **all N** attempts, surfacing flaky scenarios that pass-rate hides. `1` (default) is a single run where `pass^k` equals the pass rate. Cost & time scale ~N×. Recorded in `context.repeats` and shown in the Recent-runs **Gen config** column as `N× (pass^k)`. |

All override values are written into `agent_eval_runs.context`. The run executes
under those overrides (applied at `runtime.agent._build_llm` via
`X-WAJ-Model-Override`); the resulting run can then be ticked in the Recent runs
list and compared against baseline or other variant runs. With `repeats > 1` the
runner (`run_eval_suite_sync(repeats=N)`) runs each scenario N times and the
compare view's `pass^k` row uses the unbiased `C(c,k)/C(n,k)` estimator (k=n) per
scenario, averaged.

---

### Production monitoring

`GET /admin/evals/scheduling` · `evals_scheduling.html` · `router.py:evals_scheduling()` · `eval_runs_handlers.py:build_scheduling_context()`

Testing › **Schedules**. Results first — the indicators (latest score, replies graded
good, conversations graded, change since the run before; all percent / points) appear
only once scheduled runs exist, then the schedules table (plain cadence, "N
conversations from the last D days · judged by M", Run now / Turn off / Delete), then
the scheduled runs and their trend. With no schedules there is one empty state with a
**New schedule** button; the create form lives in a drawer (cadence defaults to
**daily**). Watching production quality on a cadence: every tick samples **real prod
chats** for one profile's agent kind and judges them **reference-free** — no synthetic
suite, no gold answer needed. This is where **all** recurring eval scheduling now
lives — the old Runs-section "Schedules" block on
[Suites & Runs](#suites--runs) is gone; legacy suite-sourced schedules (`source='suite'`,
created before this tab existed) still show here too, flagged distinctly, so nothing an
operator already configured silently disappears.

**Shows / actions**
- **New schedule** (`POST /admin/eval-runs/schedules`, `source` is always `live_chats`
  on this tab's create form — there is no suite picker here): Name · **Profile** (drives
  which agent kind is sampled) · **Judge model** · **Expectation** (textarea,
  pre-filled with the picked profile's agent-kind default via
  `default_expectations_by_kind`, editable — the auto-fill only overwrites while the
  operator hasn't typed a custom expectation of their own) · **Lookback (days)** ·
  **Sample size** · **Cadence** (manual/daily/weekly/monthly) · optional **Tenant** (`business_id`) ·
  Enabled flag. Two submit buttons: **Create**, or **Save & run now** (fires
  immediately after saving).
- **Schedules table** — name (+ a "suite (legacy)" chip on pre-existing `source='suite'`
  rows), profile, cadence, **sample config** (a compact `Nd lookback · N samples ·
  judge: <model>` string; legacy rows fall back to a raw JSON dump of `coverage` since
  they don't carry those keys), enabled, last run, next run, and per-row **Fire** /
  **Enable/Disable** / **Delete**. Manual schedules fire only via the Fire button;
  recurring cadences set `next_run_at` and are fired by an external cron hitting
  `POST /admin/eval-runs/schedules/due/fire` (same mechanism as before — see
  [Architecture](./00-architecture.md) for the cron requirement).
- **Indicators** — four tiles aggregated over the most recent scheduled runs
  (`agent_eval_runs.schedule_id IS NOT NULL`, the same ≤50-row set the Recent-scheduled-runs
  table below shows; the Trend chart is a separate, wider query — see below): **latest score** (the newest scheduled run's
  score), **pass rate** (aggregate passed/total across every scheduled run's
  `results`), **samples** (total scenario/result count across every scheduled run),
  and **drift vs previous run** (latest score minus the previous scored run's score,
  ▲/▼/→).
- **Trend** — score / pass-rate over time, **scheduled runs only**
  (`data-scheduled-only="1"` — distinct from [Suites & Runs](#suites--runs)'s trend,
  which covers every run). Filterable by metric (score/pass-rate) and window
  (7d/30d/90d/1y); fed by the same `GET /admin/eval-runs/report.json` endpoint.
- **Recent scheduled runs** — `agent_eval_runs` where `schedule_id IS NOT NULL`,
  newest first, capped at 50; run id, profile, status, score, pass rate,
  started/finished. Tick ≥2 rows → **Compare selected** (same ephemeral matrix
  [Eval run compare](#eval-run-compare) uses).

**Backing tables** — `agent_eval_schedules`, `agent_eval_runs` (`schedule_id`),
`agent_profiles`, and `ai_chat_sessions` / `ai_chat_messages` (the sampled real chats).

---

### Bake-offs

`GET /admin/evals/bakeoffs` (list + create wizard) · `GET /admin/evals/bakeoffs/{bakeoff_id}` (detail) · `GET /admin/evals/bakeoffs/{bakeoff_id}/data.json` (chart poll, JSON) · `evals_bakeoffs.html` / `evals_bakeoff_detail.html` · `router.py:evals_bakeoffs()` / `evals_bakeoffs_create()` / `evals_bakeoff_detail()` / `evals_bakeoff_data_json()` · engine: `admin/services/bakeoffs.py`

Testing › **Model comparison** (partner wording: "options" for variants, "agent" for
profile, "repeat each case" for repeats). The list shows recent comparisons first; the
four-step wizard below opens in a wide drawer from **New comparison**. A **generic,
persisted** N-variant × M-suite comparison of **one profile**. Every agent can be
baked off: N `agent_profile_versions`
snapshots as variants (each optionally overriding model and/or gen-config), run against
M suites bound to that profile, with `repeats` attempts each for pass^k consistency.

**Shows**
- **New bake-off** wizard (`POST /admin/evals/bakeoffs`), four numbered steps:
  1. **Profile** — which agent to bake off.
  2. **Variants** (2 minimum) — repeatable rows, each: **Label**, **Profile version**
     (dropdown of that profile's `agent_profile_versions`, newest first, "— live —" for
     the unpinned current profile), optional **Model override**, and an optional
     **Gen-config override** (temperature / thinking level / max output tokens /
     top_p, in a collapsed `<details>`). "+ Add variant" clones the last row
     client-side; a variant row can't be removed below 2.
  3. **Suites** — checklist of suites **bound to that profile** (client-refreshed on
     Profile change from embedded `suites_by_profile` data — no extra request). Empty until at least one suite is bound to the profile from
     [Suites & Runs](#suites--runs).
  4. **Repeats** (1/3/5/10 — pass^k) + **Name** → **Run bake-off**.

  **Prerequisites — the two bindings a bake-off actually needs.** Both are DB columns
  on `agent_eval_suites`, both are silent when missing, and both have hidden empty
  states this list makes explicit:

  - **`profile_id` (suite → profile binding).** Step 3 lists ONLY suites whose
    `profile_id` resolves to the picked profile's logical `(agent_kind, slug)`
    identity — a suite with a NULL `profile_id` appears in **no** profile's list, so
    an unbound suite makes the wizard look like there are simply no suites. Bind it
    from the suite editor's Profile picker ([Suites & Runs](#suites--runs)). The boot
    seeder (`admin/seed_eval_suites.py`) binds every starter suite to its kind's
    **active** profile (same `(agent_kind, environment, status='active')` resolution
    the runtime uses) at insert time. For rows seeded before this existed, the
    backfill is **one-shot and pristine-rows-only**: it fills a NULL `profile_id`
    only on a row never modified since seeding (`updated_at == created_at`), and its
    own write bumps `updated_at`, so each row is touched at most once, ever. Any
    operator save — including deliberately clearing the binding to "unassigned" —
    also bumps `updated_at`, after which the seeder never touches that suite again.
  - **`evaluator_model_id` (judge model).** Every **judged** suite — any
    `runner_kind` without a deterministic scorer of its own (a plugin's runner kind
    may bring one) — must name an evaluator model, or bake-off creation is refused by
    pre-flight. Set it in the suite editor's **Judge model** field. Any model row the
    engine can call works (`admin/e2e_runner._build_judge`, local Ollama included);
    the pre-flight refuses only a provider the engine cannot build.
    The boot seeder fills this on judged starter suites (preferring the main model
    of a profile whose kind the plugin names, when judge-capable, then any active Gemini, then any active
    Claude row — env-scoped to the seeder's environment) and backfills a NULL under
    the same one-shot pristine-rows-only gate as `profile_id` above (a suite an
    operator has saved or de-judged is never re-filled); when NO judge-capable model
    row exists in `agent_models`, the suite stays evaluator-less and the operator must
    add such a model row + pick it, or bake-offs on that suite cannot run.

  Two pre-flight guards run **before anything is written**, so a bad input fails clean
  with one error instead of a half-spawned bake-off: every picked judged suite needs an
  evaluator model (the error names the offending suites and points at the suite
  editor), and every variant's pinned profile-version must actually belong to
  the chosen profile. A `ValueError` re-renders the wizard with the error (`error` +
  `posted` context) instead of a 500.
- **Recent bake-offs** — table of `agent_bakeoffs` rows: name (links to detail),
  profile, variant count, suite count, status (running/complete/errored), created.

**Detail** (`/admin/evals/bakeoffs/{bakeoff_id}`) — leads with a **Recommended model**
banner once the comparison is complete (`_bakeoff_recommendation()`: most cases passed,
then higher score, then lower known cost per case; names the runner-up and the gap in
points / cost ratio), or "Still running — X leads so far". Then a results table with
the decision columns (score, cases passed, reliable-every-repeat when repeated, full
answer median, cost per case) and **All measures** under a disclosure, the charts, and
a **Runs** list linking every spawned eval run (it used to be referenced by the errored
banner without existing). Never-captured cost/tokens/latency are blanked to `—` by
`_blank_unknowns()` (via the matrix's `priced_scenarios`) so no winner is crowned on a
zero that means "unknown", and the scatter leaves an unpriced option off the plot
("cost not recorded" in its legend). While running, the chart poller reloads the page
**once** when the status settles, so the server-rendered banner/table/runs update too.
A malformed id is a 404 page (`data.json` too).
- **Suite-focus filter** — "All suites (aggregated)" (rolls up
  `build_variant_matrix().by_variant` across every suite in the bake-off) or one
  specific suite (its own `(suite, variant)` rows) via `?suite_id=`.
- **Matrix** — variants × metrics table: mean score, pass rate, pass^k (once the
  matrix engine computes it), P50/P95 TTFT, P50/P95 latency, tokens in/out, total cost,
  mean cost — tokens in/out appear in both the per-suite focused view and the default
  aggregated `by_variant` view — the winning variant per metric (by that metric's higher/lower-is-better
  direction) is bolded in the success color; informational-only columns (token counts)
  never get a winner highlight, and a metric needs ≥2 comparable values to have one at
  all.
- **Charts** (`evals-bakeoff-chart.js`, laid out in the shared `.chart-grid` — 2 columns
  desktop, 1 column ≤900px, 3 columns ≥1480px): a grouped bar (mean score + pass rate
  per variant) and a cost-vs-quality scatter (mean cost vs mean score, one point per
  variant). **The charts auto-refresh while the bake-off is running** — they poll
  `GET /admin/evals/bakeoffs/{id}/data.json?suite_id=` every ~4s and re-render until
  `status` settles to `complete`/`errored` (polling pauses while the browser tab is
  hidden and resumes on visibility). **The matrix table above is rendered server-side
  only and does not live-update** — refresh the page to pick up newly-completed runs
  in it.
- `status` is recomputed on every read from the linked runs' statuses (`complete` iff
  every linked run has reached a terminal status, else `running`) unless the row is
  already stuck `errored` (**sticky** — set when the create-time spawn loop failed
  partway; never flips back to `complete` just because the runs that did spawn
  finished).

**Actions** — Run bake-off (create). Nothing to edit/delete on this screen — a spawned
run is still individually visible/cancellable from
[Eval run detail](#eval-run-detail).

**Backing tables** — `agent_bakeoffs` (`variants` jsonb, `suite_ids` uuid[], `status`,
`repeats`), `agent_eval_runs` (`bakeoff_id`, `profile_version_id`), `agent_profiles`,
`agent_profile_versions`.

### Display conventions

Shared by every Testing screen (`admin/testing_view.py`, installed as Jinja filters on
both template envs; `_components/status_chip.html`):

- **Percent** for every rate, score and target (`pct`): `79%`, `target 90%`; changes in
  **points** (`pts`): `+4 pts` / `−3 pts`, green/red.
- **One status vocabulary** (`status_label` / `run_status`): Passed · Failed · Error ·
  Running · Queued · Cancelled · Skipped · Not run — for eval runs, cases, pytest runs,
  E2E runs, voice checks and bake-offs alike.
- **Unknown is `—`**, never a confident 0 (cost, tokens, latency the runner didn't record).
- **Names, not ids**: suites and cases are humanised (`support_hallucination_traps` →
  "Support hallucination traps"), agents by display name; raw ids only as muted
  technical names or under Technical/Advanced.
- **Plain words** for engineering terms: runner kinds, gen-config, pass^k, sim/live,
  check types (`no_uuid` → "Read out an internal id", `want` → "expected") — the raw value is
  kept under an Advanced/Technical disclosure.
- Times are relative (`yesterday`, `10 days ago`) with the absolute UTC stamp on hover.

---

## Eval run detail

`GET /admin/evals/runs/{run_id}` · `eval_run_detail.html` · `router.py:evals_run_detail()` · `eval_handlers.py:build_case_rows()`

**Verdict first (B3).** Title = the humanised suite name (subtitle: agent name + time),
breadcrumb Testing › Runs › suite › time. A **verdict banner**: status, the score as a
big percent, `target 90%`, the change since the previous comparable run of the same
suite + agent (`−3 pts since last run`), one sentence ("45 of 57 cases passed. 11 pts
below the target; 4 cases broke and 1 got fixed since the previous run."), a
passed/flaky/failed bar, and actions **Compare with previous** (→
`/admin/evals/compare?runs=<prev>,<this>`), **Run again** (same suite + agent +
tenant), **Open test suite**. Four KPIs: cases passed, typical response time, cost,
run time — cost/tokens the runner never recorded show `—` (`run_detail_kpis()` returns
None, not 0).

**Cases** are one collapsed row each, **failed first** (then flaky, then passed;
within failures, the ones that **broke** since the previous run lead), with the
**judge's reason or the first failed check on one line** (`testing_view.case_reason()`:
judge reason → run error → limit breach → failed check in words, e.g. "Gave the wrong
figure — expected 0.0" → missing proposals / tools / wording). A segmented filter —
**Failed only** (default when anything failed; `?show=failed|broke|passed|all`) ·
**Broke since last run** · **Passed** · **All**. Expanding a case shows *Why it failed*
(each failed check in words, limit breaches, missing words/tools), question/expected
only when set, the conversation as bubbles (`dir="auto"`, the flattened duplicate
transcript is gone) or the answer, per-attempt status for repeats, and under **More
detail** the call chips, all checks, actions offered, per-turn timing and the raw
result JSON. pass@k / pass^k (as "Reliable cases" with a plain explanation), TTFT/TTLT,
tokens, iterations, ids, run settings, the latency/judge charts, Laminar spans and the
"compare with another run" picker moved under **Technical details**. A malformed run id
is a 404 page (was a 500).

**Each case links to the agent's own trace** (2026-10-14). The runner mints an
`eval_ref` per case attempt and sends it, with the run id, as the `X-WAJ-Eval-Ref` /
`X-WAJ-Eval-Run` headers on every request it makes (`admin/evals.py:_override_headers`).
The engine stamps them on its request root span as `waj.eval.ref` / `waj.eval.run_id`
(`core/channel_resolver.eval_trace_attrs`, UUID-shaped values only). The run detail
page resolves ref → trace ids in one ClickHouse query
(`observability.eval_trace_ids`) and shows **See how the agent worked it out** under
each attempt's answer: it opens Conversations on that trace, with the model's
messages, tool calls and output. A multi-turn case links one trace per turn. A case
attempt with no trace says so — the agent stopped before replying, or that
environment records no traces (staging records them since 2026-10-14). Runs from
before the correlation have no `eval_ref` and show no link. Over MCP,
`eval_runs(action="get")` adds `trace_ids` to each failing case. The run-level
**Run trace** link is the runner's own `eval_suite::<suite>` trace (timing and
result per case). A case that errored (a timeout, a non-2xx) gets an `error` event
on its `eval_scenario::*` span, so the error is visible in that trace as well.

**Everything the page shows** (arranged as above) — a status card (status + score chips, pass/fail counts, and — when
`repeats > 1` — **"N scenarios · M attempts"** rather than miscounting attempts as
scenarios; timestamps, suite / profile ids, Laminar trace link, collapsible trigger
context); a **run-level KPI strip** aggregated from the per-scenario results (`run_kpis`
from `eval_handlers.run_detail_kpis()`: **pass@1 · pass@k · pass^k · attempts**, latency p50,
TTFT/TTLT p50, total tokens in/out, total cost, avg iterations, avg tool calls; with
repeats, a **per-category pass@k / pass^k table** when the suite has more than one
category). k = the attempts per scenario. **pass@k** = share of scenarios that passed at
least once in k attempts (what the agent *can* do); **pass^k** = share that passed all k
(what it does *reliably*). Unbiased estimators in `admin/services/eval_metrics.py`:
pass@k = 1 − C(n−c,k)/C(n,k), pass^k = C(c,k)/C(n,k) per scenario, averaged;
**charts** (pass/fail doughnut, per-scenario latency bars, judge-score histogram with
threshold line — fed by a **minimal scalar projection** `run_chart_data()`, not the full
results, so the embedded JSON stays parseable); a **Laminar spans** table (deep-linking
into the explorer, rendered with `data-tabulator-render="basic"` to avoid the
virtual-DOM resize loop); and a **per-scenario breakdown** grouped **one card per
scenario** (`group_results_by_scenario()`): the card leads with a consistency verdict
(`pass^k N/N` · `flaky X/N` · `0/N`) or a single pass/fail, the question and expected
shown once, and **each attempt in its own collapsible `<details>`** with its verdict,
score, judge **reason (markdown)**, actual answer, missing substrings, and analyzer
notes — so the operator reads the verdict before scrolling into the transcript.

**Actions** — **Cancel** a running run (`POST /admin/jobs/{cancel_job_id}/cancel`);
open a sibling run in the compare view via the compare picker.

**Live status** — while the run is queued/running, `eval-run-detail-poll.js` polls
`GET /admin/eval-runs/{run_id}/status.json` and updates the status / score chips **in
place** (no scroll jump). When the run settles it reloads **once** so the per-scenario
cards and Chart.js figures — which need the full results payload — render. This replaced
the old every-5s `setTimeout(location.reload)`.

**Backing tables** — `agent_eval_runs`, `agent_eval_suites`, `admin_jobs`, Laminar
spans (best-effort).

### Judge mode & context precedence

When a suite sets an evaluator model and a scenario has an expected answer, an LLM
judge scores 0–1 alongside the always-on substring check. Context resolves as
**per-scenario fields → per-run overrides → suite default**.

The judge model is resolved from the suite's `evaluator_model_id` →
`agent_models.model_string` (currently `gemini-3.5-flash`), so the
scoring model is configured per-suite via the **evaluator model** picker rather
than hard-coded.

#### Rubric-based partial credit scoring

Scoring follows the Anthropic evals guidance: **partial credit, outcome-focused,
per-criterion rubric with an escape hatch**.

- **Rubric judge** (`e2e_runner.py:_invoke_judge(rubric=…)`) — when a scenario
  carries a `rubric` list, the judge prompt asks the model to return per-criterion
  verdicts `{id, verdict: pass|fail|partial|unknown, score: 0..1, rationale}` plus
  `{overall_score, overall: pass|fail, rationale}`. `overall_score` is the
  weighted mean of criteria whose verdict is **not** `unknown` (the escape hatch —
  criteria where evidence is absent are excluded from the mean and surfaced in the
  run detail, rather than forcing a hallucinated verdict). `overall` is `pass` when
  `overall_score ≥ pass_threshold`.
- **Back-compat** — when no `rubric` is present, the judge uses a single "matches
  expected_answer" criterion and returns the same `{score, pass, rationale}` shape
  as before; existing suites are unaffected.
- **Substring check** — always-on, separate from the judge score; a scenario that
  satisfies the substring but has no judge returns score `1.0` (pass). A scenario
  with an `expected_answer` but **no judge** passes when the reply contains its
  words, and is otherwise **Not judged** (no score, out of the run's score) unless
  other deterministic checks grade it — see "No judge model" at the top. The E2E
  runner (`admin/e2e_runner.run_e2e`) does the same.

#### Mandatory judge

**Comparison runs require a judge.** A suite with no `evaluator_model_id` cannot be
used in a multi-run comparison: the runner raises a clear error so the operator
configures the evaluator before comparing. Each run records the judge model it used
(stamped in the Laminar span as `waj.eval.judge_model`) so the operator can see which
model graded which run in the comparison matrix.

---

## Eval run compare

`GET /admin/evals/compare?runs=…` · `eval_run_compare.html` · `eval_handlers.py:evals_compare()`

Multi-run comparison. Also reachable as `?a=…&b=…` for the classic two-run form (also the destination of an [A/B prompt launch](./02-configure.md#sub-views--popups)).

**Leads with "did the change help?" (B3).** The first run is the baseline, the last the
change: a verdict banner (`84% → 79%  −5 pts`, "The change made things worse … 1 case
fixed, 4 broke, 8 still failing", cost change when both runs are priced), a "which
runs" table (suite and agent names, result, score vs target, cases passed), then
**What changed, case by case** — **Fixed / Broke / Still failing** columns (from
`_compare_runs()` between baseline and change, same suite only) with each case's
one-line reason — and only then the numbers: cases passed, judge score, full-answer
median and cost in percent / seconds / dollars with a **Difference** column, the rest
(pass@k, reliable, TTFT, tokens, iterations, tool calls) and the cost/latency charts
(unrecorded cost is a gap, never a $0 bar) under **Technical metrics**. Fewer than two
runs → the portal's 400 page ("Pick at least two runs to compare"); a malformed or
missing id → 404 page.

**Voice-check runs** compare too: a run id that is not in `agent_eval_runs` is looked
up in `ai_voice_scenario_runs` and mapped to the eval-run shape (each *checked* call
step — `expect_transcript` / `expect_frame` / `http_check` — is a case, keyed
"Step N · what it expected"), so two runs of the same check read **What changed, step by
step** (fixed / broke / still failing), and metrics with no voice data (judge, cost,
latency) are dropped. Mixing a voice-check run with a text run is refused with the
portal's 400 page and a reason; on the Runs list the **Compare selected** button is
disabled with the same reason as soon as the picks mix types.

### Compare runs (`/admin/evals/compare?runs=…`)

- **What it shows:** an N-column matrix over selected eval runs. Each column header
  shows the run's `variant_label` plus the **model name** (`model_string` derived from
  `context.model_override.model_string`) and **prompt version** (small sub-label),
  so A/B comparisons are self-describing. Rows are grouped into two verticals:
  - *Quality rows* (per suite): **pass@1** (fraction of scenarios that passed),
    **pass@k** (share of scenarios that passed at least one of the k attempts),
    **pass^k** (share that passed every attempt — the consistency score), **mean score**
    (mean partial-credit `overall_score`), and **mean judge score**
    (mean of raw judge scores for comparison with prior-format runs).
  - *Performance/cost rows* (global): **iterations** (mean LLM turns per scenario),
    **tool calls** (mean tool-call events per scenario), **time-to-first-token p50/p95**
    (ms), **time-to-last-token p50/p95** (ms, labelled `total` internally), **total
    cost** (USD, from `core/pricing.py`), and **total tokens**.
  Best value per row is highlighted. Bar charts for TTFT and cost below the matrix.
- **Key actions:** **Compare with previous** on a run page; tick ≥2 runs on the Runs tab → "Compare selected"; or hit
  `?runs=id1,id2,...` directly. Each column links to that run's detail page.
- **Sub-views & popups:** none — read-only, computed live from `results`/`context` jsonb.
- **Backing tables:** `agent_eval_runs` (`results`/`context` jsonb). No new table.

The classic two-run view (`?a=…&b=…`) shows three header cards (Run A, B−A delta, Run B), a per-scenario diff table (A/B scores, Δ score, A/B latency, Δ latency, result: gained / regressed / held / only-A / only-B) sorted by largest score divergence, and collapsible per-scenario A-vs-B actual outputs.

---

## Voice scenarios — live-call acceptance

**Targeting an agent built in the portal.** A generic agent is called at its own
address, so a check for one sets **Agent type** `custom` and **Call path**
`/v1/agents/<slug>/voice` (Advanced). The run connects as the operator who started
it: their token is sent as the bearer header, only to this engine's own edge (with a
Target override, pass `Authorization` in the run's headers instead). A `voice_live`
test suite needs no path: the runner derives the address from the agent's slug and
pins the profile under test (`admin/services/test_chat.voice_target`), so a draft can
be tested. The agent's Voice tab also has a **Call** button for a call by hand. In
`sim` mode no realtime key is needed, but the agent must still have voice turned on
and a realtime model picked.

Voice-check runs also appear in Testing › **Runs** (newest-first with text runs, a
**Voice** chip, score = share of checked steps passed against a 100% target, change vs
the check's previous call; filter **Type: Text tests / Voice checks**, and each check is
selectable under the **Voice checks** group of the Test suite filter as
`voice:<slug>`). Opening one shows `voice_run_detail.html` at the usual
`/admin/evals/runs/{run_id}`: verdict banner, **Compare with previous**, **Run again**,
the call step by step (caller lines, expectations with Passed/Failed and Broke/Fixed vs
the previous call), and the captured transcript tail. Admin MCP: `eval_runs`
`list`/`get`/`compare` take voice-check runs (`kind`: text | voice) and `overview`
returns the per-agent cards with the voice row.

Testing › **Voice checks**. Results first (recent runs: check name, Passed/Failed/…,
steps passed with the failed step's check in words), then the checks (title first,
slug as a muted technical name, agent type, language, steps, **Run** / **Delete** and a
**Run against another server** disclosure for `target` + headers), then **New voice
check** in a drawer. Infra details (`WS /voice/live`, `AZURE_SPEECH_KEY`, the seeder)
moved under **Technical notes**.

`GET /admin/voice-scenarios` · `voice_scenarios.html` ·
`router.py:voice_scenarios_screen()` · runner: `admin/voice_scenario_runner.py`

Place a **real duplex voice call** against the engine's own `WS /voice/live`
edge and pass/fail on observable behaviour — the portal successor to an
operator-laptop harness, with the
scenario definitions moved into config (`ai_voice_scenarios`) so a new
tenant's acceptance calls need no code. Caller speech is synthesized with the
same Azure Speech TTS the turn-based edge uses, so runs need
`AZURE_SPEECH_KEY` on the service — without it a run lands as
**skipped**, never errors the portal.

**Steps** (ordered JSON array per scenario; shapes in the runner docstring):
`say` (synthesize + stream at real-time pace + trailing silence so semantic
VAD closes the turn) · `expect_transcript` (role + regex within a deadline)
· `expect_frame` (any other frame type — `display`, `chart`, `interrupted`)
· `http_check` (re-read backing state over HTTP and regex the body — the
IBAN-fabrication lesson: assert what **happened**, not how it was phrased)
· `wait`.

**Actions** — **Run** launches a tracked background job
(`admin_jobs` kind `other`, label `voice-scenario:<slug>`) that records an
`ai_voice_scenario_runs` row through `running → passed/failed/error/skipped`;
the recent-runs table shows per-step check results. The job is pinned to the
environment it was launched for (the portal passes the switcher env, the MCP
tool its `env=` param), so the scenario lookup and the run row resolve the
SAME branch this screen reads — a cross-env run can actually find its
scenario and its result shows up where it was launched from. Create/edit posts a JSON
steps array; invalid step types are rejected at save. The upsert is **partial**
(2026-08-18), matching the MCP tool's `set`: a blank `title` / `locale` /
`notes` is omitted rather than written as NULL. It used to be a full overwrite,
and since this screen has one shared create form and no per-row editor,
re-submitting an existing slug to fix its steps also cleared those three
columns — and did so only over the portal, never over MCP, so the same edit had
different consequences depending on which surface it arrived from. `enabled` is
the deliberate exception: the checkbox is always rendered here, so absent means
the operator unchecked it, and unchecking must keep disabling the scenario. Starter
scenarios (greeting-on-connect, tool-free spoken QA) seed at boot
(`admin/seed_voice_scenarios.py`, insert-only on slug). Deep protected-flow
scenarios that need a portal login cookie are authored per tenant — the
cookie is secret material and never stored in the scenario row; pass
per-invocation headers via the MCP tool instead (`run`'s `headers` param,
forwarded to the WS connection and merged into each `http_check` step's own
headers — see below).

Mirrored by the admin MCP `voice_scenarios` tool
(list/get/set/delete/run/runs; `run` takes an optional `target` for a remote
deployment, e.g. the demo VM, and an optional `headers` for the
per-invocation cookie/header passthrough above). The portal's own
`POST /admin/voice-scenarios/{slug}/run` route accepts the same `target`/
`headers_json` so the two surfaces stay in parity; on the screen they sit under
each check's **Run against another server** disclosure.

## Tests — pytest viewer

Testing › **Engineering** (staff-only) · `GET /admin/tests` · `tests.html` · `router.py:tests_screen()`

Run the project's pytest suites from the portal.

**Shows** — a multi-select of suites (smoke [default], unit_runtime, unit_admin, all,
plus per-file suites), a recent-runs table (when, suite, status, pass/fail/skip,
duration) linking to detail, **Recent agent E2E runs** (`agent_e2e_runs`, last 20,
agent name, environment, result, passed/failed — linking to `/admin/e2e/runs/{id}`;
the runs are still started from an agent's page), and the
[Test Fixtures](#test-fixtures) section.

**Actions** — **Run selected** (`POST /admin/tests/run`) runs suites sequentially, each
shelling out to `pytest … -q --no-cov --tb=short` and recording an `agent_test_runs`
row.

### Test run detail
`GET /admin/tests/{run_id}` · `test_run_detail.html` · `router.py:test_run_detail()`
Status + counts, a per-test outcomes table (Passed / Failed / Error / Skipped, node id, message),
and captured stdout (truncated to 50KB).

**Backing table** — `agent_test_runs`.

---

## Test Fixtures

Embedded in the Tests screen (`#fixtures`) · `test_fixtures_handlers.py`

A vault of reusable test inputs (JWTs, tenant ids, sample payloads) per environment
(**staging** / **production**), exportable as `.env.test` so pytest can source them.
Names follow `WAJ_TEST_*`.

**Shows / actions**
- Add / edit / delete fixtures (name validated `^[a-z][a-z0-9_]{0,62}$`, kind one of
  jwt / uuid / string / json / secret, env, value, description).
- `jwt` / `secret` values are **masked** in the list and audit log; a reveal link
  unmasks; a copy button copies the raw value.
- **Download `.env.test`** — `GET /admin/test-fixtures/export?env=…` emits
  `WAJ_TEST_{NAME}=value` lines for the chosen env.

**Backing table** — `test_fixtures` (unique on name + environment; service-role only,
never exposed to the runtime).
