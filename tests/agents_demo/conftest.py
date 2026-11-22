"""Shared setup for the AI-agents demo tests."""
import pytest


@pytest.fixture(autouse=True)
def _a_key_the_demo_can_run_on(monkeypatch):
    """The demo seeder only seeds where its brain model has a key
    (admin/seed_agents_demo.run); the tests that check the skip unset it."""
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
