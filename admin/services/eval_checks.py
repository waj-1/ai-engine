"""Deterministic per-turn checks for eval scenarios (chat AND voice runners).

A judge grades prose; these grade FACTS the prose cannot fake — the same
spirit as `expected_tools` and the voice runner's `http_check`. A scenario
opts in with a `checks` list (single-turn) or per-turn `turns[].checks`:

    {"type": "no_uuid"}                        no record id (UUID) in the answer
    {"type": "regex", "pattern": "..."}        answer must match (case-insensitive)
    {"type": "not_regex", "pattern": "..."}    answer must not match
    {"type": "no_question", "pattern": "..."}  no question back to the caller
                                               (whose text matches `pattern`, if
                                               given — e.g. about a branch)
    {"type": "asks"}                           the turn asks a clarifying question
    {"type": "tools_any", "tools": [...]}      at least one of these tools ran
    {"type": "proposal", "kind": "block_time",
     "payload": {"start": "{d+1}T15:00", "staff_id": "{staff:Sara}"}}
                                               exactly one card of `kind` whose
                                               STORED payload has these values
                                               (prefix match for strings)
    {"type": "no_proposal"}                    this turn created no card
    {"type": "truth", "truth": "revenue_month", "rel": 0.03}
                                               a figure in the answer matches a
                                               DIRECT report query (digits or
                                               spoken number words, EN/AR)
    {"type": "no_saved_claim"}                 never says a change is saved/done
    {"type": "screen_backed"}                  "it's on your screen" only when the
                                               screen got a table, a chart or a card
                                               this turn (user voice test 2026-09-30)

Placeholders in payload values: `{d+N}` (today + N days, ISO), `{next:thursday}`
(next such weekday, ISO), `{staff:<name>}` / `{service:<name>}` /
`{category:<name>}` (that record's id, looked up with the caller's token).

`proposal` and `truth` (and any other check type) come from a plugin's
"eval_checks" hook (`check_types()`), with the facts they read
(`facts(business_id, user_token)`): a deployment computes its truths once per
(tenant, day) with the same functions its agent's tools call, so "the numbers
match" is checked against the database, not against a string frozen into the
suite. In a build with no plugin those types report "unknown check type".
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, timedelta
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from common.screen_claims import SCREEN_CLAIM

logger = logging.getLogger(__name__)

UUID_FRAGMENT = re.compile(r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}|…|\.\.\.)")
_DIGIT_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٬٫", "01234567890123456789,.")
_NUM = re.compile(
    r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*(k\b|thousand\b|ألف|آلاف|الف)?", re.IGNORECASE)
_QUESTION_SENT = re.compile(r"[^.!?؟\n]*[?؟]")
_ASKS_STATEMENT = re.compile(
    r"\bI need (?:the|to know|a)\b|\bwhat time\b|\btell me (?:the|what|which)\b"
    r"|أحتاج|احتاج|لازم أعرف|لازم اعرف|قل لي|قول لي|أي وقت|وش الوقت|كم الساعة", re.IGNORECASE)
_SAVED_EN = re.compile(
    r"\b(it'?s|it has been|has been|is now|i'?ve|i have|was)\s+(saved|done|created|blocked|added|"
    r"updated|booked)\b|\ball set\b|(^|[.!]\s*)done\b", re.IGNORECASE)
# «تم حفظ» but not «ليتم حفظه» / «لن يتم حفظ» (a future, conditional save).
_SAVED_AR = re.compile(r"(?<![\u0600-\u06FF])(تم|تمّ)\s*(الحفظ|حفظ|الحجز|حجز|الإضافة|إضافة|التعديل)"
                       r"|حفظتها|حفظته|خلاص حفظ")
# One claim pattern for the delegate, the brain and this check.
_SCREEN_CLAIM = SCREEN_CLAIM
# A row starts with a pipe and has another; the closing pipe is optional
# (the model drops it on a table's last row — voice/live/speakable.py).
_TABLE_ROW = re.compile(r"^\s*\|.*\|.*$", re.M)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# Spoken counts: "seven bookings", «ست حجوزات», «ثمانٍ وأربعين».
_EN_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen".split())}
_EN_TENS = {w: 10 * i for i, w in enumerate(
    "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
_EN_WORD = re.compile(
    r"\b(" + "|".join(sorted(_EN_TENS, key=len, reverse=True)) + r")(?:[- ]("
    + "|".join(k for k in _EN_UNITS if _EN_UNITS[k] < 10) + r"))?\b|\b("
    + "|".join(sorted(_EN_UNITS, key=len, reverse=True)) + r")\b", re.I)
_AR_UNITS = [("واحد", 1), ("وحدة", 1), ("اثنين", 2), ("ثنتين", 2), ("حجزين", 2), ("ثلاث", 3),
             ("أربع", 4), ("اربع", 4), ("خمس", 5), ("ست", 6), ("سبع", 7), ("ثمان", 8), ("تسع", 9),
             ("عشر", 10)]
_AR_TENS = [("عشرين", 20), ("ثلاثين", 30), ("أربعين", 40), ("اربعين", 40), ("خمسين", 50),
            ("ستين", 60), ("سبعين", 70), ("ثمانين", 80), ("تسعين", 90)]


# ---------------------------------------------------------------------------
# Text helpers (pure)
# ---------------------------------------------------------------------------
def numbers_in(text: str) -> List[float]:
    out: List[float] = []
    for m in _NUM.finditer((text or "").translate(_DIGIT_MAP)):
        whole, frac, mult = m.group(1), m.group(2), m.group(3)
        try:
            value = float(whole.replace(",", "") + ("." + frac if frac else ""))
        except ValueError:
            continue
        out.append(value * 1000 if mult else value)
    return out


def word_numbers(text: str) -> List[float]:
    out: List[float] = []
    for m in _EN_WORD.finditer(text or ""):
        if m.group(1):
            out.append(_EN_TENS[m.group(1).lower()] + (_EN_UNITS[m.group(2).lower()] if m.group(2) else 0))
        else:
            out.append(_EN_UNITS[m.group(3).lower()])
    text = re.sub(r"[ً-ْـ]", "", text or "")
    for token in re.findall(r"[؀-ۿ]+(?:\s+و[؀-ۿ]+)?", text):
        total, ok = 0, False
        for part in re.split(r"\s+و", token):
            part = re.sub(r"^ال", "", part)
            val = next((v for w, v in _AR_TENS if part.startswith(w)), None)
            if val is None:
                val = next((v for w, v in _AR_UNITS if part == w or part.startswith(w)
                            and part[len(w):] in ("", "ة", "ه", "ا", "ية", "يه", "ي")), None)
            if val is not None:
                total, ok = total + val, True
        if ok:
            out.append(float(total))
    return out


_COUNT_NOUN = re.compile(r"bookings?|appointments?|حجوزات|حجوزه|حجوزة|حجز|مواعيد|موعد", re.I)
_NONE_COUNT = re.compile(r"\bno (?:bookings?|appointments?)\b|ما (?:عندك|فيه|في) (?:أي )?(?:حجوزات|حجز|مواعيد)"
                         r"|لا (?:يوجد|توجد) (?:أي )?(?:حجوزات|حجز)", re.I)
_MONTHS_EN = ("january february march april may june july august september october november "
              "december").split()
_MONTHS_AR = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر",
              "نوفمبر", "ديسمبر"]
_WEEKDAYS_AR = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]


def counts_in(text: str) -> List[float]:
    """Numbers stated AS a count of bookings — in the words right before a
    booking noun ("6 bookings", «ست حجوزات», "forty-eight bookings") — not
    every digit in the answer (a date, a time)."""
    norm = (text or "").translate(_DIGIT_MAP)
    out: List[float] = []
    for m in _COUNT_NOUN.finditer(norm):
        window = norm[max(0, m.start() - 28): m.start()]
        window = window.split(".")[-1].split("،")[-1]
        nums = numbers_in(window) + word_numbers(window)
        if nums:
            out.append(nums[-1])
    if _NONE_COUNT.search(norm):
        out.append(0.0)
    return out


def mentions_day(text: str, d: date) -> bool:
    """`d` named as a DATE: ISO, "28 September"/"September 28", «28 سبتمبر»,
    or its weekday name (EN/AR) — not any 28 in the answer."""
    t = (text or "").translate(_DIGIT_MAP)
    if d.isoformat() in t:
        return True
    for month in (_MONTHS_EN[d.month - 1], _MONTHS_AR[d.month - 1]):
        if re.search(rf"\b0?{d.day}(?:st|nd|rd|th)?\s+(?:of\s+)?{month}|{month}\s+0?{d.day}(?!\d)", t, re.I):
            return True
    return (d.strftime("%A").lower() in t.lower()) or (_WEEKDAYS_AR[d.weekday()] in t)


def near(value: float, target: float, rel: float) -> bool:
    if target == 0:
        return abs(value) < 0.5
    return abs(value - target) <= max(rel * abs(target), 0.5 if rel else 0.0)


def questions_in(text: str) -> List[str]:
    return [q.strip() for q in _QUESTION_SENT.findall(text or "") if q.strip()]


# ---------------------------------------------------------------------------
# Context: ids + truths, from the plugin, once per tenant/day
# ---------------------------------------------------------------------------


def _run(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(1) as ex:
        return ex.submit(asyncio.run, coro).result()


def _plugin_hooks() -> list:
    from core.plugins import load_hook

    return load_hook("eval_checks")


def context_facts(business_id: str, user_token: str) -> Dict[str, Any]:
    """Facts a run's checks resolve against — `today` (the tenant's date),
    `ids` (placeholder name -> record id) and `truths` (figures computed by
    a direct query). The engine knows only today's date on the eval host; a
    plugin's `facts(business_id, user_token)` supplies the rest for its own
    agents' data."""
    from core.plugins import call_hook

    for found in call_hook(_plugin_hooks(), "facts", business_id, user_token):
        if found:
            return found
    return {"truths": {}, "ids": {}, "today": date.today()}


def _plugin_check_types() -> Dict[str, Any]:
    """{check type: fn(check, turn) -> (ok, detail)} from the plugins."""
    from core.plugins import call_hook

    out: Dict[str, Any] = {}
    for found in call_hook(_plugin_hooks(), "check_types"):
        for name, fn in found.items():
            out.setdefault(name, fn)
    return out


def _resolve(value: Any, facts: Dict[str, Any]) -> Any:
    if not isinstance(value, str) or "{" not in value:
        return value
    today: date = facts["today"]

    def sub(m: "re.Match[str]") -> str:
        tok = m.group(1)
        if tok.startswith("d+") or tok.startswith("d-") or tok == "d":
            return str(today + timedelta(days=int(tok[1:] or 0)))
        if tok.startswith("next:"):
            target = _WEEKDAYS.index(tok[5:].lower()) + 1
            delta = (target - today.isoweekday()) % 7 or 7
            return str(today + timedelta(days=delta))
        return str(facts["ids"].get(tok, f"<missing {tok}>"))

    return re.sub(r"\{([^{}]+)\}", sub, value)


def _get(payload: Dict[str, Any], dotted: str) -> Any:
    cur: Any = payload
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def _matches(got: Any, want: Any) -> bool:
    if isinstance(want, dict) and "any" in want:
        return any(_matches(got, w) for w in want["any"])
    if isinstance(want, str) and isinstance(got, str):
        return got.lower().startswith(want.lower())
    if isinstance(want, (int, float)) and isinstance(got, (int, float)):
        return abs(float(got) - float(want)) < 1e-6
    if isinstance(want, list) and isinstance(got, list):
        return all(any(_matches(g, w) for g in got) for w in want)
    return got == want


# ---------------------------------------------------------------------------
# The check engine
# ---------------------------------------------------------------------------
@dataclass
class Turn:
    """What a plugin check type sees of one turn: the answer, its cards
    (`proposal` events), the tools that ran, the caller's words, the run's
    facts (`need_facts()`, computed once) and `resolve(value)`, which fills
    the `{d+N}` / `{next:<weekday>}` / `{<id key>}` placeholders."""

    answer: str
    proposals: List[Dict[str, Any]]
    tools: List[str]
    caller_text: str
    need_facts: Callable[[], Dict[str, Any]]
    resolve: Callable[[Any], Any]


def run_checks(
    checks: Sequence[Dict[str, Any]],
    *,
    answer: str,
    proposals: Sequence[Dict[str, Any]] = (),
    tools: Sequence[str] = (),
    caller_text: str = "",
    business_id: Optional[str] = None,
    user_token: Optional[str] = None,
    spoken: Optional[str] = None,
    shown: Optional[str] = None,
    charts: int = 0,
) -> List[Dict[str, Any]]:
    """Evaluate `checks` for one turn. Never raises; a broken check fails.

    `spoken` / `shown` / `charts` separate a voice turn's channels for
    `screen_backed`; omitted (chat), the answer text is both."""
    out: List[Dict[str, Any]] = []
    facts: Optional[Dict[str, Any]] = None

    def need_facts() -> Dict[str, Any]:
        nonlocal facts
        if facts is None:
            facts = context_facts(business_id or "", user_token or "") if business_id and user_token \
                else {"truths": {}, "ids": {}, "today": date.today()}
        return facts

    plugin_types = _plugin_check_types()
    turn = Turn(answer=answer or "", proposals=list(proposals), tools=list(tools),
                caller_text=caller_text, need_facts=need_facts,
                resolve=lambda value: _resolve(value, need_facts()))

    for check in checks or []:
        kind = str(check.get("type") or "")
        ok, detail = False, ""
        try:
            if kind == "no_uuid":
                m = UUID_FRAGMENT.search(answer or "")
                ok, detail = m is None, (m.group(0) if m else "")
            elif kind == "regex":
                ok = bool(re.search(check["pattern"], answer or "", re.I))
                detail = check["pattern"]
            elif kind == "not_regex":
                m = re.search(check["pattern"], answer or "", re.I)
                ok, detail = m is None, (m.group(0) if m else "")
            elif kind == "no_question":
                qs = questions_in(answer)
                pat = check.get("pattern")
                bad = [q for q in qs if not pat or re.search(pat, q, re.I)]
                ok, detail = not bad, (bad[0][:120] if bad else "")
            elif kind == "asks":
                ok = bool(questions_in(answer)) or bool(_ASKS_STATEMENT.search(answer or ""))
            elif kind == "tools_any":
                want = list(check.get("tools") or [])
                ok = bool(set(want) & set(tools))
                detail = f"ran {list(tools)}"
            elif kind == "no_proposal":
                ok, detail = not proposals, str([p.get("kind") for p in proposals])
            elif kind in plugin_types:
                ok, detail = plugin_types[kind](check, turn)
            elif kind == "screen_backed":
                said = answer if spoken is None else spoken
                seen = answer if shown is None else shown
                m = _SCREEN_CLAIM.search(said or "")
                backed = bool(_TABLE_ROW.search(seen or "")) or charts > 0 or bool(proposals)
                ok = m is None or backed
                detail = "" if ok else f"says {m.group(0)!r} with no table, chart or card on screen"
            elif kind == "no_saved_claim":
                m = _SAVED_EN.search(answer or "") or _SAVED_AR.search(answer or "")
                ok, detail = m is None, (m.group(0) if m else "")
            else:
                detail = f"unknown check type {kind!r}"
        except Exception as exc:  # a broken check fails, never crashes a run
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        out.append({"type": kind, "ok": ok, "detail": detail})
    return out


def proposals_from_sse(body: str) -> List[Dict[str, Any]]:
    """`{"type": "proposal", "proposal": {...}}` events of one SSE response."""
    found: List[Dict[str, Any]] = []
    for line in (body or "").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            evt = json.loads(line[5:].strip())
        except Exception:
            continue
        if isinstance(evt, dict) and evt.get("type") == "proposal" and isinstance(evt.get("proposal"), dict):
            found.append(evt["proposal"])
    return found


def _status_templates() -> List["re.Pattern[str]"]:
    from runtime.prompts._ui import UI_MESSAGES

    pats = []
    for loc in ("en", "ar"):
        for key in ("using_tool", "retrieved_data", "preparing_change", "prepared_change"):
            tmpl = (UI_MESSAGES.get(loc) or {}).get(key)
            if tmpl:
                pats.append(re.compile("^" + re.escape(tmpl).replace(re.escape("{tool_name}"), "(.+?)") + "$"))
    return pats


def tools_from_status_lines(lines: Sequence[str]) -> List[str]:
    """Tool names from an agent's localized status lines ("Using
    bookings...", «جاري الاطلاع على ...»). A plugin maps its agents'
    human labels back to tool names (`status_tool_labels() -> {tool: (en,
    ar)}`); a label with no mapping reads as its own name."""
    from core.plugins import call_hook

    TOOL_LABELS: Dict[str, Any] = {}
    for found in call_hook(_plugin_hooks(), "status_tool_labels"):
        for tool, pair in found.items():
            TOOL_LABELS.setdefault(tool, pair)

    by_label: Dict[str, str] = {}
    for tool, (en, ar) in TOOL_LABELS.items():
        by_label.setdefault(en.lower(), tool)
        by_label.setdefault(ar, tool)
    found: set = set()
    pats = _status_templates()
    for line in lines:
        for pat in pats:
            m = pat.match((line or "").strip())
            if m:
                label = m.group(1).strip()
                tool = by_label.get(label.lower()) or by_label.get(label)
                found.add(tool or label.replace(" ", "_"))
                break
    return sorted(found)


def tools_from_sse(body: str) -> List[str]:
    lines = []
    for line in (body or "").splitlines():
        line = line.strip()
        if line.startswith("data:"):
            try:
                evt = json.loads(line[5:].strip())
            except Exception:
                continue
            if isinstance(evt, dict) and evt.get("type") == "thought" and evt.get("content"):
                lines.append(str(evt["content"]))
    return tools_from_status_lines(lines)
