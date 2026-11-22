"""PR B6 — admin MCP lifecycle parity: safe promote, incremental
attachments, prompt text, N-run compare, version diff, skill resources,
promotion board, session/audio lookup, and the env-routing fixes.

Same FakeClient pattern as the other admin_mcp tests: `client_for_env` is
monkeypatched on the tool module, and portal helpers that would touch a
real DB/LLM are patched on their SOURCE module (the tools import them
lazily inside the function body).
"""
from __future__ import annotations

import pytest

import admin_mcp.tools_configure as tc
import admin_mcp.tools_govern as tg
import admin_mcp.tools_observe as to
import admin_mcp.tools_ship as ts
import admin_mcp.tools_test as tt
from tests.admin_mcp.conftest import FakeClient

PROFILE = {"profile_id": "p1", "agent_kind": "business", "slug": "main",
           "environment": "staging", "status": "active", "is_active": True,
           "display_name": "Biz", "tool_ids": ["t1"], "data_source_ids": ["k1"],
           "mcp_server_ids": [], "downstream_profile_ids": [],
           "model_main_id": "m-new", "model_light_id": None,
           "prompt_version_ids": {"system": "pv2"},
           "endpoint_path": "/chat/stream", "parent_profile_id": None,
           "description": None}

PROD_PROFILE = {**PROFILE, "profile_id": "p1-prod", "environment": "prod",
                "model_main_id": "m-old", "tool_ids": ["t1", "t-gone"],
                "prompt_version_ids": {"system": "pv1"}}

TOOLS = [{"tool_id": "t1", "python_name": "get_bookings", "is_deprecated": False},
         {"tool_id": "t2", "python_name": "get_sales", "is_deprecated": False},
         {"tool_id": "t-dead", "python_name": "old_tool", "is_deprecated": True},
         {"tool_id": "t-gone", "python_name": "gone_tool", "is_deprecated": False}]

PROMPT_VERSIONS = [
    {"prompt_version_id": "pv1", "prompt_id": "pr1", "version_number": 1,
     "content": "You are a helpful assistant.\nBe brief.", "notes": "v1"},
    {"prompt_version_id": "pv2", "prompt_id": "pr1", "version_number": 2,
     "content": "You are a helpful assistant.\nBe brief and kind.", "notes": "v2"},
]


def _fake(monkeypatch, module, tables):
    client = FakeClient(tables)
    monkeypatch.setattr(module, "client_for_env", lambda env: client)
    return client


def _noaudit(monkeypatch, module):
    calls = []
    monkeypatch.setattr(module, "actor_audit", lambda *a, **k: calls.append((a, k)))
    return calls


# ---------------------------------------------------------------------------
# promote_profile — dry run by default
# ---------------------------------------------------------------------------


@pytest.fixture()
def promote_env(monkeypatch):
    import admin.services.promotion as promotion
    import core.agent_profile as agent_profile

    client = _fake(monkeypatch, ts, {
        "agent_profiles": [dict(PROFILE), dict(PROD_PROFILE)],
        "agent_tools": [dict(t) for t in TOOLS],
        "agent_models": [{"model_id": "m-new", "name": "flash-3"},
                         {"model_id": "m-old", "name": "flash-2"}],
        "agent_prompt_versions": [dict(v) for v in PROMPT_VERSIONS],
        "agent_prompts": [],
    })
    monkeypatch.setattr(agent_profile, "_fetch_mcp_attachments",
                        lambda client, pid: [])
    gate = {"status": "pass", "score": 0.9, "threshold": 0.7}
    monkeypatch.setattr(promotion, "_latest_eval_for_profile",
                        lambda client, p: dict(gate))
    monkeypatch.setattr(promotion, "_prompt_mandated_unbound_tools",
                        lambda client, p: [])
    calls = []

    def fake_promote(**kw):
        calls.append(kw)
        return {"profile_id": "p1-prod", "target_env": "prod",
                "forced": False, "cross_env": False, "eval_gate": gate}

    monkeypatch.setattr(promotion, "promote", fake_promote)
    return {"client": client, "calls": calls, "gate": gate}


def test_promote_without_confirm_is_a_dry_run(promote_env):
    out = ts.promote_profile(profile_id="p1", target_env="prod")

    assert promote_env["calls"] == [], "a dry run must not promote"
    assert out["confirmed"] is False
    preview = out["preview"]
    assert preview["replaces_profile_id"] == "p1-prod"
    changes = "\n".join(preview["changes"])
    assert "model_main_id: 'm-old' -> 'm-new'" in changes
    assert "tools: -gone_tool" in changes
    assert "prompt 'system': v1 -> v2" in changes
    assert preview["blockers"] == []
    assert "promote Biz" in out["would"]


def test_promote_dry_run_reports_blockers(promote_env, monkeypatch):
    import admin.services.promotion as promotion
    monkeypatch.setattr(promotion, "_latest_eval_for_profile", lambda c, p: {
        "status": "below_threshold", "score": 0.4, "threshold": 0.7,
        "suite_name": "Business smoke"})
    monkeypatch.setattr(promotion, "_prompt_mandated_unbound_tools",
                        lambda c, p: ["book_appointment"])

    out = ts.promote_profile(profile_id="p1", target_env="prod")

    assert promote_env["calls"] == []
    blockers = " ".join(out["preview"]["blockers"])
    assert "eval gate" in blockers and "0.4" in blockers
    assert "book_appointment" in blockers


def test_promote_dry_run_new_target_says_creates(promote_env):
    promote_env["client"].table("agent_profiles")._data[:] = [dict(PROFILE)]

    out = ts.promote_profile(profile_id="p1", target_env="prod")

    assert out["preview"]["replaces_profile_id"] is None
    assert any("creates a NEW prod profile" in c for c in out["preview"]["changes"])


def test_promote_with_confirm_applies(promote_env):
    out = ts.promote_profile(profile_id="p1", target_env="prod", confirm=True)

    assert out["promoted"] is True
    assert promote_env["calls"][0]["profile_id"] == "p1"
    assert promote_env["calls"][0]["force"] is False


def test_promote_dry_run_unknown_profile(promote_env):
    out = ts.promote_profile(profile_id="nope", target_env="prod")
    assert "not found" in out["error"]
    assert promote_env["calls"] == []


def test_promotion_status_board(monkeypatch):
    import admin.services.promotion as promotion
    _fake(monkeypatch, ts, {"agent_profiles": [
        dict(PROFILE), dict(PROD_PROFILE),
        {**PROFILE, "profile_id": "tpl", "status": "template"},
        {**PROFILE, "profile_id": "s2", "agent_kind": "sales", "slug": "s"},
    ]})
    gates = {"p1": {"status": "pass"}, "s2": {"status": "below_threshold"}}
    monkeypatch.setattr(promotion, "_latest_eval_for_profile",
                        lambda c, p: gates.get(p["profile_id"], {"status": "none"}))

    out = ts.promotion_status()

    by_kind = {k["agent_kind"]: k for k in out["agent_kinds"]}
    assert [p["profile_id"] for p in by_kind["business"]["staging"]] == ["p1"]
    assert by_kind["business"]["staging"][0]["promotable"] is True
    assert [p["profile_id"] for p in by_kind["business"]["prod"]] == ["p1-prod"]
    assert by_kind["sales"]["staging"][0]["promotable"] is False
    out = ts.promotion_status(agent_kind="sales")
    assert [k["agent_kind"] for k in out["agent_kinds"]] == ["sales"]


# ---------------------------------------------------------------------------
# profile_attachments — incremental add/remove
# ---------------------------------------------------------------------------


@pytest.fixture()
def attach_env(monkeypatch):
    import admin.services.profiles as profiles
    client = _fake(monkeypatch, tc, {
        "agent_profiles": [dict(PROFILE),
                           {**PROFILE, "profile_id": "sub1", "display_name": "Sub"}],
        "agent_tools": [dict(t) for t in TOOLS],
        "agent_data_sources": [{"source_id": "k1", "name": "faq"},
                               {"source_id": "k2", "name": "pricing"}],
    })
    writes = []

    def fake_update(**kw):
        writes.append(kw)
        row = client.table("agent_profiles")._data[0]
        row.update(kw["payload"])
        return dict(row)

    monkeypatch.setattr(profiles, "update_profile_row", fake_update)
    return {"client": client, "writes": writes}


def test_attach_add_tool_keeps_existing(attach_env):
    out = tc.profile_attachments(profile_id="p1", kind="tools", action="add",
                                 ids=["t2"])

    assert attach_env["writes"][0]["payload"] == {"tool_ids": ["t1", "t2"]}
    assert out["added"] == ["t2"]
    assert [i["label"] for i in out["items"]] == ["get_bookings", "get_sales"]


def test_attach_remove_tool_keeps_the_rest(attach_env):
    attach_env["client"].table("agent_profiles")._data[0]["tool_ids"] = ["t1", "t2"]

    out = tc.profile_attachments(profile_id="p1", kind="tools", action="remove",
                                 ids=["t1"])

    assert attach_env["writes"][0]["payload"] == {"tool_ids": ["t2"]}
    assert out["removed"] == ["t1"]


def test_attach_add_existing_is_a_noop(attach_env):
    out = tc.profile_attachments(profile_id="p1", kind="tools", action="add",
                                 ids=["t1"])
    assert attach_env["writes"] == []
    assert out["updated"] is False and out["unchanged"] == ["t1"]


def test_attach_refuses_unknown_or_deprecated_ids(attach_env):
    for bad in ("nope", "t-dead"):
        out = tc.profile_attachments(profile_id="p1", kind="tools",
                                     action="add", ids=["t2", bad])
        assert "error" in out and bad in out["error"]
    assert attach_env["writes"] == []


def test_attach_kb_and_downstream(attach_env):
    out = tc.profile_attachments(profile_id="p1", kind="kb", action="add",
                                 ids=["k2"])
    assert attach_env["writes"][-1]["payload"] == {"data_source_ids": ["k1", "k2"]}
    assert out["items"][1]["label"] == "pricing"

    out = tc.profile_attachments(profile_id="p1", kind="downstream",
                                 action="add", ids=["sub1"])
    assert attach_env["writes"][-1]["payload"] == {"downstream_profile_ids": ["sub1"]}
    out = tc.profile_attachments(profile_id="p1", kind="downstream",
                                 action="add", ids=["p1"])
    assert "own sub-agent" in out["error"]


def test_attach_requires_ids(attach_env):
    out = tc.profile_attachments(profile_id="p1", kind="tools", action="add")
    assert "needs ids" in out["error"]


def test_attach_mcp_add_keeps_existing_and_their_subsets(monkeypatch):
    import admin.screens.agents as agents_mod
    import admin.services.profiles as profiles_mod
    import core.agent_profile as agent_profile
    import core.deployment as dep
    monkeypatch.setattr(dep, "scoped_build", lambda: False)
    client = _fake(monkeypatch, tc, {
        "agent_profiles": [dict(PROFILE)],
        "agent_profile_mcp_servers": [
            {"profile_id": "p1", "mcp_server_id": "m1", "tool_subset": ["ta"],
             "position": 0}],
    })
    monkeypatch.setattr(agent_profile, "_fetch_mcp_attachments", lambda c, pid: [
        {"mcp_server": {"mcp_server_id": "m1", "name": "search"},
         "tool_subset": ["ta"], "position": 0, "member_tools": []}])
    monkeypatch.setattr(agents_mod, "_attachable_mcps", lambda c, visible_profiles=None: [
        {"mcp_server_id": "m1", "name": "search", "tool_names": ["ta", "tb"]},
        {"mcp_server_id": "m2", "name": "kit", "tool_names": ["tc"]}])
    monkeypatch.setattr(profiles_mod, "invalidate_profile_loader", lambda *a: None)
    _noaudit(monkeypatch, tc)

    out = tc.profile_attachments(profile_id="p1", kind="mcp", action="add",
                                 ids=["m2"], tool_subset=["tc"])

    assert out["added"] == ["m2"]
    by_mcp = {r["mcp_server_id"]: r for r in
              client.table("agent_profile_mcp_servers")._data}
    assert by_mcp["m1"]["tool_subset"] == ["ta"], "existing subset was lost"
    assert by_mcp["m2"]["tool_subset"] == ["tc"]

    out = tc.profile_attachments(profile_id="p1", kind="mcp", action="add",
                                 ids=["m3"])
    assert "not attachable" in out["error"]
    out = tc.profile_attachments(profile_id="p1", kind="mcp", action="add",
                                 ids=["m2"], tool_subset=["bogus"])
    # (the fake fetch still reports only m1, so m2 counts as new here)
    assert "bogus" in out["error"]


def test_attach_mcp_remove(monkeypatch):
    import admin.screens.agents as agents_mod
    import admin.services.profiles as profiles_mod
    import core.agent_profile as agent_profile
    import core.deployment as dep
    monkeypatch.setattr(dep, "scoped_build", lambda: False)
    client = _fake(monkeypatch, tc, {
        "agent_profiles": [dict(PROFILE)],
        "agent_profile_mcp_servers": [
            {"profile_id": "p1", "mcp_server_id": "m1", "tool_subset": None, "position": 0},
            {"profile_id": "p1", "mcp_server_id": "m2", "tool_subset": None, "position": 1}],
    })
    monkeypatch.setattr(agent_profile, "_fetch_mcp_attachments", lambda c, pid: [
        {"mcp_server": {"mcp_server_id": "m1", "name": "a"}, "position": 0},
        {"mcp_server": {"mcp_server_id": "m2", "name": "b"}, "position": 1}])
    # m1 is no longer attachable — removing m2 must still work.
    monkeypatch.setattr(agents_mod, "_attachable_mcps",
                        lambda c, visible_profiles=None: [])
    monkeypatch.setattr(profiles_mod, "invalidate_profile_loader", lambda *a: None)
    _noaudit(monkeypatch, tc)

    out = tc.profile_attachments(profile_id="p1", kind="mcp", action="remove",
                                 ids=["m2"])

    assert out["removed"] == ["m2"]
    assert [r["mcp_server_id"] for r in
            client.table("agent_profile_mcp_servers")._data] == ["m1"]


def _skill(sid, **kw):
    return {"skill_id": sid, "agent_kind": "business", "business_id": None,
            "is_active": True, "status": "active", **kw}


def test_attach_skills_add_and_remove(monkeypatch):
    client = _fake(monkeypatch, tc, {
        "agent_profiles": [dict(PROFILE)],
        "agent_skills": [_skill("s1"), _skill("s2"),
                         _skill("tenant", business_id="b1")],
        "agent_profile_skills": [{"profile_id": "p1", "skill_id": "s1", "position": 0}],
    })
    _noaudit(monkeypatch, tc)

    out = tc.profile_attachments(profile_id="p1", kind="skills", action="add",
                                 ids=["s2"])
    assert out["items"] == ["s1", "s2"]
    rows_ = client.table("agent_profile_skills")._data
    assert [(r["skill_id"], r["position"]) for r in rows_] == [("s1", 0), ("s2", 1)]

    out = tc.profile_attachments(profile_id="p1", kind="skills", action="add",
                                 ids=["tenant"])
    assert "not attachable" in out["error"]

    out = tc.profile_attachments(profile_id="p1", kind="skills", action="remove",
                                 ids=["s1"])
    assert out["items"] == ["s2"]


def test_replace_all_attachment_tools_still_work(monkeypatch):
    """The old set paths keep their replace semantics."""
    client = _fake(monkeypatch, tc, {
        "agent_profiles": [dict(PROFILE)],
        "agent_skills": [_skill("s1"), _skill("s2")],
        "agent_profile_skills": [{"profile_id": "p1", "skill_id": "s1", "position": 0}],
    })
    _noaudit(monkeypatch, tc)
    out = tc.profile_skill_attachments(profile_id="p1", action="set", skill_ids=["s2"])
    assert out["attachments"] == ["s2"]
    assert [r["skill_id"] for r in client.table("agent_profile_skills")._data] == ["s2"]


# ---------------------------------------------------------------------------
# prompts get / list / diff
# ---------------------------------------------------------------------------


@pytest.fixture()
def prompt_env(monkeypatch):
    return _fake(monkeypatch, tc, {
        "agent_profiles": [dict(PROFILE)],
        "agent_prompts": [{"prompt_id": "pr1", "agent_kind": "business",
                           "slug": "system", "description": "main"}],
        "agent_prompt_versions": [dict(v) for v in PROMPT_VERSIONS],
    })


def test_prompts_get_served_text_by_profile(prompt_env):
    out = tc.prompts(action="get", profile_id="p1", prompt_key="system")
    assert out["prompt_version_id"] == "pv2"
    assert out["content"] == PROMPT_VERSIONS[1]["content"]
    assert out["slug"] == "system" and out["version_number"] == 2


def test_prompts_get_by_number_newest_and_truncation(prompt_env):
    assert tc.prompts(action="get", prompt_id="pr1", version_number=1)["notes"] == "v1"
    assert tc.prompts(action="get", prompt_id="pr1")["version_number"] == 2
    out = tc.prompts(action="get", version_id="pv1", max_chars=10)
    assert out["content"] == "You are a " and out["truncated"] is True
    assert "error" in tc.prompts(action="get", profile_id="p1", prompt_key="voice")
    assert "error" in tc.prompts(action="get")


def test_prompts_list_previews(prompt_env):
    out = tc.prompts(action="list", profile_id="p1")
    assert out["served_prompts"][0]["version_number"] == 2
    assert "Be brief and kind" in out["served_prompts"][0]["preview"]
    out = tc.prompts(action="list", agent_kind="business")
    assert out["prompts"][0]["latest_version"] == 2
    out = tc.prompts(action="list", prompt_id="pr1")
    assert [v["version_number"] for v in out["versions"]] == [2, 1]
    assert all("content" not in v for v in out["versions"])


def test_prompts_diff_by_ids_is_a_unified_diff(prompt_env):
    out = tc.prompts(action="diff", ids=["pv1", "pv2"])
    assert "content" not in out["a"], "bodies are opt-in"
    assert "-Be brief." in out["unified_diff"]
    assert "+Be brief and kind." in out["unified_diff"]
    assert out["diff_stat"]["lines_added"] == 1
    assert "error" in tc.prompts(action="diff", ids=["pv1"])


def test_prompt_optimize_proposes_then_saves(monkeypatch):
    import admin.prompt_handlers as ph
    client = _fake(monkeypatch, tc, {
        "agent_prompts": [{"prompt_id": "pr1", "agent_kind": "business", "slug": "system"}],
        "agent_prompt_versions": [dict(v) for v in PROMPT_VERSIONS],
        "agent_eval_runs": [{"run_id": "r1", "results": [
            {"scenario_id": "s1", "passed": False, "question": "q"},
            {"scenario_id": "s2", "passed": True}]}],
        "agent_models": [{"model_id": "m1", "model_string": "x"}],
    })
    _noaudit(monkeypatch, tc)
    import core.prompt_resolver as pr
    monkeypatch.setattr(pr.PromptResolver, "invalidate", classmethod(lambda cls: None))
    seen = {}

    async def fake_stream(model_row, meta_prompt):
        seen["meta"] = meta_prompt
        yield ph._sse({"type": "delta", "content": "Be "})
        yield ph._sse({"type": "done", "content": "Better prompt."})

    monkeypatch.setattr(ph, "_stream_optimizer", fake_stream)
    import asyncio
    out = asyncio.run(tc.prompt_optimize(prompt_id="pr1", run_id="r1", model_id="m1"))
    assert out["proposal"]["content"] == "Better prompt."
    assert out["failing_scenarios"] == 1
    assert "s1" in seen["meta"]
    assert "saved" not in out
    assert len(client.table("agent_prompt_versions")._data) == 2

    out = asyncio.run(tc.prompt_optimize(prompt_id="pr1", run_id="r1",
                                         model_id="m1", confirm=True))
    assert out["saved"] is True and out["version_number"] == 3


# ---------------------------------------------------------------------------
# eval_runs compare N
# ---------------------------------------------------------------------------


def _run(rid, results, score=None):
    return {"run_id": rid, "suite_id": "s1", "profile_id": "p1", "status": "passed",
            "score": score, "context": {},
            "results": [{"scenario_id": k, "passed": v, "score": 1.0 if v else 0.0}
                        for k, v in results.items()]}


def test_eval_runs_compare_labels_n_runs(monkeypatch):
    _fake(monkeypatch, tt, {"agent_eval_runs": [
        _run("r1", {"held": True, "gain": False, "reg": True, "fail": False, "gone": True}),
        _run("r2", {"held": True, "gain": False, "reg": True, "fail": False}),
        _run("r3", {"held": True, "gain": True, "reg": False, "fail": False, "new": True}),
    ]})

    out = tt.eval_runs(action="compare", run_ids=["r1", "r2", "r3"])

    labels = {s["scenario_id"]: s["label"] for s in out["scenarios"]}
    assert labels == {"held": "held", "gain": "gained", "reg": "regressed",
                      "fail": "still_failing", "gone": "dropped", "new": "new"}
    assert out["scenarios"][0]["label"] == "regressed", "regressions sort first"
    assert out["labels"]["regressed"] == 1 and out["labels"]["held"] == 1
    gain = next(s for s in out["scenarios"] if s["scenario_id"] == "gain")
    assert gain["passed"] == [False, False, True]
    assert gain["vs_baseline"] == [None, "still_failing", "gained"]
    assert out["baseline_run_id"] == "r1" and out["latest_run_id"] == "r3"
    assert "results" not in out["runs"][0]
    assert out["runs"][0]["results_summary"]["failed"] == 2
    assert "columns" in out["matrix"]


def test_eval_runs_compare_rejects_single_or_missing(monkeypatch):
    _fake(monkeypatch, tt, {"agent_eval_runs": [_run("r1", {"a": True})]})
    assert "at least two" in tt.eval_runs(action="compare", run_ids=["r1"])["error"]
    assert "nope" in tt.eval_runs(action="compare", run_ids=["r1", "nope"])["error"]


def test_eval_runs_get_summarizes_results_unless_asked(monkeypatch):
    _fake(monkeypatch, tt, {"agent_eval_runs": [
        _run("r1", {"a": True, "b": False})]})
    out = tt.eval_runs(action="get", run_id="r1")
    assert "results" not in out["run"]
    assert out["run"]["results_summary"]["failing"][0]["scenario_id"] == "b"
    out = tt.eval_runs(action="get", run_id="r1", include_results=True)
    assert len(out["run"]["results"]) == 2


# ---------------------------------------------------------------------------
# profile_versions diff
# ---------------------------------------------------------------------------


def test_profile_versions_diff_vs_live_and_pair(monkeypatch):
    import admin.services.profile_versions as pv
    seen = []

    def fake_diff(profile_id, a, b=None):
        seen.append((profile_id, a, b))
        return {"scalars": [{"field": "model_main_id", "a": "m1", "b": "m2",
                             "equal": False}],
                "tools": {"added_in_b": [{"id": "t2", "label": "get_sales"}],
                          "removed_in_b": [], "shared": []}}

    monkeypatch.setattr(pv, "diff_version", fake_diff)
    monkeypatch.setattr(pv, "admin_service_client", lambda: None)
    out = tc.profile_versions(action="diff", profile_id="p1", version_id="v1")
    assert out["b"] == "live"
    assert out["changes"] == ["model_main_id: 'm1' -> 'm2'", "tools: +get_sales"]
    assert "detail" not in out
    out = tc.profile_versions(action="diff", profile_id="p1", ids=["v1", "v2"],
                              include_detail=True)
    assert seen[-1] == ("p1", "v1", "v2") and "detail" in out
    assert "error" in tc.profile_versions(action="diff", profile_id="p1")


def test_diff_version_service_rejects_foreign_version(monkeypatch):
    import admin.services.profile_versions as pv
    import runtime.compiler as compiler
    client = FakeClient({"agent_profiles": [dict(PROFILE)]})
    monkeypatch.setattr(pv, "admin_service_client", lambda: client)
    monkeypatch.setattr(compiler, "load_snapshot_by_version",
                        lambda vid: {"profile_id": "other", "snapshot": {}})
    with pytest.raises(ValueError, match="not found for profile"):
        pv.diff_version("p1", "v-foreign")


# ---------------------------------------------------------------------------
# skills resources
# ---------------------------------------------------------------------------


def test_skill_resources_add_list_delete(monkeypatch):
    import core.agent_profile as agent_profile
    import core.skills as skills_mod
    client = _fake(monkeypatch, tc, {
        "agent_skills": [_skill("s1")],
        "agent_skill_versions": [
            {"skill_version_id": "sv1", "skill_id": "s1", "version_number": 1},
            {"skill_version_id": "sv2", "skill_id": "s1", "version_number": 2}],
        "agent_skill_resources": [],
    })
    _noaudit(monkeypatch, tc)
    monkeypatch.setattr(skills_mod, "invalidate_skills_cache", lambda: None)
    monkeypatch.setattr(agent_profile.AgentProfileLoader, "invalidate",
                        classmethod(lambda cls, *a: None))

    out = tc.skills(action="add_resource", skill_id="s1", filename="guide.md",
                    content="hello")
    assert out["added"] is True and out["skill_version_id"] == "sv2"
    # same filename replaces rather than duplicating
    tc.skills(action="add_resource", skill_id="s1", filename="guide.md", content="v2")
    res = client.table("agent_skill_resources")._data
    assert len(res) == 1 and res[0]["content"] == "v2"
    res[0]["resource_id"] = "r1"

    listed = tc.skills(action="list_resources", skill_id="s1")
    assert listed["resources"][0]["filename"] == "guide.md"
    assert "content" not in listed["resources"][0]

    out = tc.skills(action="delete_resource", skill_id="s1", resource_id="r1")
    assert out["confirmed"] is False
    out = tc.skills(action="delete_resource", skill_id="s1", resource_id="r1",
                    confirm=True)
    assert out["deleted"] is True and client.table("agent_skill_resources")._data == []

    too_big = "x" * (skills_mod.MAX_RESOURCE_BYTES + 1)
    assert "exceeds" in tc.skills(action="add_resource", skill_id="s1",
                                  filename="big.md", content=too_big)["error"]
    assert "does not belong" in tc.skills(action="list_resources", skill_id="s1",
                                          version_id="other")["error"]


# ---------------------------------------------------------------------------
# traces session/audio + voice_insights + paging
# ---------------------------------------------------------------------------


def test_traces_session_and_audio_lookup(monkeypatch):
    import admin.screens.observability as obs
    _fake(monkeypatch, to, {"agent_traces": [
        {"session_id": "sess-1", "lmnr_trace_id": "tr-old", "created_at": "1"},
        {"session_id": "sess-1", "lmnr_trace_id": "tr-new", "created_at": "2"},
    ]})
    monkeypatch.setattr(obs, "_coerce_session_id_for_lookup", lambda s: s)
    out = to.traces(action="session", session_id="sess-1")
    assert out["latest_trace_id"] == "tr-new" and out["count"] == 2

    seen = {}
    monkeypatch.setattr(obs, "load_voice_audio_json", lambda tid, env=None: (
        seen.update(tid=tid) or {"recordings": [{"id": "rec1"}]}))
    out = to.traces(action="audio", session_id="sess-1")
    assert seen["tid"] == "tr-new" and out["recordings"][0]["id"] == "rec1"
    out = to.traces(action="audio", session_id="unknown")
    assert out["recordings"] == [] and "no trace" in out["notice"]


def test_traces_search_pages(monkeypatch):
    import admin.screens.observability as obs
    monkeypatch.setattr(obs, "load_traces_list", lambda **kw: {
        "traces": [{"id": f"t{i}"} for i in range(30)], "total": 30, "notice": None})
    out = to.traces(action="search", limit=10, offset=20)
    assert [t["id"] for t in out["traces"]] == [f"t{i}" for i in range(20, 30)]
    assert out["total"] == 30 and out["has_more"] is False
    out = to.traces(action="search")
    assert len(out["traces"]) == 25 and out["has_more"] is True


def test_voice_insights_wraps_portal_loader(monkeypatch):
    import admin.screens.observability as obs
    seen = {}
    monkeypatch.setattr(obs, "load_voice_live_insights", lambda **kw: (
        seen.update(kw) or {"ch_ok": True, "sessions": 3}))
    out = to.voice_insights(hours=0, trace_env="prod")
    assert out["sessions"] == 3
    assert seen == {"past_hours": 1, "env": "prod"}


# ---------------------------------------------------------------------------
# env routing / aliases / annotations fixes
# ---------------------------------------------------------------------------


def _per_env(monkeypatch, module, tables):
    clients = {"staging": FakeClient(tables()), "prod": FakeClient(tables())}
    monkeypatch.setattr(module, "client_for_env", lambda env: clients[env])
    return clients


def test_channels_writes_follow_env(monkeypatch):
    clients = _per_env(monkeypatch, tg, lambda: {"agent_channels": []})
    _noaudit(monkeypatch, tg)
    import admin.channel_handlers as ch
    monkeypatch.setattr(ch, "_invalidate_channels_cache", lambda: None)
    monkeypatch.setattr(ch, "_channel_agent_kinds", lambda: ["business"])

    out = tg.channels(action="create", slug="web", name="Web", kind="embed",
                      env="prod")

    assert out["created"] is True
    assert clients["prod"].table("agent_channels").inserted
    assert not clients["staging"].table("agent_channels").inserted


def test_test_fixtures_writes_follow_env(monkeypatch):
    clients = _per_env(monkeypatch, tg, lambda: {"test_fixtures": [
        {"fixture_id": "f1", "name": "a", "environment": "prod", "kind": "string",
         "value": "x"}]})
    _noaudit(monkeypatch, tg)

    tg.test_fixtures(action="create", name="staff_jwt", kind="string",
                     value="v", env="prod")
    tg.test_fixtures(action="update", fixture_id="f1", value="y", env="prod")
    tg.test_fixtures(action="delete", fixture_id="f1", env="prod", confirm=True)

    prod = clients["prod"].table("test_fixtures")
    assert prod.inserted and prod.updated and prod.deleted
    staging = clients["staging"].table("test_fixtures")
    assert not (staging.inserted or staging.updated or staging.deleted)


def test_memory_set_config_targets_a_specific_profile(monkeypatch):
    import admin.services.profiles as profiles_mod
    client = _fake(monkeypatch, tg, {"agent_profiles": [
        dict(PROFILE),
        {**PROFILE, "profile_id": "voice1", "status": "specialist",
         "slug": "voice", "memory_config": {"enabled": True}}]})
    _noaudit(monkeypatch, tg)
    monkeypatch.setattr(profiles_mod, "invalidate_profile_loader", lambda *a: None)

    out = tg.memory(action="set_config", profile_id="voice1",
                    config={"enabled": False})

    assert out["profile_id"] == "voice1"
    rows_ = {r["profile_id"]: r for r in client.table("agent_profiles")._data}
    assert rows_["voice1"]["memory_config"] == {"enabled": False}
    assert "memory_config" not in rows_["p1"]
    got = tg.memory(action="get_config", profile_id="voice1")
    assert got["profile"]["memory_config"] == {"enabled": False}


def test_list_users_is_read_only_listing(monkeypatch):
    client = FakeClient({"user": [
        {"id": "u1", "permission": {"is_superuser": True}, "is_deleted": False},
        {"id": "u2", "permission": {}, "is_deleted": False}]})
    monkeypatch.setattr("core.auth.get_main_client", lambda: client)
    out = tg.list_users(superusers_only=True)
    assert [u["id"] for u in out["users"]] == ["u1"]
    assert "permission" not in out["users"][0]


def test_update_profile_row_environment_alias(monkeypatch):
    import admin.services.profiles as sp
    _fake(monkeypatch, tc, {})
    seen = {}
    monkeypatch.setattr(sp, "update_profile_row",
                        lambda **kw: seen.update(kw) or {"profile_id": "p1"})
    tc.update_profile(profile_id="p1", row_environment="prod")
    assert seen["payload"] == {"environment": "prod"}
    tc.update_profile(profile_id="p1", environment="staging")  # old name
    assert seen["payload"] == {"environment": "staging"}
    out = tc.update_profile(profile_id="p1", row_environment="prod",
                            environment="staging")
    assert "error" in out


def test_diff_profiles_accepts_ids_pair(monkeypatch):
    import admin.profile_fork_handlers as pfh
    _fake(monkeypatch, tc, {"agent_profiles": [dict(PROFILE), dict(PROD_PROFILE)]})
    monkeypatch.setattr(pfh, "compute_profile_diff", lambda c, a, b: {
        "a": a["profile_id"], "b": b["profile_id"]})
    out = tc.diff_profiles(ids=["p1", "p1-prod"])
    assert out["diff"] == {"a": "p1", "b": "p1-prod"}
    assert "error" in tc.diff_profiles(ids=["p1"])
    assert "error" in tc.diff_profiles()


def test_list_profiles_pages(monkeypatch):
    _fake(monkeypatch, tc, {"agent_profiles": [
        {**PROFILE, "profile_id": f"p{i}", "agent_kind": f"k{i:02d}"}
        for i in range(5)]})
    out = tc.list_profiles(limit=2, offset=2)
    assert [p["profile_id"] for p in out["profiles"]] == ["p2", "p3"]
    assert out["total"] == 5 and out["has_more"] is True


def test_get_profile_fields_trims_row(monkeypatch):
    import core.agent_profile as agent_profile
    _fake(monkeypatch, tc, {"agent_profiles": [dict(PROFILE)]})
    monkeypatch.setattr(agent_profile, "_fetch_mcp_attachments", lambda c, pid: [])
    out = tc.get_profile(profile_id="p1", fields=["endpoint_path"])
    assert out["profile"] == {"profile_id": "p1", "agent_kind": "business",
                              "slug": "main", "endpoint_path": "/chat/stream"}
