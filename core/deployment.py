"""Deployment scopes: a build of the engine that serves only some of its agents.

By default one engine process serves every agent kind it has, and the admin
portal is a control-plane over two environments (staging and prod). A plugin
may also define *scopes*: a standalone build that serves one tenant's agents
under one public path prefix, with its own portal name and colours, against a
single environment. This module is the engine's whole view of that: which
scopes exist, which one (if any) this process runs as, and the questions the
rest of the engine asks of it ("which agent kinds do I serve?", "which
environment is this box?").

Scopes come from the ``deployment`` plugin hook (see ``core.plugins.HOOKS``):

* ``scopes()`` returns ``{key: DeploymentScope}``, every scope the plugin
  knows, so a full build can leave the other scopes' kinds alone and a scoped
  build can refuse the other scopes' paths.
* ``active_scope()`` returns the scope this process runs as, or None. It reads
  its own configuration (an env var, typically) at call time and raises
  ``ValueError`` on a value it does not recognise, which makes the engine
  refuse to boot (``main._validate_env_or_die``) instead of serving
  everything from a mistyped single-tenant deploy.
* ``blocked_path_prefixes(scope)`` (optional) returns the plugin's own route
  prefixes a scoped build must 404.
* ``scope_env_vars()`` (optional) names the env vars that select a scope, so
  a test harness can clear them and run a full build.

With no plugin there are no scopes: ``active_scope()`` is None and every
function here answers for a full build.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from types import ModuleType
from typing import Dict, List, Optional

from core.plugins import call_hook, load_hook


@dataclass(frozen=True)
class DeploymentScope:
    """One scoped build of the engine (pure data, no database coupling)."""

    key: str               # the scope's identifier, as its plugin configures it
    display_name: str      # human-readable product name (admin portal, logs)
    agent_kind: str        # the main agent's agent_kind
    title_kind: str        # its title-generation sub-agent kind
    suggestions_kind: str  # its follow-up-suggestions sub-agent kind
    portal_prefix: str     # public mount for its routes, rooted ("/tenant")
    # Further top-level agent kinds the scope owns beyond `agent_kind`. Gated,
    # seeded and served exactly like `agent_kind`; see scope_kinds().
    extra_kinds: tuple[str, ...] = ()
    # True when the scope's chat endpoint picks a demo tenant server-side: the
    # portal's Test panel then shows a note in place of the business picker.
    demo_tenant: bool = False
    # Admin-portal branding (admin/nav.py:brand()). The sidebar wordmark is
    # `brand_name`; the <title>/login heading is "<brand_name> · <brand_short>".
    # `brand_accent*` re-point the portal's --accent tokens (light + dark);
    # None keeps the default accent. `brand_mark` is the letter on the sidebar
    # tile (None = the plain accent tile).
    brand_name: str = "WAJ Engine"
    brand_short: Optional[str] = None
    brand_login_product: str = "WAJ AI Engine"
    brand_mark: Optional[str] = None
    brand_accent: Optional[str] = None
    brand_accent_soft: Optional[str] = None
    brand_accent_dark: Optional[str] = None
    brand_accent_soft_dark: Optional[str] = None
    brand_accent_fg_dark: Optional[str] = None


def _providers() -> List[ModuleType]:
    """The plugin modules that implement the ``deployment`` hook. A function
    so a test can stand in a provider of its own."""
    return load_hook("deployment")


def all_scopes() -> Dict[str, DeploymentScope]:
    """Every scope the configured plugins define, keyed by ``key``."""
    merged: Dict[str, DeploymentScope] = {}
    for scopes in call_hook(_providers(), "scopes"):
        merged.update(scopes or {})
    return merged


def active_scope() -> Optional[DeploymentScope]:
    """The scope this process serves, or None (a full build).

    Read at call time, never cached, so a deployment's configuration and a
    test's monkeypatch are both seen. A plugin's ``ValueError`` for an unknown
    value propagates.
    """
    for scope in call_hook(_providers(), "active_scope"):
        if scope is not None:
            return scope
    return None


def scope_env_vars() -> tuple[str, ...]:
    """The env vars the plugins read to pick a scope (see the module doc)."""
    names: list[str] = []
    for found in call_hook(_providers(), "scope_env_vars"):
        names.extend(n for n in (found or ()) if n not in names)
    return tuple(names)


def scoped_build() -> bool:
    """True when this process serves a single scope."""
    return active_scope() is not None


def scope_kinds(scope: DeploymentScope) -> frozenset[str]:
    """The agent_kind values owned by ``scope`` (main + title + suggestions + extras)."""
    return frozenset(
        {scope.agent_kind, scope.title_kind, scope.suggestions_kind, *scope.extra_kinds}
    )


def all_scope_kinds() -> frozenset[str]:
    """Every agent kind owned by ANY scope. A full build treats them as not
    part of its own catalog (it neither seeds nor lists them)."""
    return frozenset().union(*(scope_kinds(s) for s in all_scopes().values()))


def blocked_path_prefixes(scope: DeploymentScope) -> tuple[str, ...]:
    """The request paths a build running as ``scope`` answers with a 404.

    The generic custom-agent API (``/v1/``) is not part of any scope; every
    OTHER scope's portal prefix is refused (cross-scope isolation); and each
    plugin adds the prefixes of its own routes the scope does not serve. The
    scope's own prefix is never in the list.
    """
    prefixes: list[str] = ["/v1/"]
    for extra in call_hook(_providers(), "blocked_path_prefixes", scope):
        prefixes.extend(p for p in (extra or ()) if p not in prefixes)
    for other in all_scopes().values():
        if other.key == scope.key:
            continue
        # `<prefix>/chat/stream` is subsumed by the bare prefix (startswith
        # match); kept so both surfaces are visibly in the list.
        prefixes.append(other.portal_prefix)
        prefixes.append(f"{other.portal_prefix}/chat/stream")
    return tuple(prefixes)


def mount_prefix() -> str:
    """The prefix the scope's public routes (the duplex voice socket among
    them) mount under: the active scope's ``portal_prefix``, or ``""``."""
    scope = active_scope()
    return scope.portal_prefix if scope else ""


def demo_tenant_build() -> bool:
    """True when the active scope resolves its own demo tenant (see
    ``DeploymentScope.demo_tenant``); False on a full build."""
    scope = active_scope()
    return bool(scope and scope.demo_tenant)


def serves_kind(kind: str) -> bool:
    """False iff a scope is active and ``kind`` is not one of its kinds."""
    scope = active_scope()
    return scope is None or kind in scope_kinds(scope)


def serves_agents_portal() -> bool:
    """True when this process serves the AI-agents demo portal (``/agents``):
    a full build, or a scope that owns the demo's kinds."""
    return serves_kind("agents_support")


def single_env_deployment() -> Optional[str]:
    """The one environment this deployment serves, or None on the control-plane.

    A scoped build is **not** a control-plane: it runs one engine against one
    AI Supabase, and every row it seeds carries its own ``ENVIRONMENT``. There
    is no second branch to switch to, so the admin portal's two-env defaults
    are wrong there: they open on an environment that has, by construction,
    zero profiles and zero traces. (A box running ``ENVIRONMENT=staging`` once
    showed "no agents, no traces" on the Live Monitor's prod default while
    1,200+ real traces sat one query-param away.) Env is read at call time.
    """
    if not scoped_build():
        return None  # full build: keep the two-env control-plane defaults
    raw = (os.getenv("ENVIRONMENT") or "").strip().lower()
    if raw in ("production", "prod"):
        return "prod"
    # staging, dev or unset: the switcher's envs don't describe this box. Fall
    # back to staging, which is what the seeders use when ENVIRONMENT isn't prod.
    return "staging"
