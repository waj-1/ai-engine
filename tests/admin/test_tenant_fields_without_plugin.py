"""Business fields appear only where a plugin has businesses.

A build with no plugin has no tenants, so a Business picker or a
"Business ID" box lists nothing and asks for something that does not exist.
The `agent_catalog.business_field_label()` hook decides (admin/eval_context.py):
with no label the field is hidden, or the wording is neutral where the
concept is real in the engine (voice budgets, which are keyed by an id per
agent). With the label ("Business" from the product plugin) every screen
renders as before.
"""
from __future__ import annotations

import pytest

from admin.router import templates

NO_TENANTS = ""


@pytest.fixture
def label(monkeypatch):
    """Set the tenant label the templates see ("" = no plugin with tenants)."""
    def _set(value: str) -> None:
        monkeypatch.setitem(templates.env.globals, "business_field_label", lambda: value)
    return _set


@pytest.fixture
def compare_page(client, monkeypatch):
    from admin import prompt_handlers

    monkeypatch.setattr(prompt_handlers, "_prompt_compare_context", lambda _c, _pid: {
        "prompt": {"prompt_id": "p1", "agent_kind": "custom", "slug": "support:system"},
        "suites": [], "versions": [], "profiles": [], "businesses": [],
        "active_version_number": None,
    })
    return lambda: client.get("/admin/prompts/p1/compare")


def test_prompt_compare_hides_tenant_context_without_tenants(compare_page, label):
    label(NO_TENANTS)
    page = compare_page()
    assert page.status_code == 200
    assert 'name="ctx_business_id"' not in page.text
    assert 'name="ctx_branch_id"' not in page.text
    assert "No businesses loaded" not in page.text
    label("Business")
    page = compare_page()
    assert 'name="ctx_business_id"' in page.text and 'name="ctx_branch_id"' in page.text


def test_voice_budgets_use_neutral_wording_without_tenants(client, label, monkeypatch):
    from admin import router

    monkeypatch.setattr(router, "collect_voice_budgets", lambda _c: {
        "voice_budget_rows": [{"business_id": "b-1", "daily_seconds": 600, "used_today": 0,
                               "note": "", "agent_name": "Support desk"}],
        "voice_budget_unbudgeted": [], "voice_budget_default_seconds": 900,
    })
    label(NO_TENANTS)
    page = client.get("/admin/voice-budgets")
    assert page.status_code == 200
    for word in ("Business ID", "a business", "Businesses without", "each business",
                 ">Business<"):
        assert word not in page.text, word
    assert "Budget ID" in page.text and "each agent" in page.text
    assert "Support desk" in page.text          # the agent behind the id
    label("Business")
    page = client.get("/admin/voice-budgets")
    assert "Business ID" in page.text and "Businesses without their own budget" in page.text
    assert "Support desk" not in page.text      # private wording unchanged


def test_skills_hide_the_business_scope_without_tenants(client, label):
    label(NO_TENANTS)
    page = client.get("/admin/skills")
    assert page.status_code == 200
    assert 'name="business_id"' not in page.text and "every business" not in page.text
    label("Business")
    page = client.get("/admin/skills")
    assert 'name="business_id"' in page.text


def test_channel_form_has_no_business_picker_without_tenant_kinds(client, monkeypatch):
    from admin import channel_handlers

    monkeypatch.setattr(channel_handlers, "_TENANT_KINDS", ())
    page = client.get("/admin/channels/new")
    assert page.status_code == 200
    assert 'id="business-field"' not in page.text
    monkeypatch.setattr(channel_handlers, "_TENANT_KINDS", ("tenant_site",))
    page = client.get("/admin/channels/new")
    assert 'id="business-field"' in page.text


def test_memory_add_form_asks_for_no_business_without_tenants(client, label):
    from admin.memory_handlers import _default_agent_kind

    url = "/admin/memory?user=00000000-0000-0000-0000-0000000000aa"
    label(NO_TENANTS)
    page = client.get(url)
    assert page.status_code == 200
    assert "Add a memory manually" in page.text
    assert 'name="business_id"' not in page.text
    if not _default_agent_kind().startswith("custom"):
        # A product kind keeps its tenant id where the plugin has tenants.
        label("Business")
        assert 'name="business_id"' in client.get(url).text


def test_conversation_details_hide_tenant_ids_without_tenants():
    src = templates.env.loader.get_source(templates.env, "observability_explorer.html")[0]
    assert '_kv("Business id"' not in src
    assert "const BIZ_LABEL = {{ business_field_label() | tojson }};" in src
