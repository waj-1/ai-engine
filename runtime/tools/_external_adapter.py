"""External MCP server -> LangChain StructuredTools.

`build_mcp_tools(server_row, ctx)` discovers tools via a (cached) MCP
tools/list, strips context_fields-mapped args from every inputSchema so the
LLM never sees tenant credentials, and returns one StructuredTool per tool
whose func injects those args server-side and calls tools/call through a
pooled client. Failures degrade to error strings the model can read —
never exceptions — matching the HTTP adapter's posture.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from langchain_core.tools import StructuredTool

from runtime.tools._context import build_context_kwargs, strip_context_args
from runtime.tools._http_adapter import _classify_endpoint
from runtime.tools._mcp_adapter import MCPError, get_pooled_client, list_tools_cached
from runtime.tools._schema import model_from_schema

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}


def _local_endpoints_allowed() -> bool:
    # Local dev + the demo compose run MCP servers on loopback/service hosts.
    return os.getenv("MCP_ALLOW_LOCAL_ENDPOINTS", "").strip().lower() in _TRUTHY


def build_mcp_tools(
    server_row: Dict[str, Any],
    ctx: Dict[str, Any],
    tool_subset: Optional[List[str]] = None,
    *,
    transport: Any = None,
) -> List[StructuredTool]:
    endpoint = server_row.get("endpoint") or ""
    server_name = server_row.get("name") or endpoint
    if not endpoint:
        logger.warning("mcp server %r has no endpoint; skipping", server_name)
        return []

    classification = _classify_endpoint(endpoint)
    if classification == "blocked" and not _local_endpoints_allowed():
        logger.warning(
            "mcp server %r endpoint %s is loopback/link-local; blocked "
            "(set MCP_ALLOW_LOCAL_ENDPOINTS=1 for local dev)", server_name, endpoint,
        )
        return []

    # Bearer secrets only travel to public endpoints — same rule as http tools.
    auth_token: Optional[str] = None
    auth_env_var = server_row.get("auth_env_var")
    if auth_env_var:
        token = os.getenv(auth_env_var)
        if token and classification == "public":
            auth_token = token
        elif token:
            logger.warning(
                "mcp server %r: not forwarding %s to non-public endpoint", server_name, auth_env_var,
            )

    try:
        listed = list_tools_cached(endpoint, auth_token, transport=transport)
    except MCPError as exc:
        logger.warning("mcp server %r: tools/list failed (%s); no tools resolved", server_name, exc)
        return []

    context_fields: Dict[str, Any] = server_row.get("context_fields") or {}
    subset = set(tool_subset) if tool_subset else None

    tools: List[StructuredTool] = []
    for spec in listed:
        name = spec.get("name")
        if not name or (subset is not None and name not in subset):
            continue
        llm_schema = strip_context_args(spec.get("inputSchema") or {}, context_fields)
        try:
            args_model = model_from_schema(name, llm_schema)
        except ValueError as exc:
            logger.warning("mcp server %r tool %r: bad inputSchema (%s); skipping", server_name, name, exc)
            continue

        def _make_call(tool_name: str):
            def _call(**kwargs: Any) -> str:
                # Drop Nones (unset optionals) then inject credentials last so
                # they always win over anything the model produced.
                payload = {k: v for k, v in kwargs.items() if v is not None}
                payload.update(build_context_kwargs(context_fields, ctx))
                try:
                    client = get_pooled_client(endpoint, auth_token, transport=transport)
                    result = client.call_tool(tool_name, payload)
                except MCPError as exc:
                    return json.dumps({"error": str(exc)})
                except Exception as exc:  # untrusted external server — never let it crash the turn
                    return json.dumps({"error": f"MCP tool call failed: {exc}"})
                if isinstance(result, (dict, list)):
                    return json.dumps(result, ensure_ascii=False)
                return str(result)
            return _call

        tools.append(StructuredTool.from_function(
            func=_make_call(name),
            name=name,
            description=spec.get("description") or name,
            args_schema=args_model,
        ))
    return tools
