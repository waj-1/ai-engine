"""Global search (/admin/search.json) — endpoint, ranking, scopes, docs.

The DB-backed sources run against the conftest's empty stub client here;
people/record sources with real rows are covered in test_search_people.py.
"""
from __future__ import annotations

import pytest


def _payload(client, q, env="staging", method="post"):
    if method == "get":
        r = client.get("/admin/search.json", params={"q": q, "env": env})
    else:
        r = client.post("/admin/search.json", json={"q": q, "env": env})
    assert r.status_code == 200, r.text[:300]
    return r.json()


def _results(client, q, env="staging", method="post"):
    return _payload(client, q, env, method)["results"]


def _hit(rows, label, type_=None):
    return next((r for r in rows if r["label"] == label and (type_ is None or r["type"] == type_)), None)


# --- shape -------------------------------------------------------------------


def test_result_shape_type_shown_once(client):
    rows = _results(client, "tools")
    assert rows
    for r in rows:
        assert {"group", "type", "label", "context", "sublabel", "href", "icon"} <= set(r)
        assert "_rank" not in r
        # The type is a field of its own, never repeated in the title/context.
        assert r["label"] != r["type"]
        assert r["context"] != r["type"]


def test_get_and_post_return_the_same(client):
    assert _results(client, "drift", method="get") == _results(client, "drift", method="post")


def test_empty_query_returns_nothing(client):
    p = _payload(client, "")
    assert p["results"] == [] and p["notes"] == []


def test_bad_env_falls_back_to_staging_on_post(client):
    rows = _results(client, "overview", env="dev")
    assert _hit(rows, "Overview")["href"].endswith("env=staging")


def test_env_threaded_into_href(client):
    assert "env=prod" in _hit(_results(client, "overview", env="prod"), "Overview")["href"]


# --- old names keep working ----------------------------------------------------


@pytest.mark.parametrize("q,label,href", [
    ("dashboard", "Overview", "/admin/live"),
    ("live", "Overview", "/admin/live"),
    ("traces", "Conversations", "/admin/observability"),
    ("observability", "Conversations", "/admin/observability"),
    ("sessions", "Conversations", "/admin/observability"),
    ("audit", "Activity", "/admin/audit"),
    ("evals", "Testing", "/admin/evals"),
    ("kb", "Knowledge", "/admin/kb"),
    ("profiles", "Agents", "/admin/agents"),
    ("mcp", "Tools & integrations", "/admin/tools"),
])
def test_old_names_find_the_renamed_screen(client, q, label, href):
    hit = _hit(_results(client, q), label)
    assert hit is not None, f"{q!r} should find {label!r}"
    assert hit["href"].startswith(href)


@pytest.mark.parametrize("q,label,href", [
    ("skills", "Skills", "/admin/skills"),
    ("memory", "Memory", "/admin/memory"),
    ("channels", "Channels", "/admin/channels"),
    ("voice checks", "Voice checks", "/admin/voice-scenarios"),
    ("model comparison", "Model comparison", "/admin/evals/bakeoffs"),
    ("monitoring", "Schedules", "/admin/evals/scheduling"),
    ("schedules", "Schedules", "/admin/evals/scheduling"),
    ("test suites", "Test suites", "/admin/evals/suites"),
    ("test runs", "Runs", "/admin/evals/runs"),
    ("testing runs", "Runs", "/admin/evals/runs"),
    ("testing overview", "Testing overview", "/admin/evals"),
    ("engineering tests", "Engineering", "/admin/tests"),
    ("jobs", "Background jobs", "/admin/jobs"),
    ("diagnostics runtime", "Runtime environment", "/admin/diagnostics/runtime"),
    ("telemetry check", "Telemetry check", "/admin/lmnr/diagnose"),
    ("memory settings", "Agent memory settings", "/admin/agents/memory"),
    ("models", "Models", "/admin/agents?tab=models"),
    ("drift", "Config drift", "/admin/drift"),
    ("variables", "Environment variables", "/admin/env?tab=variables"),
    ("deploy history", "Deploy history", "/admin/deployments?tab=history"),
    ("deployment targets", "Deployment targets", "/admin/deployments?tab=targets"),
    ("team", "Team & access", "/admin/users"),
    ("voice budget", "Voice budgets", "/admin/voice-budgets"),
    ("templates", "Agent templates", "/admin/templates"),
])
def test_every_tab_and_setting_is_reachable(client, q, label, href):
    hit = _hit(_results(client, q), label)
    assert hit is not None, f"{q!r} should find {label!r}"
    assert hit["href"].startswith(href)


def test_agent_tabs_are_searchable(client):
    rows = _results(client, "instructions")
    hit = _hit(rows, "Agent › Instructions")
    assert hit and hit["href"] == "/admin/agents?env=staging&tab=instructions"
    assert "#" not in hit["href"]  # the old section anchors are gone (#227)
    voice = _hit(_results(client, "voice settings"), "Agent › Voice")
    assert voice is not None and voice["href"].endswith("tab=voice")
    # Old section names land on the tab that holds them now.
    assert _hit(_results(client, "endpoint"), "Agent › Settings")
    assert _hit(_results(client, "toolsets"), "Agent › Tools & knowledge")


def test_overview_query_still_finds_the_sidebar_overview_first(client):
    # Section "Overview" tabs are named after their section, so the bare
    # word keeps meaning the sidebar's Overview.
    rows = _results(client, "overview")
    assert rows[0]["label"] == "Overview" and rows[0]["href"].startswith("/admin/live")
    assert not [r for r in rows if r["label"] == "Overview" and r["href"].startswith("/admin/evals")]


def test_settings_items_land_in_settings_group(client):
    rows = _results(client, "telemetry check")
    hit = _hit(rows, "Telemetry check")
    assert hit and hit["group"] == "Settings" and hit["href"].startswith("/admin/lmnr/diagnose")


# --- ranking -------------------------------------------------------------------


def test_exact_label_beats_keyword_match(client):
    rows = _results(client, "agents")
    assert rows[0]["label"] == "Agents"


def test_prefix_beats_substring(client):
    rows = [r for r in _results(client, "conv") if r["group"] == "Screens"]
    assert rows[0]["label"] == "Conversations"


def test_screens_rank_above_docs_for_the_same_word(client):
    rows = _results(client, "activity")
    first_doc = next(i for i, r in enumerate(rows) if r["group"] == "Docs") if any(
        r["group"] == "Docs" for r in rows) else len(rows)
    assert rows.index(_hit(rows, "Activity")) < first_doc


def test_multi_word_query_needs_every_word(client):
    rows = _results(client, "deploy history")
    assert _hit(rows, "Deploy history")
    assert not _hit(rows, "Deployment targets")


def test_groups_are_contiguous(client):
    groups = [r["group"] for r in _results(client, "test")]
    seen = []
    for g in groups:
        if not seen or seen[-1] != g:
            assert g not in seen, f"group {g!r} split in two: {groups}"
            seen.append(g)


# --- scopes --------------------------------------------------------------------


def test_actions_scope_lists_actions_only(client):
    p = _payload(client, ">")
    assert p["scope"] == "actions"
    assert p["results"] and {r["type"] for r in p["results"]} == {"Action"}
    labels = {r["label"] for r in p["results"]}
    assert {"New agent", "Run a test", "Open Help"} <= labels


def test_actions_scope_filters(client):
    rows = _results(client, ">new agent")
    assert rows[0]["label"] == "New agent"


def test_docs_scope_returns_docs_only(client):
    rows = _results(client, "?publish")
    assert rows and {r["group"] for r in rows} == {"Docs"}


def test_people_scope_runs_only_people_sources(client, monkeypatch):
    import admin.search as s
    called = []
    monkeypatch.setattr(s, "RECORD_SOURCES", (("rec", "Rec", lambda c: called.append("rec") or []),))
    monkeypatch.setattr(s, "PEOPLE_SOURCES", (("ppl", "Ppl", lambda c: called.append("ppl") or []),))
    p = _payload(client, "@sara")
    assert p["scope"] == "people" and called == ["ppl"]
    assert all(r["group"] == "Conversations & people" for r in p["results"])


# --- docs full text -------------------------------------------------------------


def test_doc_heading_deep_links(client):
    rows = _results(client, "?audit log")
    hit = next(r for r in rows if r["label"] == "Audit Log")
    assert hit["href"] == "/admin/docs/govern#audit-log"
    assert hit["type"] == "Developer doc"


def test_doc_body_text_is_searched(client):
    # A phrase that only appears in a section body, not in any heading.
    rows = _results(client, "?hash deep-link")
    assert rows and all("#" in r["href"] or r["href"].startswith("/admin/") for r in rows)


def test_help_guides_are_searched(client):
    # A guide inside a section folder: deep link = section/page slug + heading.
    rows = _results(client, "?compare a/b test improve with ai")
    hit = next((r for r in rows if r["href"].startswith("/admin/help")), None)
    assert hit is not None
    assert hit["type"] == "Help"
    assert hit["href"].startswith("/admin/help/agents/instructions#compare")


# --- shell ---------------------------------------------------------------------


def test_palette_renders_in_shell(client):
    r = client.get("/admin/live")
    assert r.status_code == 200
    assert 'id="global-search-trigger"' in r.text
    assert 'id="global-search"' in r.text and 'id="global-search-dialog"' in r.text
    assert "global-search.js" in r.text
    # Scope prefixes are explained in the footer.
    for hint in ("&gt;</code> actions", "<code>@</code> people", "<code>?</code> help"):
        assert hint in r.text
    # Quick actions for the empty state are embedded server-side.
    assert 'id="global-search-quick"' in r.text and "New agent" in r.text
