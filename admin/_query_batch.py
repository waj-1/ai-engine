"""Fan independent, blocking Supabase reads across a thread pool.

Admin pages render by firing many small PostgREST round-trips, and each has a
~0.3-1s floor (network RTT + PostgREST overhead) that is independent of payload
size — so a handful of *sequential* ``.execute()`` calls is what actually
dominates page latency, not the data volume. When those reads are independent
of one another we run them concurrently: the sync supabase client wraps a
thread-safe ``httpx`` connection pool and ``client.table(...)`` returns a fresh
request builder each call (no shared query state), so concurrent ``.execute()``
is safe. This collapses ~N serial round-trips into ~1 wall-clock one.

Each thunk runs inside a **copy of the submitting request's context**
(``contextvars.copy_context()``). ``concurrent.futures`` threads do NOT
inherit ContextVars, so without this a thunk that resolves its own client via
``core.auth.admin_service_client()`` (which reads the request-scoped
``_ADMIN_ENV`` set by the env-switcher middleware) silently fell back to the
LOCAL branch while the surrounding request read/wrote the SELECTED branch —
e.g. the bake-offs tab's ``list_bakeoffs`` fan-out. Propagating the context at
submit time fixes every caller at once; a fresh copy per task because one
``Context`` object cannot be entered by two threads concurrently.
"""
from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict


def run_parallel(tasks: Dict[str, Callable[[], Any]]) -> Dict[str, Any]:
    """Run each thunk concurrently and return ``{key: result}``.

    Exceptions propagate on ``.result()`` in submission order, preserving a
    sequential caller's "a failed read aborts the page" posture. A thunk that
    would rather degrade (e.g. an attachment lookup that returns ``[]`` on a DB
    hiccup) should swallow its own error and return a default instead of
    raising.
    """
    if not tasks:
        return {}
    results: Dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futures = {
            # copy_context() per task: pooled threads don't inherit the
            # caller's ContextVars (request-scoped _ADMIN_ENV et al.), and a
            # single Context can't be entered concurrently from two threads.
            key: pool.submit(contextvars.copy_context().run, fn)
            for key, fn in tasks.items()
        }
        for key, fut in futures.items():
            results[key] = fut.result()
    return results
