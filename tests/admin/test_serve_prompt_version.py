"""The 'Serve this version' selector endpoint — pins which prompt version the
profile's endpoint serves (agent_profiles.prompt_version_ids[key])."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

from admin import router


def _fake_client(profile_row, captured):
    table = MagicMock()
    table.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(data=[profile_row])
    def _update(payload):
        captured["update"] = payload
        m = MagicMock()
        m.eq.return_value.execute.return_value = MagicMock(data=[])
        return m
    table.update.side_effect = _update
    client = MagicMock()
    client.table.return_value = table
    return client


def test_serve_prompt_version_sets_pin_preserves_others_and_invalidates():
    captured: dict = {}
    profile_row = {"agent_kind": "orchard", "environment": "staging",
                   "prompt_version_ids": {"system": "old-vid", "title": "t-vid"}}
    with patch.object(router, "get_service_client",
                      return_value=_fake_client(profile_row, captured)), \
         patch("core.agent_profile.AgentProfileLoader.invalidate") as inv, \
         patch("core.prompt_resolver.PromptResolver.invalidate") as pinv, \
         patch.object(router, "record_change"):
        resp = asyncio.run(router.profile_serve_prompt_version(
            "pid", ctx={"user_id": "u"}, prompt_key="system", version_id="new-vid"))
    # system pin switched, other slots (title) untouched
    assert captured["update"]["prompt_version_ids"] == {"system": "new-vid", "title": "t-vid"}
    assert resp.status_code == 302
    inv.assert_called_once_with("orchard", "staging")   # profile cache cleared for env
    pinv.assert_called_once()                            # prompt cache cleared
