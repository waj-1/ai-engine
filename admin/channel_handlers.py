"""Admin CRUD for agent channels.

Channels name request sources (an app, a website, a widget, the public
API, ...) and carry the policy the runtime applies to
each: CORS allow-list, allowed agent_kinds, default profile to dispatch
to, branding, auth + rate-limit knobs.

Sidecar to ``admin/router.py``: attaches `/admin/channels*` routes to
the shared router so the file stays append-only and the parent router
module isn't touched. Imported at the bottom of router.py.

Routes:
    GET   /admin/channels                   → channels_list.html
    GET   /admin/channels/new               → channel_detail.html (blank form)
    POST  /admin/channels                   → create, then redirect to detail
    GET   /admin/channels/{channel_id}      → channel_detail.html (edit form)
    POST  /admin/channels/{channel_id}      → update, then redirect to detail
    POST  /admin/channels/{channel_id}/delete → soft-delete (is_active=false)

All writes audit-log via ``admin.audit.record_change`` and bust the
channels cache in ``core.channels`` so the runtime sees fresh policy on
the next request.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from admin._business_picker import fetch_businesses_for_picker
from admin.audit import record_change
from admin.auth import require_superuser
from admin.router import router, templates, _safe_list, current_env, client_for
# Writes go through the env-switcher client so they land on the same branch
# the list/detail reads (`client_for(env)`) query — a bare
# `get_service_client()` write targets the LOCAL branch regardless of the
# selected env (identical behavior while cross-env vars are unset).
from core.auth import admin_service_client

logger = logging.getLogger(__name__)


# Channel kinds (core/channel_kinds.py): the generic ones plus any a plugin
# adds; keep them in sync with the agent_channels CHECK constraint. A tenant
# kind is one whose channel belongs to exactly one business, so it requires a
# business_id. Fixed per process (the plugins are), hence module-level.
from core.channel_kinds import channel_kind_labels, tenant_channel_kinds  # noqa: E402

CHANNEL_KIND_LABELS: Dict[str, str] = channel_kind_labels()
_CHANNEL_KINDS: Tuple[str, ...] = tuple(CHANNEL_KIND_LABELS)
_TENANT_KINDS: Tuple[str, ...] = tenant_channel_kinds()
# The kind the create form preselects: the first one this build knows.
templates.env.globals["default_channel_kind"] = _CHANNEL_KINDS[0]
# The New channel form's example Name / Short code name (placeholders only).
from core.channel_kinds import channel_name_example  # noqa: E402

templates.env.globals["channel_name_example"] = channel_name_example()
templates.env.globals["channel_kind_label"] = lambda k: CHANNEL_KIND_LABELS.get(k or "", k or "—")

# Mirrors `_AGENT_KINDS_FOR_TOOLS` in tool_handlers.py — the agent_kinds a
# channel may be configured to dispatch to. The DB stores these as a free
# text[] so re-running the migration adding a new kind to enum doesn't
# require a channels schema change; we still constrain at the form layer.
from admin.agent_kinds import BASE_AGENT_KINDS as _AGENT_KINDS_FOR_CHANNELS  # noqa: E402

# Channel *kinds* that are a full build's own surfaces (a plugin's
# `full_build_channel_kinds`). A scoped build hides them from the list even
# though their allowed_agent_kinds is empty (every seeded channel has [] there,
# so the agent-kind intersection alone can't distinguish them). Generic kinds —
# ``other`` / ``public_api`` / ``embed`` — stay.
from admin.agent_kinds import _WAJ_ONLY_CHANNEL_KINDS  # noqa: E402  (one source)


def _channel_kinds_for_build() -> List[str]:
    """Channel kinds the create/edit forms + kind chips offer: a partner
    build never offers a full build's own surfaces."""
    from core.deployment import scoped_build

    kinds = list(_CHANNEL_KINDS)
    if scoped_build():
        kinds = [k for k in kinds if k not in _WAJ_ONLY_CHANNEL_KINDS]
    return kinds


def _channel_agent_kinds(*, include_system: bool = False) -> Tuple[str, ...]:
    """Agent kinds a channel may dispatch to. Client kinds (main first, then extras; e.g. ``("tenant",)``)
    on any scoped build; otherwise the local WAJ list (unchanged
    behavior). Mirrors ``tool_handlers._tool_agent_kinds`` /
    ``_channel_picker.visible_agent_kinds_for_picker`` — an older gate checked
    for one scope by name and fell through to the full list on every other
    scope."""
    # Partner build: its own agents; WAJ build: the shared live list (base
    # kinds + every live agent), so a channel can be restricted to any agent
    # that has a profile.
    from admin.agent_kinds import build_agent_kinds
    return build_agent_kinds(include_system=include_system)


def visible_channels(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Channel rows this build may show (``admin.agent_kinds.channel_in_build``):
    on a partner build, WAJ-product surfaces, channels restricted to or
    pinned on another agent, and another partner's channel (a client's
    channel on the agents build) are hidden. No-op on the full WAJ engine.
    Shared by the /channels list, the observability channel picker and
    search so all three narrow identically."""
    from admin.agent_kinds import build_kinds, channel_in_build

    if build_kinds() is None:
        return list(rows)
    try:
        from core.auth import admin_service_client

        profile_kinds = {
            p.get("profile_id"): p.get("agent_kind")
            for p in (admin_service_client().table("agent_profiles")
                      .select("profile_id, agent_kind").execute().data or [])
        }
    except Exception:  # noqa: BLE001 - narrowing is presentational
        profile_kinds = None
    return [r for r in rows if channel_in_build(r, profile_kinds=profile_kinds)]

# slug: same shape as tool python_name — lower alnum + underscore + hyphen,
# must start with a letter. Length 2–63 keeps it URL-safe everywhere we'd
# embed it (subdomains, header values, JSON keys).
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{1,62}$")

# Origin: scheme + host[:port], explicitly no path / query / fragment so
# the runtime can do exact string-compare against `request.headers["origin"]`
# (the browser sends the origin without a trailing slash).
_ORIGIN_RE = re.compile(r"^https?://[^/\s]+$")

_RATE_LIMIT_MIN = 1
_RATE_LIMIT_MAX = 3600


# ---------------------------------------------------------------------------
# Form helpers
# ---------------------------------------------------------------------------


def _parse_origin_list(raw: Optional[str]) -> List[str]:
    """Split the textarea body on newlines/commas, strip empties, dedupe.

    Does NOT validate — callers run `_validate_origins` separately so the
    error message can name the offending line.
    """
    if not raw:
        return []
    out: List[str] = []
    seen: set[str] = set()
    for piece in raw.replace(",", "\n").splitlines():
        origin = piece.strip()
        if not origin or origin in seen:
            continue
        seen.add(origin)
        out.append(origin)
    return out


def _validate_origins(origins: List[str]) -> Optional[str]:
    """Return the first invalid origin, or None if every entry passes."""
    for origin in origins:
        if not _ORIGIN_RE.match(origin):
            return origin
    return None


def _channel_agent_choices() -> Tuple[str, ...]:
    """The "Agents allowed on this channel" boxes: one per AGENT. A product
    kind is one agent; every generic agent shares the kind ``custom`` and is
    offered by name as ``custom:<slug>`` (admin.agent_kinds), the value the
    runtime checks (core.channel_resolver.enforce_channel_kind)."""
    from admin.agent_kinds import expand_agent_choices, custom_agent_rows

    kinds = list(_channel_agent_kinds())
    if "custom" not in kinds and _custom_kind_served() and custom_agent_rows():
        kinds.append("custom")
    return expand_agent_choices(kinds)


def _custom_kind_served() -> bool:
    from admin.agent_kinds import build_kinds

    return build_kinds() is None


def _channel_choice_known(value: str) -> bool:
    """A ``custom:<slug>`` value naming a generic agent that exists."""
    from admin.agent_kinds import custom_agent_rows, split_agent_choice

    kind, slug = split_agent_choice(value)
    return bool(slug) and _custom_kind_served() and any(
        r.get("slug") == slug for r in custom_agent_rows())


def _parse_agent_kinds(form_values: List[str]) -> List[str]:
    """Filter the multi-checkbox submission down to the known-good list,
    de-duped and order-preserving. A generic agent arrives as
    ``custom:<slug>`` (one box per agent) and is kept when that agent exists."""
    out: List[str] = []
    seen: set[str] = set()
    values = [(v or "").strip() for v in form_values if (v or "").strip()]
    if not values:
        return out
    # Validation accepts system agents too, so saving a channel that already
    # lists one (a title generator, say) never silently drops it.
    allowed = _channel_agent_kinds(include_system=True)
    for v in values:
        if v not in seen and (v in allowed or _channel_choice_known(v)):
            seen.add(v)
            out.append(v)
    return out


def _parse_config_jsonb(raw: Optional[str]) -> Dict[str, Any]:
    """Parse the config textarea. Empty / blank → {} so the operator doesn't
    have to type `{}` for the common case."""
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"config is not valid JSON: {exc}")
    if not isinstance(parsed, dict):
        raise HTTPException(400, "config must be a JSON object")
    return parsed


def _read_channel_form(form) -> Dict[str, Any]:
    """Read a /channels submission into the row payload dict.

    Raises HTTPException(400) with an operator-readable detail on the
    first validation failure so the caller can re-render the form.
    """
    slug = (form.get("slug") or "").strip().lower()
    if not _SLUG_RE.match(slug):
        raise HTTPException(
            400,
            "slug must be lowercase letters/digits/_- (2–63 chars), starting with a letter",
        )

    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")

    kind = (form.get("kind") or "").strip()
    if kind not in _CHANNEL_KINDS:
        raise HTTPException(400, f"kind must be one of {list(_CHANNEL_KINDS)}")

    origins = _parse_origin_list(form.get("allowed_origins"))
    bad = _validate_origins(origins)
    if bad is not None:
        raise HTTPException(
            400,
            f"allowed_origins entry {bad!r} is not a valid origin "
            f"(expected http(s)://host[:port], no trailing slash or path)",
        )

    agent_kinds = _parse_agent_kinds(list(form.getlist("allowed_agent_kinds")))

    default_profile_id = (form.get("default_profile_id") or "").strip() or None

    business_id = (form.get("business_id") or "").strip() or None
    if kind in _TENANT_KINDS and not business_id:
        raise HTTPException(
            400,
            f"{CHANNEL_KIND_LABELS.get(kind, kind)} channels require a business_id "
            "(pick one from the business selector)",
        )
    # Don't store a stray business on any other kind — silently drop it so a
    # kind change doesn't leak a stale tenant link.
    if kind not in _TENANT_KINDS:
        business_id = None

    try:
        rate_limit_rpm = int(form.get("rate_limit_rpm") or 60)
    except (TypeError, ValueError):
        raise HTTPException(400, "rate_limit_rpm must be an integer")
    if not (_RATE_LIMIT_MIN <= rate_limit_rpm <= _RATE_LIMIT_MAX):
        raise HTTPException(
            400,
            f"rate_limit_rpm must be between {_RATE_LIMIT_MIN} and {_RATE_LIMIT_MAX}",
        )

    config = _parse_config_jsonb(form.get("config"))

    return {
        "slug": slug,
        "name": name,
        "kind": kind,
        "is_active": form.get("is_active") == "on",
        "allowed_origins": origins,
        "allowed_agent_kinds": agent_kinds,
        "default_profile_id": default_profile_id,
        "business_id": business_id,
        "auth_required": form.get("auth_required") == "on",
        "rate_limit_rpm": rate_limit_rpm,
        "config": config,
        "description": (form.get("description") or "").strip() or None,
    }


def _fetch_profiles_for_picker(client) -> List[Dict[str, Any]]:
    """Pull active agent_profiles for the default-profile dropdown,
    ordered by agent_kind then display_name so the template can `groupby`."""
    from admin.agent_kinds import scope_to_build

    # A partner build's "Answered by" offers only its own agents.
    return scope_to_build(_safe_list(
        client.table("agent_profiles")
        .select("profile_id, agent_kind, slug, display_name, environment, is_active")
        .order("agent_kind").order("display_name").execute()
    ), also=())


def _invalidate_channels_cache() -> None:
    """Best-effort cache bust — `core.channels` may be unavailable in
    test environments where the module wasn't imported. Tests stub this
    out via patching, so a missing import here just means "no cache to
    bust yet".

    The CORS middleware reads channel origins through the same cache
    (``core.cors.live_channel_origins``), so busting it is all a new or
    edited channel's origins need: no restart."""
    try:
        from core.channels import invalidate as invalidate_channels
    except Exception:  # pragma: no cover - test environments without core.channels
        invalidate_channels = None  # type: ignore[assignment]
    if invalidate_channels is not None:
        try:
            invalidate_channels()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("channels cache invalidate failed: %s", exc)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/channels", response_class=HTMLResponse)
async def channels_list(
    request: Request,
    env: str = Depends(current_env),
    kind: Optional[str] = None,
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    """List every channel with a kind filter (chips, similar to /tools)."""
    client = client_for(env)
    q = client.table("agent_channels").select("*").order("kind").order("slug")
    if kind and kind in _CHANNEL_KINDS:
        q = q.eq("kind", kind)
    rows = _safe_list(q.execute())
    # Scoped build: hide WAJ-only channels so their kind
    # chips don't expose the catalog, and trim the kind chip bar to match.
    # Purely presentational; no-op on the full WAJ engine. An older gate
    # checked for one scope by name and left every OTHER deployment scope
    # showing WAJ's channels + kind chips.
    rows = visible_channels(rows)
    # "Answered by": the default profile's name (was buried as an id mid-form).
    profile_names = {
        p.get("profile_id"): (p.get("display_name") or p.get("slug") or "")
        for p in _fetch_profiles_for_picker(client)
    }
    channel_kinds = _channel_kinds_for_build()
    return templates.TemplateResponse(
        request,
        "channels_list.html",
        {
            "ctx": ctx,
            "active": "channels",
            "current_env": env,
            "rows": rows,
            "profile_names": profile_names,
            "kind_filter": kind if kind in _CHANNEL_KINDS else None,
            "channel_kinds": channel_kinds,
        },
    )


@router.get("/channels/new", response_class=HTMLResponse)
async def channel_new_form(
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    """Blank create form. Same template as the edit screen so we only
    maintain one form definition."""
    client = client_for(env)
    return templates.TemplateResponse(
        request,
        "channel_detail.html",
        {
            "ctx": ctx,
            "active": "channels",
            "current_env": env,
            "channel": None,
            "form": {
                # Defaults that match the DB defaults so the form mirrors
                # what an INSERT with no overrides would produce.
                "is_active": True,
                "auth_required": True,
                "rate_limit_rpm": 60,
                # The first kind this build offers (a plugin's own first).
                "kind": (_channel_kinds_for_build() or [None])[0],
            },
            "channel_kinds": _channel_kinds_for_build(),
            "tenant_kinds": list(_TENANT_KINDS),
            "agent_kinds": list(_channel_agent_choices()),
            "profiles": _fetch_profiles_for_picker(client),
            "businesses": fetch_businesses_for_picker(client),
            "error": None,
        },
    )


@router.post("/channels")
async def channel_create(
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> Response:
    form = await request.form()
    try:
        payload = _read_channel_form(form)
    except HTTPException as exc:
        return _render_form_with_error(request, env, ctx, form, exc.detail, channel=None)

    client = admin_service_client()
    try:
        inserted = client.table("agent_channels").insert(
            {**payload, "created_by": ctx["user_id"]}
        ).execute()
    except Exception as exc:  # unique-slug collisions surface here
        return _render_form_with_error(
            request, env, ctx, form, f"insert failed: {exc}", channel=None
        )
    new_row = (inserted.data or [{}])[0]
    new_id = new_row.get("channel_id")
    if not new_id:
        return _render_form_with_error(
            request, env, ctx, form, "insert succeeded but no channel_id returned",
            channel=None,
        )
    record_change(
        entity_type="agent_channels",
        entity_id=new_id,
        action="create",
        actor_user_id=ctx["user_id"],
        after=new_row,
    )
    _invalidate_channels_cache()
    return RedirectResponse(url=f"/admin/channels/{new_id}", status_code=303)


@router.get("/channels/{channel_id}", response_class=HTMLResponse)
async def channel_detail(
    channel_id: str,
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    client = client_for(env)
    rows = _safe_list(
        client.table("agent_channels").select("*")
        .eq("channel_id", channel_id).limit(1).execute()
    )
    if not rows:
        raise HTTPException(404, "channel not found")
    channel = rows[0]
    return templates.TemplateResponse(
        request,
        "channel_detail.html",
        {
            "ctx": ctx,
            "active": "channels",
            "current_env": env,
            "channel": channel,
            "form": channel,  # template reads `form.*` for value attrs
            "channel_kinds": _channel_kinds_for_build(),
            "tenant_kinds": list(_TENANT_KINDS),
            "agent_kinds": list(_channel_agent_choices()),
            "profiles": _fetch_profiles_for_picker(client),
            "businesses": fetch_businesses_for_picker(client),
            "error": None,
        },
    )


@router.post("/channels/{channel_id}")
async def channel_update(
    channel_id: str,
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> Response:
    client = admin_service_client()
    before = _safe_list(
        client.table("agent_channels").select("*")
        .eq("channel_id", channel_id).limit(1).execute()
    )
    if not before:
        raise HTTPException(404, "channel not found")

    form = await request.form()
    try:
        payload = _read_channel_form(form)
    except HTTPException as exc:
        return _render_form_with_error(
            request, env, ctx, form, exc.detail, channel=before[0]
        )

    try:
        client.table("agent_channels").update(payload).eq(
            "channel_id", channel_id
        ).execute()
    except Exception as exc:
        return _render_form_with_error(
            request, env, ctx, form, f"update failed: {exc}", channel=before[0]
        )
    record_change(
        entity_type="agent_channels",
        entity_id=channel_id,
        action="update",
        actor_user_id=ctx["user_id"],
        before=before[0],
        after={**before[0], **payload},
    )
    _invalidate_channels_cache()
    return RedirectResponse(url=f"/admin/channels/{channel_id}", status_code=303)


@router.post("/channels/{channel_id}/delete")
async def channel_delete(
    channel_id: str,
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Soft-delete by flipping is_active=false. Channels are referenced
    from `agent_traces` / log analytics in downstream PRs, so a hard
    DELETE would orphan history — keep the row, hide it from the active
    set."""
    client = admin_service_client()
    rows = _safe_list(
        client.table("agent_channels").select("*")
        .eq("channel_id", channel_id).limit(1).execute()
    )
    if not rows:
        raise HTTPException(404, "channel not found")
    before = rows[0]
    client.table("agent_channels").update({"is_active": False}).eq(
        "channel_id", channel_id
    ).execute()
    record_change(
        entity_type="agent_channels",
        entity_id=channel_id,
        action="deprecate",
        actor_user_id=ctx["user_id"],
        before=before,
        after={**before, "is_active": False},
    )
    _invalidate_channels_cache()
    return RedirectResponse(url="/admin/channels", status_code=303)


# ---------------------------------------------------------------------------
# Form-render helper for the error path. Lives at the bottom because it
# pulls `_fetch_profiles_for_picker` and `fetch_businesses_for_picker` and
# we want the public routes to read top-down.
# ---------------------------------------------------------------------------


def _render_form_with_error(
    request: Request,
    env: str,
    ctx: dict,
    form,
    message: str,
    channel: Optional[Dict[str, Any]],
) -> Response:
    """Re-render channel_detail.html with the operator's input echoed and
    an error message at the top. Returns 400 so HTMX/HTTP clients can
    detect the validation failure programmatically."""
    client = client_for(env)
    # Echo back every submitted field so the user doesn't lose their work.
    # The template reads list values via getlist for allowed_agent_kinds.
    echo = {k: form.get(k) for k in form.keys()}
    echo["allowed_agent_kinds"] = [v for v in form.getlist("allowed_agent_kinds") if v]
    echo["is_active"] = form.get("is_active") == "on"
    echo["auth_required"] = form.get("auth_required") == "on"
    return templates.TemplateResponse(
        request,
        "channel_detail.html",
        {
            "ctx": ctx,
            "active": "channels",
            "current_env": env,
            "channel": channel,
            "form": echo,
            "channel_kinds": _channel_kinds_for_build(),
            "tenant_kinds": list(_TENANT_KINDS),
            "agent_kinds": list(_channel_agent_choices()),
            "profiles": _fetch_profiles_for_picker(client),
            "businesses": fetch_businesses_for_picker(client),
            "error": message,
        },
        status_code=400,
    )
