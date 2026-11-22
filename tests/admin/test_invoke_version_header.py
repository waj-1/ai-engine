"""Task 1.5: profile_version_id threads through the eval runner.

- `_invoke_endpoint` sends `X-WAJ-Profile-Version` when a version id is
  passed, and omits it otherwise (back-compat with every existing caller).
- `start_suite_run` (admin/services/evals.py) treats a version-pinned run
  as a comparison run — same evaluator-model judge-guard as model_override,
  same error message — so a bake-off variant never scores ungraded.
"""
from unittest.mock import patch, MagicMock

import pytest

import admin.evals as ev


def test_invoke_endpoint_sends_version_header():
    captured = {}

    class FakeStream:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def iter_text(self):
            return iter(['data: {"type":"answer","content":"ok"}\n\n'])

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def stream(self, method, url, json, headers):
            captured.update(headers)
            return FakeStream()

    with patch.object(ev.httpx, "Client", FakeClient):
        ev._invoke_endpoint(base_url="http://x", endpoint="/e",
                            scenario={"question": "q"}, user_token="jwt",
                            profile_version_id="ver-7", metrics={})
    assert captured.get("X-WAJ-Profile-Version") == "ver-7"


def test_invoke_endpoint_omits_header_without_version():
    captured = {}

    class FakeStream:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def iter_text(self): return iter(['data: {"type":"answer","content":"ok"}\n\n'])

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def stream(self, method, url, json, headers):
            captured.update(headers); return FakeStream()

    with patch.object(ev.httpx, "Client", FakeClient):
        ev._invoke_endpoint(base_url="http://x", endpoint="/e",
                            scenario={"question": "q"}, user_token="jwt", metrics={})
    assert "X-WAJ-Profile-Version" not in captured


def _fake_client_for(version_rows=None, suite_rows=None, run_rows=None,
                      profile_rows=None, model_rows=None):
    """Table-aware fake Supabase client. `start_suite_run` queries
    `agent_profile_versions` (version ownership guard) and
    `agent_eval_suites` (evaluator-model judge-guard) independently, so a
    single shared fake_table (as the old version of this test used) can't
    express "version row present, suite row absent" and vice versa.

    `profile_rows` / `model_rows` back the I3 gen-config-only base-model
    resolution path (`agent_profiles.model_main_id` -> `agent_models` row).
    """
    versions_table = MagicMock()
    versions_table.select.return_value.eq.return_value.limit.return_value.execute.return_value = (
        MagicMock(data=version_rows if version_rows is not None else [])
    )
    suites_table = MagicMock()
    suites_table.select.return_value.eq.return_value.limit.return_value.execute.return_value = (
        MagicMock(data=suite_rows if suite_rows is not None else [])
    )
    runs_table = MagicMock()
    runs_table.insert.return_value.execute.return_value = (
        MagicMock(data=run_rows if run_rows is not None else [{"run_id": "run-1"}])
    )
    profiles_table = MagicMock()
    profiles_table.select.return_value.eq.return_value.limit.return_value.execute.return_value = (
        MagicMock(data=profile_rows if profile_rows is not None else [])
    )
    models_table = MagicMock()
    models_table.select.return_value.eq.return_value.limit.return_value.execute.return_value = (
        MagicMock(data=model_rows if model_rows is not None else [])
    )

    def table(name):
        return {
            "agent_profile_versions": versions_table,
            "agent_eval_suites": suites_table,
            "agent_eval_runs": runs_table,
            "agent_profiles": profiles_table,
            "agent_models": models_table,
        }.get(name, MagicMock())

    fake_client = MagicMock()
    fake_client.table.side_effect = table
    return fake_client


def test_start_suite_run_profile_version_requires_evaluator_model():
    """A version-pinned run (profile_version_id set) is a comparison run
    just like a model_override run — it must be refused up front when the
    suite has no evaluator model, with the same error message, so a
    bake-off variant never silently scores ungraded."""
    from admin.services import evals as svc

    # Version row matches the passed profile_id so the *ownership* guard
    # passes and it's the judge-guard (no evaluator_model_id) that fires.
    fake_client = _fake_client_for(
        version_rows=[{"version_id": "ver-9", "profile_id": "p"}],
        suite_rows=[],  # no evaluator_model_id on the suite
    )

    with patch.object(svc, "admin_service_client", return_value=fake_client):
        with pytest.raises(ValueError, match="comparison runs require a model judge"):
            svc.start_suite_run(
                suite_id="s", profile_id="p", business_id=None,
                actor_user_id=None, user_token=None,
                profile_version_id="ver-9",
            )


def test_start_suite_run_rejects_version_belonging_to_another_profile():
    """profile_id=A + profile_version_id=<a version of profile B> must be
    refused server-side — otherwise agent_eval_runs.profile_id and every UI
    label say A while load_version's snapshot silently executes B's config
    (silent misattribution). The suite HAS an evaluator model so this
    ownership guard is what fires, not the judge-guard."""
    from admin.services import evals as svc

    fake_client = _fake_client_for(
        version_rows=[{"version_id": "ver-9", "profile_id": "other-profile"}],
        suite_rows=[{"evaluator_model_id": "m1"}],
    )

    with patch.object(svc, "admin_service_client", return_value=fake_client):
        with pytest.raises(ValueError, match="does not belong to the selected profile"):
            svc.start_suite_run(
                suite_id="s", profile_id="p", business_id=None,
                actor_user_id=None, user_token=None,
                profile_version_id="ver-9",
            )


def test_start_suite_run_rejects_unknown_profile_version_id():
    """An unknown/deleted profile_version_id must fail closed with a clear
    error rather than silently proceeding (or 500ing further down)."""
    from admin.services import evals as svc

    fake_client = _fake_client_for(
        version_rows=[],  # no such version row
        suite_rows=[{"evaluator_model_id": "m1"}],
    )

    with patch.object(svc, "admin_service_client", return_value=fake_client):
        with pytest.raises(ValueError, match="profile version ver-missing not found"):
            svc.start_suite_run(
                suite_id="s", profile_id="p", business_id=None,
                actor_user_id=None, user_token=None,
                profile_version_id="ver-missing",
            )


def test_start_suite_run_proceeds_when_version_matches_profile():
    """The happy path: version belongs to the profile and the suite has an
    evaluator model — no raise, run row inserted, job dispatched."""
    from admin.services import evals as svc
    import admin._jobs as _jobs_mod

    fake_client = _fake_client_for(
        version_rows=[{"version_id": "ver-9", "profile_id": "p"}],
        suite_rows=[{"evaluator_model_id": "m1"}],
    )

    captured_kwargs: dict = {}

    def fake_start_tracked_job(fn, **kwargs):
        captured_kwargs.update(kwargs)
        return "job-1"

    with patch.object(svc, "admin_service_client", return_value=fake_client), \
         patch.object(_jobs_mod, "start_tracked_job", fake_start_tracked_job):
        run_id = svc.start_suite_run(
            suite_id="s", profile_id="p", business_id=None,
            actor_user_id=None, user_token=None,
            profile_version_id="ver-9",
        )

    assert run_id == "run-1"
    assert captured_kwargs.get("profile_version_id") == "ver-9"


# ---------------------------------------------------------------------------
# I3: gen-config-only variants (no explicit model_id) must still transmit a
# model_override — otherwise the "variant" silently runs baseline and the
# matrix shows a difference that never existed.
# ---------------------------------------------------------------------------


def test_start_suite_run_gen_config_only_resolves_version_model():
    """gen_config + profile_version_id (no model_id): the override is built
    from the PINNED VERSION's snapshot model_main, not the profile's live
    model — since the version is what actually executes."""
    from admin.services import evals as svc
    import admin._jobs as _jobs_mod

    fake_client = _fake_client_for(
        version_rows=[{
            "version_id": "ver-9", "profile_id": "p",
            "snapshot": {"model_main": {
                "name": "Sonnet (pinned)", "provider": "anthropic",
                "model_string": "claude-sonnet-4", "temperature": 0.2,
                "extra_config": {},
            }},
        }],
        suite_rows=[{"evaluator_model_id": "m1"}],
    )

    captured_kwargs: dict = {}

    def fake_start_tracked_job(fn, **kwargs):
        captured_kwargs.update(kwargs)
        return "job-1"

    with patch.object(svc, "admin_service_client", return_value=fake_client), \
         patch.object(_jobs_mod, "start_tracked_job", fake_start_tracked_job):
        run_id = svc.start_suite_run(
            suite_id="s", profile_id="p", business_id=None,
            actor_user_id=None, user_token=None,
            profile_version_id="ver-9",
            gen_config={"temperature": 0.9},
        )

    assert run_id == "run-1"
    # start_suite_run doesn't hand run_context back directly -- assert via
    # the insert payload the fake_client's runs_table captured.
    insert_call = fake_client.table("agent_eval_runs").insert.call_args
    context = insert_call.args[0]["context"]
    assert context["model_override"]["temperature"] == 0.9
    assert context["model_override"]["model_string"] == "claude-sonnet-4"
    assert context["model_label"] == "Sonnet (pinned)"


def test_start_suite_run_gen_config_only_resolves_profile_model():
    """gen_config alone (no model_id, no profile_version_id): the override
    is built from the RUN's PROFILE's own model_main."""
    from admin.services import evals as svc
    import admin._jobs as _jobs_mod

    fake_client = _fake_client_for(
        suite_rows=[{"evaluator_model_id": "m1"}],
        profile_rows=[{"model_main_id": "m-main"}],
        model_rows=[{
            "name": "Flash (profile default)", "provider": "google_genai",
            "model_string": "gemini-3-flash", "temperature": 0.3,
            "extra_config": {},
        }],
    )

    captured_kwargs: dict = {}

    def fake_start_tracked_job(fn, **kwargs):
        captured_kwargs.update(kwargs)
        return "job-1"

    with patch.object(svc, "admin_service_client", return_value=fake_client), \
         patch.object(_jobs_mod, "start_tracked_job", fake_start_tracked_job):
        run_id = svc.start_suite_run(
            suite_id="s", profile_id="p", business_id=None,
            actor_user_id=None, user_token=None,
            gen_config={"temperature": 0.9},
        )

    assert run_id == "run-1"
    insert_call = fake_client.table("agent_eval_runs").insert.call_args
    context = insert_call.args[0]["context"]
    assert context["model_override"]["temperature"] == 0.9
    assert context["model_override"]["model_string"] == "gemini-3-flash"
    assert context["model_label"] == "Flash (profile default)"


def test_start_suite_run_gen_config_only_no_resolvable_model_raises():
    """No model_id, no profile_version_id, and the profile has no
    model_main_id either -> raise a clear error rather than silently
    running baseline."""
    from admin.services import evals as svc

    fake_client = _fake_client_for(
        suite_rows=[{"evaluator_model_id": "m1"}],
        profile_rows=[{"model_main_id": None}],
    )

    with patch.object(svc, "admin_service_client", return_value=fake_client):
        with pytest.raises(ValueError, match="no model could be resolved"):
            svc.start_suite_run(
                suite_id="s", profile_id="p", business_id=None,
                actor_user_id=None, user_token=None,
                gen_config={"temperature": 0.9},
            )


# ---------------------------------------------------------------------------
# Judge-free runner kinds (self-scoring) are exempt from the version-pin judge
# guard — fix/bakeoff-prod-usability.
# ---------------------------------------------------------------------------


def test_start_suite_run_self_scored_version_pin_needs_no_evaluator():
    """A self-scoring suite (deterministic item-id scorer) may be version-pinned
    without an evaluator model — the comparison judge-guard must not fire."""
    from admin.services import evals as svc
    import admin._jobs as _jobs_mod

    fake_client = _fake_client_for(
        version_rows=[{"version_id": "ver-9", "profile_id": "p"}],
        suite_rows=[{"evaluator_model_id": None, "runner_kind": "sample_scored"}],
    )

    def fake_start_tracked_job(fn, **kwargs):
        return "job-1"

    with patch.object(svc, "admin_service_client", return_value=fake_client), \
         patch.object(_jobs_mod, "start_tracked_job", fake_start_tracked_job):
        run_id = svc.start_suite_run(
            suite_id="s", profile_id="p", business_id=None,
            actor_user_id=None, user_token=None,
            profile_version_id="ver-9",
        )
    assert run_id == "run-1"  # proceeded, no raise


def test_start_suite_run_chat_version_pin_still_needs_evaluator():
    """Regression: a chat (judged) suite still requires an evaluator on a
    version-pinned run."""
    from admin.services import evals as svc

    fake_client = _fake_client_for(
        version_rows=[{"version_id": "ver-9", "profile_id": "p"}],
        suite_rows=[{"evaluator_model_id": None, "runner_kind": "chat"}],
    )
    with patch.object(svc, "admin_service_client", return_value=fake_client):
        with pytest.raises(ValueError, match="comparison runs require a model judge"):
            svc.start_suite_run(
                suite_id="s", profile_id="p", business_id=None,
                actor_user_id=None, user_token=None,
                profile_version_id="ver-9",
            )


@pytest.fixture(autouse=True)
def _sample_kinds(product_build):
    """Generic logic, fictional agent kinds (tests/conftest.py::product_build)."""
