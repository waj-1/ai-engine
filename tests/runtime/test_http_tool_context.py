"""build_http_tool with context_fields: schema stripping + call-time injection."""
import json

import httpx


def _row():
    return {
        "python_name": "crm_lookup",
        "description": "Look up a CRM record",
        "endpoint_url": "https://example.test/crm",
        "http_method": "POST",
        "timeout_seconds": 5,
        "argument_schema": {
            "type": "object",
            "properties": {
                "record_id": {"type": "string"},
                "tenant_token": {"type": "string"},
            },
            "required": ["record_id", "tenant_token"],
        },
    }


def test_context_fields_stripped_from_llm_schema():
    from runtime.tools._http_adapter import build_http_tool

    tool = build_http_tool(
        _row(),
        context_fields={"tenant_token": "$ctx.user_token"},
        ctx={"user_token": "tok-9"},
    )
    schema = tool.args_schema.model_json_schema()
    assert "tenant_token" not in schema["properties"]
    assert "record_id" in schema["properties"]


def test_context_fields_injected_into_request_body(monkeypatch):
    from runtime.tools import _http_adapter

    captured = {}

    class _FakeResp:
        status_code = 200
        def json(self):
            return {"ok": True}
        text = '{"ok": true}'

    class _FakeClient:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def request(self, method, url, **kwargs):
            captured["json"] = kwargs.get("json")
            return _FakeResp()

    monkeypatch.setattr(_http_adapter.httpx, "Client", _FakeClient)
    tool = _http_adapter.build_http_tool(
        _row(),
        context_fields={"tenant_token": "$ctx.user_token"},
        ctx={"user_token": "tok-9"},
    )
    out = tool.invoke({"record_id": "r-1"})
    assert json.loads(out) == {"ok": True}
    # Injected credential reached the request; LLM-supplied args preserved.
    assert captured["json"] == {"record_id": "r-1", "tenant_token": "tok-9"}


def test_no_context_fields_is_backward_compatible():
    from runtime.tools._http_adapter import build_http_tool

    tool = build_http_tool(_row())
    schema = tool.args_schema.model_json_schema()
    assert "tenant_token" in schema["properties"]
