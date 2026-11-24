# WAJ AI Admin Portal — Documentation

**Settings › Developer docs › Overview**

The admin portal (mounted at `/admin/*`) is the **source of truth** for every AI
agent the engine serves. An agent is assembled from configuration rows — not
code — so editing a profile in the portal changes the agent's behaviour on the
next request. This documentation describes every screen, sub-view, and popup,
plus the architecture and data model behind them.

These pages are written to be read two ways:

- **As markdown** in the repo (`docs/admin-portal/`) — easy for an AI or
  a human to grep and reason about.
- **Inside the portal** at `/admin/docs` (Settings › Developer docs), rendered
  with a left-hand nav and breadcrumbs (the same markdown, rendered by `marked.js`).

---

## Navigation

### Nav rail → docs (8 items + Settings)

The sidebar is defined once in `admin/nav.py` (`PRIMARY`, `SETTINGS`). A screen
is highlighted under the item it lives in by its **path** (`section_for`), so
every merged screen lights up its area. Members of Knowledge, Connections,
Testing and Settings show an area-level **section tab strip** at the top
(`_components/section_tabs.html`, rendered by `_base.html` from
`admin/nav.py:SECTION_TABS`). Every URL below is unchanged.

| Sidebar item | Screens (route) | Section / page tabs | Documented in |
|------------|---------------------|------|---------------|
| **Overview** (was Dashboard) | `/admin/live` | — (Jobs moved to Settings › Diagnostics) | [Operate](./01-operate.md#overview) |
| **Agents** | `/admin/agents`, `/admin/templates`, `/admin/agent-types` | Agents · Models · Templates · Categories (agent page tabs: Overview · Instructions · Tools & knowledge · Sub-agents · Voice · Versions · Settings) | [Configure](./02-configure.md#agent-profiles) |
| **Conversations** (was Traces) | `/admin/observability` | — | [Operate](./01-operate.md#conversations) |
| **Knowledge** | `/admin/kb`, `/admin/skills`, `/admin/memory` | Knowledge bases · Skills · Memory | [Configure](./02-configure.md#knowledge-base) |
| **Connections** | `/admin/tools`, `/admin/channels` | Tools & integrations · Channels | [Configure](./02-configure.md#mcp--tools) |
| **Testing** | `/admin/evals`, `/admin/evals/suites`, `/admin/evals/runs`, `/admin/evals/bakeoffs`, `/admin/voice-scenarios`, `/admin/evals/scheduling`, `/admin/tests` | Overview · Test suites · Runs · Model comparison · Voice checks · Schedules · Engineering (staff) | [Test](./03-test.md#evals) |
| **Activity** (Audit Log) | `/admin/audit` | — | [Govern](./04-govern.md#audit-log) |
| **Help** | `/admin/help`, `/admin/help/{slug}` | — | [Help](#help-adminhelp) |
| **Settings** (footer) | `/admin/settings` hub → `/admin/users`, `/admin/api-keys`, `/admin/env`, `/admin/deployments`, `/admin/env?tab=variables`, `/admin/voice-budgets`, `/admin/model-keys`, `/admin/drift`, `/admin/diagnostics/runtime`, `/admin/lmnr/diagnose`, `/admin/docs` | Overview · Team & access · Voice budgets · Model keys · Releases · Environment variables · Diagnostics · Developer docs | [Govern](./04-govern.md#settings-hub) |

Inside Settings › Releases, `/admin/env` (promote) and `/admin/deployments` share
one page strip: Promote agent changes · Deploy code · History · Targets.
Settings › Diagnostics has Config drift · Runtime environment · Telemetry check.

The section pages keep their historical names (Operate / Configure / Test / Govern /
Deploy).

### Scoped (partner) builds

A deployment's plugin can define **deployment scopes** (`core/deployment.py`):
a build narrowed to one partner's agents. With a scope active
(`admin/nav.py:partner_build`, same predicate as `single_env_deployment`),
staff-only entries — Releases, Environment variables, Diagnostics, Developer docs
and the Testing › Engineering tab — are hidden from the sidebar section tabs, the
Settings hub and global search (`staff=True` entries in
`admin/search_registry.py`; search also skips the developer docs and any
plugin-supplied people source there, and narrows records/conversations to the
scope's agent kinds). `/admin/docs*` redirects (307) to `/admin/help`; the routes
behind the other hidden entries still answer. Branding (product name, accent,
logo letter) comes from the scope's `brand_*` fields via `admin/nav.py:brand()`;
a scope without `brand_accent` keeps the default teal. A build with no plugin
has no scopes, so none of this applies there.

**Only the scope's own agents.** A scoped build's AI database can still hold
other agents' rows (a shared local stack, other seeders), so every list and
picker asks `admin/agent_kinds.py` what the build serves — never a per-screen
tuple: `build_kinds()` (the scope's kinds, None on the full engine),
`scope_to_build()` / `row_in_build()` for rows (tools, suites, profiles, KB
sources, skills, voice scenarios), `build_agent_kinds()` for agent pickers (the
scope's agents, never its reserved title/suggestions kinds), `channel_in_build()`
for channels (no channel kind the scope hides, nothing restricted to or pinned on
another agent), and `mcp_server_in_build()` for MCP servers (a built-in
`__builtin__:<kind>` bundle by its kind; anything else by the agents that attach
it, through the junction table or the legacy `mcp_server_ids` column). Covered:
the Tool catalog and MCP cards, Create/edit bundle tool picker (an existing member
stays pickable), Channels list + "Answered by" + kind chips, Test suites / Runs /
Overview / Production monitoring, Knowledge / Memory / Skills pickers, Models
"Used by", Conversations agent + tool filters, Voice scenarios, and search. All
are no-ops on the full engine.

### Help (`/admin/help`)

Partner-facing guides live in `docs/partner/*.md` (`README.md` is the index;
other files are discovered by name, `01-foo.md` → `/admin/help/foo`, title =
first `# ` heading). They render with the same viewer as these docs. Images and
`<video>` go in `docs/partner/media/` and are referenced as `media/<file>`; they
are served by a static mount at `/admin/help/media`. `.dockerignore` re-includes
`docs/partner/`.

### Error page

Browser GETs under `/admin` (Accept `text/html`, not `*.json`/API paths) that
fail with 4xx render `templates/error.html` with a link back to the area — a bad
`?env=` gives a 422 page with "Open this page without it"; a malformed test/E2E
run id is a 404 (it used to 500). API callers keep the JSON envelope
(`main.py` exception handlers → `admin/router.py:render_error_page`).

### Reference

| Page | Covers |
|------|--------|
| [MCP Server](./06-mcp.md) | The admin-portal management surface exposed over MCP for Claude Code operators: the 54-tool catalog, the lifecycle map, doc resources, token minting + verified auth, the `env` parameter, the confirm gate, and where an MCP tool's behavior deviates from its portal counterpart |
| [Architecture](./00-architecture.md) | Portal-as-source-of-truth, runtime agents, env switching, compile-on-promote, seeding, observability, auth, theming & layout, legacy redirects |
| [Data Model](./data-model.md) | Every admin-portal table, its purpose, key columns, and which screen edits it |

---

## How the portal is organised

The collapsible left nav rail has eight items plus **Settings** in the footer;
each item has its own icon in the collapsed rail:

- **Overview** — live KPIs + background jobs.
- **Agents** — define how an agent behaves (+ Models tab, templates, and your own categories for grouping agents).
- **Conversations** — full trace exploration (transcripts, spans, audio).
- **Knowledge** — knowledge bases, skills, memory.
- **Connections** — MCP servers & tools, channels.
- **Testing** — eval suites, monitoring schedules, bake-offs, voice checks, pytest.
- **Activity** — the audit log.
- **Help** — partner guides from `docs/partner/`.
- **Settings** — Team & access, API keys, Releases (promote + deploy), Environment variables,
  Voice budgets, Diagnostics (drift, telemetry, jobs, tests), Developer docs (these pages).

Screens that absorbed a former standalone screen expose it as a **tab** (e.g.
Agents' Models tab is the old Models screen); every legacy URL 307-redirects to its
new home (e.g. `/admin/live?tab=jobs` → Settings › Diagnostics › Background jobs) — see [Architecture → Legacy redirects](./00-architecture.md#legacy-route-redirects).

The top bar carries a **global search** (**⌘K** / Ctrl+K — every screen and tab,
agents and their tabs, tools, knowledge, tests, conversations and the people in
them, Help and docs sections; see
[Architecture → Global search](./00-architecture.md#global-search)), the persistent **environment switcher**
(**STAGING / PRODUCTION**), and a **light/dark theme toggle**. The env selection
persists across navigation via the `waj_admin_env` cookie (default **staging**):
**staging** for 30 days, **production** only for the browser session (a session
cookie, `main.persist_admin_env`). While production is selected, every config
screen shows a red **Production** banner above the page with a *Switch to staging*
link (Operate screens, which only read, do not). Settings pages hide the switcher
unless their data is per environment (`admin/nav.py:env_chrome`): Model keys keeps
it and names the environment on the page (`env_scoped`); Voice budgets keeps it only
when staging and production read separate databases
(`router.envs_have_separate_databases()`).
**Staging** is the live, editable env (reads live config); **production** is
immutable, served from snapshots created at **Staging → Production** promotion time.
See [Architecture](./00-architecture.md).

---

## Conventions used in these docs

- **Route** — the `/admin/...` path. All routes require a superuser session.
- **Template** — the Jinja file under `admin/templates/`.
- **Handler** — the Python function that serves the route.
- **Backing table(s)** — the Supabase table(s) the screen reads/writes.
- Each screen entry lists: *what it shows · key actions · sub-views & popups · backing tables.*

---

## Maintaining these docs

> **These docs are part of the definition of done.** Whenever you add, rename, or
> remove an `/admin` screen, route, or popup — or change a backing table — update
> the matching page here **in the same PR**, before merging to `main`. The rule is
> recorded in the repo's contributor instructions.

When a change touches a screen, update the relevant section page; when it touches
a table, update [Data Model](./data-model.md); when it changes the runtime model,
update [Architecture](./00-architecture.md).
