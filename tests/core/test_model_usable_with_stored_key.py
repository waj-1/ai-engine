"""A key saved on Settings > Model keys makes that provider's models usable,
the same as its environment variable: the Automatic model (the demo agent)
must pick such a model up."""
from core import model_resolver, provider_keys


def test_a_stored_key_counts_as_a_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert model_resolver.provider_key_available("OPENAI_API_KEY") is False
    monkeypatch.setattr(provider_keys, "env_or_stored",
                        lambda env_var: "sk-stored" if env_var == "OPENAI_API_KEY" else "")
    assert model_resolver.provider_key_available("OPENAI_API_KEY") is True
    assert model_resolver.provider_key_available("ANTHROPIC_API_KEY") is False


def test_a_cloud_model_is_usable_with_only_a_stored_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    model = {"provider": "openai", "model_string": "gpt-test", "is_active": True, "extra_config": {}}
    assert model_resolver.model_unusable_reason(model) is not None
    monkeypatch.setattr(provider_keys, "env_or_stored", lambda env_var: "sk-stored")
    assert model_resolver.model_unusable_reason(model) is None
