from admin.router import _read_model_form


class _Form(dict):
    def get(self, k, default=None): return super().get(k, default)


def test_read_model_form_persists_pricing_fields():
    form = _Form({
        "name": "Claude Sonnet 4.6", "provider": "anthropic",
        "model_string": "claude-sonnet-4@20250101",
        "pricing_mode": "auto", "tokencost_model": "claude-sonnet-4",
    })
    payload = _read_model_form(form)
    assert payload["extra_config"]["pricing_mode"] == "auto"
    assert payload["extra_config"]["tokencost_model"] == "claude-sonnet-4"


def test_read_model_form_persists_thinking_config():
    """Thinking controls land in extra_config: thinking_level validated +
    lowercased; thinking_budget coerced to int."""
    form = _Form({
        "name": "Gemini 3 Flash", "provider": "google_genai",
        "model_string": "gemini-3-flash-preview", "thinking_level": "Low",
    })
    extra = _read_model_form(form)["extra_config"]
    assert extra["thinking_level"] == "low"


def test_read_model_form_thinking_budget_int_and_invalid_level_dropped():
    form = _Form({
        "name": "g", "provider": "google_genai", "model_string": "gemini-2.5-flash",
        "thinking_budget": "1024", "thinking_level": "bogus",
    })
    extra = _read_model_form(form)["extra_config"]
    assert extra["thinking_budget"] == 1024 and isinstance(extra["thinking_budget"], int)
    assert "thinking_level" not in extra  # invalid level is dropped, not forwarded


def test_buildable_providers_accepted():
    for prov in ("google_genai", "openai", "azure_openai", "anthropic"):
        form = _Form({"name": "x", "provider": prov, "model_string": "y"})
        # should not raise on provider validation
        _read_model_form(form)


def test_vertex_providers_rejected_at_save_time():
    """The three Vertex providers were removed 2026-08-16 with their builders.
    The form must reject them here rather than let a row be saved that only
    fails later, at request time, inside build_chat_model."""
    import pytest
    from fastapi import HTTPException

    for prov in ("vertex_ai", "anthropic_vertex", "vertex_maas"):
        form = _Form({"name": "x", "provider": prov, "model_string": "y"})
        with pytest.raises(HTTPException) as exc:
            _read_model_form(form)
        assert exc.value.status_code == 400
