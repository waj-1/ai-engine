from pathlib import Path

import pytest

MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
MIG = MIGRATIONS / "20260707120000_allow_template_profile_status.sql"
# The public tree ships one baseline migration instead of the history.
BASELINE = MIGRATIONS / "20261122000000_baseline.sql"


@pytest.mark.skipif(not MIG.exists(), reason="this tree has the schema baseline, not the migration history")
def test_migration_allows_template_status():
    sql = MIG.read_text()
    assert "agent_profiles_status_check" in sql
    assert "'template'" in sql
    # keeps the existing values
    for v in ("'active'", "'draft'", "'archived'"):
        assert v in sql
    assert "DROP CONSTRAINT IF EXISTS" in sql  # idempotent re-add


@pytest.mark.skipif(not BASELINE.exists(), reason="this tree has the migration history, not a baseline")
def test_baseline_allows_template_status():
    line = next(ln for ln in BASELINE.read_text().splitlines() if "agent_profiles_status_check CHECK" in ln)
    for v in ("'active'", "'draft'", "'archived'", "'template'"):
        assert v in line
