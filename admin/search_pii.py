"""Query classification + PII masking for the portal's global search.

Kept free of I/O so it is trivially testable and importable from anywhere.

Rules (docs/admin-portal/04-govern.md § Global search):

* A person's name, phone or email is shown **partially**: enough for the
  operator who typed the query to recognise the right row, never the full
  value. Names keep the first name and the initial of each other part
  ("Celine V."); phones keep the country code and the last four digits
  ("+971 ••• 4567"); emails keep two characters and the domain
  ("ce•••@example.com").
* Query terms that look like a phone number are never written to a log —
  ``redact_query`` replaces every run of 5+ digits before anything logs it.
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Optional

_BULLET = "•"
_DIGIT_RUN = re.compile(r"\d[\d\s().+-]{3,}\d")
_PHONE_SHAPE = re.compile(r"^\+?[\d\s().-]{7,}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_HEX_ID = re.compile(r"^[0-9a-f-]{6,36}$", re.I)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_RECORD_ID = re.compile(r"^[A-Za-z0-9.-]{8,64}$")

# Country calling codes WAJ customers dial from; used to derive the local
# number a `phone_number` column stores without its `country_code`.
_COUNTRY_CODES = ("971", "966", "962", "965", "968", "973", "974", "20", "961", "964", "1", "44")


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def digits_of(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def is_phone(q: str) -> bool:
    """A query that is (only) a phone number: 7+ digits, phone punctuation."""
    q = (q or "").strip()
    return bool(_PHONE_SHAPE.match(q)) and len(digits_of(q)) >= 7


def is_email(q: str) -> bool:
    return bool(_EMAIL.match((q or "").strip()))


def is_uuid(q: str) -> bool:
    return bool(_UUID.match((q or "").strip()))


def is_id_fragment(q: str) -> bool:
    """A session/trace id or a prefix of one: hex (+dashes), 6..36 chars,
    with at least one digit (so words like "decade" don't count) and at least
    one a-f letter (so a phone number doesn't)."""
    q = (q or "").strip()
    return (bool(_HEX_ID.match(q)) and any(c.isdigit() for c in q)
            and any(c in "abcdefABCDEF" for c in q) and len(q.replace("-", "")) >= 6)


def looks_like_record_id(q: str) -> bool:
    """An external record id (a key a plugin stamps on session metadata, see
    ``search_sources.session_record_keys``): no spaces, 8+ id characters, at
    least one digit."""
    q = (q or "").strip()
    return bool(_RECORD_ID.match(q)) and any(c.isdigit() for c in q)


def phone_candidates(q: str) -> list[str]:
    """The ways a dialled number can be stored in a ``phone_number`` column
    (with/without country code, with/without a trunk 0), most specific first."""
    d = digits_of(q)
    if len(d) < 7:
        return []
    out = [d]
    stripped = d.lstrip("0")
    if stripped != d:
        out.append(stripped)
    for cc in _COUNTRY_CODES:
        if d.startswith(cc) and len(d) - len(cc) >= 7:
            local = d[len(cc):]
            out.extend([local, "0" + local])
    if not any(d.startswith(cc) for cc in _COUNTRY_CODES):
        out.extend(cc + stripped for cc in ("971", "966"))
    seen: set = set()
    return [x for x in out if not (x in seen or seen.add(x))]


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------


def mask_name(name: Optional[str]) -> str:
    """First name in full, other parts as an initial: "Celine Verify" →
    "Celine V.". A single-word name keeps its first three letters."""
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    if not parts:
        return ""
    if len(parts) == 1:
        word = parts[0]
        return word if len(word) <= 3 else word[:3] + _BULLET * 3
    return " ".join([parts[0]] + [p[0] + "." for p in parts[1:]])


def mask_phone(phone: Optional[str], country_code: Optional[str] = None) -> str:
    """Country code + last four digits: "+971 ••• 4567"."""
    d = digits_of(phone or "")
    if not d:
        return ""
    cc = digits_of(country_code or "")
    if not cc:
        for c in _COUNTRY_CODES:
            if d.startswith(c) and len(d) - len(c) >= 7:
                cc, d = c, d[len(c):]
                break
    tail = d[-4:] if len(d) > 4 else d
    head = f"+{cc} " if cc else ""
    return f"{head}{_BULLET * 3} {tail}"


def mask_email(email: Optional[str]) -> str:
    """Two characters of the local part + the domain: "ce•••@example.com"."""
    e = (email or "").strip()
    if "@" not in e:
        return ""
    local, _, domain = e.partition("@")
    return f"{local[:2]}{_BULLET * 3}@{domain}"


def mask_id(value: Optional[str]) -> str:
    """Short id for context lines: first 8 characters."""
    v = (value or "").strip()
    return v[:8] + "…" if len(v) > 8 else v


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def redact_query(text: Optional[str]) -> str:
    """Replace phone-like digit runs so a query is safe to log."""
    return _DIGIT_RUN.sub("[number]", text or "")


class SearchQueryLogFilter(logging.Filter):
    """Redacts the ``q=`` value of global-search requests in access logs.

    uvicorn's access logger formats ``(client, method, path, version, status)``
    — the path carries the query string, so a GET search for a phone number
    would otherwise land in the container logs verbatim. The portal itself
    POSTs its queries (body, never logged); this covers GET callers."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            args = record.args
            if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) \
                    and "/admin/search.json" in args[2] and "q=" in args[2]:
                path = re.sub(r"([?&]q=)[^&]*", r"\1[redacted]", args[2])
                record.args = args[:2] + (path,) + args[3:]
        except Exception:  # pragma: no cover - never break logging
            pass
        return True


SEARCH_THREAD_PREFIX = "global-search"


class SearchThreadHttpLogFilter(logging.Filter):
    """Drops httpx's per-request INFO lines emitted from global-search worker
    threads: those lines carry the full PostgREST/ClickHouse URL, i.e. the
    search term (``phone_number=in.(…)``, ``param_q=…``). Warnings and errors
    still pass; requests from every other thread are untouched."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if record.levelno > logging.INFO:
            return True
        return not threading.current_thread().name.startswith(SEARCH_THREAD_PREFIX)


def install_access_log_filter() -> None:
    lg = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, SearchQueryLogFilter) for f in lg.filters):
        lg.addFilter(SearchQueryLogFilter())
    for name in ("httpx", "httpcore"):
        hl = logging.getLogger(name)
        if not any(isinstance(f, SearchThreadHttpLogFilter) for f in hl.filters):
            hl.addFilter(SearchThreadHttpLogFilter())
