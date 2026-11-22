#!/usr/bin/env python3
"""End-to-end smoke test for the AI-engine admin MCP server.

One driver, two entry points:

  live (default) — hit a deployed /mcp with a real superuser token. Read-only:
      it lists tools, calls read tools, checks the confirm-gate and error
      shapes, and reads a doc resource. It NEVER performs a real mutation
      (no promote/deploy/delete is confirmed).
          python scripts/mcp_smoke.py \
              --url https://aiengine.staging.example/mcp \
              --token "$WAJ_AI_ENGINE_MCP_TOKEN"

  --offline — boot the real server in-process on a random port with the two
      DB boundaries faked + seeded (no Supabase / GitHub / Laminar needed),
      mint a local token, and run the same driver plus a few value-exact
      assertions. A fast wiring check / demo.
          python scripts/mcp_smoke.py --offline

Exit code: 0 if every check passes (skips don't fail), 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time

# Make `scripts.*` and `admin_mcp.*` importable regardless of cwd.
_AI_AGENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _AI_AGENT not in sys.path:
    sys.path.insert(0, _AI_AGENT)

import httpx  # noqa: E402

from scripts.mint_mcp_token import mint_token  # noqa: E402

ACCEPT = {"Accept": "application/json, text/event-stream",
          "Content-Type": "application/json"}
_OFFLINE_SECRET = "offline-smoke-secret"
_OFFLINE_SUB = "00000000-0000-0000-0000-0000000000aa"
# A stable subset of tools that must always be present (robust to added tools).
_CORE_TOOLS = {"list_profiles", "get_profile", "update_profile", "promote_profile",
               "trigger_eval_run", "eval_runs", "deploy", "rollback", "mcp_servers",
               "profile_mcp_attachments", "traces", "users", "audit"}


# --------------------------------------------------------------------------- #
# JSON-RPC over HTTP
# --------------------------------------------------------------------------- #
def _rpc(endpoint, method, params=None, token=None, rid=1):
    h = dict(ACCEPT)
    if token:
        h["Authorization"] = f"Bearer {token}"
    return httpx.post(endpoint, headers=h, timeout=30,
                      json={"jsonrpc": "2.0", "id": rid, "method": method,
                            "params": params or {}})


def _call_tool(endpoint, name, args, token):
    r = _rpc(endpoint, "tools/call", {"name": name, "arguments": args}, token=token)
    if r.status_code != 200:
        return {"_http": r.status_code, "_body": r.text[:200]}
    body = r.json()
    if "error" in body:
        return {"_rpc_error": body["error"]}
    res = body.get("result", {})
    # Prefer the text content block: FastMCP serializes the tool's return value
    # there unwrapped. structuredContent nests a bare dict under a "result" key,
    # so unwrap that if we fall back to it.
    for c in res.get("content", []):
        if c.get("type") == "text":
            try:
                return json.loads(c["text"])
            except Exception:
                return c["text"]
    sc = res.get("structuredContent")
    if isinstance(sc, dict):
        return sc["result"] if list(sc.keys()) == ["result"] else sc
    return res


# --------------------------------------------------------------------------- #
# Checks (shared by both modes; value-exact extras run only offline)
# --------------------------------------------------------------------------- #
class Checks:
    def __init__(self):
        self.rows = []  # (name, status, note) ; status in {ok, fail, skip}

    def ok(self, name, note=""):
        self.rows.append((name, "ok", note))

    def fail(self, name, note=""):
        self.rows.append((name, "fail", note))

    def skip(self, name, note=""):
        self.rows.append((name, "skip", note))

    def assert_(self, cond, name, note=""):
        (self.ok if cond else self.fail)(name, note)
        return cond

    def failures(self):
        return [r for r in self.rows if r[1] == "fail"]

    def report(self):
        icon = {"ok": "PASS", "fail": "FAIL", "skip": "SKIP"}
        print()
        for name, status, note in self.rows:
            line = f"  [{icon[status]}] {name}"
            if note:
                line += f"  — {note}"
            print(line)
        n_fail = len(self.failures())
        n_skip = sum(1 for r in self.rows if r[1] == "skip")
        n_ok = sum(1 for r in self.rows if r[1] == "ok")
        print(f"\n  {n_ok} passed, {n_fail} failed, {n_skip} skipped")


def run_driver(endpoint, token, *, offline):
    c = Checks()
    forged = mint_token(sub="00000000-0000-0000-0000-000000000fff",
                        email="attacker@evil", days=1, secret="wrong-secret-xyz")

    # --- auth boundary (real HS256 verification) ---
    c.assert_(_rpc(endpoint, "tools/list").status_code == 401,
              "auth: anonymous request rejected (401)")
    c.assert_(_rpc(endpoint, "tools/list", token=forged).status_code == 401,
              "auth: forged-signature token rejected (401)")
    c.assert_(_rpc(endpoint, "tools/list", token="not.a.jwt").status_code == 401,
              "auth: malformed token rejected (401)")
    tl = _rpc(endpoint, "tools/list", token=token)
    if not c.assert_(tl.status_code == 200, "auth: valid superuser token accepted (200)",
                     "" if tl.status_code == 200 else f"got {tl.status_code}: {tl.text[:120]}"):
        return c  # nothing else will work without auth

    # --- protocol ---
    init = _rpc(endpoint, "initialize",
                {"protocolVersion": "2025-03-26", "capabilities": {},
                 "clientInfo": {"name": "mcp-smoke", "version": "0"}}, token=token).json()
    name = init.get("result", {}).get("serverInfo", {}).get("name")
    c.assert_(name == "waj-ai-engine", "mcp: initialize serverInfo", f"name={name!r}")

    tools = tl.json()["result"]["tools"]
    names = {t["name"] for t in tools}
    c.assert_(_CORE_TOOLS <= names, "mcp: tools/list has the core tool set",
              f"{len(names)} tools; missing={sorted(_CORE_TOOLS - names) or 'none'}")

    # --- read tools return well-formed results (structural; no external services) ---
    lp = _call_tool(endpoint, "list_profiles", {"env": "staging"}, token)
    c.assert_(isinstance(lp, dict) and "profiles" in lp,
              "tool: list_profiles well-formed",
              f"{len(lp.get('profiles', []))} profiles" if isinstance(lp, dict) else str(lp)[:120])
    ms = _call_tool(endpoint, "mcp_servers", {"action": "list"}, token)
    c.assert_(isinstance(ms, dict) and "mcp_servers" in ms,
              "tool: mcp_servers list well-formed",
              f"{len(ms.get('mcp_servers', []))} servers" if isinstance(ms, dict) else str(ms)[:120])
    md = _call_tool(endpoint, "models", {"action": "list"}, token)
    c.assert_(isinstance(md, dict) and "models" in md, "tool: models list well-formed")

    # --- safety rail: confirm-gate returns not_confirmed WITHOUT mutating ---
    # Prefer a non-builtin server: builtins refuse deletion outright (the
    # is_builtin guard runs BEFORE the confirm gate), so they never exercise
    # the not_confirmed payload. Where only builtins exist, assert that
    # refusal rail instead — confirm stays omitted either way, so a
    # mutation would need BOTH rails to fail.
    servers = ms.get("mcp_servers", []) if isinstance(ms, dict) else []
    target = next((s for s in servers if not s.get("is_builtin")), None)
    if target:
        d = _call_tool(endpoint, "mcp_servers",
                       {"action": "delete",
                        "mcp_server_id": target["mcp_server_id"]}, token)  # no confirm
        c.assert_(isinstance(d, dict) and d.get("confirmed") is False,
                  "safety: delete without confirm is gated (no mutation)", str(d)[:120])
    elif servers:
        d = _call_tool(endpoint, "mcp_servers",
                       {"action": "delete",
                        "mcp_server_id": servers[0]["mcp_server_id"]}, token)  # no confirm
        c.assert_(isinstance(d, dict) and "built-in" in str(d.get("error", "")),
                  "safety: builtin MCP delete refused (no mutation)", str(d)[:120])
    else:
        c.skip("safety: delete confirm-gate", "no MCP servers to target")

    # --- error shape: invalid env is rejected at the SCHEMA layer ---
    # target_env is Literal["staging","prod"], so a bad value never reaches
    # the tool body: FastMCP returns an isError result carrying pydantic's
    # validation message instead of the tool's own err() payload.
    e = _call_tool(endpoint, "promote_profile",
                   {"profile_id": "__smoke__", "target_env": "production"}, token)
    c.assert_(isinstance(e, str) and "validation error" in e and "staging" in e,
              "error: invalid env rejected by schema enum (no promotion)", str(e)[:120])

    # --- error shape: domain failures still return the err() envelope ---
    # A nonexistent profile passes schema validation but fails in the tool
    # body — read-only, so safe in live mode too. Must be a syntactically
    # valid UUID: profile_id is a uuid column, so a bare string like
    # "__smoke__" doesn't miss — it 22P02s inside PostgREST on a real DB
    # (the offline FakeTable compares strings and can't catch that).
    e2 = _call_tool(endpoint, "get_profile",
                    {"profile_id": "00000000-0000-0000-0000-00000000dead"}, token)
    c.assert_(isinstance(e2, dict) and e2.get("error") == "profile not found",
              "error: unknown profile returns err() shape", str(e2)[:120])

    # --- doc resource ---
    tmpl = _rpc(endpoint, "resources/templates/list", token=token).json().get("result", {})
    uris = [t.get("uriTemplate") for t in tmpl.get("resourceTemplates", [])]
    c.assert_("wajdocs://{slug}" in uris, "resource: wajdocs:// template registered", str(uris))
    doc = _rpc(endpoint, "resources/read", {"uri": "wajdocs://index"}, token=token).json()
    text = "".join(x.get("text", "") for x in doc.get("result", {}).get("contents", []))
    c.assert_(len(text) > 50, "resource: read wajdocs://index returns markdown",
              f"{len(text)} chars")

    # --- offline-only value-exact assertions (data is seeded + deterministic) ---
    if offline:
        # A floor, not an exact count: tool count moves whenever any PR adds
        # one (three sibling PRs were doing exactly that concurrently when
        # this went from == to >=), so pinning an exact number makes this
        # assertion flaky by construction rather than a real regression
        # signal. _CORE_TOOLS (checked above via the c.assert_ near line 149)
        # is the actual "did the wiring break" signal — this just guards
        # against the list going empty/truncated.
        c.assert_(len(names) >= 44, "offline: at least 44 tools", f"got {len(names)}")
        c.assert_(isinstance(lp, dict) and lp.get("count") == 2,
                  "offline: list_profiles count == 2")
        gp = _call_tool(endpoint, "get_profile", {"agent_kind": "sample_agent"}, token)
        c.assert_(isinstance(gp, dict) and gp.get("mcp_attachments"),
                  "offline: get_profile surfaces mcp_attachments (#1222)")
        bundle = _call_tool(endpoint, "mcp_servers",
                            {"action": "get", "mcp_server_id": "mcp-bundle"}, token)
        c.assert_(isinstance(bundle, dict) and len(bundle.get("members", [])) == 2
                  and bundle.get("attached_count") == 1,
                  "offline: mcp_servers get returns membership + attached_count")
        us = _call_tool(endpoint, "users", {"action": "list"}, token)
        first = (us.get("users") or [{}])[0] if isinstance(us, dict) else {}
        c.assert_("permission" not in first and "is_superuser" in first,
                  "offline: users list strips permission, derives is_superuser")
    return c


# --------------------------------------------------------------------------- #
# Offline server (real app, faked + seeded DB boundaries)
# --------------------------------------------------------------------------- #
def _build_offline_app():
    os.environ["SUPABASE_JWT_SECRET"] = _OFFLINE_SECRET
    os.environ["ENABLE_ADMIN_MCP"] = "1"
    for k, v in {"SUPABASE_URL": "http://127.0.0.1:54321", "SUPABASE_ANON_KEY": "anon",
                 "SUPABASE_SERVICE_KEY": "svc", "GOOGLE_API_KEY": "x"}.items():
        os.environ.setdefault(k, v)
    # A scoped (single-tenant) build would hide the sample kinds below.
    from core.deployment import scope_env_vars
    for k in scope_env_vars():
        os.environ.setdefault(k, "")

    class FakeTable:
        def __init__(self, data):
            self._d = data
            self._pred = []
            self._lim = None
            self._ins = self._upd = None
            self._del = False

        def select(self, *a, **k): return self
        def eq(self, c, v): self._pred.append(("eq", c, v)); return self
        def in_(self, c, v): self._pred.append(("in", c, list(v))); return self
        def gte(self, c, v): self._pred.append(("gte", c, v)); return self
        def order(self, *a, **k): return self
        def limit(self, n, *a, **k): self._lim = n; return self
        def range(self, *a, **k): return self
        def insert(self, p): self._ins = p; return self
        def update(self, p): self._upd = p; return self
        def delete(self): self._del = True; return self

        def _match(self, row):
            for kind, c, v in self._pred:
                if kind == "eq" and row.get(c) != v: return False
                if kind == "in" and row.get(c) not in v: return False
                if kind == "gte" and (row.get(c) is None or row.get(c) < v): return False
            return True

        def execute(self):
            r = type("R", (), {})()
            try:
                if self._ins is not None:
                    row = dict(self._ins); row.setdefault("id", f"fake-{len(self._d)}")
                    self._d.append(row); r.data = [row]; self._ins = None
                elif self._upd is not None:
                    m = [x for x in self._d if self._match(x)]
                    for x in m: x.update(self._upd)
                    r.data = m; self._upd = None
                elif self._del:
                    m = [x for x in self._d if self._match(x)]
                    ids = {id(x) for x in m}
                    self._d[:] = [x for x in self._d if id(x) not in ids]
                    r.data = m; self._del = False
                else:
                    m = [dict(x) for x in self._d if self._match(x)]  # copy on read
                    if self._lim is not None: m = m[:self._lim]
                    r.data = m
            finally:
                self._pred = []; self._lim = None
            r.count = len(r.data)
            return r

    class FakeClient:
        def __init__(self, tables): self.t = {n: FakeTable(d) for n, d in tables.items()}
        def table(self, n): return self.t.setdefault(n, FakeTable([]))

    ai = FakeClient({
        "agent_profiles": [
            {"profile_id": "p-biz", "agent_kind": "sample_agent", "slug": "main",
             "environment": "staging", "status": "active", "is_active": True,
             "display_name": "Sample Agent", "model_main_id": "m-flash",
             "model_light_id": None, "tool_ids": ["t-book", "t-docs"],
             "data_source_ids": ["ds-kb"], "mcp_server_ids": ["mcp-ext"],
             "downstream_profile_ids": [], "prompt_version_ids": {"system": "pv-1"},
             "endpoint_path": "/chat"},
            {"profile_id": "p-clin", "agent_kind": "sample_other", "slug": "main",
             "environment": "staging", "status": "active", "is_active": True,
             "display_name": "Sales Agent", "model_main_id": "m-flash",
             "tool_ids": ["t-docs"], "data_source_ids": [], "mcp_server_ids": [],
             "prompt_version_ids": {}, "endpoint_path": "/sample-other"},
        ],
        "agent_models": [{"model_id": "m-flash", "name": "gemini-flash",
                          "provider": "google_genai", "model_string": "gemini-2.5-flash",
                          "environment": "staging", "temperature": 0.7, "is_active": True}],
        "agent_tools": [
            {"tool_id": "t-book", "python_name": "get_bookings", "kind": "http",
             "agent_kind": "sample_agent", "is_deprecated": False},
            {"tool_id": "t-docs", "python_name": "search_documentation", "kind": "http",
             "agent_kind": "sample_agent", "is_deprecated": False}],
        "agent_mcp_servers": [
            {"mcp_server_id": "mcp-ext", "name": "sample-partner", "source": "external",
             "is_builtin": False, "transport": "http", "endpoint": "https://mcp.partner.example/mcp",
             "auth_env_var": "SAMPLE_PARTNER_MCP_TOKEN", "tool_subset": None, "context_fields": {},
             "usage_guidance": None, "is_active": True},
            {"mcp_server_id": "mcp-bundle", "name": "__builtin__:sample_agent",
             "source": "bundle", "is_builtin": True, "endpoint": None, "transport": None,
             "context_fields": {}, "usage_guidance": "Built-in tools for the sample agent.",
             "is_active": True}],
        "agent_mcp_tools": [{"mcp_server_id": "mcp-bundle", "tool_id": "t-book", "position": 0},
                            {"mcp_server_id": "mcp-bundle", "tool_id": "t-docs", "position": 1}],
        "agent_profile_mcp_servers": [{"profile_id": "p-biz", "mcp_server_id": "mcp-bundle",
                                       "tool_subset": ["get_bookings"], "position": 0}],
        "agent_data_sources": [{"source_id": "ds-kb", "name": "WAJ Docs", "kind": "pgvector",
                                "config": {"table": "ai_kb_embeddings"}, "is_active": True}],
        "agent_prompt_versions": [{"prompt_version_id": "pv-1", "prompt_id": "pr-sys",
                                   "version_number": 3, "content": "You are the WAJ agent.",
                                   "notes": "seed", "created_by": "u-1",
                                   "created_at": "2026-07-01T00:00:00+00:00"}],
        "agent_eval_runs": [{"run_id": "run-1", "suite_id": "s-1", "profile_id": "p-biz",
                             "status": "passed", "score": 0.92, "context": {},
                             "created_at": "2026-07-05T00:00:00+00:00",
                             "finished_at": "2026-07-05T00:05:00+00:00"}],
    })
    main_db = FakeClient({"user": [
        {"id": "u-1", "auth_id": _OFFLINE_SUB, "email": "smoke@company.example",
         "permission": {"is_superuser": True}, "is_deleted": False, "role": "owner",
         "created_at": "2026-01-01T00:00:00+00:00"},
        {"id": "u-2", "auth_id": "bbb", "email": "staff@company.example",
         "permission": {"is_superuser": False}, "is_deleted": False, "role": "staff",
         "created_at": "2026-02-01T00:00:00+00:00"}]})

    import core.auth as core_auth
    core_auth.get_main_client = lambda: main_db
    import admin.auth as admin_auth
    admin_auth.get_main_client = lambda: main_db
    import admin_mcp._common as common
    common.client_for_env = lambda env: ai
    common.record_change = lambda **k: None  # capture-only; no real audit DB

    from admin_mcp.server import build_mcp_asgi_app
    return build_mcp_asgi_app()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve(app, port):
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning", lifespan="on"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    if not server.started:
        raise RuntimeError("offline MCP server failed to start")
    return server, th


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true",
                    help="boot the real server locally with a faked+seeded DB")
    ap.add_argument("--url", default=os.getenv("MCP_SMOKE_URL", ""),
                    help="deployed /mcp endpoint (live mode)")
    ap.add_argument("--token", default=os.getenv("WAJ_AI_ENGINE_MCP_TOKEN", ""),
                    help="superuser MCP token (live mode)")
    args = ap.parse_args()

    print("=" * 68)
    if args.offline:
        print("MCP SMOKE — offline (real server, faked+seeded DB, no external deps)")
        print("=" * 68)
        app = _build_offline_app()
        port = _free_port()
        server, th = _serve(app, port)
        endpoint = f"http://127.0.0.1:{port}/mcp"
        token = mint_token(sub=_OFFLINE_SUB, email="smoke@company.example", days=1,
                           secret=_OFFLINE_SECRET)
        try:
            checks = run_driver(endpoint, token, offline=True)
        finally:
            server.should_exit = True
            th.join(timeout=5)
    else:
        if not args.url or not args.token:
            ap.error("live mode needs --url and --token "
                     "(or MCP_SMOKE_URL / WAJ_AI_ENGINE_MCP_TOKEN). "
                     "Use --offline for a no-dependency check.")
        print(f"MCP SMOKE — live against {args.url}")
        print("=" * 68)
        checks = run_driver(args.url, args.token, offline=False)

    checks.report()
    sys.exit(1 if checks.failures() else 0)


if __name__ == "__main__":
    main()
