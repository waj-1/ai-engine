"""Voice checks inside the shared Testing scores.

Voice checks (``ai_voice_scenarios`` / ``ai_voice_scenario_runs``, run by
``admin/voice_scenario_runner.py``) are scripted live calls with pass/fail
steps. They used to live only on the Voice checks tab, so a voice agent's
Testing Overview card, the Runs list, Compare and the admin MCP never saw
them. This module maps them onto the eval-run model the rest of the Testing
area already speaks — no new tables, no parallel UI:

* a **voice run** becomes an eval-run-shaped dict (``kind="voice"``) whose
  ``results`` are the call's assertion steps (``expect_transcript`` /
  ``expect_frame`` / ``http_check``), one case per step, so
  ``eval_handlers._compare_runs`` / ``build_case_rows`` / ``case_reason``
  work unchanged;
* its ``suite_id`` is the synthetic ``voice:<slug>`` so two runs of the same
  check compare case by case, while runs of different checks are matched
  only by score — exactly like two different text suites;
* per agent, the latest result of each of its voice checks rolls up into one
  **"Voice checks"** row on the Overview card: score = share of checks whose
  latest call passed, target :data:`VOICE_CHECK_TARGET`.

A voice check targets an agent *kind*; the call is answered by that kind's
active top-level profile in the environment (``endpoints/voice_live.py
_active_profile_for``), so that is the agent its results count for.

Pure functions + one loader (``load_voice_runs``) that degrades to empty on
any DB error — voice data must never take a Testing screen down.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

VOICE_SUITE_PREFIX = "voice:"
#: Voice checks are binary acceptance scripts — every one should pass.
VOICE_CHECK_TARGET = 1.0
VOICE_GROUP_NAME = "Voice checks"

ASSERT_STEPS = ("expect_transcript", "expect_frame", "http_check")

# ai_voice_scenario_runs.status -> the eval-run status vocabulary.
_STATUS = {"passed": "passed", "failed": "failed", "error": "errored",
           "running": "running", "skipped": "skipped"}
SCORED = ("passed", "failed")


def voice_suite_id(slug: str) -> str:
    return f"{VOICE_SUITE_PREFIX}{slug}"


def is_voice_suite_id(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(VOICE_SUITE_PREFIX)


def slug_of(suite_id: Any) -> str:
    return str(suite_id)[len(VOICE_SUITE_PREFIX):] if is_voice_suite_id(suite_id) else ""


def check_name(scenario: Optional[Dict[str, Any]], slug: str) -> str:
    from admin.testing_view import humanize

    return ((scenario or {}).get("title") or humanize(slug) or slug)


# ---------------------------------------------------------------------------
# One run -> eval-run shape
# ---------------------------------------------------------------------------


def _clip(text: Any, n: int = 80) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


_WORDS = re.compile(r"^[\w\s'\-]+(\|[\w\s'\-]+)*$", re.UNICODE)


def _expected_words(pattern: str) -> str:
    """'(?i)refund|payment' -> '“refund” or “payment”'; a real regex stays
    as-is (quoted) — it is the technical truth of what was checked."""
    p = re.sub(r"^\(\?[a-z]+\)", "", str(pattern or "")).strip()
    if p and _WORDS.match(p):
        words = [w.strip() for w in p.split("|") if w.strip()]
        quoted = [f"“{_clip(w, 40)}”" for w in words]
        return quoted[0] if len(quoted) == 1 else ", ".join(quoted[:-1]) + " or " + quoted[-1]
    return f"“{_clip(pattern)}”"


def step_label(check: Dict[str, Any], step: Optional[Dict[str, Any]] = None) -> str:
    """What a step expected, in words: 'Agent mentions “ahlan” or “hello”'."""
    stype = check.get("type") or (step or {}).get("type")
    pattern = check.get("pattern") if check.get("pattern") is not None else (step or {}).get("pattern")
    if stype == "expect_transcript":
        role = (step or {}).get("role") or "assistant"
        who = "Agent" if role == "assistant" else "Caller"
        return f"{who} mentions {_expected_words(pattern)}" if pattern else f"{who} replies"
    if stype == "expect_frame":
        frame = check.get("frame_type") or (step or {}).get("frame_type") or "display"
        return f"Shows a {frame} update" + (f" with {_expected_words(pattern)}" if pattern else "")
    if stype == "http_check":
        return "Follow-up check on the system passes"
    return _clip(str(stype or "Step").replace("_", " ").capitalize())


def voice_results(row: Dict[str, Any], scenario: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """A voice run's assertion steps as eval ``results[]`` cases."""
    detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
    steps = list((scenario or {}).get("steps") or [])
    out: List[Dict[str, Any]] = []
    for c in detail.get("checks") or []:
        if not isinstance(c, dict):
            continue
        stype = c.get("type")
        unknown = bool(c.get("error"))
        if stype not in ASSERT_STEPS and not unknown:
            continue  # `say` / `wait` steps always "pass" — not a test case
        idx = c.get("step")
        step = steps[idx] if isinstance(idx, int) and 0 <= idx < len(steps) else None
        label = step_label(c, step)
        ok = bool(c.get("ok"))
        detail_txt = c.get("pattern") or c.get("url") or ""
        res: Dict[str, Any] = {
            "scenario_id": f"Step {int(idx) + 1 if isinstance(idx, int) else len(out) + 1} · {label}",
            "name": label,
            "question": label,
            "passed": ok,
            "score": 1.0 if ok else 0.0,
            "scorer_detail": {"checks": [{"type": stype, "ok": ok, "detail": detail_txt}]},
        }
        if c.get("error"):
            res["error"] = c["error"]
        elif not ok and stype == "expect_transcript":
            res["reason"] = ("The agent didn't say it within the time allowed"
                             if ((step or {}).get("role") or "assistant") == "assistant"
                             else "The caller's words weren't heard within the time allowed")
        elif not ok and stype == "expect_frame":
            res["reason"] = "That screen update didn't arrive within the time allowed"
        if stype == "http_check" and not ok and c.get("body_head"):
            res["reason"] = f"Follow-up check failed: {_clip(c['body_head'], 160)}"
        out.append(res)
    return out


def voice_score(row: Dict[str, Any], results: Optional[List[Dict[str, Any]]] = None) -> Optional[float]:
    """Share of the call's assertion steps that passed; None until scored."""
    status = _STATUS.get(str(row.get("status") or ""), str(row.get("status") or ""))
    if status not in SCORED:
        return None
    res = results if results is not None else voice_results(row)
    if not res:
        return 1.0 if status == "passed" else 0.0
    return sum(1 for r in res if r.get("passed")) / len(res)


def as_eval_run(row: Dict[str, Any], scenario: Optional[Dict[str, Any]] = None,
                *, profile_id: Optional[str] = None) -> Dict[str, Any]:
    """An ``ai_voice_scenario_runs`` row in the eval-run shape the Testing
    screens and the admin MCP read (plus ``kind="voice"``)."""
    slug = row.get("slug") or (scenario or {}).get("slug") or ""
    detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
    results = voice_results(row, scenario)
    status = _STATUS.get(str(row.get("status") or ""), str(row.get("status") or "never"))
    return {
        "run_id": row.get("run_id"),
        "kind": "voice",
        "suite_id": voice_suite_id(slug),
        "profile_id": profile_id,
        "agent_kind": (scenario or {}).get("agent_kind"),
        "status": status,
        "score": voice_score(row, results),
        "created_at": row.get("started_at"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "results": results,
        "error": detail.get("reason"),
        "context": {"voice_check": slug, "target": row.get("target")},
        "_voice": True,
        "_check_name": check_name(scenario, slug),
        "_scenario": scenario or {},
    }


# ---------------------------------------------------------------------------
# Which agent a voice check speaks for
# ---------------------------------------------------------------------------


def voice_profile_by_kind(profiles: Iterable[Dict[str, Any]], env: Optional[str] = None) -> Dict[str, str]:
    """agent_kind -> the profile its voice calls reach: the kind's active,
    top-level profile, preferring the selected environment's row."""
    best: Dict[str, tuple] = {}
    for p in profiles or []:
        kind, pid = p.get("agent_kind"), p.get("profile_id")
        if not kind or not pid or p.get("parent_profile_id"):
            continue
        if (p.get("status") or "active") != "active" or p.get("is_active") is False:
            continue
        rank = 0 if (env and p.get("environment") == env) else 1
        if kind not in best or rank < best[kind][0]:
            best[kind] = (rank, pid)
    return {k: v[1] for k, v in best.items()}


# ---------------------------------------------------------------------------
# Loading (best-effort)
# ---------------------------------------------------------------------------


def load_voice_runs(client: Any, *, limit: int = 200, run_ids: Optional[List[str]] = None,
                    slug: Optional[str] = None, env: Optional[str] = None,
                    profiles: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Voice checks + their runs as eval-run dicts, scoped to this build.

    Returns ``{"scenarios": [...], "runs": [...], "profile_by_kind": {...}}``
    — runs newest-first. ``profiles`` (agent_profiles rows with
    ``agent_kind, status, environment, parent_profile_id``) may be passed in
    when the caller already has them. Any read error -> empty lists.
    """
    from admin.agent_kinds import scope_to_build

    try:
        scenarios = (client.table("ai_voice_scenarios").select("*").execute().data or [])
        q = client.table("ai_voice_scenario_runs").select("*")
        if run_ids:
            q = q.in_("run_id", list(run_ids))
        if slug:
            q = q.eq("slug", slug)
        rows = (q.order("started_at", desc=True).limit(limit).execute().data or [])
        if profiles is None or not all("status" in p for p in profiles):
            profiles = (client.table("agent_profiles")
                        .select("profile_id, agent_kind, status, environment, parent_profile_id, display_name")
                        .execute().data or [])
    except Exception as exc:  # noqa: BLE001 — voice data must never break a page
        logger.warning("voice_checks: load failed (%s)", exc)
        return {"scenarios": [], "runs": [], "profile_by_kind": {}}
    scenarios = scope_to_build(scenarios, also=())
    by_slug = {s.get("slug"): s for s in scenarios if s.get("slug")}
    if env is None:
        try:
            from core.auth import get_admin_env

            env = get_admin_env()
        except Exception:  # noqa: BLE001
            env = None
    profile_by_kind = voice_profile_by_kind(profiles or [], env)
    runs = []
    for r in rows:
        sc = by_slug.get(r.get("slug"))
        if sc is None:
            continue  # another build's check, or a deleted one
        runs.append(as_eval_run(r, sc, profile_id=profile_by_kind.get(sc.get("agent_kind"))))
    return {"scenarios": scenarios, "runs": runs, "profile_by_kind": profile_by_kind}


# ---------------------------------------------------------------------------
# Per-agent roll-up for the Overview card
# ---------------------------------------------------------------------------


def voice_group(scenarios: List[Dict[str, Any]], runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """One agent's voice checks as a single Overview suite row.

    ``scenarios`` = the agent's enabled checks; ``runs`` = their eval-shaped
    runs, newest-first. Score = share of checks whose latest scored call
    passed; ``delta`` compares with each check's previous scored call.
    """
    from admin.testing_view import verdict

    scored_by_slug: Dict[str, List[Dict[str, Any]]] = {}
    for r in runs:
        if r.get("status") in SCORED:
            scored_by_slug.setdefault(slug_of(r.get("suite_id")), []).append(r)
    latest, previous = [], []
    checks = []
    for sc in sorted(scenarios, key=lambda s: str(s.get("title") or s.get("slug") or "")):
        hist = scored_by_slug.get(sc.get("slug")) or []
        last = hist[0] if hist else None
        if last:
            latest.append(last)
            previous.append(hist[1] if len(hist) > 1 else last)
        checks.append({"slug": sc.get("slug"), "name": check_name(sc, sc.get("slug") or ""),
                       "latest": last})
    n = len(latest)
    passed = sum(1 for r in latest if r.get("status") == "passed")
    score = (passed / n) if n else None
    prev_score = (sum(1 for r in previous if r.get("status") == "passed") / n) if n else None
    has_prev = any(len(scored_by_slug.get(sc.get("slug")) or []) > 1 for sc in scenarios)
    newest = max(latest, key=lambda r: str(r.get("finished_at") or r.get("started_at") or "")) if latest else None
    failing_runs = [r for r in latest if r.get("status") != "passed"]
    focus_run = max(failing_runs, key=lambda r: str(r.get("finished_at") or r.get("started_at") or "")) if failing_runs else newest
    head = None
    if newest:
        head = {"run_id": (focus_run or newest).get("run_id"), "score": score,
                "finished_at": newest.get("finished_at"), "created_at": newest.get("started_at")}
    return {
        "kind": "voice",
        "suite": {"suite_id": None, "name": VOICE_GROUP_NAME, "kind": "voice"},
        "latest": head,
        "target": VOICE_CHECK_TARGET,
        "verdict": verdict(score, VOICE_CHECK_TARGET),
        "delta": (score - prev_score) if (n and has_prev and prev_score is not None) else None,
        "failing": len(failing_runs) if n else None,
        "n_checks": len(scenarios),
        "n_tested": n,
        "checks": checks,
    }


def load_voice_run(client: Any, run_id: str) -> Optional[Dict[str, Any]]:
    """One voice run for the run detail page: ``{"run", "raw", "scenario",
    "previous", "steps"}`` — ``run`` eval-shaped, ``previous`` the same
    check's previous scored run (eval-shaped) or None, ``steps`` every step of
    the call (say / wait included) with its outcome. None when the id is not
    a voice run in this build/environment."""
    try:
        rows = (client.table("ai_voice_scenario_runs").select("*")
                .eq("run_id", run_id).limit(1).execute().data or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("voice_checks: run lookup failed (%s)", exc)
        return None
    if not rows:
        return None
    raw = rows[0]
    data = load_voice_runs(client, limit=50, slug=raw.get("slug"))
    scenario = next((s for s in data["scenarios"] if s.get("slug") == raw.get("slug")), None)
    if scenario is None:
        return None  # another build's check (or deleted): not shown here
    run = next((r for r in data["runs"] if r.get("run_id") == run_id), None) or as_eval_run(
        raw, scenario, profile_id=data["profile_by_kind"].get(scenario.get("agent_kind")))
    previous = None
    for r in data["runs"]:  # newest-first
        if str(r.get("created_at") or "") < str(run.get("created_at") or "") and r.get("status") in SCORED:
            previous = r
            break
    defs = list(scenario.get("steps") or [])
    detail = raw.get("detail") if isinstance(raw.get("detail"), dict) else {}
    by_step = {c.get("step"): c for c in detail.get("checks") or [] if isinstance(c, dict)}
    steps = []
    for i, d in enumerate(defs):
        c = by_step.get(i)
        stype = (d or {}).get("type")
        if stype == "say":
            what = f"Caller says “{_clip((d or {}).get('text'), 120)}”"
        elif stype == "wait":
            what = f"Wait {(d or {}).get('sec', 1)} s"
        else:
            what = step_label(c or {"type": stype}, d)
        steps.append({"n": i + 1, "type": stype, "what": what,
                      "checked": stype in ASSERT_STEPS,
                      "outcome": None if c is None else ("passed" if c.get("ok") else "failed"),
                      "note": (c or {}).get("error") or ((c or {}).get("body_head") if c and not c.get("ok") else "")})
    transcript = [
        {"role": f.get("role"), "text": f.get("text") or f.get("content")}
        for f in detail.get("frames_tail") or []
        if isinstance(f, dict) and f.get("type") == "transcript" and (f.get("text") or f.get("content"))
    ]
    return {"run": run, "raw": raw, "scenario": scenario, "previous": previous,
            "steps": steps, "transcript": transcript}


def load_voice_runs_by_ids(client: Any, run_ids: List[str]) -> List[Dict[str, Any]]:
    """Eval-shaped voice runs for the given ids (this build's checks only)."""
    if not run_ids:
        return []
    return load_voice_runs(client, limit=len(run_ids) + 5, run_ids=run_ids)["runs"]
