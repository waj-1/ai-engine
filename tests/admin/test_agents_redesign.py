"""B1 Agents redesign — list table, tabbed detail view model, pickers.

Covers the pure view-model builders (admin/screens/agents_views.py,
admin/screens/agents_present.py) and the routing that picks list vs detail.
The per-tab forms' wire contracts are pinned in test_agents_form_structure.py;
the browser save round-trip was exercised against a local portal (see the PR).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.admin.conftest import client  # noqa: F401 — session-scoped superuser TestClient

from admin.screens import agents_present as P
from admin.screens import agents_views as V
from tests.scoped_build import full_build, use_agents_demo


# --------------------------------------------------------------------------- #
# Presentation helpers
# --------------------------------------------------------------------------- #
def test_channels_are_plain_words(monkeypatch):
    # A plugin names its own endpoints' channels (agent_catalog.endpoint_channels).
    monkeypatch.setattr(P, "endpoint_channels", lambda: (
        ("/sample/replies", "inbox", "Inbox", "Drafts replies"),
        ("/sample/suggest", "api", "Suggestions", "Called by an app")))
    assert [c["label"] for c in P.channels_for({"endpoint_path": "/chat/stream"})] == ["Chat"]
    assert [c["label"] for c in P.channels_for({"endpoint_path": "/sample/replies"})] == ["Inbox"]
    assert [c["label"] for c in P.channels_for({"endpoint_path": "/sample/suggest"})] == ["Suggestions"]
    assert [c["label"] for c in P.channels_for({"endpoint_path": "/other/api"})] == ["API"]
    assert P.channel_filter_options() == [
        ("chat", "Chat"), ("voice", "Voice"), ("inbox", "Inbox"),
        ("api", "API / suggestions"), ("delegated", "Via another agent")]
    both = P.channels_for({"endpoint_path": "/orchard/chat/stream",
                           "voice_config": {"model_id": "m"}})
    assert [c["key"] for c in both] == ["chat", "voice"]
    sub = P.channels_for({"parent_profile_id": "p"})
    assert sub[0]["label"] == "Via another agent"
    assert P.channels_for({})[0]["key"] == "none"


def test_malformed_voice_config_never_raises():
    assert P.has_voice({"voice_config": "garbage"}) is False
    assert P.voice_block({"voice_config": None}) == {}


def test_toolset_label_hides_the_engineering_name():
    assert P.toolset_label({"name": "__builtin__:business"}) == "Business tools"
    assert P.toolset_label({"name": "orchard-mock"}) == "Orchard mock"


def test_status_buckets_specialist_as_active():
    s = P.status_info("specialist")
    assert s["bucket"] == "active" and s["label"] == "Specialist"
    assert P.status_info(None)["key"] == "active"
    assert P.status_info("template")["bucket"] == "template"


def test_relative_time():
    now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
    assert P.relative_time((now - timedelta(days=3)).isoformat(), now) == "3 days ago"
    assert P.relative_time((now - timedelta(seconds=5)).isoformat(), now) == "just now"
    assert P.relative_time(None, now) == ""


def test_action_groups():
    assert P.action_group("get_bookings") == "Read data"
    assert P.action_group("propose_create_booking") == "Make changes"
    assert P.action_group("summarize") == "Other"


# --------------------------------------------------------------------------- #
# Detail view model
# --------------------------------------------------------------------------- #
_BUNDLE = {"mcp_server_id": "m1", "name": "__builtin__:sales", "source": "bundle",
           "is_builtin": True, "endpoint_only": False}


def _ctx(**over):
    me = {"profile_id": "p1", "agent_kind": "sales", "slug": "default",
          "display_name": "Sales", "status": "active", "endpoint_path": "/chat/stream",
          "data_source_ids": ["s1", "s-hidden"], "downstream_profile_ids": ["p2"],
          "model_main_id": "mod1"}
    spec = {"profile_id": "p2", "agent_kind": "sales_spec", "slug": "spec",
            "display_name": "Specialist", "status": "specialist", "delegatable": True,
            "description": "Handles reports."}
    tmpl = {"profile_id": "t1", "agent_kind": "sales", "slug": "mold", "display_name": "Mold",
            "status": "template"}
    ctx = {
        "active_profile": me,
        "profiles": [me, spec, tmpl],
        "top_level_profiles": [me, spec, tmpl],
        "sub_profiles_by_parent": {},
        "tools": [{"python_name": "get_sales", "display_name": "Get sales"},
                  {"python_name": "export_csv", "display_name": "Export CSV"}],
        "data_sources": [{"source_id": "s1", "name": "sales_docs", "kind": "pgvector"},
                         {"source_id": "s2", "name": "Other docs", "kind": "pgvector"}],
        "mcp_attachments": [{"mcp_server": _BUNDLE, "tool_subset": ["get_sales"]}],
        "attachable_mcps": [
            {**_BUNDLE, "tool_names": ["get_sales", "export_csv"]},
            {"mcp_server_id": "m2", "name": "__builtin__:support", "source": "bundle",
             "tool_names": ["get_ticket"]},
        ],
        "bound_tools": {"groups": [{"mcp_server_id": "m1",
                                    "tools": [{"python_name": "get_sales"}]}],
                        "external": [], "bound_count": 1, "legacy_not_bound": []},
        "prompts": [{"prompt_id": "a", "slug": "system", "content": "Line one\nLine two"},
                    {"prompt_id": "b", "slug": "title", "content": "t"},
                    {"prompt_id": "c", "slug": "mold:blank", "content": "m"}],
        "all_models": [{"model_id": "mod1", "name": "Gemini 3 Flash", "model_string": "g3"}],
        "mapped": {"source_ids": {"s1", "s-hidden"}, "downstream_ids": {"p2"}},
        "downstream_candidates": [spec, tmpl],
    }
    ctx.update(over)
    return ctx


def test_only_attached_toolsets_are_listed_and_the_rest_are_pickable():
    v = V.build_detail_view(_ctx(), "staging")
    assert [t["mcp_server_id"] for t in v["toolsets"]] == ["m1"]
    t = v["toolsets"][0]
    assert t["label"] == "Sales tools" and t["mode"] == "list"
    assert t["count_label"] == "1 of 2 actions"
    assert [c["name"] for c in t["chips"]] == ["get_sales"]
    cat = {c["id"]: c for c in v["toolset_catalog"]}
    assert cat["m1"]["attached"] is True and cat["m2"]["attached"] is False
    assert [a["label"] for a in cat["m1"]["actions"]] == ["Get sales", "Export CSV"]


def test_knowledge_lists_attached_and_keeps_hidden_ids_for_the_post():
    v = V.build_detail_view(_ctx(), "staging")
    assert [k["id"] for k in v["knowledge_attached"]] == ["s1"]
    assert v["knowledge_attached"][0]["label"] == "Sales docs"
    # An attached id the (scope-filtered) catalog can't show still posts back.
    assert v["hidden_source_ids"] == ["s-hidden"]


def test_subagent_picker_excludes_templates_and_self():
    v = V.build_detail_view(_ctx(), "staging")
    ids = [c["id"] for c in v["sub_catalog"]]
    assert ids == ["p2"]
    assert v["sub_catalog"][0]["attached"] is True
    assert [s["label"] for s in v["sub_selected"]] == ["Specialist"]


def test_template_prompts_are_split_off_on_a_real_agent():
    v = V.build_detail_view(_ctx(), "staging")
    assert [p["slug"] for p in v["other_prompts"]] == ["title"]
    assert [p["slug"] for p in v["template_prompts"]] == ["mold:blank"]
    assert v["system_prompt_preview"] == "Line one Line two"


def test_tabs_voice_only_when_configured_and_promote_target_by_env():
    v = V.build_detail_view(_ctx(), "staging")
    keys = [k for k, _l, _h in v["detail_tabs"]]
    assert "voice" not in keys
    assert keys[0] == "overview" and "settings" in keys
    assert v["promote_target"]["env"] == "prod"
    assert V.build_detail_view(_ctx(), "prod")["promote_target"] is None
    me = dict(_ctx()["active_profile"], voice_config={"agent_profile_slug": "x"})
    keys = [k for k, _l, _h in V.build_detail_view(_ctx(active_profile=me), "staging")["detail_tabs"]]
    assert "voice" in keys


def test_model_is_named_not_an_id():
    v = V.build_detail_view(_ctx(), "staging")
    assert v["main_model"]["name"] == "Gemini 3 Flash"


def test_minimal_context_still_builds():
    """Tests patch load_agents_context with minimal dicts; the builder must
    tolerate every missing key."""
    v = V.build_detail_view({"active_profile": {"profile_id": "x", "agent_kind": "k"}}, "staging")
    assert v["agent"]["id"] == "x" and v["toolsets"] == []


def test_normalize_tab():
    assert V.normalize_tab(None) == "overview"
    assert V.normalize_tab("agents") == "overview"
    assert V.normalize_tab("tools") == "tools"
    assert V.normalize_tab("bogus") == "overview"


# --------------------------------------------------------------------------- #
# List loader (fake client)
# --------------------------------------------------------------------------- #
class _Q:
    def __init__(self, rows):
        self._rows = list(rows)

    def select(self, *_a, **_k):
        return self

    def eq(self, k, v):
        self._rows = [r for r in self._rows if r.get(k) == v]
        return self

    def order(self, *_a, **_k):
        return self

    def execute(self):
        return type("R", (), {"data": self._rows})()


class _Client:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Q(self.tables.get(name, []))


def _list_tables():
    return {
        "agent_profiles": [
            {"profile_id": "a", "agent_kind": "business", "slug": "default", "display_name": "WAJ AI",
             "status": "active", "environment": "staging", "endpoint_path": "/chat/stream",
             "model_main_id": "m", "data_source_ids": ["s"], "downstream_profile_ids": []},
            {"profile_id": "b", "agent_kind": "business", "slug": "voice", "display_name": "WAJ Voice",
             "status": "specialist", "environment": "staging", "parent_profile_id": "a"},
            {"profile_id": "t", "agent_kind": "business", "slug": "mold", "display_name": "Mold",
             "status": "template", "environment": "staging"},
            {"profile_id": "x", "agent_kind": "business", "slug": "other", "display_name": "Prod one",
             "status": "active", "environment": "prod"},
        ],
        "agent_models": [{"model_id": "m", "name": "Gemini 3 Flash"}],
        "agent_profile_mcp_servers": [{"profile_id": "a", "mcp_server_id": "s1", "tool_subset": None}],
        "agent_mcp_servers": [{"mcp_server_id": "s1", "name": "__builtin__:business",
                               "source": "bundle", "endpoint_only": False, "is_active": True}],
        "agent_mcp_tools": [{"mcp_server_id": "s1", "tool_id": "t1"},
                            {"mcp_server_id": "s1", "tool_id": "t2"}],
        "agent_tools": [{"tool_id": "t1", "python_name": "get_a", "is_deprecated": False},
                        {"tool_id": "t2", "python_name": "get_b", "is_deprecated": False}],
        "agent_profile_skills": [],
    }


def test_list_nests_subagents_flags_templates_and_counts_bound_tools():
    with patch("admin.router.client_for", return_value=_Client(_list_tables())):
        data = V.load_agents_list("staging")
    rows = {r["profile_id"]: r for r in data["agent_rows"]}
    assert set(rows) == {"a", "t"}            # sub-agent nested, prod row excluded
    a = rows["a"]
    assert [c["profile_id"] for c in a["children"]] == ["b"]
    assert a["children"][0]["type_label"] == "Sub-agent"
    assert a["children"][0]["parent_name"] == "WAJ AI"
    assert a["model_name"] == "Gemini 3 Flash"
    assert a["tool_count"] == 2 and a["can_use"].startswith("2 actions")
    assert rows["t"]["type_key"] == "template"
    assert data["agent_counts"] == {"all": 2, "templates": 1, "draft": 0}
    # Templates sort last.
    assert data["agent_rows"][-1]["profile_id"] == "t"


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
def _list_payload():
    return {"agent_rows": [{
        "profile_id": "a", "name": "WAJ AI", "description": "", "status": P.status_info("active"),
        "type_key": "main", "type_label": "Main agent", "parent_name": None,
        "channels": P.channels_for({"endpoint_path": "/chat/stream"}), "channel_keys": "chat",
        "model_name": "Gemini", "can_use": "3 actions", "updated_rel": "1 day ago",
        "updated_at": "", "updated_by_you": False, "children": [], "search": "waj ai",
        "family_search": "waj ai"}],
        "agent_counts": {"all": 1, "templates": 0, "draft": 0}, "compare_options": []}


def test_bare_agents_url_is_the_table(client):  # noqa: F811
    with patch("admin.screens.agents_views.load_agents_list", return_value=_list_payload()):
        r = client.get("/admin/agents")
    assert r.status_code == 200
    assert "data-agents-table" in r.text
    assert "WAJ AI" in r.text and "Main agent" in r.text
    assert "That agent isn" not in r.text


def test_missing_profile_shows_the_list_with_a_notice(client):  # noqa: F811
    with patch("admin.screens.agents.load_agents_context",
               return_value={"active_profile": {"profile_id": "other"}, "profile_missing": True}), \
         patch("admin.screens.agents_views.load_agents_list", return_value=_list_payload()):
        r = client.get("/admin/agents?profile_id=nope")
    assert r.status_code == 200
    assert "That agent isn" in r.text and "data-agents-table" in r.text


def test_voice_tab_redirects_to_the_voice_screen(client):  # noqa: F811
    with patch("admin.screens.agents.load_agents_context",
               return_value={"active_profile": {"profile_id": "p1", "agent_kind": "k"}}):
        r = client.get("/admin/agents?profile_id=p1&tab=voice&env=staging", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/admin/agents/p1/voice?env=staging"


def test_detail_header_has_switcher_test_and_promote_behind_a_confirm(client):  # noqa: F811
    ap = {"profile_id": "p1", "agent_kind": "sales", "slug": "default", "display_name": "Sales",
          "status": "active", "endpoint_path": "/chat/stream"}
    with patch("admin.screens.agents.load_agents_context",
               return_value={"active_profile": ap, "profiles": [ap], "top_level_profiles": [ap]}), \
         patch("admin.screens.agents_views.promote_counterpart", return_value=None):
        r = client.get("/admin/agents?profile_id=p1&env=staging")
    assert r.status_code == 200
    assert "data-switcher" in r.text
    assert "data-test-chat-open" in r.text
    # Promote is a menu item opening a confirm dialog, not a primary button.
    assert 'data-open-dialog="dlg-promote"' in r.text
    assert '<dialog id="dlg-promote"' in r.text
    assert 'action="/admin/env/promote"' in r.text
    # The right rail is gone.
    assert "Tools mapped" not in r.text and "Expected request body" not in r.text


# --------------------------------------------------------------------------- #
# Other screens
# --------------------------------------------------------------------------- #
def test_diff_display_names_models_and_parent():
    from admin.profile_fork_handlers import diff_display

    tables = {"agent_models": [{"model_id": "m1", "name": "Gemini 3 Flash"}],
              "agent_profiles": [{"profile_id": "par", "display_name": "WAJ AI"}]}

    class _In(_Q):
        def in_(self, k, vals):
            self._rows = [r for r in self._rows if r.get(k) in vals]
            return self

    class _C(_Client):
        def table(self, name):
            return _In(self.tables.get(name, []))

    out = diff_display(_C(tables), {"model_main_id": "m1"}, {"parent_profile_id": "par"})
    assert out["value_names"] == {"m1": "Gemini 3 Flash", "par": "WAJ AI"}
    assert out["field_labels"]["model_main_id"] == "Model"


def test_templates_gallery_has_a_parent_picker_and_no_escaped_entities(client, monkeypatch):  # noqa: F811
    from admin import templates_handlers

    monkeypatch.setattr(templates_handlers, "list_templates", lambda c: [{
        "template_id": "t1", "slug": "s", "display_name": "Blank", "agent_kind": "business",
        "description": "d", "is_composite": False, "sub_agent_count": 0, "knowledge_count": 2}])
    monkeypatch.setattr(templates_handlers, "_parent_options",
                        lambda c: [{"profile_id": "par-1", "name": "WAJ AI"}])
    r = client.get("/admin/templates")
    assert r.status_code == 200
    assert '<select name="as_subagent_of">' in r.text
    assert '<option value="par-1">WAJ AI</option>' in r.text
    assert "mold" not in r.text.lower().replace("molds", "")  # "template", not "mold"
    assert "&amp;lt;" not in r.text  # ask_<slug> renders, not ask_&lt;slug&gt;
    assert "Includes 2 knowledge sources" in r.text


def test_prompt_pages_return_to_the_agent():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "admin" / "templates"
    for name in ("prompt_compare.html", "prompt_optimize.html", "prompt_diff.html"):
        src = (root / name).read_text()
        assert "profile_id={{ back_pid|urlencode }}&tab=instructions" in src, name


def test_single_env_build_offers_no_promote_or_production_compare(monkeypatch):
    # A partner (scoped) build has one environment: "Promote to
    # Production…" and "Compare with Production" pointed at nothing.
    use_agents_demo(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    assert V.build_detail_view(_ctx(), "staging")["promote_target"] is None
    full_build(monkeypatch)
    assert V.build_detail_view(_ctx(), "staging")["promote_target"]["env"] == "prod"
