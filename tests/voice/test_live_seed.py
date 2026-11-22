from admin.seed_agent_models import MODELS


def test_catalog_contains_a_realtime_capable_model():
    realtime = [m for m in MODELS if (m.get("extra_config") or {}).get("realtime")]
    assert realtime, "the model catalog must offer at least one realtime model for duplex voice"
    row = realtime[0]
    assert row["provider"] == "google_genai"
    assert "native-audio" in row["model_string"] or "live" in row["model_string"]


def test_realtime_model_declares_its_pricing_key():
    realtime = [m for m in MODELS if (m.get("extra_config") or {}).get("realtime")][0]
    assert (realtime.get("extra_config") or {}).get("tokencost_model")
