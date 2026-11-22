from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import scripts._kb_target as kb_target
from scripts._kb_target import resolve_source_id


def _client(rows):
    client = MagicMock()
    builder = MagicMock()
    builder.execute.return_value.data = rows
    for m in ("select", "eq", "limit"):
        getattr(builder, m).return_value = builder
    client.table.return_value = builder
    return client


@pytest.fixture(autouse=True)
def sample_sources(monkeypatch):
    """A plugin that seeds two tagged sources, one with an alias tag."""
    hook = SimpleNamespace(
        data_source_specs=lambda: [
            {"name": "help_center_docs", "config": {"source_tag": "help-center"}},
            {"name": "release_notes", "config": {"source_tag": "release-notes"}},
            {"name": "untagged_table", "config": {}},
        ],
        kb_source_tags=lambda: {"release-blog": "release_notes"},
    )
    monkeypatch.setattr("core.plugins.load_hook", lambda name: [hook] if name == "data_source_seeds" else [])


def test_source_tags_come_from_the_seeded_sources_and_aliases():
    assert kb_target.source_tag_to_name() == {
        "help-center": "help_center_docs",
        "release-notes": "release_notes",
        "release-blog": "release_notes",
    }


def test_resolve_source_id_returns_match():
    client = _client([{"source_id": "id-help"}])
    assert resolve_source_id(client, "help-center") == "id-help"
    client.table.return_value.eq.assert_called_with("name", "help_center_docs")


def test_alias_tag_maps_to_its_source():
    client = _client([{"source_id": "id-rn"}])
    assert resolve_source_id(client, "release-blog") == "id-rn"
    client.table.return_value.eq.assert_called_with("name", "release_notes")


def test_resolve_source_id_raises_on_unknown_tag():
    client = _client([{"source_id": "id-x"}])
    with pytest.raises(SystemExit):
        resolve_source_id(client, "not-a-real-tag")


def test_resolve_source_id_raises_when_row_missing():
    client = _client([])
    with pytest.raises(SystemExit):
        resolve_source_id(client, "help-center")


def test_no_plugin_knows_no_tags(monkeypatch):
    monkeypatch.setattr("core.plugins.load_hook", lambda name: [])
    with pytest.raises(SystemExit):
        resolve_source_id(_client([{"source_id": "id"}]), "help-center")
