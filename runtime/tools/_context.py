"""context_fields resolution — the credential-injection half of MCP tools.

A context_fields map ties a tool argument name to either a request-context
reference ("$ctx.<key>") or a literal. Args named in the map are stripped
from every model-facing schema (the LLM never sees them) and injected
server-side at call time. Precedence when several maps apply:
server-level < bundle-member-level < tool-level (merge left to right).
"""
from __future__ import annotations

import copy
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

CTX_REF_PREFIX = "$ctx."


def resolve_context_ref(ref: Any, ctx: Dict[str, Any]) -> Any:
    """"$ctx.key" -> ctx.get("key"); anything else is a literal."""
    if isinstance(ref, str) and ref.startswith(CTX_REF_PREFIX):
        return ctx.get(ref[len(CTX_REF_PREFIX):])
    return ref


def effective_context_fields(*maps: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge maps left->right; later (more specific) maps win; None skipped."""
    merged: Dict[str, Any] = {}
    for m in maps:
        if m:
            merged.update(m)
    return merged


def strip_context_args(schema: Dict[str, Any], context_fields: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of `schema` with injected arg names removed from properties/required."""
    out = copy.deepcopy(schema or {})
    props = out.get("properties")
    if isinstance(props, dict):
        for name in context_fields:
            props.pop(name, None)
    required = out.get("required")
    if isinstance(required, list):
        out["required"] = [r for r in required if r not in context_fields]
    return out


def build_context_kwargs(context_fields: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the map against request context.

    A $ctx ref whose key is absent is OMITTED (not sent as None) — MCP/HTTP
    servers treat explicit nulls differently from missing args, and a missing
    credential should fail loudly server-side rather than half-authenticate.
    """
    kwargs: Dict[str, Any] = {}
    for arg, ref in (context_fields or {}).items():
        if isinstance(ref, str) and ref.startswith(CTX_REF_PREFIX):
            key = ref[len(CTX_REF_PREFIX):]
            if key not in ctx:
                logger.warning("context_fields: ctx key %r missing for arg %r; omitting", key, arg)
                continue
            kwargs[arg] = ctx[key]
        else:
            kwargs[arg] = ref
    return kwargs
