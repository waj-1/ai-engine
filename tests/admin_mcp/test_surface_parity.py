"""Guards against drift between the admin MCP tool catalog, the admin
portal's mutating routes, and docs/admin-portal/06-mcp.md.

Why this exists
----------------
An audit on 2026-08-06 found the admin MCP server had silently fallen
behind the portal: voice-eval features (target/headers on a scenario run)
were unreachable over MCP for a stretch, a documented `voice_scenarios`
param had no portal form field, and 06-mcp.md's tool count sat at "39
tools" while 41 (now 44) were actually registered — nobody had ever
compared the portal's route table, the MCP tool registry, and the docs
against each other. `admin/mcp_parity_handlers.py` (`GET
/admin/api/mcp-flag-parity`, also exposed as the `mcp_flag_parity` MCP
tool) checks a completely different thing: whether a profile's MCP
*attachment* wiring resolves to a tool set at request time. It has never
asserted anything about the admin *surface* — routes, tools, docs — being
consistent with itself. This module is the guard that was missing.

Three checks, each built so a new drift item is a **failure that names the
drift**, never a silently-updated snapshot:

(a) test_every_registered_tool_is_documented — the set of tools actually
    registered on the FastMCP instance must equal the set of tools
    documented in 06-mcp.md's tool-catalog tables.
(b) test_doc_tool_counts_match_reality — every "N tool(s)" figure stated in
    06-mcp.md and README.md must equal the real registered count. This is
    the check that would have caught "39 tools" while 41 were registered.
(c) test_every_mutating_route_is_mapped_or_justified — every state-changing
    (POST/PUT/PATCH/DELETE) `/admin/*` route must appear in exactly one of
    ROUTE_TO_TOOL (mapped to the MCP tool that covers it) or PORTAL_ONLY (a
    one-line reason it is deliberately not exposed over MCP). A brand-new
    route fails this test until a human makes that call — drift can no
    longer land silently.

When this test fails
---------------------
- (a) names an undocumented tool → add a row to the matching domain table
  in docs/admin-portal/06-mcp.md. If the tool is genuinely code-only by
  design (rare — say why in the PR, don't just delete the assertion.
  (a) names a documented-but-gone tool → the doc still describes a tool
  that no longer exists; delete or fix that doc row.
- (b) names a file/line whose stated count no longer matches reality → fix
  the number there.
- (c) names an unmapped route → either add it to ROUTE_TO_TOOL (an MCP tool
  already covers it) or to PORTAL_ONLY with a real, specific one-line
  reason (SSE-streamed, cookie-session auth, a genuinely-missing MCP
  capability you just found while doing this...). Do not delete a route
  from discovery, and do not add a vague reason just to make the test
  green — the whole point of this test is that someone reads the drift and
  decides.

Entries in ROUTE_TO_TOOL / PORTAL_ONLY are kept alphabetically sorted by
route path.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict

import pytest
from fastapi.routing import APIRoute

from tests.admin_mcp.conftest import rpc

REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_DOC = REPO_ROOT / "docs" / "admin-portal" / "06-mcp.md"
README_DOC = REPO_ROOT / "docs" / "admin-portal" / "README.md"

# --------------------------------------------------------------------------- #
# (c) route -> tool / portal-only mapping
#
# Every state-changing admin route discovered below must appear in exactly
# one of these two dicts. Keep both alphabetically sorted by route path.
# --------------------------------------------------------------------------- #

#: route path -> the MCP tool (admin_mcp/tools_*.py) whose actions cover it.
ROUTE_TO_TOOL: Dict[str, str] = {
    # (POST /admin/agent-data-sources and POST /admin/agent-mcp-servers used to
    # be listed here — the _make_simple_crud factory creates. Removed 2026-08-26
    # along with the factory; the surviving routes on those paths are GET
    # redirects, which this check's POST/PUT/PATCH/DELETE discovery ignores.)
    "/admin/agent-mcp-servers/{mcp_server_id}": "mcp_servers",
    "/admin/agent-mcp-servers/{mcp_server_id}/add-tool": "mcp_servers",
    "/admin/agent-mcp-servers/{mcp_server_id}/delete": "mcp_servers",
    "/admin/agent-mcp-servers/{mcp_server_id}/members": "mcp_servers",
    "/admin/agent-models": "models",
    "/admin/agent-models/{model_id}": "models",
    "/admin/agent-models/{model_id}/promote": "models",
    # Agents › Categories. `move` (one place up or down) is `update` with
    # `sort_order` over MCP.
    "/admin/agent-types": "agent_types",
    "/admin/agent-types/{agent_type_id}/delete": "agent_types",
    "/admin/agent-types/{agent_type_id}/move": "agent_types",
    "/admin/agent-types/{agent_type_id}/update": "agent_types",
    "/admin/agents/from-template": "templates",
    # POST is a read: the portal sends the search query in the body so it
    # never lands in a URL/access log. Same payload as GET → `search` tool.
    "/admin/search.json": "search",
    "/admin/agents/{profile_id}/e2e/run": "e2e_runs",
    "/admin/agents/{profile_id}/fork": "fork_profile",
    "/admin/agents/{profile_id}/pin-version": "profile_versions",
    "/admin/agents/{profile_id}/versions": "profile_versions",
    "/admin/agents/{profile_id}/versions/{version_id}/restore": "profile_versions",
    # The dedicated duplex-voice screen writes exactly one column,
    # `agent_profiles.voice_config` — the same column `update_profile`'s
    # `voice_config` / `voice_config_patch` arguments already own, and with the
    # same flat-shape contract (the MCP refuses a nested `live` block for the
    # same reason the screen never emits one).
    "/admin/agents/{profile_id}/voice": "update_profile",
    # The cockpit's brain panel writes two profile fields to the SPECIALIST
    # row: `model_main_id` (update_profile's own argument) and
    # `memory_config` (owned over MCP by memory(action="set_config") in
    # tools_govern.py). Mapped to update_profile as the primary tool; the
    # memory half has its own MCP surface already.
    "/admin/agents/{profile_id}/voice/brain": "update_profile",
    "/admin/api-keys": "api_keys",
    "/admin/api-keys/{key_id}/revoke": "api_keys",
    "/admin/audit/verify": "audit",
    "/admin/channels": "channels",
    "/admin/channels/{channel_id}": "channels",
    "/admin/channels/{channel_id}/delete": "channels",
    "/admin/deployments": "deployment_targets",
    "/admin/deployments/{target_id}": "deployment_targets",
    "/admin/deployments/{target_id}/delete": "deployment_targets",
    "/admin/env-vars": "env_vars",
    "/admin/env-vars/{var_id}": "env_vars",
    "/admin/env-vars/{var_id}/delete": "env_vars",
    "/admin/env/promote": "promote_profile",
    "/admin/env/{environment}/deploy": "deploy",
    "/admin/env/{environment}/rollback": "rollback",
    "/admin/eval-runs/schedules": "eval_schedules",
    "/admin/eval-runs/schedules/{schedule_id}": "eval_schedules",
    "/admin/eval-runs/schedules/{schedule_id}/delete": "eval_schedules",
    "/admin/eval-runs/schedules/{schedule_id}/fire": "eval_schedules",
    "/admin/eval-runs/trigger": "trigger_eval_run",
    "/admin/evals/bakeoffs": "bakeoffs",
    "/admin/evals/run": "trigger_eval_run",
    "/admin/evals/suites": "eval_suites",
    "/admin/evals/suites/{suite_id}": "eval_suites",
    "/admin/evals/suites/{suite_id}/import": "eval_suites",
    "/admin/jobs/{job_id}/cancel": "jobs",
    "/admin/kb": "kb_sources",
    "/admin/kb/{source_id}": "kb_sources",
    "/admin/kb/{source_id}/delete": "kb_sources",
    "/admin/kb/{source_id}/upload": "kb_sources",
    "/admin/mcp/connect": "mcp_servers",
    "/admin/mcp/create": "mcp_servers",
    "/admin/mcp/discover": "mcp_servers",
    "/admin/memory/add": "memory",
    "/admin/memory/config": "memory",
    "/admin/memory/{memory_id}/delete": "memory",
    "/admin/memory/{memory_id}/edit": "memory",
    "/admin/memory/{memory_id}/pin": "memory",
    "/admin/memory/{memory_id}/assign": "memory",
    "/admin/profiles/{profile_id}": "update_profile",
    "/admin/profiles/{profile_id}/mcp-attachments": "profile_mcp_attachments",
    "/admin/profiles/{profile_id}/serve-prompt-version": "prompts",
    "/admin/profiles/{profile_id}/skill-attachments": "profile_skill_attachments",
    # Non-streaming twin of the portal's SSE optimizer (same helpers).
    "/admin/model-keys": "provider_keys",
    "/admin/model-keys/delete": "provider_keys",
    "/admin/model-keys/test": "provider_keys",
    "/admin/prompts/{prompt_id}/optimize": "prompt_optimize",
    "/admin/prompts/{prompt_id}/versions": "prompts",
    # A custom agent's own instructions: the same create_version + serve the
    # `prompts` tool does, after ensuring the agent's prompt row exists.
    "/admin/agents/{profile_id}/instructions": "prompts",
    "/admin/skills/create": "skills",
    "/admin/skills/{skill_id}/archive": "skills",
    "/admin/skills/{skill_id}/resources/{resource_id}/delete": "skills",
    "/admin/skills/{skill_id}/update": "skills",
    "/admin/skills/{skill_id}/versions/create": "skills",
    "/admin/skills/{skill_id}/versions/{version_id}/resources/create": "skills",
    "/admin/test-fixtures": "test_fixtures",
    "/admin/test-fixtures/{fixture_id}": "test_fixtures",
    "/admin/test-fixtures/{fixture_id}/delete": "test_fixtures",
    "/admin/tests/run": "pytest_runs",
    "/admin/tools/dry-run": "tools",
    "/admin/tools/new": "tools",
    "/admin/tools/{tool_id}": "tools",
    "/admin/tools/{tool_id}/delete": "tools",
    "/admin/tools/{tool_id}/edit": "tools",
    "/admin/users/{user_id}/superuser": "users",
    "/admin/voice-budgets": "voice_budgets",
    "/admin/voice-budgets/{business_id}/delete": "voice_budgets",
    "/admin/voice-scenarios": "voice_scenarios",
    "/admin/voice-scenarios/{slug}/delete": "voice_scenarios",
    "/admin/voice-scenarios/{slug}/run": "voice_scenarios",
}

#: route path -> one-line reason it is deliberately NOT exposed over MCP.
#: Seeded from the known-deliberate set in the 2026-08-06 audit. Building
#: this map also surfaced one entry that was NOT deliberate:
#: `skills/{id}/archive` had no MCP path at all. It was closed rather than
#: justified (`skills` action=update now takes status/is_active) and moved
#: into ROUTE_TO_TOOL above — which is the intended outcome when a route
#: lands here for no better reason than nobody having built the tool.
PORTAL_ONLY: Dict[str, str] = {
    "/admin/eval-runs/schedules/due/fire": (
        "batch cron fire (the pg_cron sweep entrypoint) — the per-schedule "
        "tool exists (eval_schedules action=fire)"
    ),
    "/admin/login": "session-cookie auth — MCP authenticates with a bearer JWT, not a login flow",
    "/admin/logout": "session-cookie auth — MCP authenticates with a bearer JWT, not a login flow",
    "/admin/setup": (
        "first-run page: creates the first admin before any credential exists, "
        "so there is nothing for an MCP caller to authenticate with"
    ),
    "/admin/setup/api-key": "first-run walkthrough step; over MCP it is api_keys action=create",
    "/admin/setup/model-key": "first-run walkthrough step; over MCP it is provider_keys action=set / test",
    "/admin/profiles/{profile_id}/input-validators": (
        "documented MCP gap (06-mcp.md) — no tool exposes profile.input_validators"
    ),
    "/admin/prompts/{prompt_id}/compare": (
        "documented deviation (06-mcp.md 'Deviations from the portal') — "
        "interactive form that fires two eval runs pinned to two prompt "
        "versions; over MCP that is two trigger_eval_run calls with "
        "prompt_overrides, then eval_runs action=compare"
    ),
}

# GET-only surfaces fall outside this check's POST/PUT/PATCH/DELETE
# discovery by design. Their MCP coverage, recorded here so the audit
# finding isn't lost: the profile diff views (`GET /admin/agents/diff`,
# `GET /admin/agents/{profile_id}/versions/{version_id}/diff`) are
# `diff_profiles` and `profile_versions action=diff`; `GET /admin/env`'s
# promote board is `promotion_status`; the agents page's test-chat widget is
# `chat`. `GET /admin/lmnr/diagnose` (fires a diagnostic Laminar test span
# as a side effect of a GET) still has no MCP tool. If any of these ever
# grows a real mutation, it will show up in `_mutating_admin_routes()` and
# this test will demand a decision on it like any other new route.


def _mutating_admin_routes():
    """All state-changing (POST/PUT/PATCH/DELETE) routes under /admin/*.

    Imports admin.router plus the handler modules main.py additionally
    pulls in (admin.router's own bottom-of-file imports don't cover all of
    them — prompt_handlers/channel_handlers/observability_explorer_handlers/
    jobs_handlers/eval_runs_handlers/memory_handlers register their routes
    only once main.py imports them). Importing main.py itself would also
    work but drags in Laminar/lifespan wiring this check doesn't need —
    importing the handler modules directly is the cheaper, side-effect-free
    equivalent (they only register routes on the shared `router` object at
    import time).
    """
    from admin.router import router
    import admin.prompt_handlers  # noqa: F401
    import admin.channel_handlers  # noqa: F401
    import admin.observability_explorer_handlers  # noqa: F401
    import admin.jobs_handlers  # noqa: F401
    import admin.eval_runs_handlers  # noqa: F401
    import admin.memory_handlers  # noqa: F401

    seen = set()
    out = []
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        methods = route.methods - {"HEAD", "OPTIONS"}
        if not (methods & {"POST", "PUT", "PATCH", "DELETE"}):
            continue
        # `router = APIRouter(prefix="/admin", ...)` bakes the prefix into
        # route.path already, so no manual joining is needed here.
        path = route.path
        if path in seen:
            continue
        seen.add(path)
        out.append(path)
    return sorted(out)


# --------------------------------------------------------------------------- #
# (a) + (b): doc parsing
# --------------------------------------------------------------------------- #

_TOOL_ROW = re.compile(r"^\|\s*`([a-zA-Z_][a-zA-Z0-9_]*)`\s*\|", re.MULTILINE)
_COUNT_IN_TEXT = re.compile(r"(\d+)[\s-]+tools?\b")


def _documented_tool_names() -> set:
    """Tool names documented in 06-mcp.md's tool-catalog tables (the
    Configure/Test/Ship/Observe/Govern/Ops sections between the "## Tool
    catalog" heading and the "## Usage example" heading)."""
    text = MCP_DOC.read_text()
    start = text.index("## Tool catalog")
    end = text.index("## Usage example")
    section = text[start:end]
    return set(_TOOL_ROW.findall(section))


def _stated_counts():
    """Yield (source, line_no, matched_number) for every "N tool(s)" figure
    in 06-mcp.md and README.md."""
    for path in (MCP_DOC, README_DOC):
        text = path.read_text()
        for i, line in enumerate(text.splitlines(), start=1):
            for m in _COUNT_IN_TEXT.finditer(line):
                yield (path.name, i, int(m.group(1)), line.strip())


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def registered_tool_names(mcp_client):
    resp = rpc(mcp_client, "tools/list")
    assert resp.status_code == 200
    return {t["name"] for t in resp.json()["result"]["tools"]}


def test_every_registered_tool_is_documented(registered_tool_names):
    """set(code tools) == set(documented tools) in 06-mcp.md.

    This is check (a): the drift that let voice-eval features and other new
    tools go undocumented (or a doc row survive a deleted tool) without
    anyone noticing.
    """
    documented = _documented_tool_names()
    code = registered_tool_names

    undocumented = sorted(code - documented)
    documented_but_gone = sorted(documented - code)

    assert not undocumented and not documented_but_gone, (
        "MCP tool catalog and docs/admin-portal/06-mcp.md have drifted.\n"
        f"  Registered but undocumented (add a row to 06-mcp.md): {undocumented}\n"
        f"  Documented but no longer registered (fix/remove the doc row): {documented_but_gone}"
    )


def test_doc_tool_counts_match_reality(registered_tool_names):
    """Every "N tool(s)" figure in 06-mcp.md / README.md must equal the
    real registered count.

    This is check (b) — the one that would have caught 06-mcp.md sitting at
    "39 tools" while 41 (now 44) were actually registered: a stated count
    is silent wrong information, not a crash, so nothing else here would
    have caught it.
    """
    real_count = len(registered_tool_names)
    mismatches = [
        (source, line_no, stated, line)
        for source, line_no, stated, line in _stated_counts()
        if stated != real_count
    ]
    assert not mismatches, (
        f"Doc tool-count drift: real registered count is {real_count}, but:\n"
        + "\n".join(
            f"  {source}:{line_no} states {stated!r} — {line!r}"
            for source, line_no, stated, line in mismatches
        )
    )


def test_every_mutating_route_is_mapped_or_justified():
    """Every discovered state-changing /admin/* route must appear in
    exactly one of ROUTE_TO_TOOL / PORTAL_ONLY.

    This is check (c) — the design principle is that a brand-new mutating
    route is a build failure, not a silent gap, until someone maps it to
    the MCP tool that covers it or declares (with a real reason) that it's
    portal-only on purpose.
    """
    discovered = _mutating_admin_routes()
    mapped = set(ROUTE_TO_TOOL)
    portal_only = set(PORTAL_ONLY)

    overlap = mapped & portal_only
    assert not overlap, (
        f"Route(s) listed in BOTH ROUTE_TO_TOOL and PORTAL_ONLY — pick one: {sorted(overlap)}"
    )

    unmapped = sorted(set(discovered) - mapped - portal_only)
    assert not unmapped, (
        "New (or renamed) mutating admin route(s) with no MCP-parity decision made:\n"
        + "\n".join(f"  {p}" for p in unmapped)
        + "\n\nAdd each to ROUTE_TO_TOOL (name the MCP tool whose action covers "
        "it) in tests/admin_mcp/test_surface_parity.py, or to PORTAL_ONLY with "
        "a specific one-line reason it's deliberately not exposed over MCP."
    )

    # Stale entries (route renamed/removed) are a maintenance nuisance, not a
    # drift-safety issue on their own — surfaced as an assertion message
    # rather than silently ignored so a rename gets cleaned up promptly.
    stale = sorted((mapped | portal_only) - set(discovered))
    assert not stale, (
        "ROUTE_TO_TOOL/PORTAL_ONLY reference route(s) that no longer exist "
        "(renamed or removed) — update the map:\n"
        + "\n".join(f"  {p}" for p in stale)
    )
