"""Tests for runtime/tools/_schema.py — shared JSON-schema -> pydantic bridge."""
import pytest


def test_model_from_schema_required_and_optional():
    from runtime.tools._schema import model_from_schema

    model = model_from_schema("my_tool", {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "the query"},
            "limit": {"type": "integer", "default": 5},
        },
        "required": ["query"],
    })
    fields = model.model_fields
    assert fields["query"].is_required()
    assert not fields["limit"].is_required()
    parsed = model(query="hello")
    assert parsed.query == "hello"


def test_model_from_schema_rejects_non_object_root():
    from runtime.tools._schema import model_from_schema

    with pytest.raises(ValueError):
        model_from_schema("bad", {"type": "string"})


def test_http_adapter_uses_shared_model_builder():
    # The move must not orphan the http adapter — it now imports the shared fn.
    from runtime.tools import _http_adapter
    from runtime.tools._schema import model_from_schema

    assert _http_adapter._model_from_schema is model_from_schema
