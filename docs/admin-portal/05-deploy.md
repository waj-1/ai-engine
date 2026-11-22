# Deploy

**Docs › Deploy**

This page documents the **Ship** group of the nav rail: **Environments** (promotion +
per-environment variables) and **Deploy** (dispatch deploys/rollbacks, watch run
history, manage deployment targets). The engine is one control-plane over **two**
environments — staging (live, editable) and production (immutable, snapshot-pinned).

- [Environments](#environments) — `Promote | Variables` tabs
  - [Promote tab](#promote-tab) (promotion · runtime diagnostics)
  - [Variables tab](#variables-tab) (env-var registry)
- [Deploy](#deploy) — `Dispatch | History | Targets` tabs

---

## Environments

`GET /admin/env` · `env.html` · `router.py:env_screen()`

Lives under **Settings** (staff-only; hidden from nav/hub/search on scoped builds).
Settings › **Releases** is the promote view (`/admin/env`, default) and shares an
in-page strip (`_releases_tabs.html`: Promote agent changes · Deploy code · History ·
Targets) with [Deploy](#deploy). Settings › **Environment variables** is
`/admin/env?tab=variables` (the env-var registry, formerly the standalone
`/admin/env-vars` screen); the old Promote/Variables strip was replaced by those
two Settings section tabs.

### Promote tab

`env.html` · `router.py:env_screen()` / `_release_overview_data()` · `admin/screens/govern.py:release_rows()`

**Overview grid** — one table, one row per agent (staging rows, templates excluded,
plus production-only rows): **Agent** (name + muted `agent_kind · slug`), **Production
version** (`v12 · 7 Oct by sara@…` — the pinned `served_version_id` snapshot, else the
newest `agent_profile_versions` row; "Not live yet" when there is no production
sibling), **Unpublished changes** (`N changes` + the changed areas / `Up to date` /
`New agent`), **Last quality check** (`92% · passed`, `79% · below 90%`, `Not tested
yet`) and **Promote**. Client-side **search** and an **Only with changes (N)** filter
(`?q=`, `?changed=1` kept in the URL). Changed rows sort first.

"Changes" compare the staging row with its production sibling on the columns the
promote copies (`govern.PROMOTE_FIELDS`): instructions (prompt version per role,
`v2 → v3`), tools / knowledge / integrations (added/removed by name), models (by name,
so per-env model ids don't show as changes), voice/memory settings (changed keys),
sub-agents/parent (by name), and plain fields.

**Single-environment (scoped) builds** — `single_env_deployment()` set: the same grid reads
**Draft → Live** (`govern.env_labels`); there is no STAGING/PROD two-column view
anywhere.

**Promote dialog** — **Promote** opens one `<dialog>` that fetches
`GET /admin/env/promote/preview?profile_id=` (JSON: live version, the change list,
quality summary, and the prompt-vs-tools check `missing_tools`) and shows **What will
change** before anything is written. If the quality gate or the tool check fails, it
asks **Why publish anyway?** (required) and the button reads **Publish anyway**. The
lone *force* checkbox is gone.

**Promotion** — the dialog posts the unchanged `POST /admin/env/promote`
(`profile_id`, `target_env`, `force`) plus two **optional** fields: `reason` (recorded
on the `EVAL_GATE_OVERRIDE` / `PROMPT_TOOL_MISMATCH_OVERRIDE` audit rows as
`after.reason` and on `CONFIG_PROMOTE` as `after._reason`) and `ui=1`. With `ui=1` a
forced post without a reason is refused, and gate failures come back to `/admin/env`
as a readable message (303) instead of a JSON 422; without `ui` (MCP, scripts) the
contract is exactly as before. It upserts the profile row into the production branch
and writes a self-contained snapshot to `agent_profile_versions`
([compile-on-promote](./00-architecture.md#compile-on-promote)). The **hard eval gate**
returns 422 if the latest eval is below `pass_threshold`; `force` overrides it (audited
as `EVAL_GATE_OVERRIDE`). The **hard prompt-tool gate** returns 422
(`prompt_tool_mismatch`) if the profile's system prompt tells the model to call a tool
its MCP binding omits — otherwise the model calls it at runtime and gets `Tool not
found` (#1032 twice); `force` overrides it too (`PROMPT_TOOL_MISMATCH_OVERRIDE`), and
`prepare()` also logs the mismatch at runtime. Promotion writes via
`client_for("production")` — a remote Supabase project when the production AI-Supabase
credentials are set, else same-DB by `environment`. Audited as `CONFIG_PROMOTE`.

**Runtime diagnostics** moved to Settings › Diagnostics › **Runtime environment**
(`/admin/diagnostics/runtime`).

**Backing tables** — `agent_profiles`, `agent_profile_versions`, `agent_eval_runs`,
`agent_eval_suites`, `agent_tools` / `agent_data_sources` / `agent_mcp_servers` /
`agent_models` / `agent_prompt_versions` (names in the change list).

### Variables tab

`GET /admin/env?tab=variables` · `_tab_env_vars.html` (included by `env.html`) · `env_vars_handlers.py`

Per-environment variable registry. **Resolution order at runtime:** `os.environ[name]`
wins; otherwise the runtime reads the `agent_env_vars` row for the active environment
(`core.env_resolver.resolve_env`). Locally, edit `.env` (process env always
wins); on a deployed app, whatever the host puts in the process environment (its own
env vars, secret-store references) lands in `os.environ` and overrides DB rows.

**Shows / actions** — two sub-sections (**staging / production**). Each has an add form
(name, value, source, description, `is_secret`) and a table (name → detail link, value,
source, secret chip, updated, description). A value is masked (`••••`) in the list,
the detail form and the audit log when the row is flagged `is_secret` **or** its name
matches `*_KEY` / `*_SECRET` / `*_TOKEN` / `*_PASSWORD` (also `PWD`, `CREDENTIALS`),
**or** the value embeds URL credentials — those show as `postgres://user:••••@host/db`
(`env_vars_handlers.is_sensitive` / `masked_value`). Auto-masked rows carry a
"hidden" chip. The detail page's **reveal** link (`?reveal=1`) unmasks for that one
response only; nothing logs the value. Saving the form with the masked value
untouched keeps the stored value (it is not overwritten with `••••`).

**Detail** — `GET /admin/env-vars/{var_id}` · `env_var_detail.html` — the detail URL is
**unchanged** by the merge. Edits name, environment, value, source, description,
`is_secret` (`POST …/{var_id}`), with a danger-zone delete
(`POST …/{var_id}/delete`). Source can name where the value really lives (a secret store, Supabase Vault).

**Backing table** — `agent_env_vars` (unique on environment + name).

> Legacy `GET /admin/env-vars` 307-redirects to `/admin/env?tab=variables` (query
> params carried over); the POST + detail sub-routes are unchanged.

---

## Deploy

`GET /admin/deployments` · `deployments.html` · `router.py` + `deploy_handlers.py`

Settings › **Releases** (staff-only). The controls to deploy or roll back the engine
through GitHub Actions, and the list of engine URLs the portal calls. Its page strip
(shared with promote, `_releases_tabs.html`): **Promote agent changes**
(`/admin/env`), **Deploy code** (trigger a deploy or rollback), **History** (recent
runs from the GitHub Actions API) and **Targets** (registered deploy targets).

> **These screens need a deploy target you configure.** The portal never builds or
> rolls anything itself: it dispatches two GitHub Actions workflows by file name,
> `deploy-agent-from-portal.yaml` and `rollback-agent.yaml`, in the repository named
> by `GITHUB_DEPLOY_REPO`, with the token in `GITHUB_DEPLOY_TOKEN` (a fine-grained
> token with `actions:read` + `actions:write`). This repository does not ship those
> workflows: write them for your own host, and set `GITHUB_DEPLOY_REPO` to your
> repository (`owner/name`). Until `GITHUB_DEPLOY_TOKEN` is set, the Deploy form
> renders disabled with a plain note, and History shows why it cannot list runs.

### Dispatch tab

(Shown as **Deploy code**.) Two sections.

**Deploy a new engine version** — one form: Environment (Production, read-only — the
only deployable target) and **Code version** (`main` or a release tag `vX.Y.Z`;
anything else is rejected server-side, so a compromised admin session cannot deploy
unreviewed code). **Deploy to Production** posts `POST /admin/env/prod/deploy`
(`git_ref`), dispatching `deploy-agent-from-portal.yaml` via `workflow_dispatch`; the
workflow run is the source of truth for what deployed. Audited as `DEPLOY_TRIGGERED`.
The workflow deploys the image only: apply database migrations before you deploy code
that needs them.

**Roll back** — `POST /admin/env/prod/rollback` (`target_revision`) dispatches
`rollback-agent.yaml` with a revision name (lowercase letters, digits and hyphens,
≤63 characters, checked by regex before dispatch). It bypasses CI (confirm required)
and is audited as `ROLLBACK_TRIGGERED`. The revision picker
(`GET /admin/deploy-revisions.json`, `deploy_handlers._fetch_revisions`) currently
reads revisions from Azure Container Apps (`AZURE_SUBSCRIPTION_ID`,
`ACA_RESOURCE_GROUP`, an identity with read access to the app). On any other host,
or when the list cannot load, the page opens **Advanced: enter a revision name**, a
free-text field that posts `target_revision` directly. Revisions created before
`ROLLBACK_FLOOR` are marked "too old to roll back to".

### History tab

`GET /admin/deployments?tab=history`

**Recent deploys** — a table (started, env, workflow, ref/SHA, actor, status, duration,
logs link) that polls `GET /admin/deploy-runs.json` every ~5s (backing off on error,
pausing when the tab is hidden) and pulls run status from the GitHub Actions API. When
runs can't be listed the JSON carries an `error` (no token, GitHub unreachable, a non-2xx
answer) and the table shows an error row plus that plain reason. No runs at all reads
"No deploys or rollbacks yet."

### Targets tab

`GET /admin/deployments?tab=targets`

**Targets** — add/edit/delete deployment targets (env, label, base URL, optional auth
env var, default flag, active flag). Only one `is_default` per env. The eval, E2E and
cross-environment chat runners call a target's base URL.

The engine seeds no target of its own. With none registered for an environment, test
and E2E runs call the engine itself (`AI_AGENT_BASE_URL`, else `127.0.0.1` on its own
`PORT`). A deployment's plugin may seed its own targets.

**Backing table** — `agent_deployment_targets`.
**External** — GitHub Actions (`deploy-agent-from-portal.yaml`, `rollback-agent.yaml`,
both yours to provide).
