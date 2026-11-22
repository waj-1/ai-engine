import admin_mcp.tools_configure as tc
from tests.admin_mcp.conftest import FakeClient

TMPL = {"profile_id": "t1", "slug": "mold:rag", "status": "template",
        "agent_kind": "business", "environment": "staging",
        "display_name": "RAG Mold", "downstream_profile_ids": []}


def test_templates_list(monkeypatch):
    client = FakeClient({"agent_profiles": [dict(TMPL)]})
    monkeypatch.setattr(tc, "client_for_env", lambda env: client)
    out = tc.templates(action="list")
    assert out["templates"][0]["slug"] == "mold:rag"


def test_templates_use_delegates(monkeypatch):
    monkeypatch.setattr(tc, "client_for_env", lambda env: FakeClient({}))
    seen = {}
    import admin.services.templates as st
    monkeypatch.setattr(st, "use_template",
                        lambda **kw: seen.update(kw) or {"root": "new1", "created": ["new1"]})
    out = tc.templates(action="use", template_id="t1", display_name="Support Bot")
    assert out["root"] == "new1"
    assert seen["display_name"] == "Support Bot"


def test_templates_unknown_action(monkeypatch):
    monkeypatch.setattr(tc, "client_for_env", lambda env: FakeClient({}))
    assert "error" in tc.templates(action="bogus")
