"""Generic bake-off: N profile-version variants x M suites of one profile.

Persists an agent_bakeoffs row, then spawns one eval run per (variant, suite)
via start_suite_run, tagging each with bakeoff_id + the variant's
profile_version_id / model / gen_config. Read side rolls the linked runs into a
per-suite + aggregated matrix via admin.services.eval_matrix.

Pre-flight validation (before any DB write): `start_suite_run` itself raises
ValueError when a suite lacks an evaluator model, or when a variant's pinned
`profile_version_id` doesn't belong to `profile_id` — but a bake-off spawns
many runs in a loop, so hitting either guard on run #3 would leave a
half-spawned bake-off (row inserted, some runs started, the rest missing).
Both checks are re-run up front here, once each across all suites/variants,
so `create_bakeoff` fails clean with a single clear error before the
`agent_bakeoffs` row — let alone any run — is written.

Even with both pre-flight guards, the spawn loop itself can still fail
mid-way (a transient DB error on run #2 of N, `start_tracked_job` blowing up,
etc.) — at that point the `agent_bakeoffs` row is already inserted and some
runs are already spawned. Rather than leave that row stuck at status
'running' forever (indistinguishable from a bake-off that's merely slow),
the loop is wrapped so a genuine failure marks the row 'errored' (best
effort — the DB may itself be the thing that's down) and re-raises the
original exception so the caller/route still sees it.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from admin.services.eval_matrix import build_variant_matrix
from admin.services.evals import start_suite_run
from core.auth import admin_service_client

logger = logging.getLogger(__name__)

_TERMINAL_RUN_STATUSES = {"passed", "failed", "errored", "cancelled"}


def _normalize_variant_labels(variants: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return `variants` with every `label` made unique + non-empty.

    `build_variant_matrix` groups scenario results by the exact
    `variant_label` string. Two variants sharing a label — or both left
    blank, which would otherwise both fall back to the same default — merge
    their scenario data into a single matrix row, silently corrupting the
    comparison. Normalize once, up front, so the persisted
    `agent_bakeoffs.variants` jsonb and every spawned run's `variant_label`
    agree on the same value:

    - blank/whitespace-only label -> "variant {1-based index}"
    - a label already used by an earlier variant (exact, case-sensitive
      match — matrix grouping is exact-string) -> " (2)", " (3)", ... appended
      until unique

    Returns new dicts; does not mutate the caller's variant dicts.
    """
    used: set = set()
    normalized: List[Dict[str, Any]] = []
    for i, v in enumerate(variants):
        label = (v.get("label") or "").strip()
        if not label:
            label = f"variant {i + 1}"
        base = label
        n = 2
        while label in used:
            label = f"{base} ({n})"
            n += 1
        used.add(label)
        normalized.append({**v, "label": label})
    return normalized


def create_bakeoff(*, name: str, profile_id: str,
                   variants: List[Dict[str, Any]], suite_ids: List[str],
                   repeats: int, actor_user_id: Optional[str],
                   user_token: Optional[str]) -> Dict[str, Any]:
    if name is None:
        raise ValueError("name is required")
    if len(variants) < 2:
        raise ValueError("a bake-off needs at least 2 variants")
    if not suite_ids:
        raise ValueError("pick at least one suite")

    # Clamp once, up front, so both the persisted row and every spawned
    # start_suite_run call agree on the same bounded value. start_suite_run
    # has no upper bound of its own (only trigger_eval_run's caller clamps
    # its own repeats to 1-20) — without this clamp here, a raw POST or MCP
    # `bakeoffs create` with e.g. repeats=500 would fan out
    # 500 x variants x suites eval runs.
    repeats = max(1, min(20, int(repeats or 1)))

    client = admin_service_client()

    # Pre-flight #1: every JUDGE-scored suite needs an evaluator model.
    # start_suite_run enforces this per-run (model_id/profile_version_id
    # triggers its judge guard), but by the time it fires mid-loop other runs
    # may already be spawned. Check every suite once, before the bakeoff row is
    # written. Suites whose runner_kind has a built-in deterministic scorer
    # (a plugin runner's `default_scorer`) grade themselves and are
    # correctly evaluator-less, so they're exempt — see runner_kind_needs_judge.
    from admin.services.eval_scorers import JUDGE_MODEL_HINT, suite_judge_problem

    suite_rows = (client.table("agent_eval_suites")
                  .select("suite_id, name, evaluator_model_id, runner_kind, scenarios")
                  .in_("suite_id", suite_ids)
                  .execute().data or [])
    suite_by_id = {r.get("suite_id"): r for r in suite_rows}
    problems = []
    for sid in suite_ids:
        suite = suite_by_id.get(sid) or {}
        problem = suite_judge_problem(client, suite)
        if problem:
            problems.append(f"{suite.get('name') or sid} ({problem})")
    if problems:
        # Name the suites and the actual fix location: the suite editor's
        # Judge model field. The check matches what the runner can build
        # (admin/e2e_runner._build_judge): any provider the engine calls.
        raise ValueError(
            "comparison runs require a model judge on every judged suite: "
            + "; ".join(problems) + ". " + JUDGE_MODEL_HINT + "."
        )

    # Pre-flight #2: every variant's pinned profile_version_id must belong to
    # this profile_id. Same reasoning — one clear pre-flight error rather
    # than a partial spawn. Single query across all variants' version ids.
    version_ids = [v["profile_version_id"] for v in variants if v.get("profile_version_id")]
    if version_ids:
        version_rows = (client.table("agent_profile_versions")
                        .select("version_id, profile_id")
                        .in_("version_id", version_ids)
                        .execute().data or [])
        profile_by_version = {r.get("version_id"): r.get("profile_id") for r in version_rows}
        bad_versions = [vid for vid in version_ids if profile_by_version.get(vid) != profile_id]
        if bad_versions:
            raise ValueError(
                f"profile version(s) do not belong to profile {profile_id}: "
                + ", ".join(str(v) for v in bad_versions)
            )

    # Normalize labels (unique + non-empty) before anything is persisted or
    # spawned, so the stored `variants` jsonb and every run's `variant_label`
    # agree — see `_normalize_variant_labels` for why this matters.
    variants = _normalize_variant_labels(variants)

    row = client.table("agent_bakeoffs").insert({
        "name": name.strip() or "bake-off", "profile_id": profile_id,
        "variants": variants, "suite_ids": suite_ids,
        "status": "running", "repeats": repeats,
        "created_by": actor_user_id, "updated_by": actor_user_id,
    }).execute()
    bakeoff_id = (row.data or [{}])[0].get("bakeoff_id")

    run_ids: List[str] = []
    try:
        for v in variants:
            label = v["label"]
            for suite_id in suite_ids:
                run_id = start_suite_run(
                    suite_id=suite_id, profile_id=profile_id, business_id=None,
                    actor_user_id=actor_user_id, user_token=user_token,
                    model_id=v.get("model_id"), gen_config=v.get("gen_config"),
                    variant_label=label, repeats=repeats,
                    profile_version_id=v.get("profile_version_id"),
                    bakeoff_id=bakeoff_id,
                )
                run_ids.append(run_id)
    except Exception:
        # Mid-loop failure: some runs may already be spawned (preserved in
        # run_ids for the log line below), the rest never will be. Flip the
        # row to 'errored' so it's visibly broken rather than stuck
        # 'running' forever, then re-raise — this is not ours to swallow,
        # the route/caller still needs to see it.
        logger.exception(
            "bake-off %s failed mid-spawn after %d run(s) started: %s",
            bakeoff_id, len(run_ids), run_ids,
        )
        try:
            client.table("agent_bakeoffs").update({
                "status": "errored", "updated_by": actor_user_id,
            }).eq("bakeoff_id", bakeoff_id).execute()
        except Exception:
            logger.exception(
                "bake-off %s: also failed to mark status='errored' after spawn failure",
                bakeoff_id,
            )
        raise
    return {"bakeoff_id": bakeoff_id, "run_ids": run_ids}


def get_bakeoff(bakeoff_id: str, suite_focus: Optional[str] = None) -> Dict[str, Any]:
    """Load one bake-off + its linked runs, rolled into a matrix.

    `status` is recomputed from the linked `agent_eval_runs` rows rather than
    trusted verbatim off the `agent_bakeoffs` row: 'complete' iff every
    linked run has reached a terminal status, else 'running'. A stored
    'errored' status (set by `create_bakeoff`'s mid-spawn guard) is STICKY —
    it short-circuits the recompute entirely, so a bake-off already marked
    broken never flips back to 'complete' just because its already-spawned
    runs happened to finish naturally. Without this, the recompute would
    read "every linked run is terminal" literally and silently erase the
    error signal (and persist that flip), which is exactly wrong: partial
    spawn + finished partial runs is not a legitimately complete comparison.
    A bake-off with zero linked runs keeps its stored status verbatim (no
    runs to judge terminality by). The recomputed status is written back to
    the row only when it changed AND the new status is terminal
    ('complete') — a still-running bake-off is re-read every poll, so this
    avoids writing on every single read.

    `suite_focus`, when given, narrows `matrix` to just that suite's
    per-variant rows (`{"rows": [...]}`). Otherwise `matrix` is the full
    `build_variant_matrix` output — `rows` (per suite x variant) AND
    `by_variant` (aggregated across all suites) — so the aggregated
    (no-focus) view has both.
    """
    client = admin_service_client()
    rows = (client.table("agent_bakeoffs").select("*")
            .eq("bakeoff_id", bakeoff_id).limit(1).execute().data or [])
    if not rows:
        raise ValueError(f"bake-off {bakeoff_id} not found")
    bakeoff = rows[0]

    runs = (client.table("agent_eval_runs").select("*")
            .eq("bakeoff_id", bakeoff_id).execute().data or [])

    if bakeoff.get("status") == "errored":
        # Sticky: a stored 'errored' status is never recomputed away, no
        # matter how the linked runs look. No write-back either — the row
        # already says 'errored', there's nothing to persist.
        status = "errored"
    elif runs:
        status = "complete" if all(
            r.get("status") in _TERMINAL_RUN_STATUSES for r in runs
        ) else "running"
    else:
        status = bakeoff.get("status") or "running"

    if status != bakeoff.get("status") and status == "complete":
        try:
            client.table("agent_bakeoffs").update({"status": status}).eq(
                "bakeoff_id", bakeoff_id).execute()
        except Exception:
            logger.exception(
                "bake-off %s: failed to persist recomputed status=%s", bakeoff_id, status,
            )
    bakeoff["status"] = status

    full_matrix = build_variant_matrix(runs)
    if suite_focus:
        matrix = {"rows": [r for r in full_matrix["rows"] if r.get("suite_id") == suite_focus]}
    else:
        matrix = full_matrix

    return {"bakeoff": bakeoff, "matrix": matrix, "status": status}


def list_bakeoffs(limit: int = 25) -> List[Dict[str, Any]]:
    """Newest-first `agent_bakeoffs` rows, lightly decorated for a list view:
    `n_variants` / `n_suites` counts and `_profile_name` (the owning
    profile's display name). Does NOT load linked runs — that's `get_bakeoff`
    territory; this stays lean for a list screen.

    The profile-name join is a single batched query across the page's
    `profile_id`s (not one lookup per row), so this stays O(1) queries
    regardless of `limit`.
    """
    client = admin_service_client()
    rows = (client.table("agent_bakeoffs").select("*")
            .order("created_at", desc=True).limit(limit).execute().data or [])

    profile_ids = sorted({r.get("profile_id") for r in rows if r.get("profile_id")})
    profile_name_by_id: Dict[str, str] = {}
    if profile_ids:
        profile_rows = (client.table("agent_profiles")
                         .select("profile_id, display_name")
                         .in_("profile_id", profile_ids).execute().data or [])
        profile_name_by_id = {
            p.get("profile_id"): p.get("display_name")
            for p in profile_rows if p.get("profile_id")
        }

    for r in rows:
        r["n_variants"] = len(r.get("variants") or [])
        r["n_suites"] = len(r.get("suite_ids") or [])
        r["_profile_name"] = profile_name_by_id.get(r.get("profile_id")) or r.get("profile_id")
    return rows
