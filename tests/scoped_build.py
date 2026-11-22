"""Run a test as a scoped build (core/deployment.py) without any plugin.

`use_scope(monkeypatch, scope)` stands in a deployment provider that defines
`scope` (plus `others`) and runs as `scope`; `scope=None` is a full build
that still knows the scopes. Replaces whatever the configured plugins
provide, for the length of the test.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Iterable, Optional

import core.deployment as deployment
from core.deployment import DeploymentScope

# The plugins' own providers, before any test stands one in.
_PLUGIN_PROVIDERS = deployment._providers


def make_scope(key: str = "orchard", **overrides) -> DeploymentScope:
    fields = dict(
        key=key,
        display_name=f"{key.title()} Assistant",
        agent_kind=key,
        title_kind=f"{key}_title",
        suggestions_kind=f"{key}_suggestions",
        portal_prefix=f"/{key}",
    )
    fields.update(overrides)
    return DeploymentScope(**fields)


def use_scope(monkeypatch, scope: Optional[DeploymentScope], *,
              others: Iterable[DeploymentScope] = (),
              blocked: tuple = ()) -> None:
    known = {s.key: s for s in (*others, *([scope] if scope else []))}
    provider = SimpleNamespace(
        scopes=lambda: dict(known),
        active_scope=lambda: scope,
        blocked_path_prefixes=lambda _scope: blocked,
    )
    monkeypatch.setattr(deployment, "_providers", lambda: [provider])


def full_build(monkeypatch) -> None:
    """A full build, as configured: the plugins' own providers (undoing a
    `use_scope` earlier in the test), with every env var they read to pick a
    scope cleared. The scopes the plugins define stay known."""
    monkeypatch.setattr(deployment, "_providers", _PLUGIN_PROVIDERS)
    for name in deployment.scope_env_vars():
        monkeypatch.delenv(name, raising=False)


# The fictional AI-agents demo (/agents) run as a standalone scoped build: the
# demo's four kinds, nothing else. Its title/suggestions kinds are reserved
# names only (the demo has no profile for them).
AGENTS_DEMO = make_scope(
    "agents",
    display_name="AI Agents Demo",
    agent_kind="agents_support",
    title_kind="agents_title",
    suggestions_kind="agents_suggestions",
    portal_prefix="/agents",
    extra_kinds=("agents_inquiries", "agents_collection", "agents_sales"),
    brand_short="AI Agents Demo",
)

# Another tenant's scoped build (a fictional orchard co-op), for "what the
# others must not see".
ORCHARD = make_scope("orchard")


def use_agents_demo(monkeypatch) -> None:
    use_scope(monkeypatch, AGENTS_DEMO, others=[ORCHARD])


def use_orchard(monkeypatch) -> None:
    use_scope(monkeypatch, ORCHARD, others=[AGENTS_DEMO])
