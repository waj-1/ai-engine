import json
from main import _parse_model_override_header


def test_parse_valid_header():
    payload = {"model_string": "claude-sonnet-4", "provider": "anthropic"}
    assert _parse_model_override_header(json.dumps(payload)) == payload


def test_parse_rejects_missing_model_string():
    assert _parse_model_override_header(json.dumps({"provider": "x"})) is None


def test_parse_rejects_junk():
    assert _parse_model_override_header("not json") is None
    assert _parse_model_override_header(None) is None
    assert _parse_model_override_header("[]") is None
