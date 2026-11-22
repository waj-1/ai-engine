from admin import seed_agent_models


class _FakeTable:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self._filters = {}
    def select(self, *a, **k): return self
    def eq(self, col, val): self._filters[col] = val; return self
    def limit(self, n): return self
    def execute(self):
        rows = [r for r in self.store.get(self.name, [])
                if all(r.get(k) == v for k, v in self._filters.items())]
        return type("R", (), {"data": rows})
    def insert(self, row):
        self.store.setdefault(self.name, []).append(dict(row))
        return type("R", (), {"data": [row], "execute": lambda s=None: None})


class _FakeClient:
    def __init__(self): self.store = {}
    def table(self, name): return _FakeTable(self.store, name)


def test_seed_inserts_catalog_and_is_idempotent(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(seed_agent_models, "get_service_client", lambda: client)
    first = seed_agent_models.run()
    assert first["inserted"] == len(seed_agent_models.MODELS)
    # second run inserts nothing (idempotent on name+environment)
    second = seed_agent_models.run()
    assert second["inserted"] == 0 and second["skipped"] == len(seed_agent_models.MODELS)


def test_catalog_providers_and_auto_priced():
    # Gemini runs via google_genai (vertex_ai/ChatVertexAI breaks the agent tool
    # loop); azure_voice_live is the duplex voice provider; azure_openai is the
    # Azure-hosted chat brain (aoai-example deployments).
    #
    # Tightened 2026-08-16: the allow-set used to include vertex_ai /
    # anthropic_vertex / vertex_maas, so — being a subset assertion — it would
    # have happily passed if someone re-seeded a Vertex row that no builder can
    # construct. Every provider seeded here must be in SUPPORTED_PROVIDERS.
    from runtime.llm_factory import SUPPORTED_PROVIDERS

    providers = {m["provider"] for m in seed_agent_models.MODELS}
    assert providers <= {"google_genai", "azure_voice_live", "azure_openai"}
    # azure_voice_live is a duplex-voice row built by voice/live/factory.py, not
    # by the chat-model factory, so it is legitimately absent from _BUILDERS.
    assert providers - {"azure_voice_live"} <= SUPPORTED_PROVIDERS
    # Manual pricing is the exception, not the norm. The Voice Live rows:
    # tokencost's bare "gpt-4.1-mini" key is OpenAI's TEXT rate (16-27x below
    # Voice Live audio-token billing), so AUTO would silently under-price
    # them. The GPT-5 Mini row: its model_string is the aoai-example DEPLOYMENT
    # name, and pricing must not depend on tokencost happening to carry that
    # bare name — same silent-wrong-rate trap. Any new manual row must be
    # added here with an equally good reason.
    manual = {m["name"] for m in seed_agent_models.MODELS
              if m["extra_config"]["pricing_mode"] == "manual"}
    assert manual == {"Azure Voice Live (gpt-4.1-mini)",
                      "Azure Voice Live Native (gpt-realtime)",
                      "Azure GPT-5 Mini (uaenorth)"}
    for m in seed_agent_models.MODELS:
        assert m["extra_config"]["pricing_mode"] in ("auto", "manual")
        assert m["extra_config"]["location"]


def test_catalog_rows_have_manual_cost_rates():
    for m in seed_agent_models.MODELS:
        ec = m["extra_config"]
        assert ec.get("cost_per_1k_in") is not None and ec.get("cost_per_1k_out") is not None
        assert ec["cost_per_1k_out"] >= ec["cost_per_1k_in"]  # output >= input pricing


def test_catalog_models_resolve_auto_price():
    """Every catalog model must resolve a real AUTO (tokencost) price via its
    mapped tokencost_model — not just the manual fallback."""
    import pytest
    pytest.importorskip("tokencost")
    from core import pricing
    for m in seed_agent_models.MODELS:
        if pricing.pricing_mode(m) == "manual":
            # Deliberately manual rows (see test_catalog_providers_and_auto_priced
            # for the allowlist + why) must still resolve REAL positive rates
            # from their own cin/cout — just not via tokencost.
            rates = pricing.effective_rates(m)
            assert rates and rates[0] > 0 and rates[1] > 0, f"{m['name']} has no manual rate"
            continue
        assert pricing.pricing_mode(m) == "auto"
        key = pricing.resolve_tokencost_key(m)
        from core.pricing import TOKEN_COSTS
        assert key in TOKEN_COSTS, f"{m['name']}: tokencost_model {key!r} not in tokencost DB"
        rates = pricing.effective_rates(m)
        assert rates and rates[0] > 0 and rates[1] > 0, f"{m['name']} has no auto rate"


import pytest as _pytest  # noqa: E402


@_pytest.fixture(autouse=True)
def _product_sample_data(product_build):
    """These tests use a product's agent kinds, model rows or seeded suites
    as sample data for generic logic; see tests/conftest.py::product_build."""
