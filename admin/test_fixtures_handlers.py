"""/admin/test-fixtures — operator-managed vault for pytest/eval inputs.

Why this exists
---------------
The eval runner and pytest suite need real Supabase JWTs and tenant UUIDs
to exercise the auth-gated agent endpoints. Hard-coding these into CSVs
or eval scenarios churns whenever a JWT expires; passing them per
request is ergonomically painful. This screen lets the operator record
the values once, then exports a ``.env.test`` file that pytest reads via
the ``test_fixture`` fixture in ``ai_agent/tests/conftest.py``.

Storage shape
-------------
One row per (name, environment) in ``wajagents.test_fixtures`` (created in the
consolidated ``supabase/migrations/20260521150000_wajagents_schema.sql``).  The
admin portal (service role) is the only writer; superuser-only RLS gates the
table now that the wajagents schema is exposed over PostgREST.

Why a sidecar, not a row in env_vars_handlers
---------------------------------------------
``agent_env_vars`` is read by the agent runtime at request time
(``core.env_resolver``); leaking a JWT meant for *tests* into that
codepath would make it eligible to be returned to live callers. Test
fixtures live in a separate table so the runtime never touches them.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from fastapi import Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from admin.audit import record_change
from admin.auth import require_superuser
from admin.router import client_for, current_env, router, templates, _safe_list
# Env symmetry: the fixtures section reads through the switcher env's client
# (`fetch_test_fixtures(client_for(env), env)`), so create writes to
# `client_for(<form environment>)` and update/delete go through the
# switcher-scoped `admin_service_client()` — never the bare
# `get_service_client()`, which always targeted the LOCAL branch (identical
# behavior while cross-env vars are unset).
from core.auth import admin_service_client

logger = logging.getLogger(__name__)

# Fixture names become env-var suffixes: WAJ_TEST_<UPPER_NAME>. Restricting
# to lowercase identifiers keeps the export deterministic and rejects
# whitespace/punctuation that would silently break `source .env.test`.
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_ENVIRONMENTS = ("staging", "prod")
_KINDS = ("jwt", "uuid", "string", "json", "secret")
_HIDDEN_KINDS = {"jwt", "secret"}
_SECRET_PLACEHOLDER = "••••"


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_PATTERN.match(name):
        raise HTTPException(
            400,
            "name must match ^[a-z][a-z0-9_]{0,62}$ — lowercase letters, "
            "digits, underscore; starts with a letter; max 63 chars.",
        )
    return name


def _validate_kind(kind: str) -> str:
    kind = (kind or "string").strip()
    if kind not in _KINDS:
        raise HTTPException(400, f"kind must be one of {_KINDS}")
    return kind


def _validate_environment(environment: str) -> str:
    environment = (environment or "staging").strip()
    if environment not in _ENVIRONMENTS:
        raise HTTPException(400, f"environment must be one of {_ENVIRONMENTS}")
    return environment


def _mask(row: Dict[str, Any]) -> Dict[str, Any]:
    """Audit-safe copy with the value redacted for jwt/secret kinds."""
    if row.get("kind") not in _HIDDEN_KINDS:
        return row
    masked = dict(row)
    if masked.get("value"):
        masked["value"] = _SECRET_PLACEHOLDER
    return masked


def fetch_test_fixtures(
    client: Any, env: str, reveal: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Fetch test_fixtures rows for ``env`` with display-safe annotations.

    Used by the consolidated Tests screen (/admin/tests): the test-fixtures
    management UI now lives as a section inside tests.html rather than a
    standalone /admin/test-fixtures page. Returns the same rows the old list
    view computed — secret/jwt kinds masked via the same ``_mask`` placeholder
    unless ``?reveal=<fixture_id>`` matches that row, so an over-the-shoulder
    operator only ever exposes one value at a time.
    """
    rows = _safe_list(
        client.table("test_fixtures")
        .select("*")
        .eq("environment", env)
        .order("name")
        .execute()
    )
    reveal_id = (reveal or "").strip()
    for r in rows:
        r["_hidden"] = r.get("kind") in _HIDDEN_KINDS
        r["_revealed"] = (r.get("fixture_id") == reveal_id) or not r["_hidden"]
        if not r["_revealed"]:
            r["_display_value"] = _SECRET_PLACEHOLDER if r.get("value") else ""
        else:
            r["_display_value"] = r.get("value") or ""
    return rows


@router.get("/test-fixtures", response_class=HTMLResponse)
async def test_fixtures_list(
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Legacy deep-link. Test fixtures now live inside /admin/tests, so 302
    there (anchored at the section) and let old bookmarks survive. The
    ``?reveal=`` + ``?env=`` query params are preserved by the consolidated
    section's own links once the operator lands on the Tests screen."""
    return RedirectResponse(url="/admin/tests#fixtures", status_code=302)


@router.post("/test-fixtures")
async def test_fixture_create(
    request: Request,
    environment: str = Form("staging"),
    name: str = Form(...),
    kind: str = Form("string"),
    value: str = Form(...),
    description: str = Form(""),
    ctx: dict = Depends(require_superuser),
) -> Response:
    environment = _validate_environment(environment)
    name = _validate_name(name)
    kind = _validate_kind(kind)
    if value == "" or value is None:
        raise HTTPException(400, "value is required")
    payload: Dict[str, Any] = {
        "name": name,
        "environment": environment,
        "kind": kind,
        "value": value,
        "description": (description or "").strip() or None,
        "created_by": ctx["user_id"],
    }
    # The row names its own environment — write it into THAT env's branch,
    # where the Tests screen's env-scoped read will look for it.
    client = client_for(environment)
    try:
        inserted = client.table("test_fixtures").insert(payload).execute()
    except Exception as exc:
        # Likeliest failure mode: UNIQUE (name, environment) collision.
        # Surface that as a 400 so the operator sees a clear message
        # instead of a 500.
        logger.warning("test-fixtures create failed: %s", exc)
        raise HTTPException(400, f"insert failed (duplicate name?): {exc}")
    new_row = inserted.data[0] if inserted.data else {}
    record_change(
        entity_type="test_fixtures",
        entity_id=new_row.get("fixture_id", ""),
        action="create",
        actor_user_id=ctx["user_id"],
        after=_mask(new_row),
    )
    return RedirectResponse(url="/admin/tests#fixtures", status_code=302)


@router.post("/test-fixtures/{fixture_id}")
async def test_fixture_update(
    fixture_id: str,
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    form = await request.form()
    client = admin_service_client()
    before_rows = _safe_list(
        client.table("test_fixtures").select("*").eq("fixture_id", fixture_id).execute()
    )
    if not before_rows:
        raise HTTPException(404, "test fixture not found")
    before = before_rows[0]

    payload: Dict[str, Any] = {}
    if "name" in form:
        payload["name"] = _validate_name(form.get("name") or "")
    if "kind" in form:
        payload["kind"] = _validate_kind(form.get("kind") or "")
    if "environment" in form:
        payload["environment"] = _validate_environment(form.get("environment") or "")
    if "value" in form:
        v = form.get("value")
        if v == "" or v is None:
            raise HTTPException(400, "value is required")
        payload["value"] = v
    if "description" in form:
        desc = (form.get("description") or "").strip()
        payload["description"] = desc or None
    # Bump updated_at explicitly — Postgres only refreshes it on triggers
    # (we don't define one for this table), so push it from the writer.
    from datetime import datetime, timezone
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()

    try:
        client.table("test_fixtures").update(payload).eq("fixture_id", fixture_id).execute()
    except Exception as exc:
        logger.warning("test-fixtures update failed: %s", exc)
        raise HTTPException(400, f"update failed (duplicate name?): {exc}")

    record_change(
        entity_type="test_fixtures",
        entity_id=fixture_id,
        action="update",
        actor_user_id=ctx["user_id"],
        before=_mask(before),
        after=_mask({**before, **payload}),
    )
    return RedirectResponse(url="/admin/tests#fixtures", status_code=302)


@router.post("/test-fixtures/{fixture_id}/delete")
async def test_fixture_delete(
    fixture_id: str,
    ctx: dict = Depends(require_superuser),
) -> Response:
    client = admin_service_client()
    before_rows = _safe_list(
        client.table("test_fixtures").select("*").eq("fixture_id", fixture_id).execute()
    )
    if not before_rows:
        raise HTTPException(404, "test fixture not found")
    client.table("test_fixtures").delete().eq("fixture_id", fixture_id).execute()
    record_change(
        entity_type="test_fixtures",
        entity_id=fixture_id,
        action="delete",
        actor_user_id=ctx["user_id"],
        before=_mask(before_rows[0]),
    )
    return RedirectResponse(url="/admin/tests#fixtures", status_code=302)


@router.get("/test-fixtures/export")
async def test_fixtures_export(
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Download a `.env.test` file for the active environment.

    Format: one line per fixture, `WAJ_TEST_<UPPER_NAME>=<value>`. Values
    are emitted *raw* — no quoting, no escaping. JWTs, UUIDs, and base64
    strings don't need it; values with spaces or shell metacharacters
    should be stored as base64/JSON by the operator (the `kind=json`
    field is the canonical place for structured values).
    """
    client = client_for(env)
    rows = _safe_list(
        client.table("test_fixtures")
        .select("name, value")
        .eq("environment", env)
        .order("name")
        .execute()
    )
    # A bulk reveal of every stored secret must leave a trace — same posture
    # as the env-vars reveal audit. Names only, never values.
    record_change(
        entity_type="test_fixtures",
        entity_id=None,
        action="export",
        actor_user_id=ctx["user_id"],
        after={"environment": env, "count": len(rows),
               "names": sorted(r.get("name", "") for r in rows)},
    )
    body_lines: List[str] = [
        f"# WAJ test fixtures — env={env}",
        f"# Generated from /admin/test-fixtures. Source this file before pytest:",
        f"#   set -a && source .env.test && pytest",
        "",
    ]
    for r in rows:
        body_lines.append(f"WAJ_TEST_{r['name'].upper()}={r['value']}")
    body = "\n".join(body_lines) + "\n"
    return PlainTextResponse(
        body,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename=".env.test"',
            "Cache-Control": "no-store",
        },
    )
