// Run by tests/admin/test_demo_tour_logic.py with node (no packages): loads
// the tour's catalog and the pure half of its engine with a bare `window`,
// runs fixed cases and prints {name: result} as JSON for pytest to check.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..", "..", "..", "admin", "static");
const sandbox = { window: {}, console, URL, URLSearchParams };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(root, "demo-tour-steps.js"), "utf8"), sandbox);
const CAT = sandbox.window.DEMO_TOUR_CATALOG;
// The engine without its catalog exports the pure logic and stops there.
const bare = { window: {}, console };
vm.createContext(bare);
vm.runInContext(fs.readFileSync(path.join(root, "demo-tour.js"), "utf8"), bare);
const L = bare.window.__demoTourLogic;

const step = (id) => CAT.steps.find((s) => s.id === id);
const loc = (href) => { const u = new URL(href, "http://demo.test"); return { pathname: u.pathname, search: u.search, hash: u.hash }; };
const doc = (opts) => ({
  referrer: opts.referrer || "",
  querySelector: (sel) => {
    if (sel.indexOf("data-agent-tab") >= 0 && opts.tab) {
      return { getAttribute: (n) => ({ "data-agent-tab": opts.tab, "data-agent-slug": opts.slug || "", "data-profile-id": opts.id || "" })[n] };
    }
    if (sel.indexOf("conversation-row") >= 0) return opts.rows ? {} : null;
    return null;
  }
});
const rule = (id, on, extra) => (step(id).done || []).find((r) => r.on === on && (!extra || extra(r)));
const aiOk = (id, actions) => L.aiSpecHit(step(id).ai, actions.map((a) => a.split(":")[0]), actions);
const fresh = () => ({ steps: {}, made: { agents: [], runs: [], agent_ids: [] }, runs: {}, flags: {} });

const out = {};
// AI ticks by action, not by tool name.
out.a3_list_only = aiOk("A3", ["templates:list"]);
out.a3_use = aiOk("A3", ["templates:list", "templates:use"]);
out.a4_mcp_only = aiOk("A4", ["profile_attachments:add", "profile_attachments:add:mcp"]);
out.a4_both = aiOk("A4", ["profile_attachments:add", "profile_attachments:add:mcp", "profile_attachments:add:kb"]);
out.a4_list = aiOk("A4", ["profile_attachments:list", "profile_attachments:list:mcp"]);
out.a5_downstream = aiOk("A5", ["profile_attachments:add", "profile_attachments:add:downstream"]);
out.a5_template_only = aiOk("A5", ["templates:use"]);
out.a5_template_sub = aiOk("A5", ["templates:use", "templates:use:downstream"]);
out.b4_list = aiOk("B4", ["eval_runs:list"]);
out.b4_compare = aiOk("B4", ["eval_runs:list", "eval_runs:compare"]);
out.c1_promote = aiOk("C1", ["promote_profile"]);
// "mine" and "grew".
const st = fresh();
st.made.agents.push("outage-helper"); st.made.agent_ids.push("p-1"); st.runs.r1 = { at: 1 };
out.mine_own_agent = L.isMine(st, { profile: "outage-helper" });
out.mine_shared_agent = L.isMine(st, { profile: "support-desk", profile_id: "p-sd" });
out.mine_own_run = L.isMine(st, { run_id: "r1" });
out.mine_other_run = L.isMine(st, { run_id: "r2" });
const a5 = rule("A5", "event");
st.flags.base = { "p-1": { subagents: 1 } };
out.a5_resave = L.grew(st, a5, { profile_id: "p-1", subagents: 1 });
out.a5_added = L.grew(st, a5, { profile_id: "p-1", subagents: 2 });
out.a5_rule_mine = !!a5.mine && step("A5").requires.indexOf("A3") >= 0;
out.c1_requires_a3 = step("C1").requires.indexOf("A3") >= 0 && !!rule("C1", "click").mine;
// Merging another tab's progress; a different reset is ignored.
const mine = fresh(); mine.reset_id = "g1"; mine.steps.A1 = { via: "hand" };
const other = fresh(); other.reset_id = "g1"; other.steps.B1 = { via: "ai" }; other.made.agents.push("x"); other.runs.r9 = { finished: "passed" };
out.merge_changed = L.mergeStates(mine, other, "g1", { A1: 1, B1: 1 });
out.merge_steps = Object.keys(mine.steps).sort();
out.merge_made = mine.made.agents;
out.merge_runs = Object.keys(mine.runs);
const old = fresh(); old.reset_id = "g0"; old.steps.C1 = { via: "hand" };
out.merge_other_reset = L.mergeStates(mine, old, "g1", { C1: 1 });
out.same_reset = L.sameReset({ reset_id: "g1" }, "g1");
out.new_reset = L.sameReset({ reset_id: "g0" }, "g1");
// Page rules.
out.a1_instructions = rule("A1", "page").match(loc("/admin/agents?profile_id=p&tab=instructions"), doc({ tab: "instructions" }));
out.a1_overview = rule("A1", "page").match(loc("/admin/agents?profile_id=p"), doc({ tab: "overview" }));
const a3 = rule("A3", "click-then-page");
out.a3_from_templates = a3.match(loc("/admin/agents?profile_id=new"), doc({ tab: "overview", referrer: "http://demo.test/admin/templates?new=1" }));
out.a3_from_list = a3.match(loc("/admin/agents?profile_id=sd"), doc({ tab: "overview", referrer: "http://demo.test/admin/agents" }));
const d2 = step("D2").alt;
const d2page = d2.done.find((r) => r.on === "page");
out.d2_alt_open = d2page.match(loc("/admin/observability#trace=abc"));
out.d2_alt_list = d2page.match(loc("/admin/observability"));
out.d2_alt_note_empty = typeof d2.cards[0].note === "function" ? d2.cards[0].note(loc("/admin/observability"), doc({ rows: false })) : null;
out.d2_alt_note_rows = d2.cards[0].note(loc("/admin/observability"), doc({ rows: true }));
const d2flag = (step("D2").done || []).find((r) => r.on === "page");
out.d2_flagged = d2flag.match(loc("/admin/observability?outcome=attention#trace=t"));
out.d2_unflagged_reload = d2flag.match(loc("/admin/observability#trace=t"));
const d2click = (step("D2").done || []).find((r) => r.on === "click-then-page");
out.d2_click_anchor = d2click.anchor;
out.d2_open_latest = d2click.match(loc("/admin/observability?hours=24#trace=t1"));
out.d2_open_list = d2click.match(loc("/admin/observability?hours=24"));
out.b3_open_rule = JSON.stringify(rule("B3", "open").attr);
out.steps = CAT.steps.length;
// One page served with the clock's date (the data's generation could not be
// read for a moment) keeps progress; a new generation or a new date drops it.
out.keep_same = L.keepsProgress({ reset_id: "0c85be5365" }, "0c85be5365");
out.keep_on_date_fallback = L.keepsProgress({ reset_id: "0c85be5365" }, "2026-11-25");
out.keep_new_generation = L.keepsProgress({ reset_id: "0c85be5365" }, "63510ce7a5");
out.keep_new_date = L.keepsProgress({ reset_id: "2026-11-24" }, "2026-11-25");
out.keep_nothing = L.keepsProgress(null, "2026-11-25");
out.agent_link = L.agentLink("/admin/agents?profile_id=DF493D13-0c07-4f64-9e28-7522515797fb&env=staging");
out.agent_link_env_first = L.agentLink("/admin/agents?env=staging&profile_id=df493d13-0c07-4f64-9e28-7522515797fb");
out.agent_link_list = L.agentLink("/admin/agents?env=staging");
process.stdout.write(JSON.stringify(out));
