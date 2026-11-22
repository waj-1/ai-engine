"""admin/services/custom_prompts.py — a generic (`custom`) agent owns its
instructions instead of sharing its kind's prompt slots."""
from __future__ import annotations

import pytest

from admin.services import custom_prompts as cp
from tests.admin_mcp.conftest import FakeClient

TEMPLATE_PROMPT = {"prompt_id": "pr-mold", "agent_kind": "business", "slug": "mold:blank"}
TEMPLATE_V1 = {"prompt_version_id": "v-mold-1", "prompt_id": "pr-mold", "version_number": 1,
               "content": "You are a blank tool-caller."}


def _profile(**over):
    row = {"profile_id": "c1", "agent_kind": "custom", "slug": "helpdesk", "display_name": "Helpdesk",
           "environment": "staging", "status": "draft", "prompt_version_ids": {"system": "v-mold-1"}}
    row.update(over)
    return row


_PRIMARY_KEYS = {"agent_prompts": "prompt_id", "agent_prompt_versions": "prompt_version_id"}


def _with_generated_keys(client):
    """Postgres fills a row's primary key on insert; the shared fake only sets
    a generic `id`. Mirror the database so code that reads the new row's key
    (as the service does) sees one."""
    for name, pk in _PRIMARY_KEYS.items():
        table = client.table(name)
        real_execute = table.execute

        def execute(table=table, pk=pk, real_execute=real_execute):
            inserting = table._insert_payload is not None
            result = real_execute()
            if inserting:
                for row in result.data:
                    row.setdefault(pk, f"{pk}-{row['id']}")
            return result

        table.execute = execute
    return client


def _client(profile=None, prompts=(), versions=()):
    return _with_generated_keys(FakeClient({
        "agent_profiles": [profile or _profile()],
        "agent_prompts": [dict(TEMPLATE_PROMPT), *prompts],
        "agent_prompt_versions": [dict(TEMPLATE_V1), *versions],
    }))


def _rows(client, table):
    return client.table(table)._data


def test_first_ensure_copies_the_template_text_and_moves_the_pin():
    profile = _profile()
    client = _client(profile)
    prompt = cp.ensure_own_prompt(client, profile, actor_user_id="u")
    assert prompt["agent_kind"] == "custom" and prompt["slug"] == "helpdesk:system"
    own = [v for v in _rows(client, "agent_prompt_versions") if v["prompt_id"] == prompt["prompt_id"]]
    assert len(own) == 1 and own[0]["version_number"] == 1
    assert own[0]["content"] == "You are a blank tool-caller."
    stored = _rows(client, "agent_profiles")[0]
    assert stored["prompt_version_ids"]["system"] == own[0]["prompt_version_id"]
    # The template's own history is never written to.
    assert [v for v in _rows(client, "agent_prompt_versions") if v["prompt_id"] == "pr-mold"] == [TEMPLATE_V1]


def test_ensure_is_idempotent():
    profile = _profile()
    client = _client(profile)
    first = cp.ensure_own_prompt(client, profile, actor_user_id="u")
    again = cp.ensure_own_prompt(client, _rows(client, "agent_profiles")[0], actor_user_id="u")
    assert again["prompt_id"] == first["prompt_id"]
    assert len([p for p in _rows(client, "agent_prompts") if p["agent_kind"] == "custom"]) == 1
    assert len([v for v in _rows(client, "agent_prompt_versions") if v["prompt_id"] == first["prompt_id"]]) == 1


def test_an_agent_with_no_pin_gets_an_empty_prompt_and_no_version():
    profile = _profile(prompt_version_ids={})
    client = _client(profile)
    prompt = cp.ensure_own_prompt(client, profile, actor_user_id="u")
    assert [v for v in _rows(client, "agent_prompt_versions") if v["prompt_id"] == prompt["prompt_id"]] == []
    assert _rows(client, "agent_profiles")[0]["prompt_version_ids"] == {}


def test_two_agents_never_share_a_prompt():
    a, b = _profile(), _profile(profile_id="c2", slug="sales-desk")
    client = _with_generated_keys(FakeClient({
        "agent_profiles": [a, b], "agent_prompts": [dict(TEMPLATE_PROMPT)],
        "agent_prompt_versions": [dict(TEMPLATE_V1)]}))
    pa = cp.ensure_own_prompt(client, a, actor_user_id="u")
    pb = cp.ensure_own_prompt(client, b, actor_user_id="u")
    assert pa["prompt_id"] != pb["prompt_id"]
    assert {pa["slug"], pb["slug"]} == {"helpdesk:system", "sales-desk:system"}


def test_only_custom_agents_own_a_prompt():
    """Every other kind shares its kind's slots by design; giving one its own
    row would silently detach it from the prompt the rest of the kind edits."""
    with pytest.raises(ValueError):
        cp.ensure_own_prompt(_client(), _profile(agent_kind="business"), actor_user_id="u")


def test_view_before_the_first_save_shows_the_inherited_text_and_writes_nothing():
    profile = _profile()
    client = _client(profile)
    view = cp.instructions_view(client, profile)
    assert view["slug"] == "system"            # the tab finds the system prompt by this
    assert view["prompt_id"] is None           # nothing of the agent's own yet
    assert view["content"] == "You are a blank tool-caller."
    assert view["post_url"] == "/admin/agents/c1/instructions"
    assert [p["slug"] for p in _rows(client, "agent_prompts")] == ["mold:blank"]   # read-only


def test_view_shows_the_pinned_version_of_the_agents_own_prompt():
    own = {"prompt_id": "pr-own", "agent_kind": "custom", "slug": "helpdesk:system"}
    v1 = {"prompt_version_id": "v-own-1", "prompt_id": "pr-own", "version_number": 1, "content": "one"}
    v2 = {"prompt_version_id": "v-own-2", "prompt_id": "pr-own", "version_number": 2, "content": "two"}
    profile = _profile(prompt_version_ids={"system": "v-own-1"})
    view = cp.instructions_view(_client(profile, prompts=[own], versions=[v1, v2]), profile)
    # Pinned to v1 while v2 exists: the editor shows what the agent SERVES.
    assert view["content"] == "one" and view["version_number"] == 1
    assert view["latest_version_number"] == 2 and view["prompt_id"] == "pr-own"


def test_view_ignores_a_pin_into_another_prompts_history_once_the_agent_has_its_own():
    own = {"prompt_id": "pr-own", "agent_kind": "custom", "slug": "helpdesk:system"}
    v1 = {"prompt_version_id": "v-own-1", "prompt_id": "pr-own", "version_number": 1, "content": "mine"}
    profile = _profile(prompt_version_ids={"system": "v-mold-1"})
    view = cp.instructions_view(_client(profile, prompts=[own], versions=[v1]), profile)
    assert view["content"] == "mine"


# --- the portal route: POST /admin/agents/{profile_id}/instructions ----------

import asyncio  # noqa: E402
from unittest.mock import patch  # noqa: E402

from fastapi import HTTPException  # noqa: E402

from admin import router  # noqa: E402

# Wording that tells the model about ask_agent descriptively instead of telling
# it to CALL the tool: the validator refuses it (core/prompt_validator.py).
_FAILS_VALIDATION = "When the caller asks a question, it goes to the ask_agent tool."


def _save(client, profile_id="c1", **kw):
    args = dict(request=None, env="staging", ctx={"user_id": "u"}, content="You are the helpdesk.",
                notes="first edit", override_validation=False)
    args.update(kw)
    with patch.object(router, "admin_service_client", return_value=client), \
         patch("core.agent_profile.AgentProfileLoader.invalidate"), \
         patch("core.prompt_resolver.PromptResolver.invalidate"), \
         patch.object(router, "record_change"):
        return asyncio.run(router.custom_agent_instructions_save(profile_id, **args))


def test_first_save_gives_the_agent_its_own_prompt_and_pins_the_new_version():
    profile = _profile()
    client = _client(profile)
    resp = _save(client)
    assert resp.status_code == 302 and "profile_id=c1" in resp.headers["location"]
    own_prompt = next(p for p in _rows(client, "agent_prompts") if p["agent_kind"] == "custom")
    assert own_prompt["slug"] == "helpdesk:system"
    versions = sorted((v for v in _rows(client, "agent_prompt_versions")
                       if v["prompt_id"] == own_prompt["prompt_id"]), key=lambda v: v["version_number"])
    # v1 = the template text it started from, v2 = this save.
    assert [v["content"] for v in versions] == ["You are a blank tool-caller.", "You are the helpdesk."]
    assert _rows(client, "agent_profiles")[0]["prompt_version_ids"]["system"] == versions[1]["prompt_version_id"]
    assert [v for v in _rows(client, "agent_prompt_versions") if v["prompt_id"] == "pr-mold"] == [TEMPLATE_V1]


def test_a_refused_save_creates_nothing():
    """Validation runs before the agent's prompt row is created, so a refused
    save leaves no empty prompt behind and the pin where it was."""
    profile = _profile()
    client = _client(profile)
    with pytest.raises(HTTPException) as exc:
        _save(client, content=_FAILS_VALIDATION)
    assert exc.value.status_code == 400
    assert [p["slug"] for p in _rows(client, "agent_prompts")] == ["mold:blank"]
    assert _rows(client, "agent_profiles")[0]["prompt_version_ids"] == {"system": "v-mold-1"}


def test_route_refuses_other_kinds_and_templates_and_unknown_profiles():
    for profile, status in ((_profile(agent_kind="business"), 400), (_profile(status="template"), 400)):
        with pytest.raises(HTTPException) as exc:
            _save(_client(profile))
        assert exc.value.status_code == status
    with pytest.raises(HTTPException) as exc:
        _save(_client(_profile()), profile_id="no-such-profile")
    assert exc.value.status_code == 404
