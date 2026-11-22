"""Voice-edge seams the outbound collection call relies on."""
from __future__ import annotations

import inspect
from types import SimpleNamespace

from endpoints import voice_live


def _cfg(slug="sample_outbound_persona"):
    return SimpleNamespace(persona_prompt_slug=slug)


def test_persona_renders_extra_variables():
    profile = SimpleNamespace(resolved_prompts={"sample_outbound_persona": "Hi {callee_name}, in {language}."})
    out = voice_live.resolve_persona_prompt(
        _cfg(), "sample_outbound", profile=profile, language="Arabic",
        extra_variables={"callee_name": "أستاذ فهد"},
    )
    assert out == "Hi أستاذ فهد, in Arabic."


def test_persona_without_extra_variables_is_unchanged():
    profile = SimpleNamespace(resolved_prompts={"sample_outbound_persona": "Hi {callee_name}, in {language}."})
    out = voice_live.resolve_persona_prompt(_cfg(), "sample_outbound", profile=profile, language="English")
    assert out == "Hi {callee_name}, in English."


def test_ws_accepts_call_case_param():
    assert "callCase" in inspect.signature(voice_live.voice_live).parameters

