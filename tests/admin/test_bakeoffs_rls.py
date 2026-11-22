"""RLS check for `agent_bakeoffs` (Task 0.2). Runs against the local AI
Supabase stack (:54421) with the anon key — asserts a client JWT cannot
read bake-off rows once the service-role-only policy is enabled.

Mirrors tests/admin/test_eval_schema_migration.py's env loading: the
autouse `_stub_env` fixture in conftest.py only stubs the MAIN-project
vars, so AI_SUPABASE_URL / AI_SUPABASE_ANON_KEY must come from the
worktree's ai_agent/.env via an explicit load_dotenv() here."""
import os

from dotenv import load_dotenv

load_dotenv()

import pytest
from postgrest.exceptions import APIError
from supabase import create_client


@pytest.fixture
def anon():
    url = os.getenv("AI_SUPABASE_URL")
    key = os.getenv("AI_SUPABASE_ANON_KEY")
    if not (url and key):
        pytest.skip("AI anon creds not set")

    c = create_client(url, key)
    try:
        c.table("agent_bakeoffs").select("bakeoff_id").limit(1).execute()
    except APIError:
        # Permission denied (42501) proves stack IS up — return client for test to verify RLS
        return c
    except Exception as exc:  # connection errors: dead port in CI, stack not up
        pytest.skip(f"AI Supabase not reachable: {exc}")

    return c


def test_anon_cannot_read_bakeoffs(anon):
    # RLS service-role-only -> anon select returns zero rows, or PostgREST
    # denies the query outright. Both outcomes prove anon never sees data;
    # only an unhandled exception or a non-empty payload should fail this.
    try:
        res = anon.table("agent_bakeoffs").select("bakeoff_id").limit(1).execute()
    except APIError as exc:
        assert exc.code == "42501", f"expected permission-denied, got {exc}"
    else:
        assert res.data == []
