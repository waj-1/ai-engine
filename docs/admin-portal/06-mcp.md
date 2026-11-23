# MCP Server

**Docs › MCP Server**

A Model Context Protocol server that exposes the **entire admin-portal management
surface** as callable tools, so an operator working from **Claude Code** (or any other
MCP client) can configure, test, ship, observe, and govern the AI engine without
opening a browser. It is the same trust boundary as the portal — the same tables, the
same audit trail, the same superuser gate — reachable over `/mcp` instead of HTTP
forms.

**Who it's for** — engineers and operators driving the engine from an MCP client
(Claude Code, for example). It is not a customer-facing surface and is not related to
any agent's own tool-calling (the agents never call this server; it calls *them*
indirectly by editing their config).

- [Lifecycle map](#lifecycle-map) — which tool does create / manage / dispatch / monitor / evaluate
- [Tool catalog](#tool-catalog) — 54 tools across seven domains
- [Usage example](#usage-example) — a create-from-template → test → promote → observe session
- [MCP model (post-#1222)](#mcp-model-post-1222) — bundle vs external, attachments, unconditional resolver
- [Doc resources](#doc-resources) — these pages, over `wajdocs://`
- [Auth](#auth) — minting a token, verification, revocation
- [The `env` parameter](#the-env-parameter)
- [The confirm gate](#the-confirm-gate)
- [Deviations from the portal](#deviations-from-the-portal)
- [Rollout](#rollout)

---

## Lifecycle map

Everything an operator does to an agent in the portal can be done over MCP. The
whole loop — build, change, talk to it, test, ship, watch — stays inside one MCP
session:

| Stage | What you do | Tools |
|---|---|---|
| **Create** | Start an agent from a mold or fork one; add prompts, tools, MCP servers, KB sources, skills, eval suites | `templates` (`use`), `fork_profile`, `agent_types` (`create`), `prompts` (`create_version`, `publish_from_repo`), `tools` (`create`), `mcp_servers` (`create`), `kb_sources` (`create`, `upload_document`), `skills` (`create`, `add_resource`), `eval_suites` (`create`, `import_scenarios`), `channels` (`create`) |
| **Manage** | Read and change config: model, prompt text, attachments, versions, voice/memory settings | `get_profile`, `list_profiles`, `update_profile`, `prompts` (`get`, `list`, `diff`, `serve`), `profile_attachments` (add/remove ONE tool/KB/MCP/skill/sub-agent), `profile_mcp_attachments` / `profile_skill_attachments` (replace all), `profile_versions` (`save`, `diff`, `restore`, `pin`), `diff_profiles`, `memory` (`set_config`, by `profile_id`), `models`, `provider_keys`, `voice_budgets`, `env_vars`, `agent_types` (group agents into categories; assign with `update_profile(agent_type=...)`) |
| **Dispatch** | Talk to the agent, run tests, ship | `chat`, `trigger_eval_run`, `bakeoffs` (`create`), `eval_schedules` (`fire`), `voice_scenarios` (`run`), `e2e_runs`, `pytest_runs`, `promote_profile` (dry-run first), `deploy`, `rollback` |
| **Monitor** | Watch live traffic and the release state | `live_analytics`, `caching_analytics`, `voice_insights`, `conversation_insights`, `traces` (`search`, `overview`, `transcript`, `session`, `audio`), `promotion_status`, `drift_check`, `mcp_flag_parity`, `jobs`, `list_deploy_runs`, `audit` |
| **Evaluate** | Read results and decide | `eval_runs` (`get`, `compare` N runs → held / gained / regressed / still failing, `report`, `matrix`), `bakeoffs` (`get`), `prompt_optimize` (rewrite a prompt from a failing run) |

---

## Tool catalog

54 tools, grouped by domain (`admin_mcp/tools_*.py`). Every tool returns a JSON
object; domain validation failures come back as `{"error": ..., "hint": ...}` rather
than a protocol-level exception (`admin_mcp/_common.py:tool_errors`). Full parameter
lists are in each tool's own docstring, visible to any MCP client via `tools/list`.

**Schema metadata the host keys off** (`admin_mcp/_common.py`, tested wire-level in
`tests/admin_mcp/test_schema_metadata.py`):

- **Annotations** — the pure-read tools (`list_profiles`, `get_profile`,
  `diff_profiles`, `mentionables`, `eval_runs`, `list_deploy_runs`,
  `live_analytics`, `caching_analytics`, `traces`, `search`, `voice_insights`,
  `conversation_insights`,
  `promotion_status`, `list_users`, plus Ops' `drift_check`, `mcp_flag_parity`,
  `runtime_flags`) carry `readOnlyHint: true`, so MCP hosts can auto-approve them;
  `deploy`, `rollback` and `promote_profile` carry `destructiveHint: true`.
  Action-multiplexed tools that mix reads with writes stay **unannotated** — hosts
  already treat that as worst-case, and a `readOnlyHint` there would let a host
  auto-approve their destructive actions too. (`users` used to be marked
  destructive, which made every plain listing prompt for approval; it is
  unannotated now, and `list_users` is its read-only twin.)
- **Parameter descriptions** — every parameter of every tool carries a
  `description` in its schema: from `Annotated[..., Field(description=...)]` (the
  shared `Env`/`RowEnv`/`TraceEnv`/`Confirm` aliases in `_common.py` and the newer
  tools) or from `admin_mcp/param_docs.py` (one text per parameter name, with
  per-tool overrides), applied when the server is built.
  `tests/admin_mcp/test_param_descriptions.py` fails on any parameter without one.
- **Output caps** — large bodies are opt-in and lists page: `traces` `search`
  (`limit` 25 / `offset`), `list_profiles` (`limit`/`offset`), `get_profile`
  (`fields`), `eval_runs` `get`/`compare` (`include_results`), `prompts` `diff`
  (`include_content`; the unified diff is capped at 400 lines), `live_analytics`
  (`include_series`), `memory` `list` (`limit` 50), `search` (`limit`). Each paged
  response says `total` and `has_more`, so a capped list is never mistaken for
  the whole set.
- **Enums** — closed-set parameters (`action`, `env`, `source`, `cadence`,
  `suite`, `status`, `thinking_level`, `runner_kind`, …) are typed `Literal[...]`, which FastMCP
  serializes as JSON-schema enums: an invalid value is rejected by the client's
  schema validation *before* it costs a round-trip, and comes back as a
  protocol-level `isError` (pydantic message) rather than the `err()` envelope.
  Sets whose source of truth is an admin handler constant (`KB_KINDS`,
  `_CHANNEL_KINDS`, transport/scope options, …) intentionally stay runtime-checked
  strings so the schema can't drift from the handler.
- **Server instructions** (`admin_mcp/server.py:_INSTRUCTIONS`) — the cross-tool
  conventions (env defaulting, the confirm gate, job polling, attachments vs the
  legacy `mcp_server_ids` list, `wajdocs://` resources) are sent on `initialize`
  and land in the calling LLM's system prompt.

### Configure — `admin_mcp/tools_configure.py`

| Tool | Does |
|------|------|
| `list_profiles` | List agent profiles in an environment, optionally filtered by `agent_kind`, `status` and `agent_type` (a category slug, or `none` for uncategorised agents); each row carries `agent_type`, the agent's category slug or null; pages with `limit`/`offset` (`total`, `has_more`). |
| `get_profile` | Full profile config (by `profile_id`, or `agent_kind` + `slug`) with resolved tool/source/MCP-server/model labels and prompt-version pins. Returns **both** the legacy `mcp_servers` label list (resolved from `mcp_server_ids[]`) and the new `mcp_attachments` (resolved from `agent_profile_mcp_servers`, the join the runtime resolver reads) — see [MCP model](#mcp-model-post-1222) below. `fields=[...]` trims the raw row to the named columns (it carries large JSON blobs). Prompt **text** is `prompts action=get`. |
| `update_profile` | Partial update of a profile's config, including `status` (`active`\|`draft`\|`archived`\|`specialist` — `template` refused; setting `active` is refused if another row already serves the profile's `(agent_kind, environment)` as `active`, the same dual-active guard the portal enforces) and `delegatable` (expose this profile to its parent as an `ask_<slug>` delegation tool at runtime); list fields (`tool_ids`, …) REPLACE the stored list (use `profile_attachments` to add or remove one item); audited like the portal. `row_environment` moves the row's own `environment` column (`environment` is its deprecated alias; promote with `promote_profile` instead). **`voice_config` vs `voice_config_patch`:** `voice_config` REPLACES the entire column wholesale — passing e.g. `{"barge_in_enabled": true}` wipes every other voice key (`answer_watchdog_sec`, `agent_profile_slug`, budget/ceiling knobs, …), the portal never does this (it always merges the form onto the existing column). `voice_config_patch` is the safe way to set one lever: it's deep-merged onto the profile's CURRENT `voice_config` — nested dicts merge key-by-key, a `null` value deletes that key, scalars/lists replace wholesale (`admin.services.profiles.deep_merge_voice_config`), and a row still carrying the pre-2026-08-17 nested shape is promoted to flat before the merge. Both arguments **refuse a top-level `live` key**: `voice_config` is flat since 2026-08-17 and the runtime's compat shim resolves a nested block *alone*, so writing one would not set a lever — it would hide every flat key and take that tenant's voice config offline silently. Passing both in one call is refused. `agent_type` puts the agent in a user-defined category by slug (`''` or `none` takes it out); organisational only, applied at once to every environment's row of the same agent, no promote needed. |
| `fork_profile` | Fork a profile into a new staging draft (slug auto-suffixed, `status='draft'`, `delegatable=false`). Copies the model, prompt pins (same version rows), tool/KB/legacy-MCP lists, sub-agents, endpoint, voice/memory config. Does **not** copy MCP attachments or platform skills (join tables) — re-attach with `profile_attachments`. |
| `diff_profiles` | Field-by-field diff of two profiles, a → b (scalars, tools, data sources, MCP servers, sub-agents, prompt lineage). Pass the pair as `ids=[a, b]` (or `profile_a`/`profile_b`). |
| `profile_versions` | Per-(profile, environment) `agent_profile_versions` history + the `served_version_id` pin — the MCP surface for the agents screen's version bar, delegating to `admin.services.profile_versions`: `list` (history newest-first, each decorated `is_served`) \| `view` (a version's resolved snapshot; `version_id` must belong to `profile_id`) \| `save` (`notes?` — snapshot the profile's CURRENT live/draft config into a brand-new version via the same compiler promote uses; doesn't change what's served) \| `restore` (`version_id` — copy that version's config back onto the live draft, identity fields untouched; a stale tool/KB-source/MCP-server ref is dropped, a stale model is kept as the live one, and a stale prompt version is preserved by re-creating a new `agent_prompt_versions` row from the snapshot text — all reported in `dropped`; never changes `served_version_id`) \| `pin` (`version_id`, or `""` to clear — set/clear `served_version_id`, the exact snapshot `AgentProfileLoader` serves for this profile+env) \| `diff` (`version_id` → that version vs the live/draft row, or `ids=[a, b]` → two saved versions; returns plain `changes` lines like `model_main_id: 'x' -> 'y'`, `tools: +a, -b`; `include_detail=true` adds the structured diff — the portal's version-diff view, via `admin.services.profile_versions.diff_version`). Mixed read/write, left unannotated like `prompts`/`models`/`tools`. |
| `prompts` | Prompt text + prompt-version operations: `get` (the FULL TEXT of one version — by `version_id`, by `prompt_id` + `version_number` (default newest), or by `profile_id` + `prompt_key` = the version that profile **serves**; `max_chars` truncates), `list` (short previews: a profile's served prompts, every prompt slot of an `agent_kind`, or one prompt's versions), `list_versions`, `create_version` (by `prompt_id`: adds a version, pinned nowhere until `serve`; for a generic `custom` agent, `profile_id` + `content` alone creates the agent's own prompt if needed, adds the version AND pins it — setting an agent's instructions in one call), `serve` (pin a version onto a profile slot), `diff` (`ids=[version_id_a, version_id_b]` or `prompt_id` + `version_a`/`version_b` numbers → a unified diff capped at 400 lines + `diff_stat`; `include_content=true` adds both bodies), `publish_from_repo` (the sanctioned way to get a repo prompt constant onto a live stack — imports `constant`, a dotted path that must resolve under the `runtime.prompts` package, e.g. `runtime.prompts.agents_demo.<CONSTANT>`, so the — often half-Arabic — body never passes through the MCP client/shell where quoting would corrupt it; resolves the target via `prompt_id`, or `profile_id` + `slug`; without `confirm` returns a dry run — current version, a `diff_stat` of char counts and the first differing line (never the body), and a `validation` block previewing what a confirmed call would be checked against; already-current is a no-op; with `confirm` inserts version N+1 via the same path as `create_version` and repoints via the same path as `serve`, carrying the served version's `notes` unless overridden — some seeders tell a profile's prompt versions apart by that notes string). **`create_version`, `serve` and `publish_from_repo`'s confirm branch all run `core.prompt_validator.validate_prompt` before writing** — an error-severity problem (a size-budget breach, a missing/reverted bilingual half, or an `ask_agent` instruction worded descriptively instead of imperatively) refuses the write with `{"error": ..., "problems": [...]}` and nothing is inserted or repointed; `serve` is covered too, because pinning an existing bad version is the same failure as publishing a new one. `override_validation=true` (together with `confirm=true` — a two-flag act) forces the write through and is recorded via `actor_audit` (`action="validation_override"`, carrying the full problem list) so a forced publish stays visible afterwards. |
| `models` | `agent_models` registry: `list`, `get`, `create`, `update`, `promote` (clone a model to the sibling env, idempotent on name+environment). |
| `provider_keys` | Model provider keys stored encrypted (`provider_keys`), the portal's Settings › Model keys. A stored key is used before the provider's environment variable. **No action returns a key**: only its hint (last four characters), who set it and when. `list` (per provider: `source` = `portal` \| `environment` \| `none`, the stored default key and named credentials; `can_store` is false, with the reason in `problem`, when the server has no `SECRET_KEY`) \| `set` (`provider` + `value`, optional `name` for a named credential; refused when `SECRET_KEY` is unset, never stored as plain text) \| `remove` (`provider`, optional `name`, `confirm=true`) \| `test` (asks the provider for its model list with the key in use; returns `{ok, source, detail}` with the provider's own error, key removed, on failure). Writes are audited under the calling operator. |
| `voice_budgets` | Per-tenant daily duplex-voice allowances (`ai_voice_budgets`, audio seconds/day) — mirrors the portal's Settings › Voice budgets page (formerly a card on any realtime model's detail page; the write routes are still `POST /admin/voice-budgets`): `list` (each row decorated with `used_today`) \| `get` \| `set` (`business_id` + `daily_seconds`; upsert — a raw non-UUID tenant id is mapped to the same derived row the voice edge charges) \| `delete` (tenant falls back to the engine default, `VOICE_LIVE_DAILY_SECONDS`). |
| `tools` | `agent_tools` registry — only `kind="http"` tools are writable, same rule the portal enforces: `list`, `get`, `create`, `update`, `delete` (soft, `is_deprecated`), `dry_run`. Also carries the optional `usage_guidance` and `context_fields` columns — `create`/`update` accept them when supplied. `context_fields` (JSON object) is consumed by the MCP tool resolver for server-side credential injection; `usage_guidance` (free text) is rendered by the runtime as an indented line under the tool in the `{available_tools}` prompt catalog when the tool is bound — it was previously dead config. |
| `mcp_servers` | `agent_mcp_servers` registry: `list` \| `get` (row + bundle `members` + `attached_count` of profiles wired to it) \| `create` (`source=bundle\|external`) \| `update` \| `delete` (cascade — detaches the id from every profile's `mcp_server_ids`, then removes its `agent_mcp_tools` and `agent_profile_mcp_servers` rows before the row itself) \| `members` (reconcile a bundle's `agent_mcp_tools` membership) \| `discover` (SSRF-guarded `tools/list` probe of a candidate external MCP endpoint). Built-in (`is_builtin`) MCPs are seeder-managed — `update`/`delete`/`members` all refuse them. |
| `kb_sources` | `agent_data_sources` registry CRUD + `upload_document` (stages a page and queues a background chunk/embed ingestion job, pgvector sources only; refused, with the settings to add, when no embedding model is set up, and when the source's passages were embedded by a different model than the one in force). `list` also returns `embedding`: the provider, model and address in force, `ready`, and `problem` (what to set) when not. |
| `prompt_optimize` | LLM-assisted rewrite of a prompt from a failing eval run — the portal's `/admin/prompts/{id}/optimize` without streaming, using the same helpers (`_failing_scenarios_from_run`, `build_optimizer_meta_prompt`, `_stream_optimizer`). Returns the proposal, a `diff_stat` against the newest version and its validation problems. `confirm=true` saves it **in-process** as version N+1 (through the same validation gate as `create_version`; `override_validation` needs `confirm`); it is not served until `prompts action=serve`. |
| `profile_attachments` | Attach or detach **one or a few** items without re-sending the whole list: `kind` = `tools` (`tool_ids`) \| `kb` (`data_source_ids`) \| `mcp` (`agent_profile_mcp_servers`, what binds tools at runtime; `tool_subset` pins tools on the MCPs being added) \| `skills` (`agent_profile_skills`) \| `downstream` (sub-agents); `action` = `list` \| `add` \| `remove` with `ids`. Existing items keep their order and `tool_subset`; re-adding or removing an absent id is a no-op reported in `unchanged`. New ids are validated before any write (unknown/deprecated tools, missing KB sources or profiles, non-attachable MCPs, tenant or wrong-kind skills, a profile as its own sub-agent). Writes go through the same service/reconcile helpers, audit and cache invalidation as the replace-all paths, which still work. |
| `profile_mcp_attachments` | `agent_profile_mcp_servers` for one profile: `list` (current attachments + the attachable MCP set) \| `set` (REPLACE the profile's attachments — a list of `{mcp_server_id, tool_subset?}` — detaching any MCP left out; each MCP must be attachable and every pinned `tool_subset` name must be a real tool on that MCP, validated before any write; for an external server registered with no subset of its own, the names are checked against its live `tools/list`, and an unreachable server is refused with the reason). |
| `agent_types` | User-defined agent **categories** ("Support", "Sales", "Internal tools") — mirrors Agents › Categories (`/admin/agent-types`): `list` (every category in order, each with `agent_count`, plus the allowed `colors`) \| `create` (`display_name`; optional `slug`, `description`, `color`, `sort_order`) \| `update` (`agent_type` = slug + any of `display_name`, `description`, `color`, `sort_order`; the slug itself is fixed) \| `delete` (`agent_type`, `confirm=true` — the agents in it are **not** deleted, they become uncategorised; without `confirm` it reports how many). A category is data, not an agent kind: it never changes how an agent runs and is not part of a promote snapshot. A fresh install has none. Assign with `update_profile(agent_type=...)` or `templates(action="use", agent_type=...)`; filter with `list_profiles(agent_type=...)`. |
| `mentionables` | The `@`-mention catalog for a profile's system prompt — its own tools, MCPs, and KB sources, usable as `@[tool\|mcp\|kb:name]` tokens. Scoped to the profile, not org-wide. |
| `templates` | The protected **mold catalog** (curated `status='template'` profiles): `list` (browse the molds) \| `get` (one mold's full config) \| `use` (fork a mold into a named `staging` draft, optionally `as_subagent_of` a parent — composites fork their sub-agent molds too). Read-only: templates cannot be created/edited/deleted here, and `update_profile`/`promote_profile` refuse a `status='template'` row — `fork_profile` on a template instead produces a draft copy (the same operation `use` performs). `use` also takes `model_id` (the new agent's model instead of the template's; must be an active chat model, checked before anything is created). Where generic agents are on, a blank `slug` is made from `display_name` ("Support desk" → `support-desk`) and the new agent does not keep the template's description. `use` also takes `agent_type` (a category slug) to file the new agent, and the sub-agents made with it, under that category; an unknown category refuses the call before anything is created. |
| `skills` | Agent skills (versioned instruction bundles with progressive disclosure): `list` (filter by `agent_kind`/`business_id`) \| `get` (skill + versions + latest version's resources) \| `create` (slug, display_name, description, agent_kind, content; optional `business_id` for a tenant skill, `always_on`) \| `update` (metadata patch; also archive/unarchive by passing `status="active"\|"draft"\|"archived"` and/or `is_active`) \| `create_version` (new immutable body) \| `list_resources` (a version's bundled files — name, type, size, no bodies; `version_id` default newest) \| `add_resource` (`filename`, `content`, `content_type?` — replaces a same-named file on that version, 64 KB cap, like the portal) \| `delete_resource` (`resource_id`, `confirm=true`). Same validation as the portal: slug format, no `{`/`}` in catalog text, 32 KB body cap. Archiving (`status="archived"`) forces `is_active=False` alongside it and invalidates `AgentProfileLoader` in addition to the skills cache, matching the portal's "Archive skill" button (`/admin/skills/{id}/archive`) exactly — including its audit action name, `"archive"`. The portal only archives (one-way button); reversing via `update(status="active")` is an MCP-only capability on top of the same `status` column, audited as `"unarchive"`. |
| `profile_skill_attachments` | `agent_profile_skills` for one profile: `list` (current attachment rows in position order) \| `set` (REPLACE the join with `skill_ids` in order — only active **platform** skills matching the profile's `agent_kind` attach; tenant skills resolve dynamically and are never pinned). |

### Test — `admin_mcp/tools_test.py`

| Tool | Does |
|------|------|
| `trigger_eval_run` | Fire an eval run (`source=suite` or `source=live_chats`) and return `run_id` immediately — it's a background job, not a blocking call. `source=suite` accepts an optional `profile_version_id` to pin the run to one promoted profile snapshot (e.g. reproducing a bake-off variant); it must belong to `profile_id` and the suite needs an evaluator model. `source=suite` also accepts `voice_mode` (`sim`\|`live`) for a `runner_kind=voice_live` suite, overriding the suite's `default_context.voice.mode` — blank uses the suite default (itself defaulting to `sim`). `live` is a real, billable provider call; calling this tool directly IS the manual trigger `_voice_live_guard` allows — a schedule or bake-off requesting `live` is rejected regardless (see [Test → Triggering voice suites over MCP](./03-test.md#voice-eval-suites-runner_kind-voice_live)). `live_chats` requires `judge_model` + `expectations`. `prompt_overrides` maps a prompt to the version NUMBER to run instead of the pinned one: `{"system": 3}` means this agent's own instructions (a generic agent's `<slug>:system` row), `{"<agent_kind>/<prompt slug>": 3}` names any other. They are checked before the run is queued — an entry naming no such prompt or version is refused, because the runtime would otherwise serve the live prompt and the run would be reported under the variant's label. A generic `custom` agent needs no `endpoint_path`: the runner derives `/v1/agents/{slug}/chat`. |
| `eval_runs` | `list` (rows include `profile_id`, `schedule_id`, `bakeoff_id`, `profile_version_id` so a run traces back to what spawned/pinned it), `get` (status/score, same linkage columns, and `results_summary` — counts, pass rate and the failing scenarios; `include_results=true` adds every scenario's full result), `report` (pass-rate trend over N days), `compare` (`run_ids=[baseline, …, latest]`, two or more runs; `run_id` + `run_b` still work → every scenario labelled `held` \| `gained` \| `regressed` \| `still_failing` (plus `new`/`dropped` when one side lacks it), baseline → latest, with a per-run `vs_baseline` trail, `labels` counts, regressions first; also run summaries, `a`/`b` for a pair, and the portal's N-column `matrix` from `eval_handlers._compare_runs_n`), `matrix` (`run_ids[]` or `suite_id` → per-(suite,variant) and per-variant roll-up of mean score, pass-rate, p50/p95 latency & TTFT, tokens, cost — the engine behind `bakeoffs get` and the portal's bake-off matrix), `overview` (the portal's Testing Overview: per agent its verdict and one row per text suite plus a `kind="voice"` row rolling up its voice checks with per-check status). **Voice checks are runs too:** every `list` row carries `kind` (text \| voice; `kind=` filters, default both), a voice-check run's `suite_id` is `voice:<slug>` and its `results` are the checked call steps; `get`/`compare` accept voice-check run ids, and `compare` refuses a text/voice mix. |
| `bakeoffs` | Generic bake-off (`agent_bakeoffs`): `create` (name, profile_id, ≥2 `variants` — each `{label?, profile_version_id?, model_id?, gen_config?}` with `gen_config` keys `temperature?`/`thinking_level?`/`max_output_tokens?`/`top_p?` — `suite_ids` ≥1, `repeats`; two pre-flight checks — every suite has a judge, every variant's `profile_version_id` belongs to `profile_id` — run before anything is written, so a bad input fails with one clear error instead of a half-spawned bake-off; spawns `variants × suites × repeats` linked `agent_eval_runs` via the same trigger `trigger_eval_run` uses; returns `{bakeoff_id, run_ids}`), `list` (recent bake-offs, decorated with `n_variants`/`n_suites`/`_profile_name`), `get` (`bakeoff_id` [+ optional `suite_id` focus] → `{bakeoff, matrix, status}` — `status` is recomputed from the linked runs unless already stuck `errored`, sticky). Mixed read/write like `eval_suites`/`eval_schedules`, so left unannotated rather than `READ_ONLY`. |
| `eval_suites` | `agent_eval_suites` CRUD + `import_scenarios` + `sync_from_repo`. `create`/`update` accept `profile_id` (derives + overwrites `agent_kind` from it via `_sync_agent_kind` — a suite can no longer point at a kind independent of its bound profile), `runner_kind` (`chat` default \| `voice_live`, plus any runner kind a plugin registers; selects the scoring path — see [Architecture → Eval runner_kind registry](./00-architecture.md#eval-runner_kind-registry-chat--voice_live)), and `default_context` (stored as-is, e.g. a `voice_live` suite's `{"voice": {"mode": "sim", ...}}` — now accepted on `create` too, not just `update`); `update` also accepts `name` to rename a suite. `import_scenarios` appends to the existing scenario array; a missing `id` is auto-slugified from the question. `sync_from_repo` (`name` = the suite's `name` field in `admin.seed_eval_suites._SUITES`, `confirm`) re-applies a repo suite's scenarios/`runner_kind`/`default_context`/`pass_threshold` onto its already-seeded DB row — a workaround for boot seeders being insert-if-missing by design (a repo edit to an already-seeded suite otherwise never reaches a live DB). Without `confirm`: dry-run, returns a diff (scenario id adds/removes/changed-count, and whether `runner_kind`/`default_context`/`pass_threshold` differ) and writes nothing. With `confirm=true`: applies it. Errors if `name` isn't in `_SUITES` or the suite hasn't been seeded into this env yet (it re-syncs an existing row, it does not create one). `create` stores any `scenarios` passed with it (each needs a `question`, as in `import_scenarios`). |
| `eval_schedules` | `agent_eval_schedules` CRUD (create/update are a FULL replace of the schedule config, mirroring the portal's edit form) + `fire` (run now, then roll `next_run_at` forward from cadence). `profile_id`, when supplied, derives `agent_kind` the same way `eval_suites` does; `source=live_chats` requires `agent_kind` (directly or via `profile_id`) + `judge_model`, with `expectations` defaulting to that kind's standard rubric when left blank. |
| `pytest_runs` | `trigger` (suite ∈ `smoke`\|`unit_runtime`\|`unit_admin`\|`all`, launches a background job, returns `job_id`), `get`, `list`. |
| `voice_scenarios` | Live duplex-voice acceptance scenarios (`ai_voice_scenarios`): `list` \| `get` (slug) \| `set` (slug + agent_kind + steps; upsert) \| `delete` (slug) \| `run` (slug — launches a background job that places a REAL call against the engine's own `/voice/live`, or a `target` ws(s):// for a remote deployment, e.g. the demo VM; optional `headers` are per-invocation HTTP/WS headers forwarded to the WS connection and merged into each `http_check` step's own headers — the way to carry a portal login cookie or other secret for a deep protected-flow scenario without ever storing it on the scenario row) \| `runs` (recent results, optional slug filter). Runs need `AZURE_SPEECH_KEY` on the service, else they land as `status=skipped`. See [Voice scenarios](./03-test.md#voice-scenarios--live-call-acceptance). |
| `e2e_runs` | `trigger` (runs a profile's active eval suite scenario-by-scenario against its live `endpoint_path` in `env`, judge-scored when the suite has an evaluator model; background job), `get`, `list`. |

### Ship — `admin_mcp/tools_ship.py`

| Tool | Does |
|------|------|
| `promote_profile` | Staging → Production (or reverse). **Dry-run by default** and annotated destructive: without `confirm=true` it writes nothing and returns `{"confirmed": false, "would": ..., "preview": {changes, eval_gate, prompt_tool_mismatch, blockers, replaces_profile_id}}` — a plain list of what the promote would change in the target env (model, prompt versions `v12 -> v14`, tools/KB/sub-agents `+x, -y`, MCP attachments, or "creates a NEW prod profile") and what would block it. With `confirm=true` it runs the hard eval gate: a failing gate returns `{error: eval_gate_failed, score, threshold}` unless `force=true` (audited as an override). Writes `environment='prod'` rows into the same single AI-Supabase DB — there is no cross-env branch to promote across. |
| `promotion_status` | The `/admin/env` Promote board as data: per `agent_kind`, the staging and prod rows (slug, status, served version pin, model), each one's latest eval vs its suite threshold, and `promotable` for staging rows. Read-only. |
| `deploy` | Re-ship the prod chat app at a git ref by dispatching the deployment's deploy workflow (see [Deploy](./05-deploy.md)). **Prod-only** — in this tool `staging` is an environment column, not a deployable target. Requires `confirm=true`. |
| `rollback` | Roll the prod chat app back to a prior **revision** by dispatching the deployment's rollback workflow (the revision name is validated as lowercase letters, digits and hyphens, ≤63 chars). **Prod-only**. Requires `confirm=true`. |
| `list_deploy_runs` | Recent GitHub Actions deploy/rollback runs (status, conclusion, ref, timestamps). |
| `deployment_targets` | `agent_deployment_targets` CRUD — the engine service URLs the E2E runner, cross-env promote and `chat` (other env) read (e.g. `https://engine.example.com`). The row's own environment column is `row_environment` (`environment` still accepted). |
| `env_vars` | `agent_env_vars` CRUD (row column: `row_environment`, `environment` still accepted); secret rows (`is_secret=true`) are masked `••••` in list/get output and in audit before/after payloads; `reveal=true` bypasses the mask (separately audited as `reveal`). |

### Observe — `admin_mcp/tools_observe.py`

Read-only — no audit trail, no mutations. Every tool here calls the same portal
screen functions the HTTP routes use, so payload shapes match what an operator sees
in the browser.

| Tool | Does |
|------|------|
| `live_analytics` | Laminar/ClickHouse-backed KPI dashboard data: volume, cost, latency percentiles, error rate, per-agent/channel breakdowns, top tools. `trace_env` scopes to a `waj.env` tag (`environment` still accepted); the per-bucket time series is dropped unless `include_series=true`. |
| `voice_insights` | Duplex-voice latency rollup (the Live Monitor's voice card, `load_voice_live_insights`): sessions, turns, tool calls, barge-ins, p50/p95 end-of-turn → first audio, → spoken answer, delegate and brain-LLM time. `ch_ok: false` means ClickHouse was unreachable (blanks, not zeros). |
| `conversation_insights` | The Overview's **What happened in conversations** section (`admin.screens.insights.build_insights`): outcome mix (resolved / handed to a person / left before finishing / unclear, with the change in points vs the previous period), top topics, customer languages, chat vs voice, and a per-agent mix. `period` = `24h`\|`7d`\|`30d`; `trace_env` scopes by the conversation's recorded environment (`prod` strict; `staging` also admits conversations whose environment was never recorded; `''` = all). `available: false` = the table could not be read (unknown, not zero); `has_data: false` = nothing analysed in the period yet. Partner builds only see their own agents. Rows come from the background sweep in `core/conversation_insights.py`. |
| `caching_analytics` | Prompt-cache insights (`trace_env` for the tag, `env` for the pricing branch): overall cache hit rate, cache-read tokens, estimated USD saved, a cached-vs-uncached token time series, and a per-agent cache-hit-rate breakdown (a focused slice of `live_analytics`; reads the auto-instrumented `gen_ai.usage.cache_read_input_tokens` span attribute). |
| `traces` | Trace explorer: `search` (filtered recent traces, paged `limit` 25 / `offset`, with `total`/`has_more`), `get` (span tree), `span` (full span detail), `transcript` (persisted conversation turns + tool calls), `overview` (session + per-tool rollup + errors), `session` (`session_id` → that chat session's traces, newest first — the portal's `/admin/sessions/{id}` lookup via `agent_traces`), `audio` (`trace_id` or `session_id` → call-recording **metadata**: duration, status, the portal path of the playable WAV; never the audio bytes — `load_voice_audio_json`). |
| `search` | Global search across screens and tabs, agents, models, tools, toolsets, knowledge, tests, conversations and people (masked), and Help/docs sections — the same rows as `/admin/search.json` (prefix `>` / `@` / `?` to scope), at most `limit` (≤30) results. |

### Govern — `admin_mcp/tools_govern.py`

| Tool | Does |
|------|------|
| `users` | `list` admin users (with superuser flag), `set_superuser` (grant/revoke full admin+MCP access; `confirm=true` required). Unannotated (mixed read/write). |
| `list_users` | Read-only twin of `users action=list` (optionally `superusers_only`), so a host can auto-approve a listing. |
| `audit` | `agent_audit_log` `list` (paged, `has_more`) / `get` / `probe_write` (`verify` is its deprecated name — it writes a liveness marker row and checks nothing; see Deviations). |
| `api_keys` | Portal-issued API keys (`api_keys` table; mirrors Settings › API keys through `admin.services.api_keys`): `list` (name, `key_prefix`, scopes, `created_at`, `last_used_at`, `expires_at`, `status` = active \| expired \| revoked; never the key or its digest) \| `create` (`name`, `scopes` = `chat` and/or `admin`, default `["chat"]`; `expires_in_days`, 0 = never) \| `revoke` (`key_id`; `confirm=true` required, final). **`create` returns the key once**, in `key`: only a SHA-256 digest is stored, so it cannot be read again. No `env`: keys belong to the engine instance. Both writes are audited under the caller (a key that creates a key is the actor). Unannotated (mixed read/write). |
| `memory` | Per-user memory + per-agent `memory_config`: `get_config`, `set_config`, `list` (`limit`, default 50), `add`, `edit` (re-embeds), `pin`, `delete` (soft-archive). `get_config`/`set_config` take `profile_id` to target one specific profile (a draft, a fork, or the voice specialist whose `memory_config` the voice brain reads) instead of "the active profile for `agent_kind`". |
| `channels` | `agent_channels` registry CRUD; `delete` is a soft `is_active=false` (channels are referenced from traces/log analytics, so it never hard-deletes). Reads and writes both use the `env` branch client. |
| `test_fixtures` | Vault of real JWTs/UUIDs/secrets for pytest/eval runs against auth-gated endpoints; `jwt`/`secret` kinds are masked in list output and audit payloads; `export` reveals real values (mirrors the `.env.test` download) and is audited as `action="export"` (fixture names + count, never values) — same for the portal's download route. Reads and writes both use the `env` branch client. |
| `jobs` | `admin_jobs` tracker: `list`, `get`, `cancel` (cooperative — flips `status='cancelled'` if still queued/running). Takes `env`: env-pinned spawns (the `e2e_runs` trigger, `voice_scenarios` `run`) write their tracker row to that env's branch, so poll/cancel with the env the job was triggered for (single-DB deployments fall back to local either way). |

### Dispatch — `admin_mcp/tools_dispatch.py`

| Tool | Does |
|------|------|
| `chat` | One chat turn against an agent through the **same path as the portal's test-chat widget** (`admin/static/test-chat.js`): a POST to the profile's `endpoint_path` with the operator's own JWT and the widget's body (`message`, `session_id`, `locale`, `business_id`, `agent_kind`), the SSE stream collected server-side. Returns `{reply, tool_calls, trace_id, session_id, latency_ms, errors, served_by, status}`; pass `session_id` back to continue the conversation. `tool_calls` come from the persisted assistant message (outputs capped at 2000 chars), `trace_id` from `agent_traces` — open it with `traces action=overview`. **`profile_id` itself answers** — a draft copy or a specialist too — via `X-WAJ-Profile-Id` (verified operators only, same agent_kind as the endpoint); `served_by` names it, and `warning` flags a switched-off (`is_active=false`) profile, where the active one answers instead. `profile_version_id` instead pins a saved snapshot (`X-WAJ-Profile-Version`). Each kind's body comes from `admin.services.test_chat` (shared with the widget): the AI-agents demo kinds send `agent` + `case_id` (pass `case_id` to pick one) and return a `state` token — pass it back with `session_id` to continue. Non-chat endpoints are refused with the reason. `env` equal to this process's own environment runs in-process through the app; the other env goes to its default `deployment_targets` row, which must accept your token. It is a real turn: the agent's tools run and chat history is written. Unannotated. |

### Ops — `admin_mcp/tools_ops.py`

Read-only — no audit trail, no mutations, same convention as Observe. Three operator
capabilities that used to exist only as CLI/internal code, surfaced here so an operator
never has to leave the tool loop for them. The same drift findings and runtime-flags
block also render on the portal's `/admin/drift` screen (`admin/drift_handlers.py`,
[Govern → Drift Check](./04-govern.md#drift-check)).

| Tool | Does |
|------|------|
| `drift_check` | Wraps `scripts.check_profile_drift.collect_drift()`. For `config`/`model`/`tools`/`voice`/`persona` findings it is still the sanctioned way to answer *"is my merged fix actually live?"* (prompts and config change through the portal, MCP or the database, never through a merge) — a small registry of config levers whose wrong value is silent, checked because every `scripts/seed_*.py`/`admin/seed_*.py` is insert-if-missing, so once a row exists a seeder can never push a repo edit into it. For `prompt` findings the question is different: prompts are authored in the DB now, so a served prompt DIFFERING from the repo constant is expected, not a defect, and shows as `info` (or `warning` when nothing is registered in `core.prompt_validator` to confirm the divergence is safe) — never counted in `summary.drifted`. What IS still `critical` for a prompt is the served version FAILING `core.prompt_validator.validate_prompt` outright — the same gate the `prompts` tool's publish path runs before a write. `want_diff` (default `false`) includes the actual prompt text delta — left off by default because a diff can be large and half-Arabic, expensive to put in an LLM's context. `profile`/`agent_kind` optionally narrow the findings. **`audited_environment`** names the one `agent_profiles.environment` every fetch was scoped to (the calling process's own) — the AI project is a single DB holding every environment's rows, so `drifted: 0` describes that environment and nothing else; quote it whenever you report a verdict. A finding whose summary carries `[INCIDENT LEVER LEFT ENGAGED]` is a config lever pulled during a past incident and never restored — `summary.incident_levers_engaged` counts them separately from ordinary drift. Read-only: publish a fix with `prompts action=publish_from_repo` / `prompts action=serve` / `update_profile`, not this tool. |
| `mcp_flag_parity` | For every active profile in `env`, resolves its tool set the way the runtime does and reports what actually binds — the same logic as `GET /admin/api/mcp-flag-parity`, factored into a shared `compute_mcp_flag_parity()` helper so the route and this tool can't drift apart. Run it right after `profile_mcp_attachments action=set` to confirm the wiring resolves instead of trusting the write succeeded. **Caveat carried over from the portal check:** an unreachable external MCP endpoint (e.g. a down sandbox) shows up as *fewer* bound tools, not an error — a low count on an external-source profile means "check the endpoint," not necessarily "the attachment is wrong." |
| `runtime_flags` | Process-visible values of a small curated list of container-level flags — `VOICE_LIVE_SIM_ENABLED`, `ENABLE_ADMIN_MCP`, `ENVIRONMENT`, any variable a plugin's deployment scopes are selected by, `EMBED_PROVIDER`, `EMBED_MODEL` — read directly off `os.environ` on **this** process. These are invisible to the `env_vars` tool and `/admin/env-vars`: both CRUD the `agent_env_vars` table, a different read path than a bare `os.getenv` in application code. Chiefly for `VOICE_LIVE_SIM_ENABLED`: `endpoints/voice_live.py` reads it with a bare `os.getenv` (not `core/env_resolver.py`), so a voice-eval sim run against a stack without it set silently degrades to a real billable provider session instead of erroring — check this before trusting a sim run's cost profile. `value` is `null` when the var is unset (distinct from an empty string). Flags only, never secrets — nothing on this curated list is sensitive, by design; do not extend it with a secret-valued var. **Deliberately not routed through `core/env_resolver.py`** — that resolver also reads the `agent_env_vars` DB table, which would change the gate's security posture (a DB-editable value influencing a loopback-only sim gate); wiring that up is a separate, deliberately out-of-scope follow-up. |

---

## Usage example

The tools are designed to compose into the full agent lifecycle **without leaving the
Claude Code loop**. Below is a representative session that stands up a new agent from a
template mold, specializes it, tests it, ships it, and checks its telemetry. Tool calls
are shown as `tool(args…) → result`; responses are trimmed to the fields that matter.

**1 — Start from a mold (don't fork a live profile).** Browse the protected catalog and
fork one into a named `staging` draft. Composite molds fork their sub-agents too:

```
templates(action="list")
  → { templates: [ {template_id:"…", slug:"mold:data-analyst", display_name:"Data Analyst + Charts",
                    agent_kind:"business", is_composite:true }, {slug:"mold:rag", …}, … ] }

templates(action="use", template_id="…data-analyst", display_name="Revenue Analyst")
  → { root:"sp_rev", created:["sp_rev","sp_rev_charts"], as_subagent_of:null }
        # forked mold:data-analyst → draft "Revenue Analyst" (staging)
        # forked its mold:chart-subagent → draft "Revenue Analyst — Charts"
        # the new root's downstream_profile_ids already point at the new chart draft
```

**2 — Specialize the draft** with the same connect tools the portal exposes — attach an
MCP bundle (subset-pinned), point the system prompt at the right version, tune the model:

```
profile_mcp_attachments(profile_id="sp_rev", action="set",
    attachments=[{mcp_server_id:"…reporting", tool_subset:["run_report","list_kpis"]}])
  → { attached: 1, detached: 0 }

prompts(action="create_version", prompt_id="pr_rev",             # prompt_id from get_profile(sp_rev)
    content="You are a revenue analyst… {available_tools}")       # {available_tools} auto-fills bound tools
  → { version_id:"pv_9", is_latest:true }
prompts(action="serve", profile_id="sp_rev", prompt_key="system", version_id="pv_9")
  → { served:"pv_9" }

update_profile(profile_id="sp_rev", model_main_id="…gemini-3-pro", endpoint_path="/revenue-analyst")
  → { updated:true, changed:["endpoint_path","model_main_id"] }
```

**3 — Talk to it, then test before shipping.** `chat` sends one turn through the same
path as the portal's test-chat widget. Fire the profile's eval suite (or a live-chat
judge run) and compare it against the last run; evals are background jobs:

```
chat(profile_id="sp_rev", message="What was revenue last week?", business_id="…")
  → { reply:"Revenue last week was …", tool_calls:[{name:"run_report", …}],
      trace_id:"tr_7", session_id:"…", latency_ms:2140, served_by:"…" }
trigger_eval_run(profile_id="sp_rev", source="suite")
  → { run_id:"er_42" }                                            # returns immediately (background job)
eval_runs(action="compare", run_ids=["er_41", "er_42"])
  → { labels:{held:9, gained:2, regressed:1, still_failing:1, …},
      scenarios:[{scenario_id:"refunds", label:"regressed", passed:[true,false]}, …] }
```

**4 — Promote to prod.** `promote_profile` previews first: without `confirm` it
writes nothing and lists what would change. Re-call with `confirm=true` to apply.
The hard eval gate blocks a below-threshold profile unless you force it (audited):

```
promote_profile(profile_id="sp_rev", target_env="prod")
  → { confirmed:false, would:"promote Revenue Analyst (business/…) from staging to prod: 3 change(s)",
      preview:{ changes:["model_main_id: 'm_old' -> 'm_new'", "prompt 'system': v3 -> v9",
                         "MCP attachments: +reporting[run_report,list_kpis]"], blockers:[] } }
promote_profile(profile_id="sp_rev", target_env="prod", confirm=true)
  → { promoted:true, profile_id:"sp_rev_prod", target_env:"prod", eval_gate:{status:"pass",score:0.86} }
        # below-threshold instead returns {error:"eval_gate_failed", …};
        # re-call with force=true AND confirm=true to override (audited)
```

**5 — Observe** the live agent — analytics, then drill into a trace when something looks off:

```
live_analytics(trace_env="prod", agent_kind="business", hours=168)
  → { volume:1240, cost_usd:3.11, p95_latency_ms:2200, error_rate:0.4, top_tools:[…] }
traces(action="search", agent_kind="business", status="error")
  → { traces:[ {trace_id:"tr_1", status:"error", …}, … ] }
traces(action="overview", trace_id="tr_1")
  → { turns:6, tool_calls:[…], errors:[{tool:"run_report", message:"timeout"}] }
```

The same session can then loop back — edit the prompt, re-run evals, re-promote — all
over MCP. Templates are read-only throughout: `update_profile`/`promote_profile` on a
`status='template'` row return a clear `{"error": "templates are read-only…"}` (fork one
with `templates(action="use")` instead).

---

## MCP model (post-#1222)

An MCP server row (`agent_mcp_servers`) is one of two `source` values:

- **`bundle`** — a named collection of `agent_tools`, membership tracked in
  `agent_mcp_tools` (ordered, editable via `mcp_servers action=members`). It has
  no endpoint of its own; it's a curated group of existing Python/HTTP tools.
- **`external`** — a remote MCP endpoint reached over `transport`/`endpoint`.
  `mcp_servers action=discover` runs a live, SSRF-guarded `tools/list` probe
  against a candidate endpoint before you wire it in.

Profiles attach MCPs via `agent_profile_mcp_servers` (`profile_mcp_attachments`)
rather than only an id list — each attachment can optionally pin a
`tool_subset` (a subset of that MCP's tools, instead of all of them).
`get_profile` surfaces this as `mcp_attachments`, alongside the legacy
`mcp_servers` label list still resolved from `mcp_server_ids[]`.

MCP resolution is now the sole tool-resolution path. `runtime/agent.py`'s
`prepare()` always calls `resolve_mcp_tools_for_profile`
(`runtime/tools/_mcp_resolver.py`), which reads the `agent_profile_mcp_servers`
attachments; HTTP operator tools bind through MCP bundle membership. The
`mcp_server_ids` field is legacy portal-display and scope-gating bookkeeping
only, with no runtime tool-resolution effect. Compile-on-promote freezes the
attachments into the profile version snapshot, so a profile edited through this
portal should set `profile_mcp_attachments` (which drives tool resolution); the
legacy `mcp_server_ids` field has no impact on tool availability.

*Historical note:* this retrofit shipped dark behind a `MCP_TOOL_RESOLUTION` env
flag (default off), which for a time coexisted with a legacy `tool_ids`
resolution path (`resolve_tools_for_profile` + `filter_by_profile`). Both the
flag and that legacy path have since been removed — MCP resolution is now
unconditional.

---

## Doc resources

These operator docs are exposed read-only as MCP **resources**, so a Claude Code
session can pull them into context without leaving the tool loop:

```
wajdocs://index          → README.md
wajdocs://operate        → 01-operate.md
wajdocs://configure      → 02-configure.md
wajdocs://test           → 03-test.md
wajdocs://govern         → 04-govern.md
wajdocs://deploy         → 05-deploy.md
wajdocs://mcp            → 06-mcp.md   (this page)
wajdocs://architecture   → 00-architecture.md
wajdocs://data-model     → data-model.md
```

`admin_mcp/resources.py` keeps a local `_PAGES` slug→file map (rather than importing
`admin.router` at resource-call time, which would pull in heavy Jinja templates).
`tests/admin_mcp/test_resources.py` is a **drift guard**: it asserts `_PAGES` has the
same slugs, files, and order as `admin.router.DOC_PAGES`, so the two can never quietly
diverge.

---

## Auth

The MCP endpoint accepts one of two bearer credentials, both checked in
`admin_mcp/auth.py:MCPAuthMiddleware` before any MCP protocol handling.

### API key (recommended)

Create a key under **Settings › API keys** (`/admin/api-keys`) with **Full access**
(the `admin` scope), copy it when it is shown, and send it as the bearer token:

```json
{
  "mcpServers": {
    "ai-engine": {
      "type": "http",
      "url": "https://<your engine>/mcp",
      "headers": { "Authorization": "Bearer ${AI_ENGINE_API_KEY}" }
    }
  }
}
```

- **Scope boundary.** Only a key with the `admin` scope opens `/mcp`. A `chat`-only key
  gets **403** on every MCP message: it can talk to agents over
  `POST /v1/agents/{slug}/chat` and nothing else.
- **Who the caller is.** The actor is the key itself: `MCP_ACTOR.user_id` is the key's
  row id, so an audit row written by a tool names the key (Activity shows it as
  *API key: \<name\>*), not the person who created it.
- **Revocation** is immediate: revoke the key in the portal or with
  `api_keys action=revoke`, and its next request gets 401. Unknown, malformed, revoked
  and expired keys all get the same 401 body.
- How keys are stored and checked: [Govern → API keys](./04-govern.md#api-keys).

### Superuser token

The original credential, unchanged: the **same trust boundary as the portal**
(`admin.auth.resolve_superuser`), a signature-verified JWT whose `user` row has
`permission.is_superuser`:

1. **Mint a token** with `scripts/mint_mcp_token.py`:
   ```bash
   SUPABASE_JWT_SECRET=... python scripts/mint_mcp_token.py \
     --auth-id <auth.users id> --email you@example.com --days 90
   ```
   The output is an HS256 JWT signed with `SUPABASE_JWT_SECRET` (the **main**
   Supabase project's JWT secret — the same secret admin-session JWTs are verified
   against for HS256), carrying `token_use=mcp` so mint-script tokens are identifiable
   in logs. It only works while HS256 is accepted (`SUPABASE_JWT_ALGORITHMS`, default
   `ES256,HS256`); a stack that drops HS256 needs a token from a real GoTrue sign-in.
   It is a bearer credential for whichever user's `auth_id` you pass — that user must
   already have `permission.is_superuser = true` or every call will 403.
2. **Store it** as `WAJ_AI_ENGINE_MCP_TOKEN` in your shell env and reference it from
   your MCP client's config (for Claude Code, a `.mcp.json` at the repo root) via
   `${WAJ_AI_ENGINE_MCP_TOKEN}` in the `Authorization: Bearer …` header.
3. **Verification** happens in `admin_mcp/auth.py:MCPAuthMiddleware`, which runs
   *before* any MCP protocol handling: it extracts the bearer token, calls the same
   `resolve_superuser` the portal's `require_superuser` dependency uses (off-thread,
   since it makes a synchronous Supabase call), and stashes the actor identity in a
   `MCP_ACTOR` contextvar for audit attribution. Missing/invalid tokens get a plain
   401; a verified-but-non-superuser token gets 403. This closes the same forged-`sub`
   gap that used to affect the admin HTTP path — both surfaces now **fail closed** on
   a missing or wrong key (the `SUPABASE_JWT_SECRET` for HS256, the JWKS for ES256)
   rather than trusting an unverified claim.
4. **Revocation** — there is no token blocklist. Revoke a specific user's access by
   flipping `permission.is_superuser` to `false` (via the `users` tool or
   `/admin/users`); revoke *every* outstanding token at once by rotating
   the signing key that issued it — `SUPABASE_JWT_SECRET` for HS256 tokens (this
   includes mint-script tokens), or revoking the ES256 signing key in Supabase for
   ES256 ones. Either also invalidates portal session cookies signed with that key.

---

## The `env` parameter

Every env-scoped tool takes `env: "staging" | "prod"` (a schema-level enum — any
other value is rejected client-side), defaulting to **`staging`** — the same default
the portal's environment switcher uses. It selects which value of the
`environment` column the tool reads/writes — `staging` rows are the editable working
set, `prod` rows are compile-on-promote snapshots (see Topology note below). There is
no `dev` value (the legacy tier is retired, see
[Architecture](./00-architecture.md#two-environments-staging--production)).

**One name, one meaning.** `env` is always the config branch. Two other
"environment" ideas have their own names:

| Name | Means | Tools | Old name (still accepted) |
|---|---|---|---|
| `env` | Config branch: which `environment` rows to read/write | every env-scoped tool | — |
| `row_environment` | A row's own `environment` column | `update_profile`, `deployment_targets`, `env_vars` | `environment` |
| `trace_env` | Laminar `waj.env` trace tag (`dev` allowed) | `live_analytics`, `caching_analytics`, `voice_insights` | `environment` |

Passing a new name and its old alias with **different** values is an error.
`deploy`/`rollback`/`list_deploy_runs` keep `environment`, which there names the
service (only `prod` is deployable). Process-scoped tools (`drift_check`,
`runtime_flags`, `users`/`list_users`) take no `env`: they report on this process
or the main database. Pairs and sets of ids use one convention too: `ids=[a, b]`
(`diff_profiles`, `prompts diff`, `profile_versions diff`) and `run_ids=[…]`
(`eval_runs compare`/`matrix`); the older pair arguments still work.

**Topology note.** The engine runs on **one AI-Supabase DB** — there are no separate
staging/production branch databases. `staging` and `prod` are values of the
`environment` column on the env-scoped tables only (`agent_profiles`, `agent_models`,
`agent_e2e_runs`, `agent_env_vars`, `agent_deployment_targets`, `test_fixtures`):
`staging` rows are the editable working set, `prod` rows are compile-on-promote
snapshots. `promote_profile` writes `environment='prod'` rows into the *same* DB —
there's no cross-env branch to promote across. `client_for_env` still tries a
per-branch client first (`get_service_client_for_env`) for any deployment that still
configures `STAGING_/PROD_AI_SUPABASE_*`, but on this topology those vars are unset,
so it falls back to the single service client and the `env=` filtering above is what
actually separates the two environments.

---

## The confirm gate

Destructive or hard-to-reverse actions require an explicit `confirm=true` on the
**same call** — calling without it returns `{"confirmed": false, "would": "...",
"hint": "No change was made. Re-call with confirm=true to execute."}` instead of
acting, so an operator (or an LLM driving the tool) sees what *would* happen before
committing. This covers: `tools`/`mcp_servers`/`kb_sources`/`eval_schedules`/
`deployment_targets`/`env_vars`/`channels`/`test_fixtures`/`agent_types` deletes, `provider_keys`
`remove`, `users`
`set_superuser`, `memory` `delete`, `jobs` (no confirm — cancel is cooperative and
always audited), `skills` `delete_resource`, and the Ship-domain `deploy`/`rollback`.
**`promote_profile` is gated too**: without `confirm=true` it returns the dry run
plus a `preview` of what would change and what would block it; `force=true`
(override a failing eval gate) also needs `confirm=true`. `prompt_optimize` only
saves its proposal with `confirm=true`.

---

## Deviations from the portal

Most tools mirror their portal handler exactly (same table, same columns, same audit
action strings). A few things read differently over MCP than they do in the browser —
worth knowing before you're surprised by one:

- **`audit` `probe_write`** (old name `verify`, still accepted) — this does **not**
  walk a hash chain or verify anything. It records a liveness marker
  (`record_change(entity_type="agent_audit_log", entity_id=<all-zero uuid>,
  action="CALLEE_VERIFY")`) proving a superuser can still write to the log, exactly
  like the portal's "verify" button. It was renamed so the action says what it does.
- **`pytest_runs` `trigger` is async** — it launches a background job and returns
  `job_id` immediately (poll `jobs action=get` or `pytest_runs action=get`), unlike
  the portal's `/admin/tests` page, which blocks the HTTP request until pytest
  finishes. It also always runs in the **control-plane container** against the local
  checkout — it's not env-scoped like the eval/e2e tools' `env=` parameter.
- **Prompt optimize is `prompt_optimize`, not streamed** — the portal streams the
  rewrite over SSE into a textarea; the MCP tool waits for the whole proposal and can
  save it in-process (`confirm=true`), so a half-Arabic body never round-trips
  through the client. The portal's prompt **compare** form (two eval runs pinned to
  two prompt versions) has no single tool: call `trigger_eval_run` twice with
  `prompt_overrides`, then `eval_runs action=compare`.
- **`chat` answers as the profile you name** — like the portal widget, it pins
  `profile_id` with `X-WAJ-Profile-Id`, so a draft or specialist answers as itself.
  It adds `profile_version_id` (the `X-WAJ-Profile-Version` header the eval runner
  uses) to test a saved snapshot, which the widget cannot do.
- **Not exposed yet** (no reusable helper, or out of scope for this pass): the voice
  cockpit's owner-resolved view (`/admin/agents/{id}/voice` — use `get_profile` +
  `update_profile(voice_config_patch=…)` on the OWNER row), `/admin/lmnr/diagnose`,
  KB document listing/delete and binary uploads, firing every due schedule at once,
  `input_validators`, and a signed download URL for call audio (`traces action=audio`
  returns the portal path of the WAV instead).
- **`deploy`/`rollback` are a prod-only escape hatch, not the canonical deploy path**
  — the **canonical** deploy is whatever CI the deployment runs on a merge to `main`.
  `environment="staging"` on either tool errors immediately (`"staging" is an
  environment column, not a service`): these tools only ever target prod. Use them
  only to re-ship prod at a specific `git_ref` or to roll prod back to a prior
  revision outside the normal merge-to-main flow.
- **`eval_suites action=sync_from_repo` is MCP-only** — the portal has no "re-sync
  this suite from the repo constant" button; an operator without MCP access has to
  either wait for a fresh env (seeders are insert-if-missing, so this only ever
  helps a *new* env) or hand-edit the suite's scenarios/`runner_kind`/
  `default_context`/`pass_threshold` through the edit form to match the repo by eye.
- **`voice_scenarios action=run`'s `headers` reaches the portal route too, but the
  portal UI doesn't expose it yet** — `POST /admin/voice-scenarios/{slug}/run` now
  accepts the same optional `target`/`headers_json` the MCP tool does (so a deep
  protected-flow scenario's cookie can be supplied without ever landing in the
  scenario row either way), but `voice_scenarios.html`'s Run button still posts a
  bare form — only a direct POST (or the MCP tool) can supply them today.
- **`trigger_eval_run`'s `voice_mode` now matches the portal's "Run now" form** — both
  accept `sim`/`live`/blank for a `runner_kind=voice_live` suite; this was a real gap
  (the MCP tool silently dropped the override, always falling back to the suite
  default) until this PR, not a deliberate portal-only feature.

---

## Rollout

The MCP server mounts at `/mcp` **only on the control-plane deployment**, gated by
`ENABLE_ADMIN_MCP=1` (`main.py`) — set the flag on the control-plane only, so the
public chat app built from the same image never mounts `/mcp`. It
also requires a key for the tokens it will see — `SUPABASE_JWT_SECRET` for HS256, a
reachable JWKS at `SUPABASE_URL` for ES256; without it every request fails closed
with a 401/403 rather than falling back to unverified trust. See
[Architecture → Auth model](./00-architecture.md#auth-model) and
[Architecture → MCP management surface](./00-architecture.md#mcp-management-surface).

**Deploy checklist (post-merge, not part of this doc's PR):** set
`SUPABASE_JWT_SECRET` and `ENABLE_ADMIN_MCP=1` on the control-plane only; mint a
token per operator; verify `https://<control-plane host>/mcp` rejects anonymous
requests with 401.
