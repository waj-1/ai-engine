"""admin/seed_voice_scenarios.py seeds what a plugin's voice_scenario_seeds
hook supplies, insert-only on slug; a build with no plugin seeds nothing."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from admin import seed_voice_scenarios as seeder


def _client(existing_slugs):
    client = MagicMock()
    table = client.table.return_value
    table.select.return_value.execute.return_value.data = [{"slug": s} for s in existing_slugs]
    return client


def test_no_plugin_seeds_nothing_and_touches_no_db(monkeypatch):
    monkeypatch.setattr("core.plugins.load_hook", lambda name: [])
    boom = MagicMock(side_effect=AssertionError("no DB without scenarios"))
    monkeypatch.setattr("core.auth.get_service_client", boom)
    assert seeder.run() == {"created": 0, "skipped": 0}


def test_plugin_scenarios_are_inserted_once(monkeypatch):
    hook = SimpleNamespace(voice_scenario_specs=lambda: [
        {"slug": "front_desk_greeting", "agent_kind": "custom"},
        {"slug": "front_desk_question", "agent_kind": "custom"},
    ])
    monkeypatch.setattr("core.plugins.load_hook", lambda name: [hook] if name == "voice_scenario_seeds" else [])
    client = _client(["front_desk_greeting"])
    monkeypatch.setattr("core.auth.get_service_client", lambda: client)
    assert seeder.run() == {"created": 1, "skipped": 1}
    client.table.return_value.insert.assert_called_once_with(
        {"slug": "front_desk_question", "agent_kind": "custom"})
