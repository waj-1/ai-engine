# tests/admin/test_profiles_service.py
"""update_profile_row()/fork_profile_row(): payload validation, before/after
audit, loader invalidation, fork field forcing — the service functions an
MCP configure/fork tool would call directly, without going through FastAPI.
"""
import pytest
from unittest.mock import MagicMock, patch

from admin.services import profiles
from admin.services.profiles import (
    ProfileNotFoundError,
    TemplateProtectedError,
    deep_merge_voice_config,
    fork_profile_row,
    update_profile_row,
)

PROFILE = {
    "profile_id": "p1",
    "agent_kind": "business",
    "slug": "main",
    "environment": "staging",
    "display_name": "Biz",
    "status": "active",
    "model_main_id": "m1",
    "is_active": True,
}


def _fake_client(*, select_data=None, insert_data=None):
    """MagicMock supabase client with independent select/insert/update
    builders, each returning its own canned execute() response — the
    generic single-canned-response fluent mock (test_promotion_service.py)
    can't tell select from insert on the same table, which matters here
    since fork does both against `agent_profiles` in one call."""
    select_builder = MagicMock()
    for m in ("select", "eq", "in_", "order", "limit"):
        getattr(select_builder, m).return_value = select_builder
    select_builder.execute.return_value = MagicMock(data=select_data or [])

    insert_builder = MagicMock()
    insert_builder.execute.return_value = MagicMock(data=insert_data or [])

    update_builder = MagicMock()
    update_builder.eq.return_value = update_builder
    update_builder.execute.return_value = MagicMock(data=[])

    table_mock = MagicMock()
    table_mock.select.return_value = select_builder
    table_mock.insert.return_value = insert_builder
    table_mock.update.return_value = update_builder

    client = MagicMock()
    client.table.return_value = table_mock
    return client, table_mock


def test_update_rejects_unknown_field():
    with pytest.raises(ValueError, match="unknown"):
        update_profile_row(profile_id="p1", payload={"nope": 1}, actor_user_id="u")


def test_update_missing_profile_raises():
    client, _ = _fake_client(select_data=[])
    with patch("core.auth.admin_service_client", return_value=client):
        with pytest.raises(ProfileNotFoundError):
            update_profile_row(profile_id="missing", payload={"display_name": "x"},
                               actor_user_id="u")


def test_update_happy_path_audits_update(monkeypatch):
    client, table_mock = _fake_client(select_data=[PROFILE])
    audits = []
    monkeypatch.setattr(profiles, "record_change", lambda **kw: audits.append(kw))

    with patch("core.auth.admin_service_client", return_value=client):
        result = update_profile_row(profile_id="p1", payload={"display_name": "New"},
                                    actor_user_id="u1")

    assert result["display_name"] == "New"
    assert result["agent_kind"] == "business"  # merged in from `before`
    table_mock.update.assert_called_once_with({"display_name": "New"})
    assert len(audits) == 1
    assert audits[0]["action"] == "update"
    assert audits[0]["entity_id"] == "p1"
    assert audits[0]["before"] == PROFILE
    assert audits[0]["after"]["display_name"] == "New"


def test_update_environment_change_invalidates_both_envs(monkeypatch):
    client, _ = _fake_client(select_data=[PROFILE])
    calls = []
    monkeypatch.setattr(profiles, "invalidate_profile_loader",
                        lambda kind, env: calls.append((kind, env)))
    monkeypatch.setattr(profiles, "record_change", lambda **kw: None)

    with patch("core.auth.admin_service_client", return_value=client):
        update_profile_row(profile_id="p1", payload={"environment": "prod"},
                           actor_user_id="u1")

    assert ("business", "staging") in calls  # original env
    assert ("business", "prod") in calls  # new env


def test_fork_missing_profile_raises():
    client, _ = _fake_client(select_data=[])
    with patch("core.auth.admin_service_client", return_value=client):
        with pytest.raises(ProfileNotFoundError):
            fork_profile_row(profile_id="missing", actor_user_id="u")


def test_fork_forces_staging_draft_and_suffixes_slug(monkeypatch):
    src = {**PROFILE, "environment": "dev"}
    client, table_mock = _fake_client(select_data=[src], insert_data=[])
    audits = []
    monkeypatch.setattr(profiles, "record_change", lambda **kw: audits.append(kw))

    with patch("core.auth.admin_service_client", return_value=client):
        result = fork_profile_row(profile_id="p1", actor_user_id="u1")

    assert result["environment"] == "staging"
    assert result["status"] == "draft"
    assert result["display_name"] == "Biz (fork)"
    assert result["slug"].startswith("main-fork-")
    assert result["profile_id"] != src["profile_id"]
    table_mock.insert.assert_called_once()
    assert len(audits) == 1
    assert audits[0]["action"] == "fork"
    assert audits[0]["before"] == src


def test_fork_with_display_name_and_slug(monkeypatch):
    src = {"profile_id": "t1", "agent_kind": "business", "slug": "mold:rag",
           "status": "template", "environment": "staging", "display_name": "RAG Mold",
           "tool_ids": ["x"], "downstream_profile_ids": []}
    client, table_mock = _fake_client(select_data=[src], insert_data=[])
    monkeypatch.setattr(profiles, "record_change", lambda **kw: None)

    with patch("core.auth.admin_service_client", return_value=client):
        out = fork_profile_row(profile_id="t1", actor_user_id="u",
                               display_name="Support Bot", slug="support-bot")

    assert out["display_name"] == "Support Bot"
    assert out["slug"].startswith("support-bot")   # uniqueness suffix allowed
    assert out["status"] == "draft"
    assert out["environment"] == "staging"


def test_fork_exact_slug_uses_slug_verbatim(monkeypatch):
    """exact_slug + an explicit slug -> the slug is used verbatim (no
    -fork- suffix), so a delegation specialist gets a predictable
    ask_<slug> tool name."""
    src = {"profile_id": "t1", "agent_kind": "business", "slug": "mold:blank",
           "status": "template", "environment": "staging", "display_name": "Blank",
           "tool_ids": [], "downstream_profile_ids": []}
    client, _ = _fake_client(select_data=[src], insert_data=[])
    monkeypatch.setattr(profiles, "record_change", lambda **kw: None)

    with patch("core.auth.admin_service_client", return_value=client):
        out = fork_profile_row(profile_id="t1", actor_user_id="u",
                               display_name="Reporting", slug="business_reporting",
                               exact_slug=True)

    assert out["slug"] == "business_reporting"   # verbatim, no suffix


def test_fork_exact_slug_ignored_without_slug(monkeypatch):
    """exact_slug with NO slug still suffixes the source slug (uniqueness)."""
    src = {**PROFILE, "environment": "dev"}
    client, _ = _fake_client(select_data=[src], insert_data=[])
    monkeypatch.setattr(profiles, "record_change", lambda **kw: None)

    with patch("core.auth.admin_service_client", return_value=client):
        out = fork_profile_row(profile_id="p1", actor_user_id="u1", exact_slug=True)

    assert out["slug"].startswith("main-fork-")


def test_update_refuses_template():
    client, _ = _fake_client(select_data=[
        {"profile_id": "t1", "status": "template", "agent_kind": "business",
         "environment": "staging"}])
    with patch("core.auth.admin_service_client", return_value=client):
        with pytest.raises(TemplateProtectedError):
            update_profile_row(profile_id="t1", payload={"display_name": "x"},
                               actor_user_id="u")


def test_update_rejects_bad_status_value():
    with pytest.raises(ValueError, match="status must be one of"):
        update_profile_row(profile_id="p1", payload={"status": "bogus"},
                           actor_user_id="u")


def test_update_rejects_template_as_destination_status():
    # 'template' is a seeder-managed status — an operator can't route a row
    # into it via update_profile_row, only ever out of it via fork.
    with pytest.raises(ValueError, match="status must be one of"):
        update_profile_row(profile_id="p1", payload={"status": "template"},
                           actor_user_id="u")


def test_update_refuses_dual_active():
    from admin.services.profiles import DualActiveProfileError

    target = {"profile_id": "p1", "agent_kind": "business", "slug": "specialist-a",
              "environment": "staging", "status": "specialist", "is_active": True}
    other_active = {"profile_id": "p2", "agent_kind": "business", "slug": "business_main",
                    "environment": "staging", "status": "active", "is_active": True}
    client, table_mock = _fake_client(select_data=[target, other_active])
    with patch("core.auth.admin_service_client", return_value=client):
        with pytest.raises(DualActiveProfileError, match="business_main"):
            update_profile_row(profile_id="p1", payload={"status": "active"},
                               actor_user_id="u")
    # No write attempted — the conflict is caught before the update call.
    table_mock.update.assert_not_called()


def test_update_allows_activating_when_no_conflict(monkeypatch):
    target = {"profile_id": "p1", "agent_kind": "business", "slug": "specialist-a",
              "environment": "staging", "status": "specialist", "is_active": True}
    client, table_mock = _fake_client(select_data=[target])
    monkeypatch.setattr(profiles, "record_change", lambda **kw: None)

    with patch("core.auth.admin_service_client", return_value=client):
        result = update_profile_row(profile_id="p1", payload={"status": "active"},
                                    actor_user_id="u")

    assert result["status"] == "active"
    table_mock.update.assert_called_once_with({"status": "active"})


def test_deep_merge_voice_config_sets_one_lever_preserves_siblings():
    # The exact clobber scenario from the audit: patching only
    # live.barge_in_enabled must not disturb answer_watchdog_sec,
    # agent_profile_slug, or any top-level sibling of "live".
    existing = {
        "live": {
            "barge_in_enabled": False,
            "answer_watchdog_sec": 5,
            "agent_profile_slug": "orchard_voice",
        },
        "memory_config": {"enabled": False},
        "budget_ceiling": 120,
    }
    out = deep_merge_voice_config(existing, {"live": {"barge_in_enabled": True}})
    assert out == {
        "live": {
            "barge_in_enabled": True,
            "answer_watchdog_sec": 5,
            "agent_profile_slug": "orchard_voice",
        },
        "memory_config": {"enabled": False},
        "budget_ceiling": 120,
    }
    # Pure function — the original dicts are untouched.
    assert existing["live"]["barge_in_enabled"] is False


def test_deep_merge_voice_config_nested_dict_merge_multiple_levels():
    existing = {"a": {"b": {"c": 1, "d": 2}, "e": 3}}
    out = deep_merge_voice_config(existing, {"a": {"b": {"c": 99}}})
    assert out == {"a": {"b": {"c": 99, "d": 2}, "e": 3}}


def test_deep_merge_voice_config_list_and_scalar_replace_wholesale():
    existing = {"tags": ["a", "b"], "count": 1, "nested": {"x": 1}}
    out = deep_merge_voice_config(
        existing, {"tags": ["c"], "count": 2, "nested": "not-a-dict-anymore"})
    assert out == {"tags": ["c"], "count": 2, "nested": "not-a-dict-anymore"}


def test_deep_merge_voice_config_null_deletes_key():
    existing = {"live": {"barge_in_enabled": True, "answer_watchdog_sec": 5}}
    out = deep_merge_voice_config(existing, {"live": {"answer_watchdog_sec": None}})
    assert out == {"live": {"barge_in_enabled": True}}


def test_deep_merge_voice_config_null_deletes_top_level_key():
    existing = {"live": {"a": 1}, "memory_config": {"enabled": True}}
    out = deep_merge_voice_config(existing, {"memory_config": None})
    assert out == {"live": {"a": 1}}


def test_deep_merge_voice_config_handles_missing_existing():
    assert deep_merge_voice_config(None, {"live": {"a": 1}}) == {"live": {"a": 1}}
    assert deep_merge_voice_config({}, {"live": {"a": 1}}) == {"live": {"a": 1}}


def test_fork_resets_delegatable_even_when_source_is_delegatable(monkeypatch):
    src = {**PROFILE, "delegatable": True}
    client, table_mock = _fake_client(select_data=[src], insert_data=[])
    monkeypatch.setattr(profiles, "record_change", lambda **kw: None)

    with patch("core.auth.admin_service_client", return_value=client):
        result = fork_profile_row(profile_id="p1", actor_user_id="u1")

    assert result["delegatable"] is False
    inserted_payload = table_mock.insert.call_args[0][0]
    assert inserted_payload["delegatable"] is False


# --- generic (`custom`) agents: many live per kind, status drives is_active ---

def _custom(**over):
    row = {"profile_id": "c1", "agent_kind": "custom", "slug": "helpdesk",
           "environment": "staging", "status": "draft", "is_active": False}
    row.update(over)
    return row


@pytest.mark.parametrize("status,expected", [
    ("active", True), ("specialist", True), ("draft", False), ("archived", False),
])
def test_custom_agent_status_drives_is_active(monkeypatch, status, expected):
    """A fork starts is_active=false and the portal's Settings tab writes only
    `status`. Unless the service syncs the flag, a custom agent set to Active
    is never servable (found live: status='active', is_active=false, 404)."""
    client, table_mock = _fake_client(select_data=[_custom()])
    monkeypatch.setattr(profiles, "record_change", lambda **kw: None)
    with patch("core.auth.admin_service_client", return_value=client):
        result = update_profile_row(profile_id="c1", payload={"status": status}, actor_user_id="u")
    table_mock.update.assert_called_once_with({"status": status, "is_active": expected})
    assert result["is_active"] is expected


def test_custom_agent_explicit_is_active_wins(monkeypatch):
    client, table_mock = _fake_client(select_data=[_custom()])
    monkeypatch.setattr(profiles, "record_change", lambda **kw: None)
    with patch("core.auth.admin_service_client", return_value=client):
        update_profile_row(profile_id="c1", payload={"status": "active", "is_active": False},
                           actor_user_id="u")
    table_mock.update.assert_called_once_with({"status": "active", "is_active": False})


def test_several_custom_agents_can_be_active_at_once(monkeypatch):
    """Custom agents are addressed by slug, not picked by `load_active`, so a
    second live one is not a conflict."""
    other = _custom(profile_id="c2", slug="sales-desk", status="active", is_active=True)
    client, table_mock = _fake_client(select_data=[_custom(), other])
    monkeypatch.setattr(profiles, "record_change", lambda **kw: None)
    with patch("core.auth.admin_service_client", return_value=client):
        result = update_profile_row(profile_id="c1", payload={"status": "active"}, actor_user_id="u")
    assert result["status"] == "active"
    table_mock.update.assert_called_once()


def test_other_kinds_keep_their_status_and_is_active_separate(monkeypatch):
    """Every other kind is unchanged: status does not touch is_active."""
    target = {"profile_id": "p1", "agent_kind": "business", "slug": "fork-a",
              "environment": "staging", "status": "draft", "is_active": False}
    client, table_mock = _fake_client(select_data=[target])
    monkeypatch.setattr(profiles, "record_change", lambda **kw: None)
    with patch("core.auth.admin_service_client", return_value=client):
        update_profile_row(profile_id="p1", payload={"status": "archived"}, actor_user_id="u")
    table_mock.update.assert_called_once_with({"status": "archived"})
