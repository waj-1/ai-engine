# Configure

**Docs › Configure**

Everything that defines how an agent behaves — assembled from configuration, not
code. This page documents the **Build** group of the nav rail: **Agents** (with a
Models tab), **Tools & integrations** (one screen — a compact server list + a tool catalog collapsed per server), **Channels**, and
**Knowledge**. The intended build order is **Model → Tools → Knowledge Base → Agent Profile →
Channels**. Models and profiles are env-scoped (**staging** / **production**); the
rest are shared. Edits land in the env selected in the top-bar switcher — **staging
is live and editable**, **production is immutable** (snapshot-pinned) and changes
only via a **Staging → Production** promote.

> **Configure landing retired.** The old `/admin/configure` card grid duplicated the
> Build group in the sidebar, so the screen was removed; the route 307-redirects to
> `/admin/agents`.

- [Agent Profiles](#agent-profiles) (`Agents | Models | Templates` · agent table · tabbed agent page: Overview · Instructions · Tools & knowledge · Sub-agents · Voice · Versions · Settings · one Save per tab · fork · compare · promote (confirm) · prompt optimize, A/B)
  - [Models tab](#models-tab) (list · New model drawer · detail · copy to env)
  - [Voice cockpit](#the-voice-cockpit--adminagentsprofile_idvoice) (`/admin/agents/{id}/voice` — the agent's **Voice tab**: every duplex key with effective values, the brain panel, the persona editor; the single portal writer of `voice_config`)
  - [Templates / New agent](#sub-views--popups) (template gallery · two-step "New agent" dialog → draft fork)
- [MCP & Tools](#mcp--tools) (server list · searchable tool catalog per server · connect / bundle · in-bundle HTTP-tool builder · dry-run)
- [Channels](#channels)
- [Knowledge Base](#knowledge-base) (ingestion)
- [Memory](#memory) (`Memories | Config | Inspector` tabs · per-agent picker · `?agent_kind=` param)

---

## Agent Profiles

`GET /admin/agents` · `agents_list.html` / `agent_detail.html` + `agents/_tab_*.html` ·
`router.py:agents_view()` + `screens/agents.py` (data) + `screens/agents_views.py`
(view models) + `screens/agents_present.py` (plain-language labels) · `static/agents.js`

The Agents area has three section tabs: **Agents** (the agent table), **[Models](#models-tab)**
and **Templates** (`/admin/templates`). Redesigned 2026-10-11 (portal revamp PR B1) from
one 9–10k px scroll with four independent Save buttons and a right rail into a list
plus a tabbed agent page. Every POST handler kept its contract — only the forms
regrouped; the capability map is in the PR.

### Generic agents (`custom`)

Where the generic agents API is on (`ENABLE_GENERIC_AGENTS=1`), agents made in
the portal are **generic agents**: they have no code of their own, run on
`runtime/agents/custom.py`, and answer at `POST /v1/agents/{slug}/chat`. With
the flag off (the default) nothing below applies.

- **New agent wizard.** An agent made from a template, and any sub-agents the
  template brings, is created with `agent_kind = 'custom'`
  (`admin/services/templates.py`). The templates themselves keep their
  historical kind, which the gallery no longer shows. The wizard's second
  step asks for the **Model** (blank = the template's); the new agent drops
  the template's description. Its **slug**, left blank, is made from its
  name ("Support desk" → `support-desk`, then `support-desk-2`; a name with
  no Latin letters gets `agent-<n>`), because a generic agent's slug is
  public: it is its address and its hand-off name.
- **Channel.** A live generic agent shows an **API** chip (its address is
  derived, not stored), instead of "Not connected".
- **Sub-agents tab.** Each specialist row, and the agent's own "available as
  a specialist" line, shows the hand-off tool name the parent's model is
  really given (`ask_support_desk` for slug `support-desk` —
  `runtime/delegation._tool_name`), which is the name to use in the parent's
  instructions.
- **Templates stay as seeded.** The profile seeder no longer fills a
  template's empty tool and knowledge lists with its kind's defaults, and
  `admin/seed_templates.py` resets templates that were filled that way.
- **Publishing.** On the Settings tab, setting status to **Active** or
  **Specialist** also sets `is_active`, so the agent is served straight away;
  **Draft** and **Archived** clear it. Several generic agents can be Active at
  once, since each is addressed by its own slug; the one-active-per-kind check
  still applies to every other kind (`admin/services/profiles.py`).
- **Test panel.** Sends to the agent's own address, `/v1/agents/{slug}/chat`,
  with no business to pick (`admin/services/test_chat.py`).
- **Instructions.** A generic agent owns its instructions: its prompt row is
  `agent_prompts (custom, "<agent slug>:system")`, not a slot shared by the
  kind (`admin/services/custom_prompts.py`). The Instructions tab shows the
  version the agent is pinned to and saves to
  `POST /admin/agents/{profile_id}/instructions`, which creates the agent's
  prompt if it has none, then runs the same validation, "Save anyway"
  override, audit entry and pin repoint as every other prompt save. An agent
  made from a template gets its own prompt at creation, with the template's
  text copied in as version 1, so an edit never lands in the template's
  history. Compare versions, A/B test and Improve with AI appear once the
  agent has its own prompt.
- **Not yet in the portal.** The overview cards do not count tools from an
  external MCP server.

### Agent table — `/admin/agents` (no `profile_id`)

`agents_views.load_agents_list(env)` — bulk reads only (profiles, models, MCP
attachments + bundle members + tools, skill attachments; run in parallel). One row per
agent: **Agent** (name + 2-line description), **Type** (Main agent / Sub-agent *of X* /
Template), **Channel** (Chat · Voice · API · Via another agent, plus any channel a deployment's
plugin adds — derived from `endpoint_path` and the voice block; a voice "brain" specialist
counts as Voice), **Model** (display name, never the UUID), **Can use** ("44 actions ·
3 knowledge sources · 2 sub-agents" — the action count is
`bound_tool_names_from_attachments`, i.e. what binds, not `tool_ids`), **Status**,
**Last changed** (relative `updated_at`, "by you" when it was you) and a row menu
(Open, Test in chat, Versions, Compare with…, Make a draft copy). Sub-agents
(`parent_profile_id`) nest under their parent with a collapse caret. Search + filters
are client-side: Type (All agents / Main agents / Templates — **templates are hidden by
default**), Status (Active incl. specialists / Draft / Archived), Channel. "New agent"
opens the template dialog; "Compare agents" opens a two-agent picker →
`/admin/agents/diff`. Below 1280px Type/Model columns hide.

`?profile_id=` that is not in the current environment (e.g. the env switcher flipped
under an open agent) renders this table with an "isn't in Staging/Production" notice
instead of silently opening another agent (`load_agents_context` reports it as
`profile_missing`).

### Agent page — `/admin/agents?profile_id=X&tab=<tab>`

**Header** (`agents/_header.html`, shared with the voice screen): breadcrumb
`Agents / <name> ▾` — the name is a searchable **switch-agent** menu (parents then
indented sub-agents; templates only when one is open) — status and channel pills,
"changed N ago", and actions: **Test** (opens the test-chat drawer — see
[Test drawer](#test-drawer)), **Make a draft copy** (`POST /admin/agents/{id}/fork` —
the copy carries the original's toolsets (`agent_profile_mcp_servers`, with each
tool subset), skills (`agent_profile_skills`), knowledge and sub-agents, and its own
prompt pins, so an Instructions save on the copy repoints only the copy), **More ▾** →
*Promote to Production…* (a
confirm dialog that names the Production row it replaces with a "See what changes"
diff link, posts the unchanged `POST /admin/env/promote`, and shows a refusal — e.g.
the eval gate — inline; hidden on Production, on templates, and on every
single-environment build (`single_env_deployment()`), which has nowhere to
promote to), *Compare with another
agent…*, *Version history*, *Copy agent ID*, *View activity log*, *Recent
conversations*. The right rail is gone (see the map below).

#### Test drawer

`admin/static/test-chat.js`, configured per profile by
`admin/services/test_chat.request_shape` (shared with the MCP `chat` tool). Chat
endpoints do not share one body: the AI-agents demo (`/agents/chat/stream`) needs
`agent` (its key — `support`, `sales`, …) and plays a **case** (the drawer shows a case
picker; changing it starts a new conversation) and streams a `state` token the next
turn echoes back; a generic agent posts to `/v1/agents/{slug}/chat` with no
business picker. An agent with no chat address, or whose address is not a chat
stream (a plugin names those, `agent_catalog.not_chat_endpoints()`), has **Test**
disabled with the reason. **The profile on screen answers** — a draft copy or a specialist,
not the kind's active profile: the drawer sends `X-WAJ-Profile-Id` (see
[Architecture → profile pin](./00-architecture.md#request-scoped-profile-version-pin-x-waj-profile-version))
and says so under the title.

Tabs (`agents_views.DETAIL_TABS`; `?tab=` is server-rendered; unknown → Overview;
`tab=voice` 302s to the voice screen). Old in-page anchors (`#prompt`, `#mcp-attachments`,
`#downstream`, `#voice-live` …) are redirected client-side to the tab that now holds
the section (`LEGACY_ANCHORS`).

- **Overview** (default, read-only) — *What this agent does* (purpose = description,
  who talks to it in words, model by name, which agents use it; a served voice
  specialist names and links its owner), four tiles linking to their tab
  (Instructions: first lines + version; Tools & knowledge: bound action count, top 6
  action chips +N; Sub-agents; Voice — or Versions when there is no voice), and a
  **Health** strip (last E2E run + **Run test now** → `POST …/e2e/run`; last-7-days
  Conversations link; what is being served).
- **Instructions** — the system prompt editor (Edit / Preview / ⤢ Expand, @-mentions).
  Every editor shows the version **this agent is pinned to**
  (`prompt_version_ids[slug]`), labelled "Version N" (plus "vM is the newest any agent
  of this kind saved" when another copy saved a newer one); only an unpinned prompt
  falls back to the newest. `agent_prompts` rows are shared by every profile of a kind,
  so showing the newest made a draft copy's save appear on the live original, and made
  a Versions-tab **Restore** (which repoints the pin) look like it hadn't happened.
  The tab also holds every other prompt of the agent kind as an accordion (title, suggestions, the
  voice persona with its served-vs-latest status — see
  [below](#which-persona-version-do-callers-actually-hear)). Template prompts
  (`mold:*`) fold into their own collapsed group on a real agent. Each prompt is its
  own form → `POST /admin/prompts/{id}/versions` with the hidden `profile_id` /
  `prompt_key` that repoint THIS agent's pin. **Gated by
  `core.prompt_validator.validate_prompt`** (same bar as the MCP `prompts` tool): a
  failing save is refused (400, nothing written); the tab then shows the reason and
  reveals a **"Save anyway"** checkbox on that prompt only (`override_validation`,
  recorded in the activity log as `validation_override`). Header links: *Compare
  versions* (prompt diff of the last two), *A/B test*, *Improve with AI* — they carry
  `profile_id`, so their back links return to this tab (and the optimizer's save
  repoints this agent's pin). A served voice specialist's persona renders read-only
  with a link to the owner (a save there would be a silent no-op).
- **Tools & knowledge** — attached items only, each with an **+ Add …** searchable
  picker (catalog embedded as JSON; nothing unattached is ever rendered expanded):
  - *Toolsets* (`agent_profile_mcp_servers` — the runtime binding): one row per
    attached server — friendly name (`__builtin__:<kind>` → "Tools for <agent>",
    the technical name muted), Built-in/External, "All 44 actions" / "12 of 44 actions",
    expandable action chips (exactly what binds). **Choose actions** opens a modal:
    search, "All actions (new ones added automatically)" switch (= NULL subset),
    actions grouped *Read data / Make changes / Other*, select all / clear, chosen
    first. The form posts the unchanged wire format to
    `POST /admin/profiles/{id}/mcp-attachments` — `attach_mcp_ids` for every listed
    server plus `tool_subset_mode__{id}=list` + `tool_subset_all__{id}` or
    `tool_subset_cb__{id}` per action (bundles), `tool_subset__{id}` text (external);
    the handler's validation, build-scope guard and upsert-then-delete reconcile are
    unchanged.
  - *Knowledge* → `POST /admin/profiles/{id}` with `data_source_ids` only.
  - *Skills* → `POST /admin/profiles/{id}/skill-attachments`.
  - *Advanced* (collapsed): the legacy `tool_ids` list (searchable, selected first) and
    legacy `mcp_server_ids` list, labelled "not read at runtime" with the not-bound
    names — still editable for seeders/snapshots/build-scope gating.
- **Sub-agents** — *Hands work to*: the selected `downstream_profile_ids` with each
  one's description (the text a parent reads), **+ Add sub-agent** picker (delegatable
  specialists first, templates and itself excluded) — replaces the all-profiles
  checkbox table. *Can other agents hand work to this one?*: the `delegatable` switch
  (exposes it to parents as `ask_<slug>`), its description quoted, and *Used by*.
- **Voice** — only when the agent has a voice block or is a served voice specialist:
  the [voice cockpit](#the-voice-cockpit--adminagentsprofile_idvoice) rendered with the
  same header and tab strip. "Set up voice" for other agents lives on Settings.
- **Versions** — the version history (Serving chip, pin dropdown, per-row *What
  changed* / Pin / Restore) and **Save as new version** — the same routes as the old
  version bar (`…/pin-version`, `…/versions/{vid}/restore`, `…/versions`); restore's
  `dropped_*` / `edited_from` notices render here. Compare with another agent / with
  Production (the latter not on a single-environment build).
  The compare page (`/admin/agents/diff`, and a version's *What changed*) leads with
  **Toolsets and actions** — the agent's `agent_profile_mcp_servers` attachments and
  the actions chosen in each (`tool_subset`): Added / Removed / Actions differ per
  toolset, "All 17 actions" vs "15 of 17 actions", and −/+ action chips (actions in both
  fold away) — then **Skills** (`agent_profile_skills`), Knowledge sources and
  Sub-agents. A saved version uses its frozen `mcp_attachments` / `skill_attachments`
  (a version older than that says so instead of showing none). The legacy
  `tool_ids` / `mcp_server_ids` lists move under "Legacy lists (not used when the agent
  runs)". The same `toolsets` / `skills` keys come back from the MCP `diff_profiles` /
  `profile_versions diff`, and the promote preview lists action-level changes.
- **Settings** — Basics (name, description, status), Model (main + light, by name;
  link to the model page), Channel address (`endpoint_path`, marked technical), and
  *Advanced*: IDs, the endpoint's expected request body (Pydantic schema) and the
  operator notes / example payloads JSON (`POST …/input-validators`).

**One Save per tab** (`agents/_savebar.html` + `static/agents.js`). Every editing tab
wraps its forms in `[data-tab-root]`; the sticky save bar ("No unsaved changes" ·
Discard · Save) enables when any form differs from page load, then POSTs **each
changed form** to its own endpoint in order and reloads the tab with "Saved."; the
first refusal (HTTP 4xx detail, or a handler redirect carrying `?error=`) stops the run
and is shown in the bar, naming the section. Leaving with unsaved changes warns.
**No tab can blank another tab's data:** forms that post to `/admin/profiles/{id}` omit
`_mapping_form`, so `profile_update` writes only the arrays present; an empty-value
sentinel input (`name="data_source_ids" value=""` etc.) keeps "removed the last one"
expressible; and because that handler writes `delegatable` unconditionally, every form
that does not own it re-posts the current value. Pinned by
`tests/admin/test_agents_form_structure.py`; the browser round-trip (save every tab on a
fork, diff the row after each) was run against a local portal for the PR.

**Where the old controls went** (nothing removed): left rail → agent table + header
switcher · Fork/Promote buttons → header *Make a draft copy* + *More → Promote*
(confirm) · version bar → Versions tab · prompt cards, A/B, Optimize, prompt diff →
Instructions · Model card → Settings (+ Overview) · Status card → Settings (status,
description) + Sub-agents (delegatable) · Tools card + MCP attachments → Tools &
knowledge › Toolsets (legacy lists under Advanced) · Data sources → Knowledge ·
Skills → Skills · Downstream sub-agents → Sub-agents tab · Endpoint → Settings ·
Duplex-voice card + "Voice settings" chip → Voice tab · rail KPIs → Overview tiles ·
rail request body + input validators → Settings › Advanced · rail E2E → Overview
health strip · rail "Diff against another profile" → More menu + Versions tab ·
floating Test button → header **Test** (floating launcher kept) · "+ New from
template" → **New agent**.

**Generated tool catalog (`{available_tools}`).** A prompt body no longer needs a
hand-written "## Tools available" list — write the literal placeholder
`{available_tools}` and the runtime fills it at request time from the profile's
*actual* bound tool set (name + description, one per line), so it can never drift
from what the attached Toolsets actually bind. See [Architecture → Generated
tool catalog](./00-architecture.md#generated-tool-catalog-available_tools-phase-2)
for the mechanism. **This is not retroactive** — because `seed_prompts` only inserts
a prompt version once and never overwrites it, an already-served staging/production
prompt keeps its old hand-written catalog until you roll it forward here (edit the
body + **Save instructions** on the Instructions tab) or via a re-promote, and that rollout should be checked
against the agent's golden-transcript replay before it reaches production.

**@-mentions in the prompt editor.** Typing `@` at a word boundary in the
System-Prompt editor opens a dropdown of the profile's own **tools / MCPs / KBs**
(`GET /admin/api/profiles/{id}/mentionables` — superuser-gated, scoped to the
profile's MCP attachments + `data_source_ids`, so no org-wide catalog leak).
The tools offered are filtered through the same
`bound_tool_names_from_attachments` resolution the runtime and the promote gate
use — a member tool outside the attachment's `tool_subset` is not offered, since
mentioning a tool that never binds is exactly what the promote gate's
`prompt_tool_mismatch` check blocks.
Picking one inserts a stable token — `@[tool:get_clients]`, `@[mcp:Sales Docs]`,
`@[kb:Product Guide]` — that `PromptResolver` expands at resolve time to a
backticked `` `name` `` (a pure regex pre-pass — inert on any prompt with no such
token, and it never touches `{...}` blocks, so it can't choke on JSON examples the
way `format_map` would). Like `{available_tools}`, this **ships dark**: the
insert-only seeder means running prompts carry no tokens until an operator adds one
here. **v1 tradeoff to know:** the token carries the *display name*, not a uuid, so
renaming a KB/MCP after mentioning it leaves the old name in the prompt body;
uuid-stable tokens + a portal rename-scan are a documented future refinement.

**Test agent** — when the active profile has an `endpoint_path`, the header **Test** button (or `?test=1`, the list row menu's "Test in chat") opens a
bottom-right chat drawer (the floating "Test agent" launcher still works) that streams real turns against that endpoint (on an endpoint scoped
to a business, pick one first; the operator's JWT is embedded). Assistant replies render as **markdown**
(marked + DOMPurify, the same renderer as transcripts); user input stays plain
text. The widget's styling is built from the portal's semantic tokens, so it
follows the light/dark theme. It posts to the endpoint **same-origin**, so
it works both locally and on the deployed control plane (which is built from the
agent image and serves the chat endpoints). Outside production the streamed error
event includes the **actual exception** (e.g. a missing `agent_profiles` row),
not just a generic message, so failures are self-diagnosing. Locally the boot
seeders clone the migration-seeded `dev` profile rows into the runtime's resolved
environment (`staging`) so the agent can actually serve — without that bridge every
local test 500'd with `MissingProfileError`.

#### Voice fieldset — removed 2026-08-17

The turn-based **Voice** card (Enable voice, STT/TTS provider + model, Voice ID,
Language, Sample rate) is **gone from the Agents screen**. Its eight
`voice_config` keys were read only by the `/voice/*` turn-based routes, retired
in the same series, so the controls edited nothing — and `build_voice_config_from_form`
no longer writes them. Values that existed before the cutover are preserved
under `voice_config._retired_turn_based`, which nothing reads; the
`unify_voice_config` migration put them there so a hand-tuned value could still
be recovered for one release.

Duplex settings live on the voice cockpit, described below.

##### Duplex voice (`voice_config`)

> **Shape note (2026-08-17).** This block lives at the TOP LEVEL of
> `voice_config`, not under a `live` key, and the turn-based `/voice/*` routes
> it used to sit beside are retired — so every `voice_config.live.<key>` you may
> find in an older doc or ticket means `voice_config.<key>`
> (`voice/live/config.py` reads it flat; a row still carrying `live` resolves
> from it and logs a warning). **Every writer emits the flat shape** — the
> Agents form, both voice seeders, and the admin MCP — and each promotes a
> legacy nested row on the way through, so a save repairs it
> (`voice/live/config.py::promote_legacy_voice_config`, which mirrors the
> migration: duplex keys up, old top level parked under `_retired_turn_based`).
> Do not write a `live` key back by hand: the shim resolves a nested block
> **alone**, so one would hide every flat key rather than add to them; the MCP
> refuses a nested patch outright. There is also **no channel layer** any more:
> `agent_channels.config.voice.live` is read by nothing. `filler_enabled` was
> deleted — it was declared and never consumed, and the `unify_voice_config`
> migration strips it.

A second, **opt-in** voice edge alongside the turn-based one above: a realtime
provider owns both ears and mouth over a WebSocket, instead of separate
STT/TTS calls around a text turn. It powers the demo portals' voice button,
mounted at **`/voice/live`** (`core.deployment.mount_prefix()` — empty on a
build with no deployment scope; a deployment's plugin can define a scope that
mounts it under its own prefix, resolved once at boot).

###### The voice cockpit — `/admin/agents/{profile_id}/voice`

**The one screen for the whole duplex call** — the config, the brain, and the
persona. Since the B1 redesign (2026-10-11) it IS the agent's **Voice tab**: it
renders the agent header and tab strip, opens with **Voice at a glance** (on/off,
which agent stores the settings and which answers, speech model, voice, greeting,
which persona version callers hear), then folds everything into accordions —
*Which row stores what*, *Persona* (served-vs-latest + editor), *The brain*,
*Resolved provider*, and the call settings grouped by section (Session open; each
group shows "N settings · M changed from default" and a "to check" count for
effective-value warnings). The persona, brain and voice forms share one sticky
**Save voice settings** bar (each changed form posts to its own unchanged route);
the persona's "Save anyway" checkbox appears only after a validation refusal.
Previously reached from a *Voice settings →* chip / Duplex voice link card.

It exists because a fieldset structurally could not be it. The old Agents
mapping-form fieldset had controls for 26 of the keys
`voice/live/config.py::_VOICE_FIELDS` honours, and what it omitted was not
leftovers: it included `barge_in_enabled` and `answer_watchdog_sec`, the two
levers that are *silent when wrong* — one of which was pulled
during incident #38 and left disengaged for days, because nothing on any
screen showed it. Every omission arrived the same way: a key added to the
runtime, and a hand-maintained form mapping in another file that did not grow
a row. (The key count is deliberately not written here — it drifted once,
"38" in the docs against 40 in the registry — the cockpit's subtitle derives
it from `len(_VOICE_FIELDS)`.)

So the screen is generated from a **field registry**
(`admin/screens/agent_voice.py::VOICE_FIELDS`), and
`tests/admin/test_voice_screen_field_coverage.py` asserts the partition:
every key in `_VOICE_FIELDS` has either a control here or a named entry in
`_NO_UI_BY_DESIGN`. That set is currently **empty** — as of 2026-08-18 every
operator-honoured duplex key is typed and visible — and a key added to the
runtime with neither fails the suite by name. Adding a key to `_VOICE_FIELDS`
and nothing else can no longer ship an invisible lever.

Sections, in page order: **Session** (model, voice, persona, brain profile,
greeting) · **Turn behaviour** (barge-in, the answer watchdog, all four
stall-recovery keys, sentence streaming, KB prefetch, deterministic
rendering) · **Speech** (holding line, failure and on-screen wording, each
with its per-language map, speaking rate) · **Relay** (custody and its
ceilings) · **Limits** (session, budget, concurrency — the blurb links the
hard control, Settings › Voice budgets (formerly a card on the assigned realtime model's
detail page) · **Recognition** (phrase list, backchannel tokens).

Above the sections sit three panels that make it a cockpit rather than a
form:

- **Which row stores what** — the parent/specialist pair, resolved from the
  database by slug, rendered unconditionally.
- **The persona editor** — the persona's served-vs-latest banner plus the
  editable text itself. Saving posts through the same validated
  version-create path as the Agents screen's prompt card, but the pin
  **always lands on the OWNER row** — the only pin
  `endpoints/voice_live.py::resolve_persona_prompt` reads; a specialist-side
  pin is inert and warns on every connect. The cockpit always knows the
  owner, so unlike the Agents screen (whose persona editor posts whichever
  profile is being VIEWED, and therefore renders read-only on a served
  specialist) it needs no read-only fallback.
- **The brain panel** — the SPECIALIST row's own settings, edited where the
  operator already is, each write landing on the specialist:
  - `model_main_id` (a chat-model picker — the exact inverse of the realtime
    picker's gate; a realtime row here would break every delegated turn);
  - `memory_config.enabled` (previously editable nowhere in the portal
    despite being one of the drift check's silent-when-wrong levers; the
    toggle merges onto the existing `memory_config` so retrieval/window keys
    survive, and the panel warns when it is ON for a voice brain — an awaited
    embedding between the caller's last word and the model call);
  - read-only: the brain's **served system prompt version** (number + date,
    with a link to its editor on the profile screen), and the **bound tool
    list resolved the way the runtime resolves it** — from
    `agent_profile_mcp_servers` attachments narrowed by each attachment's
    `tool_subset` (`runtime/tools/_mcp_resolver.py::
    bound_tool_names_from_attachments`), NOT from `agent_profiles.tool_ids`,
    which tool resolution reads nowhere. A `tool_ids` entry that does not
    bind renders as a subdued "listed but not bound" note naming it, because
    the checkbox list on the Agents screen looks authoritative and is not.

Three things the settings sections do that a plain form does not:

- **Inline effective-value notes.** A control whose value the provider (or
  another setting) overrides says so, above the input, instead of displaying a
  value the session will never use. The worst case is `stream_sentences`: it is
  AND-gated on the provider declaring `supports_streamed_tool_results`, which
  is `false` on `azure_voice_live` — every production duplex tenant — so the
  box read "on" while sentence streaming has never engaged there. On
  `google_genai` it IS engaged, and the note says something different: that
  provider speaks a streamed answer in full but transcribes only the first
  fragment of each sentence, so the screen's copy comes from the endpoint
  rather than from it (`transcribes_streamed_tool_results = false`). Two
  capabilities, two different notes on one checkbox — an "off" note on the
  second would send an operator chasing a latency knob that is already doing
  its job. The notes are
  derived from the capability flags **declared on the `RealtimeSession`
  subclass** (`voice/live/base.py`), not from a second opinion maintained in
  the portal, so a note cannot disagree with the gate. Also covered:
  `text_only`'s four force-disables, `speaking_rate` on a native-voice row,
  `phrase_list` outside `transcription_model: azure-speech`,
  `voice_by_language` on Gemini Live, the relay ceilings while `relay_mode` is
  off, **`relay_mode` itself on a provider that never finalizes user
  transcripts** (custody's only dispatch trigger — see the table row below),
  and a warning on each incident lever that is currently disengaged.
  Two notes come from the capability flags added when the recovery machinery
  was separated from its Azure-only assumptions: `answer_watchdog_sec` is
  marked **not armed** on a provider that does not declare
  `reports_response_in_flight` (the watchdog's only safety margin IS that
  reading — see [Architecture → duplex voice edge](./00-architecture.md#duplex-voice-edge)),
  and every commit-keyed lever — `stall_recovery_sec`,
  `stall_recovery_unannounced_sec`, `stall_recovery_silent_sec`,
  `stall_recovery_topic_terms`, `relay_intent_terms`, `backchannel_tokens` — is
  marked **inert** on a provider that does not declare
  `finalizes_user_transcripts`, because all of them arm off the final
  fragment of a caller's transcript and Gemini Live never emits one.
- **Load-bearing defaults are stated.** `stall_recovery_topic_terms` defaults
  `[]` and gates **both** residual stall tiers, so an empty box means "the
  unannounced and silent tiers are disabled on this deployment" — which the
  screen says, on that control and on the two it gates. Every control also
  shows the `LiveVoiceConfig` value an unset field resolves to, read off the
  dataclass rather than re-typed.
- **The parent/specialist relationship is the page's structure** (see *Where a
  served specialist's config lives* below), not a notice that a rail filter can
  hide. The pair is resolved from the database by slug, both halves are named
  in a panel that renders unconditionally, and the form always posts to the
  **owner's** profile id — so opening the specialist and saving writes to the
  row that actually stores the config.

Write contract: the cockpit is split into per-row writers, each refusing
rather than guessing:

- **The voice form** (`POST /admin/agents/{id}/voice`) always posts to the
  **owner's** id and owns every `_VOICE_FIELDS` key, so **blank clears** a
  control back to its built-in default, and unchecking a box persists a real
  `false` (`stream_sentences` and `relay_mode` both default `true`, so an
  omitted key would silently resolve back on). A key whose control the
  submission never carried at all (a partial POST, not an emptied input) is
  left alone, and any key with no control — `_retired_turn_based`, anything a
  future release adds — is carried through untouched. A POST without the
  `_voice_form=1` marker is refused rather than guessed at. Unparseable
  numeric input and malformed JSON keep the prior value and log, rather than
  resetting the field or rejecting the save. The **Realtime model** select
  lists only `agent_models` rows with `extra_config.realtime: true`
  (`admin/_model_picker.py::is_realtime_model` — the inverse of the
  chat-model picker's `is_chat_model` gate), with a blank "unset" option.
- **The brain form** (`POST /admin/agents/{id}/voice/brain`) posts to the
  **specialist's** id (or the owner's own, when no separate specialist
  resolves — mirroring the runtime's fallback to the kind's active profile)
  and touches only `model_main_id` and `memory_config`. It carries its own
  `_brain_form=1` marker; the memory toggle writes an explicit `false` when
  unchecked and merges onto the row's existing `memory_config`. A blank
  model select is skipped, never a clear.
- **The persona editor** posts through `POST /admin/prompts/{id}/versions`
  (prompt-validator-gated, exactly like the Agents screen's editor) with the
  hidden `profile_id` always the **owner's**, and returns to the cockpit.

**The Agents screen no longer writes `voice_config` at all.** The "Duplex
voice (live)" fieldset that lived inside the profile mapping form — 26 keys,
`live_*` input names, the `live_fieldset=1` marker, and the whole class of
wrote-to-the-viewed-row traps — was **removed 2026-08-26** along with its
writer (`admin/screens/agents.py::build_voice_config_from_form`). In its
place is a link card (keeping the `#voice-live` anchor) that opens the
cockpit, resolving the **owner** when the viewed profile is a served
specialist. `profile_update` (`POST /admin/profiles/{id}`) ignores any stray
`live_*` field a scripted caller still sends — it never assembles a
`voice_config` payload. A scripted caller that wants to write duplex keys
must POST the cockpit's route with `_voice_form=1` and `vc_*` fields (or use
the admin MCP's `update_profile` with a flat `voice_config_patch`).

| Key | Type | Description |
|-----|------|-------------|
| `enabled` | bool | Duplex voice for this profile. **Defaults to `false`** — a tenant with no `voice_config`, or `enabled` unset, gets no duplex voice and the WS closes with `CLOSE_FORBIDDEN` on connect. |
| `text_only` | bool | Ambient-scribe mode: the realtime model responds in text modality only and never synthesizes speech. Assistant text reaches the client as transcript/display frames; user-side transcription, proposals, and budgets are unchanged. **Defaults to `false`.** Set `true` for an agent that must never speak (a listening or note-taking agent); leave unset for phone agents. When enabled, the endpoint force-disables holding lines, the answer watchdog, stall recovery, and sentence streaming for the session. |
| `model_id` | uuid | An **`agent_models`** row id — the realtime model, not a chat brain. Never an agent's `model_main_id`. Must have `extra_config.realtime: true`; a missing/unknown id closes the socket at handshake (misconfiguration, not silently defaulted). |
| `agent_profile_slug` | string | Serve voice calls with a **different** profile of the same `agent_kind` (resolved by slug via `AgentProfileLoader.load_delegate`). Blank — or a slug that doesn't resolve — falls back to the kind's active profile, so this is a one-field rollback lever, same posture as `model_id`. |
| `voice_id` | string | Provider voice identifier. Defaults to `"Kore"` (a Gemini voice; Azure rows use Azure voice names, e.g. `ar-SA-HamedNeural`). |
| `voice_by_language` | object | Optional map of **primary** language subtag → voice name (`{"ar": "ar-SA-HamedNeural", "en": "en-US-AvaMultilingualNeural"}`). The session's resolved language picks its voice from here, falling back to `voice_id` when unmapped. Keyed on `"ar"`, not `"ar-SA"`, so one entry covers every regional variant. |
| `language` | string | BCP-47 tag. Blank = provider default. |
| `phrase_list` | string[] | Recognition-boosted domain terms (brand names, "IBAN", place names) for providers with a real ASR stage (`azure_voice_live`). Gemini ignores it. Entered as one term per line in the portal. |
| `caller_languages` | string[] | BCP-47 tags the caller may speak (e.g. `["ar-JO", "en-US"]`). Two or more make the Azure recognizer auto-detect among them (`input_audio_transcription.language = "ar-JO,en-US"`, max 10) instead of being locked to the session `language` — which a client's UI locale overrides, so a caller speaking Arabic to an English UI was transcribed as romanized English (2026-10-02). `azure-speech` only; ignored by the OpenAI recognizers and Gemini. Default `[]`. |
| `persona_prompt_slug` | string | An **`agent_prompts`** slug (e.g. `voice_persona`) resolved at connect time — the spoken persona is config-DB content, never a hardcoded string. Unset closes the socket (misconfiguration). Obeys the SAME promote/rollback governance as every other prompt, resolved off **this (the parent/active) profile** — the row that names the slug, and the row the portal's persona editor repoints: its frozen compile-on-promote snapshot wins if present, else its explicit `prompt_version_ids[slug]` pin, else the slug's latest version (only reached for a fresh tenant with neither, and it logs a warning because promote/rollback then govern nothing). Saving a draft version in the editor does **not** ship to prod voice callers until this profile is promoted; pinning a version in the portal does take effect for voice. Backfilled onto the parent by `20260817121000_pin_voice_persona_versions`. A pin that ends up on the voice **specialist** instead (a pre-backfill row, or an `update_profile` MCP call naming the specialist — the portal can no longer create one: that editor is read-only on a served specialist) is still honoured, with a warning naming both profiles, but ONLY when the parent carries no entry for the slug. Once the parent has one it wins outright and the warning never fires, so a stale specialist pin is inert rather than shadowing. Editable in-portal — see "Persona and other non-system prompts" below. |
| `greeting_trigger` | string | Text turn injected the moment the socket opens, so she speaks first. A realtime session is silent until spoken to, so with this blank the caller hears nothing and hangs up thinking the line is dead. Operator content, not a source constant — and since 2026-08-19 it is the **only** place the greeting's wording is specified. The persona used to carry a full example greeting in English AND Arabic on top of this; measured against `gemini-3.1-flash-live-preview`, the model reproduced the persona's Arabic example character-for-character on 4 of 4 calls, one sentence after being told to vary the wording — a script in context beats an instruction about the script. The persona now keeps only the rules (disclose once, never re-introduce, what must never drop out); the sentence lives here. Consequence for operators: **whatever this field says is what callers hear**, so an edit that drops the brand or the "AI assistant" disclosure drops them from the call — nothing downstream restores it. |
| `stream_sentences` | bool | Speak each sentence of the answer as it streams instead of waiting for the whole turn. **Defaults to `true`.** Structurally off — not config — on `azure_voice_live`, which accepts only one tool result per call id (`supports_streamed_tool_results = false`). ON for `google_genai`, where a **separate** capability decides where the caller's SCREEN text comes from: Gemini Live speaks a streamed answer in full but transcribes only the leading fragment of each response (probed live 2026-08-19 — she heard the whole answer and read `أبشر،` / `ولا تشيل` / `هل معك` / `ولا أفتحها`; the same answer as one tool result transcribed byte-identically, 5 runs of 5), so `transcribes_streamed_tool_results = false` makes the endpoint send its own copy of each sentence, released as she starts saying it. Streaming keeps its latency win and the transcript stays whole. See the explicit-off note above the table. Measured worth: **~68ms at the median** (85 streamed calls, 2026-08-19), because the brain emits the answer in one 154–539ms burst after 1.0–1.6s of nothing. Real money only on long answers. See [Data Model](./data-model.md) for the numbers. |
| `kb_prefetch_enabled` | bool | Retrieves the documentation the agent's knowledge-search tool would have searched for BEFORE the brain's first call and injects it, so a documentation question costs one model call instead of two. **Defaults to `true`.** OFF silently returns every documentation question to the two-call path — 3568ms p50 measured, the slowest class of turn on the stack — and nothing reports it; the answers stay correct and just get slow. |
| `persona_claim_recovery_enabled` | bool | **Defaults to `false`.** On, a persona turn that called no `ask_agent` but claims a result («الحين تشوف … قدامك», "here are your invoices", "refresh the page"), or that answers a caller saying the screen is empty («مش شايف إشي على الشاشة»), arms the stall recovery with the announce tier's grace: the request is delegated, and a "see nothing" re-delivers the caller's previous request. Farewells and thanks never arm it, and nor do card confirmations, navigation directions or "all set". The phrases are about an app screen, so leave it off for an agent with no screen: device talk or a goodbye would read as a claim. |
| `prefetched_context` | bool | **Defaults to `false`.** Outbound calls (the AI-agents demo portal's `agents_collection` / `agents_sales`) and its prompt-only inbound agents. The case is fetched BEFORE dialling and rendered into the persona's `{case_file}` (`runtime/tools/agents_demo/`); the realtime model answers from it and calls the agent's ACTION tools (payment/offer link, promise, note, escalation, follow-up) directly — no `ask_agent`, no brain round trip (~0.8–1.3s replies vs 2.6–4s + delegation). Every ask_agent recovery tier, the holding line and the answer watchdog stand down. Needs a persona written for it (the demo portal's `voice_persona` on those kinds); off = the delegated session. |
| `local_barge_in_sec` | number | **Defaults to `0` (off).** The endpoint listens to the caller's echo-cancelled mic itself and flushes her queued audio after this much caller speech (`voice/live/onset.py`). Needed on Gemini Live, which stops reporting `interrupted` once it finishes GENERATING — and it generates faster than real time, so most of each sentence plays from the browser buffer and could not be cut (live 2026-10-05: 18 turns, 1 barge-in). Outbound seeds 0.6 with −28 dBFS (0.3 / −35 proved too sensitive live). |
| `greeting_input_mute_sec` | number | **Defaults to `0` (off).** From the greeting's dispatch until its `turn_end` has arrived AND its queued audio has played out (capped at this many seconds), the caller's audio is forwarded to the provider as silence. `greeting_barge_in_guard_sec` only stops the endpoint reacting to an `interrupted`; on Gemini Live the provider's own activity detection has already cancelled the greeting by then, so echo or room noise in the opening second left the agent silent mid-sentence with the caller waiting for it (agents demo 10-08..10-13: ~40% of collection calls). The call recording keeps the real audio. Cost: a caller cannot talk over the greeting. The agents-demo seeds set 12. |
| `local_barge_in_dbfs` | number | Level (dBFS) above which a caller audio frame counts as speech for `local_barge_in_sec`. Default −35; raise towards −30 if noise/echo cuts her, lower towards −40 if quiet callers cannot interrupt. |
| `kb_prefetch_match_count` | number | How many KB chunks are injected before the first call. **Defaults to 3** — voice answers are 2-3 spoken sentences, so the tool's own `match_count` of 6 (12,815 chars p50) is oversized here. Too high re-creates the prompt bloat the prefetch exists to remove; too low silently starves the answer of the passage that contains it. Floored at 1. |
| `deterministic_render_enabled` | bool | INCIDENT LEVER. Renders the results of tools that have a registered renderer (`voice/live/rendering.py::register_renderer`; OTP, ticket and IBAN results, for example) — success AND failure alike — from a template and ends the turn with no synthesis call, so a renderer is structurally incapable of inventing an identifier or a positive outcome. **Defaults to `true`.** OFF hands both back to the model and reopens two fabrication surfaces: PR #137's (a live call once read back an IBAN matching neither the caller's record nor the number he had dictated minutes earlier), and the 2026-08-20 one (a live call narrated a FAILED ticket-creation call as a successful one, inventing a ticket number, branch, and technician visit time). |
| `relay_mode` | bool | **Defaults to `true`.** Server-side custody of protected multi-beat flows: once a delegated turn's tools open an OTP flow (send/verify), the voice endpoint routes every committed caller utterance straight into a delegated turn until the flow closes (`relay_quiet_grace_turns` flow-quiet turns, or the `relay_max_turns` / `relay_max_sec` ceilings below) — the realtime model keeps ears/mouth/persona but loses mid-flow discretion. Built after one live day produced a fabricated IBAN write, a phantom code request, and a dead-air stall. While custody is open the model's own `ask_agent` calls are closed **unspoken** — the endpoint has already relayed the utterance, and running both raced two ~10s delegates and two answers at the caller (live 2026-08-03). Relayed turns speak the same `progress_after_sec` holding line as delegated ones (out-of-band-capable providers only) — custody cancels the model's reply before delegating, so without it the caller read IBAN digits into 6–17s of dead air. Unchecking persists an explicit `false` (same contract as `stream_sentences`). **Provider-gated, structurally:** custody dispatches its relayed turn from the FINAL fragment of the caller's user transcript and from nothing else, so on a provider that never sends one — `google_genai` (Gemini Live streams `input_transcription` and never finishes it), declared as `finalizes_user_transcripts` on the `RealtimeSession` subclass — the endpoint keeps custody CLOSED and logs one warning per session. It used to open there and suppress the model's `ask_agent` calls with nothing to replace them: on 2026-08-19 a caller read their verification code back on a demo stack and it reached nobody (`relay mode OPEN (flow signals: ['send_otp'])`, two calls closed unspoken, `relayed=0`). Failing safe means protected flows run on the model's own calls on such a provider — answered, but without the fabricated-IBAN / unsent-code guard; the screen says so on the control. |
| `relay_intent_terms` | list | Caller-utterance substrings (case-insensitive) that open relay custody IMMEDIATELY — before any tool runs — so even the change request's first beat is relayed. Defaults to the IBAN spellings (`iban`, `آيبان`, …). Blank in the form restores the default. A false positive costs one-two relayed turns before the grace releases. |
| `server_side_iban_capture` | bool | **Defaults to `false`.** Moves a dictated account number's digit tally out of the realtime model's context into a server-side collector, so the number the caller said, the number spoken back and the number on screen cannot drift apart between pauses. The collector is supplied by a plugin (`dictation_capture()` on its `voice_capture` hook; contract in `voice/live/dictation.py`); it opens on the signal the collector names, and a build with no collector leaves it closed and logs one warning. Separate from `relay_mode`, and gated on the same `finalizes_user_transcripts` capability. |
| `speaking_rate` | string | Azure prosody `rate` (e.g. `"+10%"`, `"-20%"`, `"0.9"`). Blank = provider default (no `rate` sent at all). Applies **only** to an `azure-standard` voice (the object-shaped voice payload); it is **silently ignored on a native-voice row** (`extra_config.voice_type: "native"`, e.g. `gpt-realtime`) — a native voice's `voice` field is a bare string with no object to hang a `rate` key off, and the realtime API rejects an unrecognised shape outright, so `_voice_payload` never adds the key for that form. (The turn-based sibling that used to sit at the top level was retired with the `/voice/*` routes.) |
| `progress_after_sec` | number | Seconds before a holding line ("Still working on that, one moment.") is spoken during a long delegated turn; `0` disables it. **Defaults to 4**, and both directions from there have failed live. Too low and it collides with the answer: one demo stack ran 2.5 against a 2.68s p50 answer, so it fired on **56%** of turns and the median firing caller heard a fragment of the line before the answer's INTERRUPT delivery cut it off. Too high and it never fires — the old 8.0 was set against ~10s turns and fires on 1.3% of today's. Firing rate by threshold over 77 delegated calls (2026-08-19): 2.0s → 79%, 2.5s → 56%, 3.0s → 42%, **4.0s → 17%**, 5.0s → 2.6%. Also the default `latency_threshold_ms` (×1000) when the model row opts into Azure's server-side `interim_response` (see [Data Model → `agent_models.extra_config`](./data-model.md)). |
| `progress_spoken` | string **or string[]** | The holding line itself. Asserts no facts, so it is safe to speak verbatim. A **list** stores several variants — one is picked per lookup, because a caller who hears the identical line on every wait clocks it as fake. (The cockpit's textarea edits both forms: one line saves the string, several lines save the variant list.) |
| `progress_spoken_by_language` | object | Per-language holding lines (a JSON-object textarea on the cockpit's Speech section), keyed on the **primary** subtag like `voice_by_language` (`{"ar": "لحظة، أتأكد لك الحين."}`). Values take the same string-or-list-of-variants shape as `progress_spoken`. Without an entry the single `progress_spoken` value is spoken as written — which is how an English "still working on that" landed mid-call in an Arabic session. Since the capability-separation change there is also a **built-in per-language holding line**, used only when nobody has expressed an opinion (no entry here AND `progress_spoken` still at its shipped default): an untouched Arabic tenant no longer hears the English default, while a tenant who wrote one custom line keeps that line in every language. |
| `interrupt_confirm_window_sec` | number | False-barge-in recovery. An `interrupted` is only the provider's speech-START detection — background noise trips it too — and it used to silently drop the pending delegated answer (the caller asked, waited through the filler, heard nothing). A real barge-in confirms itself within a beat (the overriding utterance commits, or becomes a new question); an interrupt still unconfirmed after this window is treated as noise and the answer is spoken after all. **Defaults to 2.** `0` restores the old behaviour (any interrupt silences the answer) — the rollback lever. Recoveries are counted per tool span as `waj.voice.live.recovered_false_interrupt`, which is the false-barge-in rate to tune VAD against. |
| `failure_spoken` | string | Spoken when a delegated turn errors, times out, or produces nothing, and when an answer's substance is visual. Asserts no facts, same posture as `progress_spoken`. Blank falls back to built-in per-language wording. |
| `failure_spoken_by_language` / `on_screen_spoken` / `on_screen_spoken_by_language` | string / object | Per-language failure wording and the on-screen pointer line — all three on the cockpit's Speech section (they had no portal field until the voice screen existed). |
| `say_exactly_instruction` | string | The instruction wrapped around an answer the **endpoint** delivers itself — relay custody, stall/blank-call recovery, the answer watchdog's re-delivery. It reaches the model as a caller-side text turn, so it is a **prompt**: the model obeys it or improvises around it, and it obeys best in the language of the call. Must contain `{spoken}`, which is substituted with the answer (substituted, not appended — Arabic reads right to left and the instruction does not always belong in front). Blank falls back to the built-in per-language wording (EN/AR). A value that cannot carry the answer — no placeholder, an unbalanced brace, an unknown field — falls back with a WARNING rather than delivering an answer with the words missing. There is deliberately no "blank = disabled": a bare answer with no say-exactly instruction is the model free to paraphrase a figure. |
| `say_exactly_instruction_by_language` | object | Primary-subtag → instruction (`{"ar": "قل هذا للمتصل حرفياً، ولا تقل شيئاً غيره: {spoken}"}`). Same keying and same `{spoken}` requirement. |
| `delegate_timeout_sec` | number | Hard cap on one delegated agent turn. **Defaults to 30** — raised from 15 after a multi-tool turn (branch performance over a period) hit the cap and the caller was told "there was a system problem" for what was only slowness. |
| `relay_max_turns` / `relay_max_sec` / `relay_quiet_grace_turns` | numbers | Relay-custody ceilings (**8** / **180** / **2**), previously endpoint constants. Custody force-closes at either ceiling so a wedged flow can never hold a call hostage; the grace is how many consecutive flow-quiet delegated turns end custody normally (the read-back beat legitimately calls no tool, so one quiet turn is expected). |
| `max_session_sec` | number | Hard ceiling on one duplex session. **Defaults to 900.** Bounds exposure to a socket nobody is listening to; the provider caps session length anyway (~30 min on Azure). |
| `handshake_charge_sec` | number | Audio-seconds charged the moment the provider session comes up (**30**), so an abandoned socket still costs something; the periodic charge below and the teardown charge then book the rest of the real duration. |
| `budget_charge_interval_sec` | number | How often a RUNNING session books its elapsed-but-uncharged seconds to the daily tally (**60**). Without it a session contributed only the handshake advance until teardown, so concurrent in-flight sessions could accrue hours against a 1-hour cap before any of it was recorded, and a replica killed mid-session booked 30s for a 15-minute call. `<= 0` disables — the legacy two-charge scheme; positive values are floored at 5s server-side. |
| `max_concurrent_sessions` / `max_concurrent_per_ip` | numbers | Per-identity (**3**) and per-IP (**8**) concurrency caps, per engine replica and best-effort — the persisted per-tenant daily budget (Duplex-voice budgets card, below) is the hard spend control. Only a signature-verified bearer narrows the identity key, so the IP cap is what actually bounds an anonymous abuser. |
| `backchannel_tokens` | string[] | EXTRA whole-utterance acknowledgement tokens ("yes", "أيوه") merged into the built-in EN+AR set — a nod must never supersede the in-flight answer the caller is waiting for. The built-ins also cover **availability probes** ("هلو؟", "سامعني؟", "hello?"): a caller checking the line ten seconds into a slow lookup used to silently drop the very answer they were waiting for (live 2026-08-03). Add entries when a tenant serves a language the built-ins don't cover, or to make a vocative probe of the tenant's assistant name ("يا" is built in, the name is not — e.g. add `جوهرة`). Matching is case/space/tashkeel-insensitive like the built-ins. One per line in the portal. |

**Resolution precedence** (as of 2026-08-17): `LiveVoiceConfig` dataclass
defaults → profile `voice_config`, read **flat**. That is the entire chain.
There is **no channel layer** — `agent_channels.config.voice.live` is read by
nothing (no seeder wrote it, no screen labelled it, zero rows used it on any
stack), and `resolve_live_config`'s `channel` kwarg is accepted and ignored. A
profile row still carrying the old `{"live": {…}}` shape resolves from that
nested block ALONE and logs a warning — a one-release shim for an unmigrated
branch DB or a pre-migration snapshot restore, dropped once every environment
has run the `unify_voice_config` migration. See
[Architecture → Duplex voice edge](./00-architecture.md#duplex-voice-edge)
and [Data Model → `voice_config`](./data-model.md#voice_config-keys-duplex-voice).

**Adding duplex voice to a new agent kind** is one code-side registration
(`runtime/agents/voice_registry.py::register_voice_agent_builder` — the
endpoint itself is kind-agnostic); a new **tenant/profile of an
already-registered kind** is pure portal config: fork a profile, pin its
model and persona prompt, fill its voice cockpit, and point the parent's
`agent_profile_slug` at it.

**Where a served specialist's config lives.** Once a parent points
`agent_profile_slug` at a specialist (e.g. `support_default` → `support_voice`),
the specialist itself stores **no** `voice_config.*` — the parent holds all
of it. In the fieldset era, opening the specialist on the Agents screen showed
a voice card blank with Enabled unchecked — accurate, and reading exactly like
"this voice agent has no persona and can't be configured" (it was reported
that way against a demo stack).

The **voice cockpit** makes that relationship the page's structure rather than
a notice: a "Which row stores what" panel renders unconditionally, naming the
row that holds `voice_config` and the row that answers the call, and each form
posts to the row the runtime reads — the voice form to the **owner's** id, the
brain panel to the **specialist's** — so opening the specialist and saving
writes where the config actually lives. The pair is resolved from the database
by slug, not from the Agents rail's already-filtered profile list — the old
Agents-screen notice used to be derived from that list, so an environment or
build-scope filter that hid the parent also hid the explanation and left the
specialist's blank card looking authoritative. That resolution reads every
profile in the environment rather than only what the rail is showing.

A specialist's `voice_config` is in fact **never read at runtime**:
`endpoints/voice_live.py::_live_config_for` — the only voice-config resolver
left, now that the turn-based `/voice/*` router and its `_voice_config_for` are
retired — resolves it from `AgentProfileLoader.load_active(agent_kind)`, and a
`status='specialist'` row can never be returned by `load_active`. The same is
true of the two things the specialist looks like it should own: the **persona
prompt** (`persona_prompt_slug`) and the **realtime model row** are both read
off the parent, and both are now frozen into the PARENT's promote snapshot, so
promoting the specialist governs neither.

> **A live `agent_models` edit no longer reaches prod.** `runtime/compiler.py`
> freezes the resolved realtime row into `snapshot["voice_model"]` and
> `hydrate_live_model` prefers it, so a snapshot-served profile reads
> `endpoint`, `voice_type`, `turn_detection`, `api_version` and `auth_env_var`
> from the frozen copy — not from `agent_models`. Bringing those under
> promote/rollback is the point; the cost is that the established incident move
> of **editing the model row live** (repoint the endpoint, flip `voice_type`,
> rename `auth_env_var` — how the #109-class fixes shipped in minutes) is now
> **inert for prod**. After editing a realtime `agent_models` row, **re-promote
> the parent profile**. Dev/staging still fall back to a live read, so the edit
> appears to work there — that divergence is the trap. And `make drift-check`
> reads the live `agent_models` table, so it can report **ok** on a value prod
> is not serving: it verifies the row, not the snapshot.

That includes the persona's **version
pin**: it lives in the PARENT's `prompt_version_ids[<persona slug>]` — which is
exactly where the persona editor's Save writes it, so an edit on the parent is
served — and the specialist's `prompt_version_ids` carries only its brain
prompt. Until 2026-08-17 the pin sat on the specialist instead, which made an
operator's persona save on the parent a silent no-op (the portal showed the new
version, the caller kept hearing the specialist's pinned one).
`20260817121000_pin_voice_persona_versions` backfills the pin on the parent —
on the live row AND inside every promoted snapshot, which is what prod and any
`served_version_id`-pinned profile actually resolve from — and a pin that still
sits on the specialist is honoured with a warning naming both profiles, but only
while the parent carries no entry; once it does, the parent wins outright.
So a voice edit belongs on the
parent, and the portal now enforces the targeting instead of asking the
operator to remember it: the Agents screen carries no voice fields at all —
its Duplex voice card is a link into the cockpit, resolving the **owner**
when the viewed profile is a served specialist — and the cockpit's own forms
post per row (voice → owner, brain → specialist), so there is no screen left
on which a voice edit can land on the wrong row. The persona prompt itself is
a separate `agent_prompts` row for the kind (e.g.
`<kind>/voice_persona`), and since the pin moved to the parent it is
editable from two places that both pin correctly: the **parent's** Prompt
card on the Agents screen, and the **cockpit's persona editor**, whose hidden
`profile_id` is always the owner's from either entry point. On the
specialist's own Prompt card that editor renders **read-only**, with a link
to the owner — a save there would create a version and repoint the
specialist's own `prompt_version_ids[slug]`, which the parent's pin now
outranks: a silent no-op that no warning reports, because
`resolve_persona_prompt` returns the parent's entry before it ever looks at
the specialist. Every other non-system prompt on the specialist stays
editable as before.

##### Duplex-voice budgets (Settings › Voice budgets)

Per-tenant **daily duplex-voice allowances** in audio seconds
(`ai_voice_budgets`) live on their own page, **Settings › Voice budgets**
(`GET /admin/voice-budgets` · `voice_budgets.html` · `router.py:voice_budgets_screen()`),
since 2026-10-11. From 2026-08-18 they were a card repeated on every realtime
model's detail page (and `/admin/voice-budgets` 307'd to the Models tab); that
card is gone — a realtime model page now carries one line linking here, a chat
model page nothing. The URL is a page again, so old links and bookmarks land on
it directly. The write routes did not move: `POST /admin/voice-budgets` and
`POST /admin/voice-budgets/{business_id}/delete` still take a `next=` hop
(validated same-site — an unvalidated `next` on a superuser form is an open
redirect); with no or a hostile `next` they return to `/admin/voice-budgets`.

**The allowance is per tenant, not per model, and the page says so.** Rows are
keyed by `business_id`; a tenant's cap covers all of its duplex voice whichever
realtime model the call runs on — moving a tenant between realtime rows neither
changes nor resets its allowance.

The page shows each allowance (minutes, with seconds) next to today's burn, plus tenants burning the
default with no explicit row. No row = the engine default
(`VOICE_LIVE_DAILY_SECONDS`, 3600s/day) — read **at import** in
`voice/live/budget.py`, so changing the env var needs an engine restart, while
the per-tenant rows are read per call and apply as soon as you save. The
business id field accepts any tenant id — non-UUIDs are mapped
to the same uuid5-derived row the voice edge charges
(`endpoints/voice_live.py::_budget_key`), so what you paste is what gets
capped. Reservation is atomic where the DB carries
`ai_reserve_voice_seconds` (migration `20260802140000`); accounting still
**fails open** on DB errors — the cap is a spend control, not an
availability risk. Mirrored by the admin MCP `voice_budgets` tool.

###### Persona and other non-system prompts

Below the System Prompt editor, the Agents screen renders one collapsed
editor per **other** `agent_prompts` row for the active `agent_kind` — most
notably the duplex-voice persona (`persona_prompt_slug`, e.g.
`voice_persona`), plus any title/suggestions prompts. Each posts to
the same generic `POST /admin/prompts/{prompt_id}/versions` endpoint the
System Prompt editor already used, with the same hidden `profile_id` /
`prompt_key` inputs — saving creates a new `agent_prompt_versions` row **and**
repoints this profile's `prompt_version_ids[slug]` pin at it, so the edit is
served immediately on this draft (the runtime serves the PINNED version,
never "latest"). Before this, editing a persona prompt required a hand-built
`curl`/MCP call to the same endpoint; the portal now exposes it directly.
Same route, so the same `core.prompt_validator` gate described above for the
System Prompt editor applies here too — including the `ask_agent`-imperative
check, which matters for a persona specifically: a realtime model given a
descriptive ("goes to the `ask_agent` tool") rather than imperative ("call the
`ask_agent` tool") instruction can speak the tool call instead of making it.

**One exception:** the duplex-voice persona is rendered **read-only** when the
profile you are viewing is the voice *specialist* another profile serves voice
through (the same "configured elsewhere" relationship the Voice card's notice
describes). The save posts the profile being viewed, so it would repoint the
specialist's pin — and `resolve_persona_prompt` returns the PARENT's entry
unconditionally, so the edit would never be spoken and nothing would say so.
The card links to the parent, where the same editor is live.

###### Which persona version do callers actually hear?

The persona editor always shows the **latest** saved text. On a
snapshot-served profile that is not what a caller hears, so a
**served-vs-latest banner** sits on the persona editor (and again on the Duplex
voice card, since the two are far apart on a long page). It carries a
`callers hear vN` chip — green when that is the newest version, amber when it
is not, alongside `latest saved: vM` — and it is repeated in the collapsed
`<details>` summary so you do not have to expand anything to see a divergence.
The persona row auto-expands when one exists.

Under the chips it names **which of four tiers resolved it**, because they do
not behave the same way (`admin/_persona_versions.py`, mirroring
`endpoints/voice_live.py::resolve_persona_prompt` branch for branch — if the two
ever disagree the banner is a bug, not a hint):

| Tier | What the banner says | Governance |
|---|---|---|
| Promoted snapshot | "Resolved from *<parent>*'s promoted snapshot (profile vN)" | The text was frozen at promote; a save reaches callers only after the **parent** is promoted again |
| Pin on the parent | "Resolved from the pin on *<parent>* (`prompt_version_ids.<slug>`) on the live profile row" | Promote/rollback govern it, and this environment serves the live row — a save is live on the next connect |
| Legacy pin on the specialist | "Resolved from a legacy pin on the specialist *<name>*" | Honoured, but the wrong owner: every connect logs a warning, and a parent-side persona save loses to it until the pin moves |
| Nothing pins it | "Nothing pins this persona" | **Ungoverned** — promote and rollback do not control it and every save ships immediately, in every environment |

A second line answers the only question an operator actually has —
**"is my save live here?"** — and it is derived from *which row the serving tier
read*, never from the environment's name. That distinction is load-bearing in
both directions: an unpinned persona (tier 4) is live-on-save even in **prod**,
while a profile with a `served_version_id` pin is snapshot-served even in
**staging**. When served ≠ latest the banner closes with the one action that
fixes it — promote the parent, save on the parent, or repoint the pin,
depending on the tier — rather than only reporting the gap. If the parent you
are viewing is a draft or archived row the banner says so too: the voice edge
loads the *active* profile, so those numbers describe a config nobody is on.

The realtime model is registered like any other `agent_models` row (Models
tab, above), with `extra_config.realtime: true`. Two providers ship:

| Provider | Example row | What it is |
|---|---|---|
| `google_genai` | `Gemini 3.1 Flash Live (duplex voice)` / `gemini-3.1-flash-live-preview` | Half-cascade Gemini Live (native audio in, TTS out). Seeded on every build. |
| `azure_voice_live` | a cascaded row (e.g. `gpt-4.1-mini`) | Voice Live **cascaded** — Azure STT → chat model → Azure neural TTS. |
| `azure_voice_live` | a native row (e.g. `gpt-realtime`, `extra_config.voice_type: "native"`) | Voice Live **native audio** — the model generates the speech itself, which follows the prompt's dialect better than a cascaded neural voice. |

The Azure rows point at the deployment's own Azure resource, so a build with no
plugin does not seed them; add them on the Models tab (or a deployment's plugin
seeds them).

Repointing `voice_config.model_id` between rows is the per-tenant
provider/mode switch and the rollback lever — a config edit, never a deploy.
Cascaded vs. native is not just a price difference: the two rows carry
different `extra_config` (`voice_type`, `transcription_model`,
`turn_detection`, and their own `auth_env_var`), and a barge-in guard set that
is correct on one is wrong on the other — see [Architecture → Duplex voice
edge](./00-architecture.md#duplex-voice-edge) and [Data Model →
`azure_voice_live` row shape](./data-model.md#agent_models) before editing
either row's `extra_config`. The model catalog's seeder is
**insert-only and idempotent on `(name, environment)`**: editing a row's
`model_string` later (a provider migration, a new preview id) is a portal
edit or a manual `DELETE` + reseed against the AI project, never a re-run of
the seeder.

**Spend control** — every connected session reserves seconds against a daily
per-tenant budget (`ai_voice_usage_daily`, `VOICE_LIVE_DAILY_SECONDS` env,
default 3600s/day) before it's allowed to proceed; a tenant over budget gets
`CLOSE_RATE_LIMITED` at handshake. The budget key is the RESOLVED business id
(the `businessId` query param, or a tenant id a deployment's portal resolves
from its own signed session) — uuid5-derived when that id isn't already a
valid UUID. An anonymous caller with no resolvable business id (the `/agents`
demo, for example) gets a budget key derived from its IP/rate-key instead. See
[Data Model → `ai_voice_usage_daily`](./data-model.md#ai_voice_usage_daily)
for the exact derivation. The per-replica **concurrency** cap (`_MAX_CONCURRENT_PER_TENANT`) is
deliberately the opposite: it stays keyed by the caller's IP/rate-key even
for a signed-in tenant session, because a demo portal is often one shared
login — scoping concurrency to the tenant id would collapse every
simultaneous visitor onto one shared cap. See
[Data Model → `ai_voice_usage_daily`](./data-model.md#ai_voice_usage_daily).

**Backing tables** — `agent_profiles`, `agent_prompts`, `agent_prompt_versions`,
`agent_tools`, `agent_data_sources`, `agent_mcp_servers`, `agent_models`, `agent_e2e_runs`,
`ai_voice_usage_daily`.

**Backing tables** — `agent_profiles` (incl. `served_version_id`), `agent_profile_versions`,
`agent_prompts`, `agent_prompt_versions`,
`agent_tools`, `agent_data_sources`, `agent_mcp_servers`, `agent_models`, `agent_e2e_runs`.

### Sub-views & popups

**Fork** — `POST /admin/agents/{profile_id}/fork`
Clones the profile into a new **staging draft** (new id, ` (fork)` suffix, unique
slug), copies all wiring, invalidates caches, audits, and lands you on the fork.
Staging is the live, editable environment, so the draft is immediately editable.
`voice_config` is copied like every other wiring column — explicitly listed in
`_FORK_COPY_FIELDS` since 2026-08-18. It was already arriving in the fork, but
only through the "a column nobody listed" verbatim fallback, which logs a
WARNING every time it fires: a warning on the happy path, on every fork of a
voice-configured profile, is how a real one gets ignored.

**Templates / New agent** — `GET /admin/templates` · `templates_gallery.html` · `templates_handlers.py`
The **Templates** section tab (and every **New agent** button, `?new=1`) — a read-only
gallery of the protected template catalog (`agent_profiles` rows with
`status='template'`) — on a scoped build, only templates of the kinds that build
runs (`visible_agent_kinds()`; a build with none says so, and `use_template` refuses a
kind the build can't run — such an agent had no runtime there and never appeared in
its agents list), grouped *Single agents* vs *Teams* (a root template plus
sub-agent templates), each card showing what it is for and what it includes
(sub-agents, knowledge sources). There is no create / edit / delete surface — the
only action is **Use template**, a two-step dialog: (1) pick a template, (2) name it,
optionally **Make it a sub-agent of** an agent chosen from a picker (Staging agents —
`use_template` only accepts a staging parent; this used to be a pasted `profile_id`),
and under Advanced an optional technical **slug**. It posts the unchanged
`POST /admin/agents/from-template` (`template_id`, `display_name`, `slug`,
`as_subagent_of`). An explicit slug is used *verbatim* (no uniqueness suffix) — set it
to give a delegation specialist a stable `ask_<slug>` tool name; a genuine
`(agent_kind, slug, environment)` collision fails loudly. On success it forks the
template (recursively for a team, rewiring the new root's sub-agent ids) into a named
**staging draft** via `admin.services.templates.use_template` (toolsets and skills
are copied with it) and lands on
`/admin/agents?profile_id=<new draft>`. An unknown `template_id` or parent re-renders
the gallery with an error (400). Cards align to the top so opening one never stretches
its row. ("Mold" is now "template" everywhere in the UI.)

**Backing table** — `agent_profiles` (`status='template'` rows are the catalog; a
"use" always inserts a fresh `status='draft'` row, never mutates the template).

**Compare two agents** — `GET /admin/agents/diff?a=…&b=…` · `agents_diff.html` · `profile_fork_handlers.py:agents_diff()`
Titled "Compare: <A> ↔ <B>". Side headers, a **Settings** table with plain labels
(Name, Model, Channel address, Parent agent… — the field name muted underneath) where
model and parent ids render by **name** (`diff_display`), and removed / added / shared
lists for knowledge sources, sub-agents and the legacy tool / server lists. **Only
differences** is on by default (hides identical rows and collapses sections with no
differences). Warns when the two are different kinds of agent. The **version diff**
(`GET /admin/agents/{id}/versions/{vid}/diff`, same template) is titled
"<agent> — vN (date) vs live" with the checkpoint's note as the subtitle.

**Prompt version diff** — `GET /admin/prompts/{prompt_id}/diff?a=<n>&b=<m>` · `prompt_diff.html` · `prompt_handlers.py:prompt_diff_view()`
A unified, line-wrapped diff of two prompt versions (green additions, red deletions,
hunk headers) with a length-delta summary.

**A/B compare (true A/B via prompt override)** — `GET /admin/prompts/{prompt_id}/compare` · `prompt_compare.html` · `prompt_handlers.py`
Pick a suite, profile, two prompt versions, and optional business/context. Launching
creates **two queued `agent_eval_runs`**, each pinned to one version via a prompt
override, runs them in the background, and redirects to the [eval run compare](./03-test.md#eval-run-compare) view.

**Prompt optimizer (LLM-as-optimizer)** — `GET /admin/prompts/{prompt_id}/optimize` · `prompt_optimize.html` · `prompt_handlers.py`
Choose an eval run to learn from and an optimizer model. The server builds a
meta-prompt from the worst-failing scenarios and **streams** a rewrite over SSE; the
result drops into a "save new version" form (`POST /admin/prompts/{id}/versions`).

### Models tab

`GET /admin/agents?tab=models` · `_tab_models.html` (framed by `agents.html`) · `router.py:build_models_context()`

A registry of LLM definitions referenced by profiles. Register an LLM once and reuse
it. Models are env-tagged (`name@env` siblings, **staging** / **production**) so each
environment can run a different temperature / model string.

**Shows** — the registered models first: **Model** (name + muted model string),
**Provider** (plain name, e.g. "Google Gemini"; a *Voice* chip on realtime rows),
**Used by** (the agents in this environment whose main / light / voice model it is,
linked), **Environment**, **Status**; searchable. Tuning knobs live on each model's
page. **New model** opens the create form (name, env, provider, model string,
**Base URL**, temperature, max tokens, top_p/top_k, thinking level / budget,
penalties, pricing, extra-config JSON) in a side drawer — it used to be an always-open form above the
list; it still posts `POST /admin/agent-models`.

**Local and OpenAI-compatible models.** Provider **Ollama** reaches a local
Ollama server through its OpenAI-compatible API (`extra_config.base_url`, else
`OLLAMA_BASE_URL`, else `http://127.0.0.1:11434/v1`). For any other compatible
server pick **OpenAI** and fill **Base URL** (stored as `extra_config.base_url`).
A server on this machine or a private network needs no API key; a hosted one
still does. Behind a Base URL the engine asks for token counts on streamed
replies (`extra_config.stream_usage: false` turns that off for a server that
rejects `stream_options`). Providers the runtime cannot build are not offered.

**Thinking controls** (Gemini reasoning depth, stored in `extra_config`, applied by
`runtime.llm_factory._google_thinking_kwargs`): **Thinking level** (`minimal` / `low` /
`medium` / `high`) for **Gemini 3+** — lower is faster/cheaper; **Thinking budget** (an
integer reasoning-token cap, `0` disables) for **Gemini 2.5**. Level wins when both are
set. Both are validated on save (an unknown level is dropped so a typo can't fail every
request). Other providers ignore them.

**First-chunk deadline** (`extra_config.first_chunk_timeout_s`, raw extra-config JSON,
Gemini only, applied by `runtime.llm_factory._guarded_google_class`): if a streamed
model call sends nothing within this many seconds, it is cancelled and retried once;
a second stall raises. Unset (the default) changes nothing. Only the FIRST chunk is
timed, so a long answer that is already streaming is never cut. Why it exists: on
2026-10-14 one agent's model calls were either fast (p50 2.6s, none between 20s and
150s over 7 days) or hung 3-4 min producing almost nothing. The SDK can't bound this:
its own timeout covers the whole stream and it never retries a timeout. Set it well
above the model's slowest healthy first chunk; `30` suited that model.

**Detail & copy to another environment** — `GET /admin/agent-models/{model_id}` · `model_detail.html` · `model_handlers.py`
The detail URL is unchanged. **Basics** (name, provider, model string, temperature, max
tokens, active) first; **Advanced** (top_p/top_k, thinking, penalties, raw extra-config
JSON) and **Pricing** fold away; one Save (`POST /admin/agent-models/{id}`). *More →
Copy to another environment…* (`…/promote` — inserts a `name@env` sibling; organisational
only, the runtime picks models by name) and the sibling rows list. On realtime rows the
per-tenant **Duplex-voice daily budgets** card is folded at the bottom.

#### Pricing control

Each model detail page includes a **Pricing** fieldset that determines how eval and
live-traffic costs are calculated:

| Mode | Behaviour |
|------|-----------|
| **Auto (tokencost)** | Cost is derived automatically from the `tokencost` library keyed on `model_string`. No manual rates needed. The library's authoritative catalogue covers all major providers; this is the recommended default. |
| **Manual** | Uses the `cost_per_1k_in` / `cost_per_1k_out` fields you enter. Falls back to this mode when `tokencost` does not recognise the `model_string`. |

Optional **tokencost model key** — if the `model_string` registered in the portal
differs from the identifier `tokencost` expects (e.g. an Azure OpenAI deployment
name, or an OpenRouter-style `<vendor>/<model>` id), enter
the canonical `tokencost` key here (`extra_config.tokencost_model`). Leave blank to
let the library match on `model_string` directly.

Both `pricing_mode` and `tokencost_model` are stored in `agent_models.extra_config`
and are resolved by `core/pricing.py` at eval-run score time. See
[Data Model → agent_models](./data-model.md#agent_models).

**Backing table** — `agent_models`.

> Legacy `GET /admin/agent-models` 307-redirects to `/admin/agents?tab=models`
> (query params carried over); the POST + detail sub-routes are unchanged.

---

## MCP & Tools

Sidebar **Connections** › Tools & integrations.

`GET /admin/tools` · `tools_list.html` + `_tab_mcp.html` · `router.py:tools_list()` / `build_integrations_context()`

One screen, one model: **every tool the agents can call is encapsulated in an MCP
server.** There is no direct HTTP-tool creation here — you author new HTTP tools
*inside* a bundle. Page actions: **＋ Create bundle** and **＋ Connect server**.

**Shows** (redesigned 2026-10-11; the old page was ~6,800px of cards plus a flat
170-row table):

- **Servers** — one compact `.table` row per `agent_mcp_servers` row: your bundles,
  external servers, then the seeder-managed built-ins. The title is a friendly name
  (`router.py:mcp_display_name()` — `__builtin__:<kind>` becomes "Tools for <agent>",
  other names are humanised) with the stored code name as muted technical text; then
  type (Built-in / Your bundle / External), tool count, **Used by** (the attached
  agents' names — template "mold" profiles excluded) and status.
- **Tool catalog** — every `agent_tools` row, **collapsed per server** that exposes
  it (`router.py:_tool_groups()`, from `agent_mcp_tools`; a trailing "Not in any
  server" group holds the rest). Search box + Agent / Access (reads vs changes data)
  filters + **Missing guidance** and **Show retired** toggles, all client-side; a
  matching filter opens the groups that still have rows. A header meter reads
  "N of M tools have usage guidance" — `usage_guidance` is the only config-driven
  steer the LLM gets about when/how to call a tool. Each row links to its
  `/admin/tools/{tool_id}` editor. It is a **catalog, not a binding** — a tool only
  reaches an LLM when an agent's MCP attachment binds it. A scoped build sees only
  the visible agent kinds' tools. Access labels are honest even before a boot sync:
  a tool in `sync_tools.WRITE_TOOL_NAMES` always shows **Changes data**.
- **Platform services** — collapsed under an Advanced disclosure: the static
  backing-service registry (`integrations_registry.py`) with env-var names and a
  **Configured / Missing env** status.

Agent pickers and filters on this and every Build screen (Tools, Channels,
Knowledge, Skills, Memory) come from one live list, `admin/agent_kinds.py`: the
curated base kinds (plus any a deployment's plugin adds) and every kind with a
live `agent_profiles` row (so `custom` and the `agents_*` demo kinds appear), minus
system agents (`*_title`, `*_suggestions`, and any the plugin names), labelled with
the agent's default profile name. A scoped build keeps its scoped lists.

### Sub-views & popups

**MCP detail** — `GET /admin/agent-mcp-servers/{id}` · `mcp_server_detail.html` · `mcp_handlers.py`
Back link to Tools & integrations, **Used by** (the attached agents, linked), and one
**Details** `dl.kv` rail (type, status, tools, agents, address, code name, id).
Built-ins: a read-only tools table (friendly name + code name + description).
External: a **Test connection** button (the same `/admin/mcp/discover` call, with the
saved address/token) above the settings form. Settings: name, address, access token
(the *name* of the engine secret), tools to allow, usage guidance, active;
transport + context-fields JSON under **Advanced**. Bundles also get the
search + selected-first member picker (below) and **＋ Add HTTP tool**. The danger
zone (last) names the agents a delete detaches. `POST .../members` reconciles
membership (checkbox DOM order = position); `POST .../delete` cascades.

**Add HTTP tool (inside an internal MCP)** — `GET/POST /admin/agent-mcp-servers/{id}/add-tool` · `tool_new.html` · `mcp_manage_handlers.py`
The HTTP-tool builder scoped to a bundle: validated python name, description, agent
kind(s), endpoint, method, auth env var, timeout, and a JSON-schema editor. On save it
inserts the `agent_tools` (`kind='http'`, `source_file='__portal__'`) row **and** an
`agent_mcp_tools` membership row (appended last), then returns to the MCP. Refused
server-side for built-in / external MCPs. Validation failures re-render with input
preserved. The legacy `GET/POST /admin/tools/new` route is retired — it now redirects
to this screen, so there is no standalone HTTP-tool creation form.

**Tool detail** — `GET /admin/tools/{tool_id}` · `tool_detail.html` · `tool_handlers.py`
Layout (2026-10-11): back link to the server that exposes the tool, a "What the agent
sees" form with full-width fields, one **Details** `dl.kv` rail (access, type,
status, agent, servers, code name, id) instead of four single-value cards, the
argument schema + source viewer collapsed under **Developer details**, category
under Advanced, and "Also available to" as one checkbox per agent (the old
one-per-line textarea body is still accepted by `POST /admin/tools/{id}`). Reached from the tool-name links in an MCP detail page's member tables, from the
All-tools table, from an Agents-screen bound-tool chip, and from ⌘K global
search Tool results. HTTP tools: editable endpoint / method / auth /
timeout / JSON-schema (`POST /admin/tools/{id}/edit`) + soft-delete. Python tools:
read-only inferred schema + path-safe source viewer. All tools: editable display name,
description (markdown), category, scope, additional agent kinds, deprecated flag.
**This `description` is what the generated `{available_tools}` prompt placeholder
renders**, so editing it here changes what the agent tells the model. A separate
**Usage guidance** textarea (`usage_guidance`) holds free text for field semantics,
metric definitions, or gotchas (e.g. *"Active pipeline = stage NOT IN (won, lost).
'New this week' filters created_at."*) — when the tool is bound for a request, it
renders as an indented line under the tool's entry in the `{available_tools}` catalog.
See [Architecture → Generated tool
catalog](./00-architecture.md#generated-tool-catalog-available_tools-phase-2).

**Dry-run** — `POST /admin/tools/dry-run`
Validate sample arguments against the schema and call the endpoint, returning the
request (auth redacted), response (size-capped), latency and error class — without
saving.

**Create a bundle** — `GET/POST /admin/mcp/create` · `mcp_create.html` + `_mcp_member_picker.html` · `mcp_manage_handlers.py`
Name + "When should the agent use it?" (`usage_guidance`) + a picker over
non-deprecated `agent_tools`: search + agent filter over a one-line-per-tool pool,
and ticked tools move into a **Selected** list (in tick order) that sits first in the
DOM — so the saved position is the selected order. `context_fields` JSON is under
Advanced. Inserts a `source='bundle'` row and one `agent_mcp_tools` row per checked
tool (in order). Posted ids validated against the live non-deprecated set; the
`__builtin__:` prefix is reserved.

**Connect a server (external)** — `GET/POST /admin/mcp/connect` · `mcp_connect.html` + `static/mcp-discover.js` · `mcp_manage_handlers.py`
Three steps in one form: ① name, server address, access token (the *name* of the
engine secret holding it, explained in plain words, with a link to Settings ›
Environment variables on staff builds); ② **Discover tools** (`POST
/admin/mcp/discover`, SSRF-guarded, token never echoed) — results render as a
checkbox list that fills "Tools to allow", failures as a plain sentence ("That
address is private or local…") with the raw class/message underneath; ③ tools to
allow + **Connect server**. Transport and `context_fields` sit under Advanced. POST
contract unchanged. Inserts a `source='external'` row; duplicate name re-renders,
never 500s.

**Backing tables** — `agent_tools`, `agent_mcp_servers`, `agent_mcp_tools`.

---

## Channels

Sidebar **Connections** › Channels.

`GET /admin/channels` · `channels_list.html` · `channel_handlers.py:channels_list()`

A channel is a request **source** (a public API client, a website widget, a partner
portal, …). Channels drive CORS and gate which agent kinds a caller may reach. The
engine knows three channel types (`core/channel_kinds.py`: Public API, Website
widget, Partner portal / other); a deployment's plugin may add its own types and
default channels, which `seed_channels.py` inserts at boot (insert-only, on slug).
Seeded channels carry `allowed_agent_kinds = []` (no restriction) so seeding never
gates live traffic; tighten per channel from this screen.

**Shows** — a **CORS-dirty banner** (appears when channels changed since boot, since
the CORS allow-list is snapshotted at startup; "Mark as fixed" dismisses it), a type
filter, **＋ New channel**, and a table: name (code name muted), type in plain words
(`channel_handlers.CHANNEL_KIND_LABELS`), **Answered by** (the default profile's
name), sign-in, website count, status.

### Sub-views & popups

**Channel detail / new** — `GET /admin/channels/{id}` or `/admin/channels/new` · `channel_detail.html` · `channel_handlers.py`
Sections with plain labels: **Basics** (name, short code name = `slug`, type —
a type that belongs to one business (a plugin's tenant type) reveals a **business
picker** — description), **Answered by**
(`default_profile_id`, plus "Agents allowed on this channel" = `allowed_agent_kinds`
from the shared live agent list), **Access** (allowed websites = `allowed_origins`,
require sign-in = `auth_required`, requests per minute = `rate_limit_rpm`, active),
and the free-form `config` JSON under **Advanced**; a Details rail on edit. POST
field names unchanged. A danger-zone soft-delete (`…/delete`) sets `is_active=false`. Every
mutation busts the channel cache and marks CORS dirty.

**`allowed_origins` also gates the duplex voice WS handshake** (`<portal_prefix>/voice/live`),
opt-in and OFF by default: an `Origin` that matches no channel is only rejected once
*some* channel has a non-empty `allowed_origins` here — until then it's allowed through
(with a warning logged either way). See
[Architecture → Duplex voice edge](./00-architecture.md#duplex-voice-edge)'s "Origin
gate" bullet — in particular, if a portal's session cookie is ever switched to
`SameSite=None` (e.g. to allow iframing), populate `allowed_origins` for that portal's
channel **first**, since that's what stops a cross-site page from opening a duplex
voice session as a logged-in user.

**Backing table** — `agent_channels`.

---

## Skills (`/admin/skills`)

Sidebar **Knowledge** › Skills.

**What it shows.** Every skill — a named, versioned instruction bundle
(playbook) agents load on demand. Platform skills (no business) attach to
profiles; tenant skills (business-scoped) resolve dynamically for that
business's requests. Columns: name, slug, agent kind, scope
(platform/tenant), always-on flag, status.

**Key actions.** Create a skill (slug + description + first version body);
edit metadata (display name, description, always-on, linked knowledge
bases); publish a new version; bundle reference files on a version; archive.

The create form uses stacked labels (2026-10-11 — the old inline labels flowed
*after* their inputs, so each label sat beside the wrong field) and the shared live
agent list.

**Sub-views & popups.** Skill detail (`/admin/skills/{id}`): metadata form,
version history, new-version form, reference-file manager. Attachments are
edited on the Agents screen (per-profile "Skills" panel).

**Backing tables.** `agent_skills`, `agent_skill_versions`,
`agent_skill_resources`, `agent_profile_skills`.

**Runtime behaviour.** The system prompt carries only a skill catalog
(slug + description). The model fetches full bodies via the synthetic
`read_skill` tool. `always_on` skills are inlined every turn (cap 10).
Platform skills freeze into the prod profile snapshot at promote; tenant
skills always resolve live (60s cache). Description/display-name may not
contain `{` or `}`.

## Knowledge Base

Sidebar **Knowledge** › Knowledge bases.

`GET /admin/kb` · `kb.html` · `router.py:kb_screen()`

Data sources the agents read from — pgvector RAG, Supabase tables, HTTP, GitBook, and
document imports.

**Shows** — a collapsed "New knowledge base" form (name, source type in plain words —
`router.py:KB_KIND_LABELS` — agent; sync schedule + config JSON under Advanced), an
agent filter (shared live list), and a table: name, agent ("Not attached to any agent"
flagged), source type, documents, last updated, status. Status is per source type
(`router.py:_attach_kb_document_stats()`): uploaded files → **Ready** / **No
documents yet** from `ai_kb_pages`; synced sources (GitBook / Drive / URL) →
**Synced** / **Never synced**; a database table → **Live table**. "Never refreshed"
no longer shows on upload KBs, where refreshing means nothing.

### Sub-views & popups

**Source detail** — `GET /admin/kb/{source_id}` · `kb_detail.html` · `kb_handlers.py`
Order (2026-10-11): ① **Documents** — the upload drop zone at the top (files, title,
kind of document, language) and the list of documents already in the KB below it;
non-pgvector kinds get a **Source** explanation instead (synced: last sync or "has not
been synced yet"); ② **Settings** — name, agent, "Also used by" (one checkbox per
agent; `_additional_kinds_form` marker lets "none ticked" clear it, the old
newline-textarea shape still parses), sync schedule / SLA for synced kinds only,
active; source type, config JSON and the pgvector `information_schema` inspector
under Advanced; ③ **Danger zone** last. One **Details** rail (status, type, agent,
documents, passages, last sync, id).

**Documents list** — for shared source-scoped pgvector KBs (i.e. sources whose
`config.table == "ai_kb_embeddings"` / `config.pages_table == "ai_kb_pages"`), the
detail page renders a **Pages** card:
- A summary header: **N pages · M chunks** (total ingested for this source).
- A table with columns: **Title · Type · Language · Chunks · Source · Added**.

Lets an operator verify what is actually indexed (for example: "93 pages · 738 chunks"
for a product docs KB). Sources that use a legacy or custom table — or non-pgvector
kinds — show nothing in this card.

**Async ingestion** — `POST /admin/kb/{source_id}/upload`
Uploads stage parent page rows, then a background worker chunks (≈512 tokens, 64
overlap), embeds via Gemini, and inserts pgvector rows. The detail page polls
`/admin/jobs/{job_id}.json` for live progress (files done, chunk count, embed time)
and reloads on completion. Per-file errors don't fail the whole job. (5MB/file cap.)
Accepted file types: `.md`, `.txt`, `.html`, `.pdf`, `.docx` (`.docx` parsed via
`python-docx`; requires the package to be installed in the runtime environment).

**Segmentation & bulk ingestion** — documentation lives in the shared,
source-scoped pgvector tables (`ai_kb_pages` / `ai_kb_embeddings`, in the **AI/config
DB** — the same tables any dashboard-created pgvector KB uses), segmented by
`source_id`. A seeded pgvector source carries a matching `config.source_tag`. A
build with no plugin seeds no knowledge sources (the `/agents` demo seeds its own);
a deployment's plugin may seed more (`data_source_specs()`). Because they use the
shared model, every such corpus is **portal-manageable**: an operator can upload or
delete individual documents from each source's detail page exactly like any other
pgvector KB. Beyond the upload panel, bulk (re)population runs via
`scripts/import_markdown_docs.py` (authored markdown under `scripts/kb_content/`),
which targets `AI_SUPABASE_*`.

**Shared source-scoped tables (dashboard-created pgvector KBs)** — when you create a
new pgvector KB from the dashboard, the config defaults to
`{"table": "ai_kb_embeddings", "pages_table": "ai_kb_pages"}`. These two tables live
in the **AI/config DB** alongside `agent_data_sources` and require **no per-KB
migration**. Ingestion stamps `source_id` into every page and embedding row so all
content is isolated by source. Retrieval uses the `match_kb_embeddings` RPC, scoped to
the requesting agent's `data_source_ids`. Legacy or custom KBs may still point their
config at a dedicated table; only sources whose `config.table == "ai_kb_embeddings"`
use the shared model.

**Delete** — `POST /admin/kb/{source_id}/delete` · `kb_handlers.py:kb_delete()`
A danger-zone **Delete** button on the source detail page (requires superuser). Steps:
1. Loads the source row for an audit "before" snapshot.
2. Removes `source_id` from `agent_profiles.data_source_ids` on every profile that
   references it, so no live agent is left pointing at a deleted KB.
3. Deletes the `agent_data_sources` row; the FK cascade auto-removes all child
   `ai_kb_pages` and `ai_kb_embeddings` rows.
4. Writes an audit `record_change` (`entity_type = "agent_data_sources"`,
   `action = "delete"`).
5. Redirects to `/admin/kb`. Idempotent — a re-POST after the row is already gone
   skips straight to the redirect without a 404.

**Backing tables** — `agent_data_sources`, `ai_kb_pages`, `ai_kb_embeddings` (shared
source-scoped store), the configured pages + embeddings tables for legacy KBs,
`admin_jobs` (ingestion progress).

---

## Memory

Sidebar **Knowledge** › Memory.

`GET /admin/memory` · `GET /admin/memory?agent_kind=<kind>` · `memory.html` · `memory_handlers.py` + `screens/memory.py`

Per-user long-term memory and rolling conversation summaries, now generic across all
agent kinds. An **agent-kind picker** at the top of the screen selects which agent
you are managing; the selection is reflected in the `?agent_kind=<kind>` query param
and defaults to the build's default kind (`custom` on a build with no plugin; a
plugin may name another, `agent_catalog.default_kind()`).

Two tabs (**Memories**, **Inspector**) plus the per-agent **memory settings** operate
on the **selected agent kind** — so an operator can view/edit/pin/delete user
memories, tune extraction and retrieval config, and inspect per-session memory for
`custom`, an `agents_*` demo kind, or any registered agent kind.

**What memory saves and how it summarises are prompts, per agent.** They are not on
this screen: open the agent in **Agents** and edit its `memory_extraction` /
`memory_summary` prompt (an agent without one uses the shared defaults; a
deployment's plugin may seed them for its own agents). Saving pins the new version on that profile, like the
system prompt. Keep the placeholders exactly as they are, or memory ignores the edit
and uses the default.

Ships **dark** (`memory_config.enabled: false` by default) and is enabled
per-environment from the agent's memory settings.

**Memory settings moved to the agent (2026-10-11).** `GET /admin/agents/memory?agent_kind=<kind>`
(`agent_memory.html`, `memory_handlers.py:agent_memory_settings()`) — linked from the
Agents page chip strip ("Memory settings →") and from the Memory page header. The old
`/admin/memory?tab=config` 307s there; the form still POSTs to
`/admin/memory/config`, which now returns to the settings page. The master switch
("Remember facts about each person") sits first; while it is off the dependent groups
are greyed and `inert` (not `disabled`, so saving keeps their values). The agent
picker is the shared live list without system agents; a deep link to a system kind
still resolves.

Tabs:

### Memories tab

**Shows** — a user lookup form (search by UUID or email); once a user is found, a table
of their memory facts (`content`, `importance`, `pinned`, `source`, `status`,
`last_used_at`) filterable by status / source / pinned.

**Key actions:**
- **Edit** a memory's text in-line (re-embeds on save).
- **Delete** — soft-archive (`status = 'archived'`); the row is kept for audit/inspector purposes.
- **Pin / Unpin** — pinned memories are always injected into the prompt regardless of similarity.
- **Add a memory manually** — a disclosure form that inserts a new row with `source = 'manual'` and embeds it immediately so it is retrievable.

Every mutation is recorded in `agent_audit_log`.

### Memory settings (was the Config tab)

**Shows** — a labeled-field editor for the **selected agent kind's** active profile
`memory_config` JSONB (not raw JSON). Fields:

| Field | Description |
|-------|-------------|
| Max messages (window) | Size of the short-term conversation window every agent replays as verbatim history. Independent of "Memory enabled" — an agent always has a window even with long-term memory off. Replaces the old `AGENT_HISTORY_MSG_*` env defaults, so a window can be retuned from the portal instead of a redeploy. |
| Max message chars (window) | Per-message truncation cap applied when replaying the window. |
| Suggestion context messages | How many recent messages are handed to the follow-up-suggestion sub-agent so its suggestions reference the real conversation (prevents `[business name]`-style placeholders). |
| Memory enabled | Master kill-switch — toggles the whole long-term-memory feature per-environment (does **not** affect the window above). |
| Importance threshold | Drop extracted facts below this score (0–1). |
| Max facts per turn | Cap on facts extracted from a single turn. |
| Dedup merge threshold | Cosine similarity above which a new fact is merged into the nearest existing one. |
| Retrieval top-K | Number of memories retrieved per turn. |
| Min similarity | Minimum cosine similarity for a memory to be retrieved. |
| Always include pinned | Whether pinned memories are always injected even below the similarity threshold. |
| Refresh every N turns | How many turns must elapse before the rolling summary is regenerated. |
| Keep recent verbatim (turns) | Turns that stay in the prompt as verbatim messages rather than being folded into the summary. |
| Retention days | Days before memories are eligible for cleanup (blank = keep forever; Phase 2). |

Saving writes `agent_profiles.memory_config` for the selected agent kind **and active
env** — the read and the write are both scoped to the selected environment's profile
row (staging and prod share one config DB, so an unscoped lookup could otherwise land
on the wrong env's row). Staging changes are live immediately; production reads the
promoted snapshot — promote from Environments after staging validation.

### Inspector tab

**Shows** — a session lookup form (UUID); once resolved, the session's `running_summary`
(how many messages it covers, last updated) and a per-assistant-turn breakdown of which
memories were retrieved (resolved from `ai_chat_messages.metadata.memory.retrieved_memory_ids`).

Used to verify what the agent actually saw in its context during a given session.

### Sub-views & popups

- **"Add a memory manually"** disclosure — inline form on the Memories tab.

### Backing tables

`ai_user_memory`, `ai_chat_sessions` (`running_summary`, `summary_message_count`,
`summary_updated_at`), `ai_chat_messages.metadata.memory` (provenance),
`agent_profiles.memory_config`.
