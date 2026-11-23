"""Testing (/admin/evals) fixes found on the public build, 2026-11-23.

1. A judge is built from ANY model the engine can call (a local Ollama row, an
   OpenAI-compatible base URL), through the same factory agents use. Before,
   such a judge fell through to the Gemini builder ("no Gemini key set") and
   every case was left ungraded.
   When no judge can be built, a case with only an expected answer is graded
   by its words; if they aren't in the reply it is NOT JUDGED (no score, left
   out of the run's score, the run page says why), never "Scored 0%, below
   the pass mark".
2. A refused run started from the run panel renders a portal page with a way
   back, not the API's raw JSON error.
3. The comparison pre-flight accepts exactly what the runner can build, and
   says so.
4. Model comparison durations read "1 min 26 s", not "85559 ms".
5. A case's display name: the Name field, a `name` in the case JSON or an MCP
   scenario, else the question.
6. The run forms' Business picker appears only when a plugin provides it.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from admin import e2e_runner, eval_handlers, evals, testing_view


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _fake_models_db(monkeypatch, rows):
    """core.auth.get_service_client → a client whose agent_models query
    returns the row(s) matching the eq() filters."""

    class Q:
        def __init__(self):
            self.filters = {}

        def select(self, *_a, **_k):
            return self

        def eq(self, col, val):
            self.filters[col] = val
            return self

        def limit(self, _n):
            return self

        def execute(self):
            data = [r for r in rows if all(r.get(k) == v for k, v in self.filters.items())]
            return MagicMock(data=data)

    class DB:
        def table(self, _name):
            return Q()

    import core.auth

    monkeypatch.setattr(core.auth, "get_service_client", lambda: DB())


def _answer(text):
    return lambda **_k: (text, "", 200, 20)


# ---------------------------------------------------------------------------
# 1. any configured model is a judge
# ---------------------------------------------------------------------------


def test_a_local_ollama_model_builds_a_judge(monkeypatch):
    """The demo's judge: an Ollama row, on an install with no Gemini key."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    from runtime import llm_factory

    llm_factory.clear_llm_cache()
    _fake_models_db(monkeypatch, [{
        "model_id": "m-local", "name": "Local llama", "provider": "ollama",
        "model_string": "llama3.2", "is_active": True,
        "extra_config": {"base_url": "http://127.0.0.1:11434/v1"},
    }])
    judge = e2e_runner._try_build_judge("llama3.2", model_id="m-local")
    assert judge is not None
    assert getattr(judge, "model_name", None) == "llama3.2"
    assert "11434" in str(getattr(judge, "openai_api_base", "") or getattr(judge, "base_url", ""))
    assert e2e_runner.judge_unavailable_reason("llama3.2", "m-local") == ""


def test_an_openai_compatible_row_builds_a_judge(monkeypatch):
    built = {}
    import runtime.llm_factory as lf

    monkeypatch.setattr(lf, "build_chat_model", lambda m: built.update(m) or "COMPAT_JUDGE")
    _fake_models_db(monkeypatch, [{
        "model_id": "m-vllm", "provider": "openai", "model_string": "qwen3-8b",
        "is_active": True, "extra_config": {"base_url": "http://10.0.0.5:8000/v1"},
    }])
    assert e2e_runner._try_build_judge("qwen3-8b") == "COMPAT_JUDGE"
    assert built["temperature"] == 0.0 and built["max_output_tokens"] == 4096
    assert built["extra_config"]["base_url"] == "http://10.0.0.5:8000/v1"


def test_a_model_with_no_row_says_why(monkeypatch):
    _fake_models_db(monkeypatch, [])
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert e2e_runner._try_build_judge("llama3.2") is None
    assert "not a model this engine has set up" in e2e_runner.judge_unavailable_reason("llama3.2")


def test_a_gemini_judge_without_a_key_says_where_to_add_one(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr("core.provider_keys.env_or_stored", lambda _n: "")
    why = e2e_runner.judge_unavailable_reason("gemini-3.5-flash")
    assert "Google API key" in why and "Model keys" in why


def test_without_a_judge_the_expected_words_grade_the_case(monkeypatch):
    """The reported case: the expected words WERE in the answer."""
    monkeypatch.setattr(evals, "_invoke_endpoint", _answer(
        "Yes: there is an outage reported in Old Town since 9:00."))
    res = evals._run_scenario(
        {"id": "s", "question": "Is there an outage in old town?",
         "expected_answer": "outage old town"},
        profile_id="p", base_url="http://x", endpoint="/e", judge=None,
        pass_threshold=0.7, judge_note="the judge model llama3.2 is not a model this engine has set up")
    assert res["passed"] is True and not res.get("ungraded")


def test_an_unmatched_answer_with_no_judge_is_not_judged_not_zero(monkeypatch):
    monkeypatch.setattr(evals, "_invoke_endpoint", _answer("We are looking into it."))
    why = "the judge model llama3.2 is not a model this engine has set up"
    res = evals._run_scenario(
        {"id": "s", "question": "Is there an outage?",
         "expected_answer": "Yes, an outage in Old Town; engineers are on site"},
        profile_id="p", base_url="http://x", endpoint="/e", judge=None,
        pass_threshold=0.7, judge_note=why)
    assert res["ungraded"] is True and res["score"] is None and res["passed"] is False
    assert why in res["grading_note"]
    reason = testing_view.case_reason(res)
    assert reason.startswith("Not judged") and "Scored" not in reason


def test_expected_tools_still_grade_a_case_with_no_judge(monkeypatch):
    monkeypatch.setattr(evals, "_invoke_endpoint", lambda **k: (
        k["metrics"].update({"executed_tools": ["lookup_outages"]}) or ("Some answer", "", 200, 5)))
    res = evals._run_scenario(
        {"id": "s", "question": "q", "expected_answer": "an outage in Old Town",
         "expected_tools": ["lookup_outages"]},
        profile_id="p", base_url="http://x", endpoint="/e", judge=None, pass_threshold=0.7)
    assert res["passed"] is True and not res.get("ungraded")
    assert "not judged" in res["grading_note"]


def _run_suite(monkeypatch, scenarios, answer, *, evaluator_model_id=None, models=()):
    """Drive run_eval_suite_sync end to end on fakes; return the finalize kwargs."""
    run = {"run_id": "r1", "profile_id": "p1", "context": {},
           "agent_eval_suites": {"suite_id": "s1", "name": "outages", "agent_kind": "custom",
                                 "scenarios": scenarios, "pass_threshold": 0.7,
                                 "evaluator_model_id": evaluator_model_id}}
    finalized = {}

    def table(name):
        q = MagicMock()
        for m in ("select", "eq", "limit", "update", "insert", "order", "in_"):
            getattr(q, m).return_value = q
        data = {"agent_eval_runs": [run], "agent_models": list(models)}.get(name, [])
        q.execute.return_value = MagicMock(data=data)
        return q

    client = MagicMock()
    client.table.side_effect = table
    monkeypatch.setattr(evals, "admin_service_client", lambda: client)
    monkeypatch.setattr(evals, "get_service_client", lambda: client)
    monkeypatch.setattr(evals, "_resolve_profile", lambda *_a: {
        "profile_id": "p1", "agent_kind": "sample_chat", "endpoint_path": "/sample/chat",
        "environment": "staging"})
    monkeypatch.setattr(evals, "_invoke_endpoint", _answer(answer))
    monkeypatch.setattr(evals, "_finalize", lambda _c, _r, **kw: finalized.update(kw))
    evals.run_eval_suite_sync("r1")
    return finalized


def test_a_run_whose_cases_cannot_be_judged_says_so(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    _fake_models_db(monkeypatch, [])
    out = _run_suite(monkeypatch, [
        {"id": "a", "question": "Outage?", "expected_answer": "Yes, engineers are on site now"},
    ], "We are looking into it.")
    assert out["status"] == "errored", out
    assert "No case could be judged" in out["error"]
    assert out["results"][0]["ungraded"] is True


def test_not_judged_cases_stay_out_of_the_run_score(monkeypatch):
    _fake_models_db(monkeypatch, [])
    out = _run_suite(monkeypatch, [
        {"id": "a", "question": "Outage?", "expected_answer": "outage old town"},
        {"id": "b", "question": "When fixed?", "expected_answer": "Engineers expect it fixed by noon"},
    ], "There is an outage in Old Town.")
    assert out["status"] == "passed" and out["score"] == 1.0, out


def test_run_page_shows_not_judged_instead_of_a_failure():
    from admin.router import templates

    results = [
        {"scenario_id": "a", "name": "a", "question": "Is there an outage in old town?",
         "passed": False, "score": None, "ungraded": True,
         "grading_note": "Not judged: no judge model is set on this suite, and the reply does not contain the expected words."},
        {"scenario_id": "b", "name": "Opening hours", "question": "When are you open?",
         "passed": True, "score": 1.0},
    ]
    html = templates.get_template("eval_run_detail.html").render(
        ctx={"email": "t@example.test"}, active="evals", current_env="dev",
        run={"run_id": "11111111-2222-3333-4444-555555555555", "status": "passed", "score": 1.0,
             "results": results, "started_at": None, "finished_at": None, "suite_id": None,
             "context": {}},
        run_kpis=None, chart_data=[], lmnr_spans=[], sibling_runs=[], cancel_job_id=None,
        asset_version="1")
    assert "Scored 0%" not in html
    assert "Not judged" in html and "1 case could not be judged" in html
    assert "no judge model is set on this suite" in html
    # Case names: an explicit name, else the question.
    assert "Opening hours" in html and "Is there an outage in old town?" in html


def test_e2e_runner_leaves_not_judged_cases_out_of_the_score(monkeypatch):
    class _Resp:
        status_code = 200
        text = "We are looking into it."

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(e2e_runner.httpx, "Client", _Client)
    out = e2e_runner._run_one_scenario("http://x", "/e", {
        "id": "s", "question": "q", "expected_answer": "engineers are on site"}, None, 0.7)
    assert out["judge"]["ungraded"] is True and "score" not in out["judge"]


# ---------------------------------------------------------------------------
# 2 + 3. comparison runs: what the check accepts, and how a refusal looks
# ---------------------------------------------------------------------------


def _models_client(rows):
    q = MagicMock()
    q.select.return_value = q
    q.eq.return_value = q
    q.limit.return_value = q
    q.execute.return_value = MagicMock(data=rows)
    client = MagicMock()
    client.table.return_value = q
    return client


_JUDGED_SUITE = {"name": "outages", "runner_kind": "chat",
                 "scenarios": [{"id": "a", "question": "q", "expected_answer": "x"}]}


def test_the_comparison_check_accepts_a_local_judge():
    from admin.services.eval_scorers import suite_judge_problem

    client = _models_client([{"model_id": "m", "name": "Local llama", "provider": "ollama"}])
    assert suite_judge_problem(client, {**_JUDGED_SUITE, "evaluator_model_id": "m"}) is None


def test_the_comparison_check_names_what_is_wrong():
    from admin.services.eval_scorers import suite_judge_problem

    assert suite_judge_problem(_models_client([]), _JUDGED_SUITE) == "no judge model is set"
    client = _models_client([{"model_id": "m", "name": "Bedrock thing", "provider": "bedrock"}])
    assert "cannot call" in suite_judge_problem(client, {**_JUDGED_SUITE, "evaluator_model_id": "m"})


def test_the_comparison_error_no_longer_says_gemini_or_claude():
    from admin.services import bakeoffs

    suites = MagicMock()
    for m in ("select", "in_", "eq", "limit"):
        getattr(suites, m).return_value = suites
    suites.execute.return_value = MagicMock(data=[{"suite_id": "s1", **_JUDGED_SUITE}])
    client = MagicMock()
    client.table.return_value = suites
    with patch("admin.services.bakeoffs.admin_service_client", return_value=client):
        with pytest.raises(ValueError) as exc:
            bakeoffs.create_bakeoff(name="bo", profile_id="p1",
                                    variants=[{"label": "A"}, {"label": "B"}],
                                    suite_ids=["s1"], repeats=1,
                                    actor_user_id="u", user_token="t")
    msg = str(exc.value)
    assert "Gemini or Claude" not in msg
    assert "any model this engine can call" in msg and "outages" in msg


def _trigger_client():
    from fastapi.testclient import TestClient

    from admin import auth, eval_runs_handlers
    from main import app

    app.dependency_overrides[auth.require_superuser] = lambda: {
        "user_token": "jwt", "user_id": "u-1", "email": "t@example.test"}
    rec = patch.object(eval_runs_handlers, "record_change")
    rec.start()

    def teardown():
        rec.stop()
        app.dependency_overrides.pop(auth.require_superuser, None)

    return TestClient(app), teardown


def test_a_refused_run_from_the_panel_is_a_portal_page():
    from admin import eval_runs_handlers

    client, teardown = _trigger_client()
    try:
        with patch.object(eval_runs_handlers, "_start_suite_run",
                          side_effect=ValueError("comparison runs require a model judge on the suite, "
                                                 "and no judge model is set. Pick one.")):
            r = client.post("/admin/eval-runs/trigger",
                            data={"source": "suite", "profile_id": "p1", "suite_id": "s1",
                                  "model_id": "m2"},
                            headers={"accept": "text/html",
                                     "referer": "http://testserver/admin/evals/suites/s1"},
                            follow_redirects=False)
        assert r.status_code == 400
        assert r.headers["content-type"].startswith("text/html")
        assert '{"error"' not in r.text
        assert "This run can&#39;t start" in r.text or "This run can't start" in r.text
        assert "no judge model is set" in r.text
        assert "/admin/evals/suites/s1?run=1" in r.text
        # An API caller still gets the JSON error.
        with patch.object(eval_runs_handlers, "_start_suite_run", side_effect=ValueError("nope")):
            r = client.post("/admin/eval-runs/trigger",
                            data={"source": "suite", "profile_id": "p1", "suite_id": "s1"},
                            headers={"accept": "application/json"}, follow_redirects=False)
        assert r.status_code == 400 and r.json()["error"]["message"] == "nope"
    finally:
        teardown()


# ---------------------------------------------------------------------------
# 4. durations for people
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value,text", [
    (850, "850 ms"), (8400, "8.4 s"), (59_900, "59.9 s"), (59_990, "1 min 0 s"),
    (85_559, "1 min 26 s"), (3_725_000, "1 h 2 min"), (None, "—"),
])
def test_ms_reads_like_the_rest_of_the_portal(value, text):
    assert testing_view.ms(value) == text


def test_model_comparison_latency_is_not_raw_milliseconds():
    from admin.eval_runs_handlers import _fmt_metric

    assert _fmt_metric(85559, "ms") == "1 min 26 s"
    assert _fmt_metric(8412.4, "ms") == "8.4 s"


# ---------------------------------------------------------------------------
# 5. case names
# ---------------------------------------------------------------------------


def test_case_display_name():
    name = eval_handlers.case_display_name
    assert name({"scenario_id": "x1", "name": "Outage check", "question": "Is it down?"}) == "Outage check"
    # The runner records name = id when the case has none: not a real name.
    assert name({"scenario_id": "is_there_an_outage", "name": "is_there_an_outage",
                 "question": "Is there an outage in old town?"}) == "Is there an outage in old town?"
    assert name({"id": "opening_hours"}) == "Opening hours"


class _SuiteSaveClient:
    def __init__(self, row):
        self.row = row
        self.updated = None

    def table(self, _name):
        outer = self
        q = MagicMock()
        for m in ("select", "eq", "limit", "order", "in_"):
            getattr(q, m).return_value = q

        def update(payload):
            outer.updated = payload
            return q

        q.update.side_effect = update
        q.execute.return_value = MagicMock(data=[self.row])
        return q


def _save_suite(form):
    from fastapi.testclient import TestClient

    from admin import auth
    from main import app

    fake = _SuiteSaveClient({"suite_id": "s1", "name": "outages", "agent_kind": "custom",
                             "scenarios": [], "pass_threshold": 0.7, "is_active": True})
    app.dependency_overrides[auth.require_superuser] = lambda: {
        "user_token": "jwt", "user_id": "u-1", "email": "t@example.test"}
    try:
        with patch.object(eval_handlers, "admin_service_client", return_value=fake), \
             patch.object(eval_handlers, "record_change", create=True):
            r = TestClient(app).post("/admin/evals/suites/s1", data=form, follow_redirects=False)
    finally:
        app.dependency_overrides.pop(auth.require_superuser, None)
    assert r.status_code in (302, 303), r.text[:300]
    return fake.updated["scenarios"]


def test_the_add_case_name_field_sets_the_display_name():
    [sc] = _save_suite({"name": "outages", "new_scenario_question": "Is there an outage in old town?",
                        "new_scenario_name": "Outage in Old Town"})
    assert sc["name"] == "Outage in Old Town"
    assert sc["id"] == "outage_in_old_town"
    assert sc["question"] == "Is there an outage in old town?"


def test_a_name_in_the_case_json_is_honoured():
    [sc] = _save_suite({"name": "outages", "new_scenario_question": "Is there an outage?",
                        "new_scenario_json": '{"name": "Outage (JSON)"}'})
    assert sc["name"] == "Outage (JSON)"


def test_an_existing_case_can_be_renamed_and_unnamed():
    [sc] = _save_suite({"name": "outages", "scenario_ids": "a", "scenario_question_0": "q?",
                        "scenario_extra_0": '{"name": "Old"}', "scenario_name_0": "New name"})
    assert sc["name"] == "New name"
    [sc] = _save_suite({"name": "outages", "scenario_ids": "a", "scenario_question_0": "q?",
                        "scenario_extra_0": "{}", "scenario_name_0": ""})
    assert "name" not in sc


def test_mcp_scenarios_keep_their_name():
    from admin_mcp.tools_test import _clean_scenarios

    cleaned, why = _clean_scenarios([{"question": "Is there an outage?", "name": "Outage check"}])
    assert why is None
    assert cleaned[0]["name"] == "Outage check" and cleaned[0]["id"] == "outage_check"


# ---------------------------------------------------------------------------
# 6. the Business picker comes from a plugin
# ---------------------------------------------------------------------------


def _render_run_dialog():
    from admin.router import templates

    return templates.get_template("_run_dialog.html").render(
        profiles=[], suites=[], models=[], businesses=[], agent_kinds=[],
        default_expectations="", default_expectations_by_kind={},
        profile_versions_by_profile={})


def test_no_plugin_no_business_field(monkeypatch):
    import engine_plugins

    monkeypatch.setattr(engine_plugins, "PLUGINS", ())
    html = _render_run_dialog()
    assert "Business" not in html and 'name="business_id"' not in html


def test_a_plugin_names_the_business_field(monkeypatch):
    from admin import eval_context

    monkeypatch.setattr(eval_context, "business_field_label", lambda: "Business")
    from admin.router import templates

    monkeypatch.setitem(templates.env.globals, "business_field_label", lambda: "Business")
    html = _render_run_dialog()
    assert "Business (optional)" in html and 'name="business_id"' in html
