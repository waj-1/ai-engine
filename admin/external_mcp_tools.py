"""The tools an EXTERNAL MCP server offers, for the screens that count them.

A bundle's tools are catalog rows (`agent_mcp_tools` -> `agent_tools`). An
external server's are not: it lists them itself (`tools/list`), and the
runtime binds whatever it lists (narrowed by a pinned `tool_subset`). The
Connections catalog and an agent's "N available right now" counted only
catalog rows, so a server whose Test connection listed four tools, and whose
tools the agent really called, read "Tool catalog (0)" and "0 available".

These read the server's own list: the one this process saw last (Test
connection, an agent's turn — `runtime.tools._mcp_adapter` caches it), and,
for a server it has not listed yet, a live `tools/list` that the page waits
for only briefly (`LIVE_WAIT_S`). A listing that does not finish in time
still lands in the cache for the next page load; one that fails is not
retried for `RETRY_AFTER_S`, so a down server cannot slow every page.
The same rules as Test connection decide where a token may be sent.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

LIVE_WAIT_S = 3.0
RETRY_AFTER_S = 60.0
# Off in the test suite (tests/conftest.py): tests never call out.
LIVE_LISTING = True

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mcp-list")
_failed_at: Dict[Tuple[str, Optional[str]], float] = {}
_inflight: Dict[Tuple[str, Optional[str]], Any] = {}
_lock = threading.Lock()


def is_listed_server(server: Dict[str, Any]) -> bool:
    """An external server whose tools come from its own list."""
    return (server.get("source") or "external") != "bundle" and not server.get("endpoint_only") \
        and bool((server.get("endpoint") or "").strip())


def _target(server: Dict[str, Any]) -> Optional[Tuple[str, Optional[str]]]:
    """(endpoint, token) to list with, or None where Test connection would
    refuse (a local/internal address while those are not allowed)."""
    from runtime.tools._http_adapter import _classify_endpoint

    endpoint = (server.get("endpoint") or "").strip()
    if not endpoint:
        return None
    try:
        classification = _classify_endpoint(endpoint)
    except Exception:  # noqa: BLE001 - a malformed address lists nothing
        return None
    allow_local = os.getenv("MCP_ALLOW_LOCAL_ENDPOINTS", "").strip().lower() in {"1", "true", "yes", "on"}
    if classification == "blocked" and not allow_local:
        return None
    env_var = server.get("auth_env_var")
    # Same rule as Test connection: the secret only goes to a public address.
    token = os.getenv(env_var) if (env_var and classification == "public") else None
    return endpoint, token


def _slim(tools: Any) -> List[Dict[str, Any]]:
    return [{"name": t.get("name"), "description": (t.get("description") or "").strip()}
            for t in (tools or []) if isinstance(t, dict) and t.get("name")]


def _list(target: Tuple[str, Optional[str]]) -> None:
    from runtime.tools._mcp_adapter import list_tools_cached

    try:
        list_tools_cached(*target)
    except Exception as exc:  # noqa: BLE001 - counted as "not listed yet"
        logger.info("external mcp: could not list %s (%s)", target[0], exc)
        _failed_at[target] = time.monotonic()
    finally:
        with _lock:
            _inflight.pop(target, None)


def listed_tools(servers: Iterable[Dict[str, Any]], *, live: bool = True,
                 wait_s: float = LIVE_WAIT_S) -> Dict[str, Optional[List[Dict[str, Any]]]]:
    """mcp_server_id -> [{name, description}] the server lists, or None when
    its list is not known (never listed and the live read did not answer in
    time, failed, or was not allowed). Narrowed to a server's own pinned
    `tool_subset` when it has one. Servers that are not listed servers
    (bundles, connection-only) are left out."""
    from runtime.tools._mcp_adapter import last_listed_tools

    targets: Dict[str, Tuple[str, Optional[str]]] = {}
    for srv in servers:
        sid = srv.get("mcp_server_id")
        if not sid or not is_listed_server(srv):
            continue
        target = _target(srv)
        targets[str(sid)] = target  # type: ignore[assignment]

    if live and LIVE_LISTING:
        pending = []
        now = time.monotonic()
        for target in {t for t in targets.values() if t}:
            if last_listed_tools(*target) is not None:
                continue
            if now - _failed_at.get(target, -RETRY_AFTER_S * 2) < RETRY_AFTER_S:
                continue
            with _lock:
                fut = _inflight.get(target)
                if fut is None:
                    fut = _executor.submit(_list, target)
                    _inflight[target] = fut
            pending.append(fut)
        if pending:
            wait(pending, timeout=wait_s)

    by_server = {str(s.get("mcp_server_id")): s for s in servers if s.get("mcp_server_id")}
    out: Dict[str, Optional[List[Dict[str, Any]]]] = {}
    for sid, target in targets.items():
        tools = last_listed_tools(*target) if target else None
        if tools is None:
            out[sid] = None
            continue
        slim = _slim(tools)
        pinned = set(by_server.get(sid, {}).get("tool_subset") or [])
        out[sid] = [t for t in slim if t["name"] in pinned] if pinned else slim
    return out


def reset() -> None:
    """Forget failures and in-flight reads (tests)."""
    with _lock:
        _failed_at.clear()
        _inflight.clear()
