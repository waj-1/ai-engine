"""Per-request channel resolution + agent-kind gating.

Bridges the static ``agent_channels`` admin table to the live runtime:

* On every agent-bearing request, look at the inbound ``Origin``
  header and find the channel whose ``allowed_origins`` matches.
* Stash the resolved channel on ``request.state.channel`` so
  downstream handlers (and the Laminar tagger) can read it.
* Optionally enforce that the channel's ``allowed_agent_kinds`` list
  permits the agent the endpoint is about to dispatch to (returns
  403 from the dependency when violated).

Public surface:

* :func:`resolve_channel` — FastAPI dependency, returns the channel
  dict (or ``None`` when no origin / no match — public-API path).
* :func:`enforce_channel_kind` — explicit per-endpoint check; pair it
  with the agent_kind string the endpoint dispatches to.
* :func:`any_channel_has_origin_allowlist` — whether ANY channel has
  opted into origin restriction at all (non-empty ``allowed_origins``).
  For a caller that wants to reject an Origin matching no channel (no
  CORS preflight to lean on otherwise — see ``endpoints/voice_live.py``'s
  duplex voice handshake), rejecting is only correct once this is true;
  otherwise every caller of a deployment that never configured origin
  restriction gets punished for a policy nobody turned on.
* :func:`tag_active_span` — attach ``waj.channel.*`` attributes to the
  current Laminar span. Best-effort and import-safe.

Design choices
==============
We picked an *explicit second dependency* over folding the agent_kind
check into ``resolve_channel`` because each endpoint dispatches to a
different agent_kind and
FastAPI dependencies can't read the route handler's local variables.
Keeping ``resolve_channel`` policy-free and adding a one-liner
``enforce_channel_kind(request, "<kind>")`` at the top of every
endpoint is the cleanest split.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Optional

from fastapi import HTTPException
from starlette.requests import HTTPConnection

logger = logging.getLogger(__name__)


_UNKNOWN_SLUG = "unknown"

# Eval-runner correlation headers (admin/evals.py sends them on every scenario
# request). They become ``waj.eval.*`` trace attributes so a test run's case
# can link to the agent's own trace, and so test traffic stays out of the
# default Conversations list. UUID-shaped values only: anything else is
# dropped, never echoed into telemetry.
EVAL_RUN_HEADER = "X-WAJ-Eval-Run"
EVAL_REF_HEADER = "X-WAJ-Eval-Ref"
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def eval_trace_attrs(request: HTTPConnection) -> Dict[str, str]:
    """``waj.eval.run_id`` / ``waj.eval.ref`` from the eval-runner headers."""
    out: Dict[str, str] = {}
    try:
        headers = request.headers
    except Exception:
        return out
    for header, key in ((EVAL_RUN_HEADER, "waj.eval.run_id"), (EVAL_REF_HEADER, "waj.eval.ref")):
        try:
            value = headers.get(header)
        except Exception:
            value = None
        if isinstance(value, str) and _UUID_RE.match(value.strip()):
            value = value.strip()
            out[key] = value.lower()
    return out


def _current_env() -> str:
    """Normalise ``ENVIRONMENT`` to the dev/staging/prod enum used elsewhere.

    Mirrors ``core.env_resolver._current_environment`` so trace tags line up
    with the env enum the admin portal filters on.
    """
    raw = (os.getenv("ENVIRONMENT") or "staging").lower()
    if raw in ("production", "prod"):
        return "prod"
    # 'dev' / development / local collapse to the live staging env (dev retired).
    return "staging"


def _channels_snapshot() -> Dict[str, Dict[str, Any]]:
    """Lazy-import the channels module so this file stays importable
    in unit-test envs where ``core.channels`` couldn't bootstrap."""
    try:
        from core.channels import get_active_channels

        return get_active_channels()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("channel_resolver: get_active_channels failed (%s)", exc)
        return {}


def _match_channel(origin: Optional[str], channels: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the channel whose ``allowed_origins`` contains ``origin``.

    Matching is exact-string. Origin comparison is case-sensitive on
    scheme/host per the URL spec; we lowercase both sides to be
    forgiving since browsers emit lowercase but admin operators
    occasionally paste mixed-case domains.
    """
    if not origin:
        return None
    needle = origin.strip().lower()
    if not needle:
        return None
    for row in channels.values():
        if not isinstance(row, dict):
            continue
        for candidate in row.get("allowed_origins") or []:
            if isinstance(candidate, str) and candidate.strip().lower() == needle:
                return row
    return None


def any_channel_has_origin_allowlist() -> bool:
    """True iff at least one active channel has a non-empty ``allowed_origins``.

    Origin-based restriction here is OPT-IN, exactly like
    ``allowed_agent_kinds`` (an empty list there means "no restriction", not
    "allow nothing" — see :func:`enforce_channel_kind`). This is the same
    convention applied one level up: nothing in this repo enforces "Origin
    must match a channel" on its own; a caller that wants to reject an
    unmatched Origin (the duplex voice handshake, which gets no CORS
    preflight to lean on) must first check whether ANY operator has actually
    populated an allow-list anywhere. Reject before that point and every
    caller of a deployment that has never configured origin restriction gets
    punished for a policy nobody turned on.

    Scoped to whatever channels are visible in THIS deployment's own AI
    Supabase project. A scoped build's DB never carries a full build's
    channels (``admin/seed_channels.py`` seeds only through the plugin, and
    not at all on a scoped build), so this can't be flipped true by
    another deployment's configuration.
    """
    return any((row.get("allowed_origins") or []) for row in _channels_snapshot().values())


def resolve_channel(request: HTTPConnection) -> Optional[Dict[str, Any]]:
    """FastAPI dependency. Returns the channel dict matching the
    inbound ``Origin`` header, or ``None`` when there's no match.

    Non-blocking: a missing/unknown origin is NOT an error here —
    server-to-server calls (the public API path) have no Origin
    header at all and must still proceed to the endpoint's own auth
    gate. We attach whatever we found to ``request.state.channel`` so
    downstream consumers (Laminar tagger, enforce_channel_kind, …)
    can use it.

    Accepts any ``HTTPConnection`` — an HTTP ``Request`` or a ``WebSocket``.
    Both carry ``.headers`` and ``.state``, which is all this needs, and the
    duplex voice edge (``endpoints/voice_live.py``) resolves channels for
    WebSocket handshakes through the same path.
    """
    origin = request.headers.get("origin")
    snapshot = _channels_snapshot()
    channel = _match_channel(origin, snapshot)
    # Always set the attribute so callers can do `getattr(request.state,
    # "channel", None)` without exception-guarding the access.
    request.state.channel = channel
    return channel


def enforce_channel_kind(request: HTTPConnection, agent_kind: str,
                         agent_slug: Optional[str] = None) -> None:
    """Raise 403 if the resolved channel forbids ``agent_kind``.

    Generic agents all share one kind and are told apart by slug, so a
    channel names one of them as ``<kind>:<slug>`` (``custom:support-desk``).
    The bare kind still allows every agent of that kind.

    Called explicitly from endpoint handlers (after ``resolve_channel``
    has populated ``request.state.channel``) because the agent_kind is
    a property of the route, not the request, and FastAPI dependencies
    can't see the route's local agent dispatch.

    Channel ``None`` (no origin / unmatched origin) is treated as the
    public-API path and skipped — those calls go through the normal
    auth gate.
    """
    channel = getattr(request.state, "channel", None)
    if not channel:
        return
    # A slug-addressed voice call names its agent on the connection state.
    agent_slug = agent_slug or getattr(request.state, "agent_slug", None)
    allowed = channel.get("allowed_agent_kinds") or []
    # An empty allow-list means "no restriction" (the admin form lets
    # operators leave it blank to indicate "any kind"). Enforce only
    # when at least one kind is explicitly configured.
    if not allowed:
        return
    if agent_kind in allowed or (agent_slug and f"{agent_kind}:{agent_slug}" in allowed):
        return
    slug = channel.get("slug") or "<unknown>"
    raise HTTPException(
        status_code=403,
        detail=(
            f"channel '{slug}' is not permitted to dispatch agent_kind "
            f"'{agent_kind}' (allowed: {sorted(allowed)})"
        ),
    )


def root_span_attrs(
    request: HTTPConnection,
    *,
    agent_kind: Optional[str] = None,
    session_id: Optional[str] = None,
    agent_slug: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the ``waj.*`` root-span attributes for a request.

    Reads the channel off ``request.state`` (populated by
    ``resolve_channel``) and returns ``waj.channel.slug`` / ``waj.channel.kind``
    plus ``waj.agent_kind`` when provided. Callers pass the result to
    ``common.sse.sse_response(trace_attributes=...)`` so the attributes land on
    the trace's explicit root span (see ``core.observability.root_span``).

    ``session_id`` is tagged as ``waj.session_id`` so the Observability
    Transcript can link a trace back to its persisted ``ai_chat_messages``
    (Laminar's dashboard API doesn't expose the chat session id on the trace,
    so we carry our own on the span — read from ClickHouse).

    ``agent_slug`` (``waj.agent_slug``) names the one agent of a kind many
    agents share (every generic agent is ``custom``), so Conversations can
    filter to one of them.

    Split out from :func:`tag_active_span` so the streaming root span and the
    legacy in-place tagging stay in sync.
    """
    channel = getattr(request.state, "channel", None)
    attrs: Dict[str, Any] = {
        "waj.channel.slug": (channel or {}).get("slug") or _UNKNOWN_SLUG,
        "waj.channel.kind": (channel or {}).get("kind") or _UNKNOWN_SLUG,
        # Tag the runtime environment so ClickHouse rows are env-attributable
        # and the Live Monitor can filter analytics per env. Read here (rather
        # than threaded from call sites) so every endpoint gets it for free.
        "waj.env": _current_env(),
    }
    if agent_kind:
        attrs["waj.agent_kind"] = agent_kind
    if agent_slug:
        attrs["waj.agent_slug"] = agent_slug
    if session_id:
        attrs["waj.session_id"] = session_id
    attrs.update(eval_trace_attrs(request))
    return attrs


def tag_active_span(request: HTTPConnection, *, agent_kind: Optional[str] = None) -> None:
    """Attach ``waj.channel.*`` attributes to the current Laminar span.

    Reads the channel off ``request.state`` (populated by
    ``resolve_channel``) and tags slug + kind. ``agent_kind``, if
    provided, is also tagged so the observability list can be filtered
    by it without joining against ``agent_traces``. Best-effort —
    Laminar import failures are swallowed.

    NOTE: this only lands if a Laminar span is already active when it runs.
    For streaming endpoints the agent (and its spans) start lazily *after* the
    handler returns, so the request root span is opened in
    ``common.sse.sse_response`` from :func:`root_span_attrs` instead.
    """
    attrs = root_span_attrs(request, agent_kind=agent_kind)
    try:
        from lmnr import Laminar

        Laminar.set_span_attributes(attrs)
    except Exception:
        # Span tagging is purely additive — never let it surface to
        # the caller. Common case in unit tests where LMNR_DISABLED=true.
        pass
