"""core/skills.py — entry hydration, tenant cache, merge semantics, validation."""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import patch

import core.skills as skills_mod
from core.skills import (
    fetch_skill_attachments,
    invalidate_skills_cache,
    resolve_skills_for_request,
    resolve_tenant_skills,
    validate_skill_metadata,
)


class _FakeQuery:
    """Chainable PostgREST stub: .select/.eq/.in_/.is_/.order/.limit -> .execute()."""

    def __init__(self, rows_by_table: Dict[str, List[dict]], table: str):
        self._rows = rows_by_table
        self._table = table
        self._filters: List[tuple] = []

    def select(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def is_(self, col, val):
        self._filters.append(("is", col, val))
        return self

    def in_(self, col, vals):
        self._filters.append(("in", col, list(vals)))
        return self

    def execute(self):
        out = []
        for row in self._rows.get(self._table, []):
            ok = True
            for op, col, val in self._filters:
                if op == "eq" and row.get(col) != val:
                    ok = False
                elif op == "in" and row.get(col) not in val:
                    ok = False
                elif op == "is" and not (
                    (val in ("null", None) and row.get(col) is None)
                ):
                    ok = False
            if ok:
                out.append(row)

        class _R:  # PostgREST response shape
            data = out

        return _R()


class _FakeClient:
    def __init__(self, rows_by_table: Dict[str, List[dict]]):
        self._rows = rows_by_table

    def table(self, name: str) -> _FakeQuery:
        return _FakeQuery(self._rows, name)


def _fixture_rows() -> Dict[str, List[dict]]:
    return {
        "agent_profile_skills": [
            {"profile_id": "p1", "skill_id": "s1", "position": 0},
        ],
        "agent_skills": [
            {"skill_id": "s1", "slug": "refund-note", "display_name": "Refund note",
             "description": "format a refund note", "agent_kind": "support_bot",
             "business_id": None, "always_on": False,
             "linked_data_source_ids": ["ds1"], "is_active": True, "status": "active"},
            {"skill_id": "s2", "slug": "no-shows", "display_name": "No-shows",
             "description": "our no-show policy", "agent_kind": "support_bot",
             "business_id": "biz1", "always_on": True,
             "linked_data_source_ids": [], "is_active": True, "status": "active"},
            {"skill_id": "s3", "slug": "archived", "display_name": "Old",
             "description": "gone", "agent_kind": "support_bot",
             "business_id": "biz1", "always_on": False,
             "linked_data_source_ids": [], "is_active": False, "status": "archived"},
        ],
        "agent_skill_versions": [
            {"skill_version_id": "v1", "skill_id": "s1", "version_number": 1,
             "content": "old body"},
            {"skill_version_id": "v2", "skill_id": "s1", "version_number": 2,
             "content": "Refund body v2"},
            {"skill_version_id": "v3", "skill_id": "s2", "version_number": 1,
             "content": "tenant body"},
        ],
        "agent_skill_resources": [
            {"resource_id": "r1", "skill_version_id": "v2", "filename": "examples.md",
             "content_type": "text/markdown", "content": "ex", "position": 0},
        ],
        "agent_data_sources": [
            {"source_id": "ds1", "name": "Policies KB"},
        ],
    }


def setup_function(_fn):
    invalidate_skills_cache()


def test_fetch_skill_attachments_hydrates_entry():
    client = _FakeClient(_fixture_rows())
    entries = fetch_skill_attachments(client, "p1")
    assert len(entries) == 1
    e = entries[0]
    assert e["skill"]["slug"] == "refund-note"
    assert e["version"]["version_number"] == 2          # latest wins
    assert e["version"]["content"] == "Refund body v2"
    assert e["resources"][0]["filename"] == "examples.md"
    assert e["linked_data_sources"] == [{"source_id": "ds1", "name": "Policies KB"}]
    assert e["scope"] == "platform"


def test_fetch_skill_attachments_never_raises():
    class _Boom:
        def table(self, _n):
            raise RuntimeError("db down")

    assert fetch_skill_attachments(_Boom(), "p1") == []


def test_resolve_tenant_skills_filters_and_caches():
    client = _FakeClient(_fixture_rows())
    with patch.object(skills_mod, "get_service_client", return_value=client) as gc:
        first = resolve_tenant_skills("support_bot", "biz1")
        assert [e["skill"]["slug"] for e in first] == ["no-shows"]  # s3 inactive → excluded
        assert first[0]["scope"] == "tenant"
        second = resolve_tenant_skills("support_bot", "biz1")
        assert second == first
        assert gc.call_count == 1                        # cached — one DB roundtrip
        invalidate_skills_cache()
        resolve_tenant_skills("support_bot", "biz1")
        assert gc.call_count == 2


def test_resolve_skills_for_request_merges_platform_wins():
    platform = [{"skill": {"slug": "dup", "always_on": False}, "version": None,
                 "resources": [], "linked_data_sources": [], "position": 0,
                 "scope": "platform"}]

    class _P:  # minimal Profile stand-in
        agent_kind = "support_bot"
        skill_attachments = platform

    tenant = [{"skill": {"slug": "dup"}, "scope": "tenant"},
              {"skill": {"slug": "uniq"}, "scope": "tenant"}]
    with patch.object(skills_mod, "resolve_tenant_skills", return_value=tenant):
        merged = resolve_skills_for_request(_P(), {"business_id": "biz1"})
    assert [e["skill"]["slug"] for e in merged] == ["dup", "uniq"]
    assert merged[0]["scope"] == "platform"              # platform shadows tenant


def test_resolve_skills_for_request_no_business_id_skips_tenant():
    class _P:
        agent_kind = "support_bot"
        skill_attachments = []

    with patch.object(skills_mod, "resolve_tenant_skills") as rt:
        assert resolve_skills_for_request(_P(), {}) == []
        rt.assert_not_called()


def test_validate_skill_metadata():
    assert validate_skill_metadata("refund-note", "Refund", "desc") is None
    assert "slug" in validate_skill_metadata("Bad Slug!", "Refund", "desc")
    assert "display_name" in validate_skill_metadata("ok-slug", "", "desc")
    assert "{" in validate_skill_metadata("ok-slug", "Refund", "has {brace}")
