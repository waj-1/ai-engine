"""FastAPI endpoint → Pydantic request body schema extraction.

Each `agent_profiles.endpoint_path` (e.g. `/sales-chat/stream`) maps to
one FastAPI route on the app. That route is gated by a Pydantic model
declared on the handler signature — e.g. `request: ChatRequest`.
The admin portal wants to surface "what this profile expects in the
request body" without the operator having to grep main.py.

We walk `app.routes` once, pick the body parameter off each route's
dependant, and call `.model_json_schema()` to produce a JSON Schema
dict. Result is cached on first access — the route table is fixed at
process boot so a single walk is enough.

Public API:
    get_input_schema_for_endpoint(endpoint_path) -> Optional[Dict]
    list_endpoint_summaries() -> List[Dict]   # debug aid
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_SCHEMA_CACHE: Optional[Dict[str, Dict[str, Any]]] = None


def _walk_routes() -> Dict[str, Dict[str, Any]]:
    """Iterate FastAPI routes and pull the Pydantic body schema for each.

    Returns a dict keyed by `(method, path)` AND by `path` alone — the
    profile only knows its endpoint_path, so we let lookups fall back to
    the path-only key when there's exactly one method bound there. Both
    keys point at the same dict so callers can mutate freely.
    """
    out: Dict[str, Dict[str, Any]] = {}
    try:
        # Import lazily so this module can be imported by tests without
        # paying the cost of booting the full FastAPI app at import time.
        from main import app  # noqa: WPS433 — see docstring
    except Exception as exc:
        logger.warning("endpoint_schemas: cannot import main.app (%s)", exc)
        return out

    for route in getattr(app, "routes", []) or []:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if not path or not methods:
            continue
        body_model = _body_model_for(route)
        if body_model is None:
            entry = {
                "path": path,
                "methods": sorted(methods),
                "model_name": None,
                "schema": None,
                "fields": [],
            }
        else:
            try:
                schema = body_model.model_json_schema()
            except Exception as exc:
                logger.warning(
                    "endpoint_schemas: model_json_schema failed for %s on %s (%s)",
                    body_model.__name__, path, exc,
                )
                schema = {}
            entry = {
                "path": path,
                "methods": sorted(methods),
                "model_name": body_model.__name__,
                "schema": schema,
                "fields": _flatten_fields(schema),
            }
        # Path-only key wins when there's only one route per path; if a
        # later iteration overwrites it we just keep the freshest binding
        # (operators typically only need GET-or-POST per endpoint, not
        # both).
        out[path] = entry
        for m in entry["methods"]:
            out[f"{m} {path}"] = entry
    return out


def _body_model_for(route: Any) -> Optional[Any]:
    """Extract the Pydantic class declared as the request body, if any.

    FastAPI stores body parameters on `route.dependant.body_params` as a
    list of `ModelField`. We grab the first one's `type_` (the Pydantic
    class) which is the shape the route is enforcing. Routes without a
    body model (e.g. /health, /downloads/{filename}) return None.
    """
    dependant = getattr(route, "dependant", None)
    body_params = getattr(dependant, "body_params", None) or []
    for param in body_params:
        # FastAPI 0.100+ on Pydantic v2 (e.g. 0.136 here) drops the legacy
        # ModelField.type_/.outer_type_ — the annotated class now lives on
        # `field_info.annotation`. Fall back to the old attributes so this
        # keeps working if FastAPI is pinned to an older release.
        field_info = getattr(param, "field_info", None)
        candidate = (
            getattr(field_info, "annotation", None)
            or getattr(param, "type_", None)
            or getattr(param, "outer_type_", None)
        )
        if candidate is None:
            continue
        if _is_pydantic_model(candidate):
            return candidate
    return None


def _is_pydantic_model(candidate: Any) -> bool:
    try:
        from pydantic import BaseModel
    except Exception:
        return False
    return isinstance(candidate, type) and issubclass(candidate, BaseModel)


def _flatten_fields(schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Render the JSON Schema as a list of ``{name, type, required,
    description, default}`` dicts so the template can iterate without
    knowing JSON-Schema-isms. Required-ness is read off the top-level
    ``required`` list; everything else falls back to sensible defaults.

    We don't recurse into nested objects — operators want a glance, not a
    tree. The raw schema is still passed to the template for a collapsible
    "show full JSON Schema" block.
    """
    if not isinstance(schema, dict):
        return []
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    rows: List[Dict[str, Any]] = []
    for name, meta in properties.items():
        if not isinstance(meta, dict):
            continue
        rows.append({
            "name": name,
            "type": _humanise_type(meta),
            "required": name in required,
            "description": meta.get("description") or "",
            "default": meta.get("default"),
            "pattern": meta.get("pattern"),
            "enum": meta.get("enum"),
        })
    # Required fields first, then alpha within each bucket so the card
    # always lists business_id / customer_id / etc. above the trailing
    # optional flags.
    rows.sort(key=lambda r: (0 if r["required"] else 1, r["name"]))
    return rows


def _humanise_type(meta: Dict[str, Any]) -> str:
    """Convert a JSON-Schema field-meta dict into a short human label.

    The defaults are intentionally lossy (object/array don't expand) —
    the collapsible raw-schema block in the template covers the long
    tail.
    """
    if "anyOf" in meta:
        parts = []
        for sub in meta["anyOf"]:
            if isinstance(sub, dict):
                t = sub.get("type")
                if t:
                    parts.append(t)
        if parts:
            return " | ".join(dict.fromkeys(parts))
    t = meta.get("type")
    if t:
        return t if isinstance(t, str) else "/".join(t)
    if "$ref" in meta:
        return meta["$ref"].rsplit("/", 1)[-1]
    return "any"


def get_input_schema_for_endpoint(endpoint_path: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return the cached schema entry for `endpoint_path`, or None.

    The cache is built lazily on first call so test code can monkey-patch
    `main.app` before the walk runs. Callers should treat None as
    "endpoint not registered with FastAPI" — usually a typo in
    agent_profiles.endpoint_path or a profile pointed at a route that
    hasn't been deployed yet.
    """
    global _SCHEMA_CACHE
    if not endpoint_path:
        return None
    if _SCHEMA_CACHE is None:
        _SCHEMA_CACHE = _walk_routes()
    return _SCHEMA_CACHE.get(endpoint_path)


def list_endpoint_summaries() -> List[Dict[str, Any]]:
    """Return one entry per FastAPI route — debug helper for tests."""
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None:
        _SCHEMA_CACHE = _walk_routes()
    seen: List[Dict[str, Any]] = []
    seen_paths: set = set()
    for key, entry in _SCHEMA_CACHE.items():
        # The cache double-keys "METHOD /path" and "/path"; dedupe by id.
        if id(entry) in seen_paths:
            continue
        seen_paths.add(id(entry))
        seen.append(entry)
    return seen


def reset_cache() -> None:
    """Drop the cached route walk. Used in tests after patching app.routes."""
    global _SCHEMA_CACHE
    _SCHEMA_CACHE = None
