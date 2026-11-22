# ai_agent/tests/admin/test_served_version_migration.py
import pytest
from dotenv import load_dotenv
load_dotenv()
from core.auth import get_service_client


@pytest.fixture
def client():
    try:
        c = get_service_client()
        c.table("agent_profiles").select("profile_id").limit(1).execute()
        return c
    except Exception as exc:
        pytest.skip(f"AI Supabase not reachable: {exc}")


def test_agent_profiles_has_served_version_id(client):
    # selecting the new column explicitly 400s if it doesn't exist
    client.table("agent_profiles").select("profile_id, served_version_id").limit(1).execute()
