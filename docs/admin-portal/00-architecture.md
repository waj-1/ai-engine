# Architecture

**Docs › Architecture**

How the admin portal and the agent runtime fit together. Read this first — every
screen page assumes the model described here.

---

## Portal as source of truth

Every agent is defined by **one row in `agent_profiles`** per `(agent_kind, environment)`
(for the `custom` kind, per `(agent_kind, slug, environment)` — see
[Runtime agents](#runtime-agents)). That row points at everything the agent needs:

- **Model** — `model_main_id` → `agent_models`. (The former per-profile light model is retired; auxiliaries use sub-agent profiles or fall back to the main model.)
- **Prompts** — `prompt_version_ids` (a slot→version map, e.g. `{"system": <uuid>, "title": <uuid>}`) → `agent_prompt_versions`.
- **Tools** — resolved from the MCPs attached to a profile (`agent_profile_mcp_servers` → `agent_mcp_servers` → `agent_mcp_tools` → `agent_tools`); Python tools sync from code, HTTP tools are authored inside an internal MCP bundle.
- **Knowledge** — `data_source_ids[]` → `agent_data_sources` (pgvector RAG, Supabase tables, HTTP, document imports).
- **MCP servers** — `mcp_server_ids[]` → `agent_mcp_servers`.
- **Endpoint** — `endpoint_path` (the FastAPI route this profile serves, e.g. `/v1/agents/{slug}/chat` for a custom agent).
- **Sub-agents** — `downstream_profile_ids[]` / `parent_profile_id` (title and suggestion generators, delegatable specialists, …).
- **Escalation** — `escalation_policy` (hand-off thresholds).

**Editing the portal reflects on the next request.** The runtime caches the active
profile per `(agent_kind, environment)` with a short TTL (≈60s), so **staging** edits take
effect within the cache window without a redeploy.

---

## Runtime agents

`RuntimeAgent` (`runtime/agent.py`) is the generic, profile-driven base. On each
request it:

1. Loads the profile for the request (the kind's active profile, or for a custom
   agent the profile named by its slug).
2. Builds the LLM from `model_main_id`. (Cheap auxiliaries — title / suggestions — run on their own sub-agent profiles, not a separate light model.)
3. Resolves the system prompt from `prompt_version_ids` (falling back to a filesystem
   default when unset), resolves the profile's bound tools from its MCP attachments
   (`resolve_mcp_tools_for_profile`), applies per-agent binding hooks, and returns the
   prompt + tools to the streaming loop.

**Agent kinds the engine ships** (each has a module under `runtime/agents/`):

| Kind | Role |
|------|------|
| `custom` | The generic, configuration-only agent (`runtime/agents/custom.py`). It has no behaviour of its own: instructions, model, tools (the profile's attached MCP servers and bundles), knowledge (`data_source_ids` → `search_knowledge`) and sub-agents all come from the row. Served at `POST /v1/agents/{slug}/chat` (`endpoints/agents_v1.py`) when `ENABLE_GENERIC_AGENTS=1`. Many profiles share the kind and are told apart by slug, so the profile is always resolved by `(custom, slug, environment)` and a mistyped slug fails closed instead of falling back to another agent. Each custom agent owns its instructions: its prompt row is `(custom, "<slug>:system")`. |
| `agents_support`, `agents_inquiries`, `agents_collection`, `agents_sales` | The public AI-agents demo at `/agents` (`runtime/agents/agents_demo.py`, `endpoints/agents_portal.py`): four agents for a fictional provider, with text chat and duplex voice. Seeded by `admin/seed_agents_demo.py` where a Gemini or Azure OpenAI key is set. |

**A deployment's plugin may add agent kinds.** Product agents attach through the plugin
seam (`engine_plugins.PLUGINS`, `core/plugins.py`; the hook list is `core.plugins.HOOKS`):
the `agent_catalog` hook adds kinds to every picker and Testing screen, `app` adds their
routes, `tool_factories` their Python tool pools, `prompt_seeds` / `data_source_seeds` /
`eval_suite_seeds` / `model_seeds` what is seeded for them at boot, and `voice_builders`
their duplex-voice builders. A build with no plugin ships only the kinds above.

Sub-agent profiles (title / suggestions generators, specialists) each carry their own
model + prompt and are wired to their parent via `downstream_profile_ids`. A parent with
no such sub-agent falls back to the file-system prompt + its own **main** model.

### Generated tool catalog (`{available_tools}`, Phase 2)

Agent prompts used to hand-write their own "## Tools available" bullet list, kept in
sync with each tool's real `description` by hand. `RuntimeAgent.prepare()` now fills
that section: it resolves the request's bound tool set (the same list `tools_by_name`
gets built from), renders it through `runtime/prompts/_catalog.py:render_tool_catalog`
into a `- \`name\` — description` markdown list, and splices it into the system prompt
wherever the prompt contains the literal `{available_tools}` placeholder (a targeted
`str.replace`, not a `format_map` pass — see the comment in `runtime/agent.py:prepare()`
for why: prompts with bare `{...}` JSON example blocks would make `format_map` throw).
Prompts that don't include the placeholder are unaffected.

Because the catalog is generated **from the bound set** rather than typed by hand, a
prompt can no longer advertise a tool the profile never bound (which used to produce
"Tool not found" at run time): whatever the prompt advertises **is** the bound set, by
construction.

**Rollout gate — this does not take effect the moment the code merges.**
`seed_prompts` is **insert-only**: it creates a slug's `agent_prompt_versions` row
once and never overwrites an existing one (see `admin/seed_prompts.py`), and staging
reads the live profile's `prompt_version_ids` while production reads a frozen
compile-on-promote snapshot (see "Two environments" below). So a currently-running
staging or production prompt keeps serving whatever version it was seeded/promoted
with — changing a prompt *file* to use `{available_tools}` is inert for that prompt
until it is rolled forward by one of:

- a **fresh-DB re-seed** (new environment, or a deliberate reset — `seed_prompts` sees
  no existing version and inserts the new file content), or
- a **portal new-version + serve** (an operator edits/creates a new prompt version in
  the Agents screen and marks it served), or
- a **re-promote** (staging already carries the new version; promoting compiles it
  into the next `agent_profile_versions` snapshot for production).

An agent whose system prompt is **not** seeded into `agent_prompt_versions` (no pinned
`prompt_version_ids["system"]`) falls back to its prompt file on every request, so for
that agent a file change is live on the next deploy.

**`usage_guidance` shipped in a later phase** (see [Built-in bundle MCPs →
delegation and `usage_guidance`](#built-in-bundle-mcps-phase-1a-retrofit) below): a
tool's or bundle's `usage_guidance` now renders as an indented line under that tool's
entry when the tool is bound, so the catalog is no longer strictly flat. MCP-style
grouping and `@`-mention tool *references* (distinct from the `@`-mention prompt
editor described in [Configure](./02-configure.md#agent-profiles)) remain later-phase
work.

### Request-scoped model override (`X-WAJ-Model-Override`)

The runtime supports a **per-request model override** via the internal
`X-WAJ-Model-Override` HTTP header. When present, `runtime.agent._build_llm` merges
the override over the profile's `model_main` for that request only (the profile object
is never mutated) — mirroring the per-request prompt-override mechanism. The
header carries a resolved partial model dict — `provider`, `model_string`,
`temperature`, `extra_config` (and optionally `max_output_tokens`) — built by the
eval-run trigger's `_build_model_override()`; it does **not** carry a `model_id`. This
is how [Variant overrides](./03-test.md#variant-overrides-on-the-run-trigger) run a
different model without mutating the profile.

**Trust boundary:** like `X-WAJ-Prompt-Override`, this header is honoured only for an
operator (`main._request_is_operator` — a signature-verified superuser JWT); a
non-operator's header is ignored and the request runs on the profile's own model. It
only swaps which LLM config is built for the request — it cannot escalate tenancy or
read other tenants' data.

### Request-scoped profile-version pin (`X-WAJ-Profile-Version`)

**Sibling: the profile pin, `X-WAJ-Profile-Id`.** The admin Test drawer and the MCP
`chat` tool send the profile being viewed, so a draft copy or a specialist answers its
own test chat instead of the kind's active profile — no saved snapshot needed. Same
middleware and same operator gate; the value must be a UUID.
`AgentProfileLoader.load_pinned(profile_id, agent_kind)` builds the row the way ITS
OWN environment builds it (live in dev/staging — where every draft lands — the
served/promoted snapshot in prod) and returns `None` (→ the usual profile serves,
WARNING logged) for an unknown/inactive id, a template, or a profile of a different
`agent_kind` than the endpoint: it swaps config within a kind, never the agent class
or the tenant. A version pin wins over a profile pin. Not cached.

Eval runs and [bake-off](./03-test.md#bake-offs) variants need to run a request against
one **specific, immutable** `agent_profile_versions` snapshot — not just override the
model, but reproduce the whole config (prompt, tools, KB, MCP attachments) as it was
frozen at a particular promotion. This reuses the compile-on-promote snapshot rather
than inventing a second resolution path:

- **`core/profile_version_resolver.py`** — a `ContextVar[Optional[str]]` holding a
  target `version_id`, with `set_profile_version` / `get_profile_version` /
  `clear_profile_version` helpers — the same shape as `core/model_resolver.py`.
- **`profile_version_override_middleware` (`main.py`)** — reads the internal
  `X-WAJ-Profile-Version` header and parks the value in the ContextVar for the
  request. **Operator-gated** exactly like `X-WAJ-Model-Override` /
  `X-WAJ-Prompt-Override`: a non-operator or malformed header is silently ignored and
  the request proceeds on the live profile, unaffected.
- **`core.agent_profile.AgentProfileLoader.load_version(version_id)`** — builds a
  `Profile` from one `agent_profile_versions` row by reusing the same
  `_profile_from_snapshot` rehydration the prod compile-on-promote path already uses
  (see [Compile-on-promote](#compile-on-promote)) — not a second, parallel
  implementation. Raises `MissingSnapshotError` for an unknown `version_id`. Not
  cached (arbitrary, per-request version, unlike the ~60s active-profile cache).
- **`runtime.agent.RuntimeAgent.__init__`** — the single choke point: if
  `get_profile_version()` is set, the agent is built from `load_version(...)` instead
  of the usual profile load. The existing `model_override` / gen-config overrides
  still layer on top afterward, so a bake-off variant can pin a version **and**
  additionally swap its model.
- **Eval runner** (`admin/evals.py:_invoke_endpoint`) — sends
  `X-WAJ-Profile-Version` alongside `X-WAJ-Model-Override` /
  `X-WAJ-Prompt-Override` whenever the run's `profile_version_id` is set (a
  [Run-now variant override](./03-test.md#variant-overrides-on-the-run-trigger) or a
  spawned [bake-off](./03-test.md#bake-offs) run).

Back-compat: no header (or a non-operator caller) → the ContextVar stays unset → the
agent loads the live/active profile exactly as before this capability existed.

### Served-version pin (`agent_profiles.served_version_id`)

Where the previous section is a **per-request, ephemeral** override for eval/bake-off
reproduction, this is a **standing, per-(profile, environment) pin** an operator sets
from the portal (or MCP) that holds every request against one snapshot until it's
explicitly cleared — the mechanism behind a manual rollback/hold on the [agents
screen's version bar](./02-configure.md#agent-profiles).

- **Column**: `agent_profiles.served_version_id`, nullable FK →
  `agent_profile_versions.version_id` (`ON DELETE SET NULL`). See [Data Model →
  agent_profiles](./data-model.md#agent_profiles).
- **Resolution — `core.agent_profile.AgentProfileLoader._build_profile`.** All the
  row→Profile builders funnel through this one shared method — `_load_from_db` (the
  `load_active` path, i.e. every normal chat request), `load_delegate` (the primary,
  slug-based resolver, also what a custom agent uses), and `load_by_id` (the id-based
  delegation fallback) — so the pin is checked in **exactly one place** for all of them.
  The check runs at the very top, **before** the prod-bundle / `load_active_snapshot` /
  live-row branches below it: if `row["served_version_id"]` is set,
  `load_snapshot_by_version` fetches that snapshot and — if it belongs to the profile
  being built — `_profile_from_snapshot` returns it immediately, in **every**
  environment (not prod-only), short-circuiting the rest of `_build_profile`.
- **`NULL` is the default**, and skips the branch outright — resolution for an
  unpinned profile is unchanged.
- **Ownership-guarded, fails open to default (not to an error).** If the pinned
  `version_id` resolves to a snapshot whose `profile_id` does **not** match the row
  being built (a manual DB edit — `pin_served_version` itself refuses to write a
  mismatched pin, so this is defence in depth), `_build_profile` does **not** serve
  it: it logs a warning and falls through to the normal default resolution, exactly as
  if the pin were unset. The same fall-through happens if the pinned `version_id` no
  longer resolves to any snapshot (a pruned version). Serving the wrong profile's
  config on the hot chat path would be far worse than serving the environment's
  normal default.
- **Writer**: `admin.services.profile_versions.pin_served_version` (agents-screen
  version-bar Pin form / row buttons, or MCP `profile_versions action=pin`) is the
  only writer outside raw SQL. It re-verifies the ownership guard on write (a
  `version_id` belonging to a different profile raises immediately, so an operator
  sees the rejection at pin time) and calls
  `AgentProfileLoader.invalidate(agent_kind, environment)` so the pin (or its
  clearing) takes effect on the very next request, not after the ~60s cache TTL.
  `admin.services.profile_versions.save_version` / `restore_version` are separate
  writers of `agent_profile_versions` / the live `agent_profiles` row
  respectively — see [Compile-on-promote](#compile-on-promote) below and [Data
  Model → agent_profile_versions](./data-model.md#agent_profile_versions).

### Eval runner_kind registry (`chat` / `voice_live`)

Suites carry a `runner_kind` column (`agent_eval_suites.runner_kind`, default `chat`)
so a scenario can be invoked and scored by something other than the chat-SSE path,
without a bespoke pipeline per use case (`admin/services/eval_runners.py`). The engine
ships two:

- **`chat`** (default) — `_invoke_endpoint` POSTs the scenario at the profile's
  `endpoint_path` and reads a `text/event-stream` response; scoring is substring
  matching + the LLM judge + per-turn `checks` (see
  [Test → Judge mode & context precedence](./03-test.md#judge-mode--context-precedence)).
  `_invoke_endpoint` falls back to the raw response body when no SSE `answer` frames
  are found, so a plain-JSON endpoint also works through the same call.
- **`voice_live`** — dispatches to `admin/voice_eval_runner.py` instead of
  `_invoke_endpoint`, because a duplex WebSocket conversation isn't a single
  request/response. See
  [Test → Voice eval suites](./03-test.md#voice-eval-suites-runner_kind-voice_live).

**A deployment's plugin may add runner kinds and scorers** through the `eval_runners`
hook: `runners() -> {runner_kind: runner}` and `scorers() -> {assert_type: scorer}`. A
runner may replace the request for one scenario (`invoke`), play a multi-turn scenario
(`run_turns`), clean up after a run (`after_run`), read token usage from a non-SSE body
(`usage_from_output`), and name a `default_scorer` so its suites need no judge. A
scenario's scorer resolves from an explicit `assert.type` first, else from its runner's
default. A plugin may not replace `chat` or `voice_live`.

### Pricing layer (`core/pricing.py`)

`core/pricing.py` is the single resolution point for token cost calculations used by
eval runs and the comparison matrix:

1. **Auto / tokencost** (preferred) — if `agent_models.extra_config.pricing_mode`
   is `"auto"` (or unset), pricing is looked up from the
   [`tokencost`](https://github.com/AgentOps-AI/tokencost) library. The lookup key
   is `extra_config.tokencost_model` when set, otherwise `model_string`. The library
   covers all major providers.
2. **Manual fallback** — if `pricing_mode` is `"manual"` or `tokencost` does not
   recognise the key, the module falls back to `cost_per_1k_in` / `cost_per_1k_out`
   from the `agent_models` row. If both are null the cost is recorded as zero.

Eval-run `results[*].cost_usd` is populated by this module after each scenario
completes, so the comparison matrix's **Cost** row reflects real model rates.

### Model providers

The model factory builds these `provider` values:

| Want | Use |
|---|---|
| Gemini | `google_genai` + `GOOGLE_API_KEY` — also the default embedding provider |
| Claude | `anthropic` — the direct Anthropic API, `ANTHROPIC_API_KEY` (also what the eval / E2E Claude judge builds) |
| OpenAI | `openai` + `OPENAI_API_KEY` |
| Azure OpenAI deployments | `azure_openai` — see below |
| Any OpenAI-compatible endpoint | `openai` + `extra_config.base_url` — see below |

The Vertex provider values (`vertex_ai`, `anthropic_vertex`, `vertex_maas`) were removed
from the factory; a model row still carrying one is unbuildable — pick a supported
provider instead.

### Azure providers

| Provider | When to use |
|----------|-------------|
| `azure_openai` | First-party **Azure OpenAI** deployments. `model_string` is the **deployment name**, not the OpenAI model id. Needs `AZURE_OPENAI_ENDPOINT` + `AZURE_OPENAI_API_KEY` (and optionally `AZURE_OPENAI_API_VERSION`, default `2024-06-01`). |
| `openai` + `extra_config.base_url` | Any **OpenAI-compatible** endpoint. This is how the **Azure AI Foundry** catalog is reached — Foundry serves those models over the Azure AI Model Inference API at `https://<res>.services.ai.azure.com/models`, which takes the model **name in the request body**, NOT the `/openai/deployments/<name>` path `azure_openai` builds. Also works for vLLM, OpenRouter, etc. |

For an OpenAI-compatible row set two `extra_config` keys:

- `base_url` — the endpoint host.
- `api_key_env` — the **NAME** of the env var holding the credential (defaults to
  `OPENAI_API_KEY`). The name, not the key: `extra_config` is rendered in plain
  text on the Models screen, so a literal key would be exposed. The builder raises
  at construction if `base_url` is set and the named var is empty, rather than
  silently 401-ing on every call.

#### Reasoning models

Two `extra_config` knobs, honoured by both `openai` and `azure_openai`:

- `reasoning_effort` — `none|minimal|low|medium|high` (`none` turns reasoning off;
  some models need it to call tools on `/chat/completions`). The direct analogue of
  Gemini's `thinking_level`, sharing its vocabulary so a cross-provider bake-off
  compares like with like. Emitted **only when set**, because a non-reasoning
  deployment rejects the parameter.
- `omit_temperature` — reasoning models reject any non-default `temperature` and
  return HTTP 400. The seeder's default is `0.2`, so without this flag every such
  row fails on its first call. (The `anthropic` builder carries the same escape hatch.)

#### Pricing an Azure-hosted model

**Do not price it from a `tokencost` vendor key.** A vendor key prices the vendor's
*own* API; Azure Foundry can charge several times that for the same model. Look the
real meter up at `https://prices.azure.com/api/retail/prices` (Foundry models appear
under `serviceName = "Foundry Models"`) and set `pricing_mode: manual` with
`cost_per_1k_in` / `cost_per_1k_out`. Otherwise the eval comparison matrix's
**Cost** row silently understates what you actually pay.

---

## Deployment scopes

By default one engine process serves every agent kind it has, and the admin portal is
a control-plane over two environments (see below). A plugin may also define
**deployment scopes** (`core/deployment.py`, the `deployment` hook): a standalone
build that serves one tenant's agents under one public path prefix, with its own
portal name and colours, against a single environment.

- `scopes()` returns every scope the plugin defines; `active_scope()` the one this
  process runs as (read at call time from the plugin's own configuration), or `None`.
  An unrecognised value makes the engine **refuse to boot** (`main._validate_env_or_die`).
- On a scoped build the `deployment_gate` middleware (`main.py`) answers `404` for
  `/v1/` (the custom-agent API is not part of any scope), for every **other** scope's
  portal prefix, and for whatever route prefixes the plugin lists in
  `blocked_path_prefixes(scope)`. Infra routes and the scope's own prefix are never
  blocked. On a full build the gate is a no-op.
- The seeders and every admin screen narrow to the scope's agent kinds
  (`core.deployment.serves_kind`); a full build leaves every scope's kinds alone, so
  a scope's rows never land in the full build's catalog.
- The duplex voice socket mounts under the scope's `portal_prefix`
  (`core.deployment.mount_prefix()`).
- The portal takes the scope's name, mark and accent colours (`admin/nav.py:brand()`),
  hides the staff-only Settings entries, and sends `/admin/docs*` to `/admin/help`
  (see [README → Partner builds](./README.md)).

**With no plugin there are no scopes**: `active_scope()` is `None` and every function
in `core/deployment.py` answers for a full build. `tests/scoped_build.py` stands a scope
in for engine tests.

---

## Two environments: staging & production

The engine is **one control-plane** that manages and monitors **two** environments:
**staging** and **production**. (A legacy `dev` tier has been retired; the
`agent_environment` Postgres enum still contains the `dev` value but nothing reads or
writes it.)

Every admin screen — and every background job it spawns — reads/writes the **selected
env's branch**. The top-bar **environment switcher** shows **STAGING / PRODUCTION**,
indicates the active env on every screen, and switching it updates the screen's
contents. The selection **persists across navigation** via a cookie (`waj_admin_env`);
the default is **staging**.

**Single-environment (scoped) deployments.** A scoped build is not a control-plane —
one engine, one AI Supabase, every row seeded under that box's own `ENVIRONMENT`.
There, `core.deployment.single_env_deployment()` returns that env, both `current_env`
and `current_env_live` resolve to it, and the switcher is **hidden** (folded into
`hide_env_switcher` in `_base.html`) because its other tab is guaranteed empty. On such
a box the deployment's env **outranks both `?env=` and the `waj_admin_env` cookie**,
and no cookie is written: a selection the operator cannot then un-select (the
switcher is hidden, the cookie lives 30 days) would point every query at an
environment with zero rows. On the full control-plane, `?env=` and the cookie win as
usual. Note that `admin/` owns **more than one** `Jinja2Templates` instance and they do
**not** share globals: `_base.html` *calls* `single_env_deployment()`, so every such
env must register it or the screens it renders raise `UndefinedError`.

**How env selection is plumbed (request-scoped, not per-signature).** The
`persist_admin_env` middleware (`main.py`) resolves the selected env once per `/admin`
request (single-env deployment → `?env=` → `waj_admin_env` cookie → `staging`) and parks it in a request-scoped
ContextVar `core.auth._ADMIN_ENV` (same pattern as `_PROMPT_OVERRIDE` / `_MODEL_OVERRIDE`).
Env-scoped handlers call **`core.auth.admin_service_client()`**, which returns the service
client for that branch (`get_service_client_for_env(env)`), falling back to the
deployment's own branch (`get_service_client()`) when cross-env secrets aren't configured
or no env is set. Background jobs run on daemon threads that
don't inherit ContextVars, so `admin._jobs.start_tracked_job` captures the env at spawn and
re-sets it inside the worker — this is why cross-env **eval / e2e / KB-ingest / pytest /
voice-scenario** runs land on the correct branch (the voice-scenario runner additionally
pins its explicit `environment` kwarg, because the MCP `run` action arrives outside the
`/admin` middleware and the spawn capture sees no env there). The thread-pool fan-out
(`admin._query_batch.run_parallel`) copies the submitting request's context into every
pooled thunk for the same reason. **Writes follow the same rule as reads**: every request
handler resolves its client through `admin_service_client()` / `client_for(env)`,
including the `admin_jobs` tracker and `agent_audit_log` rows (an audit row lands on the
branch whose config it audits). Only inherently-local paths (boot seeders,
`db_snapshot`) keep using `get_service_client()` directly. (`client_for(env)` remains for
the handlers that thread `env` explicitly, e.g. `screens/agents.py`, and for rows that
name their own environment: an env-var or test-fixture **create** writes to the branch
of the form's `environment` field; it and `admin_service_client()` share the same
fallback logic.)

Only a deployment that carries both `STAGING_AI_SUPABASE_*` and `PROD_AI_SUPABASE_*`
reaches two separate projects; without them, both environments live in one AI project,
told apart by the `environment` column. Screens that inherently cover both —
**Environments, Deploy, Users & Roles, Audit Log, Docs** — hide the switcher
(`hide_env_switcher` template flag) and read each env's data through its own branch
client.

- **Staging is the live, editable environment.** It reads the **live** `agent_profiles`
  row — tools, sources, models and prompt versions are resolved fresh on every load, so
  admin edits appear immediately.
- **Production is immutable.** It reads a **compile-on-promote snapshot** in
  `agent_profile_versions`. The snapshot embeds resolved model rows, prompt *contents*,
  tool names and source rows — it never re-joins the config tables — so it can't drift.
- **Precompiled prod config bundle (optional).** On prod promote, every active prod
  profile's snapshot is compiled into one self-contained JSON bundle and pushed to the
  object path `ai-agent/prod-config/latest.json` in the engine's cloud storage
  (`runtime/prod_bundle.py` → `shared/cloud_storage.py`, selected by
  `CLOUD_STORAGE_BACKEND`). A prod process loads it into an in-memory registry at boot
  (`main.lifespan`) and `AgentProfileLoader` serves config from it — no config-DB
  resolution per request. With no bundle in storage the loader falls back to the
  `agent_profile_versions` DB-snapshot path. Staging never reads the bundle.

### Compile-on-promote

**Promote** is a single **Staging → Production** action (from `/admin/env` →
Environments, and from the agent detail). It calls a compiler that resolves all
dependencies and writes one self-contained row — the profile plus a new version — into
the **production branch**. After promotion, editing staging cannot change production
until the next promotion.

A **hard eval gate** blocks promotion (HTTP 422) if the profile's latest eval run is
below its suite's `pass_threshold`. A **Force** checkbox overrides it and is audited
separately as `EVAL_GATE_OVERRIDE`.

Promotion writes to the production branch via `client_for("production")` (a remote
Supabase project when the production AI-Supabase credentials are configured; otherwise a
same-DB write keyed by the `environment` column).

**Not the only writer of `agent_profile_versions`.**
`admin.services.profile_versions.save_version` (the agents screen's version-bar
**Save as new version** button / MCP `profile_versions action=save`) also inserts a
version row — in **either** environment, on demand, without promoting — by calling
the same `compile_profile_snapshot` this promote path uses, so a manually-saved
checkpoint has the identical self-contained shape as a promoted one and never
touches `served_version_id`. `restore_version` (**Restore** / `profile_versions
action=restore`) goes the other direction: it reads an existing version's snapshot
and copies its config fields back onto the live `agent_profiles` row (never profile
identity). References the snapshot carries that no longer exist in this env are
handled per kind: a missing **tool / KB-source / MCP-server** is dropped and
reported; a missing **model** is left pointing at the live row's current model
(never nulled — a profile must have one) and reported; a missing **prompt version**
is *preserved by re-creation* — `restore_version` inserts a **new
`agent_prompt_versions` row** from the snapshot's stored prompt text and points the
draft at it, so prompt content is never lost (the one case where restore writes to a
second table besides `agent_profiles`; it still never inserts a new
`agent_profile_versions` row itself). See [Served-version
pin](#served-version-pin-agent_profilesserved_version_id) above and [Configure →
Agent Profiles](./02-configure.md#agent-profiles).

---

## Boot-time seeding & sync

The FastAPI lifespan runs these idempotent, insert-if-missing steps on startup, in this
order (`main.py`, `_run_boot_seeders`):

1. **`seed_prompts`** — `agent_prompts` slots + initial `agent_prompt_versions` from code constants (the engine's own, plus a plugin's `prompt_seeds`).
2. **`sync_tools`** — walk each Python tool factory (a plugin's `tool_factories`), upsert `agent_tools` (name, schema, category, scope), mark missing tools deprecated.
3. **`seed_data_sources`** — the default RAG/data sources (a plugin's `data_source_seeds`; none without a plugin).
4. **`seed_agent_profiles`** — fill *empty* arrays on seeded **staging** profiles only (portal edits survive restarts).
5. **`seed_builtin_mcps`** — the built-in bundle MCPs described below. Runs after
   `seed_agent_profiles` because it needs both `agent_tools` and profiles with resolved
   `tool_ids` already in place.
6. **`seed_agent_models`** — the shared model catalog (plus a plugin's `model_seeds`).
7. A plugin's own seeders (`boot_seeders_after_models`).
8. **`seed_agents_demo`** — the `/agents` demo's tools, prompts, KB source, profiles and
   bundles (only where `GOOGLE_API_KEY` or `AZURE_OPENAI_API_KEY` is set).
9. **`seed_templates`** — the protected template **mold catalog** (`agent_profiles`
   rows with `status='template'`, `environment='staging'`, `is_active=false` — never
   served; users fork them via the `templates` tool / portal gallery). Runs **after**
   the model seeder so each mold's `model_main_id` resolves.
10. **`seed_eval_suites`** — starter eval suites (a plugin's `eval_suite_seeds`).
11. **`seed_voice_scenarios`** — starter live-voice scenarios (`ai_voice_scenarios`).
12. **`seed_env_vars`** — mirror `.env` into `agent_env_vars` for the **staging** row.
13. **`seed_channels`** — the default channel registry (a plugin's `channels` hook adds its own).

Because seeding only fills empty fields, restarts never clobber portal-made config. A
plugin can skip seeding on a given app (`skip_boot_seeders`).

---

## Built-in bundle MCPs (Phase 1a retrofit)

Every **factory** agent kind (a kind whose tools come from a Python tool factory, not
an external MCP) gets a **system-managed built-in bundle MCP** seeded at boot, named
`__builtin__:<kind>` (`admin/seed_builtin_mcps.py`, wired into the lifespan right after
`seed_agent_profiles`):

- **Membership** — one `agent_mcp_tools` row per non-deprecated `agent_tools` row whose
  `agent_kind` is the bundle's kind (or whose `additional_agent_kinds` contains it — how
  a sub-agent kind's tools join its parent kind's bundle). Ordering follows the kind's
  factory pool order, HTTP tools appended after
  (`admin/builtin_mcp_membership.py:ordered_membership`). Re-seeding **reconciles**
  membership to match the current tool set (adds/removes/reorders), so the bundle never
  drifts from `agent_tools`.
- **Attachment + pinned `tool_subset`** — every profile of the kind gets one
  `agent_profile_mcp_servers` row pointing at its kind's bundle, with `tool_subset`
  frozen to the **profile's current `tool_ids`** resolved to python tool names at seed
  time. Like `seed_agent_profiles`, this only creates an attachment when the profile has
  **none yet** — an admin who edits or removes an attachment through the portal is never
  overwritten by a restart.
- **Gating** — only kinds this build serves seed a bundle (`_seedable_kinds()`): a full
  build skips every deployment scope's kinds, and a scoped build seeds only its own.

**MCP resolution is the sole tool-resolution path.** At run time `prepare()` always
resolves a profile's bound tools from its MCP attachments
(`runtime/tools/_mcp_resolver.py:resolve_mcp_tools_for_profile`): an agent's factory
output is a *pool* and MCP bundle membership decides what binds. A non-fallback profile
binds exactly what its attachments name (a fallback profile with no DB row still serves
the agent's full hand-coded pool). Compile-on-promote freezes each profile's
`mcp_attachments` into the `agent_profile_versions` snapshot (schema v4), so production
always carries the data.

**Skills follow the same promote posture (snapshot schema v5).** A skill is a named,
versioned instruction bundle with progressive disclosure: `RuntimeAgent.prepare()`
injects a one-line-per-skill catalog right after the tool catalog (filling a literal
`{available_skills}` token when the prompt carries one, else appending a `## Skills`
section), inlines `always_on` bodies (cap 10), and binds the synthetic `read_skill`
tool so the model pulls full bodies on demand. Platform skills
(`agent_skills.business_id IS NULL`) are profile attachments frozen into the snapshot
as `skill_attachments` at promote; **tenant skills bypass snapshots by design** —
they belong to one business and resolve live per request with a 60s cache (the same
dynamic posture as user memory). Legacy (v4 and older) snapshots read as
`skill_attachments = []`.

**Delegation follows the same promote posture (snapshot schema v6).** A profile opts
a downstream sub-agent into delegation by setting `delegatable = true` on the
downstream row; `RuntimeAgent.prepare()` then binds one synthetic `ask_<slug>` tool
per delegatable downstream profile onto the parent — **depth-1 only**: a delegated
run itself binds none. Calling `ask_<slug>` loads the delegate profile via
`AgentProfileLoader.load_by_id`, reuses `prepare()` so the delegate's own tool
curation, `usage_guidance`, and skills all apply exactly as they would for a
first-class request, runs a bounded synchronous tool loop, and returns the
specialist's finished report to the parent. Snapshot schema v6 adds
`downstream_delegates` — the profile's delegatable downstream rows, denormalized
(profile id, slug, display name, description, agent kind) — plus the shallow
`delegatable` flag, so production can build `ask_<slug>` tools without a live join.
Legacy (v5 and older) snapshots read as `downstream_delegates = []`. A custom agent's
hand-off name is derived from its slug (`ask_<slug with non-alphanumerics as _>`).

**Tools with request-scoped state should not be delegated.** A depth-1 delegate is a
fresh sub-agent that returns only text, so a tool that relies on state shared with the
parent's request (a confirmation `ContextVar`, an SSE event buffer the parent streams)
should stay bound on the main profile instead of a delegatable specialist; see
[Configure → MCP & Tools](./02-configure.md#mcp--tools).

**`usage_guidance` renders into the tool catalog.** A tool's (or, for an external MCP
server, the server's) `usage_guidance` — free text for field semantics, metric
definitions, or gotchas — is stamped onto the resolved tool's metadata at MCP-
resolution time; when that tool is bound for a request, `render_tool_catalog` renders
it as an indented `↳` line under the tool's `{available_tools}` entry. Guidance for an
unbound tool is never rendered.

**Resolution correctness is locked by tests.** `tests/runtime/test_mcp_parity.py`
asserts the invariants over a fixture pool: bound order follows factory-pool order (via
member `position`, not the `tool_subset` allow-set), per-tool schema is preserved, and
HTTP-authored members are appended after the Python tools in deterministic
`python_name`-sorted order (`admin/builtin_mcp_membership.py:ordered_membership`).

**`/admin/api/mcp-flag-parity` is an MCP-resolution health report.** For each active
profile in the selected env it resolves tools the runtime way and reports the bound
tool names, attachment count, and any resolution error — superuser-gated and read-only.

---

## Observability

**Laminar (`lmnr`, self-hosted)** is initialised at import time in `main.py`, before
agent code loads, so it can patch LangChain / LangGraph / OpenAI / Anthropic / Gemini
and emit an OpenTelemetry span for every LLM call automatically. With
`LMNR_PROJECT_API_KEY` unset the engine boots with tracing disabled.

**An install with no tracing store.** With `LMNR_DISABLED` set and no
`LMNR_CLICKHOUSE_URL` (`core.lmnr_clickhouse.tracing_configured()` is False; the
local compose stack) there is nothing in ClickHouse to read, and the portal
says so instead of reporting an outage. **Conversations** lists and opens the
saved chats from Postgres (`ai_chat_sessions`, `ai_chat_messages`, and the
per-turn `agent_traces` rows for tokens, response time, model and cost;
`admin/screens/saved_chats.py`). **Overview** and its charts show a plain
"tracing is off" state that names what the tracing stack adds. Span-level
detail (each model call, tool inputs and outputs, timings inside a reply)
exists only with tracing on. An explicit `LMNR_CLICKHOUSE_URL` always counts as
configured, so a reader that exports no spans of its own still reads the shared
store.

It instruments the **LLM providers only** — there is no per-request FastAPI span — so
each streaming endpoint opens an explicit **root span** around the whole stream
(`common/sse.py:sse_response(trace_name=…, trace_attributes=…)` →
`core/observability.py:root_span`), tagged with `waj.agent_kind` and `waj.channel.*`
from `core/channel_resolver.py:root_span_attrs`. The agent's auto-instrumented LLM
spans nest under it, so the trace's **top span** carries `agent_kind` — which is what
the ClickHouse rollups and the Traces/Dashboard chips read. Without this the
top span has no `agent_kind` and every trace renders as "legacy". (Tagging must happen
on the root span, not at handler entry: the agent — and its spans — start lazily inside
the SSE body *after* the handler returns, so there is no active span to tag yet.)

Two additional span attributes are stamped on the root span by the SSE streaming
helper (`common/sse.py:_stamp_ttft_ttlt`) after the stream drains:

| Attribute | Meaning |
|-----------|---------|
| `waj.ttft_ms` | Milliseconds from request start to the first content-bearing SSE chunk (time to first token). |
| `waj.ttlt_ms` | Milliseconds from request start to the final `done` event (time to last token). |

Both are read by the ClickHouse analytics query in `admin/screens/live_analytics.py`
(`maxIf(simpleJSONExtractString(attributes, 'waj.ttft_ms'), …)`) and surfaced as
**Time-to-first-token** and **Time-to-last-token** KPI cards and a combined chart on
the [Dashboard Overview tab](./01-operate.md#overview). The eval runner stamps the
same values on eval spans via `e2e_runner.py:_stamp_ttft_ttlt`, so comparison-matrix
TTFT/TTLT rows draw from the same attribute names.

The rubric judge (`e2e_runner.py:_invoke_judge(rubric=…)`) stamps
`waj.eval.judge_model` on the eval span so the operator can verify which model graded
each run. See [Test → Mandatory judge](./03-test.md#mandatory-judge).

A lean **`agent_traces`** bridge row (session id, `lmnr_trace_id`, agent kind) is
written per request so the portal can join sessions to traces and deep-link into the
full Laminar span tree. Rich telemetry — tokens, latency, cost, tool calls, errors —
lives in Laminar only.

The portal reads that telemetry **one way: ClickHouse**
(`core/lmnr_clickhouse.py`, read-only SQL over `traces_replacing`/`spans`). Trace
exploration, transcripts, span detail, the Dashboard KPI tiles and the analytics
chart grid all go through it, and every loader degrades to an empty-but-shaped
result plus a notice when it's unreachable. See [Operate](./01-operate.md). The Laminar
web UI is not a read path (the self-hosted build serves no REST API the portal could
sign into); its frontend container survives in the bundled stacks only as a bootstrap
utility — it owns the ClickHouse migrations and mints the ingest API key.

**Sensitive agent kinds.** A deployment's plugin may declare kinds whose content is
sensitive (`sensitive_content` hook, `core/sensitive_content.py`): their traces export
with prompts, completions and tool I/O redacted, their duplex calls are never recorded,
and their chats are never sampled into evals. With no plugin, no kind is sensitive.

Two ClickHouse facts shape the reader:

- **LLM span I/O is not in `spans.input` / `spans.output`.** Those are empty for
  LLM spans (`span_type = 1`); the messages live in the content-addressed
  `deduped_content` table, reached via the span's `input_message_hashes` /
  `output_message_hashes` arrays. Tool / DEFAULT spans DO keep theirs inline.
  (`llm_messages`, `deduped_content_dict` and `llm_messages_dict` are all empty —
  a reader built against them looks right and returns nothing.)
- **`spans.status` is not the error signal** — it is `''` on almost every row.
  Errors come from the OTel `events` array
  (`arrayExists(x -> x.2 IN ('exception','error'), events)`).

---

## Auth model

- Every `/admin/*` route depends on `require_superuser` (`admin/auth.py`).
- The token comes from `Authorization: Bearer <jwt>` or the `waj_admin_token` cookie
  (httpOnly, secure in prod, 8h max-age) set by `/admin/login`.
- The JWT is signature-verified (with `exp`/`aud`/`sub` required) via
  `core.auth.validate_token_verified`. The token's header `alg` picks the key, from an
  allow-list (`SUPABASE_JWT_ALGORITHMS`, default `ES256,HS256`; an unsupported value
  fails ALL verified auth closed rather than falling back to the defaults): **ES256**
  tokens — what newer Supabase CLI stacks and migrated hosted projects issue — are
  checked against the main project's public keys
  (`<SUPABASE_URL>/auth/v1/.well-known/jwks.json`, override `SUPABASE_JWKS_URL`);
  **HS256** tokens against `SUPABASE_JWT_SECRET`. Each branch decodes with exactly one
  algorithm, so an HS256 token "signed" with the public key cannot verify. The keys are
  refreshed in the background (`main._jwks_refresh_loop`, every 10 min; a failed refresh
  keeps the last good keys), so the request path reads a warm cache; it fetches itself
  only when cold, on an unknown `kid` (at most once a minute) or when the keys are over
  an hour old. Fetches run one at a time (so an older fetch can't overwrite newer
  keys), and after a failed fetch requests fail closed for 30s instead of each
  retrying it (so a JWKS outage can't tie up a thread per request). Every async caller
  runs the check via `asyncio.to_thread`, so even those fetches never block the event
  loop (pinned by
  `tests/core/test_verified_jwt.py::test_no_async_function_calls_verified_auth_inline`).
  Then the `user` row is checked for `permission.is_superuser`. A valid token without
  the flag still gets 403. The token-and-permission check is
  `admin.auth.resolve_superuser(token)` — a token-only function `require_superuser`
  wraps for the FastAPI dependency shape; other machine surfaces (the MCP auth
  middleware, the `/v1/agents` API) reuse the same trust boundary.
- **Both the admin HTTP path and the MCP path enforce signature verification and
  fail closed.** A missing key source for the token's algorithm (unset
  `SUPABASE_JWT_SECRET` for HS256, unreachable JWKS for ES256), a forged `sub`, or a
  token with the wrong signature all reject before any table read — there is no unverified
  fallback on either surface. `admin_mcp/auth.py:MCPAuthMiddleware` calls the exact
  same `resolve_superuser` function `require_superuser` wraps, so a superuser grant
  or revoke takes effect identically on both.
- Login accepts **email + password** (Supabase GoTrue) with a raw-JWT paste fallback
  for service accounts / CI. See [Govern](./04-govern.md).
- **A build with no product plugin has no default login.** Until a superuser exists,
  `/`, `/admin` and `/admin/login` redirect to the first-run page (`/admin/setup`),
  which creates the first admin for whoever can type the one-time setup code the engine
  prints at boot. It is the only unauthenticated write under `/admin`; once an admin
  exists it redirects to the login page for good. A product build never has it
  (`admin/services/first_run.enabled()`). See
  [Govern → First-run setup](./04-govern.md#first-run-setup).

**The custom-agent API** (`POST /v1/agents/{slug}/chat`) and the admin MCP server
(`/mcp`) accept a **portal-issued API key** (`Authorization: Bearer wae_…`) as well as a
superuser token. A key carries scopes: `chat` opens the chat API only, `admin` opens
both. A key-authenticated turn runs as the key (its row id is the session's `user_id`),
so keys cannot read each other's conversations. `/admin/*` itself never accepts a key.
See [Govern → API keys](./04-govern.md#api-keys).

Agent chat endpoints that read a tenant's data authenticate the end user's JWT and rely
on RLS for tenant isolation; admin/service-key queries bypass RLS for operational reads.

`GET /ai/sessions` takes `business_id` to list the caller's sessions of that
business; without it, it lists the caller's sessions that belong to **no**
business (the ones a generic agent writes through `/v1/agents/{slug}/chat`).
Either way only the caller's own (`user_id` from the verified token).

The **chat-history read/manage endpoints** (`endpoints/ai_sessions.py` —
`GET /ai/sessions`, `GET /ai/sessions/{id}`, `GET /ai/sessions/{id}/messages`,
`PATCH`/`DELETE /ai/sessions/{id}`) are the exception to the RLS rule. The chat
transcript tables live in the standalone **AI project**, which has no copy of
`auth.users` and only a service-role RLS policy — so a client can't query them
directly. These endpoints authenticate the end user's JWT (signature-verified), then
read the AI project via the **service client** with **explicit `user_id` /
`business_id` scoping** (and a per-session ownership check) standing in for the
per-user RLS the AI project lacks. They are read/manage only; sessions and messages
are written by the streaming chat path (`common.chat_session.run_turn` — the sole
orchestrator for every chat agent).

### MCP management surface

The full admin-portal management surface is also reachable as an **MCP server**
mounted at `/mcp` — the Configure/Test/Ship/Observe/Govern tools plus these docs as
read-only resources (`wajdocs://{slug}`). It runs as a **stateless Streamable-HTTP**
FastMCP app (`admin_mcp/server.py`): every request is self-contained, no in-memory
session survives past it, so it works behind a host that scales or recycles replicas
between calls. It is mounted **only** when `ENABLE_ADMIN_MCP=1` (`main.py`), which
should be set only on the **control-plane** deployment — chat-serving deployments
leave it unset and never expose `/mcp`. Auth reuses the exact same
`admin.auth.resolve_superuser` trust boundary as the HTTP admin path (see
[Auth model](#auth-model) above), verified per-request by
`admin_mcp/auth.py:MCPAuthMiddleware` before any MCP protocol handling runs. See
[MCP Server](./06-mcp.md) for the full tool catalog, token minting, and the
handful of places an MCP tool's behaviour deliberately differs from its portal
counterpart.

---

## Theming & layout

The portal UI is a token-driven design system in `admin/static/portal.css`:
a **primitive layer** (gray / teal / status color scales, spacing, radii, the
Inter UI font) feeds a **semantic layer** (`--bg`, `--surface`, `--fg-1`,
`--accent`, `--success`, …) that every component consumes — so **dark mode is a
re-mapping of the semantic variables**, not a second stylesheet.

- **Theme switching** — `document.documentElement.dataset.theme` is `light` or
  `dark`. A tiny **pre-paint script** in `_base.html`'s `<head>` reads the
  `waj-admin-theme` localStorage key (falling back to the OS
  `prefers-color-scheme`) and sets the attribute before first paint, so there is
  no flash of the wrong theme. The top-bar sun/moon toggle persists the choice
  and dispatches a `waj-theme-change` event that re-skins the theme-aware
  Tabulator tables and Chart.js / uPlot charts in place.
- **Collapsible sidebar** — the nav rail collapses to an icon-only strip; the
  state persists via the `waj-admin-nav` localStorage key (value `collapsed`;
  `expanded` when a user re-opens it on a narrow screen — below 900px the rail
  starts collapsed) and is also applied by the same pre-paint script. Every item
  has its own icon; the collapse handle uses tokens only.
- **Portal name** — a build with no plugin shows "WAJ AI Engine"; `PORTAL_NAME`
  overrides it. A deployment scope brings its own name and accent (see
  [Deployment scopes](#deployment-scopes)).
- **Design-system classes** (`portal.css`, "Design-system foundation" block) —
  `.section` (flat block, divider between siblings), `.page-head` (page actions
  row), `.table` / `.table-wrap` (small static tables), `.btn` / `a.btn`
  (`.primary` / `.ghost` / `.danger` / `.small`), `.segmented`, `.row` /
  `.stack-*` / `.mt-*` utilities, `dl.kv`. Previously undefined classes are
  aliased: `.button` / `.button-ghost` → `.btn`, `.panel` → `.section`,
  `.muted` → caption colour, `pill ok|warn|bad` → chip colours; chips are one
  family (`chip-neutral` = legacy `chip-mauve`, plus `chip-info`).
- **Tabulator tables** (`portal-table.js`) — the CSV / JSON / Clear-filters
  toolbar is opt-in (`data-tabulator-export`, or automatic above 25 rows;
  `data-tabulator-export="off"` suppresses it). A lone `<td colspan>` row is the
  empty-state placeholder, and `<tr data-state="error">` renders it as an error;
  the grid re-reads its source `<tbody>` when a script rewrites it (deploy
  history). The host scrolls horizontally instead of clipping.
- **Global search** — see [Global search](#global-search) below.
- **Callouts** — operator guidance uses the shared
  `_components/callout.html` macros (`alert(kind)` block callouts, `hint` inline
  captions, `info` expandable ⓘ popovers) instead of ad-hoc caption text. Deep
  conceptual explanations live in these docs, not in the screens.
- **Markdown** — `marked.js` + DOMPurify render prompt bodies, transcript
  messages, eval scenario text, judge reasons/analysis, and these docs. Long-form
  editable fields (suite scenario question/expected, tool descriptions) get an
  **Edit/Preview** toggle.

---

## Legacy route redirects

The redesign merged several standalone screens into tabs. Old URLs survive as
**307 redirects** (query params carried over), so bookmarks and deep links keep
working:

| Legacy route | Redirects to |
|--------------|--------------|
| `/admin/configure` | `/admin/agents` (landing retired — the Build nav group replaced it) |
| `/admin/eval-runs` | `/admin/evals` (runs section is always visible; no `?tab=runs`) |
| `/admin/live?tab=jobs` | `/admin/jobs` (Settings › Diagnostics) |
| `/admin/agent-models` | `/admin/agents?tab=models` (model **detail** URLs `/admin/agent-models/{id}` are unchanged) |
| `/admin/integrations` | `/admin/tools` |
| `/admin/mcp-servers`, `/admin/agent-mcp-servers` | `/admin/tools` |
| `/admin/env-vars` | `/admin/env?tab=variables` (env-var **detail** URLs `/admin/env-vars/{id}` are unchanged) |

Earlier retirements (302): `/admin/profiles*` and `/admin/prompts` →
`/admin/agents`; `/admin/data-sources` → `/admin/kb`; `/admin/datasets*` →
`/admin/evals`; `/admin/sessions*` and `/admin/observability/sessions*` → the
Traces explorer (a session deep-links to its trace's Transcript);
`/admin/observability/{trace_id}` → the explorer with `#trace=…`.

---

## Two-project runtime

The engine talks to **two** Supabase projects.

| Env prefix | Project | What it's used for |
|---|---|---|
| `SUPABASE_*` | **Main** (application) DB | Auth: token validation and the users the portal checks for `permission.is_superuser`. Reads of the application's own data use the caller's token (`get_supabase_client(user_token)` — full RLS in effect); `get_main_client()` is the service-role client for the few paths that must bypass RLS (the superuser check, the admin user/business screens, global search). A deployment's plugin may read more of it. |
| `AI_SUPABASE_*` | **Standalone AI** project | Every agent config, record, eval, analytics, observability, and chat table — accessed via the service role — `get_service_client()` on the runtime/chat path (own branch), or `admin_service_client()` on the env-aware admin path (the switcher-selected branch). Schema `public`. RLS on these tables is service-role-only; the app is the gateway and enforces business scoping in code. |

**Know which client you're using before you write a query:** `get_service_client()` is
always the AI project, never main. Cross-environment promotion writes to
`{STAGING,PROD}_AI_SUPABASE_*` when they are set. Both local stacks can run concurrently
on different port ranges (main 543xx, AI 544xx); see `supabase/README.md`.

---

## Voice edge — speech providers

The turn-based HTTP voice routes (`/voice/transcribe`, `/voice/speak`,
`/voice/chat/stream`) were **retired**; spoken conversation is the
[duplex edge](#duplex-voice-edge) below (`WS <portal_prefix>/voice/live`). Both routers
declared `prefix="/voice"`, which is why `"/voice/"` is deliberately NOT a blocked
prefix — adding it would 404 every duplex call.

What survives in `voice/` is a **provider layer** for speech-to-text (STT) and
text-to-speech (TTS). Its caller in the engine is the scenario/eval harness
(`admin/voice_scenario_runner.py` synthesizes the simulated caller's speech) — which is
why `AZURE_SPEECH_KEY` gates duplex *testing* even though a duplex call itself never
touches these providers. A plugin's own routes may use it too.

**Two providers are registered: `azure` (the default) and `gemini`.** STT and TTS
resolve independently through `voice/factory.py`'s `_STT_BUILDERS` /
`_TTS_BUILDERS`, so the two directions may use different providers; any value outside
those maps raises `UnsupportedVoiceProviderError` at request time. Adding a provider
means implementing the `VoiceProvider` interface (`voice/providers/base.py`) and
registering a builder in those maps.

`build_stt_provider` / `build_tts_provider` take a **duck-typed** settings
object (`stt_provider` / `tts_provider`, plus `stt_model`, `tts_model`,
`speaking_rate`). Both builders read BOTH model fields — one provider object serves
ears and mouth — so an STT-only caller must still supply `tts_model`. Env defaults
live on `AGENT_CONFIG.voice_*`.

**`azure` (`voice/providers/azure.py`)** speaks Azure AI Speech's REST surface over
`httpx` (short-audio recognition; `cognitiveservices/v1` synthesis) rather than the
Azure Speech SDK. Notes an operator needs:

- Requires `AZURE_SPEECH_KEY`; set `AZURE_SPEECH_REGION` to your Speech resource's
  region. Without the key the provider fails loudly at request time — it does **not**
  silently fall back to Gemini.
- Selects its synthesizer by **voice name** (`voice_id`, e.g. `en-US-JennyNeural`) and
  its recognizer by **locale**, so `stt_model` / `tts_model` are inert for this provider.
- `language` is honoured in both directions and widened to a regional locale
  (`ar` → `ar-SA`): this endpoint does not auto-detect, and an unset language
  transcribes non-English speech as English.
- `sample_rate` must be one of Azure's raw-PCM rates (8k/16k/22.05k/24k/44.1k/
  48k). An unsupported value is refused rather than silently substituted —
  a header claiming a rate the PCM isn't in plays everything at the wrong speed.
- The short-audio REST API accepts WAV and Ogg/Opus but **not WebM/Opus**, a
  common browser `MediaRecorder` default; the provider names that case in its
  error instead of returning an opaque 400.

**Design notes:**

- Agents are **not modified** — no agent module imports voice code.
- `agent_profiles.voice_config` is the **duplex** settings block and it is read
  **flat** (no `live` sub-key) — see [Duplex voice edge](#duplex-voice-edge) and
  [Data Model → agent_profiles](./data-model.md#agent_profiles). It rides the profile
  snapshot into `agent_profile_versions`, so production reads an immutable voice config.
- `voice/config.py` holds the two coercion helpers the duplex resolver imports
  (`apply_overrides`, `coerce_bool`).

---

## Duplex voice edge

> **Configuring it:** every operator-honoured `voice_config` key has a typed
> field on **`/admin/agents/{profile_id}/voice`** — the voice cockpit, the single
> portal writer of `voice_config`, plus a brain panel that edits the specialist row's
> `model_main_id` / `memory_config` and a persona editor that always pins on the
> owner — generated from `admin/screens/agent_voice.py::VOICE_FIELDS` and pinned
> against `voice/live/config.py::_VOICE_FIELDS` by
> `tests/admin/test_voice_screen_field_coverage.py`, so a key cannot be added
> to the runtime and stay invisible. Each control carries an inline
> effective-value note derived from the provider's declared capability flags on
> `voice/live/base.py::RealtimeSession` — the screen never asserts a value the
> session will not use. See
> [Configure → the voice cockpit](./02-configure.md#the-voice-cockpit--adminagentsprofile_idvoice).

A realtime provider owns both ears and mouth over one WebSocket for the whole
conversation, and the existing chat agent stays purely the "brain" it answers
substantive questions with.

**Route.** `WS <portal_prefix>/voice/live` (`endpoints/voice_live.py`), mounted at
import time under `core.deployment.mount_prefix()`: the active deployment scope's
prefix, or empty on a full build — so on a full build the route is the bare
`WS /voice/live`. Changing the scope needs a process restart. The HTTP
`deployment_gate` middleware never sees a WebSocket handshake (Starlette skips
non-http scopes), so the endpoint checks the `agentKind` itself (see the guardrails
below).

**Registered voice kinds.** `runtime/agents/voice_registry.py` maps an `agent_kind` to
a builder (`register_voice_agent_builder`). The engine registers the four `/agents`
demo kinds (anonymous, with per-IP caps seeded on their profiles). **A deployment's
plugin may register more** through the `voice_builders` hook — one registration per
kind is the entire code footprint of duplex voice for it; a new tenant or profile of a
registered kind needs no code. The generic `custom` kind is registered too, but is
served at its own route: see [Voice for generic agents](#voice-for-generic-agents). A
registration may declare:

- `requires_auth=True` — the client must send its credentials as the FIRST frame after
  the socket opens (`{"type": "auth", "token": "<supabase JWT>", "context": {…}}`),
  because browsers cannot set WebSocket headers. The JWT is signature-verified
  (`validate_token_verified`); a missing frame (5s, `_AUTH_FRAME_TIMEOUT_SEC`), a
  non-auth first frame, or an unverifiable token closes the socket with 4403. A caller
  whose **bearer header** already verified skips the frame, and so does the trusted
  simulated-eval path (below). A non-loopback `?provider=simulated` still hits the
  frame gate, so the param cannot become an auth bypass.
- `credential` (`VoiceCredential`) — a second credential the kind needs (typically a
  token for the backend its tools read as the caller), sent under its own auth-frame
  key or header and verified off the event loop before any agent, budget or provider
  work.
- `tenant_access` (`VoiceTenantAccess`) — for a kind that reads one tenant's data as
  the caller: the handshake demands a verified caller who belongs to the tenant the
  call names (`businessId`), else `CLOSE_FORBIDDEN`.
- `client_context_keys` — the turn-context keys a client may set in the auth frame's
  `context` and in later `{"type": "context", …}` frames (which REPLACE the
  connection's context; each delegated turn snapshots it into `run_turn`'s
  `extra_context`).
- `session_reader` — reads a portal session cookie off the handshake for a kind whose
  browser portal authenticates by a signed httpOnly cookie rather than a bearer token
  (a missing/tampered/expired cookie degrades to an anonymous connection, never an
  error).

Server → client `{"type": "proposal", "proposal": {...}}` frames forward an agent's
proposed changes verbatim after the turn's `display`/`chart` frames; the voice layer
never executes them — the client confirms on screen.

**The `RealtimeSession` seam (`voice/live/base.py`).** Realtime APIs don't share a
shape: Gemini Live's function calling is non-blocking (the model keeps talking while
a tool runs); the Azure OpenAI Realtime protocol's is not. So `RealtimeSession` is an
abstract contract in the engine's own terms — `start()`, `send_audio()`,
`send_text_turn()`, `send_tool_result(call_id, payload, mode)`,
`speak_out_of_band()`, `reprompt()`, `report_playback()`, `events()`, `close()`, plus
capability flags (below) — never a vendor's method names.
`voice/live/testing.py`'s `assert_realtime_contract` conformance suite exercises any
implementation against that contract. `voice/live/factory.py` resolves a
`LiveVoiceConfig.provider` string (the `agent_models` row's `provider` column) to a
registered builder. Swapping providers is an `agent_models` row edit (or a
`voice_config.model_id` edit), not a deploy:

| Slug | Module | What it is |
|---|---|---|
| `google_genai` | `voice/live/providers/gemini_live.py` | Gemini Live. 16k in / 24k out. |
| `azure_voice_live` | `voice/live/providers/azure_voice_live.py` | Microsoft Foundry Voice Live, over the Azure OpenAI Realtime wire protocol on a raw `aiohttp` WebSocket (with Azure's extensions: `azure_semantic_vad`, `azure-speech` transcription, `azure-standard` voices). 24k in and out. **Cascaded** by default (Azure STT → a text chat model → Azure neural TTS) or **native** audio when the model row sets `extra_config.voice_type: "native"` (the model hears the audio and generates the speech itself). |
| `azure_openai` | `voice/live/providers/azure_realtime.py` | A deliberate stub for the raw Azure OpenAI Realtime API: it raises `UnsupportedRealtimeProviderError` pointing at `azure_voice_live`, so a mistaken pick fails clearly. |
| `simulated` | `voice/live/simulated.py` | Eval-only, text turns with no audio (below). |

**`azure_voice_live` row shape.** Three `extra_config` keys select behaviour per row
rather than per tenant — see
[Data Model → `azure_voice_live` row shape](./data-model.md#agent_models):
`voice_type` (`"azure-standard"` | `"native"`) changes the SHAPE of the `voice` field
in `session.update` (an object vs. a bare string — a native row still carrying an
Azure voice object is rejected outright), `transcription_model` picks the recognizer
(`azure-speech` is the only option for a cascaded chat model and the only one that
accepts `phrase_list`; a native model transcribes for display with an OpenAI
recognizer instead), and `turn_detection` is an operator override merged **over** the
provider's `azure_semantic_vad` defaults — where a **null value UNSETS a default**.
Native models are offered only in some Voice Live regions, so a native row may point
at a different region (and its own `auth_env_var`) than the engine runs in. The only
reliable check of which models a region serves is a real `session.update` —
`manual_tests/probe_azure_voice_live.py`. Barge-in guards that suit a cascaded row
(the `*_assistant_speaking` keys) can make a native row uninterruptible, so they belong
in that one row's `extra_config`, never in the provider defaults.

**`silence_duration_ms` is paid on every turn.** It is how long the service waits
before closing the caller's turn — a floor under end-of-turn latency that no
downstream speed-up can win back. On `azure_voice_live` the provider default is
**650**: A/B'd against 950 it took ~295ms off every turn with no difference in
think-pause tolerance (both held a 400ms and a 600ms mid-sentence pause and split an
800ms one), and there is a ~230ms fixed floor under the window. Gemini Live has the
same floor under a different name: the `google_genai` provider builds
`realtime_input_config.automatic_activity_detection` from the **same
`extra_config.turn_detection` key**, forwarding only the fields Gemini has
(`silence_duration_ms`, `prefix_padding_ms`, `start_of_speech_sensitivity`,
`end_of_speech_sensitivity`, `activity_handling`, `turn_coverage`) and dropping Azure's
own keys with an INFO log rather than failing the call. Its default is **600**, the
knee measured with `manual_tests/probe_gemini_turn_detection.py`: lower values split a
500ms think-pause, higher ones cost 150–350ms per turn. Re-run the probe before
changing the number. A Gemini Live model may also reject a TEXT-only modality, so a
`text_only` profile pointed at one can fail at connect.

**Capability flags: defences attach to declared capabilities.** Much of the recovery
machinery below exists because of one provider's protocol facts, so each provider
declares what it can do and the endpoint arms only what applies:

| Flag | Azure | Gemini | What it gates |
|---|---|---|---|
| `supports_streamed_tool_results` | `False` | `True` | Per-sentence answer streaming rides several tool results per call id; otherwise the whole answer is the single closing tool result. |
| `supports_out_of_band_speech` | `True` | `False` | Whether a holding line can be spoken without consuming the pending call id. |
| `supports_reprompt` | `True` | `False` | Whether a stalled turn can be handed back with a bare `response.create`. |
| `supports_barge_in_confirmation` | `True` | `False` | Whether a commit's playback flush is held for the caller's transcript verdict. |
| `reports_response_in_flight` | `True` | `False` | The answer watchdog is ARMED only where it is true. |
| `finalizes_user_transcripts` | `True` | `False` | Whether the commit-keyed levers (stall recovery, relay custody, digit capture) can run at all. Gemini's provider synthesizes the commit (`gemini_live.UserTurnState`) from the model taking the floor. |
| `transcribes_streamed_tool_results` | n/a | `False` | Where a STREAMED answer's on-screen text comes from: Gemini transcribes only a fragment of each streamed sentence, so the endpoint sends its own copy (`answer_echo`), paced to when she starts each sentence. |

The base defaults are the SAFE answer, `assert_realtime_contract` refuses a provider
that declares a flag without implementing what it promises, and the voice cockpit
shows each flag and marks the levers it makes inert.

**Simulated provider (eval-only).** Registered under `simulated`, it stands in for a
real speech provider so a `runner_kind: voice_live` eval suite (see
[Test → Voice eval suites](./03-test.md#voice-eval-suites-runner_kind-voice_live))
can drive the actual `WS /voice/live` endpoint — state machine, tool delegation,
`to_speakable()` and all — with zero provider network calls and zero audio cost. It is
gated by **three** independent checks in `_sim_override_allowed()`, all required to let
a connection request `?provider=simulated`: the deploy opted in via
`VOICE_LIVE_SIM_ENABLED` (set it only on the control-plane, never on public chat); the
peer is loopback (`127.0.0.1` / `::1` — eval runs connect to their own replica); and
the handshake carries **no** forwarding header (`X-Forwarded-For` /
`X-Forwarded-Host` / `X-Forwarded-Proto` / `Forwarded`). The third check matters
because the Dockerfile runs uvicorn with `--proxy-headers --forwarded-allow-ips="*"`,
so `scope["client"]` is rewritten from a client-supplied `X-Forwarded-For`; without it a
remote caller sending `X-Forwarded-For: 127.0.0.1` would pass the loopback check.
Failing any gate denies the override **silently** (the profile's real provider is
used), so the param can't fingerprint what a deployment supports.

**Delegation runs the same `run_turn` — the agent brain is unchanged.** The realtime
model's only tool is `ask_agent` (`voice/live/delegate.py`, `run_delegated_turn`): on a
`tool_call` event the WS handler runs the *exact* orchestrator the text chat SSE path
uses (`common/chat_session.py`'s `run_turn` — same profile, prompt, MCP tools, charts,
persistence and telemetry), then reduces the result to `{spoken, display, charts}`.
`display` is pushed to the client verbatim (screen of record); `spoken` is what the
realtime model is told to read back unmodified — deterministically rendered by
`voice/live/speakable.py::to_speakable()` from the agent's answer text (or an
agent-authored `spoken_summary` event) so numbers and dates read naturally. No agent
module imports voice code. The delegate stamps `voice_turn: true` into the agent
context, so an agent can skip work that only a text turn needs (title and suggestion
sub-agent calls). `run_turn` overlaps its persistence with the answer (the user-row
write runs alongside the stream; the transcript write runs concurrently with the
`agent_traces` insert), and everything still lands before the terminal `done` event.

**Pre-fetched context (no delegation).** With `voice_config.prefetched_context`, the
agent renders the whole case into the persona before the call, and the provider
declares the agent's `direct_voice_tools()` instead of `ask_agent`; they run locally
(`_handle_direct_tool`). With no direct tools, `tools_disabled` binds none. The
`/agents` demo uses both shapes.

**Two levers remove the second brain call for a class of delegated turn**, both silent
when off and both visible per tool span:

- **KB prefetch** (`voice_config.kb_prefetch_enabled`, `kb_prefetch_match_count`).
  `run_delegated_turn` calls the agent's `prefetch_voice_context(message, …)` hook
  BEFORE `run_turn`; a hit lands in `extra_context["prefetched_docs"]` so the model can
  answer without calling its search tool. The gate is the retrieval's own similarity
  threshold, so a non-KB question injects nothing; a wrong guess costs one embedding
  round trip (bounded at 2.0s), never a wrong answer. `waj.voice.live.prefetch_hit`.
- **Deterministic rendering** (`voice_config.deterministic_render_enabled`).
  `voice/live/rendering.py`'s registries (`register_renderer` / `render_tool_result`,
  and `register_failure_renderer` / `render_tool_failure` with `classify_tool_failure`)
  let an agent answer a structurally fixed tool result (a code was sent, a record was
  saved) from a template, with no synthesis call. It is a safety property first: a
  renderer emits the digits present in the tool result or returns `None`, and a FAILED
  side-effecting call is rendered as a truthful decline rather than handed to the model
  to narrate. Renderers fail open to the model on any unexpected shape; analytics
  results are deliberately not templated, because the synthesis call is what adapts
  data to the question. `waj.voice.live.rendered_tool` / `rendered_failure`. The
  engine ships the registry; renderers come from the agents that use it.

**An account identifier the voice speaks must have entered the turn from outside the
model** — the one guarantee on this path that is code-enforced rather than prompted.
A provenance guard (`voice/live/delegate.py` + `speakable.guard_identifiers()` over
`core/tool_provenance.py`):

- every agent that dispatches tools reports each raw result into a per-turn
  `TurnProvenance` (a ContextVar recorder the delegate installs — the text chat path
  never pays for it);
- `run_turn` adds the caller's own words: the session's USER rows oldest-first, then
  this message. Assistant rows are excluded, or one fabrication would vouch for every
  later turn;
- anything account-number-shaped in `spoken`, in each streamed sentence and in
  `display` is checked, digits-only (Arabic-Indic folded, separators ignored), against
  that corpus. **Verified → spoken in full**, regrouped into fours. **Unverified →
  withheld** behind a per-language line that asserts no fact, counted on
  `DelegateResult.identifiers_suppressed` and logged WARNING. It never substitutes a
  "probably meant" value.

An agent that dispatches tools without calling `record_tool_result` silently withholds
every CORRECT identifier it quotes — `tests/voice/test_identifier_provenance.py` pins
that structurally across `runtime/agents/`. Any early exit from a tool loop (such as a
deterministic render) must record the turn's results BEFORE it takes it. The guard
covers the brain's answer only, not a number the realtime model states without calling
`ask_agent`. Optionally, `voice_config.server_side_iban_capture` moves a dictated
account number's digit tally out of the model into a server-side state machine.
The collector is supplied by a plugin (`voice/live/dictation.py` defines the contract;
a build with none leaves it closed), and it needs `finalizes_user_transcripts`.

**Turn-taking and recovery.** The endpoint carries a set of mechanisms, each with its
own `voice_config` lever (see
[Data Model → `voice_config`](./data-model.md#voice_config-keys-duplex-voice)):

- **Holding line** (`progress_after_sec`, default 4s; `progress_spoken` /
  `progress_spoken_by_language`). Spoken out-of-band where the provider supports it,
  queued strictly in order and cleared the moment the caller speaks or a tool result is
  delivered. A model row that opts into Azure's server-side `interim_response` skips
  the endpoint's holding line. `delegate_timeout_sec` (30s) caps a delegated turn.
- **Barge-in confirmation** (`interrupt_confirm_window_sec`, default 2s; `0` restores
  the legacy immediate-flush path). Speech-start detection is noise-prone, so an
  interrupt starts UNCONFIRMED; the caller's substantive transcript (not a
  backchannel — `backchannel_tokens` extend the built-in set) or a fresh `tool_call`
  confirms it. A noise verdict drops the held flush and the answer is still spoken. The
  endpoint mirrors the browser's playback cursor and never discards audio the caller
  has not heard on an unconfirmed release (`playback_protected`), and treats a caller
  who starts speaking while audio is queued as an interruption, otherwise as ordinary
  turn-taking.
- **Caller-utterance clock.** A transcript can arrive after the model's `tool_call` for
  the same utterance, so each dispatched `ask_agent` is stamped with the utterance it
  answers; a superseding utterance silences only answers to earlier ones.
- **Every `response.cancel` names its `response_id`** — a bare cancel applies to the
  default conversation and cannot stop an out-of-band holding line.
- **Playback-position truncation.** On every flush the client reports how far playback
  got (`{"type":"played","ms":N}`); `report_playback` truncates the conversation item
  the caller was hearing, deferred until `response.done` when the item is still
  generating (truncating mid-generation stops the reply mid-word).
- **Answer watchdog** (`answer_watchdog_sec`, 4.0). Re-delivers an answer that produced
  no audio, but only while the provider reports the floor idle
  (`response_in_flight`); a response in flight buys up to three more windows. A
  refused tool result (`tool_result_dropped`) is never re-said.
- **Stall recovery.** A realtime turn is request-scoped: one that ends on "one moment,
  let me check" with no tool call is finished, not late. The announce-then-stall
  tripwire (lexical `common/announce.py` tokens at tier 1;
  `stall_recovery_topic_terms` at tier 2; `stall_recovery_unannounced_sec` and
  `stall_recovery_silent_sec` behind them) re-prompts the model
  (`RealtimeSession.reprompt()`, a bare `response.create` — per-response `tool_choice`
  is ignored by the service) and keeps `stall_recovery_sec` armed as the fallback. The
  endpoint records every string it hands the model to say (`_note_endpoint_speech`)
  and subtracts it before the announce test (`_model_own_words`), so its own holding
  line can never arm the tripwire. `persona_claim_recovery_enabled` (opt-in) also arms
  it on a persona claim of a result with no `ask_agent`.
- **Relay custody** (`relay_mode`). Closes the model's own `ask_agent` unspoken while a
  protected flow is open and dispatches the replacement turn from the caller's final
  transcript. It **never opens where it cannot dispatch** (`finalizes_user_transcripts`
  false → one WARNING, `relay_mode` shown INERT).
- **`turn_end` is a provider-RESPONSE boundary, not a turn boundary.** One assistant
  turn is often several responses; a client should close its transcript row when the
  caller takes the floor. `turn_end` carries an optional `reply` boolean (set by
  `azure_voice_live` only) for clients on completed-only STT to attach a late caller
  transcript to the right turn.
- **Local onset detection** (`local_barge_in_sec`, `local_barge_in_dbfs`,
  `voice/live/onset.py`) for a provider whose own interruption signal stops once it
  finishes generating.

Every one of these is pinned by `tests/voice/`; the event-ordering invariants live in
`tests/voice/test_voice_live_turn_invariants.py`, which enumerates the orders one
utterance can physically produce instead of scripting one interleaving.

**Config resolution** (`voice/live/config.py`): dataclass defaults → profile
`voice_config`, **read FLAT** — there is no `live` sub-key (a row still carrying one
resolves from it and logs a warning, a one-release shim) and no channel layer (the
`channel` kwarg is accepted and ignored). `hydrate_live_model()` then fills in
`provider` / `model_string` / `extra_config` from the `agent_models` row — preferring
the row **frozen into the profile snapshot at promote** (`runtime/compiler.py`'s
`snapshot["voice_model"]`), so `endpoint`, `voice_type` and `turn_detection` are
promote-governed, falling back to a live read of the row named by `model_id` in dev.
So a live `agent_models` edit does **not** reach prod until the parent profile is
re-promoted. `resolve_persona_prompt()` fetches the persona from the `agent_prompts`
slug named by `persona_prompt_slug`, resolved off the **parent (active) profile**
(parent's snapshot → parent's explicit `prompt_version_ids` pin → a misplaced pin on
the specialist, honoured with a warning → the slug's latest version, also warned), so
a draft persona edit doesn't ship to prod callers ahead of a promote. A profile with
duplex enabled but no model/persona configured gets a loud `LookupError` →
`CLOSE_FORBIDDEN` at handshake, not a silent fallback.

**The persona holds the greeting's RULES; `greeting_trigger` holds its WORDS.** A
script in the model's context beats an instruction about the script — a persona that
carried an example greeting had it recited verbatim — so the greeting's words live only
in the operator-editable trigger, while the persona keeps what config cannot express
(disclose once, never re-introduce). `tests/test_voice_prompt_size_budget.py` pins the
split. The persona's brevity rule names what to OMIT (restatements, trailing offers,
recaps) rather than saying "be brief"; `manual_tests/probe_persona_behaviour.py`
measures spoken seconds, re-greeting, announce and tool-call rates per turn.

**A call can be served by a DIFFERENT profile of the same agent kind —
`voice_config.agent_profile_slug`.** Set it to a profile slug of the same kind and the
handshake builds the agent from THAT profile — its own system prompt, tool subset
(e.g. one without chart or CSV tools) and model — while text chat keeps serving the
kind's active profile. Such a profile is typically `status='specialist'` (so
`load_active` never picks it up). Resolution goes through
`AgentProfileLoader.load_delegate(kind, slug, env)` — keyed on the slug, so it survives
compile-on-promote — and is **fail-open**: an unset key, a typo, or a specialist that
was never promoted into this environment logs a warning and falls back to the kind's
active profile. Note which profile supplies which setting: the live config
(`voice_config`, e.g. `barge_in_enabled`) comes from the **active** profile, while
`memory_config` and the brain's model come from the **specialist**.

**Caller silence is inert at the service** (measured with
`manual_tests/probe_gemini_silence.py`: zero new responses in 30s of silence or
continuous room noise). A reply that *sounds* unprompted is a reply to something the
service committed — check the caller channel of the recording (left = caller) before
suspecting the recovery tiers.

**Blocking calls.** supabase-py is synchronous and this endpoint is async: a blocking
call in the WS handler parks the whole event loop, stalling other callers' handshakes
and audio. The handshake's reads all run through `asyncio.to_thread`
(`_HANDSHAKE_OFF_LOOP`); `tests/voice/test_voice_handshake_off_loop.py` pins it by
thread identity. Treat "an async handler calling supabase-py inline" as a general
defect, not a voice one.

**Guardrails in the endpoint, beyond config resolution:**

- **`agentKind` allow-list** — `_agent_kind_allowed()` rejects (`CLOSE_FORBIDDEN`) an
  `agentKind` this build does not serve (on a scoped build, anything outside the
  scope's kinds), because WebSocket handshakes bypass the HTTP `deployment_gate`.
- **Spend control** — a per-tenant daily audio-second budget
  (`ai_voice_usage_daily`, `voice/live/budget.py`) is reserved at handshake
  (`handshake_charge_sec`), charged periodically (`budget_charge_interval_sec`) and
  settled at close; exhausting it closes with `CLOSE_RATE_LIMITED`. Keyed by the
  RESOLVED business id (uuid5-derived when not already a valid UUID). The periodic
  charger books its delta before the await and un-books it on paths that wrote
  nothing; the teardown charge stays synchronous so a cancel cannot skip
  `session.close()`. See
  [Data Model → `ai_voice_usage_daily`](./data-model.md#ai_voice_usage_daily).
- **Connection + concurrency limits** — a sliding-window per-caller connect limiter
  and best-effort per-replica concurrency caps (`max_concurrent_sessions`,
  `max_concurrent_per_ip`) close with `CLOSE_RATE_LIMITED`; `max_session_sec` (900s)
  ends any session that runs long. These stay keyed by the caller's IP/rate-key even
  for a portal session, so one shared login does not collapse every visitor onto one
  cap.
- **Who ended the call, in the log.** `voice.live: session ended by the client|the
  provider` names the side at teardown, with `client disconnected (code=…)` and
  `provider ended the event stream` from the pumps, and the provider's own close code.
  Read the "session ended by" line first.
- **Origin gate — opt-in, INACTIVE by default.** A request with no `Origin` header (the
  server-to-server path) is permitted. A WebSocket handshake gets no CORS preflight,
  so an `Origin` header that IS present but matches no configured channel is rejected
  (`CLOSE_FORBIDDEN`) **only once at least one channel has a non-empty
  `allowed_origins`** (`core.channel_resolver.any_channel_has_origin_allowlist()`).
  Until an operator populates `allowed_origins` for some channel from
  `/admin/channels`, an unmatched Origin is allowed through (a warning is logged either
  way). If a portal's session cookie is ever set `SameSite=None`, populate
  `allowed_origins` for its channel FIRST: `SameSite=Lax` is the only protection
  against a cross-site WebSocket riding the cookie until the gate is configured.
- **Application close codes** (`voice/live/protocol.py`, RFC 6455's 4000-4999
  application range): `CLOSE_FORBIDDEN` (4403) — tenant/agentKind not permitted;
  `CLOSE_RATE_LIMITED` (4429) — limiter, concurrency cap, or budget;
  `CLOSE_PROVIDER_ERROR` (4502) — the upstream realtime provider failed. Every
  rejection happens **after** `websocket.accept()` — a `close()` called before
  `accept()` is flattened by ASGI into an opaque HTTP 403 handshake rejection.

**Stage-latency metrics (`voice/live/metrics.py`).** The endpoint feeds every
provider event to a per-session collector that measures caller end-of-turn →
first audio (`user_turn_end`, an optional provider-neutral event the Azure provider
emits on `input_audio_buffer.committed`; Gemini has no equivalent, and a marker derived
from the first audio would read ~0ms, so it is left absent), question → first spoken
`ask_agent` result, and `run_delegated_turn` wall time, plus barge-in/turn/tool counts.
Results ship as numeric `waj.voice.live.*` attributes on instantaneous point spans
(`voice.live.turn` / `voice.live.tool` / `voice.live.session` — same
numbers-as-span-attributes pattern as `waj.ttft_ms`), aggregated env-scoped from
ClickHouse by the Dashboard's [Duplex voice card](./01-operate.md#overview). Stamps are
server-side (they exclude browser playback); emission is best-effort and can never
break a call. On Gemini, `turn_ms` overstates an assistant turn (its `turn_complete` is
paced to the audio timeline), so it is not comparable with Azure's.

**Per-stage timing inside the delegated turn.** `delegate_ms` is one aggregate number;
the breakdown lives as `waj.voice.live.stage.*` attributes on the **`voice.agent_turn`
root span** the delegate opens (`voice/live/delegate.py` threads a `stage_timings`
dict through the turn context and stamps it at turn end): `stage.prefetch_ms`,
`stage.history_ms` (session resolve + history load), `stage.memory_ms`, `stage.llm_ms`
+ `stage.llm_rounds`, and `stage.tools_ms` + `stage.tool_names`. `prefetch_ms` and
`history_ms` apply to every voice brain; the `memory` / `llm` / `tools` stages are
filled only by an agent loop that records them, so a turn served by another brain omits
them and its Brain LLM tile shows a dash — expected, not breakage. `stage.llm_ms` is an
**upper bound** on model time. Every timing write is guarded, and a text-chat turn
threads no dict.

**Call recordings (`voice/live/recorder.py`).** Both directions of every call
are captured at the endpoint's two audio choke points (caller mic frames in,
forwarded provider audio out) and assembled at teardown into ONE stereo WAV —
left = caller, right = assistant — persisted to the AI project
(`ai_voice_recordings` + chunked base64 in `ai_voice_recording_chunks`) and
playable from the observability explorer's
[Audio tab](./01-operate.md#conversations). Chunks are placed on a
wall-clock timeline (provider audio arrives far faster than realtime, so
naive concatenation would chipmunk every answer), and a barge-in flush
truncates the assistant track to what the caller actually heard. Each
connection mints a `call_id`, stamped on all of the call's spans as
`waj.voice.call_id`, which is how a trace finds its audio. Bot-driven calls
(the text-only sim provider, or live-mode eval/scenario runs self-declaring
with `?simRun=1`) are additionally stamped `waj.voice.simulated`, rendered as
a `sim` chip in the explorer's trace list and on the recording. A text-only sim run
carries no wire audio and stores a metadata-only `empty` row. Calls of a sensitive
agent kind are never recorded. Capture is best-effort and can never break a call;
per-call cap, retention prune, and the master switch are env-tunable
(`VOICE_RECORDING_*`, see `.env.example`).

### Voice for generic agents

`WS /v1/agents/{slug}/voice` (`endpoints/agents_v1.py`) is a call with one generic
(`custom`) agent. The wire protocol, the turn machinery and the providers are the
shared handler's; the route adds only what a kind with many live profiles needs.

**One handler, kind + slug.** The plain socket picks its agent by kind, which names
nothing when many agents share the kind. The route parks a `VoiceRoute` (the slug, the
caller check, an optional profile pin) on the socket and calls the same `voice_live`
handler, which reads it at four points: whose profile, who may call, which budget
bucket, and whose session. A `VoiceRoute` is never built from a query parameter, and
`WS /voice/live?agentKind=custom` is refused (`slug_addressed` on the registration).

| | Plain socket | Generic agent's address |
|---|---|---|
| Settings come from | the kind's active profile | the profile `(custom, slug, environment)`: fails closed, an unknown slug closes 4403 "Agent not found." and never resolves to another agent |
| Caller | anonymous, or the kind's own auth | `agents_v1._require_caller`, the chat route's check (a superuser token; API keys once they exist there). `Authorization` header, or `{"type": "auth", "token": "…"}` as the first frame |
| Brain | the active profile or `agent_profile_slug` (falls back) | the same agent, or the custom agent named by `agent_profile_slug` (no fall-back: a slug that does not exist refuses the call) |
| Persona | the prompt named by `persona_prompt_slug` (required) | that prompt when one is named, else a built-in persona that sends every question to `ask_agent` (`runtime/agents/custom_voice.py`) |
| Greeting | `greeting_trigger`, blank = silent | `greeting_trigger`, blank = a built-in "greet and ask how you can help" |
| Daily budget, recording | the business id | one bucket per agent: `uuid5("voice-live-agent:custom:<slug>")`, shown on the Voice tab |
| Session row | `agent_kind` | `agent_kind='custom'`, `source='api'`, `metadata.agent_slug=<slug>`, `metadata.channel='voice'` (as the chat route stamps it) |

**Gates.** Off unless `ENABLE_GENERIC_AGENTS=1`, never on a scoped build, and a
malformed slug is refused before anything is read. Those three close the handshake
before `accept()`, so the route looks absent.

**Why one bucket per agent.** A generic agent is called with an admin token or an API
key, not on behalf of a tenant, so there is no business id to charge. The realtime
audio is the agent owner's cost whoever dials, an API key carries no user id, and a
per-caller bucket would multiply the daily cap by the number of keys. The bucket takes
its own allowance in `ai_voice_budgets` under that id (the speech model's page);
without one the server default (`VOICE_LIVE_DAILY_SECONDS`) applies. `TurnIdentity`'s
`business_id` stays empty, as on the chat route.

**Draft and published.** The address serves the published agent. `?profileId=<id>`
calls a draft copy of the same agent instead, for an operator only (a verified
superuser token, or the trusted simulated-eval path); a pin naming a different agent
is ignored. The Voice tab's Call button and the voice eval runner send it. On a
snapshot-served environment the settings, the realtime model row and the persona pin
come from the agent's own promote snapshot, exactly as for any other kind.

**No realtime key.** A realtime provider needs a key the engine does not ship with.
The handshake checks before it dials (`voice/live/readiness.py`) and closes with the
reason: 4403 "No realtime voice model is set for this agent…", or 4502 "Realtime model
key missing: set GOOGLE_API_KEY on the server." (the variable is the one the model row
names for `azure_voice_live`). Nothing is charged and no agent is built. The Voice tab
shows the same sentence in place of the Call button.

**Testing without a key.** `tests/voice/test_custom_agent_voice.py` runs the whole
path on the simulated provider: socket, gates, auth frame, greeting, a caller turn,
`ask_agent`, `run_turn`, the custom agent's answer spoken back, and the identifier
provenance guard. A real key adds only the audio: speech recognition, the realtime
model choosing to call `ask_agent`, and synthesis.

---

## Memory layer

Server-side short-term and long-term memory, per agent. It ships dark
(`memory_config.enabled: false`) and is enabled per environment from the Config tab of
`/admin/memory`. An agent opts in by calling `core/memory/service.py` around its turn;
the schema carries `agent_kind`, so enabling another kind is config, not migration.
The engine's `custom` agent does not use it yet; a plugin's agents may.

### Config — compile-on-promote

`agent_profiles.memory_config` JSONB holds all knobs (see
[Data Model → memory_config](./data-model.md#agent_profilesmemory_config-jsonb)).
It is listed in the compiler's `_SHALLOW_FIELDS` so it is embedded verbatim into the
`agent_profile_versions` snapshot at promotion time — production always reads a
consistent, immutable copy. The `resolve_memory_config` helper in
`core/memory/config.py` deep-merges operator overrides over well-typed defaults so
callers always receive a complete config. `run_turn` reads the history-window part of
it (`resolve_window_config`) for every agent.

### Read path — context injection (blocks prompt build)

Runs before the LLM call on every turn of an opted-in agent when `enabled` is true:

1. Embed the incoming user message (see [Embeddings](#embeddings)).
2. Call the `match_user_memory` RPC: retrieve the top-K `active` memories for
   `(user_id, agent_kind)` above `retrieval.min_similarity` (cosine).
3. If `retrieval.always_include_pinned`, also fetch all pinned memories and merge
   by id (deduplicating).
4. Load `ai_chat_sessions.running_summary` for the session.
5. Build a **memory context block** prepended to the system prompt:
   - `## Conversation summary so far` — the rolling summary of older turns.
   - `## What you remember about this user` — the retrieved + pinned facts.
6. Inject the block into the prompt before the recent verbatim turns and the current
   message.
7. Stamp the assistant turn's `metadata.memory` with `retrieved_memory_ids`,
   `summary_used`, and `config_enabled` for the Inspector.
8. Bump `last_used_at` / `use_count` on retrieved memory rows (best-effort).

The embedding call adds approximately one round-trip of latency before the first SSE
token when memory is enabled — on a voice profile it sits between the caller's last
word and the model call, so leave memory off there unless you need it. Degradation: any
exception in the read path is caught and logged; the turn runs with no memory block.

### Write path — fire-and-forget (after SSE stream)

Runs asynchronously after the model finishes, from within the agent's own
`run_stream` (the long-term memory write is agent-owned). The recalled-memory
provenance for the turn is surfaced via `turn_meta.metadata.memory` and stamped
onto the assistant transcript row's `metadata.memory` by `run_turn` (not by the
agent). Never blocks the response; failures are logged and degrade to no-write:

1. **Extract** — a model pass (`extraction.model_slot`, default `light`, which falls
   back to the main model) over the user message and assistant reply produces 0–N
   candidate facts as `{content, importance}` JSON. Facts below
   `extraction.importance_threshold` are dropped; at most `extraction.max_per_turn`
   facts survive.
2. **Embed + dedup** — each surviving fact is embedded. `match_user_memory` is called
   with `p_match_count=1` and `p_min_similarity=0` to find the nearest existing memory.
   If cosine similarity ≥ `dedup.similarity_threshold`, the fact is **merged** into the
   nearest row (content updated if richer, `importance` and `updated_at` bumped,
   `pinned` preserved); otherwise a **new row** is inserted (`source='auto'`,
   provenance set from session and message ids).
3. **Summarize** — if `(total_messages − summary_message_count)` ≥
   `summary.trigger_every_n_turns × 2`, a model pass (`summary.model_slot`) regenerates
   `running_summary` by folding the prior summary and the turns it doesn't cover yet
   (up to the `summary.keep_recent_verbatim` verbatim window) into a ≤150-word summary.
   `total_messages` is the session's **whole** persisted transcript (an exact
   database count, `store.count_messages`), not the agent's capped history window:
   counting the window froze the summary after its first refresh. The save starts
   while the reply is still streaming, so this turn's rows are counted in even if
   `run_turn` hasn't persisted them yet. A refresh reads only the uncovered part of
   the transcript, paged past PostgREST's 1,000-row limit, and the next one is due
   `trigger_every_n_turns` turns later. The write is conditional on
   `summary_message_count` being unchanged since it was read, so when two saves for
   the same session overlap, the slower one is dropped instead of overwriting the
   other's summary.

### Extraction and summarization prompts

Per agent. A profile can pin its own **`memory_extraction`** (what facts to save) and
**`memory_summary`** (how to summarise) prompts in `prompt_version_ids`, exactly like
its `system` prompt: edited in the Agents screen, versioned, validated on save, and
frozen into the promote snapshot. `core/memory/service.py` resolves them the way
`prepare()` resolves the system prompt (snapshot copy in prod, pinned version in
dev/staging) and reads them off the event loop. An agent with neither pinned uses the
defaults in `core/memory/prompts.py`. Write your own when the agent's users are not the
people the defaults assume — a customer-facing agent, for example, must not save a
staff member's name as the customer's own preference. An agent's prompt must keep
exactly the default's placeholders (`{user_message}`/`{assistant_response}`,
`{prior_summary}`/`{older_messages}`); one that doesn't is ignored with a WARNING and the
default is used, rather than every extraction failing silently.

### Embeddings

The provider is chosen by `core/embedding_provider.py` (`EMBED_PROVIDER`), for user
memory and for knowledge bases alike: Gemini's `gemini-embedding-001` by default, Azure
OpenAI `text-embedding-3-small` with `EMBED_PROVIDER=azure` (needs
`AZURE_OPENAI_ENDPOINT` and a key), or any OpenAI-compatible server (Ollama, vLLM, LM
Studio, a hosted catalog) with `EMBED_BASE_URL` and `EMBED_MODEL`, which needs no cloud
key. Each caller asks for its column's width: 768 for `ai_user_memory.embedding`, 1536
for `ai_kb_embeddings.embedding`. Gemini and Azure produce either width directly. An
OpenAI-compatible model has one width of its own, checked at first use: a narrower
vector is zero-padded (cosine similarity, the only distance these columns are searched
by, is unchanged), a wider one is requested at the column's width (`dimensions`) and
refused with the model and both widths named if the server does not comply. The
request runs through `httpx` on a worker thread (`asyncio.to_thread` on the memory
path, the tool executor for `search_knowledge`, the job thread for ingestion), never
on the event loop. **The provider and model are not a runtime toggle**: vectors from
different models are not comparable and pgvector does not error, so changing them
without re-embedding makes knowledge search and memory recall return nothing useful,
silently. Migrate first with `python -m scripts.reembed --target kb|memory --apply`
(dry-run by default) under the new settings, then keep them. Knowledge bases record
the model that embedded them (`agent_data_sources.config.embedded_with`) so the portal
can flag one that was left behind; memory rows carry no such record. Settings, the
width rule and the migration are in
[Configure → Knowledge Base](./02-configure.md#knowledge-base). `to_vector_literal` serializes float lists to the pgvector
text format `"[0.1,0.2,...]"` for PostgREST compatibility.

### Admin surface

`/admin/memory` exposes three tabs:
- **Memories** — browse / edit / delete (soft-archive) / pin a user's memories; add manual facts.
- **Config** — labeled editor for `memory_config` with the master `enabled` toggle.
- **Inspector** — per-session running summary + per-assistant-turn retrieved memories.

See [Configure → Memory](./02-configure.md#memory).

---

## Cross-cutting behaviours

- **Audit** — every mutation calls `record_change(entity_type, entity_id, action, …)`
  writing `agent_audit_log` with before/after. Best-effort: it never fails the user's
  request. Secret values are masked before logging. See [Govern](./04-govern.md).
- **Cache invalidation** — profile/tool/source edits invalidate the profile loader
  cache; channel edits invalidate the channel cache and mark CORS dirty (banner on
  the Channels screen until restart — the CORS allow-list is captured at boot).
- **Background jobs** — eval runs, E2E runs, pytest runs and KB ingestion run on
  daemon threads tracked in `admin_jobs`, with cooperative cancellation. See
  [Operate → Jobs](./01-operate.md) and [Test](./03-test.md).
- **Plugin seams** — a deployment's plugin attaches its own routes, seeders, env
  requirements, channel kinds, integration cards, search sources and display rules
  through the hooks in `core.plugins.HOOKS`. A missing plugin package or an error inside
  a hook is fatal at import (a product route that silently failed to load would look
  like a healthy boot); only a hook module a plugin does not implement is skipped.

---

## Global search

The top-bar **Search** button (or **⌘K** / Ctrl+K anywhere) opens a palette
(`admin/static/global-search.js`, markup at the end of `_base.html`). It POSTs
`{q, env}` to `/admin/search.json` (`admin/search.py`) — the query rides in the
body so a phone number never lands in a URL or an access log; `GET
/admin/search.json?q=&env=` returns the same payload for older callers and the
admin MCP `search` tool. Rows: `{group, type, label, context, href, icon, tech?,
person?}`; `notes` lists sources skipped this time.

| Group | Source |
|---|---|
| Screens / Settings / Actions | `admin/search_registry.py`, **generated from `admin/nav.py`** (sidebar, section tabs, Settings hub) plus `EXTRA_PAGES`, `AGENT_TABS` (an agent's sections; "support prompt" pairs agent + tab into a deep link), `ACTIONS` and `SYNONYMS` (old names: Dashboard, Traces, Audit, Evals…). Matched in-process. |
| Docs | `admin/search_docs.py`: full text of `docs/partner/` (Help) and, off scoped builds, `docs/admin-portal/`, split at headings; a hit links to `…#<heading-slug>` (`docs.html` assigns the same slugs client-side and scrolls to the hash). |
| Agents, Knowledge & connections, Testing, Team | `admin/search_sources.py`: agent profiles (env column), models (env column), tools, toolsets, knowledge bases, skills, channels, test suites + their latest runs, model comparisons, voice checks, superusers. |
| Conversations & people | AI `ai_chat_sessions` (exact id, title, and any record keys a plugin links sessions by), ClickHouse trace/session id prefixes (30 days), main `business` (name → its recent sessions, full engine only), and any people sources a deployment's plugin adds (`search` hook, `people_sources()`). |

Rules: every source runs in parallel under a 3 s deadline and fails soft (a
quiet note, never an error); AI reads use `client_for(env)` and ClickHouse
filters `waj.env`, so results follow the STAGING/PRODUCTION switch; a scoped
build narrows every row to the scope's agent kinds and never reads the main
database's business data; people are shown masked (`admin/search_pii.py`: "Cristel T.",
"+971 ••• 9901", "cr•••@example.com") and only when they have a conversation in the
selected env; links go to `/admin/sessions/{id}` (resolves to the conversation's
trace) or the explorer. No source logs the query: errors log the source name and
exception type only, httpx request lines from search threads are dropped, and
the access log redacts `q=`. Recent items live in the viewer's `localStorage`
(never people rows). Scope prefixes: `>` actions, `@` people & conversations,
`?` help & docs. `tests/admin/test_search_registry.py` fails when a GET page
under `/admin` has no entry and no `ROUTE_EXCLUSIONS` reason.
