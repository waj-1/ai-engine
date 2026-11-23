"""The WAJ AI-engine admin MCP server.

FastMCP in stateless Streamable-HTTP mode with JSON responses: Cloud Run
autoscales the control-plane, so no in-memory session may outlive a request,
and plain JSON keeps the external HTTPS LB simple. Uses the SDK's default
streamable_http_path ("/mcp"); main.py registers this app at the literal
path "/mcp" via app.add_route (not app.mount), so the inner FastMCP app's
own routing never actually matches on path — see the comment there for why
Mount's trailing-slash redirect is unsafe here.

Deviation from the reference config: the SDK auto-enables DNS-rebinding
protection (a Host/Origin header allowlist meant for unauthenticated
localhost dev servers reachable from a browser) whenever `host` is left at
its "127.0.0.1" default. That allowlist only contains loopback variants, so
it would 421 every request through Cloud Run — including the real
production Host header and, in tests, TestClient's "testserver" — long
before MCPAuthMiddleware even runs. This server's actual trust boundary is
the verified-JWT middleware below, not the request's Host header, so DNS
rebinding protection is explicitly disabled instead of chasing an allowlist
of every legitimate Host value (custom domain, *.run.app, local dev).
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from admin_mcp.auth import MCPAuthMiddleware

# Lands in the caller's system prompt on initialize — the cross-tool
# conventions that don't fit any single tool description.
_INSTRUCTIONS = """\
Admin control-plane for the WAJ AI engine (agent config, evals, promotion, \
deploys, observability).

Lifecycle: create (templates, fork_profile) -> manage (update_profile, \
profile_attachments, prompts) -> dispatch (chat, trigger_eval_run) -> \
evaluate (eval_runs compare) -> ship (promote_profile) -> monitor \
(live_analytics, traces).

Conventions:
- `env` selects the environment branch of the config DB (staging|prod). Most \
tools default to env="staging" (the edit-then-promote workflow); pass \
env="prod" explicitly to read or change what production serves. `env` \
never means anything else: a row's own environment column is \
`row_environment`, a trace's environment tag is `trace_env`.
- Destructive actions dry-run by default: they return {"confirmed": false, \
"would": ...} and change nothing until re-called with confirm=true. \
promote_profile included: without confirm it returns a "what will change" \
preview.
- Pairs/sets of ids are passed as `ids` (or `run_ids` for eval runs).
- Large bodies are opt-in (include_results, include_content, \
include_series, fields); lists page with limit/offset.
- Long operations (eval runs, pytest, e2e, KB uploads) return a \
run_id/job_id immediately — poll with `jobs action=get` or the tool's own \
get action.
- Profile→MCP wiring: `mcp_attachments` (agent_profile_mcp_servers) is what \
the runtime tool resolver reads; the legacy `mcp_server_ids` label list is \
informational only.
- Operator docs are exposed as wajdocs:// resources (index, operate, \
configure, test, govern, deploy, mcp, architecture, data-model)."""

mcp = FastMCP(
    "waj-ai-engine",
    instructions=_INSTRUCTIONS,
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def build_mcp_asgi_app():
    """Import all tool/resource modules (registering them on `mcp`) and
    return the auth-wrapped ASGI app to mount."""
    # Imports are here (not module top) so `import admin_mcp.server` stays
    # cheap for the mint script/tests, and to avoid import cycles: tool
    # modules import `mcp` from this module.
    import admin_mcp.tools_configure  # noqa: F401
    import admin_mcp.tools_test  # noqa: F401
    import admin_mcp.tools_ship  # noqa: F401
    import admin_mcp.tools_observe  # noqa: F401
    import admin_mcp.tools_govern  # noqa: F401
    import admin_mcp.tools_ops  # noqa: F401
    import admin_mcp.tools_dispatch  # noqa: F401
    import admin_mcp.resources  # noqa: F401
    from admin_mcp.param_docs import apply_param_descriptions
    from admin_mcp.tools_ship import drop_unconfigured_deploy_tools
    drop_unconfigured_deploy_tools(mcp)
    apply_param_descriptions(mcp)

    return MCPAuthMiddleware(mcp.streamable_http_app())
