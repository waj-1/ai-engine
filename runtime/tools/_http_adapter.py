"""Generic HTTP-call adapter for operator-authored tools.

`build_http_tool(row)` turns an `agent_tools` row with `kind='http'`
into a `langchain_core.tools.StructuredTool` so the agent doesn't need
to know whether a tool is a hand-coded Python function or a UI-authored
HTTP endpoint — both look like LangChain tools at the binding layer.

The adapter is intentionally SYNCHRONOUS: LangChain `StructuredTool.func`
is a sync callable, and the LangGraph tool node already handles
off-thread execution for us. Don't switch to httpx.AsyncClient here.

Failure modes are deliberately non-fatal: a bad endpoint or non-2xx
status returns a string the LLM can read and self-correct from, rather
than raising and crashing the agent turn. Schema-parse failures DO
raise so the caller (the tool resolver) can skip the broken row.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from langchain_core.tools import StructuredTool

from runtime.tools._schema import model_from_schema as _model_from_schema

logger = logging.getLogger(__name__)


# SSRF guard for operator-authored HTTP tools. An `agent_tools` row carries an
# arbitrary `endpoint_url` plus an optional `auth_env_var` whose secret we'd
# attach as a Bearer token — so an unconstrained call lets a tool reach the
# cloud-metadata endpoint (169.254.169.254) or loopback and exfiltrate a
# process secret. We:
#   * hard-block loopback + link-local (metadata) targets, and
#   * never forward the auth token to a private/loopback target.
# RFC1918 is intentionally allowed (legit internal-service tools), just
# without secret forwarding. Residual risk: DNS rebinding between our resolve
# and httpx's — acceptable given the input is superuser-authored config.
_BLOCKED_NETS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local incl. cloud metadata
    ipaddress.ip_network("fe80::/10"),
)
_PRIVATE_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)

# Cap the body handed back to the LLM. A huge 2xx response would otherwise be
# buffered into the process and forwarded verbatim — burning memory and tokens.
_MAX_RESPONSE_CHARS = 100_000


def _resolved_ips(host: str) -> List[ipaddress._BaseAddress]:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []
    out: List[ipaddress._BaseAddress] = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            pass
    return out


def _allowlisted_hosts() -> set:
    """Operator-set exact host:port exceptions to the SSRF block.

    Read per call (not cached at import) so ops/tests can set it without a
    process restart. Kept exact-match on purpose: a pattern or CIDR here
    would quietly re-open the SSRF hole this guard exists to close. Applies
    uniformly to every entry in `_BLOCKED_NETS`, not just loopback — do NOT
    add cloud-metadata (169.254.x.x) or other sensitive blocked-net
    addresses here, as an exact match on them defeats the block for that
    entry."""
    raw = os.getenv("HTTP_TOOL_ALLOW_HOSTS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _classify_endpoint(url: str) -> str:
    """Classify the endpoint host as 'blocked' (loopback/link-local),
    'private' (RFC1918/ULA, or an operator-allowlisted loopback exception —
    allowed but no secret forwarding) or 'public'. A host we can't resolve is
    treated as 'public'; httpx will then fail the connection naturally.

    `HTTP_TOOL_ALLOW_HOSTS` (comma-separated exact host:port entries) lets an
    operator carve out a deliberate exception to the loopback block for local
    rehearsal against a mock — matched entries are 'private', never 'public',
    so `auth_env_var` secrets still aren't forwarded to them."""
    from demo_mode import northwind, settings as demo_settings

    if demo_settings.enabled():
        # The public demo: its own in-process tool server is the one exception
        # (answered without the network); everything else that is local,
        # private or internal is blocked, and no allow-list widens that.
        # A public address is reported as private too: the call may go out,
        # but no secret this process holds (`auth_env_var`) is ever sent
        # with it, since a visitor could point a tool at their own server
        # and name the model key as its credential.
        if northwind.handles(url):
            return "private"
        verdict = _classify_host(url, allowlist=False, strict=True)
        return "blocked" if verdict == "private" else ("private" if verdict == "public" else verdict)
    return _classify_host(url, allowlist=True)


def _classify_host(url: str, *, allowlist: bool, strict: bool = False) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not host:
        return "public"
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        # Malformed port text (e.g. "host:8080.evil.com") — `.port` raises
        # rather than returning None. Treat as "no port"; it can never match
        # an allowlist entry (those are always exact "host:port"), and
        # classification falls through to the normal host-based checks below.
        port = None
    if allowlist and f"{host.lower()}:{port}" in _allowlisted_hosts():
        # Deliberate operator exception (demo mocks on loopback). PRIVATE, not
        # public: the call may proceed but process secrets are still withheld.
        return "private"
    try:
        ips: List[ipaddress._BaseAddress] = [ipaddress.ip_address(host)]
    except ValueError:
        ips = _resolved_ips(host)
    if not ips:
        return "public"
    if any(any(ip in net for net in _BLOCKED_NETS) for ip in ips):
        return "blocked"
    if strict and any(not ip.is_global for ip in ips):
        # Shared (100.64/10), reserved and every other non-internet range.
        return "blocked"
    if any(ip.is_private or any(ip in net for net in _PRIVATE_NETS) for ip in ips):
        return "private"
    return "public"


def _stringify_response(resp: httpx.Response) -> str:
    """Best-effort body → string for the LLM."""
    try:
        return json.dumps(resp.json(), ensure_ascii=False)
    except Exception:
        return resp.text or ""


def build_http_tool(
    row: Dict[str, Any],
    *,
    context_fields: Optional[Dict[str, Any]] = None,
    ctx: Optional[Dict[str, Any]] = None,
) -> StructuredTool:
    """Construct a LangChain StructuredTool from an agent_tools row.

    Raises `ValueError` if the argument_schema can't be turned into a
    Pydantic model — the caller (resolver) is expected to catch that
    and skip the row with a warning rather than killing the agent.
    """
    from runtime.tools._context import build_context_kwargs, strip_context_args

    python_name: str = row["python_name"]
    description: str = row.get("description") or python_name
    endpoint_url: str = row.get("endpoint_url") or ""
    if not endpoint_url:
        raise ValueError(f"http tool {python_name!r} has no endpoint_url")
    http_method: str = (row.get("http_method") or "POST").upper()
    timeout_seconds: int = int(row.get("timeout_seconds") or 30)
    auth_env_var: Optional[str] = row.get("auth_env_var") or None
    schema: Dict[str, Any] = row.get("argument_schema") or {}

    llm_schema = strip_context_args(schema, context_fields) if context_fields else schema
    try:
        args_model = _model_from_schema(python_name, llm_schema)
    except Exception as exc:
        raise ValueError(
            f"failed to build args model for http tool {python_name!r}: {exc}"
        ) from exc

    def _call(**kwargs: Any) -> str:
        if context_fields:
            # Injected last: even if the model smuggled a same-named arg,
            # the server-side credential overrides it.
            kwargs = {**kwargs, **build_context_kwargs(context_fields, ctx or {})}
        classification = _classify_endpoint(endpoint_url)
        if classification == "blocked":
            logger.warning(
                "http tool %s: endpoint %s resolves to a loopback/link-local address; blocked",
                python_name, endpoint_url,
            )
            return "HTTP request blocked: endpoint resolves to a loopback or link-local address."

        headers: Dict[str, str] = {}
        if auth_env_var:
            token = os.getenv(auth_env_var)
            if not token:
                logger.warning(
                    "http tool %s: auth_env_var=%s not set; calling without auth",
                    python_name, auth_env_var,
                )
            elif classification != "public":
                # Don't hand a process secret to an internal/private target.
                logger.warning(
                    "http tool %s: not forwarding auth_env_var to private endpoint %s",
                    python_name, endpoint_url,
                )
            else:
                headers["Authorization"] = f"Bearer {token}"

        try:
            # follow_redirects pinned False: an allowed public endpoint must
            # not be able to 30x-redirect us to an internal host that never
            # went through _classify_endpoint. Pin it explicitly so a future
            # httpx default change or wrapper can't silently re-enable it.
            with httpx.Client(timeout=timeout_seconds, follow_redirects=False) as client:
                if http_method == "GET":
                    resp = client.request(http_method, endpoint_url, params=kwargs, headers=headers)
                else:
                    resp = client.request(http_method, endpoint_url, json=kwargs, headers=headers)
        except httpx.HTTPError as exc:
            return f"HTTP request failed: {exc}"
        except Exception as exc:  # network / DNS / unexpected
            return f"HTTP request failed: {exc}"

        body = _stringify_response(resp)
        if resp.status_code >= 400:
            return f"HTTP {resp.status_code} error: {body[:500]}"
        if len(body) > _MAX_RESPONSE_CHARS:
            dropped = len(body) - _MAX_RESPONSE_CHARS
            return f"{body[:_MAX_RESPONSE_CHARS]}...[truncated {dropped} bytes]"
        return body

    return StructuredTool.from_function(
        func=_call,
        name=python_name,
        description=description,
        args_schema=args_model,
    )
