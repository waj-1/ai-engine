"""Coverage for the operator-authored HTTP-tool adapter.

Focus: the SSRF / secret-exfiltration guard (loopback + link-local blocked,
no auth forwarding to private targets) plus schema translation and the
GET/POST/error response paths. The adapter is sync and returns LLM-readable
strings rather than raising, so we assert on those strings.
"""
import httpx
import pytest

from runtime.tools import _http_adapter as ad


# --- SSRF classification ------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://127.0.0.53:8080/y",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://[::1]/x",
    ],
)
def test_classify_blocks_loopback_and_link_local(url):
    assert ad._classify_endpoint(url) == "blocked"


@pytest.mark.parametrize(
    "url",
    ["http://10.1.2.3/x", "http://192.168.1.10/x", "http://172.16.0.5/x"],
)
def test_classify_marks_rfc1918_private(url):
    assert ad._classify_endpoint(url) == "private"


def test_classify_public_for_routable_ip():
    assert ad._classify_endpoint("https://93.184.216.34/x") == "public"


# --- guard behaviour through the built tool ----------------------------------

def _row(**over):
    row = {
        "python_name": "call_api",
        "description": "calls an api",
        "endpoint_url": "https://api.example.com/v1",
        "http_method": "POST",
        "auth_env_var": "MY_TOKEN",
        "argument_schema": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
    }
    row.update(over)
    return row


def test_blocked_endpoint_is_refused_without_calling(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "supersecret")
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("must not be called")

    monkeypatch.setattr(httpx.Client, "request", _boom)
    tool = ad.build_http_tool(_row(endpoint_url="http://169.254.169.254/token"))
    out = tool.func(q="hi")
    assert "blocked" in out.lower()
    assert called["n"] == 0


def test_auth_not_forwarded_to_private_endpoint(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "supersecret")
    seen = {}

    def _capture(self, method, url, **kwargs):
        seen["headers"] = kwargs.get("headers") or {}
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx.Client, "request", _capture)
    tool = ad.build_http_tool(_row(endpoint_url="http://10.0.0.5/internal"))
    tool.func(q="hi")
    assert "Authorization" not in seen["headers"]


def test_auth_forwarded_to_public_endpoint(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "supersecret")
    seen = {}

    def _capture(self, method, url, **kwargs):
        seen["headers"] = kwargs.get("headers") or {}
        seen["json"] = kwargs.get("json")
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx.Client, "request", _capture)
    tool = ad.build_http_tool(_row(endpoint_url="https://93.184.216.34/v1"))
    tool.func(q="hi")
    assert seen["headers"].get("Authorization") == "Bearer supersecret"
    assert seen["json"] == {"q": "hi"}


# --- method routing + responses ----------------------------------------------

def test_get_uses_params_not_body(monkeypatch):
    seen = {}

    def _capture(self, method, url, **kwargs):
        seen["method"] = method
        seen["params"] = kwargs.get("params")
        seen["json"] = kwargs.get("json")
        return httpx.Response(200, json={"r": 1})

    monkeypatch.setattr(httpx.Client, "request", _capture)
    tool = ad.build_http_tool(_row(http_method="GET", auth_env_var=None,
                                    endpoint_url="https://93.184.216.34/s"))
    tool.func(q="x")
    assert seen["method"] == "GET"
    assert seen["params"] == {"q": "x"}
    assert seen["json"] is None


def test_non_2xx_returns_readable_error(monkeypatch):
    def _capture(self, method, url, **kwargs):
        return httpx.Response(500, text="boom")

    monkeypatch.setattr(httpx.Client, "request", _capture)
    tool = ad.build_http_tool(_row(auth_env_var=None, endpoint_url="https://93.184.216.34/s"))
    out = tool.func(q="x")
    assert "HTTP 500" in out


def test_transport_error_is_non_fatal(monkeypatch):
    def _capture(self, method, url, **kwargs):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx.Client, "request", _capture)
    tool = ad.build_http_tool(_row(auth_env_var=None, endpoint_url="https://93.184.216.34/s"))
    out = tool.func(q="x")
    assert "HTTP request failed" in out


# --- schema translation -------------------------------------------------------

def test_missing_endpoint_url_raises():
    with pytest.raises(ValueError):
        ad.build_http_tool(_row(endpoint_url=""))


def test_non_object_schema_root_raises():
    with pytest.raises(ValueError):
        ad.build_http_tool(_row(argument_schema={"type": "string"}))


def test_required_vs_optional_fields():
    model = ad._model_from_schema(
        "t",
        {
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
            "required": ["a"],
        },
    )
    fields = model.model_fields
    assert fields["a"].is_required()
    assert not fields["b"].is_required()
