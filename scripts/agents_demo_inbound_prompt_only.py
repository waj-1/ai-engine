"""One-off: switch the AI-agents demo portal's INBOUND agents (support,
inquiries) to prompt-only on an already-seeded stack.

`admin/seed_agents_demo.py` seeds new stacks prompt-only, but it is
insert-only, so the rows staging and prod were seeded with on 2026-10-07 keep
the delegated, tool-backed setup. This script converges those rows — and only
those rows — for ONE environment:

  1. publishes the repo's prompt-only `system` brain and `voice_persona` for
     each inbound kind as a NEW agent_prompt_versions row (the old versions
     stay — rollback is repointing the pins), validated by
     core.prompt_validator, and pins the profile to them;
  2. updates the profile's voice_config: `prefetched_context: true` and
     `persona_prompt_slug: voice_persona`; adds the endpoint-side barge-in keys
     the pre-fetched mode was tuned on (`local_barge_in_sec`, …) ONLY where the
     row has none, and leaves every other key — `model_id`, `barge_in_enabled`,
     greeting, caps — exactly as it is;
  3. detaches the profile's tools: `tool_ids = []` and its tool-bundle
     attachments (agent_profile_mcp_servers) removed.

Idempotent: a second run finds nothing to do. `--dry-run` prints the plan and
writes nothing. The rows it reads before changing anything are printed as JSON
first, so the previous pins / voice_config / attachments are on record for a
rollback.

Point AI_SUPABASE_URL / AI_SUPABASE_SERVICE_KEY at the target AI project.
A prod profile serves its promoted snapshot, so prod also needs a self-promote
of each profile afterwards (the script prints the call).

    python -m scripts.agents_demo_inbound_prompt_only --env staging --dry-run
    python -m scripts.agents_demo_inbound_prompt_only --env staging
    python -m scripts.agents_demo_inbound_prompt_only --env production
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from typing import Any, Dict, List, Optional

NOTES = "agents demo: inbound prompt-only (scripts/agents_demo_inbound_prompt_only.py)"
INBOUND_KEYS = ("support", "inquiries")
PERSONA_SLUG = "voice_persona"
# The realtime row the demo's voice runs on in staging and prod (Gemini 3.1
# Flash Live). Never changed here — reported when a profile points elsewhere.
EXPECTED_REALTIME_MODEL_ID = "1a67d252-423b-419a-bf80-368096d979b0"


def normalize_env(value: Optional[str]) -> str:
    v = (value or "").strip().lower()
    if v in ("production", "prod"):
        return "prod"
    if v in ("staging", "stage"):
        return "staging"
    raise SystemExit(f"--env must be staging or production (got {value!r})")


def _flat_voice(voice: Any) -> Dict[str, Any]:
    """The flat voice_config block (a legacy {"live": {...}} row is promoted,
    exactly as every writer of voice_config does)."""
    block = dict(voice or {}) if isinstance(voice, dict) else {}
    nested = block.get("live")
    return dict(nested) if isinstance(nested, dict) else block


def target_voice_config(current: Any) -> Dict[str, Any]:
    from admin.seed_agents_demo import DIRECT_MODE_VOICE_KEYS

    cfg = _flat_voice(current)
    cfg["prefetched_context"] = True
    cfg["persona_prompt_slug"] = PERSONA_SLUG
    for key, value in DIRECT_MODE_VOICE_KEYS.items():
        if key != "prefetched_context":
            cfg.setdefault(key, value)
    return cfg


def _prompt_targets(kind: str) -> Dict[str, str]:
    from runtime.prompts.agents_demo import PERSONA_SLUG as SEED_SLUG, prompt_specs

    out = {slug: content for k, slug, _d, content in prompt_specs() if k == kind}
    assert SEED_SLUG == PERSONA_SLUG
    return out


def _validate(content: str, label: str) -> None:
    from core.prompt_validator import has_errors, validate_prompt

    problems = validate_prompt(content, name=None)
    if has_errors(problems):
        raise SystemExit(f"{label}: the repo prompt fails validation: {problems}")


def plan_profile(client, env: str, agent_key: str) -> Dict[str, Any]:
    """Read one inbound profile and work out what has to change."""
    from runtime.tools.agents_demo.data import AGENTS

    kind = AGENTS[agent_key].kind
    slug = f"{kind}_default"
    rows = (client.table("agent_profiles").select("*").eq("agent_kind", kind).eq("slug", slug)
            .eq("environment", env).limit(1).execute().data or [])
    if not rows:
        return {"agent": agent_key, "kind": kind, "missing": True}
    prof = rows[0]
    pins = dict(prof.get("prompt_version_ids") or {})
    prompts: Dict[str, Dict[str, Any]] = {}
    for p_slug, content in _prompt_targets(kind).items():
        _validate(content, f"{kind}/{p_slug}")
        prompt_rows = (client.table("agent_prompts").select("prompt_id").eq("agent_kind", kind)
                       .eq("slug", p_slug).limit(1).execute().data or [])
        prompt_id = prompt_rows[0]["prompt_id"] if prompt_rows else None
        versions = (client.table("agent_prompt_versions").select("prompt_version_id, version_number, content")
                    .eq("prompt_id", prompt_id).order("version_number", desc=True).execute().data or []
                    ) if prompt_id else []
        same = next((v for v in versions if v.get("content") == content), None)
        prompts[p_slug] = {
            "prompt_id": prompt_id,
            "pinned": pins.get(p_slug),
            "existing_version_id": same["prompt_version_id"] if same else None,
            "next_version_number": (max((v.get("version_number") or 0) for v in versions) + 1) if versions else 1,
            "content": content,
        }
    attachments = (client.table("agent_profile_mcp_servers").select("*")
                   .eq("profile_id", prof["profile_id"]).execute().data or [])
    voice_now = prof.get("voice_config")
    voice_new = target_voice_config(voice_now)
    return {
        "agent": agent_key, "kind": kind, "missing": False, "profile_id": prof["profile_id"],
        "before": {"prompt_version_ids": pins, "voice_config": voice_now, "tool_ids": prof.get("tool_ids") or [],
                   "mcp_attachments": attachments},
        "prompts": prompts,
        "voice_config": voice_new,
        "voice_changes": voice_now != voice_new,
        "tool_changes": bool(prof.get("tool_ids")) or bool(attachments),
        "model_id": _flat_voice(voice_now).get("model_id"),
    }


def _changes(p: Dict[str, Any]) -> List[str]:
    out = []
    for slug, info in p["prompts"].items():
        if info["existing_version_id"] is None:
            out.append(f"publish {p['kind']}/{slug} v{info['next_version_number']} and pin it")
        elif info["pinned"] != info["existing_version_id"]:
            out.append(f"pin {p['kind']}/{slug} to the existing prompt-only version "
                       f"{info['existing_version_id']}")
    if p["voice_changes"]:
        before = _flat_voice(p["before"]["voice_config"])
        diff = {k: v for k, v in p["voice_config"].items() if before.get(k) != v}
        out.append(f"voice_config: set {json.dumps(diff, ensure_ascii=False)}")
    if p["tool_changes"]:
        out.append(f"detach tools: tool_ids {len(p['before']['tool_ids'])} → 0, "
                   f"{len(p['before']['mcp_attachments'])} bundle attachment(s) removed")
    return out


def apply_profile(client, p: Dict[str, Any]) -> None:
    pins = dict(p["before"]["prompt_version_ids"])
    for slug, info in p["prompts"].items():
        vid = info["existing_version_id"]
        if vid is None:
            prompt_id = info["prompt_id"]
            if prompt_id is None:
                prompt_id = client.table("agent_prompts").insert({
                    "agent_kind": p["kind"], "slug": slug,
                    "description": f"{p['kind']} {slug} (agents demo, prompt-only)",
                }).execute().data[0]["prompt_id"]
            vid = client.table("agent_prompt_versions").insert({
                "prompt_id": prompt_id, "version_number": info["next_version_number"],
                "content": info["content"], "notes": NOTES,
            }).execute().data[0]["prompt_version_id"]
        pins[slug] = vid
    update: Dict[str, Any] = {}
    if pins != p["before"]["prompt_version_ids"]:
        update["prompt_version_ids"] = pins
    if p["voice_changes"]:
        update["voice_config"] = p["voice_config"]
    if p["before"]["tool_ids"]:
        update["tool_ids"] = []
    if update:
        client.table("agent_profiles").update(update).eq("profile_id", p["profile_id"]).execute()
    for att in p["before"]["mcp_attachments"]:
        client.table("agent_profile_mcp_servers").delete().eq("profile_id", p["profile_id"]) \
            .eq("mcp_server_id", att["mcp_server_id"]).execute()


def run(client, env: str, *, dry_run: bool, out=print) -> Dict[str, Any]:
    """Converge both inbound profiles of `env`. Returns {agent: [changes]}."""
    plans = [plan_profile(client, env, key) for key in INBOUND_KEYS]
    out("# Before (for rollback):")
    out(json.dumps({p["agent"]: ({"profile_id": p["profile_id"], **p["before"]} if not p["missing"] else None)
                    for p in plans}, ensure_ascii=False, indent=1, default=str))
    report: Dict[str, Any] = {}
    for p in plans:
        if p["missing"]:
            out(f"[{env}] {p['kind']}: no {p['kind']}_default profile — nothing to do (the seeder creates it "
                "prompt-only on the next boot)")
            report[p["agent"]] = None
            continue
        changes = _changes(p)
        report[p["agent"]] = changes
        if p["model_id"] != EXPECTED_REALTIME_MODEL_ID:
            out(f"[{env}] {p['kind']}: NOTE voice model_id is {p['model_id']} (expected "
                f"{EXPECTED_REALTIME_MODEL_ID}) — left unchanged")
        if not changes:
            out(f"[{env}] {p['kind']} ({p['profile_id']}): already prompt-only — nothing to do")
            continue
        for line in changes:
            out(f"[{env}] {p['kind']} ({p['profile_id']}): {'WOULD ' if dry_run else ''}{line}")
        if not dry_run:
            apply_profile(client, copy.deepcopy(p))
    if env == "prod" and any(report.values()) and not dry_run:
        ids = [p["profile_id"] for p in plans if not p["missing"]]
        out("\nProd serves promoted snapshots — self-promote each profile now:")
        for pid in ids:
            out(f'  promote(profile_id="{pid}", target_env="prod", force=False, ...)')
    return report


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env", default=os.getenv("ENVIRONMENT"),
                    help="staging | production (default: $ENVIRONMENT)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    args = ap.parse_args(argv)
    env = normalize_env(args.env)
    from core.auth import get_service_client

    run(get_service_client(), env, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
