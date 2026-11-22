"""The "Save prompt" endpoint (admin/router.py's prompt_version_create).

Task 2.3 (profile-version-lifecycle) removed the per-prompt "Serve version"
UI, but the "Save prompt" form on agents.html still only inserted a new
`agent_prompt_versions` row — it never repointed the profile's
`prompt_version_ids[slug]` pin, so the runtime (which serves the PINNED
version, never "latest") never actually picked up the edit. This was a
silent no-op (finding C1).

Fix: the route now accepts optional `profile_id` / `prompt_key` form fields
(sent by agents.html's hidden inputs). When `profile_id` is supplied, saving
ALSO repoints that profile's pin at the freshly-created version — reusing
the same repoint mechanics as `/profiles/{id}/serve-prompt-version`. When
`profile_id` is omitted (back-compat for any other caller), behaviour is
unchanged: create the version only, no repoint."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

from admin import router

# The exact shape of the known regression core.prompt_validator.
# check_ask_agent_imperative exists to catch — see CLAUDE.md's "Be brief"
# section and tests/test_prompt_validator.py.
_DESCRIPTIVE_ASK_AGENT_CONTENT = (
    "For ANY question about data, the request goes to the `ask_agent` tool."
)


def _fake_client(last_version_row, inserted_version_row, profile_row, captured):
    prompt_versions = MagicMock()
    prompt_versions.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[last_version_row] if last_version_row else []
    )
    prompt_versions.insert.return_value.execute.return_value = MagicMock(
        data=[inserted_version_row]
    )

    profiles = MagicMock()
    profiles.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[profile_row] if profile_row else []
    )

    def _update(payload):
        captured["profile_update"] = payload
        m = MagicMock()
        m.eq.return_value.execute.return_value = MagicMock(data=[])
        return m
    profiles.update.side_effect = _update

    def _table(name):
        return {
            "agent_prompt_versions": prompt_versions,
            "agent_profiles": profiles,
        }.get(name, MagicMock())

    client = MagicMock()
    client.table.side_effect = _table
    return client


def test_save_prompt_with_profile_id_repoints_the_profiles_pin():
    """The C1 fix: profile_id + prompt_key present -> new version created
    AND agent_profiles.prompt_version_ids[prompt_key] repointed to it, with
    the profile/prompt caches invalidated so the edit is served immediately."""
    captured: dict = {}
    profile_row = {
        "agent_kind": "sales",
        "environment": "staging",
        "prompt_version_ids": {"system": "old-vid", "title": "t-vid"},
    }
    client = _fake_client(
        last_version_row={"version_number": 3},
        inserted_version_row={"prompt_version_id": "new-pv-id", "version_number": 4},
        profile_row=profile_row,
        captured=captured,
    )
    with patch.object(router, "get_service_client", return_value=client), \
         patch("core.agent_profile.AgentProfileLoader.invalidate") as inv, \
         patch("core.prompt_resolver.PromptResolver.invalidate") as pinv, \
         patch.object(router, "record_change"):
        resp = asyncio.run(router.prompt_version_create(
            "prompt-1",
            request=None,
            ctx={"user_id": "u"},
            content="You are the new sales assistant.",
            notes="tightened tone",
            profile_id="pid-1",
            prompt_key="system",
        ))

    assert resp.status_code == 302
    # Version repoint: system pin -> the new version, other slots untouched.
    assert captured["profile_update"]["prompt_version_ids"] == {
        "system": "new-pv-id", "title": "t-vid",
    }
    inv.assert_called_once_with("sales", "staging")
    assert pinv.call_count >= 1


def test_save_prompt_without_profile_id_only_creates_the_version():
    """Back-compat: no profile_id -> version is created, but no profile is
    read or updated, and the profile-loader cache is left untouched."""
    captured: dict = {}
    client = _fake_client(
        last_version_row={"version_number": 1},
        inserted_version_row={"prompt_version_id": "new-pv-id-2", "version_number": 2},
        profile_row=None,
        captured=captured,
    )
    with patch.object(router, "get_service_client", return_value=client), \
         patch("core.agent_profile.AgentProfileLoader.invalidate") as inv, \
         patch("core.prompt_resolver.PromptResolver.invalidate") as pinv, \
         patch.object(router, "record_change"):
        resp = asyncio.run(router.prompt_version_create(
            "prompt-1",
            request=None,
            ctx={"user_id": "u"},
            content="unchanged behaviour",
            notes="",
            profile_id="",
            prompt_key="system",
        ))

    assert resp.status_code == 302
    assert "profile_update" not in captured
    inv.assert_not_called()
    assert pinv.call_count == 1


# ---------------------------------------------------------------------------
# Part 4: this route is gated by the SAME core.prompt_validator the MCP
# prompts() tool uses (admin_mcp/tools_configure.py) — the portal "Save
# prompt" button and the MCP path must not be able to drift into different
# safety bars.
# ---------------------------------------------------------------------------


def test_save_prompt_refuses_a_prompt_that_fails_validation():
    captured: dict = {}
    client = _fake_client(
        last_version_row={"version_number": 3},
        inserted_version_row={"prompt_version_id": "new-pv-id", "version_number": 4},
        profile_row=None,
        captured=captured,
    )
    with patch.object(router, "get_service_client", return_value=client), \
         patch.object(router, "record_change") as rc:
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(router.prompt_version_create(
                "prompt-1",
                request=None,
                ctx={"user_id": "u"},
                content=_DESCRIPTIVE_ASK_AGENT_CONTENT,
                notes="",
                profile_id="",
                prompt_key="system",
                # Explicit, not relied on as a default: called directly
                # (bypassing FastAPI's dependency injection) a bare
                # `= Form(False)` default is the Form() marker object
                # itself, not the boolean False.
                override_validation=False,
            ))
    assert exc_info.value.status_code == 400
    assert "ask_agent" in exc_info.value.detail or "call" in exc_info.value.detail
    # Nothing written, nothing audited — a plain refusal is not an override.
    assert client.table("agent_prompt_versions").insert.call_count == 0
    rc.assert_not_called()


def test_save_prompt_override_forces_it_through_and_audits():
    captured: dict = {}
    client = _fake_client(
        last_version_row={"version_number": 3},
        inserted_version_row={"prompt_version_id": "new-pv-id", "version_number": 4},
        profile_row=None,
        captured=captured,
    )
    with patch.object(router, "get_service_client", return_value=client), \
         patch("core.prompt_resolver.PromptResolver.invalidate"), \
         patch.object(router, "record_change") as rc:
        resp = asyncio.run(router.prompt_version_create(
            "prompt-1",
            request=None,
            ctx={"user_id": "u"},
            content=_DESCRIPTIVE_ASK_AGENT_CONTENT,
            notes="",
            profile_id="",
            prompt_key="system",
            override_validation=True,
        ))
    assert resp.status_code == 302
    assert client.table("agent_prompt_versions").insert.call_count == 1
    # TWO record_change calls: the override itself, then the ordinary
    # "create" audit the un-overridden path already produces.
    actions = [c.kwargs.get("action") for c in rc.call_args_list]
    assert actions == ["validation_override", "create"]


# ---------------------------------------------------------------------------
# Part 5: the return redirect. profile_id is a RAW form value that reaches the
# Location header — it must arrive percent-encoded so a `&` inside it stays
# part of the ONE profile_id query value instead of smuggling extra params
# (Starlette's Location quoting preserves a literal `&`).
# ---------------------------------------------------------------------------


def test_redirect_percent_encodes_profile_id():
    captured: dict = {}
    client = _fake_client(
        last_version_row={"version_number": 1},
        inserted_version_row={"prompt_version_id": "new-pv-id-3", "version_number": 2},
        profile_row=None,  # lookup misses -> no repoint; redirect still carries the form value
        captured=captured,
    )
    with patch.object(router, "get_service_client", return_value=client), \
         patch("core.prompt_resolver.PromptResolver.invalidate"), \
         patch.object(router, "record_change"):
        resp = asyncio.run(router.prompt_version_create(
            "prompt-1",
            request=None,
            env="staging",
            ctx={"user_id": "u"},
            content="You are the new sales assistant.",
            notes="",
            profile_id="pid-1&x=1",
            prompt_key="system",
        ))

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "profile_id=pid-1%26x%3D1" in location, location
    # The smuggled param must NOT survive as its own query key.
    assert "&x=1" not in location, location


def test_redirect_uses_profile_rows_environment_when_repointing():
    """When the profile row is in hand (the repoint ran), the redirect's env
    comes from the ROW, not the operator's current-env cookie/query."""
    captured: dict = {}
    profile_row = {
        "agent_kind": "sales",
        "environment": "prod",
        "prompt_version_ids": {"system": "old-vid"},
    }
    client = _fake_client(
        last_version_row={"version_number": 3},
        inserted_version_row={"prompt_version_id": "new-pv-id", "version_number": 4},
        profile_row=profile_row,
        captured=captured,
    )
    with patch.object(router, "get_service_client", return_value=client), \
         patch("core.agent_profile.AgentProfileLoader.invalidate"), \
         patch("core.prompt_resolver.PromptResolver.invalidate"), \
         patch.object(router, "record_change"):
        resp = asyncio.run(router.prompt_version_create(
            "prompt-1",
            request=None,
            env="staging",
            ctx={"user_id": "u"},
            content="You are the new sales assistant.",
            notes="",
            profile_id="pid-1",
            prompt_key="system",
        ))

    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/agents?profile_id=pid-1&env=prod"
