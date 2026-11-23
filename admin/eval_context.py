"""Extra run-context ids a deployment's agents read.

Every eval run, suite default and imported scenario can carry the engine's
own context ids (business, branch, customer, locale, ...). A plugin whose
agents read more ids (a record in its own system) lists them with the
`agent_catalog` hook's `eval_context_fields()`: `(key, label, placeholder)`
triples. They get a field on the suite form and the prompt A/B form, a
`ctx_<key>` field on run forms, a CSV column, and are forwarded to the agent
like the engine's own. A build with no plugin has none.
"""
from __future__ import annotations

from typing import Tuple


def eval_context_fields() -> Tuple[Tuple[str, str, str], ...]:
    from core.plugins import call_hook, load_hook

    out = []
    for found in call_hook(load_hook("agent_catalog"), "eval_context_fields"):
        out.extend(tuple(f) for f in found)
    return tuple(out)


def eval_context_keys() -> Tuple[str, ...]:
    return tuple(key for key, _label, _placeholder in eval_context_fields())


def run_context_from_form(form) -> dict:
    """{key: value} for the plugin's `ctx_<key>` run-form fields that are filled in."""
    out = {}
    for key in eval_context_keys():
        raw = (form.get(f"ctx_{key}") or "").strip()
        if raw:
            out[key] = raw
    return out


def business_field_label() -> str:
    """The label of the run forms' tenant picker ("Business" in a build whose
    plugin has businesses), or '' when no plugin provides one.

    The engine has no tenants of its own: in a build with no plugin the
    picker would list nothing, so the Testing screens hide it. A plugin
    whose agents serve tenants names them with the `agent_catalog` hook's
    `business_field_label()`; the first non-empty label wins."""
    from core.plugins import call_hook, load_hook

    for found in call_hook(load_hook("agent_catalog"), "business_field_label"):
        if found:
            return str(found)
    return ""
