"""Each agent can pin its own memory prompts (`memory_extraction`,
`memory_summary`), like its system prompt; an agent without them keeps the
defaults. Live testing (2026-10) showed the defaults, written for business
owners, saving a customer's "I prefer Ahmad for my appointments" as
"The user prefers to be called Ahmad" (Ahmad is the barber)."""
import hashlib
from unittest.mock import AsyncMock, patch

import pytest
from core.agent_profile import Profile
from core.memory import service
from core.memory.prompts import (
    EXTRACTION_FIELDS,
    EXTRACTION_PROMPT,
    EXTRACTION_SLOT,
    SUMMARY_PROMPT,
    SUMMARY_SLOT,
)

_CUSTOM = "CUSTOM {user_message} / {assistant_response}"


def _profile(pins=None, snapshot=None, kind="customer_chat"):
    return Profile(
        profile_id="p1", agent_kind=kind, slug=f"{kind}_default", environment="staging",
        model_main={"provider": "google_genai", "model_string": "gemini-x"},
        memory_config={"enabled": True},
        prompt_version_ids=pins or {"system": "v-system"},
        resolved_prompts=snapshot or {},
    )


def test_defaults_are_unchanged():
    # Every agent without its own memory prompt (business, a client demo's, ...)
    # must keep getting exactly the text it got before.
    assert hashlib.sha256(EXTRACTION_PROMPT.encode()).hexdigest() == \
        "f4bd07bab33f57d452683ee99fd73b3f51def2c6487ff3c3b6b515fc71ba1896"
    assert hashlib.sha256(SUMMARY_PROMPT.encode()).hexdigest() == \
        "9a68bc2b318a3c95f7450ac7db466f076cc4ecab4052b621c3c07f66e9453753"


def test_an_agent_without_its_own_prompt_gets_the_default():
    with patch.object(service.PromptResolver, "resolve") as resolve:
        got = service._agent_memory_prompt(
            _profile(), EXTRACTION_SLOT, EXTRACTION_PROMPT, EXTRACTION_FIELDS)
    assert got == EXTRACTION_PROMPT
    resolve.assert_not_called()  # no DB read for an agent that never set one


def test_the_pinned_version_is_used():
    profile = _profile(pins={"system": "v-system", EXTRACTION_SLOT: "v-mem"})
    with patch.object(service.PromptResolver, "resolve", return_value=_CUSTOM) as resolve:
        got = service._agent_memory_prompt(profile, EXTRACTION_SLOT, EXTRACTION_PROMPT, EXTRACTION_FIELDS)
    assert got == _CUSTOM
    assert resolve.call_args.args[0] == "v-mem"
    assert resolve.call_args.kwargs["prompt_key"] == EXTRACTION_SLOT


def test_prod_uses_the_promote_snapshot_copy():
    profile = _profile(pins={EXTRACTION_SLOT: "v-dev-only"}, snapshot={EXTRACTION_SLOT: _CUSTOM})
    got = service._agent_memory_prompt(profile, EXTRACTION_SLOT, EXTRACTION_PROMPT, EXTRACTION_FIELDS)
    assert got == _CUSTOM


def test_a_prompt_missing_a_placeholder_falls_back_to_the_default(caplog):
    broken = "CUSTOM {user_message} only"  # {assistant_response} deleted by an edit
    profile = _profile(snapshot={EXTRACTION_SLOT: broken})
    got = service._agent_memory_prompt(profile, EXTRACTION_SLOT, EXTRACTION_PROMPT, EXTRACTION_FIELDS)
    assert got == EXTRACTION_PROMPT
    assert "unusable" in caplog.text


def test_a_prompt_with_an_unknown_placeholder_falls_back_to_the_default():
    profile = _profile(snapshot={EXTRACTION_SLOT: _CUSTOM + " {customer_name}"})
    got = service._agent_memory_prompt(profile, EXTRACTION_SLOT, EXTRACTION_PROMPT, EXTRACTION_FIELDS)
    assert got == EXTRACTION_PROMPT


@pytest.mark.parametrize("bad", [
    "CUSTOM {user_message!z} / {assistant_response}",     # unknown conversion
    "CUSTOM {user_message:bogus} / {assistant_response}",  # invalid format spec
    "CUSTOM {user_message:{x}} / {assistant_response}",    # a field hidden in the spec
])
def test_a_prompt_that_cannot_be_formatted_falls_back_to_the_default(bad):
    # The placeholder names are right, so only a trial format catches these.
    profile = _profile(snapshot={EXTRACTION_SLOT: bad})
    got = service._agent_memory_prompt(profile, EXTRACTION_SLOT, EXTRACTION_PROMPT, EXTRACTION_FIELDS)
    assert got == EXTRACTION_PROMPT


async def test_extract_facts_sends_the_given_prompt():
    from core.memory.extract import extract_facts

    seen = {}

    class _LLM:
        async def ainvoke(self, messages):
            seen["prompt"] = messages[0].content

            class R:
                content = '{"facts": []}'
            return R()

    await extract_facts(_LLM(), "I prefer Ahmad", "Noted!", {}, prompt=_CUSTOM)
    assert seen["prompt"] == "CUSTOM I prefer Ahmad / Noted!"


async def test_process_turn_uses_the_agents_extraction_prompt(monkeypatch):
    profile = _profile(snapshot={EXTRACTION_SLOT: _CUSTOM})
    extract = AsyncMock(return_value=[])
    monkeypatch.setattr(service, "extract_facts", extract)
    monkeypatch.setattr(service, "build_chat_model", lambda _m: object())
    monkeypatch.setattr(service.store, "get_session_summary", AsyncMock(return_value={}))
    monkeypatch.setattr(service.store, "count_messages", AsyncMock(return_value=2))
    await service.process_turn(
        profile=profile, user_id="u1", business_id="b1", agent_kind="customer_chat",
        session_id="s1", user_message="hi", assistant_response="hello",
        previous_messages=[], client=object(),
    )
    assert extract.await_args.kwargs["prompt"] == _CUSTOM
