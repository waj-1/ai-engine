"""The global-search registry of SCREENS: every page, tab, agent tab, setting
and quick action the portal's Ctrl-K palette can open.

Generated from ONE source: the information architecture in ``admin/nav.py``
(sidebar, section tabs, Settings hub). That module is what the sidebar and the
tab strips render from, so a screen added there is searchable the moment it
exists. What nav.py cannot know — sub-pages without a nav item, an agent's own
tabs, quick actions, and the old names people still type — lives in the small
tables below (``SYNONYMS``, ``EXTRA_PAGES``, ``AGENT_TABS``, ``ACTIONS``).

``tests/admin/test_search_registry.py`` walks every GET route under /admin and
fails when a page has neither an entry here nor a reasoned line in
``ROUTE_EXCLUSIONS`` — so a new screen cannot silently go missing from search.

Partner (scoped) builds drop every ``staff`` entry, on the same predicate
the sidebar and Settings hub use (``admin.nav.partner_build``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from admin import nav

# Result groups, in the palette's default reading order.
GROUP_SCREENS = "Screens"
GROUP_AGENTS = "Agents"
GROUP_PEOPLE = "Conversations & people"
GROUP_BUILD = "Knowledge & connections"
GROUP_TESTING = "Testing"
GROUP_DOCS = "Docs"
GROUP_SETTINGS = "Settings"
GROUP_ACTIONS = "Actions"

GROUP_ORDER: Tuple[str, ...] = (
    GROUP_SCREENS, GROUP_AGENTS, GROUP_PEOPLE, GROUP_BUILD,
    GROUP_TESTING, GROUP_DOCS, GROUP_SETTINGS, GROUP_ACTIONS,
)


@dataclass(frozen=True)
class Entry:
    label: str
    type: str                 # shown once, right-aligned ("Screen", "Tab", …)
    group: str
    href: str                 # may contain "{env}"
    icon: str = "screen"
    context: str = ""         # one-line context under the title
    keywords: Tuple[str, ...] = ()
    staff: bool = False       # hidden on partner builds


# ---------------------------------------------------------------------------
# Synonyms — the names people still type. Keyed by nav key / tab key.
# Old sidebar names (Dashboard, Traces, Audit, Evals, Docs …) stay findable.
# ---------------------------------------------------------------------------

SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "overview": ("dashboard", "live", "live monitor", "kpi", "home", "monitor", "operate",
                 "analytics", "stats", "metrics"),
    "agents": ("agent profiles", "profiles", "prompts", "bots", "assistants", "sub-agents",
               "configure", "personas"),
    "conversations": ("traces", "observability", "spans", "laminar", "explorer", "telemetry",
                      "sessions", "chats", "chat", "replay", "transcript", "transcripts",
                      "calls", "recordings"),
    "knowledge": ("kb", "knowledge base", "rag", "documents", "data sources", "ingest",
                  "pgvector"),
    "connections": ("integrations", "tools", "mcp", "connectors", "apis"),
    "testing": ("evals", "eval", "evaluations", "tests", "quality", "qa", "scenarios", "judge"),
    "activity": ("audit", "audit log", "history", "changes", "change log", "trail", "who changed"),
    "help": ("guide", "guides", "how to", "manual", "documentation", "support", "faq"),
    "settings": ("preferences", "admin", "configuration", "config"),
    # section tabs
    "kb": ("kb", "knowledge base", "documents", "rag", "upload", "ingest", "data sources"),
    "skills": ("skill bundles", "playbooks", "progressive disclosure", "skill versions"),
    "memory": ("memories", "user memory", "long-term memory", "facts", "recall", "inspector"),
    "tools": ("mcp & tools", "mcp", "mcp servers", "toolsets", "http tools", "python tools",
              "functions", "integrations", "connectors"),
    "channels": ("channel", "cors", "origins", "whatsapp", "landing", "embed", "widget", "web chat"),
    "suites": ("evals", "eval suites", "test sets", "test suites", "scenarios", "evaluator",
               "cases"),
    "runs": ("test runs", "eval runs", "evaluation runs", "results", "scores", "failures",
             "run history", "testing runs"),
    "testing_overview": ("testing", "quality", "ready", "needs work", "agent health",
                         "evals overview"),
    "monitoring": ("monitoring", "production monitoring", "schedules", "scheduled evals",
                   "scheduling", "cron", "nightly", "live chats", "cadence"),
    "bakeoffs": ("bake-off", "bakeoff", "model comparison", "compare models", "a/b"),
    "voice": ("voice scenarios", "voice tests", "duplex", "live call", "call test", "voice live"),
    "tests": ("pytest", "unit tests", "engineering tests", "engineering", "suite", "fixtures"),
    "users": ("users", "roles", "users & roles", "superuser", "permissions", "access", "team",
              "members", "staff", "admins"),
    "releases": ("environments", "env", "promote", "publish", "staging", "production", "prod",
                 "go live"),
    "variables": ("env vars", "environment variables", "secrets", "config values", "registry"),
    "diagnostics": ("drift", "drift check", "health", "debug", "troubleshoot", "runtime",
                    "telemetry", "background jobs"),
    "docs": ("developer docs", "docs", "architecture", "data model", "api reference",
             "engineering docs"),
    "voice_budgets": ("voice budget", "minutes", "daily cap", "voice seconds", "call minutes"),
    "model_keys": ("model keys", "provider keys", "api key", "openai key", "gemini key",
                   "anthropic key", "azure key", "credentials"),
}

_SECTION_LABEL = {item.key: item.label for item in nav.PRIMARY + (nav.SETTINGS,)}
_SECTION_ICON = {item.key: item.icon for item in nav.PRIMARY + (nav.SETTINGS,)}


# ---------------------------------------------------------------------------
# Pages nav.py does not list (sub-pages, tab variants, old screens' new homes)
# ---------------------------------------------------------------------------

EXTRA_PAGES: Tuple[Entry, ...] = (
    Entry("Models", "Tab", GROUP_AGENTS, "/admin/agents?tab=models&env={env}", "model",
          "In Agents — the AI models agents can use",
          ("llm", "model", "provider", "gpt", "gemini", "temperature", "tokens", "agent models")),
    Entry("Agent templates", "Tab", GROUP_AGENTS, "/admin/templates?env={env}", "agents",
          "In Agents — start a new agent from a ready-made template",
          ("templates", "templates tab", "molds", "new agent", "create agent", "gallery")),
    Entry("Agent categories", "Tab", GROUP_AGENTS, "/admin/agent-types?env={env}", "agents",
          "In Agents — your own groups for agents, such as Support or Sales",
          ("categories", "category", "agent types", "types", "groups", "group agents",
           "labels", "tags", "organise", "organize")),
    Entry("Agent memory settings", "Screen", GROUP_AGENTS, "/admin/agents/memory?env={env}", "agents",
          "What each agent type remembers about people, and for how long",
          ("memory settings", "agent memory", "memory config", "remember", "retention",
           "long-term memory")),
    Entry("Compare agents", "Screen", GROUP_AGENTS, "/admin/agents/diff?env={env}", "agents",
          "See two agents' setups side by side", ("diff", "compare", "difference")),
    Entry("Compare test runs", "Screen", GROUP_TESTING, "/admin/evals/compare?env={env}", "testing",
          "Put test runs side by side: what got fixed, what broke",
          ("compare runs", "run comparison", "eval compare", "regression", "evaluation runs")),
    Entry("Test fixtures", "Tab", GROUP_TESTING, "/admin/tests?env={env}#fixtures", "testing",
          "Engineering test inputs (tokens, ids)", ("fixtures", "test inputs", "jwt"), staff=True),
    Entry("Promotion history", "Tab", GROUP_SETTINGS, "/admin/env?tab=history&env={env}", "deploy",
          "In Settings › Releases — every agent version published, by whom and when",
          ("promotion history", "promote history", "published versions", "agent versions",
           "who promoted"), staff=True),
    Entry("Deploy history", "Tab", GROUP_SETTINGS, "/admin/deployments?tab=history&env={env}", "deploy",
          "In Settings › Releases — past engine deploys",
          ("deploy history", "deploy runs", "workflow runs", "github actions", "rollback history"),
          staff=True),
    Entry("Deployment targets", "Tab", GROUP_SETTINGS, "/admin/deployments?tab=targets&env={env}", "deploy",
          "In Settings › Releases — where the engine deploys to",
          ("deployment targets", "targets", "base url", "container apps", "cloud run"), staff=True),
    Entry("API reference", "Developer doc", GROUP_DOCS, "/admin/docs/api", "docs",
          "Interactive API reference (Swagger)", ("api", "swagger", "openapi", "endpoints"),
          staff=True),
    Entry("API reference (ReDoc)", "Developer doc", GROUP_DOCS, "/admin/docs/api/redoc", "docs",
          "The same API reference, as one long page", ("redoc", "api", "openapi"), staff=True),
)


# ---------------------------------------------------------------------------
# An agent's own tabs (``/admin/agents?profile_id=X&tab=Y``; the tab keys are
# ``admin/screens/agents_views.DETAIL_TABS``). Generic rows open the Agents
# list; when the query also names an agent ("support prompt") the search layer
# pairs the two into "Support › Instructions" with the agent's id filled in.
# Keywords keep the old section names (model, status, endpoint, toolsets…)
# pointing at the tab that holds that section now.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentTab:
    key: str
    label: str
    tab: str                  # the ?tab= value on the agent page
    keywords: Tuple[str, ...] = field(default_factory=tuple)


AGENT_TABS: Tuple[AgentTab, ...] = (
    AgentTab("overview", "Overview", "overview",
             ("overview", "summary", "agent health", "at a glance")),
    AgentTab("instructions", "Instructions", "instructions",
             ("prompt", "prompts", "system prompt", "persona", "instructions", "what it says")),
    AgentTab("tools", "Tools & knowledge", "tools",
             ("tools", "actions", "functions", "mcp", "toolsets", "integrations",
              "mcp attachments", "knowledge", "sources", "kb", "data sources", "skills",
              "skill attachments")),
    AgentTab("subagents", "Sub-agents", "subagents",
             ("sub-agents", "subagents", "downstream", "handoff", "delegate")),
    AgentTab("voice", "Voice", "voice",
             ("voice", "voice settings", "voice cockpit", "barge-in", "greeting", "call")),
    AgentTab("versions", "Versions", "versions",
             ("versions", "history", "rollback", "publish", "pin")),
    AgentTab("settings", "Settings", "settings",
             ("settings", "model", "llm", "temperature", "status", "active", "draft", "archive",
              "endpoint", "api path", "url", "memory settings")),
)


def agent_tab_href(tab: AgentTab, profile_id: Optional[str], env: str) -> str:
    if profile_id:
        return f"/admin/agents?env={env}&profile_id={profile_id}&tab={tab.tab}"
    # No agent named: open the Agents list (it ignores ?tab= until one is picked).
    return f"/admin/agents?env={env}&tab={tab.tab}"


# ---------------------------------------------------------------------------
# Quick actions (the ``>`` scope). ``quick`` ones also show when the palette
# opens empty.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    label: str
    href: str
    context: str
    keywords: Tuple[str, ...] = ()
    icon: str = "action"
    staff: bool = False
    quick: bool = False


ACTIONS: Tuple[Action, ...] = (
    Action("New agent", "/admin/templates?env={env}", "Start from a template",
           ("create agent", "add agent", "new bot", "new profile"), "agents", quick=True),
    Action("Run a test", "/admin/evals?env={env}", "Pick a test suite and run it against an agent",
           ("run eval", "test agent", "evaluate", "run suite"), "testing", quick=True),
    Action("Open Help", "/admin/help", "Guides for using the portal",
           ("help", "guide", "how to"), "help", quick=True),
    Action("Read recent conversations", "/admin/observability?env={env}", "What customers said today",
           ("conversations", "traces", "chats"), "conversations", quick=True),
    Action("Create test suite", "/admin/evals?env={env}", "A new set of test conversations",
           ("new suite", "add eval", "create suite", "new test set")),
    Action("Add knowledge", "/admin/kb?env={env}", "Upload documents an agent can answer from",
           ("upload", "ingest", "add document", "new knowledge base", "import")),
    Action("Create a toolset", "/admin/mcp/create?env={env}", "Bundle tools into a toolset (MCP server)",
           ("new mcp", "create mcp", "bundle", "new toolset", "http tool", "new tool", "create tool")),
    Action("Connect an external server", "/admin/mcp/connect?env={env}", "Connect a remote MCP server",
           ("connect mcp", "external mcp", "remote server", "add mcp")),
    Action("Add channel", "/admin/channels/new?env={env}", "Where customers reach your agents",
           ("new channel", "create channel", "add website", "cors")),
    Action("Add model", "/admin/agents?tab=models&env={env}", "Make another AI model available",
           ("new model", "add llm", "create model", "provider")),
    Action("Promote agent changes", "/admin/env?env={env}", "Copy tested changes from staging to live",
           ("promote", "publish", "go live", "release", "promote profile"), "deploy", staff=True),
    Action("Deploy the engine", "/admin/deployments?env={env}", "Ship or roll back an engine release",
           ("deploy", "ship", "rollout", "rollback", "deploy to env"), "deploy", staff=True),
    Action("Manage env vars", "/admin/env?tab=variables&env={env}", "Configuration values the engine reads",
           ("env vars", "variables", "secrets", "environment variables"), "variables", staff=True),
    Action("Add deployment target", "/admin/deployments?tab=targets&env={env}", "Register where the engine runs",
           ("new target", "deployment target", "base url"), "deploy", staff=True),
    Action("Run engineering tests", "/admin/tests?env={env}", "Run the engine's pytest suites",
           ("pytest", "run tests", "execute tests"), "testing", staff=True),
)


# ---------------------------------------------------------------------------
# GET routes that deliberately have no search entry (coverage test)
# ---------------------------------------------------------------------------

ROUTE_EXCLUSIONS: Dict[str, str] = {
    "/admin": "redirects to the default screen",
    "/admin/": "redirects to the default screen",
    "/admin/login": "sign-in page, not a destination",
    "/admin/demo/assistant/history": "JSON for the demo's assistant panel, not a page",
    "/admin/demo/call": "a redirect to the Support desk's Voice tab (the demo banner's call link)",
    "/admin/setup": "first-run page; exists only until the admin account is created",
    "/admin/setup/model-key": "first-run walkthrough step, shown once to the admin who set up",
    "/admin/setup/api-key": "first-run walkthrough step, shown once to the admin who set up",
    "/admin/configure": "retired; redirects to Agents",
    "/admin/agent-data-sources": "old URL; redirects to Knowledge",
    "/admin/data-sources": "old URL; redirects to Knowledge",
    "/admin/agent-mcp-servers": "old URL; redirects to Tools & integrations",
    "/admin/mcp-servers": "old URL; redirects to Tools & integrations",
    "/admin/integrations": "old URL; redirects to Tools & integrations",
    "/admin/agent-models": "old URL; redirects to Agents › Models",
    "/admin/profiles": "old URL; redirects to Agents",
    "/admin/prompts": "old URL; redirects to Agents",
    "/admin/sessions": "old URL; redirects to Conversations",
    "/admin/observability/sessions": "old URL; redirects to Conversations",
    "/admin/observability/explorer": "same screen as Conversations (/admin/observability)",
    "/admin/datasets": "retired; redirects to Testing",
    "/admin/eval-runs": "old URL; redirects to Testing",
    "/admin/env-vars": "old URL; redirects to Settings › Environment variables",
    "/admin/tools/new": "retired form; redirects to Tools & integrations",
    "/admin/test-fixtures": "redirects to Engineering tests #fixtures (entry: Test fixtures)",
    "/admin/test-fixtures/export": "file download, not a page",
    "/admin/env/promote/preview": "JSON preview fetched by the Promote dialog, not a page",
}


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def _section_entries() -> List[Entry]:
    out: List[Entry] = []
    for item in nav.PRIMARY:
        out.append(Entry(
            item.label, "Screen", GROUP_SCREENS, _with_env(item.href, item.key), item.icon,
            item.hint, SYNONYMS.get(item.key, ()),
        ))
    s = nav.SETTINGS
    out.append(Entry(s.label, "Screen", GROUP_SETTINGS, s.href, s.icon, s.hint,
                     SYNONYMS.get(s.key, ())))
    return out


def _tab_entries() -> List[Entry]:
    out: List[Entry] = []
    for section, tabs in nav.SECTION_TABS.items():
        group = GROUP_SETTINGS if section == "settings" else GROUP_SCREENS
        sec_label = _SECTION_LABEL.get(section, section.title())
        for tab in tabs:
            if section == "settings" and tab.key == "overview":
                continue  # == the Settings screen itself
            label, syn_key = tab.label, tab.key
            if tab.key == "overview":
                # "Overview" alone is the sidebar screen; name the section's own.
                label, syn_key = f"{sec_label} overview", f"{section}_overview"
            out.append(Entry(
                label, "Setting" if section == "settings" else "Tab", group,
                _with_env(tab.href, section), _SECTION_ICON.get(section, "screen"),
                f"In {sec_label}", SYNONYMS.get(syn_key, ()), staff=tab.staff_only,
            ))
    return out


def _settings_entries() -> List[Entry]:
    """Settings hub items and every link inside them."""
    out: List[Entry] = []
    for e in nav.SETTINGS_ENTRIES:
        first_href = e.links[0][1] if e.links else nav.SETTINGS.href
        out.append(Entry(e.title, "Setting", GROUP_SETTINGS, _with_env(first_href, "settings"),
                         e.icon, e.description, SYNONYMS.get(e.key, ()), staff=e.staff_only))
        if len(e.links) > 1:
            for label, href, note in e.links:
                out.append(Entry(label, "Setting", GROUP_SETTINGS, _with_env(href, "settings"),
                                 e.icon, note or f"In Settings › {e.title}",
                                 _link_keywords(label) + (e.title.lower(),),
                                 staff=e.staff_only))
    return out


def _link_keywords(label: str) -> Tuple[str, ...]:
    extra = {
        "Config drift": ("drift", "drift check", "prompt drift", "config levers", "incident lever"),
        "Telemetry check": ("telemetry", "laminar", "clickhouse", "diagnose", "spans missing"),
        "Background jobs": ("jobs", "background", "tasks", "queue", "workers"),
        "Engineering tests": ("tests", "pytest", "unit"),
        "Code deploys": ("deploy", "deployment", "ship", "rollout", "rollback"),
        "Promote agent changes": ("promote", "publish", "staging", "production"),
        "Runtime environment": ("runtime", "runtime environment", "supabase url", "process env",
                                "runtime flags", "deploy token"),
    }
    return extra.get(label, ())


def _agent_tab_entries() -> List[Entry]:
    return [
        Entry(f"Agent › {t.label}", "Agent tab", GROUP_AGENTS, agent_tab_href(t, None, "{env}"),
              "agents", "Open an agent, then this tab", t.keywords)
        for t in AGENT_TABS
    ]


def _action_entries() -> List[Entry]:
    return [Entry(a.label, "Action", GROUP_ACTIONS, a.href, a.icon, a.context, a.keywords,
                  staff=a.staff) for a in ACTIONS]


def _with_env(href: str, section: str) -> str:
    """Env-scoped screens carry ?env=; Help/Settings/docs are env-agnostic."""
    if section in ("help",) or href.startswith(("/admin/help", "/admin/docs", "/admin/settings")):
        return href
    sep = "&" if "?" in href else "?"
    path, _, frag = href.partition("#")
    return f"{path}{sep}env={{env}}" + (f"#{frag}" if frag else "")


def _dedupe(entries: List[Entry]) -> List[Entry]:
    seen: set = set()
    out: List[Entry] = []
    for e in entries:
        key = (e.label.lower(), e.href)
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def all_entries() -> List[Entry]:
    """Every screen-like entry, staff ones included (the coverage test uses
    this; the palette uses ``visible_entries``)."""
    return _dedupe(
        _section_entries() + _tab_entries() + list(EXTRA_PAGES) + _settings_entries()
        + _agent_tab_entries() + _action_entries()
    )


def _demo_allows(href: str) -> bool:
    """The public demo does not offer a screen it refuses (demo_mode/policy.py)."""
    return nav._demo_allows(href.replace("{env}", "staging"))


def visible_entries() -> List[Entry]:
    partner = nav.partner_build()
    return [e for e in all_entries() if not (partner and e.staff) and _demo_allows(e.href)]


def quick_actions() -> List[Action]:
    partner = nav.partner_build()
    return [a for a in ACTIONS if a.quick and not (partner and a.staff) and _demo_allows(a.href)]


def registry_paths() -> set:
    """The route paths the registry links to (query + fragment stripped)."""
    paths = set()
    for e in all_entries():
        p = urlsplit(e.href.replace("{env}", "staging")).path.rstrip("/") or "/"
        paths.add(p)
    return paths
