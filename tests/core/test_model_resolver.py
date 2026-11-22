from core import model_resolver


def test_set_get_clear_roundtrip():
    assert model_resolver.get_model_override() is None
    model_resolver.set_model_override({"model_string": "x", "provider": "google_genai"})
    assert model_resolver.get_model_override()["model_string"] == "x"
    model_resolver.clear_model_override()
    assert model_resolver.get_model_override() is None
