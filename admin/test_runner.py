"""Run pytest from the admin portal and record results to agent_test_runs.

A suite is a free-form key the UI passes in; it maps to a pytest invocation:

  unit_runtime  → tests/runtime/
  unit_admin    → tests/admin/
  all           → tests/
  agent_kind:X  → tests/test_<X>* (matches existing per-agent test files)

The runner shells out to pytest with `--json-report --json-report-file=-`
when the plugin is installed; otherwise it parses the summary line.
Either way the row in `agent_test_runs` carries:
  - pytest stdout (truncated to 50KB)
  - pass/fail/skip counts
  - per-test outcomes (from the JSON report if available)
  - status: passed | failed | errored
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

# Env-aware client: agent_test_runs rows must land on the branch the
# /admin/tests screen reads (`client_for(env)` — the switcher env). The portal
# invokes `run_suite` via `asyncio.to_thread`, which copies the request
# context, so the middleware-set `_ADMIN_ENV` is visible here; the MCP
# `pytest_runs` trigger runs with no admin env selected and falls back to the
# local branch, exactly as before.
from core.auth import admin_service_client

logger = logging.getLogger(__name__)

# Cap stored output so a chatty pytest doesn't blow up the row.
_OUTPUT_LIMIT = 50_000

# ai_agent/ is the working dir for pytest; supabase/migrations and node_modules live outside.
_AGENT_DIR = Path(__file__).resolve().parent.parent

_SUITE_PATHS = {
    "unit_runtime": "tests/runtime/",
    "unit_admin": "tests/admin/",
    "smoke": "tests/admin/test_smoke.py tests/runtime/ tests/test_suggestion_prompts.py",
    "all": "tests/",
}

_SUMMARY_RE = re.compile(
    r"(?P<passed>\d+)\s+passed"
    r"(?:.*?(?P<failed>\d+)\s+failed)?"
    r"(?:.*?(?P<skipped>\d+)\s+skipped)?"
)


def list_suites() -> List[Dict[str, Any]]:
    """Return what suites the UI can offer.

    Each entry: {key, label, path, description}. Per-agent suites are
    discovered from the tests directory so adding a new file under
    tests/test_<kind>* automatically surfaces it.
    """
    suites: List[Dict[str, Any]] = [
        {"key": "smoke", "label": "Smoke (runtime + admin + suggestions)",
         "path": _SUITE_PATHS["smoke"], "description": "Fast scoped tests; ~50 tests"},
        {"key": "unit_runtime", "label": "Runtime unit tests",
         "path": _SUITE_PATHS["unit_runtime"], "description": "RuntimeAgent / bindings / compiler"},
        {"key": "unit_admin", "label": "Admin portal smoke",
         "path": _SUITE_PATHS["unit_admin"], "description": "Every admin route + seeders"},
        {"key": "all", "label": "Full suite",
         "path": _SUITE_PATHS["all"], "description": "Everything — slowest"},
    ]
    # Per-agent: tests/test_<kind>*.py
    tests_dir = _AGENT_DIR / "tests"
    if tests_dir.is_dir():
        for path in sorted(tests_dir.glob("test_*.py")):
            stem = path.stem  # test_chat_persistence
            suites.append({
                "key": f"file:{path.name}",
                "label": stem.replace("_", " ").title(),
                "path": f"tests/{path.name}",
                "description": "Single file",
            })
    return suites


def run_suite(*, suite: str, triggered_by: Optional[str] = None) -> Dict[str, Any]:
    """Run pytest synchronously and persist the result. Returns the run summary."""
    path = _resolve_suite_path(suite)
    if not path:
        return _record_errored(suite, triggered_by, f"unknown suite: {suite}")

    client = admin_service_client()
    run_id = str(uuid4())
    started = _iso_now()
    try:
        client.table("agent_test_runs").insert({
            "run_id": run_id, "suite": suite, "status": "running",
            "started_at": started, "triggered_by": triggered_by,
        }).execute()
    except Exception as exc:
        logger.warning("test_runner: insert running-row failed (%s)", exc)

    started_at = time.monotonic()
    try:
        cmd = ["python3", "-m", "pytest", *path.split(), "-q", "--no-cov", "--tb=short"]
        proc = subprocess.run(
            cmd, cwd=_AGENT_DIR, capture_output=True, text=True, timeout=600,
        )
        stdout = (proc.stdout or "")[-_OUTPUT_LIMIT:]
        stderr = (proc.stderr or "")[-_OUTPUT_LIMIT:]
        duration_ms = int((time.monotonic() - started_at) * 1000)
        summary = _parse_summary(stdout)
        per_test = _parse_per_test(stdout)
        status = "passed" if proc.returncode == 0 else (
            "failed" if (summary.get("failed", 0) or 0) > 0 else "errored"
        )
        payload = {
            "status": status,
            "pass_count": summary.get("passed", 0),
            "fail_count": summary.get("failed", 0),
            "skip_count": summary.get("skipped", 0),
            "duration_ms": duration_ms,
            "output": (stdout + ("\n--- STDERR ---\n" + stderr if stderr.strip() else ""))[-_OUTPUT_LIMIT:],
            "results": per_test,
            "finished_at": _iso_now(),
            "error": None if proc.returncode == 0 else (stderr[:500] or "pytest non-zero exit"),
        }
        try:
            client.table("agent_test_runs").update(payload).eq("run_id", run_id).execute()
        except Exception as exc:
            logger.warning("test_runner: update failed (%s)", exc)
        payload["run_id"] = run_id
        return payload
    except subprocess.TimeoutExpired:
        return _record_errored(suite, triggered_by, "pytest timeout (>600s)", run_id=run_id, client=client)
    except Exception as exc:
        return _record_errored(suite, triggered_by, str(exc)[:500], run_id=run_id, client=client)


def _resolve_suite_path(suite: str) -> Optional[str]:
    if suite in _SUITE_PATHS:
        return _SUITE_PATHS[suite]
    if suite.startswith("file:"):
        filename = suite[len("file:"):]
        candidate = _AGENT_DIR / "tests" / filename
        if candidate.is_file():
            return f"tests/{filename}"
    return None


def _parse_summary(stdout: str) -> Dict[str, int]:
    """Pull pass/fail/skip counts from the pytest summary line."""
    out: Dict[str, int] = {}
    # pytest prints e.g. "12 passed, 2 failed, 1 skipped in 0.45s"
    for line in stdout.splitlines()[::-1]:
        line = line.strip()
        if not line or "passed" not in line and "failed" not in line:
            continue
        for kw, key in (("passed", "passed"), ("failed", "failed"),
                        ("skipped", "skipped"), ("errors", "errors")):
            m = re.search(rf"(\d+)\s+{kw}", line)
            if m:
                out[key] = int(m.group(1))
        if out:
            break
    return out


def _parse_per_test(stdout: str) -> List[Dict[str, Any]]:
    """Best-effort per-test parse from the short summary section.

    pytest -q prints lines like:
      tests/runtime/test_runtime.py::test_binding_registry_falls_back_to_default PASSED
    or in --tb=short:
      FAILED tests/admin/test_smoke.py::test_redirect[/admin/profiles-/admin/agents] - AssertionError
    """
    results: List[Dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+)(?:\s+-\s+(.*))?$", line)
        if m:
            results.append({
                "outcome": m.group(1).lower(),
                "nodeid": m.group(2),
                "message": m.group(3) or "",
            })
    return results[:200]  # cap


def _record_errored(suite: str, triggered_by: Optional[str], message: str,
                    *, run_id: Optional[str] = None, client=None) -> Dict[str, Any]:
    if client is None:
        try:
            client = admin_service_client()
        except Exception:
            return {"status": "errored", "error": message}
    payload = {
        "status": "errored", "error": message,
        "finished_at": _iso_now(), "triggered_by": triggered_by,
    }
    try:
        if run_id:
            client.table("agent_test_runs").update(payload).eq("run_id", run_id).execute()
        else:
            inserted = client.table("agent_test_runs").insert(
                {"suite": suite, **payload}
            ).execute()
            run_id = inserted.data[0]["run_id"] if inserted.data else None
    except Exception:
        pass
    return {"run_id": run_id, "status": "errored", "error": message}


def _iso_now() -> str:
    import datetime as _dt
    return _dt.datetime.utcnow().isoformat() + "Z"
