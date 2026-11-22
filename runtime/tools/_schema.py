"""Shared JSON-schema -> pydantic bridge for data-defined tools.

Used by the HTTP adapter (kind='http') and the external-MCP adapter
(tools/list inputSchema). Handles the common subset operators author:
a top-level object with `properties` + `required`. Anything exotic
falls through as `Any`.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Type

from pydantic import BaseModel, Field, create_model

_JSON_TYPE_TO_PY: Dict[str, Type[Any]] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def model_from_schema(name: str, schema: Dict[str, Any]) -> Type[BaseModel]:
    """Translate a JSON-schema dict into a Pydantic model.

    Handles the common subset we expect operators to author: a top-level
    object with `properties` + `required`. Anything more exotic (oneOf,
    nested refs) falls through as `Any` so the model still builds. We
    deliberately use `pydantic.create_model` here rather than
    `convert_to_openai_function` — the latter goes the other direction
    (model → schema) and would force us to round-trip.
    """
    if not isinstance(schema, dict):
        raise ValueError(f"argument_schema must be an object, got {type(schema).__name__}")

    # Root JSON-schema must describe an object — the adapter binds named
    # kwargs and a scalar root would have no fields to bind. We mirror the
    # admin-side validator (`tool_handlers._validate_argument_schema`) so a
    # bad row caught only at runtime fails the same way.
    schema_type = schema.get("type")
    if schema_type is not None and schema_type != "object":
        raise ValueError(f"argument_schema root type must be 'object', got {schema_type!r}")

    props = schema.get("properties") or {}
    if not isinstance(props, dict):
        raise ValueError("argument_schema.properties must be an object")

    required = set(schema.get("required") or [])
    fields: Dict[str, Any] = {}
    for prop_name, prop_schema in props.items():
        if not isinstance(prop_schema, dict):
            prop_schema = {}
        json_type = prop_schema.get("type", "string")
        py_type: Type[Any]
        if isinstance(json_type, list):
            # ["string","null"] etc — pick the first non-null.
            non_null = [t for t in json_type if t != "null"]
            py_type = _JSON_TYPE_TO_PY.get(non_null[0] if non_null else "string", Any)
        else:
            py_type = _JSON_TYPE_TO_PY.get(json_type, Any)
        description = prop_schema.get("description", "")
        if prop_name in required:
            fields[prop_name] = (py_type, Field(..., description=description))
        else:
            default = prop_schema.get("default", None)
            fields[prop_name] = (Optional[py_type], Field(default, description=description))

    # `create_model` requires a valid Python identifier; sanitize.
    model_name = "".join(c if c.isalnum() else "_" for c in name) or "ToolArgs"
    return create_model(model_name, **fields)  # type: ignore[call-overload]
