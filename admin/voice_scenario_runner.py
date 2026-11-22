"""Run DB-defined live-voice scenarios against a real WS /voice/live edge.

The portal/MCP-runnable successor to an operator-laptop scenario harness:
scenario definitions live in `ai_voice_scenarios` (seeded by
`admin/seed_voice_scenarios.py`, edited at /admin/voice-scenarios or via the
admin MCP `voice_scenarios` tool), caller speech is synthesized with the
same Azure Speech TTS the turn-based voice edge uses (no macOS `say`), and
each execution lands in `ai_voice_scenario_runs` with per-step results.

Step shapes (the `steps` JSON array, executed in order):

  {"type": "say", "text": "...", "voice": "en-US-JennyNeural",
   "language": "en-US", "silence_sec": 1.6}
      Synthesize `text` and stream it into the socket as 24kHz pcm16 at
      real-time pace, then `silence_sec` of trailing silence so semantic
      VAD closes the turn.

  {"type": "expect_transcript", "role": "assistant", "pattern": "regex",
   "within_sec": 25}
      Wait until a transcript frame from `role` emitted AFTER the previous
      step matches `pattern` (case-insensitive search). Empty pattern =
      "any non-empty transcript".

  {"type": "expect_frame", "frame_type": "display", "pattern": "regex",
   "within_sec": 25}
      Same, for any other JSON frame type (display/chart/interrupted/...);
      the pattern is searched in the frame's JSON serialization.

  {"type": "http_check", "url": "https://...", "method": "GET",
   "pattern": "regex", "headers": {...}}
      Side-effect assertion — re-read backing state after the call beats
      (the IBAN-fabrication lesson: assert what HAPPENED, not how it was
      phrased). The pattern is searched in the response body.

  {"type": "wait", "sec": 2}

Failure containment mirrors the voice edge itself: a scenario that cannot
run (no TTS key, disabled row, unreachable target) is recorded as `skipped`/
`error`, never raised into the caller.

**A scenario run is a REAL call and costs real money.** It opens a genuine
provider session and its seconds land in `ai_voice_usage_daily` like any
other. It carries no portal cookie, so the edge charges the ANONYMOUS bucket
derived from the connecting IP (loopback for an in-process run) rather than a
merchant's allowance — a run cannot drain a tenant's daily budget, but it does
consume the loopback IP's concurrency slots (`max_concurrent_per_ip`,
default 8), so a large parallel batch will start refusing itself. Pass
`headers` (a portal cookie) only when the scenario genuinely needs an
authenticated session for its tool calls, and expect that run to bill the
tenant it authenticates as.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Module-level (not call-site) so tests can patch the factory the runner
# actually uses; `set_admin_env` pins the branch inside the job thread.
from core.auth import admin_service_client, set_admin_env

logger = logging.getLogger(__name__)

STEP_TYPES = ("say", "expect_transcript", "expect_frame", "http_check", "wait")

_SAMPLE_RATE = 24000
_CHUNK_BYTES = 1920          # 40ms at 24kHz mono pcm16
_CHUNK_SEC = 0.04
_DEFAULT_WITHIN_SEC = 25.0
_DEFAULT_SILENCE_SEC = 1.6
_FRAME_TAIL_KEPT = 80        # how many trailing frames land in run detail

_DEFAULT_VOICES = {
    "ar": "ar-SA-HamedNeural",
    "en": "en-US-JennyNeural",
}


def default_target(ws_path: str) -> str:
    """The engine's own voice edge, reached over loopback.

    The runner executes inside the same process/container as the edge, so
    loopback is both the simplest and the most honest target: it exercises
    the full stack (channel resolution, budget, provider session) exactly as
    a browser would. `VOICE_SCENARIO_TARGET` overrides the host part for
    running against a remote deployment (e.g. the demo VM) instead.
    """
    base = os.getenv("VOICE_SCENARIO_TARGET", "").strip().rstrip("/")
    if not base:
        port = os.getenv("PORT", "8000")
        base = f"ws://127.0.0.1:{port}"
    if base.startswith("http"):
        base = "ws" + base[4:]
    return f"{base}{ws_path}"


GENERIC_AGENT_PATH = re.compile(r"^/v1/agents/[a-z0-9][a-z0-9_-]{0,63}/voice$")


def scenario_headers(
    scenario: Dict[str, Any], *, target: Optional[str],
    headers: Optional[Dict[str, str]], user_token: Optional[str],
) -> Optional[Dict[str, str]]:
    """The headers a run connects with.

    A scenario whose call path is a generic agent's address
    (`/v1/agents/{slug}/voice`) needs a caller the agent admits, so the
    operator who started the run is sent as its bearer token. Only to this
    engine's own loopback edge: with an explicit `target` the token would
    leave the server, so there the caller supplies headers themselves. An
    `Authorization` header passed in always wins.
    """
    path = str(scenario.get("ws_path") or "")
    if (not user_token or target or os.getenv("VOICE_SCENARIO_TARGET", "").strip()
            or not GENERIC_AGENT_PATH.match(path)):
        return headers
    if any(k.lower() == "authorization" for k in (headers or {})):
        return headers
    return {**(headers or {}), "Authorization": f"Bearer {user_token}"}


def _voice_for(step: Dict[str, Any], locale: Optional[str]) -> str:
    if step.get("voice"):
        return str(step["voice"])
    primary = (str(step.get("language") or locale or "en")).split("-")[0].lower()
    return _DEFAULT_VOICES.get(primary, _DEFAULT_VOICES["en"])


async def _synthesize(text: str, *, voice: str, language: Optional[str]) -> bytes:
    from voice.providers.azure import AzureSpeechVoiceProvider

    provider = AzureSpeechVoiceProvider()
    return await provider.synthesize_pcm(
        text, voice=voice, language=language, sample_rate=_SAMPLE_RATE
    )


def evaluate_pattern(frames: List[Dict[str, Any]], *, since_index: int,
                     frame_type: str, role: Optional[str], pattern: str) -> bool:
    """True when a frame after `since_index` matches. Pure — unit-tested."""
    rx = re.compile(pattern, re.IGNORECASE | re.DOTALL) if pattern else None
    for frame in frames[since_index:]:
        if frame.get("type") != frame_type:
            continue
        if role and frame.get("role") != role:
            continue
        if rx is None:
            text = frame.get("text") or frame.get("content") or ""
            if str(text).strip():
                return True
            continue
        if rx.search(json.dumps(frame, ensure_ascii=False)):
            return True
    return False


async def run_scenario(
    scenario: Dict[str, Any],
    *,
    target: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout_sec: float = 240.0,
) -> Dict[str, Any]:
    """Execute one scenario dict; returns {status, checks, frames_tail, ...}.

    Never raises: every failure mode is folded into the returned status so
    the tracked-job wrapper can persist it.
    """
    steps = list(scenario.get("steps") or [])
    if not steps:
        return {"status": "skipped", "reason": "scenario has no steps"}
    if not os.getenv("AZURE_SPEECH_KEY"):
        return {"status": "skipped",
                "reason": "AZURE_SPEECH_KEY is unset — cannot synthesize caller speech"}

    import aiohttp

    ws_path = scenario.get("ws_path") or "/voice/live"
    locale = scenario.get("locale")
    url = target or default_target(ws_path)
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}agentKind={scenario['agent_kind']}"
    if locale:
        url += f"&locale={locale}"
    # Scenario runs are bot-driven acceptance calls — self-declare so the
    # observability screens badge their sessions "sim" (the endpoint stamps
    # waj.voice.simulated on the call's spans + recording).
    url += "&simRun=1"

    frames: List[Dict[str, Any]] = []
    audio_bytes = {"n": 0}
    checks: List[Dict[str, Any]] = []

    async def _receiver(ws) -> None:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                audio_bytes["n"] += len(msg.data)
            elif msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    frames.append(json.loads(msg.data))
                except Exception:
                    frames.append({"type": "unparsed", "raw": msg.data[:200]})
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                return

    async def _drive(ws, http: "aiohttp.ClientSession") -> str:
        status = "passed"
        for i, step in enumerate(steps):
            stype = step.get("type")
            mark = len(frames)
            if stype == "say":
                voice = _voice_for(step, locale)
                language = step.get("language") or locale
                pcm = await _synthesize(str(step.get("text") or ""),
                                        voice=voice, language=language)
                silence = b"\x00" * int(
                    _SAMPLE_RATE * 2 * float(step.get("silence_sec", _DEFAULT_SILENCE_SEC))
                )
                for off in range(0, len(pcm) + len(silence), _CHUNK_BYTES):
                    chunk = (pcm + silence)[off:off + _CHUNK_BYTES]
                    if chunk:
                        await ws.send_bytes(chunk)
                    # Real-time pacing: semantic VAD reasons over time, and a
                    # burst-delivered utterance ends turns unrealistically.
                    await asyncio.sleep(_CHUNK_SEC)
                checks.append({"step": i, "type": stype, "ok": True,
                               "said": str(step.get("text") or "")[:120]})
            elif stype in ("expect_transcript", "expect_frame"):
                frame_type = ("transcript" if stype == "expect_transcript"
                              else str(step.get("frame_type") or "display"))
                role = step.get("role") if stype == "expect_transcript" else None
                pattern = str(step.get("pattern") or "")
                deadline = time.monotonic() + float(step.get("within_sec", _DEFAULT_WITHIN_SEC))
                ok = False
                while time.monotonic() < deadline:
                    if evaluate_pattern(frames, since_index=0 if step.get("from_start") else mark,
                                        frame_type=frame_type, role=role, pattern=pattern):
                        ok = True
                        break
                    await asyncio.sleep(0.25)
                checks.append({"step": i, "type": stype, "ok": ok,
                               "frame_type": frame_type, "pattern": pattern})
                if not ok:
                    status = "failed"
            elif stype == "http_check":
                ok, body_head = False, ""
                try:
                    async with http.request(
                        str(step.get("method") or "GET"), str(step["url"]),
                        headers={**(headers or {}), **(step.get("headers") or {})},
                    ) as resp:
                        body = await resp.text()
                        body_head = body[:200]
                        ok = bool(re.search(str(step.get("pattern") or ""), body,
                                            re.IGNORECASE | re.DOTALL))
                except Exception as exc:
                    body_head = f"{type(exc).__name__}: {exc}"
                checks.append({"step": i, "type": stype, "ok": ok,
                               "url": step.get("url"), "body_head": body_head})
                if not ok:
                    status = "failed"
            elif stype == "wait":
                await asyncio.sleep(float(step.get("sec", 1)))
                checks.append({"step": i, "type": stype, "ok": True})
            else:
                checks.append({"step": i, "type": stype, "ok": False,
                               "error": f"unknown step type {stype!r}"})
                status = "failed"
        return status

    try:
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(url, headers=headers or {}, max_msg_size=0) as ws:
                receiver = asyncio.create_task(_receiver(ws))
                try:
                    status = await asyncio.wait_for(_drive(ws, http), timeout=timeout_sec)
                finally:
                    receiver.cancel()
                    await asyncio.gather(receiver, return_exceptions=True)
    except asyncio.TimeoutError:
        return {"status": "error", "reason": f"scenario exceeded {timeout_sec}s",
                "checks": checks, "frames_tail": frames[-_FRAME_TAIL_KEPT:]}
    except Exception as exc:
        logger.exception("voice scenario: run failed for %s", scenario.get("slug"))
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}",
                "checks": checks, "frames_tail": frames[-_FRAME_TAIL_KEPT:]}

    return {
        "status": status,
        "checks": checks,
        "frames_tail": frames[-_FRAME_TAIL_KEPT:],
        "audio_bytes_received": audio_bytes["n"],
        "target": url,
    }


def run_scenario_job(
    *,
    slug: str,
    environment: str = "prod",
    triggered_by: str = "",
    target: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    user_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Sync entrypoint for admin/_jobs.start_tracked_job (thread context).

    Loads the scenario row, executes it on a fresh event loop, and persists
    an `ai_voice_scenario_runs` row through running -> terminal status.

    ``environment`` pins the env branch the run reads/writes: the portal
    passes the switcher env (which `start_tracked_job` also re-sets in this
    worker thread), and the admin MCP passes its explicit ``env=`` param —
    which the job-spawn capture can NOT see, because /mcp requests never run
    the /admin env middleware. Pinning here makes the scenario lookup and the
    run row land on the SAME branch the voice-scenarios screen (and the MCP
    ``runs`` action) read via their env-scoped clients — before this, both
    went through the bare `get_service_client()` and a cross-env run could
    not even find the scenario row it was asked to run.
    """
    # No reset needed: start_tracked_job runs the worker inside a context
    # COPY (admin/_jobs.py `_run`), so this pin dies with the worker and
    # cannot leak into the tracker's own terminal update.
    set_admin_env(environment)
    client = admin_service_client()
    rows = (
        client.table("ai_voice_scenarios").select("*").eq("slug", slug)
        .limit(1).execute().data
    )
    if not rows:
        return {"status": "error", "reason": f"no scenario with slug {slug!r}"}
    scenario = rows[0]

    run_row = {
        "scenario_id": scenario["scenario_id"],
        "slug": slug,
        "status": "running",
        "target": target or default_target(scenario.get("ws_path") or "/voice/live"),
    }
    run_id = None
    try:
        inserted = client.table("ai_voice_scenario_runs").insert(run_row).execute().data
        run_id = (inserted[0] or {}).get("run_id") if inserted else None
    except Exception:
        logger.warning("voice scenario: could not record run start", exc_info=True)

    if not scenario.get("enabled", True):
        result: Dict[str, Any] = {"status": "skipped", "reason": "scenario is disabled"}
    else:
        result = asyncio.run(
            run_scenario(scenario, target=target,
                         headers=scenario_headers(scenario, target=target, headers=headers,
                                                  user_token=user_token))
        )

    if run_id:
        try:
            client.table("ai_voice_scenario_runs").update({
                "status": result.get("status", "error"),
                "detail": {k: v for k, v in result.items() if k != "status"},
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }).eq("run_id", run_id).execute()
        except Exception:
            logger.warning("voice scenario: could not record run result", exc_info=True)

    return {**result, "run_id": run_id, "slug": slug, "environment": environment,
            "triggered_by": triggered_by}
