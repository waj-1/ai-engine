"""Per-scenario executor for runner_kind='voice_live' suites (spec §5).

Bridges the async caller-loop driver into admin/evals.py's synchronous
scenario loop and maps the conversation into the standard eval results[]
record shape — so the matrix/compare/bake-off machinery works unchanged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from admin.e2e_runner import _invoke_judge
from admin.voice_eval_driver import render_transcript, run_voice_eval_scenario

logger = logging.getLogger(__name__)

# Sentence-final punctuation, across the scripts this edge actually speaks:
# Latin, Arabic (؟ ، ۔), and CJK full-width. A trailing colon counts — she
# legitimately ends a turn by opening a list ("تبي تفصيل حسب:").
_UTTERANCE_END = ".!?:…؟۔。！？"
# Closers that may follow the real terminator. Quotes and brackets ONLY — an
# Arabic comma («،») is a mid-sentence separator, so a reply ending on one is
# cut off, not closed, and must not be stripped away into a pass.
_UTTERANCE_TRAIL = "\"'”’)]»"


def _ends_like_a_finished_utterance(text: str) -> bool:
    """Did this reply END, or was it cut off?

    Checked against the 2026-08-06 04:13 call verbatim: the two replies the
    caller reported as cut («…كيف أقدر أ», «…ولا حسب الطرفية») are exactly
    the two that fail this, and the two that completed pass it. See
    tests/admin/test_voice_eval_limits.py, which pins that call as the
    fixture.

    Deliberately NOT a heuristic about word shape or length: this is opt-in
    per scenario (`limits.require_complete_utterances`) precisely because a
    scenario that asks for a bare list or a spelled-out IBAN readback would
    legitimately end without punctuation, and the operator writing that
    scenario is the one who knows.
    """
    stripped = (text or "").rstrip()
    while stripped and stripped[-1] in _UTTERANCE_TRAIL:
        stripped = stripped[:-1].rstrip()
    return bool(stripped) and stripped[-1] in _UTTERANCE_END


def _run_checks(checks: List[Dict[str, Any]], headers: Optional[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Side-effect oracles after hangup — http_check only in v1, same
    semantics as admin/voice_scenario_runner's http_check step."""
    import httpx

    out: List[Dict[str, Any]] = []
    for i, check in enumerate(checks or []):
        if check.get("type") != "http_check":
            out.append({"step": i, "type": check.get("type"), "ok": False,
                        "error": f"unknown check type {check.get('type')!r}"})
            continue
        ok, body_head = False, ""
        try:
            resp = httpx.request(
                str(check.get("method") or "GET"), str(check["url"]),
                headers={**(headers or {}), **(check.get("headers") or {})},
                timeout=20.0,
            )
            body = resp.text
            body_head = body[:200]
            matched = bool(re.search(str(check.get("pattern") or ""), body,
                                     re.IGNORECASE | re.DOTALL))
            ok = matched if check.get("expect_match", True) else not matched
        except Exception as exc:
            body_head = f"{type(exc).__name__}: {exc}"
        out.append({"step": i, "type": "http_check", "ok": ok,
                    "url": check.get("url"), "body_head": body_head})
    return out


def run_voice_scenario_sync(
    scenario: Dict[str, Any],
    *,
    voice_ctx: Dict[str, Any],
    judge: Any,
    pass_threshold: float,
    caller_llm: Any,
) -> Dict[str, Any]:
    started = time.monotonic()
    scenario_id = scenario.get("id") or scenario.get("name", "unnamed")
    turn_specs: List[Dict[str, Any]] = list(scenario.get("turns") or [])
    if turn_specs:
        # A scripted, check-graded scenario: `turns: [{say, checks}]` becomes
        # the caller's script (no caller LLM) and each turn's checks run on
        # what was spoken AND shown (admin/services/eval_checks.py).
        scenario = {**scenario, "caller": {**(scenario.get("caller") or {}),
                                           "script": [t.get("say") or t.get("question") or ""
                                                      for t in turn_specs]}}
    caller = scenario.get("caller") or {}
    goal = str(caller.get("goal") or "")
    expected = scenario.get("expected_answer") or goal
    expected_substrings: List[str] = scenario.get("expected_substrings") or []

    try:
        transcript = asyncio.run(run_voice_eval_scenario(
            scenario, voice_ctx, caller_llm=caller_llm,
        ))
    except Exception as exc:
        # The driver's contract is "never raises" — but if that contract is
        # ever broken, letting the exception propagate would blow up the
        # WHOLE suite run in run_eval_suite_sync's outer handler (finalized
        # "errored" with no results=, discarding every already-collected
        # scenario). Contain it here instead, same as _run_scenario contains
        # a transport error per-scenario on the chat path.
        logger.exception("voice eval: driver raised for scenario %s", scenario_id)
        transcript = {
            "status": "error", "reason": f"{type(exc).__name__}: {exc}",
            "turns": [], "per_turn": [], "audio_bytes_received": 0,
            "termination": "ws_closed", "target": "",
        }

    turns = transcript.get("turns") or []
    rendered = render_transcript(turns)
    proposals = transcript.get("proposals") or []
    if proposals:
        # Surface the proposal events to the judge alongside the spoken
        # transcript — rubric criteria like "an address-change proposal was
        # surfaced for review" are otherwise ungradeable: the frames are
        # wire events, not spoken turns.
        rendered += "\n" + "\n".join(
            "[PROPOSAL SURFACED: "
            + str(p.get("type") or "unknown")
            + (f" — {json.dumps({k: v for k, v in p.items() if k not in ('type', 'confidence')}, ensure_ascii=False)[:160]}")
            + "]"
            for p in proposals
        )
    spoken_assistant = " ".join(
        t["text"] for t in turns if t.get("role") == "assistant"
    )
    completed = transcript.get("status") == "completed"
    skipped = transcript.get("status") == "skipped"

    missing = [
        frag for frag in expected_substrings
        if frag.lower() not in spoken_assistant.lower()
    ]

    judge_result: Optional[Dict[str, Any]] = None
    grading_note: Optional[str] = None
    if skipped:
        grading_note = f"skipped: {transcript.get('reason')}"
        passed = False
    elif not completed:
        passed = False
    elif turn_specs and not scenario.get("expected_answer"):
        passed = True        # the per-turn checks below are the grade
    else:
        if judge is not None:
            try:
                judge_result = _invoke_judge(
                    judge,
                    question=goal,
                    expected=expected,
                    actual=rendered,
                    pass_threshold=pass_threshold,
                    rubric=scenario.get("rubric"),
                )
            except Exception as exc:
                judge_result = {"score": 0.0, "pass": False,
                                "rationale": f"judge_error: {exc}"[:300]}
        if judge_result is not None:
            passed = bool(judge_result["pass"]) and not missing
        elif expected_substrings:
            passed = not missing
        else:
            passed = False
            grading_note = (
                "ungraded: voice_live suites need an evaluator model "
                "(pick one in the suite editor)"
            )

    checks = _run_checks([c for c in (scenario.get("checks") or []) if c.get("type") == "http_check"],
                         (voice_ctx.get("headers") or None))
    if turn_specs:
        from admin.services.eval_checks import run_checks, tools_from_status_lines

        per_turn_recs = transcript.get("per_turn") or []
        for i, spec in enumerate(turn_specs):
            rec = per_turn_recs[i] if i < len(per_turn_recs) else {}
            spoken, shown = rec.get("text") or "", rec.get("display") or ""
            for r in run_checks(
                spec.get("checks") or [], answer=f"{spoken}\n{shown}".strip(),
                proposals=rec.get("proposals") or [],
                tools=tools_from_status_lines(rec.get("status") or []),
                caller_text=spec.get("say") or "",
                business_id=voice_ctx.get("business_id"), user_token=voice_ctx.get("user_token"),
                spoken=spoken, shown=shown, charts=int(rec.get("charts") or 0),
            ):
                checks.append({"turn": i + 1, **r})
        if not per_turn_recs:
            checks.append({"type": "turns", "ok": False, "detail": "no turn completed"})
    checks_ok = all(c.get("ok") for c in checks) if checks else True
    if not checks_ok:
        passed = False  # hard gate, like expected_tools (spec §5)

    # Proposal hard gate: every type listed in the scenario's
    # `expected_proposals` must have actually been surfaced as a proposal
    # frame. Same spirit as `checks` — the judge grades prose, and prose can
    # say "queued for your review" while nothing reached the screen.
    expected_proposals: List[str] = scenario.get("expected_proposals") or []
    missing_proposals: Optional[List[str]] = None
    if expected_proposals:
        surfaced = [str(p.get("type") or "") for p in proposals]
        missing_proposals = [
            t for t in expected_proposals if t not in surfaced
        ]
        if missing_proposals:
            passed = False

    per_turn = transcript.get("per_turn") or []

    # Per-scenario metric hard gates ("limits"): {"max_latency_ms": ...,
    # "max_turn_ms": ..., "require_turn_end": ...}. Independent of the judge
    # verdict, same as the `checks` gate above — a scenario can read as a
    # great conversation and still blow its latency budget. Only evaluated
    # on a completed transcript: a skipped/error scenario already fails for
    # its own reason and its per_turn data (if any) isn't meaningful.
    limits = scenario.get("limits")
    limit_breaches: Optional[List[Dict[str, Any]]] = None
    if completed and limits:
        limit_breaches = _check_limits(limits, per_turn)
        if limit_breaches:
            passed = False

    # ANSWER latency is the headline number (results[].latency_ms/ttft_ms):
    # the first NON-out-of-band assistant transcript per turn. The old
    # basis — first assistant transcript of any kind — pegged at
    # `progress_after_sec` on every tool turn, because the holding line
    # ("still working on that") arrives on schedule regardless of how slow
    # the real answer is; the metric was blind to a delegate regression.
    # Falls back to the legacy first-reply latency only for turns with no
    # `answer_ms` at all (rows recorded by a pre-marker driver).
    answer_latencies = [p["answer_ms"] for p in per_turn
                        if p.get("answer_ms") is not None]
    first_replies = [p["latency_ms"] for p in per_turn
                     if p.get("latency_ms") is not None]
    latencies = answer_latencies or first_replies
    mean_latency = int(sum(latencies) / len(latencies)) if latencies else 0

    scorer_detail: Dict[str, Any] = {
        "mode": voice_ctx.get("mode", "sim"),
        "turns": len(turns),
        "per_turn": per_turn,
        # Fraction of turns where a holding line preceded the answer — a
        # health signal on its own: it counts how often the delegate blew
        # past `progress_after_sec` and the caller sat through filler.
        "holding_line_rate": (
            round(sum(1 for p in per_turn if p.get("holding_line")) / len(per_turn), 3)
            if per_turn else 0.0
        ),
        "audio_bytes_received": transcript.get("audio_bytes_received", 0),
        "transcript": turns,
        "proposals": proposals,
        "checks": checks,
        "termination": transcript.get("termination"),
        "target": transcript.get("target"),
    }
    if limit_breaches is not None:
        scorer_detail["limit_breaches"] = limit_breaches
    if missing_proposals is not None:
        scorer_detail["missing_proposals"] = missing_proposals

    return {
        "scenario_id": scenario_id,
        "name": scenario.get("name", scenario_id),
        "category": scenario.get("category"),
        "question": goal,
        "expected": expected,
        "expected_substrings": expected_substrings,
        "passed": passed,
        "score": (judge_result["score"] if judge_result
                  else (1.0 if passed else 0.0)),
        "missing_substrings": missing,
        "judge": judge_result,
        "criteria": (judge_result.get("criteria") if judge_result else None),
        "grading_note": grading_note,
        "http_status": 101 if completed else 0,
        "latency_ms": mean_latency,
        "ttft_ms": mean_latency,
        "total_ms": int((time.monotonic() - started) * 1000),
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": None,
        "iterations": 1,
        "tool_count": 0,
        "executed_tools": [],
        "expected_tools": None,
        "forbidden_tools": None,
        "missing_expected_tools": None,
        "forbidden_tools_used": None,
        "tool_assertion_passed": (False if checks and not checks_ok
                                  else (True if checks else None)),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "error": transcript.get("reason") if not completed and not skipped else None,
        "actual": rendered,
        "actual_output_excerpt": (rendered[:200] + "…") if len(rendered) > 200 else rendered,
        "scorer_detail": scorer_detail,
    }


def _check_limits(
    limits: Dict[str, Any], per_turn: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Evaluate a scenario's `limits` hard gate against the driver
    transcript's per-turn metrics (`turn`/`latency_ms`/`first_reply_ms`/
    `answer_ms`/`holding_line`/`turn_ms`/`turn_end_seen`/`audio_ms`/
    `audio_discarded_ms`/`text` — see admin/voice_eval_driver.py). Returns
    one breach dict per violated metric per turn; a null metric value (e.g.
    no `latency_ms` because the assistant never replied that turn) is
    skipped rather than treated as a breach.

    `max_answer_ms` vs `max_latency_ms`: the latter caps the first assistant
    transcript of ANY kind — on a tool turn that is usually the out-of-band
    holding line at `progress_after_sec`, so it cannot see a slow answer at
    all (kept for back-compat with existing suite rows). `max_answer_ms`
    caps `answer_ms`, the first NON-out-of-band transcript — the number a
    delegate regression actually moves. New suites should gate on
    `max_answer_ms`.

    Two of these limits exist because a rubric judge CANNOT catch a reply
    that was cut off: the truncated text still matches the pattern for the
    answer, and a `no_truncation` criterion reads a fragment as a pass. They
    are the mechanical, judge-free half:

    * `require_complete_utterances` — the reply's own text must end like a
      finished utterance. When the SERVICE stops generating (the 2026-08-06
      04:13 call: «كيف أقدر أ», «…ولا حسب الطرفية»), audio and transcript
      stop together, so no audio/text ratio can see it. The unfinished
      sentence is the only wire-visible evidence there is.
    * `min_played_audio_ratio` — of the audio the engine sent for this turn,
      how much survived to be heard. Catches the OTHER truncation family: a
      playback flush discarding an answer the model believes it delivered
      (#70), where the text is complete and only the sound is missing.
    """
    breaches: List[Dict[str, Any]] = []
    max_latency_ms = limits.get("max_latency_ms")
    max_answer_ms = limits.get("max_answer_ms")
    max_turn_ms = limits.get("max_turn_ms")
    require_turn_end = limits.get("require_turn_end")
    require_complete = limits.get("require_complete_utterances")
    min_played_ratio = limits.get("min_played_audio_ratio")
    for turn in per_turn:
        turn_no = turn.get("turn")
        if require_complete:
            text = (turn.get("text") or "").strip()
            if text and not _ends_like_a_finished_utterance(text):
                breaches.append({
                    "turn": turn_no, "kind": "incomplete_utterance",
                    # The tail, not the whole reply: this lands in a run
                    # detail pane and the last few words are the evidence.
                    "value": text[-40:], "limit": True,
                })
        if min_played_ratio is not None:
            sent = turn.get("audio_ms")
            discarded = turn.get("audio_discarded_ms")
            if sent and discarded is not None and sent > 0:
                ratio = max(0.0, (sent - discarded)) / float(sent)
                if ratio < float(min_played_ratio):
                    breaches.append({
                        "turn": turn_no, "kind": "min_played_audio_ratio",
                        "value": round(ratio, 3), "limit": min_played_ratio,
                    })
        if max_latency_ms is not None:
            value = turn.get("latency_ms")
            if value is not None and value > max_latency_ms:
                breaches.append({"turn": turn_no, "kind": "max_latency_ms",
                                 "value": value, "limit": max_latency_ms})
        if max_answer_ms is not None:
            value = turn.get("answer_ms")
            if value is not None and value > max_answer_ms:
                breaches.append({"turn": turn_no, "kind": "max_answer_ms",
                                 "value": value, "limit": max_answer_ms})
        if max_turn_ms is not None:
            value = turn.get("turn_ms")
            if value is not None and value > max_turn_ms:
                breaches.append({"turn": turn_no, "kind": "max_turn_ms",
                                 "value": value, "limit": max_turn_ms})
        if require_turn_end:
            value = turn.get("turn_end_seen")
            if value is not None and not value:
                breaches.append({"turn": turn_no, "kind": "missing_turn_end",
                                 "value": value, "limit": True})
    return breaches
