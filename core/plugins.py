"""Plugin loading: how product code attaches to the generic engine.

The engine has a few seams where a product needs to add something of its own:
routes, voice-agent builders, boot seeders, prompt seeds. At each seam the
engine calls `load_hook("<name>")`, which imports `<plugin>.<name>` for every
package listed in `engine_plugins.PLUGINS`. A hook module does its work at
import time (registering with a registry) or exposes functions the caller
uses; the engine itself never names a product package.

A plugin that does not implement a hook is skipped. Anything else that goes
wrong while importing a hook is NOT swallowed: a product route that silently
fails to load is an outage that looks like a healthy boot.

Hooks (``<plugin>.<hook>`` modules) and what the engine reads from them:

``app``
    Product routes and boot-time behaviour (``register(app)``,
    ``required_env_vars``, ``validate_env``, ``on_startup``,
    ``lifespan_contexts`` — async context managers held while the app
    serves, e.g. a background sweep — ``health_issues``, …). Read by
    ``main``.
``voice_builders``
    Registers duplex-voice agent builders with
    ``runtime.agents.voice_registry`` at import time, including each kind's
    handshake needs (auth, a second credential, client context keys).
    ``ask_agent_subject()`` names who the realtime model's ask_agent tool
    description says handles the caller's request.
``prompt_seeds`` / ``data_source_seeds``
    The prompt slots and knowledge sources seeded at boot.
``sensitive_content``
    ``sensitive_agent_kinds()``: agent kinds whose traffic carries data that
    must not leave the process in telemetry. Their traces export redacted,
    their duplex calls are never recorded and their real conversations are
    never sampled into evals (``core.sensitive_content``).
``integrations``
    ``integrations()``: extra cards for the MCP & Tools screen's
    integrations list (``admin.integrations_registry``);
    ``deployed_env_vars()``: extra env var names a deployed service mirrors
    to /admin/env (``admin.seed_env_vars``).
``search``
    How the portal's global search links people to conversations
    (``admin.search_sources``): ``session_record_keys()`` — session-metadata
    keys holding an external record id, searchable by exact value — and
    ``customer_record_links()`` — ``(customers column, metadata key, person
    label)`` triples.
``portal_display``
    How the portal displays the plugin's own things: ``message_markdown(meta)``
    renders a structured assistant message on the Conversations screen,
    ``non_chat_agent_kinds()`` names one-shot kinds with no chat transcript,
    ``display_acronyms()`` adds acronyms for humanized names.
"""
from __future__ import annotations

import importlib
import logging
from types import ModuleType
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

# The hook modules a plugin may implement, and what each supplies. Documentation
# for plugin authors: `load_hook` accepts any name, and a plugin implements only
# the hooks it needs.
HOOKS: dict[str, str] = {
    "app": (
        "Routes and app wiring, called from main.py: register(app), "
        "required_env_vars(scope), validate_env(env, scope), "
        "boot_seeders_after_models(), skip_boot_seeders(), on_startup(app), "
        "lifespan_contexts(), health_issues(), validation_error_response(), "
        "rate_limit_exempt_paths() (server-to-server routes the per-IP limiter "
        "skips), and cors_origins(): extra browser origins allowed at request "
        "time (read per request, so a plugin may refresh the set in the "
        "background)."
    ),
    "deployment": (
        "Deployment scopes (core/deployment.py): scopes() returns every scoped "
        "build the plugin defines; active_scope() the one this process runs "
        "as, or None, raising ValueError on an unknown configuration (boot "
        "refuses); blocked_path_prefixes(scope) the plugin's own route "
        "prefixes that build answers with a 404 (the deployment gate); "
        "deploy_repo(): the default repository the portal's deploy buttons dispatch, "
        "container_app() and resource_group(): the default Azure Container App "
        "and resource group its rollback picker reads (admin/deploy_handlers.py)."
    ),
    "channels": (
        "Channel kinds and default channels (core/channel_kinds.py): "
        "channel_kinds() {kind: label}, tenant_channel_kinds() (kinds whose "
        "channel belongs to one business), full_build_channel_kinds() (kinds a "
        "scoped build hides), default_channels() (rows seeded at boot)."
    ),
    "voice_builders": (
        "Duplex-voice builders per agent kind (runtime/agents/voice_registry.py); "
        "ask_agent_subject(): who the realtime providers' ask_agent description "
        "says handles the request, default \"the agent\" (voice/live/base.py)."
    ),
    "voice_capture": (
        "dictation_capture(): the duplex endpoint's server-side dictation "
        "collector (voice/live/dictation.py); none means the capture stays closed."
    ),
    "prompt_seeds": "prompt_specs(): the prompt slots seeded at boot (admin/seed_prompts.py).",
    "data_source_seeds": (
        "data_source_specs(), superseded_names(): knowledge sources seeded at "
        "boot (admin/seed_data_sources.py); kb_source_tags(): extra importer "
        "source tags -> source name (scripts/_kb_target.py)."
    ),
    "sensitive_content": (
        "sensitive_agent_kinds(): kinds whose traces export redacted, whose duplex "
        "calls are never recorded and whose chats are never sampled into evals "
        "(core/sensitive_content.py)."
    ),
    "integrations": (
        "integrations(): extra MCP & Tools integration cards; deployed_env_vars(): "
        "extra env names mirrored to /admin/env (admin/integrations_registry.py, "
        "admin/seed_env_vars.py)."
    ),
    "search": (
        "Global search (admin/search.py, admin/search_sources.py): people_sources() "
        "-> [(key, label, fn(ctx))] extra people sources; session_record_keys() and "
        "customer_record_links() link people to sessions by an external record id."
    ),
    "portal_display": (
        "How the portal displays the plugin's own things: message_markdown(meta) "
        "for a structured session message, non_chat_agent_kinds() (kinds with no "
        "chat transcript to link), display_acronyms() (admin/screens/observability.py, "
        "admin/testing_view.py)."
    ),
    "agent_catalog": (
        "The deployment's own agent kinds for pickers and the Testing screens: "
        "catalog_kinds(), kind_labels(), system_kinds(), seeded_kinds(), "
        "suite_kinds(), live_chat_kinds(), extra_eval_profile_kinds(), "
        "default_expectations(), template_agent_kind(), picker_kinds(), kb_kinds(), "
        "chart_kinds(), kind_colors(), background_kinds(), "
        "default_kind(), eval_context_fields() (extra run-context ids, admin/eval_context.py), not_chat_endpoints(), endpoint_channels(), tenantless_kinds(), "
        "topic_sets() (admin/agent_kinds.py, core/conversation_insights.py)."
    ),
    "eval_suite_seeds": (
        "eval_suite_specs(): the eval suites seeded at boot; judge_profile_kinds(): "
        "kinds whose profile model is the preferred judge (admin/seed_eval_suites.py)."
    ),
    "eval_runners": (
        "runners() -> {runner_kind: runner}, scorers() -> {assert_type: scorer}: eval "
        "runner kinds beyond the engine's chat and voice_live (admin/services/eval_runners.py)."
    ),
    "prompt_rules": (
        "size_budgets(), bilingual_rules(), forbidden_phrases(), count_parity_prompt_names(): "
        "the rules core/prompt_validator.py applies to a named prompt."
    ),
    "drift_checks": (
        "prompt_checks(), config_checks(), model_config_checks(), tool_subset_checks(), "
        "info_checks(), persona_checks(): what scripts/check_profile_drift.py audits."
    ),
    "tool_factories": (
        "tool_factories() -> [(agent_kind, source_file, module, factory, kwargs)], "
        "write_tool_names(): the agents' Python tool pools (admin/sync_tools.py)."
    ),
    "eval_checks": (
        "check_types() -> {type: fn(check, turn)}, facts(business_id, user_token), "
        "status_tool_labels(): eval check types beyond the generic ones (admin/services/eval_checks.py); "
        "check_labels() -> {type: label}: how a failed one reads (admin/testing_view.py)."
    ),
    "model_seeds": "model_rows(): agent_models rows seeded for the plugin's own agents (admin/seed_agent_models.py).",
    "voice_scenario_seeds": (
        "voice_scenario_specs(): starter live-voice scenarios seeded at boot "
        "(admin/seed_voice_scenarios.py)."
    ),
}


def plugin_names() -> tuple[str, ...]:
    """Read at call time, so a test can swap the list."""
    import engine_plugins

    return tuple(engine_plugins.PLUGINS)


def has_product() -> bool:
    """True when this build carries a product plugin.

    What the shared seeders and the Testing screens ask before offering one
    deployment's product agents, models, knowledge sources or test suites: a
    build with no plugin has none of them. Its own function (not a bare
    `plugin_names()` check) so a test of generic logic that uses product
    kinds as sample data can say "pretend a product is present" without
    naming a plugin package that `load_hook` would then try to import.
    """
    return bool(plugin_names())


def load_hook(hook: str) -> List[ModuleType]:
    """Import `<plugin>.<hook>` for every configured plugin; return the modules.

    Missing hook module -> skipped. Missing plugin PACKAGE, or any error raised
    inside the hook, propagates.
    """
    modules: List[ModuleType] = []
    for name in plugin_names():
        target = f"{name}.{hook}"
        try:
            modules.append(importlib.import_module(target))
        except ModuleNotFoundError as exc:
            if exc.name != target:
                # The plugin package itself, or something the hook imports, is
                # missing: a broken build, not an unimplemented hook.
                raise
            logger.debug("plugin %s has no %s hook", name, hook)
    return modules


def call_hook(modules: List[ModuleType], name: str, *args: Any) -> List[Any]:
    """Call `module.<name>(*args)` on every plugin module that defines it;
    return the results in plugin order. A plugin that does not define the
    function is skipped; an exception from one that does propagates."""
    results: List[Any] = []
    for module in modules:
        fn = getattr(module, name, None)
        if callable(fn):
            results.append(fn(*args))
    return results


def optional_import(dotted: str) -> Optional[ModuleType]:
    """Import a module that only some builds ship; None when it is absent.

    For an engine feature with a product-specific half: a generic screen or
    check that can do more when a product's module is present. The caller
    MUST handle None by saying the feature is not part of this build — this
    is not a way to hide a required dependency. An error raised INSIDE the
    module (a real bug) still propagates; only the module itself being absent
    yields None.
    """
    try:
        return importlib.import_module(dotted)
    except ModuleNotFoundError as exc:
        if exc.name and (dotted == exc.name or dotted.startswith(exc.name + ".")):
            return None
        raise
