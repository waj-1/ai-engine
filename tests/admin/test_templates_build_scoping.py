"""An agent created from a template must be one the build can run — and so
one its agents list shows. Every mold today is a `business` mold; on the
`agents` partner build (which serves only the `agents_*` kinds) the created
draft never appeared in the list and had no runtime or chat endpoint there.
The gallery now offers only molds of the kinds the build serves, and
use_template refuses the rest before writing anything."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from admin.services import templates as svc
from tests.admin.test_draft_copy_isolation import FakeSupabase
from tests.scoped_build import full_build, use_agents_demo


def _tables():
    return {"agent_profiles": [
        {"profile_id": "t-biz", "agent_kind": "business", "slug": "mold:rag",
         "status": "template", "environment": "staging", "display_name": "RAG"},
        {"profile_id": "t-demo", "agent_kind": "agents_support", "slug": "mold:demo",
         "status": "template", "environment": "staging", "display_name": "Demo mold"},
    ]}


def test_full_build_lists_every_template(monkeypatch):
    full_build(monkeypatch)
    ids = {t["template_id"] for t in svc.list_templates(FakeSupabase(_tables()))}
    assert ids == {"t-biz", "t-demo"}


def test_partner_build_lists_only_kinds_it_serves(monkeypatch):
    use_agents_demo(monkeypatch)
    ids = {t["template_id"] for t in svc.list_templates(FakeSupabase(_tables()))}
    assert ids == {"t-demo"}


def test_partner_build_refuses_a_template_it_cannot_run(monkeypatch):
    use_agents_demo(monkeypatch)
    fake = FakeSupabase(_tables())
    with patch("core.auth.admin_service_client", return_value=fake):
        with pytest.raises(svc.TemplateNotFoundError, match="doesn't run 'business'"):
            svc.use_template(template_id="t-biz", display_name="Demo x", actor_user_id="u")
    assert fake.writes == []
