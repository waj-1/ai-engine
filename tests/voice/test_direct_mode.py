"""Pre-fetched-context mode at the voice edge: the provider declares the
agent's direct tools INSTEAD of ask_agent, and the ask_agent machinery stands down."""
from __future__ import annotations

from endpoints.voice_live import _direct_tools_for
from voice.live.config import LiveVoiceConfig
from voice.live.providers import azure_voice_live, gemini_live

DECL = {"name": "send_payment_link", "description": "d",
        "parameters": {"type": "object", "properties": {}, "required": []}}


class _Agent:
    def direct_voice_tools(self):
        return [(DECL, lambda args: {"ok": True})]


def test_off_by_default_keeps_the_delegated_session():
    cfg = LiveVoiceConfig(stall_recovery_sec=2.0)
    assert _direct_tools_for(cfg, _Agent()) == {}
    assert cfg.direct_tools == [] and cfg.stall_recovery_sec == 2.0


def test_on_fills_declarations_and_stands_down_recoveries():
    cfg = LiveVoiceConfig(prefetched_context=True, stall_recovery_sec=2.0,
                          stall_recovery_topic_terms=["iban"], answer_watchdog_sec=4.0,
                          progress_after_sec=4.0, relay_mode=True, persona_claim_recovery_enabled=True)
    handlers = _direct_tools_for(cfg, _Agent())
    assert list(handlers) == ["send_payment_link"]
    assert cfg.direct_tools == [DECL]
    assert cfg.stall_recovery_sec == 0 and cfg.stall_recovery_unannounced_sec == 0
    assert cfg.stall_recovery_silent_sec == 0 and cfg.stall_recovery_topic_terms == []
    assert cfg.answer_watchdog_sec == 0 and cfg.progress_after_sec == 0
    assert not cfg.relay_mode and not cfg.persona_claim_recovery_enabled


def test_kind_without_direct_tools_falls_back():
    cfg = LiveVoiceConfig(prefetched_context=True)
    assert _direct_tools_for(cfg, object()) == {}
    assert cfg.direct_tools == []


def test_gemini_declares_direct_tools_instead_of_ask_agent():
    assert [d["name"] for d in gemini_live._function_declarations(LiveVoiceConfig())] == ["ask_agent"]
    decls = gemini_live._function_declarations(LiveVoiceConfig(direct_tools=[DECL]))
    assert decls == [DECL]


def test_azure_declares_direct_tools_instead_of_ask_agent():
    plain = azure_voice_live._session_payload(LiveVoiceConfig(), "p")
    tools = plain["session"]["tools"] if "session" in plain else plain["tools"]
    assert [t["name"] for t in tools] == ["ask_agent"]
    direct = azure_voice_live._session_payload(LiveVoiceConfig(direct_tools=[DECL]), "p")
    tools = direct["session"]["tools"] if "session" in direct else direct["tools"]
    assert tools == [{"type": "function", **DECL}]
