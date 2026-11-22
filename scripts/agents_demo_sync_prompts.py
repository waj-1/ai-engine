"""Converge the AI-agents demo profiles' prompts and greeting with the repo.

`admin/seed_agents_demo.py` is insert-only, so a prompt edit in
runtime/prompts/agents_demo.py never reaches a stack that was already seeded.
For ONE environment and all four demo agents this script:

  1. publishes each repo prompt (`system` brain and `voice_persona`) whose
     content differs from the version the profile pins, as a NEW
     agent_prompt_versions row (validated by core.prompt_validator; old
     versions stay, so rollback is repointing the pins), and pins it — or pins
     an existing version that already has the repo content;
  2. sets `voice_config.greeting_trigger` to the repo's GREETING_TRIGGERS entry
     when it differs. No other voice_config key, tool or model is touched.

Idempotent; `--dry-run` writes nothing. The pins and greeting it is about to
replace are printed as JSON first, for rollback. Prod serves promoted
snapshots, so prod also needs a self-promote of each changed profile (the
script prints the calls).

    python -m scripts.agents_demo_sync_prompts --env staging --dry-run
    python -m scripts.agents_demo_sync_prompts --env production
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from typing import Any, Dict, List, Optional

from scripts.agents_demo_inbound_prompt_only import _validate, normalize_env

NOTES = "agents demo: prompts synced from the repo (scripts/agents_demo_sync_prompts.py)"


def _repo_prompts(kind: str) -> Dict[str, str]:
    from runtime.prompts.agents_demo import prompt_specs

    return {slug: content for k, slug, _d, content in prompt_specs() if k == kind}


def plan_profile(client, env: str, agent_key: str) -> Dict[str, Any]:
    from runtime.prompts.agents_demo import GREETING_TRIGGERS
    from runtime.tools.agents_demo.data import AGENTS

    kind = AGENTS[agent_key].kind
    rows = (client.table("agent_profiles").select("profile_id, prompt_version_ids, voice_config")
            .eq("agent_kind", kind).eq("slug", f"{kind}_default").eq("environment", env)
            .limit(1).execute().data or [])
    if not rows:
        return {"agent": agent_key, "kind": kind, "missing": True}
    prof = rows[0]
    pins = dict(prof.get("prompt_version_ids") or {})
    prompts: Dict[str, Dict[str, Any]] = {}
    for slug, content in _repo_prompts(kind).items():
        _validate(content, f"{kind}/{slug}")
        prompt_rows = (client.table("agent_prompts").select("prompt_id").eq("agent_kind", kind)
                       .eq("slug", slug).limit(1).execute().data or [])
        prompt_id = prompt_rows[0]["prompt_id"] if prompt_rows else None
        versions = (client.table("agent_prompt_versions").select("prompt_version_id, version_number, content")
                    .eq("prompt_id", prompt_id).order("version_number", desc=True).execute().data or []
                    ) if prompt_id else []
        pinned = next((v for v in versions if v["prompt_version_id"] == pins.get(slug)), None)
        if pinned and pinned.get("content") == content:
            continue  # already serving the repo text
        same = next((v for v in versions if v.get("content") == content), None)
        prompts[slug] = {
            "prompt_id": prompt_id,
            "existing_version_id": same["prompt_version_id"] if same else None,
            "next_version_number": (max((v.get("version_number") or 0) for v in versions) + 1) if versions else 1,
            "content": content,
        }
    voice = dict(prof.get("voice_config") or {})
    greeting = GREETING_TRIGGERS[agent_key]
    return {
        "agent": agent_key, "kind": kind, "missing": False, "profile_id": prof["profile_id"],
        "before": {"prompt_version_ids": pins, "greeting_trigger": voice.get("greeting_trigger")},
        "voice_config": voice,
        "prompts": prompts,
        "greeting": greeting if voice.get("greeting_trigger") != greeting else None,
    }


def _changes(p: Dict[str, Any]) -> List[str]:
    out = []
    for slug, info in p["prompts"].items():
        if info["existing_version_id"]:
            out.append(f"pin {p['kind']}/{slug} to existing version {info['existing_version_id']}")
        else:
            out.append(f"publish {p['kind']}/{slug} v{info['next_version_number']} and pin it")
    if p["greeting"]:
        out.append("voice_config.greeting_trigger: update to the repo text")
    return out


def apply_profile(client, p: Dict[str, Any]) -> None:
    pins = dict(p["before"]["prompt_version_ids"])
    for slug, info in p["prompts"].items():
        vid = info["existing_version_id"]
        if vid is None:
            prompt_id = info["prompt_id"]
            if prompt_id is None:
                prompt_id = client.table("agent_prompts").insert({
                    "agent_kind": p["kind"], "slug": slug, "description": f"{p['kind']} {slug} (agents demo)",
                }).execute().data[0]["prompt_id"]
            vid = client.table("agent_prompt_versions").insert({
                "prompt_id": prompt_id, "version_number": info["next_version_number"],
                "content": info["content"], "notes": NOTES,
            }).execute().data[0]["prompt_version_id"]
        pins[slug] = vid
    update: Dict[str, Any] = {}
    if pins != p["before"]["prompt_version_ids"]:
        update["prompt_version_ids"] = pins
    if p["greeting"]:
        update["voice_config"] = {**p["voice_config"], "greeting_trigger": p["greeting"]}
    if update:
        client.table("agent_profiles").update(update).eq("profile_id", p["profile_id"]).execute()


def run(client, env: str, *, dry_run: bool, out=print) -> Dict[str, Any]:
    from runtime.tools.agents_demo.data import AGENTS

    plans = [plan_profile(client, env, key) for key in AGENTS]
    out("# Before (for rollback):")
    out(json.dumps({p["agent"]: ({"profile_id": p["profile_id"], **p["before"]} if not p["missing"] else None)
                    for p in plans}, ensure_ascii=False, indent=1, default=str))
    report: Dict[str, Any] = {}
    for p in plans:
        if p["missing"]:
            out(f"[{env}] {p['kind']}: no profile — nothing to do")
            report[p["agent"]] = None
            continue
        changes = _changes(p)
        report[p["agent"]] = changes
        if not changes:
            out(f"[{env}] {p['kind']} ({p['profile_id']}): in sync — nothing to do")
            continue
        for line in changes:
            out(f"[{env}] {p['kind']} ({p['profile_id']}): {'WOULD ' if dry_run else ''}{line}")
        if not dry_run:
            apply_profile(client, copy.deepcopy(p))
    if env == "prod" and not dry_run:
        changed = [p["profile_id"] for p in plans if not p["missing"] and report.get(p["agent"])]
        if changed:
            out("\nProd serves promoted snapshots — self-promote each changed profile now:")
            for pid in changed:
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
