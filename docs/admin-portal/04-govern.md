# Govern

**Docs › Govern**

Control who can use the portal and review every change made through it.

- [Login & access](#login--access)
- [Users & Roles](#users--roles)
- [Audit Log](#audit-log)
- [Drift Check](#drift-check)

---

## Login & access

`GET/POST /admin/login` · `login.html` · `router.py` + `auth.py`

Email + password sign-in via Supabase GoTrue, with a collapsible "sign in with token
instead" fallback (paste a raw Supabase JWT — for service accounts / CI). On success
the `waj_admin_token` cookie is set (httpOnly, secure in prod, 8h) and you land on
`/admin/live`.

Every `/admin/*` route is gated by `require_superuser`: the JWT's signature is verified
and the `user` row must have `permission.is_superuser`. A valid token without
the flag still gets 403. See [Architecture → Auth](./00-architecture.md#auth-model).

---

## Settings hub

`GET /admin/settings` · `settings.html` · `router.py:settings_hub()` · entries in `admin/nav.py:SETTINGS_ENTRIES`

The sidebar's footer item. A plain-language list linking to Team & access
(`/admin/users`), API keys (`/admin/api-keys`), Releases (`/admin/env` promote + `/admin/deployments`),
Environment variables (`/admin/env?tab=variables`), Voice budgets
(`/admin/voice-budgets`, its own Settings page since 2026-10-11), Model keys
(`/admin/model-keys`, provider keys stored encrypted, see
[Configure](./02-configure.md#model-keys-settings--model-keys)), Diagnostics (`/admin/drift`,
`/admin/diagnostics/runtime`, `/admin/lmnr/diagnose`, `/admin/jobs`, `/admin/tests`) and Developer docs
(`/admin/docs`). On a partner build only Team & access, Voice budgets and Model keys are listed (API keys is
staff-only: the chat API a key opens is not served there).
Each member screen shows the Settings section tabs. The Voice budgets entry reads
"How much voice-call time each agent can use per day.", or "… each business …" when
a plugin provides businesses (`agent_catalog.business_field_label()`), as on the
Voice budgets page.

---

## First-run setup

`GET /admin/setup` · `setup.html` · `first_run_handlers.py` · `admin/services/first_run.py`
· table `engine_setup`

A fresh install has no admin account and **no default login**. While that is true, `/`,
`/admin` and `/admin/login` redirect to `/admin/setup`, where the first person creates
the admin account. Every other `/admin` route still requires a signed-in superuser.

**Only in a build with no product plugin.** `first_run.enabled()` is false whenever
`core.plugins.has_product()` is true, and the whole module is then inert: no database
read at boot, no code, no banner, and the setup routes redirect to the login page. A
product build keeps its users in its own main database and must never offer to create
one here. In a build without a plugin, `FIRST_RUN_SETUP=0` turns the page off.

**What "first run" is.** The `engine_setup` row is not completed **and** no
`public."user"` row has `permission.is_superuser = true`. Both are read once at boot,
off the event loop (`first_run.on_boot`, called from the lifespan after the seeders). A
superuser found at boot (a headless install made one from `ADMIN_EMAIL` /
`ADMIN_PASSWORD`) completes the row, so setup stays closed even if every admin is
removed later. A read error means "not first run": the page fails closed and the log
says so. Per request the answer comes from memory; while in first run the database is
re-read at most every 5 seconds (in a worker thread) to notice an admin made elsewhere.

**The setup code.**

- Made at boot while in first run: 16 characters from a 31-symbol alphabet without
  look-alikes (about 79 bits), shown as `XXXX-XXXX-XXXX-XXXX`.
- Printed **once per boot**, in a banner on the engine's standard output
  (`docker compose logs engine`). It is not sent through the logging system, and it is
  never stored, logged or returned anywhere else: the database holds its SHA-256
  digest, its expiry (24 hours) and the count of wrong tries.
- **Each boot replaces it.** The page says so. A restart between reading the log and
  typing the code means reading the log again.
- Compared with `hmac.compare_digest` on digests. 5 wrong tries lock setup; an expired
  or locked code is replaced by restarting the engine.
- Void once an admin exists: the row is completed and the digest removed.

**The three steps.**

1. `POST /admin/setup` (email, password twice, setup code). The password must be 10 to
   72 characters and not obviously weak (a short deny-list, repeats, sequences, the
   email). The account is made by `core.admin_account.ensure_admin_account`, the same
   function the headless init step calls: an auth user (already confirmed) and a
   `public."user"` row with `is_superuser`. A claim on the `engine_setup` row (a
   conditional `UPDATE` on `claimed_at`) lets exactly one of two simultaneous
   submissions create an account, in one engine process or several. The handler then
   signs in with the new credentials and sets the portal cookie with the login
   handler's own `_set_admin_cookie`.
2. `GET` / `POST /admin/setup/model-key` (signed in). Choose a provider, paste a key,
   **Save and test**: `provider_keys.set_key` then `check_key`
   (see [Model keys](./02-configure.md#model-keys-settings--model-keys)). Optional:
   **Skip for now**, or **I will use a local model** (Ollama instructions). With no
   `SECRET_KEY` the form is replaced by the reason and the step can only be skipped.
3. `GET` / `POST /admin/setup/api-key` (signed in). The POST creates a **Full access**
   key with `api_keys.create_key` and answers with the page that shows it once (never a
   redirect, as on [API keys](#api-keys)), with a copy button, a `curl` chat call and
   the MCP client JSON. It closes the walkthrough (`finished_at`), so a reload cannot
   mint a second key.

Steps 2 and 3 are shown only to the admin who completed step 1 (`completed_by`) and
only until the walkthrough is finished; anyone else is redirected to `/admin/`.

**After setup** `GET` and `POST /admin/setup` redirect to `/admin/login`, permanently.

**Request forgery.** The portal has no CSRF token. Step 1 is protected by the setup
code, which a forged form cannot know; steps 2 and 3 by the `SameSite=Lax` session
cookie. In addition every POST here refuses a request the browser marks as cross-site
(`Sec-Fetch-Site`, or an `Origin` that is not this host), so a page on another site
cannot use up the code's five tries on an engine that only listens on localhost.
`POST /admin/setup` is also under the per-IP rate limiter, like the login.

**Headless installs.** With `ADMIN_EMAIL` and `ADMIN_PASSWORD` set, the init step
creates the admin and the page never appears. With neither, `deploy/compose/setup.py`
writes no admin login and `deploy/compose/init.py` creates no account. Setting only one
of the two stops the stack with a message.

---

## Public demo mode

`DEMO_MODE=true` · `demo_mode/` · `admin/demo_handlers.py` · `builder-assistant.js` ·
operator guide: [`docs/deploy/demo.md`](../deploy/demo.md)

Off by default. With it on, the install is a public, one-click demo:

- **Entry.** No first-run page. `/`, `/admin` and every page a signed-out visitor
  opens send them to `/admin/login`, which shows only **Try the demo**:
  `POST /admin/demo/enter` signs in as the demo admin `demo@example.com` (password
  derived from `SECRET_KEY`, never shown) with a session of its own and redirects to
  `/admin/agents?env=staging` (and sets the environment cookie to staging).
  Cross-site posts are refused, like the setup page.
- **Banner.** Every portal page has a **Public demo** bar (anyone can see what you
  type, resets every night, link to the repository).
- **Build with AI.** A side panel on every page (open by default on the first
  visit) talks to `POST /admin/demo/assistant/chat` (JSON `{message, session_id?,
  page?}` → SSE: `answer`, `thought`, `step` `{tool, action, label, ok, note,
  links}` with a plain-words `label`, `working` (a keep-alive while a long step
  runs), `session`, `changed`, `done`). The assistant is the seeded custom agent
  `builder-assistant`, run with the admin MCP tools called in process as the
  visitor (`demo_mode/assistant.py`: `ALLOWED_TOOLS`, `ALLOWED_ARGS`, `check_call`)
  plus `wait_for_test_run` (waits up to two minutes for a run and returns its score
  and failing cases). Its chats are ordinary saved conversations. The turn runs
  apart from the browser's connection, so a visitor who follows a link mid-turn
  gets the rest from `GET /admin/demo/assistant/history?session_id=` (saved
  messages, plus the latest turn's steps while the engine holds it).
- **The Builder assistant is read-only** (`demo_mode/protect.py`): any change to its
  profile or instructions (`/admin/agents/<id>…`, `/admin/profiles/<id>…`,
  `/admin/prompts/<id>…`, promote) is refused, the assistant's own tools refuse to
  change, fork, promote or chat with it, and it is left out of the Agents list,
  pickers, the Conversations filter and search. The seed puts its instructions, model
  and status back to the code's on every start and reset.
- **Locked.** `demo_mode/policy.py` refuses, for GET and POST, Settings › Model keys,
  API keys, Environment variables (also the `tab=variables` of Releases), Team &
  access, Voice budgets, Diagnostics (drift, runtime, telemetry, engineering tests),
  Developer docs, deployments and deploy runs, code deploy / rollback, the background
  jobs list, prompt optimisation, `/mcp`, `/agents`, and voice calls (see **Voice** below). Models,
  scheduled test runs, model comparisons, voice checks and Activity are read-only.
  Locked screens are hidden from the sidebar, Settings and search too.
- **Voice.** Off unless `DEMO_VOICE=true` (`demo_mode/voice.py`). With it on and a
  realtime model set up, the seed turns voice on for the Support desk, the banner
  links to its Call button (`GET /admin/demo/call`), and every call on
  `WS /v1/agents/<slug>/voice` runs under the demo's own caps: call length, calls at
  the same time, calls per visitor per hour, and seconds per day for the whole demo
  (bucket `voice_seconds` in `demo_usage_daily`). A refused or ended call closes
  with code 4429 and a sentence the Call button shows. The Voice tab shows the
  limits instead of the Voice budgets link. Calls are never recorded in demo mode.
  `/voice/live` and Settings › Voice budgets stay locked.
- **Tools.** A tool server or HTTP tool at a local, private or internal address is
  refused when saved and when called (no allow-list widens it), and no process
  secret (`auth_env_var`) is sent to any address. Knowledge bases stay on the
  built-in tables. The Northwind tool server is served at `/demo/northwind/mcp` and
  called in process.
- **Caps.** Per-visitor hourly limits on chat turns, assistant messages and tool
  calls, test runs, uploads and entries; a daily cap on model turns (agent chats,
  assistant turns and test-judge calls) in `demo_usage_daily`; a lower upload size;
  an output-length cap on every model.
- **Reset.** Every night at `DEMO_RESET_HOUR_UTC` (or `python -m scripts.demo_reset`)
  everything visitors made is deleted and the Northwind demo is seeded again.

---

## API keys

Settings › **API keys** · `GET /admin/api-keys` · `api_keys.html` ·
`admin/api_keys_handlers.py` · service `admin/services/api_keys.py` · request auth
`core/api_keys.py` · table [`api_keys`](./data-model.md#api_keys)

A key lets a program call the engine without a signed-in operator's token. It is sent
as `Authorization: Bearer wae_…`.

| Scope | Portal label | Opens |
|---|---|---|
| `chat` | Chat only | `POST /v1/agents/{slug}/chat` |
| `admin` | Full access (stored with `chat`) | The chat API **and** the admin MCP server at `/mcp`: everything an admin can do, including creating more keys |

A `chat` key on `/mcp` gets 403. The `X-WAJ-*-Override` / `X-WAJ-Profile-Id` operator
headers on a chat request are honoured for an `admin` key only
(`main._request_is_operator`). A key is never a portal session: `/admin/*` still needs
a sign-in.

**The screen**

- **Keys** lists every key: name, the key's first 12 characters, access, created, last
  used and status (Active, with its expiry date if it has one / Expired / Revoked).
  Revoked keys stay in the list.
- **Create a key** takes a name, the access level and an optional expiry (30 days,
  90 days, 1 year or never). `POST /admin/api-keys` answers with the page itself,
  showing the new key **once** with a **Copy** button and a "you will not see it again"
  warning. It deliberately does not redirect: the key exists only in that response, so
  nothing (query string, cookie, session) has to hold it for a second page. `/admin`
  responses are `Cache-Control: no-store`.
- **Revoke** (`POST /admin/api-keys/{key_id}/revoke`, behind a confirm) sets
  `revoked_at`. It takes effect on the key's next request and cannot be undone.

The writes are plain form POSTs under the portal's session cookie (`SameSite=Lax`),
like every other portal write. Both are recorded in the [audit log](#audit-log)
(`entity_type = api_keys`, actions `create` / `revoke`) with the key's name, prefix and
scopes, never the key or its digest.

**How a key is stored and checked**

- A key is `wae_` + 32 random bytes (url-safe, 43 characters). Only its **SHA-256
  digest** and the 12-character display prefix are stored. With 256 bits of entropy a
  plain digest is enough, and there is no server secret whose rotation would break
  every key. The management code never selects the digest column.
- A request's key is hashed and looked up by digest (unique index), then the stored
  digest is compared again with `hmac.compare_digest`.
- Malformed, unknown, revoked and expired keys all get the **same 401** (`Invalid API
  key`); a malformed one never reaches the database. A live key without the scope the
  route needs gets 403. If the key store cannot be reached the answer is 503, never
  access.
- Nothing logs a key, a digest or a prefix; log lines name a key by its row id.
- `last_used_at` is written from a background thread, at most once a minute per key
  and engine process, so it never delays a request (and can lag by up to a minute).
- There is no cache: every request checks the database, which is why revoking is
  immediate on every engine process.
- No limiter of its own: `/v1/*` and `/mcp` sit behind the engine's per-IP request
  limit, and failed attempts count against it.

**Who a key acts as**

The key itself. A chat turn made with a key runs with `user_id` = the key's row id, so
`ai_chat_sessions.user_id` holds the key's id: a key can continue only the sessions it
started, two keys never see each other's conversations, and neither acts as the person
who created them. Over MCP the key's id is the audit actor; the Activity screen shows
it as *API key: \<name\>*.

Keys belong to the engine instance, not to an environment: they are always read and
written through the engine's own database (`core.auth.get_service_client`), and the
environment switcher is hidden on this screen. On a partner build the screen is not
listed (the chat API is not served there).

**For code that issues a key** (a setup flow, a script):

```python
from admin.services.api_keys import create_key
row, key = create_key("First key", ["chat", "admin"], created_by=user_id)
# `key` is the only copy. Show it once; `row` never contains it.
```

Over MCP: the [`api_keys`](./06-mcp.md#govern--admin_mcptools_governpy) tool.

---

## Users & Roles

Settings › **Team & access** · `GET /admin/users` · `users.html` · `router.py:users_screen()` ·
`admin/screens/govern.py` (`build_team`, `is_service_account`, `last_activity_by_actor`, `last_sign_in`)

**Shows** — one table of people (from the `user` table the portal's sign-in checks):
person (email → phone → id-prefix, plus, where a plugin words it, their role in its
own app as a caption), **Access** (`Admin` chip, or
"No portal access"), **Last active** (the later of the person's newest Activity row —
matched on `public.user.id` *or* their auth id, since MCP/script writers record the JWT
`sub` — and, for admins, the last sign-in from the main project's auth admin API) and
one action. A client-side search filters the rows. **Service accounts** (`role =
system`, `@e2e.test`-style emails, example.com / example.org addresses that are not
admins, the zero-uuid system row, rows with no email and no phone) are hidden behind
**Show service accounts (N)** (`?service=1`). An admin with an example.com address
(what a fresh install's first-run admin often types) and the signed-in admin are
always listed as people.

**Wording** — the sentence under **People** and the role caption come from
`govern.team_copy()`: neutral in the engine ("Everyone else listed here can't open
the portal.", no role caption); a plugin's `portal_display.team_copy()` replaces
them (a product build names its own apps there and shows "<role> in <its app>").

**Roles** — there is still exactly one access level: the portal's superuser bit
(`user.permission.is_superuser`), labelled **Admin** and explained on the page. No new
role system.

**Actions** — one control per row: **Make admin** / **Remove admin** (confirm dialog;
you can't remove your own admin access). Both post the unchanged contract
`POST /admin/users/{user_id}/superuser` with `grant=true|false` (plus an optional
`return_service=1` to come back to the service-accounts view), flip
`permission.is_superuser`, and are audited as `GRANT_SUPERUSER` / `REVOKE_SUPERUSER`.
(The old duplicate Revoke button + toggle switch are gone.)

**Backing table** — `user` (`permission` JSONB) in the main project; `agent_audit_log`
for last activity.

---

## Audit Log

Sidebar **Activity** · `GET /admin/audit` · `audit_list.html` · `router.py:audit_list()` ·
`admin/screens/govern.py`

An append-only record of every config-plane mutation, written best-effort by
`record_change()` so it never fails the user's request. Secret values are masked
before they're stored. The row follows the env switcher: `record_change()` writes
through `admin_service_client()`, so an edit made against the selected branch leaves
its audit row on that SAME branch — where this screen's env-scoped read
(`client_for(env)`) will find it. (With no env in scope — `/mcp` calls, background
work outside a tracked job — it falls back to the local branch, as before.)

**Shows** — one row per entry, written as a sentence: *"sara@partner.com published
WAJ AI v12 to live · 2h ago"*. `govern.describe_row()` builds it:

- **Actor** → person via the main DB `user` table (`resolve_people`, by `id` and then
  `auth_id`); a null or zero-uuid actor reads **System**; an id with no user row reads
  "Unknown user (1a2b3c4d)".
- **Verb** from `ACTION_VERBS` (unknown actions fall back to the lower-cased code).
- **Entity** → its name + a link to its page through the `ENTITY_TYPES` registry
  (agent → `/admin/agents?profile_id=…`, tool, integration, channel, KB, skill, test
  set/run, env var, …; one batched lookup per type). `CONFIG_PROMOTE` rows get the
  version they minted (`v12`, matched from `agent_profile_versions.promoted_at`).
- Meta line: relative time (full timestamp on hover), changed-field count, the
  override **reason** when one was given, and a **Details** link.

**Filters** — chip rows instead of the old free-text entity type:
**Who** (`who=` — Anyone · You · Automated · the most recent named actors),
**What** (`what=` — Publishing & releases · Agents · Knowledge · Connections ·
Testing · Team & settings; `WHAT_FILTERS` maps each to entity types and/or actions),
**When** (`when=24h|7d|30d`). The legacy `?entity_type=` link still filters (shown as a
removable chip). Pagination shows "Showing 1–50 of N" with Newer/Older.

**Overflow menu (⋯)** — staff only (hidden on partner builds): **Export (JSON)**
(`?format=json`, the filtered rows up to 1,000, each with `summary`, `actor_name`,
`entity_name`) and **Verify log replica** (`POST /admin/audit/verify`, records a
synthetic `CALLEE_VERIFY` entry).

### Audit detail
`GET /admin/audit/{audit_id}` · `audit_detail.html`
One flat section: the sentence as the heading, then a `dl.kv` (Who with email, What,
Item linked to its page, When, Reason given). **Changes** lists each changed field:
short values inline (*old → new*), long text/JSON as a unified diff (`.diff-block`),
and on a create the value as plain text; prompt version ids read as `v3 (1a2b3c4d)`.
**Technical details and raw data** (collapsed) holds the ids, action code, raw before/after
JSON and any metadata. A malformed id is a 404 page.

**Backing table** — `agent_audit_log`.

---

## Drift Check

Settings › **Diagnostics** › Config drift · `GET /admin/drift` · `drift.html` · `admin/drift_handlers.py`

A read-only report. For `config`/`model`/`voice`/`persona` findings it still answers
*"is my merged config fix actually live?"*: every `scripts/seed_*.py` /
`admin/seed_*.py` is insert-if-missing by design, so once a profile row exists a
seeder can never push a repo edit into it. Prompts and config change through the
portal, the MCP server or the database, never through a merge.

The check lists (`PROMPT_CHECKS`, `CONFIG_CHECKS`, …) come from each plugin's
`drift_checks` hook (`scripts/check_profile_drift.py`). A build with no plugin
registers none, so the report has no findings until a deployment adds its own.

For `prompt` findings the question is different, and it changed: **prompts are
authored in the DB now, never through a merge** — the sanctioned path is
`prompts(action="publish_from_repo")` or the portal, both gated by
`core.prompt_validator`. A served prompt DIFFERING from the repo constant is
therefore no longer, by itself, a defect — it is the expected steady state, and shows
as `info` (or `warning` when nothing is registered to confirm the divergence is
safe — see `core.prompt_validator.has_registered_rules`). What this screen still
answers for prompts is "is the version this profile is PINNED to actually safe?" —
a served version that FAILS `core.prompt_validator.validate_prompt` (the exact gate
the publish path runs before a write) is `critical`, whether that version is stale,
hand-edited, or freshly published. This screen renders
`scripts.check_profile_drift.collect_drift()`, the same structured findings the
`drift_check` MCP tool returns (see the Ops domain in
[MCP Server → Tool catalog](./06-mcp.md#tool-catalog)).

**Shows**

- A **summary** chip row (N ok / N drifted), above it the **audited environment**
  ("audited environment: prod (from ENVIRONMENT env var)"). Every profile fetch is
  scoped to one `agent_profiles.environment` value inside the single shared AI DB,
  so "0 drifted" about staging and "0 drifted" about prod are the same chip —
  naming the scope is what makes the verdict readable.
- A **runtime flags** panel — process-visible container-level flags on *this* process
  (`VOICE_LIVE_SIM_ENABLED`, `ENABLE_ADMIN_MCP`, `ENVIRONMENT`, any variable a
  plugin's deployment scopes are selected by (`scope_env_vars()`), `EMBED_PROVIDER`, `EMBED_MODEL`), read with a bare `os.getenv` rather than through
  `core/env_resolver.py` — so they are invisible to the `env_vars` tool and
  `/admin/env-vars`, which both CRUD the `agent_env_vars` table instead. An unset flag
  renders as an explicit "unset" chip, not a blank cell — this is chiefly here for
  `VOICE_LIVE_SIM_ENABLED`: a voice-eval sim run against a stack without it set
  silently degrades to a real billable provider session instead of erroring.
- Findings are **grouped by agent** (`govern.group_drift` — the profile slug before the
  first `.` / ` -> ` of the target), worst agents first, each group a collapsible row
  with **count badges** (critical / warning / info / ok). A group with a critical or
  warning finding opens by default and lists those first; its info/ok findings sit
  behind a nested "N more" disclosure. Info/ok-only groups start collapsed.
- The runtime flags table is collapsed on this screen; the same flags (plus the old
  Promote-tab runtime card) live on Settings › Diagnostics › **Runtime environment**
  (`GET /admin/diagnostics/runtime` · `diagnostics_runtime.html` ·
  `drift_handlers.diagnostics_runtime`).
- Each finding (`prompt`, `config`, `model`, `tools`, (`prompt`, `config`, `model`, `tools`,
  `voice` or `persona`) shows a severity chip (`ok` / `info` / `warning` / `critical` — `info` renders
  as a neutral ghost chip, never the warning-orange one, precisely so it does not
  read as something to fix), the target (profile slug, or `slug.config.path` for a
  config lever), and the summary line. `critical`/`warning` findings are shown
  **expanded** with the `why` this check exists; a diff (present on a divergent
  `prompt` finding of ANY severity, including `info`) always renders regardless of
  severity, same as the CLI's own unconditional diff output. A finding whose
  summary carries `[INCIDENT LEVER LEFT ENGAGED]` is a config lever that was pulled
  during a past incident and never restored (e.g. `voice_config.barge_in_enabled`
  left `false` after #38) — a different failure mode from "never configured," and
  called out louder for it.

**The duplex incident levers** — `barge_in_enabled` and `answer_watchdog_sec` plus
the three stall-recovery timers (`stall_recovery_sec`,
`stall_recovery_unannounced_sec`, `stall_recovery_silent_sec`) are the levers a check
list typically covers for each duplex voice profile. The three timers are best
registered as **"0 disables" rows, not equality rows**: none has a single correct
live value — a voice profile whose real `ask_agent` takes 7–13s needs a much longer
`stall_recovery_sec` than the 2.0 code default, or the tripwire fires while a healthy
call is still in flight — so any tuned value reads `ok` and only `0`, the value
`voice/live/config.py` calls "the rollback lever" for each of them, reports as an
engaged incident lever.

`stall_recovery_topic_terms` gets a line of its own, and it is not a timer: it is the
tenant vocabulary that **gates both residual tiers**. It defaults to `[]`, so an
empty value is not "unset, fine" — it means `stall_recovery_unannounced_sec` and
`stall_recovery_silent_sec` never arm on that deployment whatever they are set to.
Those two timers therefore report `info` with an **`[INERT — … ]`** note rather than
`ok` while the gate is empty (the tiers are opt-in by design, so this is never
drift), and an explicit `0` still reports as an engaged lever on top of it.

Since 2026-08-17 the duplex levers live at the TOP LEVEL of `voice_config`
(`voice_config.barge_in_enabled`, `voice_config.answer_watchdog_sec`,
`voice_config.text_only`, `voice_config.progress_after_sec`) and the check's
`CONFIG_CHECKS` read them there, so a voice finding on this screen can be taken
at face value. A profile on a stack that has **not** run the
`unify_voice_config` migration still stores the old `{"live": {…}}` shape; the
check promotes it the same way the writers do and adds its own `warning` naming
that profile ("voice_config still carries a legacy 'live' sub-block"), so the
levers are still read rather than silently coming back "unset". Seeing that
warning means the row is unmigrated, not that the lever is wrong.

**The `model` tier is provider-gated where the knob is provider-specific** — a
voice brain's thinking knob is `thinking_level` on a `google_genai` row but
`reasoning_effort` on an `azure_openai` row, and the right value differs per
provider (too little thinking can make a brain invent facts; too much costs
latency). A `ModelConfigCheck` with `provider` set applies
only while the profile's resolved model row has that provider and reports
`n/a` (ok) otherwise, so repointing the brain across clouds never leaves the
wrong check crying "unset" forever.

**The `tools` tier** (`TOOL_SUBSET_CHECKS`) compares a profile's MCP
bundle-attachment `tool_subset` (`agent_profile_mcp_servers` — the allowlist
`runtime/tools/_mcp_resolver` filters bundle members down to, i.e. the
profile's ACTUAL callable tool set) against the seed constant it was
provisioned from, imported rather than copied from the seeder that provisioned it.
Membership is compared, not order. A subset that silently loses members leaves a
caller's question with no tool that can answer it, so it falls through to the
docs search. An empty/None subset is reported too: it means "expose every bundle member",
which hands a phone caller `render_chart` and `export_csv`.

**Actions** — **Re-run** reloads the screen; every load re-runs the check live, so
there is nothing to cache or invalidate. There is deliberately **no "fix drift"
button** here — publishing a fix (a new `agent_prompt_versions` row + repointing
`prompt_version_ids`, or an `update_profile` config edit) is a separate action, done
from the Agents screen or the `prompts`/`update_profile` MCP tools. For the
duplex-voice **persona**, repoint it on the **parent (active) profile** — that
`prompt_version_ids[<persona slug>]` is what
`endpoints/voice_live.py::resolve_persona_prompt` serves, and what one promote of
the parent freezes alongside the edge config and the realtime model row. Pinning
it on the voice specialist instead is honoured only while the parent carries no
entry for the slug, and logs a warning naming both profiles rather than doing it
silently; once the parent has a pin it wins outright. That is why the persona
editor is **read-only** when you are viewing a served specialist — the save would
repoint a pin nothing reads.
`20260817121000_pin_voice_persona_versions` backfills the parent's pin, into the
live row and into every promoted snapshot.

You do not have to infer any of that from the drift screen: the Agents screen's
persona editor and Duplex voice card both carry a **served-vs-latest banner**
naming the version a caller hears, which of the four tiers resolved it
(promoted snapshot / pin on the parent / legacy pin on the specialist /
unpinned and therefore ungoverned), and whether a save on this screen reaches
callers or needs a promote first — see [Configure → Which persona version do
callers actually hear?](./02-configure.md).

Two of those tiers are governance findings in their own right even though nothing
looks broken, and the drift check now reports both as `persona` findings:

| Finding | What it means | Fix |
|---|---|---|
| `pinned on the parent (tier snapshot\|parent_pin, version N)` — `ok` | Governed. Promote and rollback control which persona callers hear. | — |
| `persona pin lives on the SPECIALIST '<slug>', not on this parent` — `warning` | The legacy placement. It resolves, but it warns on every connect and a persona save made while viewing the **parent** cannot displace it. | Repoint `prompt_version_ids[<persona slug>]` on the parent — a persona save made while viewing the parent does exactly that. |
| `persona is pinned NOWHERE — it resolves to the globally-latest version` — `warning` | Ungoverned. Promote and rollback control nothing, and a draft save is live on the next call. | Pin it on the parent from the Agents screen's persona editor, then promote the profile. |

The tier order is **not re-derived** by the drift check: it calls the same
`admin/_persona_versions.py` pure functions the banner uses, which mirror
`endpoints/voice_live.py::resolve_persona_prompt`. A third implementation free to
disagree with the resolver would be the same defect class the banner exists to
remove.

**Reading a finding** — `ok` means the live value matches this checkout (config) or
the served prompt passes validation (prompt, whether or not it matches the repo
constant); `info` is visibility only, never drift — an operator-owned config lever,
or a prompt divergence the validator actually checked and found nothing wrong with;
`warning` usually means an unset config lever (the code default applies), a
non-incident config mismatch, or — for a prompt — a divergence from the repo
constant that nothing is registered to confirm is safe; `critical` means either a
served PROMPT FAILS validation (`core.prompt_validator.validate_prompt` — the exact
gate the publish path runs before a write), or a config incident lever is still
engaged. `collect_drift()` does not switch DATABASES
(it queries whatever `core.auth.get_service_client()` resolves for this process —
the single AI-project DB), which is why the screen hides the staging/production
switcher other screens show. It does scope every fetch to one
`agent_profiles.environment` — this process's own — and reports which, as the
banner above the summary.

> **From a shell, always pass `--env`.** `python -m scripts.check_profile_drift`
> refuses to run without one (exit 2) rather than guess. An unset `ENVIRONMENT`
> collapses to `staging`, and the documented prod-access recipe is a local shell
> with `AI_SUPABASE_*` pointed at prod and `ENVIRONMENT` unset — so guessing
> audits staging rows in the prod DB, skips prod's incident levers entirely, and
> exits 0. Use `make drift-check ENV=prod`.

**Extending the checks** — add a row to `PROMPT_CHECKS`, `CONFIG_CHECKS`,
`MODEL_CONFIG_CHECKS`, `TOOL_SUBSET_CHECKS`, `INFO_CHECKS` or `PERSONA_CHECKS` in
`scripts/check_profile_drift.py`; both the MCP tool and this screen pick it up with no
further wiring. A `ConfigCheck` declares **exactly one** comparison mode —
`expected` (there is one correct value) or `disabled_value` (there is not, but there
is one value that turns the thing off) — and a test fails the PR if a row declares
neither or both.
