"""Extra run-context id fields (admin/eval_context.py) come from a plugin's
agent_catalog hook; a build with no plugin offers none."""
from types import SimpleNamespace

import pytest

from admin import eval_context
from admin import evals


@pytest.fixture
def no_plugin(monkeypatch):
    monkeypatch.setattr("core.plugins.load_hook", lambda name: [])


@pytest.fixture
def ticket_field(monkeypatch):
    hook = SimpleNamespace(eval_context_fields=lambda: (("ticket_id", "Ticket id", "TCK-…"),))
    monkeypatch.setattr("core.plugins.load_hook", lambda name: [hook] if name == "agent_catalog" else [])


def test_no_plugin_has_no_extra_fields(no_plugin):
    assert eval_context.eval_context_fields() == ()
    assert eval_context.run_context_from_form({"ctx_ticket_id": "T1"}) == {}


def test_plugin_fields_are_read_from_run_forms(ticket_field):
    assert eval_context.eval_context_keys() == ("ticket_id",)
    assert eval_context.run_context_from_form({"ctx_ticket_id": " T1 ", "ctx_other": "x"}) == {"ticket_id": "T1"}
    assert eval_context.run_context_from_form({"ctx_ticket_id": "  "}) == {}


def test_plugin_fields_are_forwarded_to_the_agent(ticket_field):
    body = evals._payload_for({"question": "hi", "ticket_id": "T2"}, run_context={"customer_id": "c1"})
    assert body["ticket_id"] == "T2" and body["customer_id"] == "c1"


def test_suite_form_shows_plugin_fields_only(monkeypatch, client, ticket_field):
    from admin import router

    html = router.templates.env.from_string(
        "{% for key, label, _p in eval_context_fields() %}[{{ key }}:{{ label }}]{% endfor %}").render()
    assert html == "[ticket_id:Ticket id]"


def test_no_plugin_does_not_forward_unknown_ids(no_plugin):
    body = evals._payload_for({"question": "hi", "ticket_id": "T2"}, run_context={"customer_id": "c1"})
    assert "ticket_id" not in body and body["customer_id"] == "c1"
