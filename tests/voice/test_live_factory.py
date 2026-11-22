import pytest

from voice.live.config import LiveVoiceConfig
from voice.live.factory import (
    UnsupportedRealtimeProviderError,
    build_realtime_session,
    register_realtime_provider,
)
from voice.live.testing import FakeRealtimeSession


def test_registered_provider_is_built():
    register_realtime_provider("fake_provider", lambda cfg, persona: FakeRealtimeSession())
    session = build_realtime_session(LiveVoiceConfig(provider="fake_provider"), persona="p")
    assert isinstance(session, FakeRealtimeSession)


def test_persona_reaches_the_builder():
    seen = {}
    register_realtime_provider(
        "persona_probe",
        lambda cfg, persona: seen.setdefault("persona", persona) and None or FakeRealtimeSession(),
    )
    build_realtime_session(LiveVoiceConfig(provider="persona_probe"), persona="be brief")
    assert seen["persona"] == "be brief"


def test_provider_slug_is_case_insensitive():
    register_realtime_provider("fake_provider", lambda cfg, persona: FakeRealtimeSession())
    assert build_realtime_session(LiveVoiceConfig(provider="FAKE_PROVIDER"), persona="p")


def test_unknown_provider_names_the_supported_set():
    with pytest.raises(UnsupportedRealtimeProviderError) as exc:
        build_realtime_session(LiveVoiceConfig(provider="wishful"), persona="p")
    assert "wishful" in str(exc.value)


def test_azure_stub_raises_with_actionable_guidance():
    import voice.live.providers.azure_realtime  # noqa: F401 — registers the slug

    with pytest.raises(UnsupportedRealtimeProviderError) as exc:
        build_realtime_session(LiveVoiceConfig(provider="azure_openai"), persona="p")
    message = str(exc.value)
    # Assert on INTENT, not incidental phrasing: the message must route the
    # operator to the real Azure provider slug and its design spec.
    assert "azure_voice_live" in message
    assert "2026-07-28-azure-voice-live-provider-design.md" in message
