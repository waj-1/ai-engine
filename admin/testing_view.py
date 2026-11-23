"""Display helpers for the Testing area (evals, runs, comparisons, bake-offs).

Pure functions + stdlib only, so every module that owns a ``Jinja2Templates``
instance (``admin.router``, ``admin.eval_handlers``) can install the filters
below without an import cycle, and tests can call them without a DB.

One vocabulary for the whole area (PLAN §2.6/§2.10):

* rates and targets are always **percent** (``79%``, ``target 90%``), changes
  are **points** (``−3 pts``);
* run/case status is always one of **Passed · Failed · Error · Running ·
  Queued · Cancelled · Not run**;
* a value we don't know is ``—``, never a confident ``0``;
* engineering identifiers (suite slugs, check types) are humanised, with the
  raw value kept only as a muted technical name.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------

# status value (eval runs, test runs, e2e runs, voice checks, bake-offs,
# per-case outcomes) -> (label, chip variant)
_STATUS: Dict[str, tuple] = {
    "passed": ("Passed", "ok"),
    "pass": ("Passed", "ok"),
    "ok": ("Passed", "ok"),
    "complete": ("Finished", "ok"),
    "failed": ("Failed", "err"),
    "fail": ("Failed", "err"),
    "errored": ("Error", "err"),
    "error": ("Error", "err"),
    "running": ("Running", "info"),
    "queued": ("Queued", "neutral"),
    "cancelled": ("Cancelled", "warn"),
    "skipped": ("Skipped", "neutral"),
    "never": ("Not run", "neutral"),
}


def status_label(status: Any) -> str:
    s = str(status or "never").strip().lower()
    return _STATUS.get(s, (s.replace("_", " ").capitalize() or "Not run", "neutral"))[0]


def status_variant(status: Any) -> str:
    s = str(status or "never").strip().lower()
    return _STATUS.get(s, ("", "neutral"))[1]


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------


def _num(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def pct(v: Any, *, dash: str = "—") -> str:
    """0..1 fraction -> '79%'. None/unparsable -> '—'."""
    f = _num(v)
    if f is None:
        return dash
    return f"{round(f * 100)}%"


def pts(delta: Any, *, signed: bool = True) -> str:
    """A change between two 0..1 fractions, in percentage points: '+4 pts',
    '−3 pts', '±0 pts'. Uses a real minus sign for readability."""
    f = _num(delta)
    if f is None:
        return "—"
    n = round(f * 100)
    if not signed:
        return f"{abs(n)} pts"
    if n > 0:
        return f"+{n} pts"
    if n < 0:
        return f"−{abs(n)} pts"
    return "±0 pts"


def money(v: Any) -> str:
    """USD cost, or '—' when unknown. Sub-cent values keep 4 decimals."""
    f = _num(v)
    if f is None:
        return "—"
    if f == 0:
        return "$0"
    return f"${f:.2f}" if f >= 1 else f"${f:.4f}"


def ms(v: Any) -> str:
    """Milliseconds -> '850 ms' / '7.3 s' / '1 min 26 s' / '1 h 2 min'; '—'
    when unknown. Past a minute the same shape as `duration`."""
    f = _num(v)
    if f is None:
        return "—"
    if f < 1000:
        return f"{round(f)} ms"
    if f < 59950:  # below what rounds to 60.0 s
        return f"{f / 1000:.1f} s"
    secs = int(round(f / 1000))
    if secs < 3600:
        return f"{secs // 60} min {secs % 60} s"
    return f"{secs // 3600} h {(secs % 3600) // 60} min"


def count(v: Any) -> str:
    f = _num(v)
    if f is None:
        return "—"
    return f"{int(f):,}"


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

_BASE_ACRONYMS = frozenset({"ai", "ar", "en", "id", "kb", "sms", "url", "faq", "csv", "pos", "vip", "ui", "qa"})


def _acronyms() -> frozenset:
    """_BASE_ACRONYMS plus a plugin's own (its ``portal_display`` hook,
    ``display_acronyms()``), read once per plugin list."""
    from core.plugins import plugin_names

    names = plugin_names()
    if _acronym_cache.get("plugins") != names:
        from core.plugins import call_hook, load_hook

        extra = {a.lower() for found in call_hook(load_hook("portal_display"), "display_acronyms")
                 for a in (found or ())}
        _acronym_cache.update(plugins=names, acronyms=_BASE_ACRONYMS | extra)
    return _acronym_cache["acronyms"]


_acronym_cache: dict = {}


def humanize(value: Any) -> str:
    """'sales_hallucination_traps' -> 'Sales hallucination traps';
    'support_voice_refund-flow' -> 'Support voice refund flow'. Strings that
    already contain spaces (a human-written name) are returned unchanged."""
    s = str(value or "").strip()
    if not s:
        return ""
    if " " in s:
        return s
    words = [w for w in re.split(r"[_\-\.]+", s) if w]
    if not words:
        return s
    out = []
    acronyms = _acronyms()
    for i, w in enumerate(words):
        lw = w.lower()
        if lw in acronyms and i > 0:
            out.append(lw.upper())
        elif i == 0:
            out.append(lw.upper() if lw in acronyms else lw.capitalize())
        else:
            out.append(lw)
    return " ".join(out)


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def _parse_ts(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    s = str(ts).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s[:19])
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def when(ts: Any, *, now: Optional[datetime] = None) -> str:
    """Short, human timestamp: 'just now', '5 min ago', '3 h ago',
    'yesterday', '4 days ago', else 'Oct 1'. '—' when missing."""
    dt = _parse_ts(ts)
    if dt is None:
        return "—"
    now = now or datetime.now(timezone.utc)
    secs = (now - dt).total_seconds()
    if secs < 0:
        return dt.strftime("%b %-d, %H:%M")
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    days = int(secs // 86400)
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    if dt.year == now.year:
        return dt.strftime("%b %-d")
    return dt.strftime("%b %-d, %Y")


def duration(start: Any, end: Any) -> str:
    """'4 min 12 s' between two timestamps; '' when either is missing."""
    a, b = _parse_ts(start), _parse_ts(end)
    if a is None or b is None or b < a:
        return ""
    secs = int((b - a).total_seconds())
    if secs < 60:
        return f"{secs} s"
    if secs < 3600:
        return f"{secs // 60} min {secs % 60} s"
    return f"{secs // 3600} h {(secs % 3600) // 60} min"


def stamp(ts: Any) -> str:
    """Absolute timestamp for titles/tooltips: 'Oct 1, 2026 12:19 UTC'."""
    dt = _parse_ts(ts)
    if dt is None:
        return "—"
    return dt.astimezone(timezone.utc).strftime("%b %-d, %Y %H:%M UTC")


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


def threshold_of(suite: Optional[Dict[str, Any]], default: float = 0.7) -> float:
    f = _num((suite or {}).get("pass_threshold"))
    return default if f is None else f


def verdict(score: Any, target: Any) -> str:
    """'ready' (score meets target), 'needs_work', or 'untested'."""
    s, t = _num(score), _num(target)
    if s is None:
        return "untested"
    if t is None:
        t = 0.7
    return "ready" if s >= t - 1e-9 else "needs_work"


VERDICT_LABELS = {
    "ready": ("Ready", "ok"),
    "needs_work": ("Needs work", "err"),
    "untested": ("Not tested", "neutral"),
    "missing": ("Agent missing here", "warn"),
}


def verdict_label(v: str) -> str:
    return VERDICT_LABELS.get(v, VERDICT_LABELS["untested"])[0]


def verdict_variant(v: str) -> str:
    return VERDICT_LABELS.get(v, VERDICT_LABELS["untested"])[1]


# ---------------------------------------------------------------------------
# Why did a case fail? (one line, plain language)
# ---------------------------------------------------------------------------

# Voice / chat check types -> what a failure means, in words.
# The engine's own check types (admin/services/eval_checks.py). A plugin's
# check types label themselves through the eval_checks hook's check_labels().
CHECK_LABELS: Dict[str, str] = {
    "regex": "Didn't say what was expected",
    "not_regex": "Said something it shouldn't",
    "no_proposal": "Offered an action it shouldn't",
    "no_question": "Asked a question back instead of answering",
    "tools_any": "Didn't look up the right data",
    "tools_all": "Didn't look up all the required data",
    "no_tools": "Looked up data it shouldn't",
    "no_uuid": "Read out an internal id",
    "http_check": "A follow-up check on the system failed",
    "expect_transcript": "Didn't say what was expected",
    "expect_frame": "Didn't send the expected screen update",
}


LIMIT_LABELS: Dict[str, str] = {
    "max_latency_ms": "Took too long to start replying",
    "max_turn_ms": "A turn took too long",
    "max_turns": "The call went on too long",
}


def limit_label(kind: Any) -> str:
    k = str(kind or "").strip()
    return LIMIT_LABELS.get(k, (humanize(k) or "Limit") + " over the limit")


def _plugin_check_labels() -> Dict[str, str]:
    from core.plugins import call_hook, load_hook

    out: Dict[str, str] = {}
    for found in call_hook(load_hook("eval_checks"), "check_labels"):
        out.update(found)
    return out


def check_label(kind: Any) -> str:
    k = str(kind or "").strip()
    label = CHECK_LABELS.get(k) or _plugin_check_labels().get(k)
    return label or humanize(k) or "Check failed"


def check_detail(detail: Any) -> str:
    """A check's raw detail in words: 'want 0.0' -> 'expected 0.0'."""
    text = str(detail or "").strip()
    text = re.sub(r"\bwant exactly\b", "expected exactly", text)
    text = re.sub(r"(^|[;,]\s*|\s)want\b", lambda m: m.group(1) + "expected", text)
    return text


def _clip(text: str, n: int = 180) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def failed_checks(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    sd = result.get("scorer_detail") if isinstance(result, dict) else None
    checks = (sd or {}).get("checks") if isinstance(sd, dict) else None
    out = []
    for c in checks or []:
        if isinstance(c, dict) and c.get("ok") is False:
            out.append(c)
    return out


def case_reason(result: Dict[str, Any]) -> str:
    """The single most useful sentence about why a case failed (or ''), in
    priority order: the judge's own explanation, a run error, hard limit
    breaches, the first failed check, missing tool calls / wording."""
    if not isinstance(result, dict):
        return ""
    if result.get("ungraded") and result.get("grading_note"):
        # Not judged (no judge, nothing deterministic to check): say why,
        # never "Scored 0%".
        return _clip(result["grading_note"])
    judge = result.get("judge")
    reason = result.get("reason")
    if not reason and isinstance(judge, dict):
        reason = judge.get("reason") or judge.get("rationale") or judge.get("explanation")
    if reason:
        return _clip(reason)
    if result.get("error"):
        return _clip(f"Error: {result['error']}")
    sd = result.get("scorer_detail") if isinstance(result.get("scorer_detail"), dict) else {}
    breaches = sd.get("limit_breaches") or []
    if breaches:
        b = breaches[0] if isinstance(breaches[0], dict) else {}
        return _clip(f"{limit_label(b.get('kind'))} on turn {b.get('turn', '?')}: "
                     f"{b.get('value', '?')} (limit {b.get('limit', '?')})")
    fails = failed_checks(result)
    if fails:
        c = fails[0]
        text = check_label(c.get("type"))
        detail = check_detail(c.get("detail"))
        if detail:
            text += f" — {detail}" if c.get("type") not in ("regex", "not_regex") else f" (looked for: {detail})"
        if len(fails) > 1:
            text += f" (+{len(fails) - 1} more)"
        return _clip(text)
    if sd.get("missing_proposals"):
        return _clip("Didn't offer: " + ", ".join(str(p) for p in sd["missing_proposals"]))
    errored_tools = result.get("errored_expected_tools")
    if errored_tools:
        first = next((e for e in (result.get("tool_errors") or [])
                      if isinstance(e, dict) and e.get("error")), None)
        text = "Tool failed: " + ", ".join(str(t) for t in errored_tools)
        if first:
            text += f" ({first['error']})"
        return _clip(text)
    missing_tools = result.get("missing_expected_tools")
    if missing_tools:
        return _clip("Didn't use: " + ", ".join(str(t) for t in missing_tools))
    forbidden = result.get("forbidden_tools_used")
    if forbidden:
        return _clip("Used a tool it shouldn't: " + ", ".join(str(t) for t in forbidden))
    missing = result.get("missing_substrings")
    if missing:
        return _clip("Answer was missing: " + ", ".join(f"“{m}”" for m in missing))
    tool_errors = [e for e in (result.get("tool_errors") or []) if isinstance(e, dict)]
    if tool_errors and result.get("passed") is False:
        return _clip(f"Tool error: {tool_errors[0].get('tool')} ({tool_errors[0].get('error')})"
                     + (f" (+{len(tool_errors) - 1} more)" if len(tool_errors) > 1 else ""))
    if result.get("passed") is False and result.get("grading_note"):
        return _clip(result["grading_note"])
    if result.get("passed") is False:
        sc = _num(result.get("score"))
        if sc is not None:
            return f"Scored {pct(sc)}, below the pass mark"
    return ""


# ---------------------------------------------------------------------------
# Sparkline points (percent values for _components/sparkline.html)
# ---------------------------------------------------------------------------


def score_series(runs: Iterable[Dict[str, Any]], limit: int = 10) -> List[float]:
    """Oldest -> newest percent scores of scored runs (``runs`` newest-first)."""
    vals = [_num(r.get("score")) for r in runs]
    vals = [round(v * 100, 1) for v in vals if v is not None][:limit]
    return list(reversed(vals))


# ---------------------------------------------------------------------------
# Jinja wiring
# ---------------------------------------------------------------------------


def _build_case_rows(results: Any, prev_results: Any = None) -> List[Dict[str, Any]]:
    # Lazy: admin.eval_handlers imports admin.router, which installs these.
    from admin.eval_handlers import build_case_rows

    return build_case_rows(results, prev_results)


def install_filters(templates: Any) -> None:
    templates.env.globals["build_case_rows"] = _build_case_rows
    f = templates.env.filters
    f["pct"] = pct
    f["pts"] = pts
    f["money"] = money
    f["ms"] = ms
    f["count"] = count
    f["humanize"] = humanize
    f["when"] = when
    f["stamp"] = stamp
    f["check_label"] = check_label
    f["check_detail"] = check_detail
    f["limit_label"] = limit_label
    f["duration"] = duration
    f["case_reason"] = case_reason
    f["status_label"] = status_label
    f["status_variant"] = status_variant
    f["verdict_label"] = verdict_label
    f["verdict_variant"] = verdict_variant
