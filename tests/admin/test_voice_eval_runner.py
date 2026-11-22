"""voice_live per-scenario runner: transcript -> judge -> record mapping."""
import admin.voice_eval_runner as ver


SCENARIO = {
    "id": "iban_change",
    "name": "IBAN change",
    "category": "flow",
    "caller": {"persona": "p", "goal": "change IBAN to SA03X", "max_turns": 8},
    "rubric": [{"id": "goal_completed", "weight": 3, "criterion": "..."}],
    "expected_substrings": ["SA03X"],
    "checks": [],
}
VOICE_CTX = {"mode": "sim", "agent_kind": "orchard", "ws_path": "/voice/live"}

TRANSCRIPT = {
    "status": "completed",
    "reason": None,
    "turns": [
        {"role": "assistant", "text": "Hello!"},
        {"role": "caller", "text": "Change my IBAN to SA03X please."},
        {"role": "assistant", "text": "Done — your new IBAN ending SA03X is saved."},
    ],
    "per_turn": [{"turn": 0, "latency_ms": 120, "turn_ms": 300, "turn_end_seen": True}],
    "audio_bytes_received": 480,
    "termination": "hangup",
    "target": "ws://x/voice/live",
}


def _patch_driver(monkeypatch, transcript=TRANSCRIPT):
    async def fake_run(scenario, voice_ctx, *, caller_llm, target=None, timeout_sec=240.0):
        return dict(transcript)
    monkeypatch.setattr(ver, "run_voice_eval_scenario", fake_run)


def _patch_judge(monkeypatch, score=0.9):
    def fake_invoke_judge(judge, question, expected, actual, pass_threshold, **kw):
        fake_invoke_judge.last = {"question": question, "actual": actual, "kw": kw}
        return {"score": score, "pass": score >= pass_threshold,
                "rationale": "ok", "criteria": []}
    monkeypatch.setattr(ver, "_invoke_judge", fake_invoke_judge)
    return fake_invoke_judge


def test_passing_record_shape(monkeypatch):
    _patch_driver(monkeypatch)
    judge_spy = _patch_judge(monkeypatch, score=0.9)
    rec = ver.run_voice_scenario_sync(
        SCENARIO, voice_ctx=VOICE_CTX, judge=object(),
        pass_threshold=0.7, caller_llm=object(),
    )
    assert rec["passed"] is True
    assert rec["score"] == 0.9
    assert rec["scenario_id"] == "iban_change"
    assert rec["question"] == SCENARIO["caller"]["goal"]
    assert rec["http_status"] == 101
    assert rec["latency_ms"] == 120
    assert rec["judge"]["score"] == 0.9
    sd = rec["scorer_detail"]
    assert sd["mode"] == "sim" and sd["termination"] == "hangup"
    assert sd["turns"] == 3 and sd["transcript"]
    # Judge saw the rendered transcript, and the rubric went through.
    assert "ASSISTANT: Done" in judge_spy.last["actual"]
    assert judge_spy.last["kw"]["rubric"] == SCENARIO["rubric"]


def test_missing_substring_fails(monkeypatch):
    t = dict(TRANSCRIPT)
    t["turns"] = [{"role": "assistant", "text": "Hello, nothing done."}]
    _patch_driver(monkeypatch, t)
    _patch_judge(monkeypatch, score=0.95)
    rec = ver.run_voice_scenario_sync(
        SCENARIO, voice_ctx=VOICE_CTX, judge=object(),
        pass_threshold=0.7, caller_llm=object(),
    )
    assert rec["passed"] is False        # substring hard gate beat the judge
    assert rec["missing_substrings"] == ["SA03X"]


def test_failed_check_is_hard_gate(monkeypatch):
    _patch_driver(monkeypatch)
    _patch_judge(monkeypatch, score=0.95)

    def fake_checks(checks, headers):
        return [{"step": 0, "type": "http_check", "ok": False, "url": "u"}]
    monkeypatch.setattr(ver, "_run_checks", fake_checks)
    scenario = {**SCENARIO, "checks": [{"type": "http_check", "url": "u", "pattern": "x"}]}
    rec = ver.run_voice_scenario_sync(
        scenario, voice_ctx=VOICE_CTX, judge=object(),
        pass_threshold=0.7, caller_llm=object(),
    )
    assert rec["passed"] is False
    assert rec["scorer_detail"]["checks"][0]["ok"] is False


def test_driver_error_maps_to_error_record(monkeypatch):
    _patch_driver(monkeypatch, {"status": "error", "reason": "boom", "turns": [],
                                "per_turn": [], "audio_bytes_received": 0,
                                "termination": "ws_closed", "target": "t"})
    _patch_judge(monkeypatch)
    rec = ver.run_voice_scenario_sync(
        SCENARIO, voice_ctx=VOICE_CTX, judge=object(),
        pass_threshold=0.7, caller_llm=object(),
    )
    assert rec["passed"] is False
    assert rec["http_status"] == 0
    assert rec["error"] == "boom"


def test_skipped_record(monkeypatch):
    _patch_driver(monkeypatch, {"status": "skipped", "reason": "no key",
                                "turns": [], "per_turn": [],
                                "audio_bytes_received": 0,
                                "termination": "skipped", "target": "t"})
    _patch_judge(monkeypatch)
    rec = ver.run_voice_scenario_sync(
        SCENARIO, voice_ctx=VOICE_CTX, judge=object(),
        pass_threshold=0.7, caller_llm=object(),
    )
    assert rec["passed"] is False
    assert rec["grading_note"].startswith("skipped")


def test_driver_exception_is_contained(monkeypatch):
    """The driver's contract is 'never raises' — but if that contract is
    ever broken, run_voice_scenario_sync must still return a normal error
    record rather than letting the exception escape (which would blow up
    the whole suite run in run_eval_suite_sync's outer handler and discard
    every already-collected scenario)."""
    async def fake_run_raises(scenario, voice_ctx, *, caller_llm, target=None, timeout_sec=240.0):
        raise RuntimeError("driver bug")
    monkeypatch.setattr(ver, "run_voice_eval_scenario", fake_run_raises)
    _patch_judge(monkeypatch)

    rec = ver.run_voice_scenario_sync(
        SCENARIO, voice_ctx=VOICE_CTX, judge=object(),
        pass_threshold=0.7, caller_llm=object(),
    )
    assert rec["passed"] is False
    assert rec["http_status"] == 0
    assert "driver bug" in rec["error"]


# ---------------------------------------------------------------------------
# Step 5: dispatch test — run_eval_suite_sync routes runner_kind='voice_live'
# suites through admin.voice_eval_runner.run_voice_scenario_sync.
# ---------------------------------------------------------------------------

import admin.evals as evals_mod


class _FakeTable:
    """Minimal chainable Supabase-table stand-in — only the methods the
    voice_live dispatch path in run_eval_suite_sync actually calls
    (select/update -> eq* -> limit? -> execute)."""

    def __init__(self, client, name):
        self._client = client
        self._name = name
        self._filters = {}
        self._op = None
        self._payload = None

    def select(self, *_args):
        self._op = "select"
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def eq(self, column, value):
        self._filters[column] = value
        return self

    def limit(self, _n):
        return self

    def execute(self):
        if self._op == "update":
            self._client.updates.append({
                "table": self._name,
                "payload": self._payload,
                "filters": dict(self._filters),
            })
            return type("R", (), {"data": [self._payload]})()
        rows = self._client.data.get(self._name, [])
        filtered = [
            r for r in rows
            if all(r.get(k) == v for k, v in self._filters.items())
        ]
        return type("R", (), {"data": filtered})()


class _FakeClient:
    def __init__(self, data):
        self.data = data
        self.updates = []

    def table(self, name):
        return _FakeTable(self, name)


def _dispatch_fake_client():
    suite = {
        "suite_id": "s1",
        "name": "voice suite",
        "agent_kind": "orchard",
        "runner_kind": "voice_live",
        "pass_threshold": 0.7,
        "evaluator_model_id": "m1",
        "scenarios": [SCENARIO],
        "default_context": {},
    }
    run_row = {
        "run_id": "run-1",
        "profile_id": "p1",
        "context": {},
        "agent_eval_suites": suite,
    }
    profile_row = {
        "profile_id": "p1",
        "agent_kind": "orchard",
        "endpoint_path": None,
        "environment": "staging",
        "model_main_id": None,
    }
    model_row = {"model_id": "m1", "model_string": "gemini-3.5-flash"}
    return _FakeClient({
        "agent_eval_runs": [run_row],
        "agent_profiles": [profile_row],
        "agent_models": [model_row],
    })


def test_run_eval_suite_sync_dispatches_voice_live(monkeypatch):
    client = _dispatch_fake_client()
    monkeypatch.setattr(evals_mod, "admin_service_client", lambda: client)
    monkeypatch.setattr(evals_mod, "_try_build_judge", lambda _model, **_kw: object())

    canned = {"scenario_id": "iban_change", "passed": True, "score": 1.0}

    def fake_run_voice_scenario_sync(scenario, *, voice_ctx, judge, pass_threshold, caller_llm):
        return dict(canned)

    import admin.voice_eval_runner as ver_mod
    monkeypatch.setattr(ver_mod, "run_voice_scenario_sync", fake_run_voice_scenario_sync)

    evals_mod.run_eval_suite_sync("run-1")

    final_updates = [
        u for u in client.updates
        if u["table"] == "agent_eval_runs" and u["payload"].get("status") in
        ("passed", "failed", "errored", "cancelled")
    ]
    assert final_updates, "no terminal status update was written"
    last = final_updates[-1]
    assert last["payload"]["status"] == "passed"
    assert len(last["payload"]["results"]) == 1
    assert last["payload"]["results"][0]["scenario_id"] == "iban_change"


# ---------------------------------------------------------------------------
# Finding 3: default_context.voice resolution — top-level vs. extra.voice.
# ---------------------------------------------------------------------------

def _dispatch_and_capture_voice_ctx(monkeypatch, default_context):
    client = _dispatch_fake_client()
    client.data["agent_eval_runs"][0]["agent_eval_suites"]["default_context"] = default_context
    monkeypatch.setattr(evals_mod, "admin_service_client", lambda: client)
    monkeypatch.setattr(evals_mod, "_try_build_judge", lambda _model, **_kw: object())

    captured = {}

    def fake_run_voice_scenario_sync(scenario, *, voice_ctx, judge, pass_threshold, caller_llm):
        captured["voice_ctx"] = voice_ctx
        return {"scenario_id": "iban_change", "passed": True, "score": 1.0}

    import admin.voice_eval_runner as ver_mod
    monkeypatch.setattr(ver_mod, "run_voice_scenario_sync", fake_run_voice_scenario_sync)

    evals_mod.run_eval_suite_sync("run-1")
    return captured["voice_ctx"]


def test_voice_ctx_resolves_voice_nested_under_extra(monkeypatch):
    # An operator who pasted {"voice": {...}} into the Advanced JSON box
    # before the eval_suite_save fix landed stored it at
    # default_context.extra.voice — the voice_ctx build must still find it.
    voice_ctx = _dispatch_and_capture_voice_ctx(monkeypatch, {
        "extra": {"voice": {"greeting_trigger": "Hi from extra"}},
    })
    assert voice_ctx["greeting_trigger"] == "Hi from extra"


def test_voice_ctx_top_level_voice_wins_over_extra(monkeypatch):
    voice_ctx = _dispatch_and_capture_voice_ctx(monkeypatch, {
        "voice": {"greeting_trigger": "Hi from top-level"},
        "extra": {"voice": {"greeting_trigger": "Hi from extra"}},
    })
    assert voice_ctx["greeting_trigger"] == "Hi from top-level"


# ---------------------------------------------------------------------------
# Per-scenario metric hard gates ("limits") — found on the client demo VM's
# first prod voice-eval run: nothing enforced a latency/turn-time/turn_end
# ceiling, so a scenario could pass the judge while the underlying duplex
# session was badly out of budget.
# ---------------------------------------------------------------------------

def test_limit_breach_fails_a_judge_passing_scenario(monkeypatch):
    t = dict(TRANSCRIPT)
    t["per_turn"] = [{"turn": 0, "latency_ms": 20000, "turn_ms": 300, "turn_end_seen": True}]
    _patch_driver(monkeypatch, t)
    _patch_judge(monkeypatch, score=0.95)  # judge alone would pass
    scenario = {**SCENARIO, "limits": {"max_latency_ms": 15000}}
    rec = ver.run_voice_scenario_sync(
        scenario, voice_ctx=VOICE_CTX, judge=object(),
        pass_threshold=0.7, caller_llm=object(),
    )
    assert rec["passed"] is False
    assert rec["scorer_detail"]["limit_breaches"] == [
        {"turn": 0, "kind": "max_latency_ms", "value": 20000, "limit": 15000},
    ]


def test_answer_latency_is_the_headline_metric_not_the_holding_line(monkeypatch):
    """The 2500ms wall: on tool turns the first assistant transcript is the
    holding line at progress_after_sec, so a first-reply mean says ~2500
    whatever the delegate does. results[].latency_ms/ttft_ms must aggregate
    `answer_ms` (first NON-out-of-band transcript) instead, and
    scorer_detail.holding_line_rate must expose how often the filler
    preceded the answer."""
    t = dict(TRANSCRIPT)
    t["per_turn"] = [
        {"turn": 0, "latency_ms": 2500, "first_reply_ms": 2500,
         "answer_ms": 6800, "holding_line": True,
         "turn_ms": 7200, "turn_end_seen": True},
        {"turn": 1, "latency_ms": 400, "first_reply_ms": 400,
         "answer_ms": 400, "holding_line": False,
         "turn_ms": 900, "turn_end_seen": True},
    ]
    _patch_driver(monkeypatch, t)
    _patch_judge(monkeypatch, score=0.9)
    rec = ver.run_voice_scenario_sync(
        SCENARIO, voice_ctx=VOICE_CTX, judge=object(),
        pass_threshold=0.7, caller_llm=object(),
    )
    assert rec["latency_ms"] == 3600   # mean of answer_ms, not of first_reply_ms
    assert rec["ttft_ms"] == 3600
    assert rec["scorer_detail"]["holding_line_rate"] == 0.5


def test_per_turn_rows_without_answer_ms_fall_back_to_first_reply(monkeypatch):
    """Rows recorded by a pre-marker driver carry only latency_ms — the
    aggregate must fall back rather than report 0 (test_passing_record_shape
    pins the value; this pins the reason)."""
    _patch_driver(monkeypatch)  # TRANSCRIPT's per_turn has no answer_ms
    _patch_judge(monkeypatch, score=0.9)
    rec = ver.run_voice_scenario_sync(
        SCENARIO, voice_ctx=VOICE_CTX, judge=object(),
        pass_threshold=0.7, caller_llm=object(),
    )
    assert rec["latency_ms"] == 120
    assert rec["scorer_detail"]["holding_line_rate"] == 0.0


def test_max_answer_ms_breach_fails_what_max_latency_ms_cannot_see(monkeypatch):
    """The regression max_latency_ms is structurally blind to: the holding
    line lands at 2500ms (inside any latency budget) while the answer takes
    15s. Only max_answer_ms can fail this scenario."""
    t = dict(TRANSCRIPT)
    t["per_turn"] = [
        {"turn": 0, "latency_ms": 2500, "first_reply_ms": 2500,
         "answer_ms": 15000, "holding_line": True,
         "turn_ms": 16000, "turn_end_seen": True},
    ]
    _patch_driver(monkeypatch, t)
    _patch_judge(monkeypatch, score=0.95)  # judge alone would pass
    scenario = {**SCENARIO,
                "limits": {"max_latency_ms": 20000, "max_answer_ms": 12000}}
    rec = ver.run_voice_scenario_sync(
        scenario, voice_ctx=VOICE_CTX, judge=object(),
        pass_threshold=0.7, caller_llm=object(),
    )
    assert rec["passed"] is False
    assert rec["scorer_detail"]["limit_breaches"] == [
        {"turn": 0, "kind": "max_answer_ms", "value": 15000, "limit": 12000},
    ]


def test_clean_limits_pass_with_empty_breach_list(monkeypatch):
    t = dict(TRANSCRIPT)
    t["per_turn"] = [{"turn": 0, "latency_ms": 100, "turn_ms": 300, "turn_end_seen": True}]
    _patch_driver(monkeypatch, t)
    _patch_judge(monkeypatch, score=0.95)
    scenario = {**SCENARIO, "limits": {"max_latency_ms": 15000, "max_turn_ms": 30000,
                                       "require_turn_end": True}}
    rec = ver.run_voice_scenario_sync(
        scenario, voice_ctx=VOICE_CTX, judge=object(),
        pass_threshold=0.7, caller_llm=object(),
    )
    assert rec["passed"] is True
    assert rec["scorer_detail"]["limit_breaches"] == []


def test_no_limits_key_absent_from_scorer_detail(monkeypatch):
    _patch_driver(monkeypatch)
    _patch_judge(monkeypatch, score=0.95)
    rec = ver.run_voice_scenario_sync(
        SCENARIO, voice_ctx=VOICE_CTX, judge=object(),
        pass_threshold=0.7, caller_llm=object(),
    )
    assert "limit_breaches" not in rec["scorer_detail"]


def test_max_turn_ms_breach(monkeypatch):
    t = dict(TRANSCRIPT)
    t["per_turn"] = [{"turn": 0, "latency_ms": 100, "turn_ms": 40000, "turn_end_seen": True}]
    _patch_driver(monkeypatch, t)
    _patch_judge(monkeypatch, score=0.95)
    scenario = {**SCENARIO, "limits": {"max_turn_ms": 30000}}
    rec = ver.run_voice_scenario_sync(
        scenario, voice_ctx=VOICE_CTX, judge=object(),
        pass_threshold=0.7, caller_llm=object(),
    )
    assert rec["passed"] is False
    assert rec["scorer_detail"]["limit_breaches"] == [
        {"turn": 0, "kind": "max_turn_ms", "value": 40000, "limit": 30000},
    ]


def test_missing_turn_end_breach(monkeypatch):
    t = dict(TRANSCRIPT)
    t["per_turn"] = [{"turn": 0, "latency_ms": 100, "turn_ms": 300, "turn_end_seen": False}]
    _patch_driver(monkeypatch, t)
    _patch_judge(monkeypatch, score=0.95)
    scenario = {**SCENARIO, "limits": {"require_turn_end": True}}
    rec = ver.run_voice_scenario_sync(
        scenario, voice_ctx=VOICE_CTX, judge=object(),
        pass_threshold=0.7, caller_llm=object(),
    )
    assert rec["passed"] is False
    assert rec["scorer_detail"]["limit_breaches"] == [
        {"turn": 0, "kind": "missing_turn_end", "value": False, "limit": True},
    ]


def test_null_metric_values_are_skipped_not_breached(monkeypatch):
    t = dict(TRANSCRIPT)
    # No first assistant reply -> latency_ms is None; must not be treated as
    # a breach of max_latency_ms.
    t["per_turn"] = [{"turn": 0, "latency_ms": None, "turn_ms": 300, "turn_end_seen": True}]
    _patch_driver(monkeypatch, t)
    _patch_judge(monkeypatch, score=0.95)
    scenario = {**SCENARIO, "limits": {"max_latency_ms": 100}}
    rec = ver.run_voice_scenario_sync(
        scenario, voice_ctx=VOICE_CTX, judge=object(),
        pass_threshold=0.7, caller_llm=object(),
    )
    assert rec["passed"] is True
    assert rec["scorer_detail"]["limit_breaches"] == []


def test_limits_not_evaluated_on_skipped_transcript(monkeypatch):
    """Only completed transcripts get limits evaluated — a skipped/error
    scenario already fails for its own reason, and per_turn may not mean
    anything on it."""
    _patch_driver(monkeypatch, {"status": "skipped", "reason": "no key",
                                "turns": [], "per_turn": [],
                                "audio_bytes_received": 0,
                                "termination": "skipped", "target": "t"})
    _patch_judge(monkeypatch)
    scenario = {**SCENARIO, "limits": {"max_latency_ms": 100}}
    rec = ver.run_voice_scenario_sync(
        scenario, voice_ctx=VOICE_CTX, judge=object(),
        pass_threshold=0.7, caller_llm=object(),
    )
    assert rec["passed"] is False
    assert "limit_breaches" not in rec["scorer_detail"]


def test_voice_ctx_extra_voice_does_not_leak_into_run_context(monkeypatch):
    # Regression guard for the suite_default.pop("extra") mutation bug:
    # resolving extra.voice for voice_ctx must not depend on (or be broken
    # by) the run_context build's earlier, separate flattening of
    # default_context.extra into run_context's top level.
    voice_ctx = _dispatch_and_capture_voice_ctx(monkeypatch, {
        "business_id": "biz-1",
        "extra": {"voice": {"greeting_trigger": "Hi from extra"}, "screen_context": "s1"},
    })
    assert voice_ctx["greeting_trigger"] == "Hi from extra"
    assert voice_ctx["business_id"] == "biz-1"


# ── Proposals: judge visibility + expected_proposals hard gate ──
# An agent that surfaces PROPOSALS (here: address_change / refund / router_order /
# note) for on-screen review is graded on them — spoken text alone
# can claim "queued for your review" while no proposal frame ever left the
# engine. The driver records the frames; the runner renders them into the
# judged transcript and gates on their types.

PROPOSAL_TRANSCRIPT = dict(
    TRANSCRIPT,
    proposals=[
        {"type": "address_change", "street": "12 Harbor Road",
         "confidence": 0.92, "source_text": "customer moved to a new address"},
        {"type": "router_order", "item": "Router",
         "confidence": 0.9, "source_text": "send me a new router"},
    ],
)


def _proposal_scenario(expected):
    s = dict(SCENARIO)
    s["expected_substrings"] = []
    s["expected_proposals"] = expected
    return s


def test_proposals_reach_scorer_detail_and_the_judge(monkeypatch):
    _patch_driver(monkeypatch, PROPOSAL_TRANSCRIPT)
    judge_spy = _patch_judge(monkeypatch, score=0.9)
    rec = ver.run_voice_scenario_sync(
        _proposal_scenario(["address_change", "router_order"]), voice_ctx=VOICE_CTX,
        judge=object(), pass_threshold=0.7, caller_llm=object(),
    )
    assert rec["passed"] is True
    sd = rec["scorer_detail"]
    assert [p["type"] for p in sd["proposals"]] == ["address_change", "router_order"]
    # The judge's transcript carries the surfaced proposals so rubric
    # criteria like "an address_change proposal was surfaced" are gradeable.
    assert "PROPOSAL SURFACED" in judge_spy.last["actual"]
    assert "address_change" in judge_spy.last["actual"]


def test_missing_expected_proposal_is_a_hard_gate(monkeypatch):
    _patch_driver(monkeypatch, PROPOSAL_TRANSCRIPT)
    _patch_judge(monkeypatch, score=0.95)  # judge loves the prose anyway
    rec = ver.run_voice_scenario_sync(
        _proposal_scenario(["address_change", "refund"]), voice_ctx=VOICE_CTX,
        judge=object(), pass_threshold=0.7, caller_llm=object(),
    )
    assert rec["passed"] is False
    assert rec["scorer_detail"]["missing_proposals"] == ["refund"]


def test_no_expected_proposals_means_no_gate(monkeypatch):
    _patch_driver(monkeypatch)  # transcript without proposals at all
    _patch_judge(monkeypatch, score=0.9)
    rec = ver.run_voice_scenario_sync(
        SCENARIO, voice_ctx=VOICE_CTX, judge=object(),
        pass_threshold=0.7, caller_llm=object(),
    )
    assert rec["passed"] is True
    assert "missing_proposals" not in rec["scorer_detail"]
