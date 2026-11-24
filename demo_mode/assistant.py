"""The builder assistant: an engine agent that builds and tests the demo's agents.

A visitor talks to it in the portal's "Build with AI" panel
(``POST /admin/demo/assistant/chat``, admin/demo_handlers.py). It is an
ordinary custom agent, the seeded "Builder assistant" profile (its
instructions and model are rows a visitor can read on the Agents screen),
run by the generic runtime with one addition: the admin MCP server's own tool
functions (admin_mcp/), called IN PROCESS with the visitor's demo session as
the actor. Never through the public ``/mcp`` endpoint, which is off in demo
mode.

Only the tools in `ALLOWED_TOOLS` are bound, each narrowed to the actions in
it and to the arguments in `ALLOWED_ARGS`, and `check_call` refuses any call outside that list, whatever the model
asks for (a call to a tool it was never shown, a forbidden action, a server
address other than the demo's own Northwind tools); an argument outside the
list is dropped before the tool runs.
Every call is also counted against the visitor's hourly tool allowance
(``DEMO_ASSISTANT_TOOLS_PER_HOUR``) and the per-message limit
(``DEMO_ASSISTANT_TOOLS_PER_TURN``).

Each call becomes a "step" event in the panel: the tool, the action, whether
it worked, and a link to whatever it created or changed ("Open Support desk").
"""
from __future__ import annotations

import asyncio
import contextvars
import copy
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from demo_mode import limits, settings

logger = logging.getLogger(__name__)

BUILDER_SLUG = "builder-assistant"

#: tool name -> the actions it may run (None: the tool has no `action`, or
#: every action it has is safe). Anything not here is never bound or run:
#: model keys, API keys, users, environment variables, deploys and targets,
#: runtime flags, background jobs, the audit log, memories, skills, model
#: comparisons (bake-offs call several models per case) and the rest.
ALLOWED_TOOLS: Dict[str, Optional[frozenset]] = {
    "list_profiles": None,
    "get_profile": None,
    "update_profile": None,
    "fork_profile": None,
    "templates": None,
    "prompts": frozenset({"get", "list", "list_versions", "create_version", "serve", "diff"}),
    "profile_attachments": None,
    "mentionables": None,
    "agent_types": None,
    "kb_sources": frozenset({"list", "get", "create", "update", "upload_document"}),
    "mcp_servers": frozenset({"list", "get", "members", "discover", "create", "update"}),
    "models": frozenset({"list", "get"}),
    "eval_suites": frozenset({"list", "get", "create", "update"}),
    "trigger_eval_run": None,
    "eval_runs": None,
    "chat": None,
    "search": None,
    "promote_profile": None,
}

#: tool name -> the arguments the assistant may pass. The schema it sees has
#: only these, and any other argument in a call is dropped before the tool
#: runs (so it takes its default: `env` is always staging, no environment
#: variable is ever named as a credential, no eval gate is forced, no
#: endpoint or voice setting is changed).
ALLOWED_ARGS: Dict[str, frozenset] = {
    "list_profiles": frozenset({"agent_kind", "status", "agent_type", "limit"}),
    "get_profile": frozenset({"profile_id", "agent_kind", "slug", "fields"}),
    "update_profile": frozenset({"profile_id", "display_name", "description", "status",
                                 "model_main_id", "is_active", "delegatable",
                                 "prompt_version_ids", "data_source_ids",
                                 "downstream_profile_ids", "agent_type"}),
    "fork_profile": frozenset({"profile_id"}),
    "templates": frozenset({"action", "template_id", "display_name", "slug",
                            "as_subagent_of", "model_id", "agent_type"}),
    "prompts": frozenset({"action", "prompt_id", "content", "notes", "profile_id", "prompt_key",
                          "version_id", "version_number", "agent_kind", "ids",
                          "include_content", "confirm"}),
    "profile_attachments": frozenset({"profile_id", "kind", "action", "ids", "tool_subset"}),
    "mentionables": frozenset({"profile_id"}),
    "agent_types": frozenset({"action", "agent_type", "display_name", "slug", "description",
                              "color", "confirm"}),
    "kb_sources": frozenset({"action", "source_id", "name", "kind", "config", "is_active",
                             "confirm", "filename", "content", "title", "language"}),
    "mcp_servers": frozenset({"action", "mcp_server_id", "name", "source", "transport",
                              "endpoint", "tool_subset", "usage_guidance", "is_active",
                              "confirm"}),
    "models": frozenset({"action", "model_id"}),
    "eval_suites": frozenset({"action", "suite_id", "name", "profile_id", "pass_threshold",
                              "is_active", "evaluator_model_id", "scenarios", "confirm"}),
    "trigger_eval_run": frozenset({"profile_id", "suite_id", "source", "model_id",
                                   "prompt_overrides", "variant_label", "repeats"}),
    "eval_runs": frozenset({"action", "run_id", "run_b", "suite_id", "profile_id", "days",
                            "limit", "run_ids", "include_results"}),
    "chat": frozenset({"profile_id", "message", "session_id", "locale", "timeout_seconds"}),
    "search": frozenset({"q", "limit"}),
    "promote_profile": frozenset({"profile_id", "target_env", "confirm"}),
}


def allowed_args(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """`args` without the arguments the assistant may not pass."""
    keep = ALLOWED_ARGS.get(name, frozenset())
    return {k: v for k, v in (args or {}).items() if k in keep}


_SHARED_KB_TABLES = {"table": "ai_kb_embeddings", "pages_table": "ai_kb_pages"}
_MAX_SCENARIOS = 10
_MAX_DESCRIPTION = 1500

REFUSED = "That is switched off in the public demo."


# --------------------------------------------------------------------------
# The allow-list
# --------------------------------------------------------------------------


def check_call(name: str, args: Dict[str, Any]) -> Optional[str]:
    """Why the assistant may not make this call, or None when it may."""
    from demo_mode import northwind

    if name not in ALLOWED_TOOLS:
        return f"The tool {name!r} is not available to the assistant in the public demo."
    args = args if isinstance(args, dict) else {}
    actions = ALLOWED_TOOLS[name]
    action = str(args.get("action") or "")
    if actions is not None and action and action not in actions:
        return f"{name}(action={action!r}) is not available in the public demo."

    if name == "mcp_servers" and action in ("create", "update", "discover"):
        endpoint = str(args.get("endpoint") or "")
        if action in ("create", "discover") or endpoint:
            if not northwind.handles(endpoint):
                return ("In the public demo the assistant can only connect the built-in "
                        f"Northwind tool server ({northwind.northwind_endpoint()}).")
        if action == "create" and (args.get("source") or "external") != "external":
            return "Only an external server pointing at the Northwind tools can be added."
    if name == "kb_sources":
        if action == "create" and (args.get("kind") or "pgvector") != "pgvector":
            return "Knowledge bases in the public demo are built-in (pgvector) ones."
        config = args.get("config")
        if isinstance(config, dict):
            for key in ("table", "pages_table"):
                if config.get(key) and config[key] != _SHARED_KB_TABLES[key]:
                    return "Knowledge bases in the public demo use the built-in tables."
        if action == "upload_document":
            size = len(str(args.get("content") or "").encode("utf-8"))
            if size > settings.max_upload_bytes():
                return (f"Documents in the public demo can be at most "
                        f"{settings.max_upload_bytes() // 1024} KB.")
    if name == "trigger_eval_run":
        if (args.get("source") or "suite") != "suite":
            return "In the public demo a test run grades a test suite (source='suite')."
        if args.get("voice_mode"):
            return "Voice test runs are switched off in the public demo."
        if int(args.get("repeats") or 1) > 2:
            return "In the public demo a test run repeats each case at most twice."
        if int(args.get("concurrency") or 0) > 2:
            return "In the public demo a test run runs at most two cases at a time."
    if name == "eval_suites" and action in ("create", "update"):
        if args.get("runner_kind") not in (None, "", "chat"):
            return "In the public demo a test suite chats with the agent (runner_kind='chat')."
        scenarios = args.get("scenarios")
        if isinstance(scenarios, list) and len(scenarios) > _MAX_SCENARIOS:
            return f"A test suite in the public demo has at most {_MAX_SCENARIOS} cases."
    if name == "chat":
        try:
            timeout = int(args.get("timeout_seconds") or 120)
        except (TypeError, ValueError):
            timeout = 120
        if timeout > 120:
            return "chat: timeout_seconds can be at most 120 in the public demo."
    return None


# --------------------------------------------------------------------------
# The turn: who is asking, what has run
# --------------------------------------------------------------------------


@dataclass
class Turn:
    actor: Dict[str, Any]
    ip: str = "unknown"
    session: Optional[str] = None
    calls: int = 0
    wrote: bool = False
    steps: List[Dict[str, Any]] = field(default_factory=list)
    #: What is running now ("Waiting for the test run"), for the panel.
    working: str = ""
    _sent: int = 0

    def drain(self) -> List[Dict[str, Any]]:
        out = self.steps[self._sent:]
        self._sent = len(self.steps)
        return out


TURN: contextvars.ContextVar[Optional[Turn]] = contextvars.ContextVar("demo_assistant_turn", default=None)

_READ_ACTIONS = {"list", "get", "members", "discover", "list_versions", "view", "diff",
                 "report", "compare", "matrix", "overview"}
_READ_TOOLS = {"list_profiles", "get_profile", "diff_profiles", "mentionables", "eval_runs",
               "search", "promotion_status"}


def _is_write(name: str, args: Dict[str, Any]) -> bool:
    if name in _READ_TOOLS:
        return False
    action = str(args.get("action") or "")
    if name in ("chat",):
        return False
    return not (action and action in _READ_ACTIONS)


def _label(row: Dict[str, Any]) -> str:
    return str(row.get("display_name") or row.get("name") or row.get("slug") or "it")[:60]


def links_for(name: str, args: Dict[str, Any], result: Any) -> List[Dict[str, str]]:
    """Portal links for what a call created, changed or ran."""
    out: List[Dict[str, str]] = []
    seen = set()

    def add(text: str, href: str) -> None:
        if href not in seen:
            seen.add(href)
            out.append({"text": text, "href": href})

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 3 or len(out) >= 4:
            return
        if isinstance(node, dict):
            if node.get("profile_id") and (node.get("display_name") or node.get("slug")):
                add(f"Open {_label(node)}", agent_href(node["profile_id"]))
            if node.get("run_id") and name in ("trigger_eval_run", "eval_runs"):
                add("Open the test run", f"/admin/evals/runs/{node['run_id']}")
            if node.get("suite_id") and node.get("name") and name == "eval_suites":
                add(f"Open {_label(node)}", f"/admin/evals/suites/{node['suite_id']}")
            if node.get("source_id") and node.get("name") and name == "kb_sources":
                add(f"Open {_label(node)}", f"/admin/kb/{node['source_id']}")
            if node.get("mcp_server_id") and node.get("name") and name == "mcp_servers":
                add(f"Open {_label(node)}", f"/admin/agent-mcp-servers/{node['mcp_server_id']}")
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:3]:
                walk(item, depth + 1)

    if _is_write(name, args) or name in ("trigger_eval_run",):
        walk(result)
    if not out and args.get("profile_id") and _is_write(name, args):
        add("Open the agent", agent_href(args["profile_id"]))
    return out


def _profile_name(profile_id: str) -> str:
    """An agent's display name, for a step label. Blocking; '' on any error."""
    try:
        from core.auth import get_service_client

        rows = (get_service_client().table("agent_profiles").select("display_name, slug")
                .eq("profile_id", profile_id).limit(1).execute().data or [])
    except Exception:  # noqa: BLE001 — a label is cosmetic
        return ""
    return str((rows[0].get("display_name") or rows[0].get("slug") or "") if rows else "")[:60]


def agent_href(profile_id: str) -> str:
    """An agent's page in the portal (always the staging copy the demo edits)."""
    return f"/admin/agents?profile_id={profile_id}&env=staging"


def _summary(name: str, args: Dict[str, Any]) -> str:
    """A tool call in the panel before it has run (its working label)."""
    return step_label(name, args, None)


_KIND_WORDS = {"mcp": "tools", "kb": "the knowledge base", "downstream": "a sub-agent",
               "tools": "tools", "skills": "a skill"}


def _name_in(result: Any) -> str:
    """The display name of the first agent, suite or source in a result."""
    def walk(node: Any, depth: int = 0) -> str:
        if depth > 3:
            return ""
        if isinstance(node, dict):
            for key in ("display_name", "name"):
                if isinstance(node.get(key), str) and node[key].strip():
                    return node[key].strip()[:60]
            for value in node.values():
                found = walk(value, depth + 1)
                if found:
                    return found
        elif isinstance(node, list) and node:
            return walk(node[0], depth + 1)
        return ""
    return walk(result)


def step_label(name: str, args: Dict[str, Any], result: Any) -> str:
    """What a tool call did, in words a visitor reads in the panel
    ("Created Outage helper from a template"), never a raw tool name."""
    action = str(args.get("action") or "")
    named = _name_in(result) if isinstance(result, (dict, list)) else ""
    given = str(args.get("display_name") or args.get("name") or "").strip()[:60]
    who = given or named
    if name == "list_profiles":
        return "Looked at the agents"
    if name == "get_profile":
        return f"Looked at {who}" if who else "Looked at the agent"
    if name == "update_profile":
        if args.get("status"):
            return f"Set {who or 'the agent'} to {args['status']}"
        return f"Updated {who}" if who else "Updated the agent"
    if name == "fork_profile":
        return f"Copied {who}" if who else "Copied the agent"
    if name == "templates":
        if action == "use":
            return f"Created {given or 'an agent'} from a template"
        return "Looked at the templates"
    if name == "prompts":
        return {"create_version": "Wrote its instructions",
                "serve": "Switched it to the new instructions",
                "diff": "Compared two versions of the instructions"}.get(action, "Read the instructions")
    if name == "profile_attachments":
        what = _KIND_WORDS.get(str(args.get("kind") or ""), "it")
        if what == "tools" and str(args.get("kind")) == "mcp" and "northwind" in json.dumps(
                result or {}, default=str).lower():
            what = "the Northwind tools"
        if action == "add":
            return f"Attached {what}"
        if action == "remove":
            return f"Removed {what}"
        return "Looked at what it has attached"
    if name == "mentionables":
        return "Looked at what its instructions can mention"
    if name == "agent_types":
        return {"create": f"Created the category {given}".strip(),
                "update": "Updated a category", "delete": "Deleted a category"}.get(
                    action, "Looked at the categories")
    if name == "kb_sources":
        return {"create": f"Created the knowledge base {given}".strip(),
                "update": "Updated the knowledge base",
                "upload_document": "Added a document to the knowledge base"}.get(
                    action, "Looked at the knowledge bases")
    if name == "mcp_servers":
        return {"create": "Connected the Northwind tools", "update": "Updated the tool server",
                "discover": "Checked the Northwind tools"}.get(action, "Looked at the tool servers")
    if name == "models":
        return "Looked at the models"
    if name == "eval_suites":
        return {"create": f"Created the test suite {given}".strip(),
                "update": "Updated the test suite"}.get(action, "Looked at the test suites")
    if name == "trigger_eval_run":
        return "Started a test run"
    if name == WAIT_TOOL:
        if isinstance(result, dict) and result.get("finished"):
            passed, total = result.get("passed"), result.get("scenarios")
            if isinstance(passed, int) and isinstance(total, int) and total:
                return f"Test run finished: {passed} of {total} passed"
            return "Test run finished"
        if isinstance(result, dict):
            return "The test run is still going"
        return "Waiting for the test run"
    if name == "eval_runs":
        return {"compare": "Compared two test runs", "list": "Looked at recent test runs"}.get(
            action, "Read the test results")
    if name == "chat":
        return f"Chatted with {given}" if given else "Chatted with the agent"
    if name == "search":
        return "Searched the portal"
    if name == "promote_profile":
        return "Published it to production" if args.get("confirm") else "Checked what publishing would change"
    return "Used a tool"


# --------------------------------------------------------------------------
# The tools
# --------------------------------------------------------------------------

_registered = False


def _admin_mcp():
    """The admin MCP server with every tool module imported (registering its
    tools), the way `admin_mcp.server.build_mcp_asgi_app` does, minus the
    HTTP app."""
    global _registered
    from admin_mcp import server

    if not _registered:
        import admin_mcp.tools_configure  # noqa: F401
        import admin_mcp.tools_dispatch  # noqa: F401
        import admin_mcp.tools_govern  # noqa: F401
        import admin_mcp.tools_observe  # noqa: F401
        import admin_mcp.tools_ops  # noqa: F401
        import admin_mcp.tools_ship  # noqa: F401
        import admin_mcp.tools_test  # noqa: F401
        from admin_mcp.param_docs import apply_param_descriptions

        apply_param_descriptions(server.mcp)
        _registered = True
    return server.mcp


def _narrow_schema(name: str, schema: Dict[str, Any]) -> Dict[str, Any]:
    """The tool's JSON schema as the assistant sees it: only its allowed
    actions, without the arguments it may not set."""
    out = copy.deepcopy(schema or {"type": "object", "properties": {}})
    keep = ALLOWED_ARGS.get(name, frozenset())
    props = {k: v for k, v in (out.get("properties") or {}).items() if k in keep}
    out["properties"] = props
    actions = ALLOWED_TOOLS.get(name)
    if actions is not None and isinstance(props.get("action"), dict):
        spec = props["action"]
        if isinstance(spec.get("enum"), list):
            spec["enum"] = [a for a in spec["enum"] if a in actions]
        spec["description"] = "Operation to run: " + " | ".join(sorted(actions)) + "."
        if spec.get("default") not in actions:
            spec.pop("default", None)
    if isinstance(out.get("required"), list):
        out["required"] = [r for r in out["required"] if r in props]
    out.pop("title", None)
    return out


async def run_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Run one admin tool for the current turn's visitor, or refuse it."""
    turn = TURN.get()
    args = allowed_args(name, args)
    if turn is None:
        return {"ok": False, "error": "The assistant can only act for a signed-in visitor."}
    reason = check_call(name, args)
    if reason is None and (_is_write(name, args) or name == "chat"):
        from demo_mode import protect

        if await asyncio.to_thread(protect.mentions_protected, list(args.values())):
            reason = protect.READ_ONLY_MESSAGE
    if reason is None and turn.calls >= settings.assistant_tools_per_turn():
        reason = STEP_CAP_FOR_MODEL
    if reason is None and not limits.hit("assistant_tools", ip=turn.ip, session=turn.session):
        reason = limits.RATE_MESSAGE
    step: Dict[str, Any] = {"tool": name, "action": str(args.get("action") or ""),
                            "label": _summary(name, args)}
    if reason is not None:
        logger.info("demo assistant: refused %s (%s)", name, reason)
        step.update(ok=False, note=STEP_CAP_FOR_VISITOR if reason == STEP_CAP_FOR_MODEL else reason,
                    links=[])
        turn.steps.append(step)
        return {"ok": False, "error": reason}
    turn.calls += 1

    from pydantic import ValidationError

    from admin_mcp.context import MCP_ACTOR

    tool = _admin_mcp()._tool_manager.get_tool(name)
    if tool is None:
        step.update(ok=False, note="not available on this install", links=[])
        turn.steps.append(step)
        return {"ok": False, "error": f"{name} is not available on this install."}
    token = MCP_ACTOR.set({"user_id": turn.actor.get("user_id"), "email": turn.actor.get("email"),
                           "user_token": turn.actor.get("user_token")})
    try:
        meta = tool.fn_metadata
        parsed = meta.arg_model.model_validate(meta.pre_parse_json(args)).model_dump_one_level()
        if tool.is_async:
            result = await tool.fn(**parsed)
        else:
            # Blocking database calls: off the event loop. to_thread copies
            # the context, so the tool still sees MCP_ACTOR.
            result = await asyncio.to_thread(tool.fn, **parsed)
    except ValidationError as exc:
        result = {"error": "invalid arguments", "detail": exc.errors(include_url=False)[:5]}
    except Exception as exc:  # noqa: BLE001 — the model reads the error and recovers
        logger.warning("demo assistant: %s failed: %s", name, exc)
        result = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    finally:
        MCP_ACTOR.reset(token)
    ok = not (isinstance(result, dict) and result.get("error"))
    if ok and _is_write(name, args) and not (isinstance(result, dict) and result.get("confirmed") is False):
        turn.wrote = True
    label_args = dict(args)
    if name in ("chat", "get_profile", "update_profile", "fork_profile") and args.get("profile_id") \
            and not args.get("display_name"):
        agent = await asyncio.to_thread(_profile_name, str(args["profile_id"]))
        if agent:
            label_args["display_name"] = agent
    step.update(ok=ok, label=step_label(name, label_args, result if ok else None),
                links=links_for(name, args, result) if ok else [],
                note="" if ok else str((result or {}).get("error"))[:200])
    turn.steps.append(step)
    return result if isinstance(result, dict) else {"result": result}


WAIT_TOOL = "wait_for_test_run"
# The per-message step cap: what the model is told, and what the visitor sees.
STEP_CAP_FOR_MODEL = ("That is as many steps as one message may take in the public demo. "
                      "Tell the visitor what is done and ask them to continue in a new message.")
STEP_CAP_FOR_WAIT = ("That is as many steps as one message may take in the public demo. "
                     "Tell the visitor the run is going and link it.")
STEP_CAP_FOR_VISITOR = "Step limit for one message reached"
_WAIT_MAX_SEC = 120
_POLL_SEC = 5.0
_RUNNING = {"", "queued", "pending", "running", "started", "in_progress"}
_CASE_KEYS = ("scenario_id", "id", "question", "expected_answer", "response", "reply", "answer",
              "reasoning", "judge_reasoning", "reason", "score", "error")


def _brief_case(case: Any) -> Dict[str, Any]:
    if not isinstance(case, dict):
        return {"case": str(case)[:300]}
    return {k: (str(case[k])[:300] if isinstance(case[k], str) else case[k])
            for k in _CASE_KEYS if case.get(k) not in (None, "")}


def summarize_run(run: Dict[str, Any]) -> Dict[str, Any]:
    """What the visitor needs from a finished (or unfinished) test run."""
    status = str(run.get("status") or "")
    summary = run.get("results_summary") or {}
    failing_ids = {str(f.get("scenario_id") or f.get("id") or "") if isinstance(f, dict) else str(f)
                   for f in summary.get("failing") or []}
    full = [r for r in run.get("results") or [] if isinstance(r, dict) and not r.get("passed")]
    cases = [_brief_case(r) for r in full][:3] or [_brief_case(f) for f in
                                                   (summary.get("failing") or [])[:3]]
    out: Dict[str, Any] = {
        "run_id": run.get("run_id"),
        "finished": status.lower() not in _RUNNING,
        "status": status,
        "score": run.get("score"),
        "scenarios": summary.get("scenarios"),
        "passed": summary.get("passed"),
        "failed": summary.get("failed"),
        "failing_cases": cases,
        "failing_ids": sorted(i for i in failing_ids if i)[:10],
    }
    if run.get("error"):
        out["error"] = str(run["error"])[:300]
    if run.get("run_id"):
        out["link"] = f"/admin/evals/runs/{run['run_id']}"
    return out


async def wait_for_test_run(run_id: str, max_seconds: int = 110) -> Dict[str, Any]:
    """Wait (up to two minutes) for a test run to finish; one tool call."""
    turn = TURN.get()
    if turn is None:
        return {"ok": False, "error": "The assistant can only act for a signed-in visitor."}
    run_id = str(run_id or "").strip()
    reason = None
    if not run_id:
        reason = "Pass the run_id that trigger_eval_run returned."
    elif turn.calls >= settings.assistant_tools_per_turn():
        reason = STEP_CAP_FOR_WAIT
    elif not limits.hit("assistant_tools", ip=turn.ip, session=turn.session):
        reason = limits.RATE_MESSAGE
    if reason:
        note = STEP_CAP_FOR_VISITOR if reason == STEP_CAP_FOR_WAIT else reason
        turn.steps.append({"tool": WAIT_TOOL, "action": "", "label": step_label(WAIT_TOOL, {}, None),
                           "ok": False, "note": note, "links": []})
        return {"ok": False, "error": reason}
    turn.calls += 1
    from admin_mcp.context import MCP_ACTOR

    tool = _admin_mcp()._tool_manager.get_tool("eval_runs")
    budget = max(5, min(int(max_seconds or _WAIT_MAX_SEC), _WAIT_MAX_SEC))
    deadline = time.monotonic() + budget
    turn.working = "Waiting for the test run"
    token = MCP_ACTOR.set({"user_id": turn.actor.get("user_id"), "email": turn.actor.get("email"),
                           "user_token": turn.actor.get("user_token")})
    summary: Dict[str, Any] = {"run_id": run_id, "finished": False}
    try:
        while True:
            try:
                run = await asyncio.to_thread(tool.fn, action="get", run_id=run_id,
                                              include_results=True)
            except Exception as exc:  # noqa: BLE001
                run = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            if isinstance(run, dict) and isinstance(run.get("run"), dict):
                run = run["run"]  # eval_runs(action="get") wraps the row
            if isinstance(run, dict) and run.get("error") and not run.get("status"):
                summary = {"run_id": run_id, "finished": False, "error": run["error"]}
                break
            summary = summarize_run(run if isinstance(run, dict) else {})
            if summary["finished"] or time.monotonic() + _POLL_SEC > deadline:
                break
            await asyncio.sleep(_POLL_SEC)
    finally:
        MCP_ACTOR.reset(token)
        turn.working = ""
    if not summary.get("finished") and not summary.get("error"):
        summary["note"] = (f"Still running after {budget} seconds. Say so and link it: "
                           f"/admin/evals/runs/{run_id}")
    ok = not summary.get("error")
    turn.steps.append({"tool": WAIT_TOOL, "action": "", "ok": ok,
                       "label": step_label(WAIT_TOOL, {}, summary),
                       "note": "" if ok else str(summary["error"])[:200],
                       "links": [{"text": "Open the test run",
                                  "href": f"/admin/evals/runs/{run_id}"}]})
    return summary


def build_tools() -> List[Any]:
    """The allow-listed admin tools as LangChain tools, plus the demo's own
    `wait_for_test_run`."""
    from langchain_core.tools import StructuredTool

    server = _admin_mcp()
    out = [StructuredTool(
        name=WAIT_TOOL,
        description=("Wait for a test run to finish (up to two minutes) and return its score, "
                     "how many cases passed and the failing cases (question, the agent's "
                     "answer, the judge's reasoning). Call it right after trigger_eval_run "
                     "with the run_id it returned. If it is still running, say so and link it."),
        args_schema={"type": "object", "properties": {
            "run_id": {"type": "string", "description": "The run_id from trigger_eval_run."},
            "max_seconds": {"type": "integer", "description": "How long to wait (at most 120)."}},
            "required": ["run_id"]},
        coroutine=wait_for_test_run,
    )]
    for name in ALLOWED_TOOLS:
        tool = server._tool_manager.get_tool(name)
        if tool is None:
            continue

        def _make(tool_name: str):
            async def _call(**kwargs: Any) -> str:
                result = await run_tool(tool_name, kwargs)
                return json.dumps(result, ensure_ascii=False, default=str)
            return _call

        out.append(StructuredTool(
            name=name,
            description=(tool.description or name)[:_MAX_DESCRIPTION],
            args_schema=_narrow_schema(name, tool.parameters),
            coroutine=_make(name),
        ))
    return out


# --------------------------------------------------------------------------
# The agent
# --------------------------------------------------------------------------


def page_note(page: Optional[str]) -> str:
    page = (page or "").strip()[:200]
    if not page.startswith("/admin"):
        return ""
    return (f"## Where the visitor is\nThe visitor has the portal page {page} open. "
            "\"This agent\" or \"this test\" means the one on that page when its id is in "
            "the address.")


def create_agent(page: Optional[str] = None):
    """The Builder assistant, ready to run. Blocking (profile load)."""
    from runtime.agents.custom import CustomRuntimeAgent

    class BuilderAssistant(CustomRuntimeAgent):
        max_iterations = 10

        def _knowledge_tools(self) -> List[Any]:
            return build_tools() + list(super()._knowledge_tools())

        def _turn_context(self, voice_turn: bool, prefetched: Optional[str]) -> str:
            base = super()._turn_context(voice_turn, prefetched)
            note = page_note(page)
            return base + ("\n\n" + note if note else "")

    return BuilderAssistant(BUILDER_SLUG)


def visitor_of(scope: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    from demo_mode.gate import visitor

    return visitor(scope)
