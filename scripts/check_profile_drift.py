"""Report where a live AI project disagrees with this checkout.

WHY THIS EXISTS

Every `scripts/seed_*.py` is insert-if-missing by design — it must be safe to
re-run on boot without stomping an operator's portal edits. The consequence is
that once a row exists the seeder never touches it again, so a repo edit can
NEVER reach a stack that has already been seeded. The repo and the live
database drift apart silently and permanently, and nothing reports it.

That is not theoretical. On 2026-08-05 a single day surfaced three instances,
each invisible until it caused a live symptom:

  * a client demo's chat profile was serving a `system` prompt version
    created 7 WEEKS earlier, so a merged prompt fix (#51) had no effect on
    the demo at all — Arabic callers kept hitting the reversed branch rule
    the fix had removed.
  * `barge_in_enabled` was still `false` on the demo, left over from the #38
    incident. Callers could not interrupt at all, and every in-flight answer
    played to the end. Nobody had turned it back on because nothing said it
    was off.
  * voice `memory_config.enabled` was `true`, putting a cross-cloud embedding
    on the answer's critical path.

Each was found by hand, days or weeks late. This tool finds them in seconds.

WHAT IT CHECKS

  prompt   — whether a profile's SERVED prompt (resolved through
             `prompt_version_ids`, not merely the newest version — a profile
             pinned to an OLD version while newer ones exist is exactly the
             failure this tier exists to catch) is SAFE: it must pass
             `core.prompt_validator.validate_prompt`, the same gate
             `admin_mcp/tools_configure.py`'s publish path
             (`prompts(action="create_version"|"serve"|"publish_from_repo")`)
             runs before a write. A served prompt DIFFERING from the repo
             constant is no longer drift by itself — prompts are authored in
             the DB now (CLAUDE.md: "prompts change through MCP/API/DB, never
             through a merge"), so divergence is EXPECTED and reported as
             `info` when the validator has real rules to check it against,
             or `warning` when it does not (nothing here can call an
             unregistered prompt's divergence safe, so it is not reported as
             clean either). What is still `critical` is the original
             failure restated for DB-authoring: the SERVED version itself
             FAILS validation — whether that version is stale, hand-edited,
             or freshly published, a bad pin is a bad pin. The 2026-08-05
             incident below (#51's Arabic branch-rule reversal) is still
             caught under these semantics: it is exactly what
             `core.prompt_validator.FORBIDDEN_PHRASES` flags.
  config   — a small set of levers whose WRONG value is silent. Emergency
             switches are the dangerous class: they are pulled during an
             incident and left, because a disabled feature raises nothing.
  model    — the `agent_models` row a profile's `model_main_id` names,
             compared on a dotted `extra_config` path. Profiles are not the
             only rows that drift: on 2026-08-06 the voice delegate's model
             row ran `reasoning_effort: "low"` while the repo seeds
             "minimal" — ~3.1s added to EVERY voice delegate call — and
             nothing reported it, because the config tier digs only
             `agent_profiles` fields. Checks may be gated on the row's
             `provider`, because the thinking knob is provider-specific:
             a client demo's voice brain is Gemini 3.5 Flash-Lite at
             `thinking_level: "low"` (a deliberate 2026-08-19
             anti-fabrication choice) while a fresh seed still lands on the
             Azure GPT-5 Mini row and its `reasoning_effort: "minimal"`.
  tools    — a profile's MCP bundle-attachment `tool_subset`
             (`agent_profile_mcp_servers`) vs the seed constant it was
             provisioned from. The subset IS the profile's callable tool
             set, it is edited live in /admin, and on 2026-08-26 the live
             client demo had silently dropped `get_transactions` and
             `get_daily_branch_report` from the voice allowlist — callers
             asking for their transactions got KB answers, and no check
             caught it.
  voice    — cross-row: a profile whose voice_config.model_id names a
             NATIVE-audio realtime row must not map Azure neural voice names
             over it (voice_by_language / voice_id) — that override replaces
             the model's own dialect speech with an MSA-trained TTS voice,
             silently (the 2026-08-16 accent regression).
  persona  — WHERE the duplex voice persona's version pin lives. Two states
             work and are governance holes anyway, which is why nothing ever
             reported them: a legacy pin on the SPECIALIST (honoured, but it
             warns on every connect and a parent-side save cannot displace
             it), and no pin at all (globally-latest — promote and rollback
             control nothing, and a draft save is live on the next call). The
             tier order is reused from `admin/_persona_versions.py`, never
             re-derived.
  info     — operator-owned levers reported for visibility, never failed.
             `progress_after_sec` is the registered example: the live stack
             runs 2.5 vs the code default 4.0, and neither is simply
             "right" — 2.5 fires on 56% of turns against a 2.68s p50
             answer, i.e. mostly ON TOP of the answer it was masking. The
             report shows live-vs-default so an operator can see the lever;
             it never affects the exit code.

USAGE

    python -m scripts.check_profile_drift --env prod      # report, exit 1 on drift
    python -m scripts.check_profile_drift --env staging --quiet   # exit code only
    python -m scripts.check_profile_drift --env prod --diff       # + prompt diff

    make drift-check ENV=prod

`--env` names the `agent_profiles.environment` rows to audit, and a bare
invocation REFUSES to run (exit 2) rather than guess. That is not pedantry:
the documented prod-access recipe is a local shell with `AI_SUPABASE_*`
pointed at prod and `ENVIRONMENT` unset — and an unset `ENVIRONMENT` collapses
to 'staging' (`core.agent_profile._default_environment`). Guessing therefore
means reading STAGING rows out of the prod database, never examining prod's
incident levers, and exiting 0: a clean bill of health from the one tool whose
entire purpose is catching silent prod drift. An exported `ENVIRONMENT` counts
as the choice having been made, and either way the report NAMES the audited
environment as its first finding and again beside the verdict.

The DATABASE is still whatever `core.auth.get_service_client()` resolves — the
env flag selects rows within it, it does not repoint the connection. So point
`AI_SUPABASE_*` at the stack you mean AND pass the matching `--env`. On a
demo VM whose bundled Supabase is not reachable from outside, run it inside
the engine container.

NOT a CI gate today: CI cannot reach a demo VM's bundled Supabase. It is an
operator/cron tool, and the natural next step is an /admin screen calling
`collect_drift()` directly — the reporting is deliberately separated from the
checking for that reason.
"""
from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from admin._persona_versions import (  # noqa: E402
    TIER_SPECIALIST_PIN,
    collect_persona_version_status,
)
from voice.live.config import promote_legacy_voice_config  # noqa: E402

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2, "ok": 3}

# "info" findings are visibility, not drift — they never affect the exit code.
DRIFT_SEVERITIES = ("critical", "warning")

_UNSPECIFIED = object()


@dataclass
class PromptCheck:
    """A profile's served prompt vs the repo constant it was seeded from."""

    profile_slug: str
    agent_kind: str
    prompt_key: str            # key inside prompt_version_ids, e.g. "system"
    repo_constant: str         # dotted path, for the report
    loader: Callable[[], str]  # deferred so an import error is not fatal
    why: str


@dataclass
class ConfigCheck:
    """A config value whose wrong setting is SILENT.

    `expected` is what a healthy stack looks like. `incident_lever` marks the
    switches that get pulled in an outage and left — the class that cost the
    most here, because nothing anywhere reports a feature being off.

    Set EITHER `expected` (there is one correct value) OR `disabled_value`
    (there is not, but there is one value that turns the thing OFF).
    """

    profile_slug: str
    path: List[str]            # e.g. ["voice_config", "barge_in_enabled"]
    why: str
    expected: Any = _UNSPECIFIED
    # The "0 disables" mode. Several duplex levers have NO single correct
    # value — they are deliberately tuned per tenant, and an equality check
    # would report drift on a correctly-tuned stack until operators learned to
    # ignore the report. `stall_recovery_sec` is the worked example: the code
    # default is 2.0 and one client demo's seeder deliberately sets 15,
    # because that tenant's real `ask_agent` takes 7.4-13.1s and a 2s tripwire
    # fires while a healthy call is still in flight.
    #
    # What IS always wrong-if-forgotten is the value that disables the rescue
    # outright — `voice/live/config.py` calls each of these "the rollback
    # lever" in so many words. So the check passes on ANY value except this
    # one, which is reported exactly like an engaged incident lever.
    disabled_value: Any = _UNSPECIFIED
    incident_lever: bool = False
    # Slugs like a kind's literal "default" are not unique across agent
    # kinds the way a "<kind>_default" slug is — set this to pin the profile fetch
    # to one kind instead of trusting `.eq("slug", ...).limit(1)` to pick
    # the right row.
    agent_kind: Optional[str] = None
    # When set and equal to `expected`, an UNSET key is healthy: the code
    # default is in force and the default IS the expected value. Without it,
    # unset stays a warning — "nothing configured this" is worth a look.
    code_default: Any = _UNSPECIFIED
    # When set, the check does not apply at all until this path resolves to
    # something on the profile — i.e. a not-yet-rolled-out parent block (e.g.
    # `voice_config` never configured for a profile) is "ok, not applicable"
    # rather than drift. An EMPTY block counts as unconfigured, which matters
    # post-unification: `voice_config` is `{}` on a stack that has never
    # touched voice, where the pre-unification gate looked for a missing
    # `live` key. Distinct from `code_default`, which handles an unset LEAF
    # under an already-present parent. See a `text_only` row: a
    # stack that hasn't reached ops rollout step 2 has no voice config at
    # all, and that must not turn the drift check red before rollout happens.
    gate_path: Optional[List[str]] = None
    # When set (with `agent_kind`), the profile is selected by
    # (agent_kind, status, environment) instead of by slug — `profile_slug`
    # becomes a display label only. This is how a check targets "whatever
    # profile is ACTIVE for kind X", which is the row the runtime's
    # `load_active()` actually reads config from; a demo's active
    # profile has no repo-known slug, so a slug-keyed check could silently
    # go vacuous against the wrong row.
    status: Optional[str] = None
    # When set, this lever only DOES anything while the named sibling path
    # holds a non-empty value — so reporting it "ok" while the gate is empty
    # states something false: the timer is inert whatever it says.
    #
    # `stall_recovery_topic_terms` is the reason this exists. It defaults to
    # `[]` and gates BOTH residual stall tiers
    # (`stall_recovery_unannounced_sec` AND `stall_recovery_silent_sec`), so
    # an empty list is not "unset, fine" — it means the last two lines of
    # defence against a model that simply stops never arm on this deployment.
    # The finding is downgraded to `info` in that state rather than to `ok`:
    # the tiers are opt-in by design (the engine is unbranded and cannot guess
    # a tenant's vocabulary), so failing every stack that has not opted in
    # would train operators to ignore the report — but saying "ok" about an
    # inert timer is exactly the silent-success this tool exists to prevent.
    gated_by: Optional[List[str]] = None


@dataclass
class ModelConfigCheck:
    """An `agent_models.extra_config` value whose wrong setting is SILENT.

    Resolves the profile `profile_slug` names, follows its `model_main_id` to
    the `agent_models` row, and compares the dotted `path` inside that row's
    `extra_config`. Exists because the config tier digs only `agent_profiles`
    fields, and model rows drift by exactly the same mechanism (seeders are
    insert-only) with exactly the same silence.
    """

    profile_slug: str
    path: List[str]            # inside extra_config, e.g. ["reasoning_effort"]
    expected: Any
    why: str
    incident_lever: bool = False
    # When set, the check applies only while the profile's resolved model row
    # has this `agent_models.provider` — otherwise it reports "ok, not
    # applicable" naming the actual provider. Exists because the knob that
    # controls thinking is provider-specific (`thinking_level` on
    # google_genai rows, `reasoning_effort` on azure_openai rows) while the
    # registry keys checks by PROFILE: without the gate, repointing a
    # profile's model_main_id at the other cloud would make the old check
    # report "unset" forever and the right one not exist.
    provider: Optional[str] = None


@dataclass
class ToolSubsetCheck:
    """A profile's MCP bundle-attachment allowlist vs the seed constant.

    The attachment row (`agent_profile_mcp_servers`, keyed profile x bundle)
    carries `tool_subset` — the allowlist `runtime/tools/_mcp_resolver.
    _bundle_tools` filters bundle members down to, i.e. the profile's ACTUAL
    tool set. It is edited live from /admin, the seeder is insert-if-missing
    like every other, and nothing compared it to the repo until 2026-08-26,
    when a live client demo turned out to be serving 13 of the 15 seeded
    voice tools: `get_transactions` and `get_daily_branch_report` had been
    silently dropped, so a caller asking «آخر معاملاتي» had no tool that
    could answer and fell through to the docs search.

    `loader` is deferred (like PromptCheck's) and must return the seed
    constant itself — imported, never copied, so this check can not drift
    from the list it audits against.
    """

    profile_slug: str
    bundle_name: str           # agent_mcp_servers.name, e.g. "__builtin__:<kind>"
    loader: Callable[[], List[str]]
    why: str
    agent_kind: Optional[str] = None


@dataclass
class InfoCheck:
    """An operator-owned lever: reported for visibility, NEVER failed.

    Some knobs are deliberately tuned per deployment and have no single
    'correct' repo value — flagging them as drift would train operators to
    ignore the report. This tier shows the live value against the code
    default so the lever stays visible, without ever affecting the exit code.
    """

    profile_slug: str
    path: List[str]
    code_default: Any
    why: str
    # Same selection semantics as ConfigCheck.agent_kind / .status.
    agent_kind: Optional[str] = None
    status: Optional[str] = None
    # Other keys this one GATES. When set and this value is empty, the summary
    # names them as inert — the reverse view of ConfigCheck.gated_by, so the
    # gate itself is legible on its own line instead of only as a footnote on
    # the timers it silences.
    gates: List[str] = field(default_factory=list)


@dataclass
class PersonaCheck:
    """Where the duplex voice PERSONA's version pin lives — if it lives anywhere.

    `endpoints/voice_live.py::resolve_persona_prompt` resolves the persona in
    four tiers: the parent's promoted snapshot, the parent's
    `prompt_version_ids[<slug>]`, a LEGACY pin on the specialist (honoured, but
    it warns on every connect), and finally the globally-latest version of the
    slug — ungoverned, where promote and rollback control nothing and a draft
    save is live on the next call.

    The last two are the invisible conditions this tool is for: both WORK.
    Nothing is broken, no caller complains, and until now the only trace was a
    log line at connect time. `profile_slug` names the PARENT — the profile
    that carries `voice_config.persona_prompt_slug` and
    `voice_config.agent_profile_slug`; the specialist is looked up from the
    latter rather than assumed, so the check follows the same wiring the
    endpoint does.
    """

    profile_slug: str
    why: str
    # Same selection semantics as ConfigCheck.agent_kind / .status.
    agent_kind: Optional[str] = None
    status: Optional[str] = None


@dataclass
class Finding:
    severity: str
    kind: str
    target: str
    summary: str
    detail: str = ""
    diff: List[str] = field(default_factory=list)


_CHECK_LISTS = {
    "PROMPT_CHECKS": "prompt_checks",
    "CONFIG_CHECKS": "config_checks",
    "MODEL_CONFIG_CHECKS": "model_config_checks",
    "TOOL_SUBSET_CHECKS": "tool_subset_checks",
    "INFO_CHECKS": "info_checks",
    "PERSONA_CHECKS": "persona_checks",
}


def _checks() -> Any:
    """This module, so a check list resolves through `__getattr__` (and a
    test's monkeypatch of one is seen)."""
    return sys.modules[__name__]


def _plugin_checks(fn: str) -> list:
    """Every plugin's `drift_checks.<fn>()` list, in plugin order. The
    checks name a deployment's own profiles, so the engine ships none."""
    from core.plugins import call_hook, load_hook

    return [c for found in call_hook(load_hook("drift_checks"), fn) for c in found]


def __getattr__(name: str) -> Any:
    # PROMPT_CHECKS, CONFIG_CHECKS, ... read as module constants, loaded on
    # first use (a plugin's check module imports this one for its classes).
    if name in _CHECK_LISTS:
        value = _plugin_checks(_CHECK_LISTS[name])
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Azure neural voice names ("ar-SA-HamedNeural") are per-locale TTS voices for
# the CASCADED stack. On a NATIVE-audio row they override the model's own
# dialect-capable speech via resolve_session_voice — an MSA-trained voice
# reading colloquial spellings, which is the exact ceiling the native row
# exists to escape (2026-08-16 prod accent regression).
_AZURE_NEURAL_VOICE_RE = re.compile(r"^[a-z]{2,3}-[A-Z][A-Za-z]+-[A-Za-z]+Neural$")


def _dig(obj: Any, path: List[str]) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _persona_status(
    client, environment: str, parent: Dict[str, Any], voice: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """`collect_persona_version_status` for a parent row, with its specialist.

    The specialist is looked up from the parent's
    `voice_config.agent_profile_slug` rather than assumed, because that is the
    row `resolve_persona_prompt` is handed and the only reason it is needed
    here is to spot the legacy misplaced pin on it. Scoped to the same
    (agent_kind, environment) as every other fetch in this module.

    Returns None if the shared resolution raises — a display helper failing
    must not take down a drift report that has already collected real
    findings.
    """
    specialist = None
    spec_slug = str(voice.get("agent_profile_slug") or "").strip()
    if spec_slug:
        rows = (
            client.table("agent_profiles")
            .select("profile_id, slug, agent_kind, status, environment, "
                    "display_name, served_version_id, prompt_version_ids, "
                    "voice_config")
            .eq("environment", environment)
            .eq("agent_kind", parent.get("agent_kind"))
            .eq("slug", spec_slug)
            .limit(1)
            .execute()
            .data
            or []
        )
        specialist = rows[0] if rows else None
    try:
        return collect_persona_version_status(
            client, environment=environment,
            parent_row=parent, specialist_row=specialist,
        )
    except Exception:
        return None


def _served_prompt_content(client, profile: Dict[str, Any], key: str) -> Optional[str]:
    """Resolve what the profile ACTUALLY serves.

    Through `prompt_version_ids`, deliberately — not the newest version of the
    prompt. A profile pinned to an old version while newer ones exist is
    exactly the failure this tool was written for, and 'latest' would hide it.
    """
    ids = profile.get("prompt_version_ids") or {}
    if not isinstance(ids, dict):
        return None
    version_id = ids.get(key)
    if not version_id:
        return None
    rows = (
        client.table("agent_prompt_versions")
        .select("content")
        .eq("prompt_version_id", version_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0]["content"] if rows else None


def collect_drift(
    client=None, *, want_diff: bool = False, environment: Optional[str] = None
) -> List[Finding]:
    """Return every disagreement between this checkout and the live project.

    Separated from the printing so an /admin screen or a cron alert can call it
    without scraping stdout.

    ``environment`` names the ``agent_profiles.environment`` rows to audit. It
    is EXPLICIT for a reason: the documented prod-access recipe is a local shell
    with ``AI_SUPABASE_*`` pointed at prod and ``ENVIRONMENT`` unset — and an
    unset ``ENVIRONMENT`` collapses to ``staging`` (see
    ``core.agent_profile._default_environment``). Left implicit, the tool
    deterministically audits staging rows against a prod database, never looks
    at prod's incident levers, and exits 0: a clean report from the one tool
    whose entire purpose is catching silent prod drift. ``None`` still falls
    back to the process default for in-process callers (the /admin screen, the
    MCP tool) where ``ENVIRONMENT`` really does describe the running service —
    but the audited environment is then named in the FIRST finding, so a clean
    report can never be read as "prod is clean" by accident.
    """
    if client is None:
        from core.auth import get_service_client
        client = get_service_client()

    findings: List[Finding] = []
    profiles: Dict[str, Dict[str, Any]] = {}

    # One fetch spec per profile LABEL. A label is normally the slug; a check
    # with `status` set selects by (agent_kind, status) instead and its
    # `profile_slug` is a display label only ("support(active)") — that is how
    # a check targets whatever row `load_active()` actually reads, without
    # hardcoding a slug the repo doesn't own. A slug alone is not always
    # unique either ("default" exists per agent kind); any check that pins an
    # agent_kind narrows the fetch for that label.
    specs: Dict[str, Dict[str, Optional[str]]] = {}
    for c in (*_checks().PROMPT_CHECKS, *_checks().CONFIG_CHECKS, *_checks().MODEL_CONFIG_CHECKS,
              *_checks().TOOL_SUBSET_CHECKS, *_checks().INFO_CHECKS, *_checks().PERSONA_CHECKS):
        spec = specs.setdefault(
            c.profile_slug, {"slug": c.profile_slug, "agent_kind": None, "status": None}
        )
        if getattr(c, "agent_kind", None):
            spec["agent_kind"] = c.agent_kind
        if getattr(c, "status", None):
            spec["status"] = c.status
            spec["slug"] = None

    # Every fetch is environment-scoped, matching the runtime loader: the
    # single AI DB carries one row per environment for a given slug or
    # (kind, status='active'), and `.limit(1)` without the env filter could
    # silently compare the OTHER environment's row.
    if environment:
        env_source = "explicit"
    else:
        from core.agent_profile import _default_environment
        environment = _default_environment()
        env_source = (
            "ENVIRONMENT env var" if os.getenv("ENVIRONMENT")
            else "DEFAULT (ENVIRONMENT unset — an unset value collapses to "
                 "'staging', so this is NOT a prod audit)"
        )

    # `profile_id` / `status` / `served_version_id` / `display_name` are here
    # for the persona tier: `admin._persona_versions` reproduces the loader's
    # snapshot-vs-live branch off exactly those columns, and reusing it rather
    # than re-deriving the tier order is the whole point (a third
    # implementation that can disagree with the resolver is the defect Part A
    # spent a fix wave removing).
    _PROFILE_COLUMNS = (
        "profile_id, slug, agent_kind, status, environment, display_name, "
        "served_version_id, prompt_version_ids, voice_config, memory_config, "
        "model_main_id"
    )

    for label in sorted(specs):
        spec = specs[label]
        query = (
            client.table("agent_profiles")
            .select(_PROFILE_COLUMNS)
            .eq("environment", environment)
        )
        if spec["slug"]:
            query = query.eq("slug", spec["slug"])
        if spec["agent_kind"]:
            query = query.eq("agent_kind", spec["agent_kind"])
        if spec["status"]:
            query = query.eq("status", spec["status"])
        rows = query.limit(1).execute().data or []
        if not rows:
            continue
        row = rows[0]
        # `voice_config` is FLAT since the 2026-08-17 unification, and every
        # voice check below (CONFIG_CHECKS, INFO_CHECKS, and the native-voice
        # scan) reads the flat path. A row still carrying the old
        # `{"live": {...}}` shape is read through the same promotion the
        # writers apply, and SAID SO — because the alternative is the failure
        # this whole tool exists to prevent: a nested block silently resolves
        # nothing at the flat path, every voice check comes back "unset", and
        # the report is clean on exactly the incident levers
        # (barge_in_enabled, answer_watchdog_sec) it was written to catch.
        stored = row.get("voice_config")
        if isinstance(stored, dict) and isinstance(stored.get("live"), dict):
            row["voice_config"] = promote_legacy_voice_config(stored)
            # `label` is a display label under the (agent_kind, status)
            # selection — "support(active)" is not a slug anyone can grep for or
            # open in /admin, so name the matched row's real slug when the two
            # differ. The printer already emits `target`, so no label echo.
            real_slug = row.get("slug")
            matched = (f" (matched row slug {real_slug!r})"
                       if real_slug and real_slug != label else "")
            findings.append(Finding(
                "warning", "config", label,
                "voice_config still carries a legacy 'live' sub-block — "
                "read through the compat promotion for the checks below"
                + matched,
                detail=(
                    "This stack has not run the unify_voice_config migration "
                    "(supabase/migrations/20260817120000_unify_voice_config.sql). "
                    "The runtime still resolves it, but the next save from "
                    "/admin/agents or a seeder run will rewrite the row flat."
                ),
            ))
        profiles[label] = row

    # Absent is not drift: a stack that does not run this client legitimately
    # has no such profile. Say it ONCE per (tier, profile) rather than once per
    # registered check — one client's rows alone would otherwise print five
    # identical "no such profile" lines on a WAJ stack, and a report padded
    # with repetition is a report nobody reads to the end.
    absent_reported: set = set()

    def _absent(kind: str, label: str) -> None:
        if (kind, label) in absent_reported:
            return
        absent_reported.add((kind, label))
        findings.append(Finding(
            "ok", kind, label, "no such profile on this stack (not an error)",
        ))

    for check in _checks().PROMPT_CHECKS:
        profile = profiles.get(check.profile_slug)
        if profile is None:
            _absent("prompt", check.profile_slug)
            continue

        try:
            repo_text = check.loader()
        except Exception as exc:
            findings.append(Finding(
                "warning", "prompt", check.profile_slug,
                f"could not load {check.repo_constant}: {exc}",
            ))
            continue

        live_text = _served_prompt_content(client, profile, check.prompt_key)
        if live_text is None:
            findings.append(Finding(
                "critical", "prompt", check.profile_slug,
                f"serves no {check.prompt_key!r} prompt version at all",
                detail=check.why,
            ))
            continue

        # Validate what's actually SERVED — the same gate
        # admin_mcp/tools_configure.py's publish path runs before a write.
        # Under DB-authoring (prompts change through MCP/API/DB, not a
        # merge — see CLAUDE.md) a served prompt differing from the repo
        # constant is EXPECTED, not a defect: the repo constant is only the
        # seed for a new stack. What is still critical is the modern shape
        # of the original failure this check was built for — a profile
        # PINNED to a bad version, whether that version is stale,
        # hand-edited, or freshly published. `canonical_name` is the
        # dotted `repo_constant`'s attribute name, e.g. "VOICE_PERSONA_
        # PROMPT" — the same name core.prompt_validator's registries key
        # size-budget and bilingual-parity rules by.
        canonical_name = check.repo_constant.rpartition(".")[-1]
        from core.prompt_validator import has_registered_rules, validate_prompt
        problems = validate_prompt(live_text, name=canonical_name)
        errors = [p for p in problems if p.severity == "error"]

        diff: List[str] = []
        if want_diff and live_text != repo_text:
            diff = list(difflib.unified_diff(
                repo_text.splitlines(), live_text.splitlines(),
                fromfile="repo", tofile="live", lineterm="", n=1,
            ))[:80]

        if errors:
            others = "; ".join(e.message for e in errors[1:])
            findings.append(Finding(
                "critical", "prompt", check.profile_slug,
                f"served {check.prompt_key} FAILS validation "
                f"({len(errors)} problem(s)): {errors[0].message}",
                detail=(
                    f"{check.why} A merged change to the repo constant will "
                    f"NOT fix this — the seeder is insert-if-missing and will "
                    f"not update an existing version. Publish a corrected "
                    f"version via prompts(action='publish_from_repo') or the "
                    f"portal instead."
                    + (f" Also: {others}" if others else "")
                ),
                diff=diff,
            ))
            continue

        if live_text == repo_text:
            findings.append(Finding(
                "ok", "prompt", check.profile_slug,
                f"{check.prompt_key} matches {check.repo_constant} "
                f"(and passes validation)",
            ))
            continue

        delta = len(live_text) - len(repo_text)
        if has_registered_rules(canonical_name):
            # The validator actually checked this prompt (size budget and/or
            # bilingual parity are registered for it) and found nothing
            # wrong — divergence from the repo constant is informational,
            # not drift, exactly as DB-authoring intends.
            findings.append(Finding(
                "info", "prompt", check.profile_slug,
                f"served {check.prompt_key} differs from {check.repo_constant} "
                f"({len(live_text)} chars live vs {len(repo_text)} repo, "
                f"{delta:+d}) — informational under DB-authoring: the served "
                f"version passed validation, so this is an EXPECTED "
                f"divergence, not drift.",
                detail=(
                    f"{check.why} Prompts are authored in the DB now "
                    f"(publish_from_repo / the portal); the repo constant is "
                    f"only the seed for a new stack, not the thing a live one "
                    f"must keep matching."
                ),
                diff=diff,
            ))
        else:
            # No size-budget or bilingual-parity rules are registered for
            # this name — the validator ran only its universal ask_agent
            # check. A divergence here is NOT confirmed safe by anything;
            # reporting it as clean would be exactly the "fail wrong" this
            # tool exists to avoid. Warning, not critical: an unregistered
            # prompt MIGHT be a legitimate DB-authored edit, but nothing
            # here can tell — same reasoning as the config tier's "unset
            # without a declared code_default stays a warning" case.
            findings.append(Finding(
                "warning", "prompt", check.profile_slug,
                f"served {check.prompt_key} differs from {check.repo_constant} "
                f"({len(live_text)} chars live vs {len(repo_text)} repo, "
                f"{delta:+d}) — UNCONFIRMED: no size-budget or bilingual-"
                f"parity rules are registered for {canonical_name!r}, so "
                f"nothing here can say whether this divergence is safe.",
                detail=(
                    f"{check.why} Register this prompt in core.prompt_"
                    f"validator's SIZE_BUDGETS / BILINGUAL_RULES to get a "
                    f"real verdict instead of this warning."
                ),
                diff=diff,
            ))

    for check in _checks().CONFIG_CHECKS:
        profile = profiles.get(check.profile_slug)
        if profile is None:
            _absent("config", check.profile_slug)
            continue

        dotted = ".".join(check.path)

        # Empty counts as unconfigured, not as "configured with nothing" — see
        # ConfigCheck.gate_path.
        if check.gate_path is not None and not _dig(profile, check.gate_path):
            findings.append(Finding(
                "ok", "config", f"{check.profile_slug}.{dotted}",
                f"{'.'.join(check.gate_path)} not configured on this stack "
                f"yet — check not applicable",
            ))
            continue

        actual = _dig(profile, check.path)

        # A lever whose gate is empty does nothing, whatever its own value is.
        # Reporting it "ok" would state something false — see
        # ConfigCheck.gated_by.
        inert = ""
        if check.gated_by is not None and not _dig(profile, check.gated_by):
            inert = (f"  [INERT — {'.'.join(check.gated_by[-1:])} is empty, so "
                     f"this tier never arms]")

        # --- "0 disables" mode: any tuned value passes, the off value does not
        if check.disabled_value is not _UNSPECIFIED:
            if actual == check.disabled_value:
                findings.append(Finding(
                    "critical" if check.incident_lever else "warning",
                    "config", f"{check.profile_slug}.{dotted}",
                    f"is {actual!r} — DISABLED"
                    + ("  [INCIDENT LEVER LEFT ENGAGED]"
                       if check.incident_lever else "")
                    + inert,
                    detail=check.why,
                ))
            elif inert:
                # Not drift (the tier is opt-in), but not "ok" either.
                findings.append(Finding(
                    "info", "config", f"{check.profile_slug}.{dotted}",
                    (f"unset — code default {check.code_default!r} applies"
                     if actual is None else f"= {actual!r}") + inert,
                    detail=check.why,
                ))
            elif actual is None:
                findings.append(Finding(
                    "ok", "config", f"{check.profile_slug}.{dotted}",
                    f"unset — code default {check.code_default!r} applies "
                    f"(armed; {check.disabled_value!r} would disable it)",
                ))
            else:
                findings.append(Finding(
                    "ok", "config", f"{check.profile_slug}.{dotted}",
                    f"= {actual!r} (armed; {check.disabled_value!r} would "
                    f"disable it)",
                ))
            continue

        if actual == check.expected:
            findings.append(Finding(
                "ok", "config", f"{check.profile_slug}.{dotted}",
                f"= {actual!r}",
            ))
            continue

        # An unset value is not the same as a wrong one: it means the default
        # is in force. When the check declares that default and it IS the
        # expected value, unset is healthy — this is what a fresh environment
        # looks like now that the seeders no longer write barge_in_enabled.
        # Without a declared default, unset stays a warning worth a look.
        if actual is None:
            if check.code_default is not _UNSPECIFIED and check.code_default == check.expected:
                findings.append(Finding(
                    "ok", "config", f"{check.profile_slug}.{dotted}",
                    f"unset — code default {check.code_default!r} applies, "
                    f"which is the expected value",
                ))
            else:
                findings.append(Finding(
                    "warning", "config", f"{check.profile_slug}.{dotted}",
                    f"unset — the code default applies, expected {check.expected!r}",
                    detail=check.why,
                ))
            continue

        findings.append(Finding(
            "critical" if check.incident_lever else "warning",
            "config", f"{check.profile_slug}.{dotted}",
            f"is {actual!r}, expected {check.expected!r}"
            + ("  [INCIDENT LEVER LEFT ENGAGED]" if check.incident_lever else ""),
            detail=check.why,
        ))

    # --- model tier: the agent_models row a profile's model_main_id names ---
    model_rows: Dict[str, Optional[Dict[str, Any]]] = {}

    def _model_row(model_id: str) -> Optional[Dict[str, Any]]:
        if model_id not in model_rows:
            rows = (
                client.table("agent_models")
                .select("model_id, name, model_string, provider, extra_config")
                .eq("model_id", model_id)
                .limit(1)
                .execute()
                .data
                or []
            )
            model_rows[model_id] = rows[0] if rows else None
        return model_rows[model_id]

    for check in _checks().MODEL_CONFIG_CHECKS:
        profile = profiles.get(check.profile_slug)
        dotted = ".".join(check.path)
        target = f"{check.profile_slug} -> model.extra_config.{dotted}"
        if profile is None:
            _absent("model", check.profile_slug)
            continue

        model_id = profile.get("model_main_id")
        if not model_id:
            findings.append(Finding(
                "warning", "model", target,
                "profile has no model_main_id — cannot verify the model row",
                detail=check.why,
            ))
            continue

        row = _model_row(model_id)
        if row is None:
            findings.append(Finding(
                "critical", "model", target,
                f"model_main_id {model_id} has no agent_models row",
                detail=check.why,
            ))
            continue

        label = row.get("name") or row.get("model_string") or model_id
        if check.provider:
            row_provider = (row.get("provider") or "").strip()
            if not row_provider:
                # An empty provider makes EVERY gated check stand aside, so
                # the knob would go unaudited under a green report — the
                # exact silent-success this tool exists to prevent.
                findings.append(Finding(
                    "warning", "model", target,
                    f"model {label!r} has NO provider set — cannot tell "
                    f"whether this provider-gated check applies, so the "
                    f"knob is unaudited until the row names one",
                    detail=check.why,
                ))
                continue
            if row_provider != check.provider:
                findings.append(Finding(
                    "ok", "model", target,
                    f"n/a — model {label!r} is provider "
                    f"{row_provider!r}, check gated on {check.provider!r}",
                ))
                continue
        actual = _dig(row.get("extra_config") or {}, check.path)
        if actual == check.expected:
            findings.append(Finding(
                "ok", "model", target,
                f"= {actual!r}  (model {label!r})",
            ))
            continue

        if actual is None:
            findings.append(Finding(
                "warning", "model", target,
                f"unset on model {label!r} — expected {check.expected!r}",
                detail=check.why,
            ))
            continue

        findings.append(Finding(
            "critical" if check.incident_lever else "warning",
            "model", target,
            f"is {actual!r} on model {label!r}, expected {check.expected!r}"
            + ("  [INCIDENT LEVER LEFT ENGAGED]" if check.incident_lever else ""),
            detail=check.why,
        ))

    # --- tools tier: the MCP attachment allowlist the profile actually serves ---
    # The attachment's tool_subset is what runtime/tools/_mcp_resolver hands
    # the model as its callable tools, and it drifts by the same mechanism as
    # everything else here (seeders are insert-if-missing, /admin edits it
    # live). Compared against the SEED CONSTANT via the check's deferred
    # loader — the constant is imported, never copied.
    for check in _checks().TOOL_SUBSET_CHECKS:
        profile = profiles.get(check.profile_slug)
        target = f"{check.profile_slug} -> {check.bundle_name}.tool_subset"
        if profile is None:
            _absent("tools", check.profile_slug)
            continue

        try:
            expected = list(check.loader())
        except Exception as exc:
            findings.append(Finding(
                "warning", "tools", target,
                f"could not load the seed constant: {exc}",
            ))
            continue

        bundle = (
            client.table("agent_mcp_servers").select("mcp_server_id")
            .eq("name", check.bundle_name).limit(1).execute().data or []
        )
        if not bundle:
            findings.append(Finding(
                "warning", "tools", target,
                f"no {check.bundle_name!r} bundle row on this stack — cannot "
                f"verify the attachment (and the boot sweep will create one "
                f"with a DEFAULT subset when the bundle appears)",
                detail=check.why,
            ))
            continue

        attach = (
            client.table("agent_profile_mcp_servers").select("tool_subset")
            .eq("profile_id", profile.get("profile_id"))
            .eq("mcp_server_id", bundle[0]["mcp_server_id"])
            .limit(1).execute().data or []
        )
        if not attach:
            findings.append(Finding(
                "warning", "tools", target,
                "profile has no attachment to the bundle — it resolves no "
                "tools from it at all",
                detail=check.why,
            ))
            continue

        actual = list(attach[0].get("tool_subset") or [])
        if not actual:
            findings.append(Finding(
                "warning", "tools", target,
                "tool_subset is empty/None — 'expose every bundle member', "
                "which hands a phone caller render_chart and export_csv, "
                "tools a voice call cannot use",
                detail=check.why,
            ))
            continue

        # Membership, not order: the resolver treats the subset as an
        # allowlist, so a reordered-but-equal list is not drift.
        missing = [t for t in expected if t not in actual]
        extra = [t for t in actual if t not in expected]
        if not missing and not extra:
            findings.append(Finding(
                "ok", "tools", target,
                f"= the seeded allowlist ({len(expected)} tools)",
            ))
            continue

        parts = []
        if missing:
            parts.append(f"missing {missing}")
        if extra:
            parts.append(f"extra {extra}")
        findings.append(Finding(
            "warning", "tools", target,
            "differs from the seeded allowlist: "
            + "; ".join(parts),
            detail=(
                f"{check.why}. Seeders are insert-if-missing, so merging a "
                f"seed edit will NOT repair a live attachment — update the "
                f"agent_profile_mcp_servers row via /admin or the admin MCP."
            ),
        ))

    # --- voice tier: a native-audio model overridden by Azure voice names ---
    # Cross-row by nature (profile voice_config x agent_models row), so it is
    # its own scan rather than a ConfigCheck: for every fetched profile with
    # duplex voice configured, if the realtime row is NATIVE, no Azure neural
    # voice name may be mapped over it — resolve_session_voice would hand the
    # session an MSA-trained TTS voice and the model's own dialect speech
    # (the entire point of paying for the native row) never plays.
    #
    # Reads the FLAT block (2026-08-17 unification), and reads it off
    # `profiles`, which is post-promotion: an unmigrated `{"live": {...}}` row
    # was already flattened by the compat block above, so this scan covers it
    # too. That promotion is also what keeps the check honest — it stashes the
    # pre-unification TURN-BASED keys under `_retired_turn_based` instead of
    # merging them up, so a turn-based `voice_id` (also an Azure neural name,
    # and correct for the TTS path) can never be mistaken for a duplex
    # override here.
    for label in sorted(profiles):
        profile = profiles[label]
        live = _dig(profile, ["voice_config"]) or {}
        model_id = live.get("model_id")
        if not model_id:
            continue
        row = _model_row(model_id)
        if row is None:
            findings.append(Finding(
                "critical", "voice", f"{label}.voice_config.model_id",
                f"names agent_models row {model_id!r} which does not exist",
            ))
            continue
        voice_type = str((row.get("extra_config") or {}).get("voice_type") or "")
        if voice_type != "native":
            continue
        overrides: Dict[str, Any] = {}
        if live.get("voice_id"):
            overrides["voice_id"] = live["voice_id"]
        vbl = live.get("voice_by_language")
        if isinstance(vbl, dict):
            for lang, name in vbl.items():
                if name:
                    overrides[f"voice_by_language.{lang}"] = name
        azure_named = {
            k: v for k, v in overrides.items()
            if _AZURE_NEURAL_VOICE_RE.match(str(v))
        }
        label_model = row.get("name") or row.get("model_string") or model_id
        if azure_named:
            listed = ", ".join(f"{k}={v!r}" for k, v in sorted(azure_named.items()))
            findings.append(Finding(
                "critical", "voice", f"{label}.voice_config",
                f"NATIVE-audio model {label_model!r} is overridden by Azure "
                f"neural voice name(s): {listed}",
                detail=(
                    "an MSA-trained TTS voice replaces the native model's own "
                    "dialect speech (the 2026-08-16 accent regression). "
                    "Clear voice_by_language / voice_id, or point model_id "
                    "back at a cascaded row if the Azure voices are intended."
                ),
            ))
        else:
            findings.append(Finding(
                "ok", "voice", f"{label}.voice_config",
                f"native model {label_model!r} keeps its own voice "
                f"(no Azure neural override)",
            ))

    # --- voice tier: Gemini start-of-speech sensitivity must stay default ---
    # Cross-row like the native-voice scan above (profile voice_config x the
    # realtime agent_models row). START_SENSITIVITY_LOW was hand-tuned onto
    # the demo's Gemini row on 2026-08-19 and the operator-facing symptom was
    # a whole complaint class, not a knob: "slower, less interactive, keeps
    # repeating itself" — the caller's speech registered late and soft
    # interjections not at all, so answers got talked over, cut, re-asked and
    # re-answered. Removed 2026-08-27; interactivity returned immediately
    # (the silence window is a separate, measured knob and stays fair game).
    # The decided state is: provider-default start sensitivity. Deviating
    # again must be a deliberate, measured act — hence a warning, not a FAIL.
    for label in sorted(profiles):
        profile = profiles[label]
        live = _dig(profile, ["voice_config"]) or {}
        model_id = live.get("model_id")
        if not model_id:
            continue
        row = _model_row(model_id)
        if row is None:
            continue  # the native-voice scan above already flagged this
        if (row.get("provider") or "") != "google_genai":
            continue
        td = (row.get("extra_config") or {}).get("turn_detection")
        sens = td.get("start_of_speech_sensitivity") if isinstance(td, dict) else None
        label_model = row.get("name") or row.get("model_string") or model_id
        if sens:
            findings.append(Finding(
                "warning", "voice",
                f"{label} -> model.extra_config.turn_detection.start_of_speech_sensitivity",
                f"set to {sens!r} on Gemini row {label_model!r} — the decided "
                f"state is provider default",
                detail=(
                    "live 2026-08-27: START_SENSITIVITY_LOW made the caller's "
                    "speech register late and soft interjections not at all — "
                    "felt as 'slower, less interactive, repeats itself'. "
                    "Removing it restored the early-Gemini interactivity. If "
                    "this is set again deliberately (noisy room), record the "
                    "measurement and update this check's expectation."
                ),
            ))
        else:
            findings.append(Finding(
                "ok", "voice",
                f"{label} -> model.extra_config.turn_detection.start_of_speech_sensitivity",
                f"unset on Gemini row {label_model!r} — provider-default "
                f"ears (the 2026-08-27 decided state)",
            ))

    # --- persona tier: WHERE the voice persona's version pin lives ----------
    # Cross-row (parent x specialist x agent_prompt_versions) and tier-ordered,
    # so it is its own scan rather than a ConfigCheck. Two states resolve fine
    # and are governance findings anyway, which is why nothing had ever
    # reported them: a legacy pin on the SPECIALIST (honoured, warns on every
    # connect, and a parent-side save cannot displace it) and NO pin at all
    # (globally-latest — promote and rollback control nothing).
    #
    # The tier order is NOT re-derived here. `admin/_persona_versions.py`
    # already mirrors `endpoints/voice_live.py::resolve_persona_prompt` as
    # pure functions for the portal's served-vs-latest banner, and a third
    # implementation free to disagree with the resolver is precisely the
    # defect class this whole spec exists to remove.
    for check in _checks().PERSONA_CHECKS:
        parent = profiles.get(check.profile_slug)
        target = f"{check.profile_slug}.voice_config.persona_prompt_slug"
        if parent is None:
            _absent("persona", check.profile_slug)
            continue

        voice = _dig(parent, ["voice_config"]) or {}
        if not voice:
            findings.append(Finding(
                "ok", "persona", target,
                "voice_config not configured on this stack yet — "
                "check not applicable",
            ))
            continue

        slug = str(voice.get("persona_prompt_slug") or "").strip()
        if not slug:
            # `resolve_persona_prompt` raises LookupError without a slug, so
            # this is only quiet while duplex is off.
            findings.append(Finding(
                "warning" if voice.get("enabled") else "ok",
                "persona", target,
                "duplex voice is enabled but names no persona_prompt_slug — "
                "resolve_persona_prompt raises and the connect fails"
                if voice.get("enabled")
                else "duplex voice not enabled and no persona configured — "
                     "check not applicable",
                detail=check.why,
            ))
            continue

        status = _persona_status(client, environment, parent, voice)
        if status is None:
            findings.append(Finding(
                "warning", "persona", f"{check.profile_slug}.{slug}",
                "could not resolve which persona version this profile serves",
                detail=check.why,
            ))
            continue

        tier = status["tier"]
        served_num = (status.get("served") or {}).get("version_number")
        spec_slug = (status.get("specialist") or {}).get("slug")
        if status["governed"]:
            findings.append(Finding(
                "ok", "persona", f"{check.profile_slug}.{slug}",
                f"pinned on the parent (tier {tier}, version {served_num})",
            ))
        elif tier == TIER_SPECIALIST_PIN:
            findings.append(Finding(
                "warning", "persona", f"{check.profile_slug}.{slug}",
                f"persona pin lives on the SPECIALIST {spec_slug!r}, not on "
                f"this parent (version {served_num}) — honoured, but it warns "
                f"on every connect",
                detail=(
                    f"{check.why}. The parent owns the persona since "
                    f"20260817121000: it names the slug, holds every other "
                    f"edge setting, and one promote of it freezes edge config "
                    f"+ realtime model row + persona version together. Fix by "
                    f"repointing prompt_version_ids[{slug!r}] on "
                    f"{check.profile_slug} — a persona save made while VIEWING "
                    f"the parent does exactly that."
                ),
            ))
        else:
            findings.append(Finding(
                "warning", "persona", f"{check.profile_slug}.{slug}",
                f"persona is pinned NOWHERE — it resolves to the "
                f"globally-latest version (version {served_num}), so promote "
                f"and rollback control nothing and a draft save is live on the "
                f"next call",
                detail=(
                    f"{check.why}. Fix by pinning "
                    f"prompt_version_ids[{slug!r}] on {check.profile_slug} "
                    f"(the Agents screen's persona editor writes it) and "
                    f"promoting the profile."
                ),
            ))

    # --- info tier: operator-owned levers, reported but never failed ---
    for check in _checks().INFO_CHECKS:
        profile = profiles.get(check.profile_slug)
        if profile is None:
            continue  # nothing to show, and absence is not worth a line
        dotted = ".".join(check.path)
        actual = _dig(profile, check.path)
        if check.gates:
            # A gate is reported by what it ENABLES, not by its own value: the
            # number of terms is uninteresting, whether two tiers of stall
            # recovery can arm at all is not.
            gated = ", ".join(check.gates)
            if not actual:
                summary = (
                    f"EMPTY — {gated} never arm on this stack, whatever their "
                    f"timers say (opt-in by design, not drift)"
                )
            else:
                n = len(actual) if isinstance(actual, (list, tuple, dict)) else 1
                summary = f"{n} term(s) configured — {gated} can arm"
        elif actual is None:
            summary = f"unset — code default {check.code_default!r} applies"
        elif actual == check.code_default:
            summary = f"= {actual!r} (the code default)"
        else:
            summary = (
                f"= {actual!r} (code default {check.code_default!r}) — "
                f"operator-owned, informational only"
            )
        findings.append(Finding(
            "info", "config", f"{check.profile_slug}.{dotted}",
            summary, detail=check.why,
        ))

    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.kind, f.target))
    # Inserted AFTER the sort, so it is finding #0 no matter its severity: a
    # report that does not say what it audited is a report whose "no drift" line
    # can be read as being about prod when it is about staging. Every consumer
    # (stdout, /admin/drift, the MCP tool) gets it for free.
    findings.insert(0, Finding(
        "info", "scope", "environment",
        f"audited environment: {environment} (from {env_source})",
        detail=(
            "Findings below describe agent_profiles rows with "
            f"environment='{environment}' ONLY. The AI project is a single DB "
            "holding every environment's rows, so a clean report here says "
            f"nothing about any environment other than '{environment}'. Pass "
            "--env (CLI) or environment= (collect_drift) to audit another one."
        ),
    ))
    return findings


# A bare CLI invocation REFUSES to guess. The documented prod-access recipe is
# a local shell with AI_SUPABASE_* pointed at prod and ENVIRONMENT unset, and an
# unset ENVIRONMENT collapses to 'staging' — so guessing means auditing staging
# rows against the prod database, never reading prod's incident levers, and
# exiting 0. A clean report from the tool whose whole purpose is catching silent
# prod drift is worse than no report.
_NO_ENV_MSG = (
    "refusing to guess which environment to audit.\n"
    "\n"
    "  An unset ENVIRONMENT collapses to 'staging' (core.agent_profile."
    "_default_environment), and the documented prod-access recipe is a local "
    "shell with AI_SUPABASE_* pointed at prod and ENVIRONMENT unset — so a "
    "guess here audits STAGING rows in the prod DB and exits 0.\n"
    "\n"
    "  Pass one:  --env prod   |   --env staging\n"
    "  (or export ENVIRONMENT=... if this shell already describes a service)"
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Report repo <-> live-DB drift.")
    ap.add_argument("--quiet", action="store_true", help="exit code only")
    ap.add_argument("--diff", action="store_true", help="show prompt diffs")
    ap.add_argument(
        "--env", choices=("staging", "prod"), default=None,
        help=("which agent_profiles.environment rows to audit. REQUIRED unless "
              "ENVIRONMENT is exported — see the note in this file about why a "
              "bare invocation refuses to guess."),
    )
    args = ap.parse_args()

    if not args.env and not os.getenv("ENVIRONMENT"):
        print(f"drift check: {_NO_ENV_MSG}", file=sys.stderr)
        return 2

    try:
        findings = collect_drift(want_diff=args.diff, environment=args.env)
    except Exception as exc:
        print(f"drift check could not run: {exc}", file=sys.stderr)
        return 2

    drifted = [f for f in findings if f.severity in DRIFT_SEVERITIES]
    audited = next((f.summary for f in findings
                    if f.kind == "scope" and f.target == "environment"), None)

    if not args.quiet:
        # Banner FIRST, before any finding, so the audited environment is read
        # before the verdict rather than after it.
        if audited:
            print(f"== {audited} ==\n")
        for f in findings:
            mark = {"critical": "FAIL", "warning": "WARN",
                    "info": "INFO", "ok": " ok "}[f.severity]
            print(f"[{mark}] {f.kind:6} {f.target}: {f.summary}")
            if f.detail and f.severity in DRIFT_SEVERITIES:
                print(f"         why: {f.detail}")
            for line in f.diff:
                print(f"         | {line}")
        print()
        if drifted:
            print(f"{len(drifted)} drift finding(s). The seeders are "
                  f"insert-if-missing, so merging alone will NOT fix a prompt "
                  f"or config finding — for a `prompt` finding, publish a "
                  f"validated version via prompts(action='publish_from_repo') "
                  f"or the portal (never hand-write a version row); for "
                  f"anything else, push the fix from /admin or update the "
                  f"row directly.")
        else:
            print("no drift: this stack matches the checkout.")
        # …and repeated with the verdict: "no drift" alone is the line that gets
        # quoted, and it means nothing without the environment it is about.
        if audited:
            print(f"({audited})")

    return 1 if drifted else 0


if __name__ == "__main__":
    raise SystemExit(main())
