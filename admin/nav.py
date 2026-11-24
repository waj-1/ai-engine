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
    "glyph": None,                # "engine" = the engine's own mark instead of the plain tile
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
            # The engine's own mark on the sidebar, sign-in and first-run
            # pages (admin/templates/_components/brand_mark.html).
            out.update(name=ENGINE_PORTAL_NAME, title=ENGINE_PORTAL_NAME, glyph="engine")
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
    ("/admin/agent-types", "agents"),     # Agents › Categories
    ("/admin/agent-models", "agents"),
    ("/admin/templates", "agents"),
    ("/admin/prompts", "agents"),
    ("/admin/profiles", "agents"),
    ("/admin/e2e", "agents"),
    ("/admin/voice-budgets", "settings"),
    ("/admin/model-keys", "settings"),
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
    ("/admin/api-keys", "settings"),
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
    "api_keys": "settings",
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
        SectionTab("api_keys", "API keys", "/admin/api-keys", staff_only=True),
        SectionTab("voice_budgets", "Voice budgets", "/admin/voice-budgets"),
        SectionTab("model_keys", "Model keys", "/admin/model-keys"),
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
    "/admin/api-keys": ("settings", "api_keys"),
    "/admin/voice-budgets": ("settings", "voice_budgets"),
    "/admin/model-keys": ("settings", "model_keys"),
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
    return [t for t in SECTION_TABS.get(section, ())
            if not (partner and t.staff_only) and _demo_allows(t.href)]


def _demo_on() -> bool:
    from demo_mode import settings as demo_settings

    return demo_settings.enabled()


def _demo_allows(href: str) -> bool:
    """False for a link the public demo refuses (demo_mode/policy.py), so a
    locked screen is not offered in the navigation either."""
    if not _demo_on():
        return True
    from demo_mode.policy import href_allowed

    return href_allowed(href)


def demo_chrome() -> Dict[str, Any]:
    """What the templates need to show the demo banner and entry button."""
    from demo_mode import settings as demo_settings

    if not demo_settings.enabled():
        return {"enabled": False}
    return {"enabled": True, "repo_url": demo_settings.repo_url(),
            # Voice calls are on: the banner and the entry page offer one.
            "voice": demo_settings.voice_enabled()}


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


def page_section(path: Optional[str]) -> Optional[str]:
    """The section key ('settings', 'knowledge', ...) of a page that carries a
    section tab strip, or None. Unlike `section_tabs_for` it does not depend on
    how many tabs the build shows."""
    p = (path or "").rstrip("/") or "/"
    hit = _TAB_PAGES.get(p)
    if hit is None and p.startswith("/admin/docs/") and not p.startswith("/admin/docs/api"):
        return "settings"
    return hit[0] if hit else None


# Operate screens read what agents are doing; they change nothing, so the
# "you are working on production" banner would be noise there.
_READ_ONLY_ACTIVE = frozenset({"live", "observability"})


def env_chrome(request: Any, active: Optional[str], env_scoped: bool = False) -> Dict[str, bool]:
    """Whether a page shows the STAGING / PRODUCTION switch and the
    production banner.

    Settings pages mostly hold install-wide data (team, API keys, releases,
    diagnostics) that is the same whichever environment is picked, so they do
    not show the switch; the few that hold per-environment data pass
    ``env_scoped`` and then name the environment on the page. Every other
    config screen shows the switch, and a production banner when production
    is picked, so editing production is never silent."""
    path = getattr(getattr(request, "url", None), "path", None)
    in_settings = page_section(path) == "settings"
    return {
        "switch": env_scoped or not in_settings,
        "prod_banner": (active or "") not in _READ_ONLY_ACTIVE,
        "page_marker": bool(env_scoped),
    }


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


_VOICE_BUDGETS_AGENT_TEXT = "How much voice-call time each agent can use per day."
_VOICE_BUDGETS_BUSINESS_TEXT = "How much voice-call time each business can use per day."

SETTINGS_ENTRIES: Tuple[SettingsEntry, ...] = (
    SettingsEntry(
        "users", "Team & access",
        "Who can sign in to this portal, and who has admin rights.", "users",
        (("Manage team", "/admin/users", ""),),
    ),
    SettingsEntry(
        "api_keys", "API keys",
        "Keys that let your own programs talk to your agents or manage the engine.", "variables",
        (("Manage API keys", "/admin/api-keys", ""),),
        # The chat API a key opens is not served on a partner build
        # (main.deployment_gate), so the screen is not offered there.
        staff_only=True,
    ),
    SettingsEntry(
        "voice_budgets", "Voice budgets",
        # Neutral default; a build whose plugin has businesses says so
        # (`settings_entries`), the same wording as the Voice budgets page.
        _VOICE_BUDGETS_AGENT_TEXT, "voice",
        (("Set voice budgets", "/admin/voice-budgets", ""),),
    ),
    SettingsEntry(
        "model_keys", "Model keys",
        "The keys your agents use to reach each AI provider. Stored encrypted and never shown again.", "variables",
        (("Manage model keys", "/admin/model-keys", ""),),
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
    from dataclasses import replace

    from admin.eval_context import business_field_label

    partner = partner_build()
    has_businesses = bool(business_field_label())
    entries: List[SettingsEntry] = []
    for e in SETTINGS_ENTRIES:
        if partner and e.staff_only:
            continue
        if e.key == "voice_budgets" and has_businesses:
            e = replace(e, description=_VOICE_BUDGETS_BUSINESS_TEXT)
        entries.append(e)
    if not _demo_on():
        return entries
    from demo_mode.policy import HIDDEN_SETTINGS

    out: List[SettingsEntry] = []
    for e in entries:
        if e.key in HIDDEN_SETTINGS:
            continue
        links = tuple(link for link in e.links if _demo_allows(link[1]))
        if links:
            out.append(replace(e, links=links))
    return out


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
    g["env_chrome"] = env_chrome
    g["partner_build"] = partner_build
    g["brand"] = brand
    g["demo_chrome"] = demo_chrome
    g["search_quick_actions"] = search_quick_actions
    g["agent_palette"] = agent_palette
    g["agent_channel_filter_options"] = agent_channel_filter_options
    g["agent_type_options"] = agent_type_options


def agent_type_options() -> List[Dict[str, Any]]:
    """The user-defined agent categories, in display order, for a category
    picker (admin/services/agent_types.py). Empty on any failure: a picker
    must not take a page down."""
    try:
        from admin.services.agent_types import list_types

        return list_types(with_counts=False)
    except Exception:  # noqa: BLE001
        return []


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
