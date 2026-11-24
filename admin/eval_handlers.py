"""
Admin portal — eval suite editor + per-scenario run detail.

These routes live in a sidecar module to keep `admin/router.py` lean. The
module attaches to the shared `router` instance via `from admin.router
import router`. The single line at the bottom of `router.py` that imports
this module is the only wiring needed.

Routes added:
  - POST /admin/evals/suites                     create a new suite (empty scenarios)
  - GET  /admin/evals/suites/{suite_id}          suite editor (scenarios, threshold, evaluator)
  - POST /admin/evals/suites/{suite_id}          save scenarios + suite settings
  - POST /admin/evals/suites/{suite_id}/import   bulk-append scenarios from a CSV upload
  - GET  /admin/evals/compare?a=...&b=...        side-by-side run-vs-run comparison

The existing GET /admin/evals/runs/{run_id} handler in router.py is left
untouched — we only rewrite its template (eval_run_detail.html) to render
the per-scenario detail it already passes in `run.results`.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from admin.audit import record_change
from admin.auth import require_superuser
from admin.eval_context import business_field_label, eval_context_fields, eval_context_keys
from admin.router import current_env, router, _safe_list
from core.auth import get_service_client, admin_service_client

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.filters["pretty_json"] = lambda v: json.dumps(v or {}, indent=2, default=str)

# This module owns its own Jinja2Templates instance (separate from
# admin.router's), so the template globals router.py installs on its env
# are NOT shared. Mirror `asset_version` so _base.html cache-busting matches.
try:  # pragma: no cover — defensive: keep eval routes importable if this moves
    from admin.router import _STATIC_VERSION as _ROUTER_STATIC_VERSION

    templates.env.globals.setdefault("asset_version", _ROUTER_STATIC_VERSION)
except Exception:  # pragma: no cover
    pass

# Same reason, but NOT optional: _base.html *calls* this one on every render,
# so a missing global raises UndefinedError instead of degrading to falsy.
# Imported from core.deployment (stdlib-only, no cycle) rather than from
# admin.router, which imports this module back.
from core.deployment import single_env_deployment as _single_env_deployment  # noqa: E402

templates.env.globals["single_env_deployment"] = _single_env_deployment
templates.env.globals["eval_context_fields"] = eval_context_fields
templates.env.globals["business_field_label"] = business_field_label

# Same reason again: _base.html calls the nav/brand helpers on every render.
from admin.nav import install_template_globals as _install_nav_globals  # noqa: E402

_install_nav_globals(templates)

from admin.testing_view import install_filters as _install_testing_filters  # noqa: E402

_install_testing_filters(templates)

# agent_kind enum values offered when creating a brand-new suite: the
# deployment's own agents (plugin `agent_catalog` hook). A build with no
# plugin offers none: a suite there is always made for a chosen agent.
from admin.agent_kinds import catalog_tuple as _catalog_tuple  # noqa: E402

_AGENT_KINDS_FOR_SUITES = _catalog_tuple("suite_kinds")
# The suite scoring paths: the engine's own plus each plugin's (see
# admin/services/eval_runners.py). One list for the portal and the admin MCP:
# when a runner kind was once added here, the MCP kept its own copy and went
# on refusing that kind's suites.
from admin.services.eval_runners import runner_kinds as _runner_kinds, runner_options as _runner_options  # noqa: E402

RUNNER_KINDS = _runner_kinds()


def _suite_agent_kinds() -> tuple:
    """agent_kind values a new eval suite may target. Collapses to the active
    scope's own kind (e.g. ``("tenant",)``) on any scoped build so the
    create dialog + server validation agree; the WAJ list otherwise. Mirrors
    ``eval_runs_handlers._eval_agent_kinds`` (the dropdown source)."""
    from core.deployment import active_scope

    if (client := active_scope()):
        return (client.agent_kind, *client.extra_kinds)
    return _AGENT_KINDS_FOR_SUITES


def _sync_agent_kind(client, profile_id: str) -> str:
    """Resolve `profile_id`'s agent_kind so a suite write can keep
    `agent_kind` as a derived mirror of the one profile it's bound to.

    A suite that names an agent_kind independent of its profile_id could
    silently point at kind X while actually running against a profile of
    kind Y — this is the single lookup every suite create/update funnels
    through so the two never drift. Returns "" when `profile_id` is empty
    or doesn't resolve to a live row (deleted/typo'd profile); callers
    reject the write in that case rather than persisting a mismatch.
    """
    if not profile_id:
        return ""
    rows = (
        client.table("agent_profiles")
        .select("profile_id,agent_kind")
        .eq("profile_id", profile_id)
        .limit(1)
        .execute()
        .data
    ) or []
    return (rows[0].get("agent_kind") or "") if rows else ""


@router.get("/evals/suites/{suite_id}", response_class=HTMLResponse)
async def eval_suite_edit(
    suite_id: str,
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    """Render the suite page: summary + Run this suite + run history, then the
    editable cases and settings."""
    if not _is_uuid(suite_id):  # a malformed id used to 500 inside postgrest
        raise HTTPException(status_code=404, detail="test suite not found")
    client = admin_service_client()
    suites = _safe_list(
        client.table("agent_eval_suites").select("*").eq("suite_id", suite_id).execute()
    )
    if not suites:
        raise HTTPException(status_code=404, detail="test suite not found")
    suite = suites[0]

    models = _safe_list(
        client.table("agent_models")
        .select("model_id,name,provider,is_active")
        .order("name")
        .execute()
    )
    profiles = _safe_list(
        client.table("agent_profiles")
        .select("profile_id,display_name,agent_kind")
        .order("display_name")
        .execute()
    )
    # Scope the picker to suite-eligible kinds — same collapse
    # `_suite_agent_kinds()` already applies to the create-dialog dropdown, so
    # a scoped build never offers another scope's profiles here.
    _allowed_kinds = _suite_agent_kinds()
    all_profiles = profiles
    # Keep the suite's CURRENT agent in the picker even when its kind is
    # outside the list (e.g. a client demo's suite on the full build) — otherwise the
    # select renders "none" and a plain Save silently unlinks the suite.
    profiles = [p for p in profiles
                if p.get("agent_kind") in _allowed_kinds or p.get("profile_id") == suite.get("profile_id")]
    profile_index = {p["profile_id"]: p for p in all_profiles if p.get("profile_id")}

    # Run history of this suite (newest first) + the verdict of the latest
    # run as configured — the suite page is no longer a dead end.
    from admin.eval_runs_handlers import _comparable, previous_run_map, profile_label
    suite_runs = _safe_list(
        client.table("agent_eval_runs")
        .select("run_id, suite_id, profile_id, status, score, created_at, started_at, finished_at, "
                "bakeoff_id, schedule_id, context")
        .eq("suite_id", suite_id).order("created_at", desc=True).limit(20).execute()
    )
    prev_by_run = previous_run_map(suite_runs)
    for r in suite_runs:
        prev = prev_by_run.get(r.get("run_id"))
        r["_delta"] = (float(r["score"]) - float(prev["score"])) if (prev and r.get("score") is not None) else None
        r["_comparable"] = _comparable(r)
        ctx_ = r.get("context") or {}
        if r.get("bakeoff_id"):
            r["_label"] = "Model comparison · " + (ctx_.get("variant_label") or "variant")
        elif r.get("schedule_id"):
            r["_label"] = "Scheduled"
        else:
            r["_label"] = ctx_.get("variant_label") or ""
    latest = next((r for r in suite_runs if r["_comparable"]), None)

    # `?imported=N` is set by the CSV-import redirect so we can render a
    # transient green chip at the top of the page. Coerce to int safely;
    # if anything weird arrives in the URL we just suppress the chip.
    imported_raw = request.query_params.get("imported")
    try:
        imported_count = int(imported_raw) if imported_raw else None
    except (TypeError, ValueError):
        imported_count = None

    from admin._business_picker import fetch_businesses_for_picker

    return templates.TemplateResponse(
        request,
        "eval_suite_edit.html",
        {
            "ctx": ctx,
            "active": "evals",
            "current_env": env,
            "suite": suite,
            "scenarios": suite.get("scenarios") or [],
            "models": models,
            "profiles": profiles,
            "imported_count": imported_count,
            "businesses": fetch_businesses_for_picker(client),
            "suite_runs": suite_runs,
            "latest_run": latest,
            "agent_name": profile_label(suite.get("profile_id"), profile_index) if suite.get("profile_id") else None,
            "agent_missing": bool(suite.get("profile_id")) and suite.get("profile_id") not in profile_index,
            "target": float(suite.get("pass_threshold") or 0.7),
            "runner_options": _runner_options(),
        },
    )


@router.post("/evals/suites/{suite_id}")
async def eval_suite_save(
    suite_id: str,
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Save settings + the rewritten scenarios array. Form-encoded.

    Field naming convention:
      name                              — suite display name (text)
      pass_threshold                    — float 0..1
      is_active                         — checkbox ("on" when checked)
      evaluator_model_id                — uuid or "" (treated as NULL)
      scenario_ids                      — repeating: existing scenario ids (in render order)
      scenario_category_{idx}           — text per row
      scenario_question_{idx}           — text per row
      scenario_expected_{idx}           — text per row
      scenario_extra_{idx}              — JSON blob preserving free-form keys
      scenario_delete_{idx}             — "1" when row should be removed
      scenario_name_{idx}               — the case's display name ('' = none)
      new_scenario_category/_question/_expected/_name/_id — single add-row form
      new_scenario_json                 — optional advanced JSON merged into
                                           the new scenario dict (lets the
                                           add-row form author a `caller`/
                                           `limits`/`checks`/`expected_proposals`
                                           voice scenario, which the plain
                                           fields can't express); explicit
                                           id/category/question/expected_answer
                                           fields win over same-named JSON keys
    """
    form = await request.form()

    client = admin_service_client()
    before = _safe_list(
        client.table("agent_eval_suites").select("*").eq("suite_id", suite_id).execute()
    )
    if not before:
        raise HTTPException(status_code=404, detail="suite not found")
    before_row = before[0]

    # ---- rebuild scenarios array from the form ---------------------------
    existing_ids = form.getlist("scenario_ids")
    scenarios: List[Dict[str, Any]] = []
    for idx, sc_id in enumerate(existing_ids):
        if form.get(f"scenario_delete_{idx}") == "1":
            continue
        extra_raw = form.get(f"scenario_extra_{idx}", "").strip()
        try:
            extra = json.loads(extra_raw) if extra_raw else {}
            if not isinstance(extra, dict):
                extra = {}
        except json.JSONDecodeError:
            extra = {}

        scenario = dict(extra)  # start from preserved free-form keys
        scenario["id"] = sc_id or _slugify(form.get(f"scenario_question_{idx}", ""))
        # The Name field: what the case is called on screen. Blank removes a
        # name (the question is shown); absent (an older form) keeps it.
        if f"scenario_name_{idx}" in form:
            sc_name = (form.get(f"scenario_name_{idx}") or "").strip()
            if sc_name:
                scenario["name"] = sc_name
            else:
                scenario.pop("name", None)
        scenario["category"] = form.get(f"scenario_category_{idx}", "").strip()
        scenario["question"] = form.get(f"scenario_question_{idx}", "").strip()
        scenario["expected_answer"] = form.get(f"scenario_expected_{idx}", "").strip()
        scenarios.append(scenario)

    new_q = (form.get("new_scenario_question") or "").strip()
    new_json_raw = (form.get("new_scenario_json") or "").strip()
    if new_q or new_json_raw:
        # Advanced JSON (optional): the plain id/category/question/expected
        # fields can't express a voice scenario's `caller`/`limits`/`checks`/
        # `expected_proposals` — a scenario is otherwise unauthorable in the
        # portal except by surviving as `scenario_extra_{idx}` once it
        # already exists. Parsed dict is the base so those keys pass
        # through untouched; the plain form fields (when non-empty) win on
        # the four keys they both cover.
        new_extra: Dict[str, Any] = {}
        if new_json_raw:
            try:
                parsed = json.loads(new_json_raw)
                if isinstance(parsed, dict):
                    new_extra = parsed
            except json.JSONDecodeError:
                pass
        new_scenario = dict(new_extra)
        # Name: the form's Name field, else a `name` key in the case JSON. It
        # is what the case is called on screen; the id stays a slug.
        new_name = (
            (form.get("new_scenario_name") or "").strip()
            or str(new_extra.get("name") or "").strip()
        )
        if new_name:
            new_scenario["name"] = new_name
        new_scenario["id"] = (
            (form.get("new_scenario_id") or "").strip()
            or str(new_extra.get("id") or "").strip()
            or _slugify(new_name or new_q)
        )
        new_scenario["category"] = (
            (form.get("new_scenario_category") or "").strip()
            or new_extra.get("category", "")
        )
        new_scenario["question"] = new_q or new_extra.get("question", "")
        new_scenario["expected_answer"] = (
            (form.get("new_scenario_expected") or "").strip()
            or new_extra.get("expected_answer", "")
        )
        scenarios.append(new_scenario)

    # ---- assemble update payload ----------------------------------------
    payload: Dict[str, Any] = {"scenarios": scenarios}

    name = (form.get("name") or "").strip()
    if name:
        payload["name"] = name

    threshold_raw = form.get("pass_threshold")
    if threshold_raw is not None and threshold_raw != "":
        try:
            parsed_threshold = float(threshold_raw)
        except ValueError:
            pass
        else:
            # Mirror the create-path guard (eval_suite_create): an out-of-range
            # threshold here would silently neuter the promotion eval-gate
            # (a negative value makes every run "pass"; >1 makes every run "fail").
            if not (0.0 <= parsed_threshold <= 1.0):
                raise HTTPException(400, "pass_threshold must be between 0 and 1")
            payload["pass_threshold"] = parsed_threshold

    payload["is_active"] = form.get("is_active") == "on"

    evaluator_id = (form.get("evaluator_model_id") or "").strip()
    payload["evaluator_model_id"] = evaluator_id or None

    rk = (form.get("runner_kind") or "chat").strip()
    if rk not in RUNNER_KINDS:
        raise HTTPException(400, f"runner_kind must be one of {list(RUNNER_KINDS)}")
    payload["runner_kind"] = rk

    # Default trigger context — set once on the suite, inherited by every
    # run, overridable per-run (router.py /evals/run merges form values
    # on top). Drop empty strings so the runtime context stays clean.
    default_ctx: Dict[str, Any] = {}
    for field_name, db_key in (
        ("default_business_id", "business_id"),
        ("default_branch_id", "branch_id"),
        ("default_customer_id", "customer_id"),
        *((f"default_{key}", key) for key in eval_context_keys()),
        ("default_locale", "locale"),
    ):
        value = (form.get(field_name) or "").strip()
        if value:
            default_ctx[db_key] = value
    extra_json = (form.get("default_context_json") or "").strip()
    # A `voice` key pasted into the Advanced JSON box must land TOP-LEVEL
    # (default_context.voice), never under default_context.extra.voice —
    # admin/evals.py's voice_ctx build only ever reads the top-level key,
    # so a nested one would be silently unreachable (Finding 3b).
    advanced_voice: Optional[Dict[str, Any]] = None
    if extra_json:
        try:
            extra = json.loads(extra_json)
            if isinstance(extra, dict):
                if "voice" in extra:
                    candidate = extra.pop("voice")
                    if isinstance(candidate, dict):
                        advanced_voice = candidate
                if extra:
                    # Stash the rest under `extra` so the per-field
                    # round-trip stays clean; the runtime merger flattens
                    # both.
                    default_ctx["extra"] = extra
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"default_context_json must be a JSON object: {exc}")

    if advanced_voice is not None:
        # Advanced JSON explicitly supplied `voice` — it wins over whatever
        # was already on the suite.
        default_ctx["voice"] = advanced_voice
    else:
        # This rebuild has no field for `voice` (it's editor-only via the
        # Advanced JSON box), so silently carrying nothing forward would
        # WIPE it on every save (Finding 3a) — e.g. a seeded
        # voice_live suite. Carry the existing row's top-level
        # `voice` block over untouched.
        existing_default_context = before_row.get("default_context") or {}
        if isinstance(existing_default_context, dict) and isinstance(
            existing_default_context.get("voice"), dict
        ):
            default_ctx["voice"] = existing_default_context["voice"]

    payload["default_context"] = default_ctx

    # Profile binding: only touch profile_id/agent_kind when the form
    # supplies the profile_id FIELD at all. Legacy/API callers that omit
    # it entirely (a bare programmatic PATCH) keep today's behavior — this
    # save path has never touched agent_kind on its own, so leaving both
    # keys out of the payload preserves that exactly.
    #
    # But the edit form's Profile <select> always submits profile_id,
    # including the "— none —" option, which submits "" (present but
    # empty) — that's a deliberate CLEAR, not "field absent". Distinguish
    # the two via a sentinel default on `.get()` (works for any Mapping-like
    # form object, not just Starlette's FormData — some tests stub a bare
    # get/getlist double without `__contains__`) rather than truthiness, or
    # choosing "— none —" would silently no-op and the stale binding would
    # persist forever. Clearing only nulls profile_id; agent_kind is left
    # as-is (the suite goes back to being manually-managed, same as one
    # that was never bound).
    _profile_id_absent = object()
    raw_profile_id_field = form.get("profile_id", _profile_id_absent)
    if raw_profile_id_field is not _profile_id_absent:
        raw_profile_id = (raw_profile_id_field or "").strip()
        if raw_profile_id:
            derived_kind = _sync_agent_kind(client, raw_profile_id)
            if not derived_kind:
                raise HTTPException(400, f"profile {raw_profile_id} not found")
            payload["profile_id"] = raw_profile_id
            payload["agent_kind"] = derived_kind
        else:
            payload["profile_id"] = None

    # Mark the row operator-touched. The set_updated_at BEFORE UPDATE
    # trigger (db-functions) bumps updated_at on every deployed stack —
    # there now() simply overwrites this value — but a bare local stack
    # whose db-functions never ran has no trigger, and
    # admin/seed_eval_suites.py's one-shot binding backfill gates on
    # updated_at != created_at to know an operator has edited (possibly
    # deliberately CLEARED) this suite. Set it explicitly so that signal
    # exists everywhere.
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()

    try:
        client.table("agent_eval_suites").update(payload).eq("suite_id", suite_id).execute()
    except Exception as exc:
        # If the column doesn't exist yet on this environment (migration not
        # applied), retry without it so the rest of the save still lands.
        if "evaluator_model_id" in str(exc):
            logger.warning("eval_suite_save: evaluator_model_id column missing, retrying without it")
            payload.pop("evaluator_model_id", None)
            client.table("agent_eval_suites").update(payload).eq("suite_id", suite_id).execute()
        else:
            raise

    record_change(
        entity_type="agent_eval_suites",
        entity_id=suite_id,
        action="update",
        actor_user_id=ctx.get("user_id"),
        before=before_row,
        after=payload,
    )

    return RedirectResponse(url=f"/admin/evals/suites/{suite_id}", status_code=302)


def _slugify(text: str) -> str:
    """Generate a stable-ish scenario id from a question. Mirrors the
    convention in seed_eval_suites.py — lowercase, underscore-separated,
    non-alphanumerics dropped."""
    out = []
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "_":
            out.append("_")
    slug = "".join(out).strip("_")
    return slug[:48] or "scenario"


# ---------------------------------------------------------------------------
# Track B2 — CSV import for scenarios
# ---------------------------------------------------------------------------

# Recognised CSV column names. Everything else is dropped silently — we don't
# want to surface obscure errors when operators paste exports from elsewhere
# that happen to include extra columns.
_CSV_KNOWN_COLUMNS = {
    "id",
    "name",
    "category",
    "question",
    "expected_answer",
    "expected_substrings",
    "business_id",
    "branch_id",
    "customer_id",
    "locale",
}  # plus the plugin's admin.eval_context keys

# Scalar columns that should be copied straight through after trimming.
_CSV_SCALAR_COLUMNS = (
    "id",
    "name",
    "category",
    "question",
    "expected_answer",
    "business_id",
    "branch_id",
    "customer_id",
    "locale",
)


def csv_scenario_columns() -> tuple:
    """The scalar CSV columns an imported scenario keeps: the engine's own and
    the ids a plugin's agents read (admin/eval_context.py)."""
    return (*_CSV_SCALAR_COLUMNS, *eval_context_keys())


def _slugify_question_first_words(question: str, max_words: int = 6) -> str:
    """Slug derived from the first N words of the question, lowercase, underscores.

    Differs from `_slugify` in that it preserves word boundaries before
    truncation — which matters when the operator wants the auto id to be
    recognisable from the question (e.g. "what_are_your_hours" rather than
    "what_are_your_hours_on_a_satur" from the global 48-char cap).
    """
    words = question.lower().split()[:max_words]
    cleaned: List[str] = []
    for word in words:
        scrubbed = "".join(ch if ch.isalnum() else "" for ch in word)
        if scrubbed:
            cleaned.append(scrubbed)
    slug = "_".join(cleaned)
    return slug or "scenario"


@router.post("/evals/suites/{suite_id}/import")
async def eval_suite_import_csv(
    suite_id: str,
    request: Request,
    csv_file: UploadFile = File(...),
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Append scenarios parsed from an uploaded CSV onto the suite.

    Format: a `csv.DictReader`-readable file. `question` is the only
    required column; everything else listed in `_CSV_KNOWN_COLUMNS` is
    optional. Blank cells are dropped from the row entirely so the
    resulting JSON object doesn't carry a forest of empty strings.

    TODO: consider deduping by `id` against existing scenarios in a
    follow-up — for now operators iterating on a CSV may re-upload the
    same file multiple times and we intentionally accept that.
    """
    client = admin_service_client()
    before = _safe_list(
        client.table("agent_eval_suites").select("*").eq("suite_id", suite_id).execute()
    )
    if not before:
        raise HTTPException(status_code=404, detail="suite not found")
    before_row = before[0]

    # Read the upload into memory — eval suites are tiny (single-digit MB
    # at the extreme), so streaming would be premature optimisation.
    raw = await csv_file.read()
    try:
        text = raw.decode("utf-8-sig")  # tolerate Excel BOM
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400, detail=f"CSV parse error: file is not UTF-8 ({exc})"
        )

    rows: List[Dict[str, Any]] = []
    try:
        reader = csv.DictReader(io.StringIO(text))
        # DictReader rows are numbered from 2 if we count the header as
        # row 1, which matches what an operator sees in Excel.
        for row_num, raw_row in enumerate(reader, start=2):
            if raw_row is None:
                continue

            question = (raw_row.get("question") or "").strip()
            if not question:
                # Skip rows with no question — these are usually blank
                # trailing rows or operator scratch comments.
                continue

            scenario: Dict[str, Any] = {}
            for col in csv_scenario_columns():
                value = (raw_row.get(col) or "").strip()
                if value:
                    scenario[col] = value

            substrings_raw = (raw_row.get("expected_substrings") or "").strip()
            if substrings_raw:
                substrings = [
                    frag.strip()
                    for frag in substrings_raw.split(";")
                    if frag and frag.strip()
                ]
                if substrings:
                    scenario["expected_substrings"] = substrings

            if "id" not in scenario:
                scenario["id"] = _slugify_question_first_words(scenario.get("name") or question)

            rows.append(scenario)
    except csv.Error as exc:
        # csv.Error doesn't always carry a line number; fall back to a
        # generic message but still surface the underlying string.
        raise HTTPException(status_code=400, detail=f"CSV parse error: {exc}")
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover — defensive
        raise HTTPException(
            status_code=400, detail=f"CSV parse error at row {row_num}: {exc}"
        )

    existing_scenarios = list(before_row.get("scenarios") or [])
    combined = existing_scenarios + rows

    client.table("agent_eval_suites").update({"scenarios": combined}).eq(
        "suite_id", suite_id
    ).execute()

    record_change(
        entity_type="agent_eval_suites",
        entity_id=suite_id,
        action="import_scenarios",
        actor_user_id=ctx.get("user_id"),
        after={"count": len(rows)},
    )

    return RedirectResponse(
        url=f"/admin/evals/suites/{suite_id}?imported={len(rows)}", status_code=302
    )


# ---------------------------------------------------------------------------
# Create a new eval suite — POST /admin/evals/suites
#
# `agent_eval_suites` rows used to be seed-only. This handler backs the
# "Create suite" dialog on /admin/evals: it inserts an empty-scenario
# suite, audit-logs it, and redirects to the new suite's editor where the
# operator adds scenarios (CSV import or manual).
# ---------------------------------------------------------------------------


@router.post("/evals/suites")
async def eval_suite_create(
    request: Request,
    ctx: dict = Depends(require_superuser),
    name: str = Form(""),
    agent_kind: str = Form(""),
    profile_id: str = Form(""),
    pass_threshold: str = Form("0.7"),
    evaluator_model_id: str = Form(""),
    is_active: str = Form(""),
    runner_kind: str = Form("chat"),
) -> Response:
    """Insert a brand-new eval suite with an empty scenarios array.

    Validation:
      * `name` must be non-empty.
      * `profile_id`, when supplied, must resolve to a live profile —
        `agent_kind` is then DERIVED from that profile via
        `_sync_agent_kind` (overriding whatever the form's agent_kind
        select carried; the create dialog disables that select once a
        profile is picked, but server-side we don't trust the client to
        have kept them in sync).
      * Otherwise (no `profile_id` — legacy/API callers) `agent_kind` must
        be one of `_suite_agent_kinds()` (client-aware), same as before.
      * `pass_threshold` must parse to a float in [0, 1] (defaults 0.7).
      * `runner_kind` must be one of RUNNER_KINDS (defaults chat).
    On success, audit-log via record_change and redirect to the new
    suite's editor at /admin/evals/suites/{new_id}.
    """
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "name is required")

    db = admin_service_client()

    profile_id = (profile_id or "").strip()
    agent_kind = (agent_kind or "").strip()
    if profile_id:
        derived_kind = _sync_agent_kind(db, profile_id)
        if not derived_kind:
            raise HTTPException(400, f"profile {profile_id} not found")
        agent_kind = derived_kind
    else:
        _suite_kinds = _suite_agent_kinds()
        if agent_kind not in _suite_kinds:
            raise HTTPException(
                400, f"agent_kind must be one of {list(_suite_kinds)}"
            )

    threshold_raw = (pass_threshold or "").strip()
    try:
        threshold = float(threshold_raw) if threshold_raw else 0.7
    except (TypeError, ValueError):
        raise HTTPException(400, "pass_threshold must be a number between 0 and 1")
    if not (0.0 <= threshold <= 1.0):
        raise HTTPException(400, "pass_threshold must be between 0 and 1")

    rk = (runner_kind or "chat").strip()
    if rk not in RUNNER_KINDS:
        raise HTTPException(400, f"runner_kind must be one of {list(RUNNER_KINDS)}")

    insert_payload: Dict[str, Any] = {
        "name": name,
        "agent_kind": agent_kind,
        "scenarios": [],
        "pass_threshold": threshold,
        "is_active": is_active == "on",
        "runner_kind": rk,
    }
    if profile_id:
        insert_payload["profile_id"] = profile_id
    evaluator_id = (evaluator_model_id or "").strip()
    if evaluator_id:
        insert_payload["evaluator_model_id"] = evaluator_id

    try:
        insert = db.table("agent_eval_suites").insert(insert_payload).execute()
    except Exception as exc:
        # Tolerate environments where the evaluator_model_id column hasn't
        # been migrated yet — retry without it so suite creation still works.
        if "evaluator_model_id" in str(exc):
            logger.warning(
                "eval_suite_create: evaluator_model_id column missing, retrying without it"
            )
            insert_payload.pop("evaluator_model_id", None)
            insert = db.table("agent_eval_suites").insert(insert_payload).execute()
        else:
            raise

    rows = getattr(insert, "data", None) or []
    new_suite_id = rows[0]["suite_id"] if rows else None

    record_change(
        entity_type="agent_eval_suites",
        entity_id=new_suite_id or name,
        action="create",
        actor_user_id=ctx.get("user_id"),
        after=insert_payload,
    )

    if new_suite_id:
        return RedirectResponse(
            url=f"/admin/evals/suites/{new_suite_id}", status_code=302
        )
    # Insert returned no row (e.g. RLS hid the result) — fall back to the list.
    return RedirectResponse(url="/admin/evals/suites", status_code=302)


# ---------------------------------------------------------------------------
# Track B4 — Run-compare view
# ---------------------------------------------------------------------------


def _scenario_id_for_result(result: Dict[str, Any], fallback_index: int) -> str:
    """Pick a stable join key for a scenario result.

    Mirrors the lookup logic in eval_run_detail.html so the comparison
    view groups rows the same way the operator sees them in the detail
    page. Falls back to a positional id when the run was recorded
    without per-scenario ids (legacy rows).
    """
    return (
        result.get("scenario_id")
        or result.get("id")
        or result.get("name")
        or f"scenario_{fallback_index}"
    )


def _passed(result: Optional[Dict[str, Any]]) -> Optional[bool]:
    if result is None:
        return None
    return bool(result.get("passed"))


def _score(result: Optional[Dict[str, Any]]) -> Optional[float]:
    if result is None:
        return None
    value = result.get("score")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _latency(result: Optional[Dict[str, Any]]) -> Optional[int]:
    if result is None:
        return None
    value = result.get("elapsed_ms")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _result_chip(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> str:
    """Classify the A→B transition into one of six labels."""
    if a is None and b is not None:
        return "only_b"
    if b is None and a is not None:
        return "only_a"
    a_pass = _passed(a)
    b_pass = _passed(b)
    if a_pass and b_pass:
        return "held_pass"
    if not a_pass and not b_pass:
        return "held_fail"
    if not a_pass and b_pass:
        return "gained"
    return "regressed"


def _compare_runs(
    run_a: Dict[str, Any], run_b: Dict[str, Any]
) -> Dict[str, Any]:
    """Pure data-shaping for the compare view.

    Joins two runs' `results` arrays by scenario_id, classifies each pair
    into one of six labels (`held_pass`, `held_fail`, `gained`,
    `regressed`, `only_a`, `only_b`), and rolls up summary deltas. Pulled
    out of the route handler so it's easy to test without booting the app.
    """
    results_a = run_a.get("results") or []
    results_b = run_b.get("results") or []

    a_by_sid: Dict[str, Dict[str, Any]] = {}
    for idx, res in enumerate(results_a):
        a_by_sid[_scenario_id_for_result(res, idx)] = res
    b_by_sid: Dict[str, Dict[str, Any]] = {}
    for idx, res in enumerate(results_b):
        b_by_sid[_scenario_id_for_result(res, idx)] = res

    all_ids = list(a_by_sid.keys())
    for sid in b_by_sid:
        if sid not in a_by_sid:
            all_ids.append(sid)

    joined: List[Dict[str, Any]] = []
    for sid in all_ids:
        ra = a_by_sid.get(sid)
        rb = b_by_sid.get(sid)
        score_a = _score(ra)
        score_b = _score(rb)
        latency_a = _latency(ra)
        latency_b = _latency(rb)

        delta_score: Optional[float] = None
        if score_a is not None and score_b is not None:
            delta_score = score_b - score_a

        delta_latency: Optional[int] = None
        if latency_a is not None and latency_b is not None:
            delta_latency = latency_b - latency_a

        joined.append(
            {
                "scenario_id": sid,
                "a": ra,
                "b": rb,
                "score_a": score_a,
                "score_b": score_b,
                "latency_a": latency_a,
                "latency_b": latency_b,
                "delta_score": delta_score,
                "delta_latency_ms": delta_latency,
                "result": _result_chip(ra, rb),
            }
        )

    # Largest absolute score divergences float to the top so the operator
    # immediately sees what moved. Missing scores sort last.
    joined.sort(
        key=lambda j: abs(j["delta_score"]) if j["delta_score"] is not None else -1.0,
        reverse=True,
    )

    def _passes(results: List[Dict[str, Any]]) -> int:
        return sum(1 for r in results if r.get("passed"))

    def _mean_latency(results: List[Dict[str, Any]]) -> Optional[float]:
        latencies = [_latency(r) for r in results]
        latencies = [v for v in latencies if v is not None]
        if not latencies:
            return None
        return sum(latencies) / len(latencies)

    passes_a = _passes(results_a)
    passes_b = _passes(results_b)
    mean_lat_a = _mean_latency(results_a)
    mean_lat_b = _mean_latency(results_b)
    score_a_total = run_a.get("score")
    score_b_total = run_b.get("score")
    delta_score_total: Optional[float] = None
    if score_a_total is not None and score_b_total is not None:
        try:
            delta_score_total = float(score_b_total) - float(score_a_total)
        except (TypeError, ValueError):
            delta_score_total = None
    delta_mean_latency: Optional[float] = None
    if mean_lat_a is not None and mean_lat_b is not None:
        delta_mean_latency = mean_lat_b - mean_lat_a

    summary = {
        "delta_passes": passes_b - passes_a,
        "delta_score": delta_score_total,
        "delta_mean_latency_ms": delta_mean_latency,
        "passes_a": passes_a,
        "passes_b": passes_b,
        "mean_latency_a": mean_lat_a,
        "mean_latency_b": mean_lat_b,
    }
    return {"joined": joined, "summary": summary}


# ---------------------------------------------------------------------------
# Track C1 — N-run comparison aggregation
# ---------------------------------------------------------------------------


def _run_prompt_version_label(run: Dict[str, Any]) -> str:
    # NOTE: Intentional local copy — importing from eval_runs_handlers would
    # create fragile load-order coupling (both import admin.router at bottom).
    ctx = run.get("context") or {}
    pv = ctx.get("prompt_version") or ctx.get("prompt_version_label")
    if pv:
        return str(pv)
    if ctx.get("prompt_override"):
        return "override"
    return "baseline"


def _variant_label_for_run(run: Dict[str, Any]) -> str:
    ctx = run.get("context") or {}
    return (ctx.get("variant_label") or ctx.get("model_label")
            or _run_prompt_version_label(run))


def _percentile(values: List[float], pct: float) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    k = (len(vals) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def _best_index(values: List[Optional[float]], lower_is_better: bool) -> Optional[int]:
    present = [(i, v) for i, v in enumerate(values) if v is not None]
    if not present:
        return None
    return (min if lower_is_better else max)(present, key=lambda t: t[1])[0]


def run_detail_kpis(results: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """Aggregate a run's per-scenario `results` rows into run-level KPIs for the
    run detail header: latency (p50), TTFT/TTLT (p50), total tokens + cost, and
    average iterations / tool calls. Returns None when there are no results."""
    rows = [r for r in (results or []) if isinstance(r, dict)]
    if not rows:
        return None

    def nums(key: str) -> List[float]:
        return [r[key] for r in rows
                if isinstance(r.get(key), (int, float)) and r.get(key) is not None]

    def p50(vals: List[float]) -> Optional[int]:
        v = _percentile(vals, 0.50)
        return round(v) if v is not None else None

    def avg(vals: List[float]) -> Optional[float]:
        return round(sum(vals) / len(vals), 1) if vals else None

    in_tok, out_tok = sum(nums("input_tokens")), sum(nums("output_tokens"))
    # Consistency: group attempts by scenario so pass^k (every attempt of a
    # scenario passed) reads distinctly from pass@1 (attempt-level pass rate).
    # With one attempt per scenario the two coincide — exactly as expected.
    by_scn: Dict[Any, List[bool]] = {}
    for r in rows:
        by_scn.setdefault(_scenario_key(r), []).append(bool(r.get("passed")))
    distinct = len(by_scn)
    passhat_k = (sum(1 for ps in by_scn.values() if ps and all(ps)) / distinct
                 if distinct else None)
    pass_at_1 = (sum(1 for r in rows if r.get("passed")) / len(rows)) if rows else None
    # pass@k (at least one of the k attempts passed) — the counterpart of
    # pass^k; k = the run's attempts per scenario. Per category too, so a
    # suite that groups scenarios (emoji / stickers / language) reads per group.
    from admin.services.eval_metrics import pass_k_summary
    k_summary = pass_k_summary(rows)
    return {
        "scenarios": distinct,
        "distinct_scenarios": distinct,
        "total_attempts": len(rows),
        "repeated": any(len(ps) > 1 for ps in by_scn.values()),
        "passhat_k": round(passhat_k, 3) if passhat_k is not None else None,
        "pass_at_1": round(pass_at_1, 3) if pass_at_1 is not None else None,
        "k": k_summary["k"],
        "pass_at_k": k_summary["pass_at_k"],
        "by_category": k_summary["by_category"],
        "latency_p50_ms": p50(nums("total_ms") or nums("latency_ms")),
        "ttft_p50_ms": p50(nums("ttft_ms")),
        "ttlt_p50_ms": p50(nums("total_ms")),
        # Unknown is None ('—' on screen), never a confident 0: runners that
        # don't capture cost/tokens (voice, some plugin runners) leave them null/0.
        "cost_usd": round(sum(nums("cost_usd")), 4) if nums("cost_usd") else None,
        "tokens_in": in_tok if (in_tok or out_tok) else None,
        "tokens_out": out_tok if (in_tok or out_tok) else None,
        "tokens_total": (in_tok + out_tok) if (in_tok or out_tok) else None,
        "avg_iterations": avg(nums("iterations")),
        "avg_tool_count": avg(nums("tool_count")),
        # Tool calls that came back error-shaped (admin/evals.py
        # `tool_errors`). None for rows from before it was recorded, so an
        # old run reads '—', not a confident 0.
        "tool_errors": (sum(int(r.get("tool_error_count") or 0) for r in rows)
                        if any("tool_error_count" in r for r in rows) else None),
        "cases_with_tool_errors": (len({_scenario_key(r) for r in rows
                                        if r.get("tool_error_count")})
                                   if any("tool_error_count" in r for r in rows) else None),
    }


def _scenario_key(r: Dict[str, Any]) -> Any:
    """Stable identity for a scenario across its repeated attempts."""
    return r.get("scenario_id") or r.get("name") or r.get("question") or id(r)


def _judge_score(r: Dict[str, Any]) -> Optional[float]:
    """Pull the judge's 0..1 score from a result row, however it's shaped."""
    j = r.get("judge")
    if isinstance(j, dict):
        j = j.get("score")
    if isinstance(j, (int, float)):
        return float(j)
    return None


def run_chart_data(results: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Minimal, strictly-scalar projection of results for the run-detail charts.

    The charts only need pass/fail, score, latency and the judge score — NOT
    the full `actual`/`reason` markdown. Dumping the whole result set embedded
    raw newlines that broke `JSON.parse` in the browser and left every canvas
    blank; this projection is JSON-safe and an order of magnitude smaller.
    """
    out: List[Dict[str, Any]] = []
    for r in (results or []):
        if not isinstance(r, dict):
            continue
        lat = r.get("latency_ms")
        if lat is None:
            lat = r.get("elapsed_ms") if isinstance(r.get("elapsed_ms"), (int, float)) else (
                r.get("total_ms") if isinstance(r.get("total_ms"), (int, float)) else None)
        out.append({
            "label": str(_scenario_key(r))[:24],
            "passed": bool(r.get("passed")),
            "score": r.get("score") if isinstance(r.get("score"), (int, float)) else None,
            "latency_ms": lat,
            "judge_score": _judge_score(r),
        })
    return out


def case_display_name(case: Optional[Dict[str, Any]]) -> str:
    """What a case is called on screen: its `name` (the suite page's Name
    field, a `name` key in the case JSON or an MCP scenario), else its
    question, else its id made readable.

    A result row carries `name` = the scenario's name or, when it has none,
    its id; a name equal to the id is not a name someone gave it."""
    from admin.testing_view import humanize

    if not isinstance(case, dict):
        return ""
    sid = str(case.get("scenario_id") or case.get("id") or "")
    name = str(case.get("name") or "").strip()
    if name and name != sid:
        return name
    question = " ".join(str(case.get("question") or case.get("input") or "").split())
    if question:
        return question if len(question) <= 90 else question[:89].rstrip() + "…"
    return humanize(sid) if sid else ""


def group_results_by_scenario(results: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Collapse per-attempt result rows into one group per scenario.

    Lets the run-detail page show all N attempts of a question together with a
    per-scenario consistency verdict (passed every attempt?) instead of N loose
    cards. Preserves first-seen order; attempts keep their original order.

    Unlike `run_chart_data()` (a deliberately minimal scalar projection for
    the charts), each attempt here is the FULL raw result row, unreshaped —
    so a `voice_live` scenario's `scorer_detail` (transcript, per-turn
    timing, limit_breaches, checks, proposals, …; see
    admin/voice_eval_runner.py) rides through untouched for the template to
    render, same as `reason`/`actual`/`analysis` already do. A chat-run
    attempt simply has no `scorer_detail` key.
    """
    groups: Dict[Any, Dict[str, Any]] = {}
    for r in (results or []):
        if not isinstance(r, dict):
            continue
        key = _scenario_key(r)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "scenario_id": r.get("scenario_id") or r.get("name") or str(key)[:48],
                "display_name": case_display_name(r),
                "question": r.get("question") or r.get("input"),
                "category": r.get("category"),
                "attempts": [],
            }
        g["attempts"].append(r)
    out: List[Dict[str, Any]] = []
    for g in groups.values():
        att = g["attempts"]
        passes = sum(1 for a in att if a.get("passed"))
        scores = [a["score"] for a in att if isinstance(a.get("score"), (int, float))]
        g["n"] = len(att)
        g["passes"] = passes
        g["consistent"] = bool(att) and passes == len(att)
        g["mean_score"] = round(sum(scores) / len(scores), 3) if scores else None
        out.append(g)
    return out


def build_case_rows(
    results: Optional[List[Dict[str, Any]]],
    prev_results: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Run-detail cases, verdict-first: one row per case (repeats grouped),
    failed cases first, each with a one-line plain reason and — when a
    previous run of the same suite exists — whether it broke or got fixed
    since then.

    outcome: 'failed' (no attempt passed), 'flaky' (some did), 'passed', or
    'ungraded' (no attempt could be judged: no judge and nothing
    deterministic to check).
    """
    from admin.testing_view import case_reason

    groups = group_results_by_scenario(results)
    prev_pass: Dict[Any, bool] = {}
    for g in group_results_by_scenario(prev_results):
        prev_pass[g["scenario_id"]] = bool(g["consistent"])
    rows: List[Dict[str, Any]] = []
    for g in groups:
        att = g["attempts"]
        if g["consistent"]:
            outcome = "passed"
        elif g["passes"] == 0 and att and all(a.get("ungraded") for a in att):
            # Could not be judged at all: not a pass, not a failure.
            outcome = "ungraded"
        elif g["passes"] == 0:
            outcome = "failed"
        else:
            outcome = "flaky"
        failing = next((a for a in att if not a.get("passed")), None)
        lat = [a.get("elapsed_ms") or a.get("total_ms") or a.get("latency_ms") for a in att]
        lat = [v for v in lat if isinstance(v, (int, float))]
        was = prev_pass.get(g["scenario_id"]) if prev_results is not None else None
        rows.append({
            **g,
            "outcome": outcome,
            "reason": case_reason(failing) if failing else "",
            "latency_ms": round(sum(lat) / len(lat)) if lat else None,
            "broke": was is True and outcome != "passed",
            "fixed": was is False and outcome == "passed",
            "was": was,
        })
    order = {"failed": 0, "flaky": 1, "ungraded": 2, "passed": 3}
    rows.sort(key=lambda r: (order[r["outcome"]], not r["broke"]))
    return rows


def _suite_metric_row(metric: str, values: List[Optional[float]], lower_is_better: bool = False) -> Dict[str, Any]:
    return {"metric": metric, "values": values,
            "best_index": _best_index(values, lower_is_better),
            "lower_is_better": lower_is_better}


def _compare_runs_n(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build an N-column comparison matrix from selected eval runs.

    Quality metrics (pass_rate, mean_judge_score) are grouped per suite so the
    verticals stay separate; performance (ttft/total percentiles) and cost
    (cost_total, tokens_total) are pooled per variant. A variant that didn't
    run a given suite shows None for that cell.
    """
    def _model_label(r: Dict[str, Any]) -> str:
        ctx = r.get("context") or {}
        mo = ctx.get("model_override") or {}
        return (mo.get("model_string") or ctx.get("model_label") or "—")

    columns = [{"run_id": r.get("run_id"), "label": _variant_label_for_run(r),
                "suite_id": r.get("suite_id"), "profile_id": r.get("profile_id"),
                "model": _model_label(r),
                "prompt_version": _run_prompt_version_label(r)}
               for r in runs]

    # --- per-suite quality rows ---
    suite_ids: List[str] = []
    for r in runs:
        sid = r.get("suite_id")
        if sid and sid not in suite_ids:
            suite_ids.append(sid)

    suites = []
    for sid in suite_ids:
        pass_rates: List[Optional[float]] = []
        judge_means: List[Optional[float]] = []
        pass_at_1_vals: List[Optional[float]] = []
        passhat_k_vals: List[Optional[float]] = []
        pass_at_k_vals: List[Optional[float]] = []
        mean_score_vals: List[Optional[float]] = []
        for r in runs:
            if r.get("suite_id") != sid:
                pass_rates.append(None)
                judge_means.append(None)
                pass_at_1_vals.append(None)
                passhat_k_vals.append(None)
                pass_at_k_vals.append(None)
                mean_score_vals.append(None)
                continue
            results = r.get("results") or []
            if results:
                n = len(results)
                n_passed = sum(1 for x in results if x.get("passed"))
                pass_rates.append(n_passed / n)
                js = [x["judge"]["score"] for x in results
                      if x.get("judge") and "score" in x["judge"]]
                judge_means.append(sum(js) / len(js) if js else None)
                # pass@1: fraction of scenarios that passed (same as pass_rate
                # but named explicitly for the comparison matrix)
                pass_at_1_vals.append(n_passed / n)
                # pass^k (passhat_k): consistency across REPEATED attempts of the
                # same scenario. Group results by scenario; for a scenario with c
                # passes out of n attempts, the unbiased estimate that "all k
                # sampled attempts pass" is C(c,k)/C(n,k). We use k=n (the attempt
                # count) → 1.0 iff every attempt of that scenario passed. Averaged
                # across scenarios = the fraction the model passes RELIABLY. With a
                # single attempt per scenario (n=1) this equals pass_rate (no
                # consistency signal exists from one sample) — instead of the old
                # all-or-nothing that read 0 the moment any one scenario failed.
                from collections import defaultdict as _dd
                from math import comb as _comb
                _scen = _dd(lambda: [0, 0])  # scenario key -> [passes c, attempts n]
                for x in results:
                    key = x.get("scenario_id") or x.get("name") or x.get("question")
                    _scen[key][1] += 1
                    if x.get("passed"):
                        _scen[key][0] += 1
                _ph = [
                    _comb(c, n_att) / _comb(n_att, n_att)  # k=n_att → 1.0 iff c==n_att
                    for c, n_att in _scen.values() if n_att >= 1
                ]
                passhat_k_vals.append(sum(_ph) / len(_ph) if _ph else None)
                # pass@k: at least one of the k attempts passed (the capability
                # counterpart of pass^k) — see admin/services/eval_metrics.py.
                from admin.services.eval_metrics import pass_k_summary
                pass_at_k_vals.append(pass_k_summary(results)["pass_at_k"])
                # mean_score: mean of per-scenario score fields
                scores = [x["score"] for x in results if x.get("score") is not None]
                mean_score_vals.append(sum(scores) / len(scores) if scores else None)
            else:
                pass_rates.append(None)
                judge_means.append(None)
                pass_at_1_vals.append(None)
                passhat_k_vals.append(None)
                pass_at_k_vals.append(None)
                mean_score_vals.append(None)
        suites.append({"suite_id": sid, "rows": [
            _suite_metric_row("pass_rate", pass_rates),
            _suite_metric_row("mean_judge_score", judge_means),
            _suite_metric_row("pass_at_1", pass_at_1_vals),
            _suite_metric_row("pass_at_k", pass_at_k_vals),
            _suite_metric_row("passhat_k", passhat_k_vals),
            _suite_metric_row("mean_score", mean_score_vals),
        ]})

    # --- global per-variant performance + cost ---
    def _col_vals(key: str) -> List[List[Optional[float]]]:
        out = []
        for r in runs:
            out.append([x.get(key) for x in (r.get("results") or [])])
        return out

    ttft = _col_vals("ttft_ms")
    total = _col_vals("total_ms")
    costs = _col_vals("cost_usd")
    iters = _col_vals("iterations")
    tool_counts = _col_vals("tool_count")
    toks = []
    for r in runs:
        toks.append(sum((x.get("input_tokens") or 0) + (x.get("output_tokens") or 0)
                        for x in (r.get("results") or [])))

    def _mean_col(col: List[Optional[float]]) -> Optional[float]:
        vals = [v for v in col if v is not None]
        return sum(vals) / len(vals) if vals else None

    global_rows = [
        _suite_metric_row("ttft_p50", [_percentile([v for v in c if v is not None], 0.5) for c in ttft], lower_is_better=True),
        _suite_metric_row("ttft_p95", [_percentile([v for v in c if v is not None], 0.95) for c in ttft], lower_is_better=True),
        _suite_metric_row("total_p50", [_percentile([v for v in c if v is not None], 0.5) for c in total], lower_is_better=True),
        _suite_metric_row("total_p95", [_percentile([v for v in c if v is not None], 0.95) for c in total], lower_is_better=True),
        _suite_metric_row("cost_total", [round(sum(v for v in c if v is not None), 6) if any(v is not None for v in c) else None for c in costs], lower_is_better=True),
        _suite_metric_row("tokens_total", [t if t else None for t in toks], lower_is_better=True),
        _suite_metric_row("iterations", [_mean_col(c) for c in iters], lower_is_better=True),
        _suite_metric_row("tool_count", [_mean_col(c) for c in tool_counts], lower_is_better=True),
    ]
    return {"columns": columns, "suites": suites, "global_rows": global_rows}


def _is_uuid(value: Any) -> bool:
    import uuid as _uuid
    try:
        _uuid.UUID(str(value))
        return True
    except (ValueError, TypeError):
        return False


def _compare_run_ids(runs: Optional[str], a: Optional[str], b: Optional[str]) -> List[str]:
    """Run ids for the compare view: `runs=id1,id2,...` (preferred) or the
    legacy `a`/`b` pair. Order preserved, blanks dropped, de-duped."""
    ids: List[str] = []
    if runs:
        ids = [x.strip() for x in runs.split(",") if x.strip()]
    elif a or b:
        ids = [x for x in (a, b) if x]
    seen, out = set(), []
    for rid in ids:
        if rid not in seen:
            seen.add(rid)
            out.append(rid)
    return out


@router.get("/evals/compare", response_class=HTMLResponse)
async def evals_compare(
    request: Request,
    runs: str = "",
    a: str = "",
    b: str = "",
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    """Side-by-side comparison of N eval runs (select-existing, computed live).

    Leads with the answer to "did my change help?": the first run is the
    baseline, the last one the change — a verdict sentence, then the cases
    that got Fixed / Broke / are Still failing, then the metric matrix."""
    from admin._query_batch import run_parallel
    from admin.eval_runs_handlers import profile_label
    from admin.testing_view import case_reason, threshold_of

    run_ids = _compare_run_ids(runs or None, a or None, b or None)
    if len(run_ids) < 2:
        raise HTTPException(400, "Pick at least two runs to compare")
    if not all(_is_uuid(r) for r in run_ids):
        raise HTTPException(404, "One or more of these runs doesn't exist")
    client = admin_service_client()
    rows = _safe_list(
        client.table("agent_eval_runs").select("*").in_("run_id", run_ids).execute()
    )
    by_id = {r["run_id"]: {**r, "kind": "text"} for r in rows if r.get("run_id")}
    missing = [rid for rid in run_ids if rid not in by_id]
    if missing:
        # Voice checks share the Runs list; their runs live in
        # ai_voice_scenario_runs (admin/voice_checks.py maps them).
        from admin.voice_checks import load_voice_runs_by_ids

        by_id.update({r["run_id"]: r for r in load_voice_runs_by_ids(client, missing)})
    ordered = [by_id[rid] for rid in run_ids if rid in by_id]
    if len(ordered) < 2:
        raise HTTPException(404, "One or more of these runs doesn't exist")
    if len({r.get("kind") for r in ordered}) > 1:
        raise HTTPException(
            400, "A voice check can only be compared with other voice-check runs — "
                 "a call's steps and a test suite's cases have nothing to line up. "
                 "Pick runs of one type.")
    matrix = _compare_runs_n(ordered)

    from admin.voice_checks import VOICE_CHECK_TARGET, is_voice_suite_id

    suite_ids = sorted({r.get("suite_id") for r in ordered
                        if r.get("suite_id") and not is_voice_suite_id(r.get("suite_id"))})
    profile_ids = sorted({r.get("profile_id") for r in ordered if r.get("profile_id")})
    looked_up = run_parallel({
        "suites": lambda: _safe_list(
            client.table("agent_eval_suites").select("suite_id, name, pass_threshold")
            .in_("suite_id", suite_ids).execute()) if suite_ids else [],
        "profiles": lambda: _safe_list(
            client.table("agent_profiles").select("profile_id, display_name, slug")
            .in_("profile_id", profile_ids).execute()) if profile_ids else [],
    })
    suite_by_id = {x["suite_id"]: x for x in looked_up["suites"] if x.get("suite_id")}
    profile_index = {x["profile_id"]: x for x in looked_up["profiles"] if x.get("profile_id")}
    for r in ordered:
        if r.get("kind") == "voice":
            suite_by_id[r["suite_id"]] = {"suite_id": r["suite_id"], "name": r.get("_check_name"),
                                          "pass_threshold": VOICE_CHECK_TARGET}
    for col, r in zip(matrix["columns"], ordered):
        col["kind"] = r.get("kind") or "text"
        if col["kind"] == "voice":
            col["label"] = "Voice check"
        suite = suite_by_id.get(r.get("suite_id") or "") or {}
        col["suite_name"] = suite.get("name") or ("Live conversations" if not r.get("suite_id") else "Deleted suite")
        col["target"] = threshold_of(suite) if suite else None
        col["agent"] = profile_label(r.get("profile_id"), profile_index)
        col["status"] = r.get("status")
        col["score"] = r.get("score")
        col["when"] = r.get("finished_at") or r.get("created_at")
        res = r.get("results") or []
        col["cases_passed"] = sum(1 for x in res if isinstance(x, dict) and x.get("passed"))
        col["cases_total"] = len(res)
    for srow in matrix["suites"]:
        srow["name"] = (suite_by_id.get(srow["suite_id"]) or {}).get("name") or srow["suite_id"][:8]

    # Case changes between the baseline (first) and the change (last).
    base, last = ordered[0], ordered[-1]
    changes: Dict[str, List[Dict[str, Any]]] = {"fixed": [], "broke": [], "still": []}
    same_suite = base.get("suite_id") == last.get("suite_id")
    if same_suite:
        joined = _compare_runs(base, last)["joined"]
        for j in joined:
            kind = {"gained": "fixed", "regressed": "broke", "held_fail": "still"}.get(j["result"])
            if kind:
                changes[kind].append({
                    "scenario_id": j["scenario_id"],
                    "display_name": case_display_name(j["b"] or j["a"] or {}),
                    "reason": case_reason(j["b"] or {}) if kind != "fixed" else "",
                })
        for v in changes.values():
            v.sort(key=lambda x: str(x["scenario_id"]))
    score_delta = None
    if base.get("score") is not None and last.get("score") is not None:
        score_delta = float(last["score"]) - float(base["score"])

    def _cost(r: Dict[str, Any]) -> Optional[float]:
        vals = [x.get("cost_usd") for x in (r.get("results") or []) if isinstance(x, dict)
                and isinstance(x.get("cost_usd"), (int, float))]
        return sum(vals) if vals else None

    cost_a, cost_b = _cost(base), _cost(last)
    cost_change = ((cost_b - cost_a) / cost_a) if (cost_a and cost_b is not None) else None
    return templates.TemplateResponse(
        request,
        "eval_run_compare.html",
        {"ctx": ctx, "active": "evals", "current_env": env,
         "runs": ordered, "matrix": matrix, "changes": changes, "same_suite": same_suite,
         "is_voice": ordered[0].get("kind") == "voice",
         "score_delta": score_delta, "cost_change": cost_change},
    )
