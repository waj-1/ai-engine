"""Seed agent_eval_suites from the deployment's suite specs.

Idempotent on `name`. Called from main.py lifespan after the rest of the
registry seeders so it can bind suites to real profile and model rows.

The engine ships no suite of its own: every spec comes from a plugin's
`eval_suite_seeds` hook (`<plugin>.eval_suite_seeds.eval_suite_specs()`, see
core/plugins.py), in plugin order. A spec is a dict with `name`,
`agent_kind`, `pass_threshold` and either inline `scenarios` or a
`scenarios_file` under evals/scenarios/; optional keys: `evaluator` (bind an
LLM judge), `runner_kind`, `default_context`, `first_message` (mark every
scenario as a session's first message) and `profile` ({slug, environment}:
bind to that named profile; the suite is skipped until it exists).
"""
from __future__ import annotations

import copy
import json
import logging
import pathlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.auth import get_service_client
from core.deployment import active_scope, all_scope_kinds, scope_kinds

logger = logging.getLogger(__name__)

_SCENARIO_DIR = pathlib.Path(__file__).resolve().parent.parent / "evals" / "scenarios"


def _suite_specs() -> List[Dict[str, Any]]:
    """Every plugin's suite specs, in plugin order."""
    from core.plugins import call_hook, load_hook

    specs: List[Dict[str, Any]] = []
    for found in call_hook(load_hook("eval_suite_seeds"), "eval_suite_specs"):
        specs.extend(found)
    return specs


def _judge_profile_kinds() -> List[str]:
    """Agent kinds whose profile's main model a plugin prefers as the judge."""
    from core.plugins import call_hook, load_hook

    kinds: List[str] = []
    for found in call_hook(load_hook("eval_suite_seeds"), "judge_profile_kinds"):
        kinds.extend(found)
    return kinds


# Read once per process; a test swaps it with monkeypatch.
_SUITES: List[Dict[str, Any]] = _suite_specs()

# Every agent kind owned by ANY deployment scope (every scope its plugin defines).
# Used by run()'s registry gate below — mirrors sync_tools.py / seed_prompts.py.
_ALL_SCOPE_KINDS = all_scope_kinds()


def _resolve_judge_model_id(client: Any, environment: str) -> Optional[str]:
    """Pick an LLM-judge model for the suites that need qualitative scoring.

    Preference order (each step skipped when it yields nothing):
      1. the main model of a profile whose kind a plugin names
         (`judge_profile_kinds()`), IF `admin/e2e_runner._try_build_judge`
         can actually build it (Gemini or Claude model_string only — anything
         else, e.g. an Azure GPT-5 Mini chat model, 404s at judge-call time
         instead of falling back);
      2. any active Gemini model row in `environment` (the cheap default
         judge; `agent_models` is env-scoped in the shared AI DB, so an
         unscoped ilike could hand back another environment's row);
      3. any active Claude model row in `environment` — the only OTHER
         provider `_try_build_judge` supports.

    Since 2026-11-23 the judge builder takes ANY provider the engine can call
    (Ollama, OpenAI-compatible, Azure, ...); this seeder still prefers Gemini
    then Claude so the product build seeds as before, and an operator can pick
    any other row in the suite editor.

    None means genuinely no judge-capable model row exists (e.g. an
    Azure-only catalog) — those suites seed without `evaluator_model_id`,
    fall back to substring-only grading on plain runs, and the bake-off
    pre-flight (`admin/services/bakeoffs.create_bakeoff`) names the fix.
    """
    for kind in _judge_profile_kinds():
        try:
            prof = (
                client.table("agent_profiles").select("model_main_id")
                .eq("agent_kind", kind).limit(1).execute().data or []
            )
            model_main_id = prof[0].get("model_main_id") if prof else None
            if model_main_id:
                model_rows = (
                    client.table("agent_models").select("model_string")
                    .eq("model_id", model_main_id).limit(1).execute().data or []
                )
                model_string = ((model_rows[0].get("model_string") if model_rows else None) or "").lower()
                if "gemini" in model_string or "claude" in model_string:
                    return model_main_id
        except Exception:  # noqa: BLE001
            pass
    for pattern in ("%gemini%", "%claude%"):
        try:
            m = (
                client.table("agent_models").select("model_id")
                .ilike("model_string", pattern).eq("is_active", True)
                .eq("environment", environment)
                .limit(1).execute().data or []
            )
            if m:
                return m[0]["model_id"]
        except Exception:  # noqa: BLE001
            pass
    return None


def _resolve_active_profile_id(
    client: Any, agent_kind: str, environment: str,
    cache: Dict[str, Optional[str]],
) -> Optional[str]:
    """profile_id of the ACTIVE profile for `agent_kind` in `environment` —
    the same (agent_kind, environment, is_active, status='active') resolution
    the runtime's `AgentProfileLoader._load_from_db` uses, so a seeded suite
    binds to the row the bake-off wizard's profile picker actually offers.

    None when absent or on any lookup error: a suite can seed unbound (the
    portal flags it "unassigned") and seeding must never gate boot. Memoized
    per run() via `cache` so N suites of one kind cost one query.
    """
    if agent_kind in cache:
        return cache[agent_kind]
    profile_id: Optional[str] = None
    try:
        rows = (
            client.table("agent_profiles").select("profile_id")
            .eq("agent_kind", agent_kind)
            .eq("environment", environment)
            .eq("is_active", True)
            .eq("status", "active")
            .limit(1).execute().data or []
        )
        profile_id = rows[0]["profile_id"] if rows else None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "seed_eval_suites: active-profile lookup failed for kind=%s env=%s (%s)",
            agent_kind, environment, exc,
        )
    cache[agent_kind] = profile_id
    return profile_id


def _row_untouched_since_seed(existing: Dict[str, Any]) -> bool:
    """True iff this suite row was never modified after its INSERT —
    `updated_at == created_at` (both default to the same `now()` in the
    seeder's INSERT statement, which sets neither).

    Any later write flips this permanently: the `set_updated_at` BEFORE
    UPDATE trigger (supabase/db-functions/validation/created_updated_columns.ts
    — applied to every deployed stack, incl. a self-hosted demo VM's init) bumps
    `updated_at` on every UPDATE, the portal's suite editor also sets it
    explicitly (for a stack whose db-functions never ran), and the backfill
    below writes it into its own patch for the same reason.

    Missing/unparseable timestamps count as TOUCHED — when we can't prove
    the row is pristine, the seeder keeps its hands off (fail safe).
    """
    created, updated = existing.get("created_at"), existing.get("updated_at")
    if not created or not updated:
        return False
    if created == updated:
        return True
    try:
        def _parse(v: Any) -> datetime:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return _parse(created) == _parse(updated)
    except Exception:  # noqa: BLE001
        return False


def _backfill_suite_bindings(
    client: Any, existing: Dict[str, Any], *,
    profile_id: Optional[str], evaluator_model_id: Optional[str],
) -> bool:
    """One-shot fill of NULL binding columns on a pristine seeded suite row.

    A NULL `profile_id` / `evaluator_model_id` silently hides the suite from
    the bake-off wizard (`_load_suites_by_profile` drops NULL-bound suites)
    and blocks bake-off creation (the judge pre-flight) — but a NULL is only
    the seeder's own gap when the row was **never modified since seeding**.
    The suite editor legitimately CLEARS both columns to NULL
    (admin/eval_handlers.py: "— none —" nulls profile_id; an empty evaluator
    select nulls evaluator_model_id), and "unassigned" is a real state an
    operator can choose — re-filling it on the next boot would be exactly
    the seeder-stomps-operator-edits failure this repo's seeder rules exist
    to prevent.

    So the backfill is gated on `_row_untouched_since_seed` (updated_at ==
    created_at) and has one-shot semantics: its own UPDATE bumps
    `updated_at` (via the set_updated_at trigger where db-functions ran,
    and explicitly in the patch below for a bare local stack without it), so
    each row is backfilled AT MOST ONCE, ever. After that — and after any
    operator save or clear — the seeder never touches the row again. On the
    pristine row itself only NULL columns are filled; non-NULL values (a
    first-boot insert-time binding) are left as-is. Pass None for a column
    to leave it alone entirely. Returns True when it wrote.
    """
    if not _row_untouched_since_seed(existing):
        return False
    patch: Dict[str, Any] = {}
    if profile_id and existing.get("profile_id") is None:
        patch["profile_id"] = profile_id
    if evaluator_model_id and existing.get("evaluator_model_id") is None:
        patch["evaluator_model_id"] = evaluator_model_id
    if not patch:
        return False
    # Explicit updated_at: the set_updated_at trigger already bumps it on
    # every deployed stack (db-functions), where now() simply overwrites this
    # value — carrying it in the patch keeps the one-shot guarantee on a
    # stack whose db-functions never ran (bare local `supabase db reset`).
    patch["updated_at"] = datetime.now(timezone.utc).isoformat()
    client.table("agent_eval_suites").update(patch).eq(
        "suite_id", existing["suite_id"]).execute()
    return True


def _localize_voice_ws_path(default_context: Dict[str, Any], scope: Any) -> Dict[str, Any]:
    """Rewrite a voice_live suite's `default_context.voice.ws_path` to the
    active deployment scope's mount point (`<portal_prefix>/voice/live`)
    when one is active.

    A client's voice_live suite is only
    ever seeded on a scoped build (see run()'s registry gate below), and
    on a scoped build the duplex voice router mounts at
    `<portal_prefix>/voice/live`, not the bare `/voice/live` the `_SUITES`
    literal declares (see main.py:_mount_prefix) — so the literal value was
    ALWAYS wrong for the one build it's ever inserted on.

    Returns `default_context` UNCHANGED (same object, no copy) when there is
    nothing to rewrite — no active client, or the suite has no
    `voice.ws_path` — and a deep copy with only `voice.ws_path` rewritten
    otherwise. Never mutates the input in place: `_SUITES` is a shared
    module-level literal and seeders can run more than once per process
    lifetime (every lifespan tick), so mutating it here would corrupt every
    later run/read of the constant.
    """
    if scope is None:
        return default_context
    voice = default_context.get("voice")
    if not isinstance(voice, dict) or "ws_path" not in voice:
        return default_context
    new_context = copy.deepcopy(default_context)
    new_context["voice"]["ws_path"] = f"{scope.portal_prefix}/voice/live"
    return new_context


def _resolve_named_profile_id(client: Any, agent_kind: str, slug: str,
                              environment: str) -> Optional[str]:
    """profile_id of the profile `(agent_kind, slug, environment)`; None when
    absent or on any lookup error (seeding must never gate boot)."""
    try:
        rows = (
            client.table("agent_profiles").select("profile_id")
            .eq("agent_kind", agent_kind).eq("slug", slug).eq("environment", environment)
            .limit(1).execute().data or []
        )
        return rows[0]["profile_id"] if rows else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("seed_eval_suites: profile lookup failed for %s/%s (%s)",
                       agent_kind, slug, exc)
        return None


def _suite_profile_id(client: Any, suite: Dict[str, Any], environment: str,
                      cache: Dict[str, Optional[str]]) -> Optional[str]:
    """The profile a suite binds to: its named `profile` when the spec names
    one, else the active profile of its kind in `environment`."""
    named = suite.get("profile")
    if named:
        return _resolve_named_profile_id(
            client, suite["agent_kind"], named["slug"], named.get("environment") or environment)
    return _resolve_active_profile_id(client, suite["agent_kind"], environment, cache)


def run() -> Dict[str, int]:
    if not _SUITES:
        # A build with no plugin has no suites to seed (and its agents are
        # made in the portal, with suites made for them there).
        logger.info("seed_eval_suites: no suite specs in this build — nothing seeded")
        return {"inserted": 0, "skipped": 0, "backfilled": 0}
    client = get_service_client()
    # Registry gate (mirrors sync_tools.run() / seed_prompts.run()): a
    # scoped build seeds ONLY the active scope's suites; a full build seeds
    # everything EXCEPT the union of ALL scopes' kinds. Previously this was a
    # hand-rolled two-way check for one scope by name, which only ever
    # excluded that scope: on any other scope every full-build suite passed
    # the check and leaked into that demo's DB. Idempotent on name.
    try:
        scope = active_scope()
    except ValueError as exc:
        # Unknown scope configuration: boot validation elsewhere refuses to
        # start; a seeder must not crash — fail safe to the full-build gate
        # (which skips every scope's kind).
        logger.warning("seed_eval_suites: %s — treating as normal WAJ build", exc)
        scope = None
    allowed_kinds = scope_kinds(scope) if scope is not None else None
    # Suites bind to the ACTIVE profile of their agent_kind in THIS process's
    # environment — the same environment every other seeder targets
    # (core.agent_profile._default_environment; unset ENVIRONMENT = staging).
    # The judge fallback is scoped to the same environment.
    from core.agent_profile import _default_environment
    environment = _default_environment()
    judge_model_id = _resolve_judge_model_id(client, environment)
    profile_cache: Dict[str, Optional[str]] = {}
    inserted = 0
    skipped = 0
    backfilled = 0
    for suite in _SUITES:
        if allowed_kinds is not None:
            # Scoped build: ONLY the active client's kinds.
            if suite["agent_kind"] not in allowed_kinds:
                continue
        elif suite["agent_kind"] in _ALL_SCOPE_KINDS:
            # Normal WAJ build: no deployment scope's kinds, ever.
            continue
        # Per-suite guard: one bad suite (malformed entry, a DB refusal on
        # this one row, …) must log and move on — the same isolation
        # main.py's boot loop applies per seeder. Before this, one failing
        # insert aborted every later suite in the list.
        try:
            existing = (
                client.table("agent_eval_suites")
                .select("suite_id, profile_id, evaluator_model_id, created_at, updated_at")
                .eq("name", suite["name"]).limit(1).execute().data or []
            )
            if existing:
                skipped += 1
                # One-shot backfill, pristine rows only: a NULL binding
                # column is the seeder's own gap ONLY on a row nobody has
                # modified since seeding (updated_at == created_at) — the
                # suite editor can deliberately clear these to NULL, and a
                # cleared binding must stay cleared. Any write (an
                # operator's, or this backfill's own) bumps updated_at, so
                # the seeder touches each row at most once, ever
                # (see _backfill_suite_bindings).
                if _backfill_suite_bindings(
                    client, existing[0],
                    profile_id=(
                        _suite_profile_id(client, suite, environment, profile_cache)
                        if existing[0].get("profile_id") is None else None
                    ),
                    evaluator_model_id=(
                        judge_model_id if suite.get("evaluator") else None
                    ),
                ):
                    backfilled += 1
                continue
            scenarios = _resolve_scenarios(suite)
            if scenarios is None:
                continue
            row: Dict[str, Any] = {
                "name": suite["name"],
                "agent_kind": suite["agent_kind"],
                "scenarios": scenarios,
                "pass_threshold": suite["pass_threshold"],
            }
            # Bind to the kind's active profile so the suite is visible to
            # the bake-off wizard (`_load_suites_by_profile` drops suites
            # with NULL profile_id). Unresolvable -> seed unbound, the
            # portal flags it "unassigned".
            profile_id = _suite_profile_id(client, suite, environment, profile_cache)
            if profile_id:
                row["profile_id"] = profile_id
            elif suite.get("profile"):
                # Bound to one named profile that does not exist yet (a fresh
                # stack before its seeder ran): seed it on a later boot.
                logger.info(
                    "seed_eval_suites: no %s profile %r (env=%s) yet — skipping %s",
                    suite["agent_kind"], suite["profile"].get("slug"),
                    suite["profile"].get("environment"), suite["name"],
                )
                continue
            if suite.get("evaluator") and judge_model_id:
                row["evaluator_model_id"] = judge_model_id
            # Most _SUITES entries are 'chat' suites and never set these — only
            # carry them when the suite dict actually has them (e.g.
            # a voice suite's runner_kind='voice_live' + default_context.voice)
            # so every other suite's insert payload is byte-for-byte unchanged.
            if "runner_kind" in suite:
                row["runner_kind"] = suite["runner_kind"]
            if "default_context" in suite:
                row["default_context"] = _localize_voice_ws_path(suite["default_context"], scope)
            client.table("agent_eval_suites").insert(row).execute()
            inserted += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "seed_eval_suites: suite %r failed (%s: %s) — continuing with the rest",
                suite.get("name"), type(exc).__name__, exc,
            )

    if backfilled:
        logger.info(
            "seed_eval_suites: backfilled NULL bindings (profile_id/"
            "evaluator_model_id) on %d existing suite row(s)", backfilled,
        )
    return {"inserted": inserted, "skipped": skipped}


def _resolve_scenarios(suite: Dict[str, Any]) -> Any:
    """Either return the inline `scenarios` list or load the JSON file."""
    if "scenarios" in suite:
        scenarios = suite["scenarios"]
    else:
        path = _SCENARIO_DIR / suite["scenarios_file"]
        if not path.exists():
            logger.warning("seed_eval_suites: missing %s; skipping %s", path, suite["name"])
            return None
        scenarios = json.loads(path.read_text())
    # A suite of standalone single-turn conversations marks every scenario as
    # the first message, so the agent persists a session + the user/assistant
    # turn — otherwise the trace has no chat session and the Observability
    # transcript falls back to span-only reconstruction, which surfaces
    # internal model output (e.g. the memory-extraction JSON) instead of the
    # conversation.
    if suite.get("first_message") and isinstance(scenarios, list):
        for s in scenarios:
            if isinstance(s, dict):
                s.setdefault("is_first_message", True)
    return scenarios
