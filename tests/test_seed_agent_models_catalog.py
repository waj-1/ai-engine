from admin.seed_agent_models import MODELS

BY_NAME = {m["name"]: m for m in MODELS}


def test_azure_voice_live_model_row():
    # Duplex voice ears-and-mouth model for the Azure Voice Live provider.
    azure = BY_NAME["Azure Voice Live (gpt-4.1-mini)"]
    assert azure["provider"] == "azure_voice_live"
    assert azure["model_string"] == "gpt-4.1-mini"
    assert azure["extra_config"]["endpoint"] == "https://voice-live-example.services.ai.azure.com"
    assert azure["extra_config"]["api_version"] == "2026-04-10"
    assert azure["extra_config"]["auth_env_var"] == "AZURE_VOICE_LIVE_API_KEY"
    assert azure["extra_config"]["realtime"] is True
    # MANUAL, never auto: tokencost's bare "gpt-4.1-mini" key is OpenAI's TEXT
    # rate, 16-27x below Voice Live audio-token billing — auto would silently
    # under-price every voice minute.
    assert azure["extra_config"]["pricing_mode"] == "manual"
    assert azure["extra_config"]["cost_per_1k_in"] == 0.011
    assert azure["extra_config"]["cost_per_1k_out"] == 0.026


def test_native_realtime_row_uses_the_locale_locked_recognizer():
    """gpt-4o-transcribe is Whisper-family: `language` only BIASES it, so on
    silence it emitted Bulgarian/German/Danish/Japanese into an ar-SA-pinned
    Saudi session. azure-speech is locked to the locale and is also the only
    recognizer that honours voice_config.live.phrase_list."""
    from admin.seed_agent_models import MODELS

    native = [m for m in MODELS
              if (m.get("extra_config") or {}).get("voice_type") == "native"]
    assert native, "no native-audio realtime row in the catalog"
    for row in native:
        ec = row["extra_config"]
        assert ec.get("transcription_model") == "azure-speech", row["name"]
        assert ec.get("realtime") is True, row["name"]

