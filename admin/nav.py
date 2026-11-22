"""Admin portal information architecture — the single source of truth for the
sidebar, the in-page section tabs, the Settings hub and partner-build gating.

Pure data + stdlib + ``core.deployment`` (no database), so every module
that owns a ``Jinja2Templates`` instance (``admin.router``,
``admin.eval_handlers``) can install the globals below without an import
cycle. Partner gating asks for the deployment scope at CALL time, matching
``core.deployment``'s contract (tests monkeypatch the scope).

Layout (docs/admin-portal/README.md has the human version):

  Overview · Agents · Conversations · Knowledge · Connections · Testing ·
  Activity · Help                                     … footer: Settings

Every pre-existing URL keeps working: a page is mapped onto one of these
items by its *path*, so a screen does not need to know where it lives in the
nav. Merge/hide only — nothing here removes a route.
"""
from __future__ import annotations

import os

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from core.deployment import active_scope, scoped_build
from core.plugins import has_product


# ---------------------------------------------------------------------------
# Partner builds
# ---------------------------------------------------------------------------


def partner_build() -> bool:
    """True on a scoped (partner) build: staff-only screens are hidden.

    Same predicate as ``single_env_deployment()``: any active deployment
    scope. An unknown scope configuration raises in
    ``active_scope``; the engine refuses to boot on that long before a
    template renders, so treat it as "not a partner build" here rather than
    500 every page.
    """
    try:
        return scoped_build()
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Branding (scope-driven, see core/deployment.py DeploymentScope.brand_*)
# ---------------------------------------------------------------------------

# The shell's name with no scope and no PORTAL_NAME: the WAJ build (a product
# plugin present) keeps "WAJ Admin"; the engine on its own, as published, is
# "WAJ AI Engine".
PRODUCT_PORTAL_NAME = "WAJ Admin"
ENGINE_PORTAL_NAME = "WAJ AI Engine"

_DEFAULT_BRAND: Dict[str, Any] = {
    "key": None,
    "name": PRODUCT_PORTAL_NAME,  # sidebar wordmark + <title> suffix
    "title": PRODUCT_PORTAL_NAME,  # login heading / default <title>
    "login_copy": "Sign in with your WAJ superuser account.",
    "mark": None,                 # letter on the brand tile (None = plain tile)
    "css": "",                    # accent overrides (empty = WAJ teal)
}


def brand() -> Dict[str, Any]:
    """Branding for the current build, from its deployment scope.

    A full build keeps the default shell: "WAJ Admin" where a product plugin
    is installed, "WAJ AI Engine" where none is. A scoped build gets the
    scope's own product name; when the scope carries an accent the
    ``--accent`` tokens are re-pointed for that ``data-brand`` (light + dark)
    so every accented surface follows it.
    """
    try:
        client = active_scope()
    except ValueError:
        client = None
    if client is None:
        out = dict(_DEFAULT_BRAND)
        if not has_product():
            out.update(name=ENGINE_PORTAL_NAME, title=ENGINE_PORTAL_NAME)
        # PORTAL_NAME renames the shell for a deployment that is not WAJ's.
        name = (os.getenv("PORTAL_NAME") or "").strip()
        if name:
            out.update(name=name, title=name, login_copy="Sign in with your admin account.")
        return out
    name = client.brand_name or "WAJ Engine"
    title = f"{name} · {client.brand_short}" if client.brand_short else name
    css = ""
    if client.brand_accent:
        sel = f'html[data-brand="{client.key}"]'
        css = (
            f"{sel}{{--accent:{client.brand_accent};--accent-fg:#FFFFFF;"
            f"--accent-soft:{client.brand_accent_soft or 'var(--teal-soft-l)'};}}"
            f'{sel}[data-theme="dark"]{{--accent:{client.brand_accent_dark or client.brand_accent};'
            f"--accent-fg:{client.brand_accent_fg_dark or '#14181B'};"
            f"--accent-soft:{client.brand_accent_soft_dark or 'var(--teal-soft-d)'};}}"
        )
    return {
        "key": client.key,
        "name": name,
        "title": title,
        "login_copy": f"Sign in to the {client.brand_login_product or 'WAJ AI Engine'} admin.",
        "mark": client.brand_mark,
        "accent": client.brand_accent,
        "css": css,
    }


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NavItem:
    key: str
    label: str
    href: str
    icon: str
    hint: str = ""


PRIMARY: Tuple[NavItem, ...] = (
    NavItem("overview", "Overview", "/admin/live", "overview",
            "Health and activity across your agents"),
    NavItem("agents", "Agents", "/admin/agents", "agents",
            "Set up what each agent says and does"),
    NavItem("conversations", "Conversations", "/admin/observability", "conversations",
            "Read and review real conversations"),
    NavItem("knowledge", "Knowledge", "/admin/kb", "knowledge",
            "Documents, skills and memory your agents use"),
    NavItem("connections", "Connections", "/admin/tools", "connections",
            "Tools, integrations and channels"),
    NavItem("testing", "Testing", "/admin/evals", "testing",
            "Check quality before and after you publish"),
    NavItem("activity", "Activity", "/admin/audit", "activity",
            "Who changed what, and when"),
    NavItem("help", "Help", "/admin/help", "help",
            "Guides for using the portal"),
)
SETTINGS = NavItem("settings", "Settings", "/admin/settings", "settings",
                   "Team, releases and system settings")

# Path prefix → nav key. Matched on a segment boundary, first match wins, so
# the more specific prefixes sit above the general ones.
_PATH_SECTIONS: Tuple[Tuple[str, str], ...] = (
    ("/admin/lmnr", "settings"),          # telemetry diagnose → Settings › Diagnostics
    ("/admin/live", "overview"),
    ("/admin/jobs", "settings"),          # background jobs → Settings › Diagnostics
    ("/admin/observability", "conversations"),
    ("/admin/sessions", "conversations"),
    ("/admin/agents", "agents"),
    ("/admin/agent-models", "agents"),
    ("/admin/templates", "agents"),
    ("/admin/prompts", "agents"),
    ("/admin/profiles", "agents"),
    ("/admin/e2e", "agents"),
    ("/admin/voice-budgets", "settings"),
    ("/admin/kb", "knowledge"),
    ("/admin/skills", "knowledge"),
    ("/admin/memory", "knowledge"),
    ("/admin/data-sources", "knowledge"),
    ("/admin/agent-data-sources", "knowledge"),
    ("/admin/tools", "connections"),
    ("/admin/mcp", "connections"),
    ("/admin/agent-mcp-servers", "connections"),
    ("/admin/mcp-servers", "connections"),
    ("/admin/integrations", "connections"),
    ("/admin/channels", "connections"),
    ("/admin/evals", "testing"),
    ("/admin/eval-runs", "testing"),
    ("/admin/datasets", "testing"),
    ("/admin/voice-scenarios", "testing"),
    ("/admin/tests", "testing"),
    ("/admin/test-fixtures", "testing"),
    ("/admin/audit", "activity"),
    ("/admin/help", "help"),
    ("/admin/settings", "settings"),
    ("/admin/users", "settings"),
    ("/admin/env-vars", "settings"),
    ("/admin/env", "settings"),
    ("/admin/deployments", "settings"),
    ("/admin/drift", "settings"),
    ("/admin/diagnostics", "settings"),
    ("/admin/docs", "settings"),
)

# Legacy `active` context values → nav key (fallback when the path is unknown).
_ACTIVE_SECTIONS: Dict[str, str] = {
    "live": "overview", "jobs": "settings",
    "observability": "conversations",
    "agents": "agents", "models": "agents", "templates": "agents",
    "kb": "knowledge", "skills": "knowledge", "memory": "knowledge",
    "tools": "connections", "channels": "connections", "mcp": "connections",
    "evals": "testing", "voice_scenarios": "testing", "tests": "testing",
    "audit": "activity",
    "help": "help",
    "settings": "settings", "users": "settings", "env": "settings",
    "env_vars": "settings", "deployments": "settings", "drift": "settings",
    "docs": "settings",
}


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def section_for(path: Optional[str], active: Optional[str] = None) -> Optional[str]:
    """Which sidebar item a page belongs to (by path first, then `active`)."""
    p = (path or "").rstrip("/") or "/"
    for prefix, key in _PATH_SECTIONS:
        if _under(p, prefix):
            return key
    if active:
        return _ACTIVE_SECTIONS.get(active)
    return None


def nav_item(key: Optional[str]) -> Optional[NavItem]:
    for item in PRIMARY + (SETTINGS,):
        if item.key == key:
            return item
    return None


def portal_nav() -> Dict[str, Any]:
    """Sidebar model for _base.html."""
    return {"primary": PRIMARY, "settings": SETTINGS, "partner": partner_build()}


# ---------------------------------------------------------------------------
# Section tabs (Knowledge / Connections / Testing / Settings)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SectionTab:
    key: str
    label: str
    href: str
    staff_only: bool = False


SECTION_TABS: Dict[str, Tuple[SectionTab, ...]] = {
    "knowledge": (
        SectionTab("kb", "Knowledge bases", "/admin/kb"),
        SectionTab("skills", "Skills", "/admin/skills"),
        SectionTab("memory", "Memory", "/admin/memory"),
    ),
    "connections": (
        SectionTab("tools", "Tools & integrations", "/admin/tools"),
        SectionTab("channels", "Channels", "/admin/channels"),
    ),
    "testing": (
        SectionTab("overview", "Overview", "/admin/evals"),
        SectionTab("suites", "Test suites", "/admin/evals/suites"),
        SectionTab("runs", "Runs", "/admin/evals/runs"),
        SectionTab("bakeoffs", "Model comparison", "/admin/evals/bakeoffs"),
        SectionTab("voice", "Voice checks", "/admin/voice-scenarios"),
        SectionTab("monitoring", "Schedules", "/admin/evals/scheduling"),
        SectionTab("tests", "Engineering", "/admin/tests", staff_only=True),
    ),
    "settings": (
        SectionTab("overview", "Overview", "/admin/settings"),
        SectionTab("users", "Team & access", "/admin/users"),
        SectionTab("voice_budgets", "Voice budgets", "/admin/voice-budgets"),
        SectionTab("releases", "Releases", "/admin/env", staff_only=True),
        SectionTab("variables", "Environment variables", "/admin/env?tab=variables", staff_only=True),
        SectionTab("diagnostics", "Diagnostics", "/admin/drift", staff_only=True),
        SectionTab("docs", "Developer docs", "/admin/docs", staff_only=True),
    ),
}

# Pages that render the section tab strip: exact path → (section, tab key).
# Detail pages (one KB, one tool, one run…) deliberately don't — they carry a
# back link instead. A callable picks the tab from the query string.
_TAB_PAGES: Dict[str, Tuple[str, Any]] = {
    "/admin/kb": ("knowledge", "kb"),
    "/admin/skills": ("knowledge", "skills"),
    "/admin/memory": ("knowledge", "memory"),
    "/admin/tools": ("connections", "tools"),
    "/admin/channels": ("connections", "channels"),
    "/admin/evals": ("testing", "overview"),
    "/admin/evals/suites": ("testing", "suites"),
    "/admin/evals/runs": ("testing", "runs"),
    "/admin/evals/scheduling": ("testing", "monitoring"),
    "/admin/evals/bakeoffs": ("testing", "bakeoffs"),
    "/admin/voice-scenarios": ("testing", "voice"),
    "/admin/tests": ("testing", "tests"),
    "/admin/settings": ("settings", "overview"),
    "/admin/users": ("settings", "users"),
    "/admin/voice-budgets": ("settings", "voice_budgets"),
    "/admin/env": ("settings", lambda q: "variables" if q.get("tab") == "variables" else "releases"),
    "/admin/deployments": ("settings", "releases"),
    "/admin/drift": ("settings", "diagnostics"),
    "/admin/diagnostics/runtime": ("settings", "diagnostics"),
    "/admin/lmnr/diagnose": ("settings", "diagnostics"),
    "/admin/jobs": ("settings", "diagnostics"),
    "/admin/docs": ("settings", "docs"),
}


def visible_tabs(section: str) -> List[SectionTab]:
    partner = partner_build()
    return [t for t in SECTION_TABS.get(section, ()) if not (partner and t.staff_only)]


def section_tabs_for(path: Optional[str], query: Optional[Mapping[str, str]] = None) -> Optional[Dict[str, Any]]:
    """The section tab strip for a page, or None when the page has none."""
    p = (path or "").rstrip("/") or "/"
    hit = _TAB_PAGES.get(p)
    if hit is None and p.startswith("/admin/evals/bakeoffs/"):
        hit = ("testing", "bakeoffs")  # bake-off detail kept its tab bar
    if hit is None and p.startswith("/admin/docs/") and not p.startswith("/admin/docs/api"):
        hit = ("settings", "docs")
    if hit is None:
        return None
    section, current = hit
    if callable(current):
        current = current(query or {})
    tabs = visible_tabs(section)
    if len(tabs) < 2:
        return None
    return {"section": section, "tabs": tabs, "current": current}


def section_tabs_for_request(request: Any) -> Optional[Dict[str, Any]]:
    """Jinja-friendly wrapper: reads path + query off a Starlette request."""
    if request is None:
        return None
    try:
        return section_tabs_for(request.url.path, request.query_params)
    except Exception:  # pragma: no cover — never break a page render over tabs
        return None


# ---------------------------------------------------------------------------
# Settings hub
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SettingsEntry:
    key: str
    title: str
    description: str
    icon: str
    links: Tuple[Tuple[str, str, str], ...]  # (label, href, one-line note)
    staff_only: bool = False


SETTINGS_ENTRIES: Tuple[SettingsEntry, ...] = (
    SettingsEntry(
        "users", "Team & access",
        "Who can sign in to this portal, and who has admin rights.", "users",
        (("Manage team", "/admin/users", ""),),
    ),
    SettingsEntry(
        "voice_budgets", "Voice budgets",
        "How many minutes of voice calls each business can use per day.", "voice",
        (("Set voice budgets", "/admin/voice-budgets", ""),),
    ),
    SettingsEntry(
        "releases", "Releases",
        "Copy tested agent changes from staging to live, and ship new engine versions.", "deploy",
        (("Promote agent changes", "/admin/env", "Move an agent's setup from staging to production."),
         ("Code deploys", "/admin/deployments", "Deploy, roll back and review engine releases.")),
        staff_only=True,
    ),
    SettingsEntry(
        "variables", "Environment variables",
        "Configuration values the engine reads while it runs. Secret values stay hidden.", "variables",
        (("Manage variables", "/admin/env?tab=variables", ""),),
        staff_only=True,
    ),
    SettingsEntry(
        "diagnostics", "Diagnostics",
        "Health checks for engineers when something looks wrong.", "diagnostics",
        (("Config drift", "/admin/drift", "What each agent actually serves compared with the code."),
         ("Runtime environment", "/admin/diagnostics/runtime", "Which databases and services this engine is connected to."),
         ("Telemetry check", "/admin/lmnr/diagnose", "Whether conversation traces are being recorded."),
         ("Background jobs", "/admin/jobs", "Long-running tasks and their status."),
         ("Engineering tests", "/admin/tests", "Run the engine's automated test suites.")),
        staff_only=True,
    ),
    SettingsEntry(
        "docs", "Developer docs",
        "Technical reference: architecture, data model, MCP server and API.", "docs",
        (("Open developer docs", "/admin/docs", ""),),
        staff_only=True,
    ),
)


def settings_entries() -> List[SettingsEntry]:
    partner = partner_build()
    return [e for e in SETTINGS_ENTRIES if not (partner and e.staff_only)]


# ---------------------------------------------------------------------------
# Jinja wiring
# ---------------------------------------------------------------------------


def install_template_globals(templates: Any) -> None:
    """Register the nav/brand helpers on a Jinja2Templates instance.

    Registered as callables so they re-read the deployment scope per render.
    """
    g = templates.env.globals
    g["portal_nav"] = portal_nav
    g["nav_section"] = section_for
    g["section_tabs_for"] = section_tabs_for_request
    g["partner_build"] = partner_build
    g["brand"] = brand
    g["search_quick_actions"] = search_quick_actions
    g["agent_palette"] = agent_palette
    g["agent_channel_filter_options"] = agent_channel_filter_options


def agent_channel_filter_options() -> List[Tuple[str, str]]:
    """The Agents list's channel filter (admin/screens/agents_present.py)."""
    from admin.screens.agents_present import channel_filter_options

    return channel_filter_options()


def agent_palette() -> Dict[str, str]:
    """Per-agent-kind colours for the charts (admin/screens/observability.py)."""
    from admin.screens.observability import AGENT_PALETTE  # lazy: heavy module

    return dict(AGENT_PALETTE)


def search_quick_actions(env: str = "staging") -> List[Dict[str, str]]:
    """The global-search palette's empty-state quick actions (JSON-ready)."""
    from admin.search_registry import quick_actions  # lazy: search_registry imports nav

    return [{"label": a.label, "href": a.href.replace("{env}", env), "context": a.context,
             "icon": a.icon, "type": "Action"} for a in quick_actions()]
