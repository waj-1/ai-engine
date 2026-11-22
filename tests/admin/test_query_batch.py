"""admin._query_batch.run_parallel — the shared fan-out primitive behind the
/admin/agents, /admin/evals*, and /admin/tools query batching. Pins its four
contract points: results are keyed correctly, the thunks actually overlap, a
raising thunk propagates (so a failed read still aborts the page), and the
submitting request's ContextVars are visible inside every pooled thunk (so an
env-scoped `admin_service_client()` read resolves the SELECTED branch, not the
local one)."""
from __future__ import annotations

import threading
import time

import pytest

from admin._query_batch import run_parallel


def test_returns_results_keyed_by_task_name():
    out = run_parallel({"a": lambda: 1, "b": lambda: "two", "c": lambda: [3]})
    assert out == {"a": 1, "b": "two", "c": [3]}


def test_empty_tasks_returns_empty():
    assert run_parallel({}) == {}


def test_thunks_run_concurrently():
    def _slow(v):
        def _f():
            time.sleep(0.05)
            return v
        return _f

    tasks = {f"k{i}": _slow(i) for i in range(8)}
    start = time.monotonic()
    out = run_parallel(tasks)
    elapsed = time.monotonic() - start

    assert out == {f"k{i}": i for i in range(8)}
    # 8 thunks x 50ms = 400ms if serial; overlapping they finish near ~50ms.
    assert elapsed < 0.2, f"expected overlap, took {elapsed:.3f}s for 8x50ms thunks"


def test_exception_propagates():
    def _boom():
        raise ValueError("read failed")

    with pytest.raises(ValueError, match="read failed"):
        run_parallel({"ok": lambda: 1, "bad": _boom})


def test_contextvars_propagate_into_pooled_thunks():
    """A ContextVar set in the submitting (request) context must be visible
    inside every pooled thunk — `concurrent.futures` threads do NOT inherit
    ContextVars on their own, and `admin_service_client()` reads the
    request-scoped `_ADMIN_ENV` to pick the env branch. Without propagation
    the bake-offs tab (and every other fan-out that resolves its own client
    inside a thunk) read the LOCAL branch while the surrounding request
    served the SELECTED one."""
    from core.auth import get_admin_env, reset_admin_env, set_admin_env

    main_ident = threading.get_ident()
    token = set_admin_env("prod")
    try:
        out = run_parallel({
            "env": get_admin_env,
            "ident": threading.get_ident,
        })
    finally:
        reset_admin_env(token)

    # The thunk genuinely ran on a pool thread…
    assert out["ident"] != main_ident
    # …and still saw the submitting context's admin env.
    assert out["env"] == "prod"


def test_contextvar_writes_inside_thunks_do_not_leak_out():
    """Each thunk runs in a COPY of the caller's context — a write inside one
    must not bleed into the caller or a sibling submission afterwards."""
    from core.auth import get_admin_env, set_admin_env

    before = get_admin_env()
    run_parallel({"w": lambda: set_admin_env("prod" if before != "prod" else "staging")})
    assert get_admin_env() == before
