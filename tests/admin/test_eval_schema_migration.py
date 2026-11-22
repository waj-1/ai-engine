"""Schema presence checks for the eval-screen migration. Runs against the
local AI Supabase stack (:54421) via the service client used by admin code.
Skips cleanly when the stack isn't up so CI without a DB doesn't fail."""
from dotenv import load_dotenv

# core.auth.get_service_client() reads AI_SUPABASE_URL / AI_SUPABASE_SERVICE_KEY
# from the process env at call time (no module-level dotenv load of its own).
# tests/admin/conftest.py's autouse _stub_env fixture stubs the MAIN-project
# vars only, so this is the only place the AI-project creds get populated —
# load the worktree's ai_agent/.env before the client fixture runs.
load_dotenv()

import pytest
from core.auth import get_service_client


@pytest.fixture
def client():
    try:
        c = get_service_client()
        c.table("agent_eval_suites").select("suite_id").limit(1).execute()
        return c
    except Exception as exc:  # local AI stack not up
        pytest.skip(f"AI Supabase not reachable: {exc}")


def test_suite_has_profile_and_runner_kind(client):
    # Selecting the new columns explicitly 400s if they don't exist.
    client.table("agent_eval_suites").select("suite_id, profile_id, runner_kind").limit(1).execute()


def test_runs_have_new_columns(client):
    client.table("agent_eval_runs").select(
        "run_id, schedule_id, bakeoff_id, profile_version_id"
    ).limit(1).execute()


def test_bakeoffs_table_exists(client):
    client.table("agent_bakeoffs").select(
        "bakeoff_id, name, profile_id, variants, suite_ids, status, repeats, notes, created_at"
    ).limit(1).execute()
