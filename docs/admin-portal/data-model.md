# Data Model

**Docs › Data Model**

The Supabase tables behind the admin portal. Each agent's behaviour lives in these
rows — see [Architecture](./00-architecture.md) for how they compose.

> **Two-project split.** All `agent_*` tables, `admin_jobs`, `test_fixtures`,
> `ai_chat_sessions` / `ai_chat_messages`, and `ai_user_memory` now live in the
> **AI project's `public` schema** (previously `wajagents` / `public` in the main
> project). Migrations live in `supabase/migrations/`; RLS policies in
> `supabase/db-functions/rls/agent_*.ts`. The `user` and `business` tables
> stay in the **main project** (`public`). `business_id`, `user_id`, `created_by`,
> and `branch_id` columns in the AI project are **loose UUID references** — no
> cross-project foreign keys. RLS on all AI-project tables is **service-role-only**;
> the app is the gateway and enforces tenant scoping in code.

**Shared patterns**
- **Schema** — all AI-domain tables (`agent_*`, `admin_jobs`, `test_fixtures`,
  `ai_chat_sessions`, `ai_chat_messages`) are in the AI project's `public` schema.
- **Audit columns** — `created_by` / `created_at` / `updated_by` / `updated_at` on
  most tables (attached by global triggers).
- **Environment scoping** — env-scoped tables carry an `agent_environment` enum.
  The portal uses **two** values: **`staging`** (the live, editable env) and
  **`production`** (immutable, served from promotion snapshots). The enum still
  technically defines a legacy `dev` value, but it is no longer used or surfaced.
- **Versioning** — an immutable `_versions` table paired with a live mutable parent
  (prompts, profiles).

---

## Quick map — table → screen

| Table | Purpose | Edited from |
|-------|---------|-------------|
| `ai_user_memory` | Per-user durable memory facts (long-term memory) | [Configure → Memory](./02-configure.md#memory) |
| `agent_profiles` | One row per agent kind/env: the runtime config | [Configure → Agent Profiles](./02-configure.md#agent-profiles) |
| `agent_types` | User-defined agent categories (name, colour, order); organisational only | [Configure → Categories](./02-configure.md#categories--adminagent-types) |
| `agent_profile_versions` | Version snapshots (promotion + on-demand save/restore) | [Deploy → Environments](./05-deploy.md#environments), [Configure → Agent Profiles](./02-configure.md#agent-profiles) (version bar) |
| `agent_prompts` | Prompt slots per agent kind | [Configure → Agent Profiles](./02-configure.md#agent-profiles) |
| `agent_prompt_versions` | Immutable versioned prompt bodies | [Configure → Agent Profiles](./02-configure.md#agent-profiles) |
| `agent_models` | LLM definitions (provider, cost, knobs) | [Configure → Agents (Models tab)](./02-configure.md#models-tab) |
| `agent_tools` | Tool registry (Python + HTTP) | [Configure → MCP & Tools](./02-configure.md#mcp--tools) |
| `agent_data_sources` | Knowledge / RAG sources | [Configure → Knowledge Base](./02-configure.md#knowledge-base) |
| `ai_kb_pages` | Shared page store for dashboard-created pgvector KBs (AI DB) | [Configure → Knowledge Base](./02-configure.md#knowledge-base) |
| `ai_kb_embeddings` | Shared embedding store for dashboard-created pgvector KBs (AI DB) | [Configure → Knowledge Base](./02-configure.md#knowledge-base) |
| `agent_mcp_servers` | Bundle or external MCP servers | [Configure → MCP & Tools](./02-configure.md#mcp--tools) |
| `agent_mcp_tools` | Bundle membership (MCP ↔ tool mapping) | [Configure → MCP & Tools](./02-configure.md#mcp--tools) |
| `agent_profile_mcp_servers` | Profile ↔ MCP attachment with optional tool filtering | [Configure → MCP & Tools](./02-configure.md#mcp--tools) |
| `agent_skills` | Skill identity (slug, description, agent kind, platform/tenant scope) | [Configure → Skills](./02-configure.md#skills-adminskills) |
| `agent_skill_versions` | Immutable versioned skill bodies (playbook markdown) | [Configure → Skills](./02-configure.md#skills-adminskills) |
| `agent_skill_resources` | Reference files bundled with a skill version | [Configure → Skills](./02-configure.md#skills-adminskills) |
| `agent_profile_skills` | Profile ↔ platform-skill attachment (ordered) | [Configure → Agent Profiles](./02-configure.md#agent-profiles) |
| `agent_channels` | Request sources + CORS/agent gating | [Configure → Channels](./02-configure.md#channels) |
| `agent_eval_suites` | Eval suites + scenarios, bound to a profile | [Test → Evals (Suites & Runs)](./03-test.md#suites--runs) |
| `agent_eval_runs` | One suite/live-chats/bake-off execution + results | [Test → Eval run detail](./03-test.md#eval-run-detail) |
| `agent_eval_schedules` | Recurring eval coverage (prod-chat + legacy suite) | [Test → Evals (Production monitoring)](./03-test.md#production-monitoring) |
| `agent_bakeoffs` | Persisted N-variant × M-suite profile comparisons | [Test → Evals (Bake-offs)](./03-test.md#bake-offs) |
| `agent_e2e_runs` | End-to-end runs against a deployed env | [Configure → Agent Profiles](./02-configure.md#agent-profiles), [Deploy](./05-deploy.md#environments) |
| `agent_test_runs` | pytest invocations | [Test → pytest viewer](./03-test.md#tests--pytest-viewer) |
| `test_fixtures` | Named test inputs (per env) | [Test → Test Fixtures](./03-test.md#test-fixtures) |
| `agent_deployment_targets` | Deploy targets (engine base URLs, e.g. `https://engine.example.com`) | [Deploy → Deploy (Targets tab)](./05-deploy.md#targets-tab) |
| `agent_env_vars` | Per-env variable registry | [Deploy → Environments (Variables tab)](./05-deploy.md#variables-tab) |
| `provider_keys` | Model provider keys, stored encrypted | [Configure → Model keys](./02-configure.md#model-keys-settings--model-keys) |
| `agent_audit_log` | Append-only change log | [Govern → Audit Log](./04-govern.md#audit-log) |
| `admin_jobs` | Background job tracking | [Operate → Background jobs](./01-operate.md#background-jobs) |
| `agent_traces` | Per-request observability summary | [Operate → Traces](./01-operate.md#conversations) |
| `ai_conversation_insights` | Per-conversation outcome / topic / language, derived by a background sweep | [Operate → Overview](./01-operate.md#overview) |
| `ai_background_leases` | One-process-at-a-time lease + last result for background sweeps | [Operate → Overview](./01-operate.md#overview) |
| `ai_voice_usage_daily` | Per-tenant daily duplex-voice audio-second budget | [Configure → Duplex voice](./02-configure.md#duplex-voice-voice_config) |
| `ai_voice_budgets` | Per-tenant daily allowance overriding the engine default | [Configure → Duplex-voice budgets](./02-configure.md#duplex-voice-budgets-settings--voice-budgets) |
| `api_keys` | Portal-issued API keys: a digest and a display prefix, never the key | [Govern → API keys](./04-govern.md#api-keys) |
| `engine_setup` | First-run setup state (one row): a digest of the setup code, never the code | [Govern → First-run setup](./04-govern.md#first-run-setup) |
| `ai_voice_scenarios` / `ai_voice_scenario_runs` | Live-call acceptance scenarios + their executions | [Test → Voice scenarios](./03-test.md#voice-scenarios--live-call-acceptance) |
| `ai_voice_recordings` / `ai_voice_recording_chunks` | Per-call duplex-voice audio, playable from the explorer's Audio tab | [Operate → Traces](./01-operate.md#conversations) |
| `user` | Accounts + superuser flag | [Govern → Users & Roles](./04-govern.md#users--roles) |

---

## Config

### `agent_profiles`
One row per `(agent_kind, slug, environment)` — the compiled config a runtime agent
loads. Key columns: `model_main_id` (→ `agent_models`; the legacy `model_light_id`
column is retained but vestigial — the light model is retired and the loader ignores it),
`prompt_version_ids` (slot→version JSONB), `tool_ids[]`, `data_source_ids[]`,
`mcp_server_ids[]`, `downstream_profile_ids[]`, `parent_profile_id`, `endpoint_path`,
`status` (`draft`/`active`/`archived`/`template`/`specialist` — only `active` is
served; `template` rows are the protected fork catalog surfaced by the `templates`
tool + "New from template" gallery, never served at runtime; `specialist` rows serve
only via delegation from a parent profile and are exempt from the dual-active-per-kind
guard), `delegatable` (boolean `NOT NULL DEFAULT false` — opt-in that exposes this
profile to its parent as an `ask_<slug>` delegation tool at runtime), `escalation_policy`,
`input_validators`, `voice_config` JSONB. Only `active` rows are visible to the runtime.

**`served_version_id`** (nullable FK → `agent_profile_versions.version_id`,
`ON DELETE SET NULL`, added in `20260724120000_profile_served_version.sql`) — a
per-(profile, environment) served-version pin: when set, `AgentProfileLoader` serves
exactly that `agent_profile_versions` snapshot for every request, bypassing the
environment's default resolution (staging = live row, production = latest promoted
snapshot / prod bundle) entirely. **NULL is the default for every row** — unset,
resolution is byte-for-byte unchanged from before this column existed. The only
write path outside raw SQL is `admin.services.profile_versions.pin_served_version`
(the agents screen's version-bar Pin action / MCP `profile_versions action=pin`),
which enforces that `version_id` belongs to `profile_id` before writing (a mismatched
pin raises rather than being silently accepted) and invalidates the loader's cache
for `(agent_kind, environment)` so the pin (or its removal) takes effect on the next
request. See [Architecture → Served-version
pin](./00-architecture.md#served-version-pin-agent_profilesserved_version_id) for the
read-side resolution order and its ownership guard.

**`agent_type_id`** (nullable FK → [`agent_types`](#agent_types)`.agent_type_id`,
`ON DELETE SET NULL`, added in `20261123040000_agent_types.sql`) — the agent's
user-defined **category**, or `NULL` for uncategorised (the default for every row).
It is organisational only: the runtime never reads it, and it is **not** part of an
`agent_profile_versions` snapshot. Written by `admin.services.profiles.update_profile_row`
(the Settings tab's Category picker / MCP `update_profile(agent_type=...)`), which
checks the category exists and writes every row of the same `(agent_kind, slug)` in
that database, and by `admin.services.templates.use_template` (the New agent wizard).
A fork copies it; promote carries it (matched by slug across two databases).

`agent_kind` is the `ai_agent_kind` Postgres enum. The engine's own values are
**`custom`** (the generic, configuration-only agent: many profiles told apart by
slug) and the `agents_*` kinds of the fictional `/agents` demo (`agents_support`,
`agents_inquiries`, `agents_collection`, `agents_sales`, plus the `agents_title` /
`agents_suggestions` sub-agents). A deployment's plugin may add its own kinds; new
kinds are added via `ALTER TYPE ... ADD VALUE` migrations. See
[Architecture → Runtime agents](./00-architecture.md#runtime-agents).

**`voice_config` JSONB** (default `{}`): **the duplex-voice settings block,
read FLAT.**

Two things changed on 2026-08-17 and both are easy to get wrong:

1. **There is no `live` sub-key.** `voice_config` used to hold the turn-based
   settings at the top level with duplex settings nested under
   `voice_config.live`. The turn-based `/voice/*` routes are retired, so the
   nesting had nothing left to disambiguate: `voice_config` IS the duplex
   block, and every key in the table below sits at the top level. A row still
   carrying `{"live": {...}}` resolves from the nested block ALONE and logs a
   warning (`voice/live/config.py::_voice_block`) — a one-release shim for an
   unmigrated branch DB or a pre-migration snapshot restore. The
   `unify_voice_config` migration flattens it, and every WRITER (the Agents
   form, both voice seeders, `update_profile` over MCP) promotes such a row on
   the way through (`promote_legacy_voice_config`, mirroring the migration:
   duplex keys up, the old top level parked under `_retired_turn_based`), so a
   save repairs it. Never write a `live` key back by hand — because the shim
   resolves a nested block ALONE, one would hide every flat key instead of
   adding to them.
2. **The old turn-based keys are gone with their routes.** `stt_provider`,
   `stt_model`, `tts_provider`, `tts_model` and `sample_rate` were read only
   by `voice/config.py::resolve_voice_config`, which is deleted. `voice_id`, `language` and `enabled` survive as **duplex**
   keys below — note `voice_id` now means a realtime-provider voice
   (`"Kore"`), not an Azure neural voice name.

The block promotes into the `agent_profile_versions` snapshot (added in
snapshot schema v3; current schema is v5), so the production runtime reads
voice config from the snapshot while staging reads the live row. Absent keys
fall back to the `LiveVoiceConfig` dataclass defaults.

**Resolution precedence:** dataclass defaults → profile `voice_config`. That is
the whole chain — there is **no channel layer**. `agent_channels.config.voice`
(and `config.voice.live`) is no longer read by anything: no seeder wrote it, no
screen labelled it, and an audit on 2026-08-17 found zero rows using it in any
deployed database. `resolve_live_config`
still accepts a `channel` kwarg and ignores it, so existing call sites keep
working; it is dropped with the `live` shim.

#### `voice_config` keys (duplex voice)

Shape owned by `voice/live/config.py`'s `LiveVoiceConfig` / `_VOICE_FIELDS`,
for the realtime WebSocket edge (`<portal_prefix>/voice/live`).

| Key | Type | Description |
|-----|------|-------------|
| `enabled` | bool | Duplex voice for this profile. Default `false`. |
| `text_only` | bool | Ambient-scribe mode: the realtime model responds in text modality only and never synthesizes speech. Default `false`; set `true` on a profile that should listen and write, not speak. See [Configure → Duplex voice](./02-configure.md#duplex-voice-voice_config). |
| `model_id` | uuid | `agent_models` row id for the realtime model (`extra_config.realtime: true`). Not an agent's `model_main_id`. |
| `voice_id` | string | Provider voice identifier. Default `"Kore"`. |
| `language` | string | BCP-47 tag. Blank = provider default. |
| `persona_prompt_slug` | string | `agent_prompts` slug for the spoken persona (e.g. `support_voice_persona`), resolved at connect time off **this (the parent/active) profile** — its compile-on-promote snapshot, else its `prompt_version_ids[slug]` pin, matching every other prompt's promote/rollback governance. Latest-version is only the fallback for a fresh/unpinned tenant, and it warns. Backfilled by `20260817121000_pin_voice_persona_versions`, into the live row AND into `agent_profile_versions.snapshot` (`prompt_version_ids` + `prompt_contents`) — without the snapshot half prod would keep resolving latest-version and logging "run the backfill" on every connect. A pin left on the voice **specialist** instead is still honoured, with a warning naming both profiles, but only while the parent carries no entry; once it does, the parent wins outright. |
| `phrase_list` | string[] | Recognition-boosted domain terms (e.g. a brand name, `"IBAN"`, branch names) handed to a provider with a real ASR stage. Consumed by `azure_voice_live` — folded into the Voice Live `session.update`'s `input_audio_transcription.phrase_list`; Gemini ignores it. Default `[]`. |
| `caller_languages` | string[] | BCP-47 tags the caller may speak (e.g. `["ar-JO", "en-US"]`). Two or more make the Azure recognizer auto-detect among them (`input_audio_transcription.language = "ar-JO,en-US"`, max 10) instead of being locked to the session `language` — which the app's UI locale overrides, so an English-UI caller speaking Arabic was transcribed as romanized English (2026-10-02). `azure-speech` only; ignored by the OpenAI recognizers and Gemini. Default `[]`. |
| `voice_by_language` | object | Primary-language-subtag → voice name map (e.g. `{"ar": "ar-SA-HamedNeural", "en": "en-US-JennyNeural"}`), resolved by `resolve_session_voice()`. Keyed on the session's PRIMARY BCP-47 subtag (`language.split("-")[0]`), so one `"ar"` entry covers every regional variant (`ar-SA`, `ar-AE`, …). Falls back to `voice_id` when the session's language has no matching entry, or when `language` is unset. Default `{}`. |
| `greeting_trigger` | string | Text turn injected right after the socket opens so the assistant speaks first — a realtime session is silent until spoken to, so without it the caller waits and assumes the line is broken. Operator content, not a source constant; blank disables the greeting. Default `""`. |
| `delegate_timeout_sec` | number | Hard cap on one delegated `ask_agent` turn. Default **30**. |
| `progress_after_sec` | number | Speak a holding line once a delegated turn exceeds this. `0` disables it. Default **4** (was 8, set when turns ran ~10s). Fires on ~17% of turns at today's answer distribution — see [Configure](./02-configure.md) for the measured firing-rate table and why 2.5 collides with the answer. |
| `progress_spoken` | string | The holding line. Operator text that asserts no facts, so it stays inside the same verbatim-safety contract as `FAILURE_SPOKEN`. Default `"Still working on that, one moment."`. |
| `progress_spoken_by_language` | object | Per-language holding lines, keyed on the PRIMARY subtag exactly like `voice_by_language` (`resolve_progress_spoken()`). Unmapped language → `progress_spoken`. Default `{}`. |
| `failure_spoken` | string | Spoken when a delegated turn errors, times out, or produces nothing. Blank = the built-in per-language wording (EN/AR). Operator text asserting no facts, so it stays inside the verbatim-safety contract. |
| `failure_spoken_by_language` | object | Primary-subtag → failure line (e.g. `{"ar": "…"}`). Falls back to `failure_spoken`, then to the built-in. |
| `on_screen_spoken` | string | Spoken in place of a table/chart, and the ENTIRE spoken output of a charts-only turn. Blank = the built-in per-language wording. |
| `on_screen_spoken_by_language` | object | Primary-subtag → on-screen pointer. Falls back to `on_screen_spoken`, then to the built-in. |
| `say_exactly_instruction` | string | The instruction wrapped around an answer the **endpoint** delivers itself (relay custody, stall/blank-call recovery, the answer watchdog's re-delivery). Reaches the model as a caller-side text turn, so it is a prompt and follows the call's language. Must contain `{spoken}`. Blank = the built-in per-language wording (EN/AR); a template that cannot carry the answer falls back with a WARNING. |
| `say_exactly_instruction_by_language` | object | Primary-subtag → instruction. Falls back to `say_exactly_instruction`, then to the built-in. |
| `stream_sentences` | bool | Speak the agent's answer sentence-by-sentence as it streams rather than waiting for the whole turn. Default **`true`**, but structurally gated off regardless of this value on a provider that can't take several tool results for one call id (`supports_streamed_tool_results = False`, i.e. `azure_voice_live`). A **second, separate** capability added 2026-08-19 does NOT gate it: `transcribes_streamed_tool_results` (False on `google_genai`) says the provider speaks a streamed answer in full but transcribes only the leading fragment of each response, so the endpoint sends the caller's screen its own copy of each sentence instead of the provider's truncated echo — streaming stays on and keeps its latency win. Set an explicit `false` per tenant only if sentences repeat or land out of order. **Worth ~68ms at the median, not the "~10s → ~3s" this line used to claim** — measured over 85 streamed calls on a demo deployment 2026-08-19, first-sentence delivery led whole-answer delivery by a median 68ms, *flat* across 1-, 2- and 3-sentence answers. The brain emits its answer in a burst (probed the same day: 1.0–1.6s producing nothing, then the whole answer in 154–539ms), so the burst length is the entire prize and streaming already takes most of it. Leave it on — long answers do win real time (a 10-sentence answer led by 3.7s) — but do not expect it, or the chunker's boundary rules, to move a median call. |
| `answer_watchdog_sec` | number | Post-delivery answer watchdog. A delivered answer's `response.create` can be rejected by a racing response, and the provider's own retry stands down whenever a commit interleaved — a NOISE commit (empty transcript) too, leaving the result item in the conversation with nothing speaking it: dead air with no error (live 2026-08-03, tid_shorthand ~1/3). If a delivered `INTERRUPT` answer produces no assistant audio within this window and the caller hasn't superseded it (`interrupt_epoch` unchanged), the endpoint re-delivers it ONCE as a say-exactly text turn (cancel-then-speak, the relay delivery discipline). Fires with a `voice.live: delivered answer … produced no audio` WARNING. `0` disables — the rollback lever. Default **4**. **Armed only on a provider that declares `reports_response_in_flight`** (`azure_voice_live`; not Gemini Live) — the watchdog's safety margin is that reading, not the threshold, and on a provider that cannot report the floor it degrades to a duplicate generator. The voice screen renders an inline "not armed" note there. |
| `stall_recovery_sec` | number | Announce-then-stall recovery. The native model sometimes completes its reply right after the looking-it-up line ("one moment", «ثانية وحدة») without emitting the `ask_agent` call at all (`tool_calls=0`, clean wire — live client sessions 2026-08-04, EN and AR, after a persona same-breath rule failed to hold); in server-VAD mode nothing prompts it again, so the caller waits forever. An announce phrase in a **completed** reply while the caller's committed question is still unanswered is the tripwire (no legitimate reply declares a lookup and ends with no call behind it — matching is normalized against the built-in `_ANNOUNCE_TOKENS` list in `endpoints/voice_live.py`). The tripwire now **re-prompts first** (2026-08-05): on a provider declaring `supports_reprompt` the endpoint hands the model the floor back immediately — a bare `response.create`, nothing added to the conversation — and the owed `ask_agent` lands in ~0.4s, on the normal tool path with its own call id. This grace is what the endpoint waits AFTER that ask before giving up: if no call arrives, it dispatches the committed question itself via the recovery path (say-exactly delivery), exactly as before. `waj.voice.live.reprompts` on the session span counts the asks; a session with reprompts > 0 and no recovery WARNING behind them is stalls costing ~1s instead of ~10s. Fires with a `voice.live: model announced a lookup and never called ask_agent` WARNING. `0` disables — the rollback lever. Default **2**. |
| `barge_in_enabled` | bool | `true` | Master switch for interrupting her mid-sentence. `false` = she keeps the floor: a committed utterance waits for the sentence she is speaking, and an answer already in flight is still spoken instead of silently dropped. Six mechanisms here can cancel a response; on a live mic whose recognizer invents words from room noise they starve her, so a scripted demo turns this off. |

Both timing defaults were retuned from live calls (8s not 5s, 30s not 15s) —
see [Architecture → Duplex voice edge](./00-architecture.md#duplex-voice-edge)
for why, before changing them back.

The **voice cockpit** (`/admin/agents/{profile_id}/voice`) is the single
portal writer of this block — its POST resolves the OWNER row and owns every
`_VOICE_FIELDS` key. The Agents screen's mapping form does **not** write
`voice_config`: its 26-key duplex fieldset was removed 2026-08-26 (it wrote
to whichever row was being viewed — on a served specialist, a row the runtime
never reads), leaving a link card in its place. The admin MCP's
`update_profile` (flat `voice_config` / `voice_config_patch`) remains the
API writer — see
[Configure → Duplex voice](./02-configure.md#duplex-voice-voice_config).
`hydrate_live_model()` fills `provider`/`model_string`/`extra_config` from the
`agent_models` row — preferring the row frozen into the profile snapshot at
promote (`snapshot["voice_model"]`, so `endpoint`/`voice_type`/`turn_detection`
are promote-governed like the brain model), falling back to a live read of the
row named by `model_id` where there is no snapshot. It and the persona lookup
both raise `LookupError` (closing the socket with `CLOSE_FORBIDDEN`) on a
missing/unknown reference rather than silently defaulting.

### `agent_types`

User-defined agent **categories** ("Support", "Sales", "Internal tools"): groups for
the agent table, its filter and reporting. Migration `20261123040000_agent_types.sql`;
service-role RLS like every other engine table (`db-functions/rls/agent_tables.ts`).

| Column | Type | Notes |
|--------|------|-------|
| `agent_type_id` | `uuid` PK | |
| `slug` | `text`, unique | Stable handle (`^[a-z][a-z0-9-]{0,47}$`, enforced in `admin/services/agent_types.py`). Fixed after creation; it is what the MCP tools take and what `db_snapshot` and a cross-database promote match on. |
| `display_name` | `text` | The name shown on chips and pickers. Unique ignoring case (service-level). |
| `description` | `text`, nullable | At most 280 characters. |
| `color` | `text`, default `slate` | One of a fixed palette, checked by the table: `slate`, `teal`, `blue`, `violet`, `pink`, `red`, `amber`, `green`. The portal maps each key to a chip style (`.chip-cat-<key>`). |
| `sort_order` | `integer`, default `0` | Position in pickers, the filter and the Categories screen (ascending, then by name). |
| `created_at` / `created_by` / `updated_at` / `updated_by` | | Audit columns, as on the other config tables. |

Referenced by `agent_profiles.agent_type_id` (`ON DELETE SET NULL`): deleting a
category leaves its agents uncategorised, never deletes them.

**What it is not.** A category is not an `agent_kind`. `agent_kind` (the
`ai_agent_kind` enum) names the *code* that runs a profile and is not
user-editable; a category is a row a user makes, selects no code and changes nothing
about how an agent runs. A generic agent is resolved by `(custom, slug, environment)`
whatever its category.

**Seeding.** None. A fresh database has no rows here and no seeder adds any.

**Promotion and snapshots.** Not frozen: `agent_type_id` is absent from
`runtime.compiler._SHALLOW_FIELDS`, so it is in no `agent_profile_versions.snapshot`
and `restore_version` never touches it. See
[Configure → Categories](./02-configure.md#categories--adminagent-types).

**`db_snapshot`.** `python -m admin.db_snapshot export` includes this table (before
`agent_profiles`), and `restore` matches categories by `slug` and remaps each
profile's `agent_type_id` to the target's id, so a category re-created after a
reset is reused rather than duplicated and no profile is lost to the foreign key.

### `agent_prompts`
Logical prompt slots per agent kind. Key columns: `(agent_kind, slug)` unique,
`description`. Prompts are agent-kind-scoped, shared across all profiles of that kind.

### `agent_prompt_versions`
Immutable versioned bodies. Key columns: `prompt_id`, `version_number` (monotonic),
`content`, `variables`, `notes`, `is_active`, `created_by`.

### `agent_models`
LLM registrations. Key columns: `(name, environment)` unique, `provider`,
`model_string`, `temperature`, `max_output_tokens`, `extra_config` (top_p/top_k/
penalties etc.), `cost_per_1k_in` / `_out`, `is_active`.

**`extra_config` pricing keys** (no schema change — stored in the existing JSONB column):

| Key | Type | Description |
|-----|------|-------------|
| `pricing_mode` | `"auto"` \| `"manual"` | `"auto"` resolves cost via the `tokencost` library (default when absent). `"manual"` uses `cost_per_1k_in`/`_out`. |
| `tokencost_model` | string \| null | Override the `tokencost` lookup key when it differs from `model_string` (e.g. an Azure OpenAI *deployment* name, which is not an OpenAI model id). Leave blank to match on `model_string`. |

See [Architecture → Pricing layer](./00-architecture.md#pricing-layer-corepricingpy) and [Configure → Pricing control](./02-configure.md#pricing-control).

**`azure_voice_live` row shape (`extra_config`).** A realtime row with the
provider slug `azure_voice_live` can be named by `voice_config.model_id` — see
[Architecture → Duplex voice edge](./00-architecture.md#duplex-voice-edge). A build
with no plugin seeds none; add one on the Models screen. Its `model_string` names a
Voice Live **fully-managed** model, either cascaded (Azure STT → chat model → Azure
TTS, e.g. `gpt-4.1-mini`) or native audio (the model generates the speech, e.g.
`gpt-realtime`). There is no separate Azure OpenAI deployment behind the row: the
resource a row talks to is its own `extra_config.endpoint`, so two rows in different
regions point at two resources.

| Key | Type | Description |
|-----|------|-------------|
| `endpoint` | string | The Voice Live resource's HTTPS base URL (e.g. `https://<your-resource>.services.ai.azure.com`). The provider rewrites `https://` → `wss://` and appends `/voice-live/realtime`. Required, and must start with `https://` — anything else raises `RealtimeProviderError` **by name** at connect time rather than failing as an opaque connect error; there is no shared-endpoint fallback like the `azure_openai` chat provider has. |
| `api_version` | string | Voice Live API version query param. Falls back to the provider's own default (`"2026-04-10"`) when unset. URL-escaped before use, since it is operator-edited text. |
| `auth_env_var` | string | Name of the env var holding the `api-key` header value — the name, not the key (`extra_config` renders in plain text on the Models screen). For example `AZURE_VOICE_LIVE_API_KEY` on a cascaded row and `AZURE_VOICE_LIVE_NATIVE_API_KEY` on a native one (two resources = two keys); falls back to `AZURE_VOICE_LIVE_API_KEY` when unset. A missing/unset env var raises `RealtimeProviderError` at connect time. |
| `voice_type` | `"azure-standard"` \| `"native"` | Selects the **shape** of the session's `voice` field: an object (`{name, type: "azure-standard"}`) for an Azure TTS voice, a bare string (`"alloy"`, `"marin"`, …) for a native model's own voice. A property of the model row, not the tenant — pointing `model_id` at the native row while keeping an Azure voice object is rejected at `session.update`. Defaults to `"azure-standard"`. |
| `transcription_model` | string | Which recognizer hears the caller. `azure-speech` (the default, and the only option for a cascaded chat model) takes full BCP-47 (`"ar-SA"`) and is the only one that accepts `phrase_list`; the OpenAI recognizers (`gpt-4o-transcribe`, `whisper-1`, used on the native row) take the primary subtag only (`"ar"`) and **silently ignore** anything else, falling back to auto-detect — which on a live Saudi call transcribed Korean and Latin word-salad and fed it to the agent as the caller's question. The provider formats `language` per recognizer and withholds `phrase_list` from the non-Azure ones. |
| `turn_detection` | object | Operator override for the session's turn-detection block. Merged (shallow, top-level keys only) OVER the provider's `azure_semantic_vad` defaults — not a replacement — and a **`null` value REMOVES a default key**, so a row can express "unset", not only "add or change". Without that, a row trying to switch the barge-in guards off silently kept them (the merge preserved the baked-in defaults) and the caller still could not interrupt. The seeded cascaded row uses this to add the `*_assistant_speaking` guards, which belong on that row **only** — on the native row the same guards stop a human interrupting at all. Useful further keys for noisy rooms (all documented Voice Live fields pass straight through): `speech_duration_ms` (min speech before detection starts), `end_of_utterance_detection` (semantic EOU — English/multilingual models only, **no Arabic**), `appended_text_after_truncation` (tells the model it was cut off). Optional. |
| `interim_response` | object | **Opt-in** server-side tool-call filler (Azure Voice Live `interim_response`, api-version ≥ `2026-01-01-preview`; in the GA `2026-04-10` default). When set, the SERVICE fills tool-call/latency silence itself with bounded static text and the endpoint's own out-of-band holding line is skipped entirely (`handles_interim_response`), removing the whole class of holding-line/answer races. The operator states the mechanism (e.g. `{"triggers": ["tool"]}`); `type` defaults to `static_interim_response`, `texts` default in from the session's resolved `progress_spoken(_by_language)` variants and `latency_threshold_ms` from `progress_after_sec` ×1000, so per-language wording is configured once. Dropped on a native-voice row (documented: realtime audio models don't support it). **Not a default on purpose**: probe once per deployment with `manual_tests/probe_azure_voice_live.py` before setting it — an api-version too old for the key rejects the whole `session.update`. |
| `voice_params` | object | Extra keys merged into the `azure-standard` voice object — the HD-voice expressiveness knobs (`temperature` 0–1 HD-only, `style`, `pitch`, `volume`, all SSML-prosody-shaped values). Ignored (dropped) on a native-voice row, whose voice is a bare string. `null` values are skipped; an explicit key here wins over the derived `rate` from `voice_config.speaking_rate`. Optional. |
| `pricing_mode` | `"manual"` | Pinned to `manual` on both rows rather than the usual `"auto"` default: `tokencost` does carry a bare `gpt-4.1-mini` key, but it's OpenAI's text-chat rate — 16–27× below Voice Live's audio-token billing — so `auto` would silently under-price every session. Paired with `cost_per_1k_in`/`cost_per_1k_out` set to audio-token retail rates as the manual fallback (cascaded ~$0.02/min, native Pro tier ~$0.09/min). |

#### `context_window_compression` on a `google_genai` realtime row

**On by default** (`{"trigger_tokens": 25600, "target_tokens": 12800}` — a
sliding window over the live context), and load-bearing for any call longer
than a few minutes. Without it a session accumulates every audio token until
the model's ceiling, and the failure arrives as two seemingly separate bugs:
tool-calling discipline collapses as the context bloats (live 2026-08-27:
eleven consecutive announce-then-stall recoveries — every answer arrived via
the tripwire's direct delegation, none via the model's own `ask_agent`), then
the service kills the connection outright (`receive failed`). The persona and
tool declarations survive compression by API contract. Override either key
with `extra_config.context_window_compression`; an explicit `null` disables
compression and hands session limits back to the provider — the same opt-out
contract as `turn_detection` below. Only meaningful on `google_genai` rows.

#### `turn_detection` on a `google_genai` realtime row

The **same key**, deliberately — an operator should learn one name — but a
different service behind it, so only the fields Gemini Live actually has are
forwarded to `realtime_input_config.automatic_activity_detection`:

| Key | Type | Description |
|-----|------|-------------|
| `silence_duration_ms` | int | Silence before the caller's turn is committed. **Defaults to 600**, measured: ~280ms per turn faster than leaving it unset, and the knee — the fastest value that still held a 500ms mid-sentence think-pause. 700/800 are strictly dominated (150–200ms slower for the same 800ms-pause split as 600); only 900+ buys an 800ms pause back. Numbers and method: [Architecture → duplex voice edge](./00-architecture.md#duplex-voice-edge) and `manual_tests/probe_gemini_turn_detection.py`. |
| `prefix_padding_ms` | int | Speech required before start-of-speech commits. Lower = more sensitive onset, more false positives. |
| `start_of_speech_sensitivity` | `"high"` \| `"low"` | Full enum names (`START_SENSITIVITY_HIGH`) are accepted too. |
| `end_of_speech_sensitivity` | `"high"` \| `"low"` | As above. |
| `disabled` | bool | Turn automatic activity detection off entirely; the client must then send activity signals (nothing in this repo does — this is an escape hatch, not a supported mode). |
| `activity_handling` | `"start_of_activity_interrupts"` \| `"no_interruption"` | Lives on `RealtimeInputConfig`, accepted as a sibling here so the operator sees one block. |
| `turn_coverage` | `"turn_includes_only_activity"` \| `"turn_includes_all_input"` | As above. |

A `null` value REMOVES a default, the same rule as the Azure block — so
`{"silence_duration_ms": null}` hands end-of-speech detection back to Google
without a deploy. Keys this provider does not have (an Azure block copied onto
a Gemini row: `type`, `threshold`, `speech_duration_ms`, `interrupt_response`,
`auto_truncate`, `remove_filler_words`, the `*_assistant_speaking` guards) are
**dropped with an INFO log naming them**, never forwarded — the SDK raises on
an unknown field and that would fail the whole call over one inapplicable
setting. A value that cannot be used (a non-numeric duration, an unrecognised
sensitivity word) is likewise dropped with a WARNING rather than raised.

Both rows also carry the same non-Azure-specific keys every `agent_models`
row does: `realtime: true` (marks it a realtime/ears-and-mouth model, hidden
from both chat-model pickers — see the `model_id` row of [`voice_config`](#voice_config-keys-duplex-voice)
above) and `location` (the seeder's generic deployment/region field; it
records which Foundry region the row belongs to but is not itself read by the
provider — `endpoint` is what routes the call).

### `agent_tools`
Tool catalog (synced from code at boot + portal-declared HTTP tools). Key columns:
`python_name` unique, `display_name`, `description`, `category`, `scope`
(`read`/`write`), `argument_schema`, `source_file`, `is_deprecated`, `agent_kind` +
`additional_agent_kinds[]`, `usage_guidance` (free text — field semantics, metric
definitions, gotchas; runtime-consumed: rendered as an indented line under the tool
in the `{available_tools}` prompt catalog when the tool is bound for a request),
`context_fields` (per-tool arg→context mapping), and for HTTP tools `kind`, `endpoint_url`, `http_method`,
`auth_env_var`, `timeout_seconds`.

**`context_fields` JSONB.** Map of tool-argument name → request-context reference
(e.g., `{"auth_token": "$ctx.user_token"}`). Read by the MCP resolver as a `context_fields`
source (tool-level overrides bundle-member-level overrides server-level). MCP resolution is
the sole tool-resolution path, so this is read unconditionally at request time.

### `agent_data_sources`
Knowledge sources. Key columns: `name` unique, `kind` (`pgvector` / `supabase_table` /
`http` / `gitbook` / `ms_drive`), `config` JSONB, `refresh_cadence`,
`freshness_sla_hours`, `last_refreshed_at`, `is_active`, `agent_kind` +
`additional_agent_kinds[]`. A build with no plugin seeds no
knowledge sources; a deployment's plugin may seed its own at boot
(`data_source_specs()`, `admin/seed_data_sources.py`). Every other source is created
from the dashboard.

For dashboard-created pgvector KBs, `config` defaults to
`{"table": "ai_kb_embeddings", "pages_table": "ai_kb_pages"}` — the shared
source-scoped tables described below. Deleting an `agent_data_sources` row cascades to
both `ai_kb_pages` and `ai_kb_embeddings` (FK `ON DELETE CASCADE`).

`config.embedded_with` (`provider:model`, for example
`openai_compatible:nomic-embed-text`) records which embedding model wrote a shared
source's vectors. Ingestion sets it on the first upload and `scripts/reembed.py` after
a `kb` run; the Knowledge screens compare it with the model in force. No column on
`ai_kb_embeddings` records the model, so this key is the only such record, and a source
without it is treated as unknown, not as mismatched.

---

## Knowledge Base

### `ai_kb_pages`

Shared parent-page table for dashboard-created pgvector knowledge bases. Lives in the
**AI/config DB** alongside `agent_data_sources`. No per-KB migration required — all
sources that use the shared model write here, scoped by `source_id`.

| Column | Type | Purpose |
|--------|------|---------|
| `id` | uuid PK | Auto-generated via `gen_random_uuid()`. |
| `source_id` | uuid NOT NULL → `agent_data_sources(source_id)` ON DELETE CASCADE | Parent KB; cascade ensures cleanup when a KB is deleted. |
| `title` | text NOT NULL | Page / document title. |
| `type` | text NOT NULL default `'document'` | Page type tag (e.g. `document`, `faq`). |
| `language` | text NOT NULL default `'en'` | BCP-47 language tag; used by `match_kb_embeddings` to filter by language. |
| `content` | text NULL | Raw source text (stored for re-ingestion; may be large). |
| `source` | text NULL | Optional provenance label (e.g. original filename). |
| `checksum` | text NULL | Content hash (title + excerpt/body) written by the batch importers *after* their embeddings are successfully inserted, so a failed run leaves the checksum stale and the page is re-processed on the next pass instead of being skipped as "unchanged". NULL for admin-portal uploads, which always re-embed. |
| `path` | text NULL | Canonical per-corpus key (e.g. `docs/getting-started`, `blog/my-post`) the batch importers write to make re-runs idempotent on `(source_id, path)`. NULL for admin-portal uploads. |
| `metadata` | jsonb NOT NULL default `'{}'` | Importer provenance (e.g. a source CMS's record id, `section`, `sort_order`). Empty for admin-portal uploads. |
| `created_at` | timestamptz NOT NULL default now() | |
| `created_by` | uuid NULL | Operator who uploaded the document. |

**Indexes:**
- `ai_kb_pages_source_id_idx` — btree on `(source_id)`.
- `ai_kb_pages_source_path_key` — **partial** unique index on `(source_id, path) WHERE
  path IS NOT NULL` (added by `20260705140000_extend_ai_kb_for_docs.sql`), so the
  batch importers can upsert idempotently while admin-portal uploads (which always
  leave `path` NULL) are unaffected. PostgREST's `on_conflict` can't target a partial
  index, so the importers upsert manually (select-then-update-or-insert) rather than
  relying on `ON CONFLICT`.

### `ai_kb_embeddings`

Shared chunk-level embedding table for dashboard-created pgvector KBs. Lives in the
**AI/config DB**. Each row is a ≈512-token chunk derived from an `ai_kb_pages` row.

| Column | Type | Purpose |
|--------|------|---------|
| `id` | uuid PK | Auto-generated via `gen_random_uuid()`. |
| `source_id` | uuid NOT NULL → `agent_data_sources(source_id)` ON DELETE CASCADE | KB-level scoping; enables source-filtered retrieval without a join to `ai_kb_pages`. |
| `page_id` | uuid NOT NULL → `ai_kb_pages(id)` ON DELETE CASCADE | Parent page; cascade removes chunks when a page is deleted. |
| `content` | text NOT NULL | Chunk text (≈512 tokens, 64-token overlap with neighbours). |
| `heading` | text NULL | Section heading at the point of chunking, if detectable. |
| `slug` | text NULL | URL-safe identifier for the chunk (optional, populated by importer). |
| `token_count` | integer NULL | Approximate token count of `content`. |
| `embedding` | `vector(1536)` | 1536-dim embedding from Gemini `gemini-embedding-001` (`output_dimensionality=1536`); used for cosine ANN retrieval. |
| `created_at` | timestamptz NOT NULL default now() | |

**Indexes:**
- `ai_kb_embeddings_source_id_idx` — btree on `(source_id)` (hot filter for source-scoped retrieval).
- `ai_kb_embeddings_embedding_idx` — HNSW on `embedding` (`vector_cosine_ops`) for approximate nearest-neighbour retrieval.

**RLS** — service-role-only, consistent with all AI-project tables (`db-functions/rls/agent_tables.ts`).

### `match_kb_embeddings` RPC

`supabase/db-functions/rpc/match_kb_embeddings.ts` — idempotent `CREATE OR REPLACE FUNCTION`.

**Signature:**
```sql
public.match_kb_embeddings(
  p_source_ids      uuid[],
  query_embedding   vector(1536),
  match_threshold   float   DEFAULT 0.5,
  match_count       int     DEFAULT 5,
  filter_language   text    DEFAULT NULL,
  filter_type       text    DEFAULT NULL
) RETURNS TABLE (
  id uuid, page_id uuid, source_id uuid, content text, heading text,
  page_title text, page_type text, similarity float
)
```

Joins `ai_kb_embeddings` to `ai_kb_pages` on `page_id`, filters to
`source_id = ANY(p_source_ids)` (multi-source retrieval for agents with several KBs
attached), applies the cosine threshold, and optionally filters by `p.language`
(`filter_language`) and `p.type` (`filter_type` — parity with the retired main-DB
`search_documentation` RPC's `doc_type` filter, e.g. `user_guide`). Orders by
ascending vector distance (closest first), limits to `match_count`. The `similarity`
column is `1 - (embedding <=> query_embedding)`.

---

### `agent_mcp_servers`

Registry of MCP servers: either portal-composed bundles or external MCP endpoints (each
attached to a profile via `agent_profile_mcp_servers`; editable from
[MCP & Tools](./02-configure.md#mcp--tools)). Key columns: `name` unique,
`source` (`bundle` | `external` — discriminator; Phase 0 default `external`), `is_builtin`
(seeder-owned bundle; not deletable in the portal, membership managed by the seeder),
`transport` (nullable; `stdio`/`sse`/`http` for external only), `endpoint` (nullable; external
only), `auth_env_var`, `tool_subset[]` (**legacy** server-level filter — see note below),
`context_fields` (server-level arg→context map; injected server-side, stripped from model
schema), `usage_guidance` (runtime-consumed for **external** servers only — broadcast to
every tool discovered from that endpoint and rendered in the `{available_tools}` catalog;
for a **bundle**, guidance instead comes from each member's own `agent_tools.usage_guidance`
— the bundle-level field here is not read at request time), `is_active`.

**`source` discriminator:**
- **`external`** — remote MCP server (discovery via `tools/list`). Requires `endpoint` + `transport`. Tools exposed as-is with optional arg injection (context_fields).
- **`bundle`** — portal-composed tool collection (rows in `agent_mcp_tools`). No `endpoint` or `transport`. Tools curated per-server with optional per-member context overrides.

**`tool_subset[]` (legacy).** Pre-existing baseline column (not touched by the Phase 0
migration). It is the **old, server-level** tool filter. The MCP resolver instead reads the
**per-attachment** `agent_profile_mcp_servers.tool_subset` — don't conflate the two same-named
columns: the new per-profile column is authoritative for what the resolver exposes.

**`context_fields` JSONB.** Server-level map of tool-argument name → request-context
reference (e.g., `{"user_token": "$ctx.user_token"}`). Args named here are stripped from
LLM-facing schemas and injected server-side at call time. Read by the MCP resolver as a
`context_fields` source (tool-level overrides bundle-member-level overrides server-level).
MCP resolution is the sole tool-resolution path, so this is read unconditionally at request
time. Portal management screens shipped.

### `agent_mcp_tools`

Bundle membership: which `agent_tools` rows a source=`bundle` MCP server exposes. Many-to-many
linking; external servers do not use this table. The `context_fields` column here overrides
the server-level map from `agent_mcp_servers.context_fields` for that specific member (most-specific wins).

| Column | Type | Purpose |
|--------|------|---------|
| `mcp_server_id` | uuid NOT NULL → `agent_mcp_servers(mcp_server_id)` ON DELETE CASCADE | Parent bundle server. |
| `tool_id` | uuid NOT NULL → `agent_tools(tool_id)` ON DELETE CASCADE | Exposed tool; deletion cascades this row. |
| `position` | integer NOT NULL default 0 | Sort order within the bundle (for portal UI). |
| `context_fields` | jsonb NULL | Per-member arg→context override (e.g., `{"token": "$ctx.user_token"}`); null = use server-level map. |
| `created_at` | timestamptz NOT NULL default now() | Creation timestamp. |

**Primary key:** `(mcp_server_id, tool_id)`.

**Runtime visibility:** Read unconditionally by the runtime — MCP resolution is the sole tool-resolution path (the `MCP_TOOL_RESOLUTION` flag is retired). Portal CRUD screens shipped.

### `agent_profile_mcp_servers`

Profile↔MCP attachment: which MCP servers a profile has access to, with optional per-profile
tool filtering. Replaces the legacy `agent_profiles.mcp_server_ids[]` array (to be dropped
at end of Phase 1). The `tool_subset` column lets you pin exactly which tools from an MCP
are exposed to the profile's LLM (null = all current and future tools).

| Column | Type | Purpose |
|--------|------|---------|
| `profile_id` | uuid NOT NULL → `agent_profiles(profile_id)` ON DELETE CASCADE | Parent profile. |
| `mcp_server_id` | uuid NOT NULL → `agent_mcp_servers(mcp_server_id)` ON DELETE CASCADE | Attached MCP (bundle or external). |
| `tool_subset` | text[] NULL | Pinned tool names (python_name for code tools, MCP tool names for external servers); null = expose all. |
| `position` | integer NOT NULL default 0 | Sort order for portal UI (e.g., in Configure → MCP & Tools screen). |
| `created_at` | timestamptz NOT NULL default now() | Creation timestamp. |

**Primary key:** `(profile_id, mcp_server_id)`.

**Runtime visibility:** Read unconditionally by the runtime — MCP resolution is the sole tool-resolution path (the `MCP_TOOL_RESOLUTION` flag is retired). Portal CRUD screens shipped.

### `agent_skills`

Skill identity — a named, versioned instruction bundle (playbook) an agent loads
on demand (progressive disclosure: catalog line in-prompt, body via the synthetic
`read_skill` tool). Key columns: `slug` (unique per owner scope), `display_name`,
`description` (the in-prompt catalog line — `{`/`}` banned), `agent_kind`,
`business_id` (**NULL = platform skill**, attachable to profiles; **set = tenant
skill**, resolved dynamically per request and never pinned to a profile),
`always_on` (body inlined into the system prompt every turn), `status`
(`active`/`archived`), `is_active`, `linked_data_source_ids[]` (KB pointers).

### `agent_skill_versions`

Immutable versioned skill bodies, mirroring `agent_prompt_versions`. Key columns:
`skill_id`, `version_number` (monotonic per skill), `content` (markdown, ≤32 KB),
`notes`. The runtime always serves the **latest** version; platform skills freeze
the latest version into the promote snapshot.

### `agent_skill_resources`

Named reference files bundled with one skill version (long reference docs,
templates, examples — read via `read_skill(slug, file)`). Key columns:
`skill_version_id`, `filename` (unique per version), `content_type`, `content`
(≤64 KB), `position`.

### `agent_profile_skills`

Profile ↔ **platform**-skill attachment join (tenant skills never appear here).
Key columns: `profile_id`, `skill_id`, `position` (catalog order). Primary key
`(profile_id, skill_id)`. Live-joined in staging; frozen into
`agent_profile_versions.snapshot.skill_attachments` (snapshot schema v5) on
promote, so the production runtime needs no live join.

### `agent_channels`
Request sources. Key columns: `slug` unique, `name`, `kind`, `is_active`,
`allowed_origins[]` (CORS), `allowed_agent_kinds[]`, `default_profile_id`,
`business_id` (tenant), `auth_required`, `rate_limit_rpm`, `config` JSONB.

### `agent_profiles.memory_config` JSONB

Memory layer configuration stored on the profile row alongside `voice_config`.
Empty `{}` means disabled (defaults applied in code via `resolve_memory_config`).
Promoted into `agent_profile_versions` snapshots so production reads a consistent
copy without re-joining the live row.

| Key | Type | Description |
|-----|------|-------------|
| `window.max_messages` | int | Short-term conversation window: how many prior messages an agent replays as verbatim history (default 10). Independent of `enabled`. Supersedes the `AGENT_HISTORY_MSG_COUNT` env default for agents that read it. |
| `window.max_message_chars` | int | Per-message truncation cap when replaying the window (default 4000). |
| `window.suggestion_context_messages` | int | Recent messages fed to the follow-up-suggestion sub-agent so suggestions cite the real conversation instead of placeholders (default 6). |
| `enabled` | bool | Master kill-switch for **long-term** memory (extraction/retrieval/summary below). Defaults `false`. Does not affect `window`. |
| `extraction.model_slot` | string | Which profile model slot runs fact extraction: `light` (default) or `main`. |
| `extraction.importance_threshold` | float | Drop extracted facts below this score (0–1, default 0.5). |
| `extraction.max_per_turn` | int | Maximum facts stored from a single turn (default 5). |
| `dedup.similarity_threshold` | float | Cosine similarity above which a new fact is merged into the nearest existing one (default 0.9). |
| `retrieval.top_k` | int | Number of memories retrieved per turn via `match_user_memory` (default 6). |
| `retrieval.min_similarity` | float | Minimum cosine similarity for a memory to qualify (default 0.7). |
| `retrieval.always_include_pinned` | bool | Inject pinned memories regardless of similarity (default true). |
| `summary.trigger_every_n_turns` | int | Turns between rolling-summary refreshes (default 6). |
| `summary.keep_recent_verbatim` | int | Turns kept in the prompt verbatim rather than folded into the summary (default 4). |
| `summary.model_slot` | string | Model slot for summarization: `light` (default) or `main`. |
| `retention_days` | int \| null | Days until memories are eligible for cleanup (null = keep forever; Phase 2). |

---

## Memory

### `ai_user_memory`

One row per durable fact for a user. Auto-extracted after each turn of an agent
that runs long-term memory (`memory_config.enabled`), embedded via Gemini `text-embedding-004`
(768d), and retrieved by cosine similarity. No RLS — service-role only, consistent
with `ai_chat_*`.

| Column | Type | Purpose |
|--------|------|---------|
| `id` | uuid PK | Auto-generated via `gen_random_uuid()`. |
| `user_id` | uuid NOT NULL | The subject (per-user scoping). Usually the caller's auth user id; an agent with no logged-in caller may derive a stable id instead. |
| `business_id` | uuid NULL | Tenant scoping where the build's plugin has tenants; NULL for a generic agent's memories (nullable since `20261123150000`). |
| `agent_kind` | `ai_agent_kind` NOT NULL | Namespaces memory per agent kind. |
| `agent_slug` | text NULL | The generic agent (`custom` kind, `agent_profiles.slug`) the memory belongs to; read and written by kind AND slug, so generic agents never share memories. NULL for product kinds. A `custom` row with NULL here predates per-agent memory and is recalled by no agent until assigned (Memory › Memories). Added by `20261123150000_agent_scoped_memory.sql`. |
| `content` | text NOT NULL | The fact text, normalized. |
| `embedding` | `vector(768)` | Gemini `text-embedding-004` 768-dim embedding; used by HNSW index for ANN retrieval. |
| `importance` | real NOT NULL default 0.5 | Extraction score 0–1; used for threshold filtering and ranking. |
| `pinned` | boolean NOT NULL default false | If true, always injected into the prompt regardless of similarity. |
| `source` | text NOT NULL default `'auto'` | `auto` (extracted) \| `manual` (admin-inserted) \| `tool` (future). |
| `status` | text NOT NULL default `'active'` | `active` (live) \| `archived` (soft-deleted). |
| `origin_session_id` | uuid NULL | Provenance — session the fact was extracted from. |
| `origin_message_id` | uuid NULL | Provenance — specific message the fact was extracted from. |
| `use_count` | int NOT NULL default 0 | Times this memory was retrieved into a prompt. |
| `last_used_at` | timestamptz NULL | Last retrieval timestamp; used for recency/decay in Phase 2. |
| `metadata` | jsonb NOT NULL default `'{}'` | Reserved for future enrichment. |
| `created_at` | timestamptz NOT NULL default now() | |
| `updated_at` | timestamptz NOT NULL default now() | |

**Constraints / indexes:**
- `CHECK (source IN ('auto', 'manual', 'tool'))`, `CHECK (status IN ('active', 'archived'))`.
- `idx_ai_user_memory_lookup` — btree on `(user_id, agent_kind, status)` (hot read filter).
- `idx_ai_user_memory_business` — btree on `(business_id, agent_kind)` (admin browse).
- `idx_ai_user_memory_agent_slug` — btree on `(user_id, agent_kind, agent_slug, status)` (a generic agent's reads).
- `match_user_memory(p_user_id, p_agent_kind, p_query_embedding, p_match_count, p_min_similarity, p_agent_slug)` — the recall RPC; for kind `custom` it returns only `agent_slug = p_agent_slug` rows.
- HNSW index on `embedding` (`vector_cosine_ops`) for approximate nearest-neighbour retrieval.
- Requires the `vector` extension (`CREATE EXTENSION IF NOT EXISTS vector`).

### `ai_chat_sessions` — memory columns (additive)

Three columns added by migration `20260614120000_add_user_memory.sql`:

| Column | Type | Purpose |
|--------|------|---------|
| `running_summary` | text NULL | Rolling summary of older turns (short-term memory). Refreshed when message count crosses `memory_config.summary.trigger_every_n_turns`. |
| `summary_message_count` | int NOT NULL default 0 | How many messages the current `running_summary` covers. |
| `summary_updated_at` | timestamptz NULL | Last summary refresh timestamp. |

`ADD COLUMN IF NOT EXISTS` — additive, no backfill needed.

### `ai_chat_sessions.metadata.client` — portal history tag

No new table or column. An entry point that serves a portal may stamp the session's
`metadata` JSONB with a `client` tag, so the portal's own history can be told apart
from machine traffic (eval runs, smoke checks) that signs in as the same identity.
The `/agents` demo stamps `{"client": "agents_portal", "case": "<case id>"}`
(`endpoints/agents_portal.py`). Untagged rows never appear in a history list that
filters on the tag.

### `ai_chat_messages.metadata.memory`

No new table. The existing `metadata` JSONB on each **assistant** message is stamped
per-turn with:

```jsonc
{
  "memory": {
    "retrieved_memory_ids": ["<uuid>", "..."],  // ids from ai_user_memory used in this turn
    "summary_used": true,                        // whether running_summary was injected
    "config_enabled": true                       // snapshot of enabled at turn time
  }
}
```

The Inspector tab resolves `retrieved_memory_ids` against `ai_user_memory` to replay
exactly what the agent saw. No schema migration required.

Written by `common.chat_session.run_turn`, not the agent directly: an agent that owns
its own memory subsystem yields this block on
`turn_meta.metadata.memory`, and `run_turn` copies it onto the `metadata` argument of
`ChatSession.append_assistant` when persisting the assistant row. Agents that don't set
`turn_meta.metadata.memory` get `{}`.

### `match_user_memory` RPC

`supabase/db-functions/rpc/match_user_memory.ts` — idempotent `CREATE OR REPLACE FUNCTION`.

**Signature:**
```sql
public.match_user_memory(
  p_user_id        uuid,
  p_agent_kind     ai_agent_kind,
  p_query_embedding text,           -- pgvector text literal "[0.1,0.2,...]"
  p_match_count    integer DEFAULT 6,
  p_min_similarity real    DEFAULT 0.7
) RETURNS TABLE (id uuid, content text, importance real, pinned boolean,
                  source text, similarity real, last_used_at timestamptz)
```

Returns the user's `active` memories with a non-null embedding, ordered by cosine
similarity descending, filtered to rows where
`1 - (embedding <=> query_embedding::vector) >= p_min_similarity`.

Used by both the read-path retrieval (top-K) and the write-path dedup check
(nearest-1 with `p_min_similarity = 0`). The embedding is passed as a text literal
to avoid PostgREST↔pgvector coercion ambiguity.

---

## Application Tables

### `ai_conversation_insights`

One derived row per finished conversation (`session_id` PK → `ai_chat_sessions.id`,
`ON DELETE CASCADE`), written only by `core/conversation_insights.py` and read
by the Overview + the `conversation_insights` MCP tool through
`ai_conversation_insights_rollup(p_hours, p_env, p_kinds)`. Safe to truncate:
the sweep rebuilds the last 30 days. Migration `20261011120000_conversation_insights.sql`.

| Column | Type | Purpose |
|--------|------|---------|
| `agent_kind` | text | Copied from the session. |
| `environment` | text | `prod` / `staging` / NULL (not recorded) — from `ai_chat_sessions.metadata.environment` or the trace's `waj.env`. Rollups: prod strict, staging admits NULL. |
| `channel` | text | `chat` / `voice`. |
| `outcome` | text | `resolved` / `abandoned` / `escalated` / `unknown`. |
| `outcome_source` | text | `telemetry` (hand-off action ran) / `llm` / `rule`. |
| `topic` | text | Key from the agent's label set, `greeting_only`, or `other`; NULL = not classified. |
| `language` | text | ISO 639-1 of the customer's messages, `mixed`, or NULL. |
| `user_turns` / `message_count` | integer | Size of the conversation when analysed. |
| `is_simulated` | boolean | Bot-driven voice call — excluded from every rollup. |
| `started_at` / `last_message_at` | timestamptz | Session created / last activity (`updated_at`) at analysis. A session that continues later is re-analysed. |
| `classifier_model` | text | Model that labelled it; NULL = rules only (retried every 6 h when a key exists). |
| `version` / `analyzed_at` | smallint / timestamptz | Derivation version + when. |

`ai_conversation_insight_candidates(p_idle_minutes, p_lookback_days, p_limit,
p_retry_unclassified)` picks the next batch (idle, ≥1 customer message, never
analysed / continued since / unclassified). **`ai_background_leases`** (`name`
PK, `holder`, `lease_until`, `last_run_at`, `last_result` jsonb) keeps one
sweeper at a time; row `conversation_insights` is seeded by the migration.
Both tables are service-role only.

New sessions also carry **`ai_chat_sessions.metadata.environment`**
(`prod`/`staging`, from the writer's `ENVIRONMENT`) and voice calls
**`metadata.channel = "voice"`**; older rows have neither.

### `ai_voice_usage_daily`

Per-tenant per-day audio-second tally for the duplex voice edge
(`voice/live/budget.py`). Realtime audio bills per audio token in **both**
directions, so a single long-lived WebSocket can spend far more than any
per-request rate limiter can see — this counter is persisted (not
in-process) so the budget holds across replicas and restarts. Written at
session handshake (a flat 30s reservation so an abandoned socket still costs
something) and again at close (the real elapsed duration, minus the
handshake reservation already booked).

| Column | Type | Purpose |
|--------|------|---------|
| `business_id` | uuid, PK (composite) | Tenant. An authenticated portal keys this on the RESOLVED tenant id (a query param, or the id in the portal's signed session cookie), uuid5-deriving a stable id when that value isn't already a valid UUID, so it's never keyed by caller IP. An anonymous caller (no login, e.g. the `/agents` demo) gets a stable synthetic uuid derived from its IP/session key, the only case where an IP-derived key is intentional. |
| `usage_date` | date, PK (composite) | UTC calendar day. |
| `seconds_used` | integer NOT NULL default 0 | Running total for that tenant/day. |
| `created_at` / `updated_at` | timestamptz | Standard audit columns. |

Migration `20260726120000_add_voice_live_usage.sql`. **Fails open**: if the
AI project is unreachable, `reserve_audio_seconds` logs a warning and lets
the call proceed rather than taking voice down for everyone (same posture as
`core/chat_persistence.py`). Reserving past the daily limit raises
`BudgetExhausted`, which the WS endpoint turns into a `CLOSE_RATE_LIMITED`
close at handshake. The limit is the tenant's `ai_voice_budgets` row when
one exists, else `VOICE_LIVE_DAILY_SECONDS` (default 3600s); where the DB
carries `ai_reserve_voice_seconds` (migration `20260802140000`) the
reservation is a row-locked stored function — the recorded tally clamps AT
the limit and stays there, and the Python read-modify-write survives only
as the fallback for pre-migration DBs.

### `ai_voice_budgets`

Per-tenant daily allowance override. `business_id` (uuid PK, same derivation
rules as `ai_voice_usage_daily`), `daily_seconds` (integer ≥ 0), `note`,
audit timestamps. Managed from the
[Voice budgets page](./02-configure.md#duplex-voice-budgets-settings--voice-budgets)
on any realtime model's detail page (the rows are per **tenant**, so the same
table renders on every realtime model and applies regardless of which one a
tenant uses) and the admin MCP `voice_budgets` tool. Migration
`20260802140000_voice_budgets_and_atomic_reserve.sql`.

### `ai_voice_scenarios` / `ai_voice_scenario_runs`

Live-call acceptance: scenario definitions (`slug` unique, `agent_kind`,
`ws_path` — covers a deployment's mount prefix, e.g. `/<prefix>/voice/live` —
`locale`, ordered `steps` JSONB, `enabled`) and their executions (`status`
running|passed|failed|error|skipped, `detail` JSONB with per-step checks and
the trailing frame timeline, `target`). See
[Test → Voice scenarios](./03-test.md#voice-scenarios--live-call-acceptance).
Starter rows seed at boot (`admin/seed_voice_scenarios.py`, insert-only on
slug). Migration `20260802130000_voice_scenarios.sql`.

### `ai_voice_recordings` / `ai_voice_recording_chunks`

Per-call audio for the observability explorer's
[Audio tab](./01-operate.md#conversations). One `ai_voice_recordings` row
per `/voice/live` websocket connection (`call_id` unique — also stamped on the
call's spans as `waj.voice.call_id`; `session_id` is the **coerced** chat
session UUID, joining `agent_traces.session_id` directly), with capture
metadata: `provider`, `simulated` (bot-driven call — text-only sim provider
or a live-mode eval/scenario run self-declared via `?simRun=1`; a text-only
sim run stores a metadata-only `empty` row, deliberately without synthesized
speech), `sample_rate`, `channels` (2 —
left caller / right assistant), `duration_ms`, `size_bytes`, `chunk_count`,
`status` uploading|complete|empty|failed (inserted `uploading`, flipped to
`complete` after the last chunk lands so a concurrent read never serves a
torn file), `note` (anomaly marker, e.g. mid-call capture failure),
`started_at`. The WAV bytes live in
`ai_voice_recording_chunks` as ordered base64 slices (`recording_id` + `seq`;
concatenating every decoded chunk is the playable file). Written only by the
voice edge at call teardown (`voice/live/recorder.py`), capped per call and
pruned past `VOICE_RECORDING_RETENTION_DAYS` — a rolling window, not an
archive. Migration `20260808120000_voice_call_recordings.sql`.

---

## Versioning & promotion

### `agent_profile_versions`
Immutable snapshots. Key columns: `profile_id`,
`environment`, `version_number`, `snapshot` JSONB (resolved models + prompt contents +
tool names + source rows — self-contained), `promoted_by`, `promoted_at`, `notes`.
Snapshot schema lineage: v4 added `mcp_attachments`; v5 added `skill_attachments`
(the profile's platform skills, denormalized with latest version bodies + resources);
**v6 adds `downstream_delegates`** (the profile's delegatable downstream rows,
denormalized — profile id, slug, display name, description, agent kind) plus the
shallow `delegatable` flag, so production can build `ask_<slug>` delegation tools
without a live join. Legacy snapshots read as empty lists — backward-compatible.
The profile's **category** (`agent_type_id`) is deliberately **not** in the
snapshot: it is organisational, the runtime never reads it, and a rename or a move
between categories must not wait for a re-promote.

**Two writers now, not just promotion.** Written on **Staging → Production**
promotion into the production branch (unchanged) — the production runtime reads
this snapshot, never the live `agent_profiles` row; staging serves the live row
directly. It is now also written **on demand, in either environment, without
promoting**, by `admin.services.profile_versions.save_version` (the agents screen
version bar's **Save as new version** button / MCP `profile_versions action=save`),
which checkpoints the profile's *current* draft config. Both writers call the same
`runtime.compiler.compile_profile_snapshot`, so a manually-saved version has the
identical self-contained shape as a promoted one. `restore_version` (**Restore** in
the version bar / MCP `profile_versions action=restore`) copies a chosen version's
snapshot back onto the live `agent_profiles` row (config fields only, identity
untouched). Missing references are handled per-kind: a stale **tool / KB-source /
MCP-server** is dropped and reported; a stale **model** is left as the live row's
current model and reported; a stale **prompt version** is preserved by
re-creation — `restore_version` inserts a **new `agent_prompt_versions` row** from
the snapshot's stored prompt text and repoints the draft at it (so it does write to
`agent_prompt_versions` as a side effect, though it never inserts a new
`agent_profile_versions` row itself). See
[Configure → Agent Profiles](./02-configure.md#agent-profiles) and
[Architecture → Served-version
pin](./00-architecture.md#served-version-pin-agent_profilesserved_version_id).

---

## Evals & testing

### `agent_eval_suites`
Key columns: `name`, `agent_kind`, `scenarios` JSONB, `pass_threshold`, `is_active`,
`evaluator_model_id` (judge LLM), `default_context` JSONB, **`profile_id`** (uuid, FK
→ `agent_profiles(profile_id)` `ON DELETE SET NULL`, nullable — the profile this suite
grades; `NULL` means an "unassigned"/legacy suite, still manually keyed on
`agent_kind`), **`runner_kind`** (text, `NOT NULL DEFAULT 'chat'` — the engine's `chat` | `voice_live`, plus any
runner kind a plugin adds; selects how a scenario is invoked/scored, see
[Test → Eval runner internals](./03-test.md#eval-runner-internals)).
Indexed on `profile_id`. `agent_kind` is a **derived mirror** of `profile_id` when one
is bound — every write path (`eval_handlers._sync_agent_kind`, the MCP `eval_suites`
tool) re-derives `agent_kind` from the profile rather than trusting a client-supplied
value, so the two columns can't drift for a bound suite.

### `agent_eval_runs`
Key columns: `suite_id`, `profile_id`, `status` (queued/running/passed/failed/errored),
`score`, `results` JSONB (per-scenario), `context` JSONB (overrides incl. prompt
override for A/B), `lmnr_trace_id`, `started_at` / `finished_at`, **`schedule_id`**
(uuid, FK → `agent_eval_schedules(schedule_id)` `ON DELETE SET NULL`, indexed
`WHERE schedule_id IS NOT NULL` — groups a schedule's fired runs for
[Production monitoring](./03-test.md#production-monitoring) without scanning
`context`), **`bakeoff_id`** (uuid, FK → `agent_bakeoffs(bakeoff_id)` `ON DELETE
SET NULL`, indexed `WHERE bakeoff_id IS NOT NULL` — groups a
[bake-off](./03-test.md#bake-offs)'s spawned runs for its detail matrix), and
**`profile_version_id`** (uuid, FK → `agent_profile_versions(version_id)` `ON DELETE
SET NULL` — the immutable profile snapshot this run executed against, when pinned;
`NULL` means the live/active profile, today's default behaviour — see
[Architecture → Profile-version pinning](./00-architecture.md#request-scoped-profile-version-pin-x-waj-profile-version)).
The variant descriptor (model label, gen-config, version label) still also lands in
`context` for back-compat with the comparison matrix's `variant_label` grouping. For
**live-chats (real-data) runs** `context` also carries `source="live_chats"`, the
sampled `agent_kind`, and the operator's `judge_model` + `expectations` (both
required).

**`context` JSONB — variant-override keys** (no schema change — stored in the existing column):

| Key | Description |
|-----|-------------|
| `model_override` | Object with `provider`, `model_string`, `temperature`, `extra_config`, and optionally `max_output_tokens` — the model substitution applied for this run via `X-WAJ-Model-Override`. Built by `_build_model_override()`; does **not** carry `model_id`. |
| `prompt_overrides_json` | Prompt-slot pin map (same shape as `agent_profiles.prompt_version_ids`) when specific prompt versions were pinned at trigger time. |
| `variant_label` | Free-text label set by the operator at trigger time (e.g. `"gemini-flash-v3"`); surfaced in trend-chart legends and comparison column headers. |
| `model_label` | Resolved display name of the overriding model (populated by the runner); used by the comparison matrix when no `variant_label` was set. |

**`results` JSONB — per-scenario performance keys** (no schema change — added to each scenario result object):

| Key | Type | Description |
|-----|------|-------------|
| `ttft_ms` | int | Time-to-first-token in milliseconds (wall-clock from request start). |
| `total_ms` | int | Total end-to-end latency in milliseconds. |
| `input_tokens` | int | Input token count for this scenario. |
| `output_tokens` | int | Output token count for this scenario. |
| `cost_usd` | float | Scenario cost in USD, computed by `core/pricing.py` from the resolved model rates. |

These keys power the **Performance** and **Cost** rows in the [Compare runs](./03-test.md#eval-run-compare) matrix.

### `agent_eval_schedules`
Recurring eval coverage. Key columns: `suite_id`, `profile_id`, cadence/schedule
fields, enabled flag, last-fired timestamp, and `coverage` JSONB. Drives
[Evals — Production monitoring](./03-test.md#production-monitoring) (schedule CRUD +
fire-now/fire-due), which creates `agent_eval_runs` rows (linked back via the run's
`schedule_id` column). Two shapes coexist in the same table: `source="live_chats"`
rows (the current, prod-chat-monitoring kind — created only from the Production
monitoring tab, always bound to a `profile_id`) whose `coverage` holds
`lookback_days` / `sample_size` and the **required** `judge_model` + `expectations`;
and legacy `source="suite"` rows (created before that tab existed, no longer
creatable from the portal but still manageable there, flagged `_legacy`).

**Real-data sampling depends on `ai_chat_sessions.agent_kind`.** That column (the
`ai_agent_kind` enum) is the discriminator the live-chat sampler filters on,
stamped when the session row is created — `ChatSession.resolve_or_create` (in
`run_turn`) for agents migrated onto the orchestrator, or
`chat_persistence.ensure_session` for those not yet migrated — for every
session. Legacy rows with a NULL `agent_kind`
fall back to matching `metadata.session_type`.

### `agent_bakeoffs`
A persisted N-variant × M-suite comparison of **one profile** — see
[Evals — Bake-offs](./03-test.md#bake-offs). Key columns: `bakeoff_id`, `name`,
`profile_id` (uuid, `NOT NULL` FK → `agent_profiles(profile_id)` `ON DELETE CASCADE`),
`variants` JSONB (`NOT NULL DEFAULT '[]'` — array of
`{label, profile_version_id?, model_id?, gen_config?}`; every `label` is normalized
unique + non-empty before the row is written, since the comparison matrix groups
scenario results by the exact label string), `suite_ids` `uuid[]` (`NOT NULL DEFAULT
'{}'` — the suites of this profile included), `status` (`running` | `complete` |
`errored` — recomputed on every read from the linked runs' statuses and only
persisted back when it settles to `complete`; a stored `errored` is sticky and never
recomputed away), `repeats` (`integer NOT NULL DEFAULT 1`), `notes`, plus the standard
audit columns. Creating a bake-off spawns `len(variants) × len(suite_ids) × repeats`
linked `agent_eval_runs` rows (via the same `start_suite_run` the on-demand trigger
uses), each tagging `bakeoff_id` + the variant's `profile_version_id` / `model_id` /
`gen_config`. Indexed on `profile_id` and `created_at DESC`. RLS is service-role-only
(added to the shared `supabase/db-functions/rls/agent_tables.ts` list), matching every
other `agent_*` table.

### `agent_e2e_runs`
Key columns: `profile_id`, `environment`, `status`, `score`, pass/fail counts,
`results`, `triggered_by`, timestamps. Drives the eval-status chips that gate promotion.

### `agent_test_runs`
pytest invocations. Key columns: `suite`, `status`, `pass_count` / `fail_count` /
`skip_count`, `duration_ms`, `output` (≤50KB), `results` JSONB (per-test), `error`,
`triggered_by`.

### `test_fixtures`
Key columns: `(name, environment)` unique, `kind` (jwt/uuid/string/json/secret),
`value`, `description`, `last_used_at`. Service-role only; never exposed to the runtime.

---

## Operations

### `agent_deployment_targets`
Key columns: `(environment, label)` unique, `base_url`, `auth_env_var`, `is_default`
(one per env), `is_active`.

### `agent_env_vars`
Key columns: `(environment, name)` unique, `value` (nullable), `source`
(`db` / `secret_manager:…` / `supabase_vault:…`), `is_secret`, `description`. Resolved
by `core.env_resolver` (process env wins).

### `api_keys`
Portal-issued API keys (migration `20261123010000_api_keys.sql`). One row per key;
rows are never deleted. Key columns: `id` (also the `user_id` of the chat sessions the
key creates and the audit actor of its MCP calls), `name`, `key_prefix` (the first 12
characters, for display), `key_hash` (SHA-256 hex of the key, **unique**; the key itself
is not stored), `scopes` (`text[]`, a non-empty subset of `chat` / `admin`, enforced by
a check constraint), `created_by`, `created_at`, `last_used_at` (throttled, may lag a
minute), `expires_at` (nullable), `revoked_at` / `revoked_by`. RLS: service role only,
and `anon` / `authenticated` hold no privilege on the table at all. Not
environment-scoped. See [Govern → API keys](./04-govern.md#api-keys).
### `provider_keys`
Model provider keys entered on Settings › Model keys. Key columns:
`(environment, provider, name)` unique; `provider` (`google_genai` / `openai` /
`anthropic` / `azure_openai`); `name` (`''` = the provider's default key, anything
else a named credential a model selects with `extra_config.credential`);
`ciphertext` (a Fernet token made with a key derived from the server's
`SECRET_KEY`; never returned by the portal or the MCP); `hint` (last four
characters); `created_by` / `created_at` / `updated_by` / `updated_at`.
Service-role RLS, and table privileges for `service_role` only. Written by
`admin/services/provider_keys.py`; read by `core/provider_keys.py`, which
prefers a stored key over the environment variable. Added by migration
`20261123020000_provider_keys.sql`.

### `engine_setup`
First-run setup state, one row (`id = 1`, enforced by a check). `code_hash` (SHA-256
digest of the current one-time setup code; the code itself is only printed in the
engine's log), `code_expires_at`, `failed_attempts` (setup locks at 5), `claimed_at`
(the lock one submission takes, with a conditional `UPDATE`, to create the admin),
`completed_at` / `completed_by` (set when the first admin exists; from then on setup
never opens again, and `code_hash` is cleared), `finished_at` (the first admin finished
or left the model-key and API-key steps). Service-role RLS and table privileges for
`service_role` only. Read and written by `admin/services/first_run.py`, only in a build
with no product plugin. Not in `db_snapshot`: it is install state, not configuration.
Added by migration `20261123070000_engine_setup.sql`.

### `agent_audit_log`
Append-only. Key columns: `entity_type`, `entity_id` (**nullable** — global / bulk
actions such as the eval scheduler's `due_fire`, or the retired channels CORS
dirty-flag's old `cors_dirty_clear` rows, have no single entity), `action`, `before` / `after` JSONB, `actor_user_id`, `created_at`,
optional `signed_hash`. Secrets masked before insert; writes are best-effort.

### `admin_jobs`
Background job tracker. Key columns: `kind` (eval_run / kb_upload / deploy_dispatch /
env_promote / tool_dry_run / other), `label`, `status`
(queued/running/succeeded/failed/cancelled), `related_entity_type` / `related_entity_id`,
`triggered_by`, `duration_ms`, `error`, `metadata` JSONB, timestamps. Supports
cooperative cancellation.

### `agent_traces`
Lean session↔trace index. Key columns: `trace_id` (PK), `session_id` (loose UUID ref →
`ai_chat_sessions.id`, no FK across projects), `lmnr_trace_id` (Laminar root-span
id for drill-down), `agent_kind`, `status`, `created_at`. `status` is the turn
outcome written by `common/turn_telemetry.record_turn_telemetry`: `'ok'` for a
completed turn, `'error'` for one that failed mid-stream (the agent yielded an
`error` event, or the model raised). It is **nullable with no default** — rows
written before the column existed are NULL and must be read as `'ok'`, since the
orchestrator recorded no row at all for a failed turn back then. Rich observability data — tokens,
latency, tool calls, errors — lives in Laminar only and is surfaced through
`load_live_insights` and the Laminar explorer. The Dashboard's `sessions_24h` KPI
is counted from this table.

> **Analytics source — Laminar ClickHouse (not a Postgres table).** The
> Dashboard's chart grid and per-agent insight columns are aggregated by read-only
> SQL against Laminar's ClickHouse (`traces_replacing` joined to `spans`), via
> `core/lmnr_clickhouse.py` → `admin/screens/live_analytics.py:load_analytics`,
> served at `GET /admin/live/analytics.json`. `agent_kind`/`channel` come from the
> top span's attributes, error-rate from OTel `exception` events, cost from
> Laminar's precomputed `total_cost`. Config: `LMNR_CLICKHOUSE_URL` /
> `LMNR_CLICKHOUSE_USER` / `LMNR_CLICKHOUSE_PASSWORD` / `LMNR_CLICKHOUSE_DB`.

> **Prompt-cache metrics are not stored anywhere in Postgres — not in
> `agent_traces`, not in a dedicated table.** Cache-read tokens are read at query
> time from the auto-instrumented Laminar span attribute
> `gen_ai.usage.cache_read_input_tokens` on LangChain LLM spans (`span_type = 1`);
> `gen_ai.usage.cache_creation_input_tokens` (Anthropic-only — a cache write)
> appears on the provider span alongside it. Both feed the Dashboard's cache KPIs
> and charts ([Operate → Dashboard](./01-operate.md#overview)) and the Traces
> explorer's per-trace Cache read/write/hit and per-step Cache read
> ([Operate → Traces](./01-operate.md#conversations)) — always via live
> ClickHouse aggregation, never a stored rollup. Anthropic prompt caching is
> **opt-in per model**, off by default: it only fires when the model's
> `provider` is `anthropic` (the direct Anthropic API — the Vertex providers are gone) **and** `agent_models.extra_config.prompt_cache`
> is truthy (`runtime/prompt_cache.py`). Gemini caches implicitly and read-only —
> there is no opt-in flag, and no `cache_creation_input_tokens` is ever produced
> for it.

### `user`
Accounts. Admin-relevant columns: `auth_id`, `email`, `phone_number`, `country_code`,
`role`, `permission_level`, `permission` JSONB (`is_superuser`), `business_id`,
`is_deleted`. The portal's auth gate keys on `permission.is_superuser`.
