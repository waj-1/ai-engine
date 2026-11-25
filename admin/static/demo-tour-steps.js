/* Demo tour: the step catalog (plain data plus small pure functions).
 *
 * Read by admin/static/demo-tour.js. No DOM work, no listeners, no storage:
 * this file only sets window.DEMO_TOUR_CATALOG. Shape: the "Step catalog
 * shape" section of the tour contract. Copy changes go here, never in the
 * tour script.
 *
 * Code strings: {BASE} becomes location.origin and {AGENT} the visitor's own
 * agent slug (or support-desk). They are what a visitor runs against their
 * own copy of the engine; nothing in the demo takes an API key.
 */
(function () {
  "use strict";

  var SUPPORT = "support-desk";
  var SUITE = "Support desk: everyday questions";

  // ---------------------------------------------------------------------
  // Small pure helpers
  // ---------------------------------------------------------------------

  function param(loc, name) {
    try { return new URLSearchParams(loc.search || "").get(name) || ""; }
    catch (e) { return ""; }
  }

  function path(loc) {
    return String((loc && loc.pathname) || "").replace(/\/+$/, "") || "/";
  }

  function referrer(doc) {
    try { return String((doc && doc.referrer) || ""); } catch (e) { return ""; }
  }

  function mine(made) {
    var list = (made && made.agents) || [];
    return list.length ? list[list.length - 1] : "";
  }

  // Agent pages are addressed by profile UUID, which changes at every
  // nightly reset. A step names its agent by slug in `agent(made)`; CORE
  // resolves it (POST /admin/search.json) and opens that page. `start` is
  // the Agents list, the fallback when the lookup fails, and the first card
  // (`agent-link` on that slug's row) is skipped once the page is open.
  function agentTarget(slug, tab, query) {
    return { slug: slug || SUPPORT, tab: tab || "", query: query || "" };
  }

  // The agent's page URL, as a Promise (CORE accepts one from `start`):
  // POST /admin/search.json {q: slug} lists the agent as {type: "Agent",
  // tech: slug, href: "/admin/agents?env=…&profile_id=<uuid>"}. Falls back to
  // the Agents list, where the first card points at the agent's row.
  function agentPage(target) {
    var list = "/admin/agents";
    if (typeof fetch !== "function" || typeof Promise !== "function") return list;
    return fetch("/admin/search.json", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ q: target.slug, env: "staging" })
    }).then(function (r) { return r.ok ? r.json() : {}; }).then(function (data) {
      var hit = ((data && data.results) || []).filter(function (x) {
        return x && x.type === "Agent" && x.tech === target.slug && x.href;
      })[0];
      if (!hit) return list;
      var extra = (target.tab ? "&tab=" + target.tab : "") +
        (target.query ? "&" + target.query : "");
      return hit.href + extra;
    }).catch(function () { return list; });
  }

  function agentTab(doc) {
    if (!doc || !doc.querySelector) return "";
    var el = doc.querySelector("[data-agent-tab]");
    return el ? String(el.getAttribute("data-agent-tab") || "") : "";
  }

  // An agent detail page (any agent), optionally on one tab.
  function onAgentPage(loc, tab, doc) {
    if (path(loc) !== "/admin/agents" || !param(loc, "profile_id")) return false;
    if (!tab) return true;
    var shown = agentTab(doc) || param(loc, "tab") || "overview";
    return shown === tab;
  }

  function agentSlugIn(doc) {
    if (!doc || !doc.querySelector) return "";
    var el = doc.querySelector("[data-agent-tab][data-agent-slug]");
    return el ? String(el.getAttribute("data-agent-slug") || "") : "";
  }

  // The card that opens an agent from the Agents list, when the direct
  // lookup did not take the visitor there. `pick.value(made)` names the row.
  function openAgentCard(slugOf) {
    return {
      anchor: "agent-link",
      pick: { attr: "data-tour-slug", value: slugOf },
      skip: function (loc) { return onAgentPage(loc); },
      title: "Open the agent",
      body: "Agents lists every agent in staging, with its slug under its name.",
      yourTurn: "open the highlighted agent.",
      advance: { on: "page", match: function (loc) { return onAgentPage(loc); } }
    };
  }

  function mineOr(made) { return mine(made) || SUPPORT; }

  // --- "Same step, in code" generators: one admin MCP tool call --------

  function pyValue(v) {
    if (v === true) return "True";
    if (v === false) return "False";
    if (v === null) return "None";
    if (Array.isArray(v)) return "[" + v.map(pyValue).join(", ") + "]";
    if (typeof v === "object") {
      return "{" + Object.keys(v).map(function (k) {
        return JSON.stringify(k) + ": " + pyValue(v[k]);
      }).join(", ") + "}";
    }
    return JSON.stringify(v);
  }

  // get_profile(agent_kind="custom", slug="support-desk")
  function mcpCall(name, args) {
    var parts = Object.keys(args || {}).map(function (k) {
      return k + "=" + pyValue(args[k]);
    });
    var one = name + "(" + parts.join(", ") + ")";
    if (one.length <= 72) return one;
    return name + "(\n    " + parts.join(",\n    ") + ")";
  }

  function mcpCalls(calls) {
    return calls.map(function (c) {
      return (c.note ? "# " + c.note + "\n" : "") + mcpCall(c.tool, c.args);
    }).join("\n");
  }

  // The same call over HTTP: JSON-RPC tools/call on the admin MCP server
  // (stateless Streamable HTTP with JSON replies, so no session is needed).
  function curlCall(name, args) {
    var keys = Object.keys(args || {});
    var pair = function (k) { return JSON.stringify(k) + ": " + JSON.stringify(args[k]); };
    var a = "{" + keys.map(pair).join(", ") + "}";
    var tail = "  \"arguments\": " + a + "}}'";
    if (tail.length > 78) {
      tail = "  \"arguments\": {\n" + keys.map(function (k) {
        return "    " + pair(k);
      }).join(",\n") + "}}}'";
    }
    return [
      "curl -s {BASE}/mcp \\",
      "  -H \"Authorization: Bearer $AI_ENGINE_API_KEY\" \\",
      "  -H \"Content-Type: application/json\" \\",
      "  -H \"Accept: application/json, text/event-stream\" \\",
      "  -d '{\"jsonrpc\": \"2.0\", \"id\": 1, \"method\": \"tools/call\",",
      "  \"params\": {\"name\": \"" + name + "\",",
      tail
    ].join("\n");
  }

  var PY_HELPER = [
    "import json, os, httpx",
    "",
    "def call(tool, **args):  # one admin MCP tool call",
    "    r = httpx.post(\"{BASE}/mcp\", timeout=60, json={",
    "        \"jsonrpc\": \"2.0\", \"id\": 1, \"method\": \"tools/call\",",
    "        \"params\": {\"name\": tool, \"arguments\": args}},",
    "        headers={\"Accept\": \"application/json, text/event-stream\",",
    "                 \"Authorization\": \"Bearer \"",
    "                 + os.environ[\"AI_ENGINE_API_KEY\"]})",
    "    return json.loads(r.json()[\"result\"][\"content\"][0][\"text\"])",
    ""
  ].join("\n");

  function pyArgs(args) {
    return Object.keys(args || {}).map(function (k) {
      return k + "=" + pyValue(args[k]);
    }).join(", ");
  }

  function pythonCalls(calls) {
    return PY_HELPER + "\n" + calls.map(function (c) {
      var line = (c.as ? c.as + " = " : "") + "call(\"" + c.tool + "\"";
      var a = pyArgs(c.args);
      if (a && line.length + a.length + 3 <= 78) {
        line += ", " + a;
      } else if (a) {
        line += ",\n    " + Object.keys(c.args).map(function (k) {
          return k + "=" + pyValue(c.args[k]);
        }).join(",\n    ");
      }
      return (c.note ? "# " + c.note + "\n" : "") + line + ")";
    }).join("\n");
  }

  // Portal path + MCP + curl + Python for a step whose action is MCP calls.
  // `httpIndex` picks the call the curl tab shows (curl shows one call).
  function toolCode(portal, calls, httpIndex) {
    var main = calls[httpIndex || 0];
    return {
      portal: portal,
      mcp: mcpCalls(calls),
      curl: curlCall(main.tool, main.args),
      python: pythonCalls(calls)
    };
  }

  // ---------------------------------------------------------------------
  // Snippets for the chat, OpenAI and MCP-client steps
  // ---------------------------------------------------------------------

  var CHAT_CURL = [
    "export AI_ENGINE_API_KEY=wae_...",
    "",
    "curl -N {BASE}/v1/agents/{AGENT}/chat \\",
    "  -H \"Authorization: Bearer $AI_ENGINE_API_KEY\" \\",
    "  -H \"Content-Type: application/json\" \\",
    "  -d '{\"message\": \"Is there an outage in Riverside?\"}'",
    "",
    "# continue the conversation: send the session id back",
    "curl -N {BASE}/v1/agents/{AGENT}/chat \\",
    "  -H \"Authorization: Bearer $AI_ENGINE_API_KEY\" \\",
    "  -H \"Content-Type: application/json\" \\",
    "  -d '{\"message\": \"And my order?\", \"session_id\": \"<id>\"}'"
  ].join("\n");

  var CHAT_CURL_A2 = [
    "curl -N {BASE}/v1/agents/" + SUPPORT + "/chat \\",
    "  -H \"Authorization: Bearer $AI_ENGINE_API_KEY\" \\",
    "  -H \"Content-Type: application/json\" \\",
    "  -d '{\"message\": \"My internet is slow tonight\"}'",
    "# SSE: session, answer…, usage, done"
  ].join("\n");

  var CHAT_PY = [
    "import json, os, httpx",
    "",
    "url = \"{BASE}/v1/agents/{AGENT}/chat\"",
    "key = os.environ[\"AI_ENGINE_API_KEY\"]",
    "body = {\"message\": \"Is there an outage in Riverside?\"}",
    "with httpx.stream(\"POST\", url, json=body, timeout=120,",
    "                  headers={\"Authorization\": f\"Bearer {key}\"}) as r:",
    "    for line in r.iter_lines():",
    "        if not line.startswith(\"data: \"):",
    "            continue",
    "        event = json.loads(line[6:])",
    "        if event[\"type\"] == \"session\":",
    "            session_id = event[\"id\"]  # send back to continue",
    "        elif event[\"type\"] == \"answer\":",
    "            print(event[\"content\"], end=\"\", flush=True)"
  ].join("\n");

  var CHAT_PY_A2 = [
    "import json, os, httpx",
    "",
    "url = \"{BASE}/v1/agents/" + SUPPORT + "/chat\"",
    "key = os.environ[\"AI_ENGINE_API_KEY\"]",
    "body = {\"message\": \"My internet is slow tonight\"}",
    "with httpx.stream(\"POST\", url, json=body, timeout=120,",
    "                  headers={\"Authorization\": f\"Bearer {key}\"}) as r:",
    "    for line in r.iter_lines():",
    "        if line.startswith(\"data: \"):",
    "            print(json.loads(line[6:]))  # session, answer…, usage, done"
  ].join("\n");

  var OPENAI_PY = [
    "import os",
    "from openai import OpenAI",
    "",
    "client = OpenAI(base_url=\"{BASE}/v1\",",
    "                api_key=os.environ[\"AI_ENGINE_API_KEY\"])",
    "",
    "stream = client.chat.completions.create(",
    "    model=\"{AGENT}\",  # the agent is the model",
    "    messages=[{\"role\": \"user\",",
    "               \"content\": \"Is there an outage in Riverside?\"}],",
    "    stream=True,",
    ")",
    "for chunk in stream:",
    "    if chunk.choices and chunk.choices[0].delta.content:",
    "        print(chunk.choices[0].delta.content, end=\"\", flush=True)",
    "",
    "print([m.id for m in client.models.list()])  # published agents"
  ].join("\n");

  var OPENAI_JS = [
    "import OpenAI from \"openai\";",
    "",
    "const client = new OpenAI({",
    "  baseURL: \"{BASE}/v1\",",
    "  apiKey: process.env.AI_ENGINE_API_KEY,",
    "});",
    "",
    "const stream = await client.chat.completions.create({",
    "  model: \"{AGENT}\", // the agent is the model",
    "  messages: [{ role: \"user\", content: \"Is there an outage in Riverside?\" }],",
    "  stream: true,",
    "});",
    "for await (const chunk of stream) {",
    "  process.stdout.write(chunk.choices[0]?.delta?.content ?? \"\");",
    "}"
  ].join("\n");

  var OPENAI_CURL = [
    "curl {BASE}/v1/chat/completions \\",
    "  -H \"Authorization: Bearer $AI_ENGINE_API_KEY\" \\",
    "  -H \"Content-Type: application/json\" \\",
    "  -d '{\"model\": \"{AGENT}\", \"messages\": [{\"role\": \"user\",",
    "       \"content\": \"Is there an outage in Riverside?\"}]}'"
  ].join("\n");

  var MCP_JSON = [
    "{",
    "  \"mcpServers\": {",
    "    \"waj-ai-engine\": {",
    "      \"type\": \"http\",",
    "      \"url\": \"{BASE}/mcp\",",
    "      \"headers\": { \"Authorization\": \"Bearer wae_...\" }",
    "    }",
    "  }",
    "}"
  ].join("\n");

  var PLUGIN = [
    "# in your shell: where the engine is, and a Full access key",
    "export WAJ_AI_ENGINE_URL={BASE}",
    "export WAJ_AI_ENGINE_API_KEY=wae_...",
    "",
    "# in Claude Code",
    "/plugin marketplace add waj-1/ai-engine",
    "/plugin install waj-ai-engine@waj-ai-engine",
    "",
    "# then ask, for example:",
    "# \"Write a test suite for the support agent and run it\""
  ].join("\n");

  var CI_YAML = [
    "# .github/workflows/agent-tests.yml",
    "on: [pull_request]",
    "jobs:",
    "  agent-tests:",
    "    runs-on: ubuntu-latest",
    "    steps:",
    "      - uses: waj-1/ai-engine-test-action@v1",
    "        with:",
    "          engine-url: {BASE}",
    "          api-key: ${{ secrets.ENGINE_API_KEY }}",
    "          suite: \"" + SUITE + "\"",
    "          min-pass-rate: 90"
  ].join("\n");

  // The chat API's event order (endpoints/agents_v1.py, common/chat_session.py).
  var SSE_EVENTS = [
    { name: "session", text: "the session id, to continue later" },
    { name: "answer", text: "text chunks, as they stream" },
    { name: "usage", text: "tokens in and out" },
    { name: "done", text: "the turn is over" }
  ];

  var KEYS_OFF = "API keys are switched off in the public demo. These run as-is " +
    "against your own copy.";
  var PUBLIC_NOTE = "Anyone can see what you type here, so do not enter anything private.";
  var LIMIT = "The demo has reached today's limit. Do it by hand, or try again tomorrow";
  // The hourly pace limit (the assistant says "wait a few minutes").
  var LIMIT_RATE = "The demo is busy right now. Do it by hand, or try again in a few minutes";

  var A_AND_B = ["A1", "A2", "A3", "A4", "A5", "B1", "B2", "B3", "B4"];

  // ---------------------------------------------------------------------
  // The 17 steps
  // ---------------------------------------------------------------------

  var steps = [
    // ----------------------------- Build -----------------------------
    {
      id: "A1",
      track: "build",
      title: "Read what an agent is told",
      summary: "Open the Support desk's instructions: the text its model gets on every turn.",
      minutes: 1,
      short: "See the text the Support desk's model gets on every turn.",
      kind: "walkthrough",
      agent: function () { return agentTarget(SUPPORT, "instructions"); },
      start: function (made) { return agentPage(agentTarget(SUPPORT, "instructions")); },
      cards: [
        openAgentCard(function () { return SUPPORT; }),
        {
          anchor: null,
          skip: function (loc, doc) { return onAgentPage(loc, "instructions", doc); },
          title: "Open the Instructions tab",
          body: "Each agent page has tabs for its instructions, tools, sub-agents and settings.",
          yourTurn: "open Instructions.",
          advance: { on: "page",
            match: function (loc, doc) { return onAgentPage(loc, "instructions", doc); } }
        },
        {
          anchor: "agent-instructions",
          title: "Read what the agent is told",
          body: "An agent is configuration: these instructions, a model, tools from MCP " +
            "servers and optional sub-agents. Note how it names its tools and hands " +
            "billing to @billing-helper.",
          advance: { on: "next" }
        }
      ],
      done: [
        { on: "page", match: function (loc, doc) { return onAgentPage(loc, "instructions", doc); } }
      ],
      requires: [],
      requiresAny: [],
      doneWhen: "you open the Instructions tab of any agent",
      code: toolCode("Agents › Support desk › Instructions", [
        { tool: "get_profile", args: { agent_kind: "custom", slug: SUPPORT }, as: "agent" },
        { tool: "prompts", args: { action: "get", profile_id: "<profile_id>" } }
      ]),
      records: null
    },
    {
      id: "A2",
      track: "build",
      title: "Talk to it in the test chat",
      summary: "Ask the Support desk a question and watch it call check_outage.",
      minutes: 1,
      short: "Ask the Support desk a question and watch it call check_outage.",
      kind: "walkthrough",
      agent: function () { return agentTarget(SUPPORT, ""); },
      start: function (made) { return agentPage(agentTarget(SUPPORT, "")); },
      cards: [
        openAgentCard(function () { return SUPPORT; }),
        {
          anchor: "agent-test",
          title: "Open the test chat",
          body: "Test opens a chat beside the page. It calls the same chat API your app " +
            "would, on the draft or the published agent.",
          yourTurn: "click Test.",
          advance: { on: "click" }
        },
        {
          anchor: "test-input",
          title: "Talk to it in the test chat",
          body: "Send a customer message and watch it call check_outage before it answers. " +
            "Then try something it should refuse.",
          yourTurn: "press Send. The card moves on when the reply arrives.",
          advance: { on: "event", name: "tour:test-chat-sent" },
          note: PUBLIC_NOTE
        }
      ],
      done: [
        { on: "event", name: "tour:test-chat-sent" },
        { on: "ai", tools: ["chat"] }
      ],
      requires: [],
      requiresAny: [],
      doneWhen: "you send one message and the reply arrives",
      ai: {
        prompt: "Chat with the Support desk as a customer whose internet is slow tonight",
        tools: ["chat"],
        actions: ["chat"]
      },
      code: {
        portal: "Agents › Support desk › Test",
        mcp: mcpCall("chat", { profile_id: "<profile_id>",
          message: "My internet is slow tonight" }),
        curl: CHAT_CURL_A2,
        python: CHAT_PY_A2
      },
      records: null
    },
    {
      id: "A3",
      track: "build",
      title: "Create an agent from a template",
      summary: "Copy a template into a new draft agent of your own.",
      minutes: 1,
      short: "Copy a template into a draft agent of your own.",
      kind: "walkthrough",
      start: function () { return "/admin/agents"; },
      cards: [
        {
          anchor: "agents-new",
          title: "Start from a template",
          body: "New agent copies a template into a draft in staging. A draft serves " +
            "nothing until you publish it.",
          yourTurn: "click New agent.",
          advance: { on: "click" }
        },
        {
          anchor: "template-pick",
          title: "Pick a template",
          body: "A template holds starting instructions and a model. Your copy can change; " +
            "the template never does.",
          yourTurn: "pick a template.",
          advance: { on: "click" }
        },
        {
          anchor: "template-next",
          title: "Next",
          body: "Next asks for a name and a model.",
          yourTurn: "click Next.",
          advance: { on: "click" }
        },
        {
          anchor: "agent-create",
          title: "Name it and create it",
          body: "Type a name, such as Outage helper. The name becomes its slug: " +
            "it answers at /v1/agents/outage-helper/chat.",
          yourTurn: "name it, then Create draft agent.",
          advance: { on: "click" }
        }
      ],
      done: [
        // The very next page is a new agent's, reached from the create form.
        { on: "click-then-page", anchor: "agent-create",
          match: function (loc, doc) {
            return onAgentPage(loc, "overview", doc) && referrer(doc).indexOf("/admin/templates") !== -1;
          } },
        { on: "ai", actions: ["templates:use", "fork_profile"] }
      ],
      requires: [],
      requiresAny: [],
      doneWhen: "your new agent's page opens",
      ai: {
        prompt: "Build me a support agent that checks outages",
        tools: ["templates", "fork_profile"],
        actions: ["templates:use", "fork_profile"]
      },
      code: toolCode("Agents › New agent", [
        { tool: "templates", args: { action: "list" } },
        { tool: "templates", args: { action: "use", template_id: "<template_id>",
          display_name: "Outage helper" } }
      ], 1),
      records: "agent",
      recordFrom: function (loc, doc) { return agentSlugIn(doc); }
    },
    {
      id: "A4",
      track: "build",
      title: "Give it MCP tools and knowledge",
      summary: "Attach the Northwind MCP server, untick a tool it must never call, and add the help center.",
      minutes: 2,
      short: "Attach the Northwind server and untick a tool the agent must never call.",
      kind: "walkthrough",
      agent: function (made) { return agentTarget(mine(made), "tools"); },
      start: function (made) { return mine(made) ? agentPage(agentTarget(mine(made), "tools")) : "/admin/agents"; },
      cards: [
        openAgentCard(mine),
        {
          anchor: null,
          skip: function (loc, doc) { return onAgentPage(loc, "tools", doc); },
          title: "Open Tools & knowledge",
          body: "This tab holds the agent's toolsets, knowledge bases and skills.",
          yourTurn: "open Tools & knowledge.",
          advance: { on: "page",
            match: function (loc, doc) { return onAgentPage(loc, "tools", doc); } }
        },
        {
          anchor: "tools-add-toolset",
          title: "Add the Northwind tools",
          body: "Tools come from MCP servers. Add Northwind tools, then untick " +
            "create_ticket so this agent cannot open tickets.",
          yourTurn: "add a toolset.",
          advance: { on: "click" }
        },
        {
          anchor: "tools-add-knowledge",
          title: "Add the help center",
          body: "A knowledge base gives the agent a search tool over its documents. " +
            "Add the Northwind help center.",
          yourTurn: "add a knowledge base.",
          advance: { on: "click" }
        },
        {
          anchor: "agent-save",
          title: "Save",
          body: "Saving writes the staging row. The next chat turn uses the new tools.",
          yourTurn: "save the changes.",
          advance: { on: "event", name: "tour:saved", mine: true, match: savedTools }
        }
      ],
      done: [
        { on: "event", name: "tour:saved", mine: true, match: savedTools }
      ],
      requires: ["A3"],
      requiresAny: [],
      doneWhen: "your agent has a toolset and a knowledge base saved",
      ai: {
        prompt: "Attach the Northwind tools and the help center to my agent {AGENT}, without create_ticket",
        tools: ["profile_attachments", "update_profile"],
        // Both attached in the turn: an MCP server and a knowledge base.
        actions: [["profile_attachments:add:mcp", "profile_attachments:add:kb"]]
      },
      code: toolCode("Agents › your agent › Tools & knowledge", [
        { tool: "mcp_servers", args: { action: "list" } },
        { tool: "profile_attachments", args: { profile_id: "<profile_id>", kind: "mcp",
          action: "add", ids: ["<mcp_server_id>"],
          tool_subset: ["check_outage", "get_order_status", "list_plans"] } },
        { tool: "profile_attachments", args: { profile_id: "<profile_id>", kind: "kb",
          action: "add", ids: ["<source_id>"] } }
      ], 1),
      records: null
    },
    {
      id: "A5",
      track: "build",
      title: "Add a specialist sub-agent",
      summary: "Let your agent hand work to a specialist, as the Support desk does with the Billing helper.",
      minutes: 1,
      short: "Give your agent a specialist to hand work to.",
      kind: "walkthrough",
      agent: function (made) { return agentTarget(mine(made), "subagents"); },
      start: function (made) { return mine(made) ? agentPage(agentTarget(mine(made), "subagents")) : "/admin/agents"; },
      cards: [
        openAgentCard(mine),
        {
          anchor: null,
          skip: function (loc, doc) { return onAgentPage(loc, "subagents", doc); },
          title: "Open Sub-agents",
          body: "Sub-agents are other agents this one may hand work to.",
          yourTurn: "open Sub-agents.",
          advance: { on: "page",
            match: function (loc, doc) { return onAgentPage(loc, "subagents", doc); } }
        },
        {
          anchor: "subagents-add",
          title: "Hand work to a specialist",
          body: "Add a sub-agent, such as the Billing helper. The parent gets one " +
            "ask_<slug> tool for each sub-agent.",
          yourTurn: "add a sub-agent.",
          advance: { on: "click" }
        },
        {
          anchor: "agent-save",
          title: "Save",
          body: "Saving writes the staging row. The hand-off tool is there on the next turn.",
          yourTurn: "save the changes.",
          advance: { on: "event", name: "tour:saved", mine: true, grew: "subagents", match: savedSubagents }
        }
      ],
      // A save on the visitor's own agent that added a sub-agent (CORE compares
      // the count with the one the tab loaded with: `grew`).
      done: [
        { on: "event", name: "tour:saved", mine: true, grew: "subagents", match: savedSubagents }
      ],
      requires: ["A3"],
      requiresAny: [],
      doneWhen: "you add a sub-agent to your agent and save",
      ai: {
        prompt: "Add a refund specialist as a sub-agent of my agent {AGENT}",
        tools: ["templates", "fork_profile", "update_profile", "profile_attachments"],
        actions: ["profile_attachments:add:downstream", "templates:use:downstream"]
      },
      code: toolCode("Agents › your agent › Sub-agents", [
        { note: "make the specialist and add it under its parent",
          tool: "templates", args: { action: "use", template_id: "<template_id>",
            display_name: "Refund specialist", as_subagent_of: "<profile_id>" } },
        { note: "or add an existing agent as a sub-agent",
          tool: "profile_attachments", args: { profile_id: "<profile_id>",
            kind: "downstream", action: "add", ids: ["<sub_agent_id>"] } }
      ]),
      records: null
    },

    // ----------------------------- Prove -----------------------------
    {
      id: "B1",
      track: "prove",
      title: "Open the Support desk suite",
      summary: "See the five cases the Support desk is graded on.",
      minutes: 1,
      short: "See the five cases the Support desk is graded on.",
      kind: "walkthrough",
      start: function () { return "/admin/evals/suites"; },
      cards: [
        {
          anchor: "suite-row",
          pick: { attr: "data-tour-name", value: SUITE },
          skip: function (loc) { return isSuitePage(loc); },
          title: "Open the Support desk suite",
          body: "A suite is a list of cases. Each has a question and what the answer must " +
            "contain, call or meet.",
          yourTurn: "open \"" + SUITE + "\".",
          advance: { on: "page", match: function (loc) { return isSuitePage(loc); } }
        },
        {
          anchor: null,
          title: "Cases and a target score",
          body: "The outage case expects a check_outage call. A judge model grades each " +
            "answer against what the case expects.",
          advance: { on: "next" }
        }
      ],
      done: [
        { on: "page", match: function (loc) { return isSuitePage(loc); } }
      ],
      requires: [],
      requiresAny: [],
      doneWhen: "you open a test suite",
      ai: {
        prompt: "Show me the Support desk test suite",
        tools: ["eval_suites"],
        actions: ["eval_suites:get"]
      },
      code: toolCode("Testing › Test suites › " + SUITE, [
        { tool: "eval_suites", args: { action: "list" } },
        { tool: "eval_suites", args: { action: "get", suite_id: "<suite_id>" } }
      ], 1),
      records: null
    },
    {
      id: "B2",
      track: "prove",
      title: "Run it and read the score",
      summary: "Run the suite against the agent and read its pass rate.",
      minutes: 2,
      short: "Run the suite and read the pass rate.",
      kind: "walkthrough",
      start: function () { return "/admin/evals/suites"; },
      cards: [
        {
          anchor: "suite-row",
          pick: { attr: "data-tour-name", value: SUITE },
          skip: function (loc) { return isSuitePage(loc); },
          title: "Open a suite",
          body: "Open the Support desk suite, or one you made.",
          yourTurn: "open a suite.",
          advance: { on: "page", match: function (loc) { return isSuitePage(loc); } }
        },
        {
          anchor: "run-tests",
          title: "Run this suite",
          body: "Every case is sent to the agent, then graded. A run takes up to two minutes.",
          yourTurn: "click Run this suite.",
          advance: { on: "click" }
        },
        {
          anchor: "run-start",
          title: "Start the run",
          body: "You can pick another model or repeat each case. The defaults are fine.",
          yourTurn: "click Start run.",
          advance: { on: "event", name: "tour:run-started" }
        },
        {
          anchor: null,
          title: "Running, about a minute",
          body: "You can move on. The step ticks when the run finishes, even on another page.",
          advance: { on: "event", name: "tour:run-finished", mine: true }
        }
      ],
      done: [
        { on: "event", name: "tour:run-finished", mine: true },
        { on: "ai", actions: ["trigger_eval_run"] }
      ],
      requires: [],
      requiresAny: [],
      doneWhen: "a run you started in this browser finishes",
      ai: {
        prompt: "Test the Support desk",
        tools: ["trigger_eval_run", "wait_for_test_run"],
        actions: ["trigger_eval_run"]
      },
      code: (function () {
        var c = toolCode("Testing › Test suites › " + SUITE + " › Run this suite", [
          { tool: "trigger_eval_run", args: { profile_id: "<profile_id>",
            suite_id: "<suite_id>" }, as: "run" },
          { tool: "eval_runs", args: { action: "get", run_id: "<run_id>" } }
        ]);
        c.ci = CI_YAML;
        return c;
      })(),
      records: "run",
      recordFrom: function (loc, doc, detail) { return (detail && detail.run_id) || ""; }
    },
    {
      id: "B3",
      track: "prove",
      title: "Find out why a case failed",
      summary: "Open a failed case and read what the agent said and why it failed.",
      minutes: 1,
      short: "Read the transcript and the judge's reasoning.",
      kind: "walkthrough",
      start: function (made) {
        var runs = (made && made.runs) || [];
        return runs.length
          ? "/admin/evals/runs/" + encodeURIComponent(runs[runs.length - 1]) + "?show=failed"
          : "/admin/evals/runs";
      },
      cards: [
        {
          anchor: null,
          skip: function (loc) { return isRunPage(loc); },
          title: "Open a run",
          body: "Runs lists every test run, newest first. Open one with failed cases.",
          yourTurn: "open a run.",
          advance: { on: "page", match: function (loc) { return isRunPage(loc); } }
        },
        {
          anchor: "run-cases",
          title: "Open a failed case",
          body: "Open the failed case to see the transcript, the tool calls and the " +
            "judge's reasoning. Failed cases are listed first.",
          yourTurn: "open a failed case.",
          advance: { on: "open", anchor: "case-row",
            attr: { name: "data-outcome", values: ["failed", "flaky"] } }
        }
      ],
      done: [
        { on: "open", anchor: "case-row",
          attr: { name: "data-outcome", values: ["failed", "flaky"] } }
      ],
      requires: [],
      requiresAny: [],
      doneWhen: "you open a failed case",
      ai: {
        prompt: "Why did the last test fail?",
        tools: ["eval_runs"],
        actions: ["eval_runs:get"]
      },
      code: toolCode("Testing › Runs › a run › Failed only", [
        { tool: "eval_runs", args: { action: "list", suite_id: "<suite_id>", limit: 1 } },
        { tool: "eval_runs", args: { action: "get", run_id: "<run_id>",
          include_results: true } }
      ], 1),
      records: null
    },
    {
      id: "B4",
      track: "prove",
      title: "Compare two runs",
      summary: "Line two runs up case by case: fixed, broke or still failing.",
      minutes: 1,
      short: "Line up two runs: fixed, broke, still failing.",
      kind: "walkthrough",
      start: function () { return "/admin/evals/runs"; },
      cards: [
        {
          anchor: "runs-pick",
          title: "Tick two runs",
          body: "Pick two runs of the same suite, for example before and after a change.",
          yourTurn: "tick two runs.",
          advance: { on: "click" }
        },
        {
          anchor: "runs-compare",
          title: "Compare them",
          body: "Each case is marked fixed, broke or still failing, with the score change.",
          yourTurn: "click Compare selected.",
          advance: { on: "page", match: function (loc) { return isComparePage(loc); } }
        }
      ],
      done: [
        { on: "page", match: function (loc) { return isComparePage(loc); } }
      ],
      requires: [],
      requiresAny: [],
      doneWhen: "you open the compare view",
      ai: {
        prompt: "Compare the last two Support desk runs",
        tools: ["eval_runs"],
        actions: ["eval_runs:compare"]
      },
      code: toolCode("Testing › Runs › tick two › Compare selected", [
        { tool: "eval_runs", args: { action: "compare",
          run_ids: ["<baseline_run_id>", "<latest_run_id>"] } }
      ]),
      records: null
    },

    // ---------------------------- Wire in ----------------------------
    {
      id: "C1",
      track: "wire",
      title: "Promote staging to production",
      summary: "See what promoting your agent from staging to production would change.",
      minutes: 1,
      short: "See what a promote would change in production.",
      kind: "walkthrough",
      agent: function (made) { return agentTarget(mine(made), ""); },
      start: function (made) { return mine(made) ? agentPage(agentTarget(mine(made), "")) : "/admin/agents"; },
      cards: [
        openAgentCard(mine),
        {
          anchor: "agent-more",
          title: "Open More",
          body: "Promote lives in the More menu, behind a confirm dialog.",
          yourTurn: "click More.",
          advance: { on: "click" }
        },
        {
          anchor: "agent-promote",
          title: "Promote to production",
          body: "The dialog lists what changes in production: model, instructions, tools " +
            "and sub-agents. Promoting is optional here.",
          yourTurn: "open the promote dialog.",
          advance: { on: "click" }
        }
      ],
      done: [
        // On the visitor's own agent only.
        { on: "click", anchor: "agent-promote", mine: true },
        { on: "ai", actions: ["promote_profile"] }
      ],
      requires: ["A3"],
      requiresAny: [],
      doneWhen: "the promote dialog opens. Promoting is optional",
      ai: {
        prompt: "Promote my agent {AGENT} to production",
        tools: ["promote_profile"],
        actions: ["promote_profile"]
      },
      code: toolCode("Agents › your agent › More › Promote to Production", [
        { note: "dry run: lists what would change", tool: "promote_profile",
          args: { profile_id: "<profile_id>", target_env: "prod" } },
        { note: "write it", tool: "promote_profile",
          args: { profile_id: "<profile_id>", target_env: "prod", confirm: true } }
      ]),
      records: null
    },
    {
      id: "C2",
      track: "wire",
      title: "Call it over the chat API",
      summary: "Stream one turn from POST /v1/agents/{slug}/chat, then continue it.",
      minutes: 1,
      short: "Stream one turn from the chat API.",
      kind: "code",
      start: function () { return null; },
      cards: [
        {
          anchor: null,
          title: "Call the Support desk from your code",
          body: "A published agent answers at POST /v1/agents/{slug}/chat, streamed as " +
            "server-sent events."
        }
      ],
      done: [
        { on: "copy" },
        { on: "event", name: "tour:test-chat-sent",
          match: function (detail, loc) { return param(loc, "tour") === "C2"; } }
      ],
      requires: [],
      requiresAny: [],
      doneWhen: "you copy a snippet or open Test chat",
      code: { portal: "Agents › Support desk › Test", curl: CHAT_CURL, python: CHAT_PY },
      codeCard: {
        tabs: [
          { id: "curl", label: "curl", code: CHAT_CURL },
          { id: "python", label: "Python (httpx)", code: CHAT_PY }
        ],
        events: SSE_EVENTS,
        notesTitle: "Stream, in order",
        notes: SSE_EVENTS.map(function (e) { return e.name + ": " + e.text; }).concat([
          "thought and tool_activity can arrive while it works, error at any point."
        ]),
        action: {
          label: "Try it in Test chat instead",
          href: "/admin/agents",
          // The Support desk's page with its Test chat open, as a Promise.
          resolve: function () {
            return agentPage(agentTarget(SUPPORT, "", "test=1&tour=C2"));
          }
        }
      },
      records: null
    },
    {
      id: "C3",
      track: "wire",
      title: "Use it as an OpenAI model",
      summary: "Point an OpenAI SDK at /v1 and use the agent's slug as the model.",
      minutes: 1,
      short: "Point an OpenAI SDK at the engine.",
      kind: "code",
      start: function () { return null; },
      cards: [
        {
          anchor: null,
          title: "Use it as an OpenAI model",
          body: "Any OpenAI SDK can call a published agent as a model. The engine serves " +
            "GET /v1/models and POST /v1/chat/completions."
        }
      ],
      done: [{ on: "copy" }],
      requires: [],
      requiresAny: [],
      doneWhen: "you copy a snippet",
      code: { portal: "Agents › an agent › Settings › Status: Active", curl: OPENAI_CURL,
        python: OPENAI_PY },
      codeCard: {
        tabs: [
          { id: "python", label: "OpenAI SDK, Python", code: OPENAI_PY },
          { id: "js", label: "OpenAI SDK, JS", code: OPENAI_JS },
          { id: "curl", label: "curl", code: OPENAI_CURL }
        ],
        notes: [
          "The client keeps the conversation and sends all of it every time. System " +
            "messages are dropped; the agent's own instructions apply.",
          "Tools run inside the engine. The client gets the answer text.",
          "Only published agents answer. GET /v1/models lists them."
        ],
        notesTitle: "Good to know"
      },
      records: null
    },
    {
      id: "C4",
      track: "wire",
      title: "Drive the engine from your assistant",
      summary: "Connect Claude Code or any MCP client to the admin MCP server at /mcp.",
      minutes: 1,
      short: "Connect your assistant to the admin MCP server.",
      kind: "code",
      start: function () { return null; },
      cards: [
        {
          anchor: null,
          title: "Drive the engine from your assistant",
          body: "Your assistant can build and test agents over /mcp, with the tools the " +
            "Builder assistant uses here."
        }
      ],
      done: [{ on: "copy" }],
      requires: [],
      requiresAny: [],
      doneWhen: "you copy a snippet",
      code: { portal: "Settings › API keys › Full access", mcp: MCP_JSON },
      codeCard: {
        tabs: [
          { id: "mcp-json", label: ".mcp.json", code: MCP_JSON },
          { id: "plugin", label: "Claude Code plugin", code: PLUGIN }
        ],
        notes: [
          "The MCP server takes a key with Full access. A Chat only key is refused there.",
          "The plugin adds four skills: build-agent, test-agent, improve-instructions and " +
            "compare-models.",
          "In this demo /mcp is off. The Builder assistant calls the same tools in process."
        ],
        notesTitle: "Good to know"
      },
      records: null
    },

    // ---------------------------- Operate ----------------------------
    {
      id: "D1",
      track: "operate",
      title: "Read the Overview",
      summary: "Health and activity across every agent, on one page.",
      minutes: 1,
      short: "Health and activity across every agent.",
      kind: "walkthrough",
      start: function () { return "/admin/live"; },
      cards: [
        {
          anchor: "overview-kpis",
          title: "Read the Overview",
          body: "Conversations, those that need attention, hand-offs, failed actions and " +
            "response time, for the period you pick.",
          advance: { on: "next" }
        }
      ],
      done: [
        { on: "page", match: function (loc) { return path(loc) === "/admin/live"; } }
      ],
      requires: [],
      requiresAny: [],
      doneWhen: "you open the Overview",
      records: null
    },
    {
      id: "D2",
      track: "operate",
      title: "Review a conversation that needs attention",
      summary: "Open a conversation the Overview flagged and read what went wrong.",
      minutes: 1,
      short: "Open a flagged conversation.",
      kind: "walkthrough",
      start: function () { return "/admin/live"; },
      cards: [
        {
          anchor: "needs-attention",
          title: "Review a conversation that needs attention",
          body: "Failed actions and errors, grouped by cause. Open latest shows that " +
            "conversation step by step.",
          yourTurn: "open a flagged conversation.",
          advance: { on: "page", match: function (loc) { return isOpenConversation(loc); } }
        }
      ],
      // Open latest inside Needs attention, confirmed by the very next page; or
      // the attention filter with a conversation open.
      done: [
        { on: "click-then-page", anchor: "needs-attention",
          match: function (loc) { return isOpenConversation(loc); } },
        { on: "page", match: function (loc) { return isFlaggedConversation(loc); } }
      ],
      requires: [],
      requiresAny: [],
      doneWhen: "you open a flagged conversation",
      // Overview lists "Needs attention" only with a tracing store and some
      // traffic; the hosted demo has none. CORE swaps these fields in when the
      // list is not on Overview (or the server says tracing is off).
      alt: {
        title: "Review a conversation",
        summary: "Open a conversation and read it turn by turn, with the tool calls and the reply.",
        short: "Open a conversation and read it turn by turn.",
        start: function () { return "/admin/observability"; },
        cards: [
          {
            anchor: "conversation-list",
            title: "Review a conversation",
            body: "Every conversation with every agent, newest first, including your test chats. " +
              "Open one to read it turn by turn.",
            yourTurn: "open a conversation.",
            // Only when the list is empty.
            note: function (loc, doc) {
              return doc && doc.querySelector && doc.querySelector('[data-tour="conversation-row"]') ? null
                : "Empty list? Talk to the Support desk in Test chat first.";
            },
            advance: { on: "click", anchor: "conversation-row" }
          }
        ],
        done: [
          { on: "click", anchor: "conversation-row" },
          { on: "page", match: function (loc) { return isOpenConversation(loc); } }
        ],
        doneWhen: "you open any conversation"
      },
      records: null
    },
    {
      id: "D3",
      track: "operate",
      title: "See your changes in Activity",
      summary: "Every change to an agent, a suite or a tool, with who made it and when.",
      minutes: 1,
      short: "Who changed what, and when.",
      kind: "walkthrough",
      start: function () { return "/admin/audit"; },
      cards: [
        {
          anchor: "audit-list",
          title: "See your changes in Activity",
          body: "Changes from the portal, the API and the Builder assistant all land here. " +
            "Open one to see what changed.",
          advance: { on: "next" }
        }
      ],
      done: [
        { on: "page", match: function (loc) { return path(loc) === "/admin/audit"; } }
      ],
      requires: [],
      requiresAny: A_AND_B.slice(),
      doneWhen: "you open Activity after a Build or Prove step",
      records: null
    },
    {
      id: "D4",
      track: "operate",
      title: "Jump anywhere with Ctrl K",
      summary: "Search agents, suites, runs and conversations from any page.",
      minutes: 1,
      short: "Search from any page.",
      kind: "walkthrough",
      start: function () { return null; },
      cards: [
        {
          anchor: "search-trigger",
          title: "Jump anywhere with Ctrl K",
          body: "Search finds screens, agents, suites, runs and conversations. Type > for " +
            "quick actions, such as Resume tour.",
          yourTurn: "press Ctrl K, then open a result.",
          advance: { on: "event", name: "tour:palette-used" }
        }
      ],
      done: [
        { on: "event", name: "tour:palette-used" }
      ],
      requires: [],
      requiresAny: [],
      doneWhen: "you use the palette once",
      records: null
    }
  ];

  // --- page matchers used above --------------------------------------

  function isSuitePage(loc) {
    return /^\/admin\/evals\/suites\/[0-9a-f-]{36}$/i.test(path(loc));
  }

  function isRunPage(loc) {
    return /^\/admin\/evals\/runs\/[^/]+$/.test(path(loc));
  }

  function isComparePage(loc) {
    return path(loc) === "/admin/evals/compare";
  }

  // A conversation open in the explorer filtered to Needs attention.
  function isFlaggedConversation(loc) {
    return isOpenConversation(loc) && param(loc, "outcome") === "attention";
  }

  function savedTools(detail) {
    return !!detail && detail.kind === "tools" && detail.toolsets > 0 && detail.knowledge > 0;
  }

  function savedSubagents(detail) {
    return !!detail && detail.kind === "subagents" && detail.subagents > 0;
  }

  // Any conversation open in the explorer (D2 without Needs attention).
  function isOpenConversation(loc) {
    return path(loc) === "/admin/observability" && String(loc.hash || "").indexOf("#trace=") === 0;
  }

  // ---------------------------------------------------------------------
  // Tracks and fixed copy
  // ---------------------------------------------------------------------

  var REPO_DOCS = "{REPO}/blob/main/docs/";

  window.DEMO_TOUR_CATALOG = {
    version: 1,
    tracks: [
      { id: "build", eyebrow: "Build", letter: "A", title: "Build", goal: "Build an agent",
        blurb: "Instructions, model, MCP tools, knowledge and sub-agents. All configuration, " +
          "no redeploy.",
        meta: "5 steps · 6 min", minutes: 6 },
      { id: "prove", eyebrow: "Prove it works", letter: "B", title: "Prove", goal: "Prove it works",
        blurb: "Test suites scored by a judge model, failing cases, and run-against-run " +
          "comparison.",
        meta: "4 steps · 5 min", minutes: 5 },
      { id: "wire", eyebrow: "Wire it into my stack", letter: "C", title: "Wire in", goal: "Wire it into my stack",
        blurb: "Streaming chat API, the OpenAI-compatible endpoint, the admin MCP server, " +
          "staging to production.",
        meta: "4 steps · 4 min", minutes: 4 },
      { id: "operate", eyebrow: "Operate it", letter: "D", title: "Operate", goal: "Operate it",
        blurb: "Overview, conversations that need attention, the audit log and Ctrl K.",
        meta: "4 steps · 3 min", minutes: 3 }
    ],
    steps: steps,
    // The Builder assistant's tools: demo_mode/assistant.py ALLOWED_TOOLS plus
    // its own wait_for_test_run. Every ai.tools name is one of these.
    assistantTools: [
      "list_profiles", "get_profile", "update_profile", "fork_profile", "templates",
      "prompts", "profile_attachments", "mentionables", "agent_types", "kb_sources",
      "mcp_servers", "models", "eval_suites", "trigger_eval_run", "eval_runs", "chat",
      "search", "promote_profile", "wait_for_test_run"
    ],
    copy: {
      welcome: {
        eyebrow: "Public demo · Northwind Internet",
        title: "What do you want to see first?",
        body: "A guided tour of the engine on four sample agents. Every step shows the click " +
          "in the portal and the MCP or HTTP call behind it. You can switch tracks at any time.",
        goalLabel: "Pick a goal",
        langLabel: "Show calls as",
        langs: { mcp: "MCP tool", curl: "curl", python: "Python" },
        aiLabel: "Offer “Let AI do it” on each step",
        aiHint: "The Builder assistant does the step with the engine's admin tools.",
        start: "Start: {goal}",
        explore: "Explore on my own",
        footer: "Progress is kept in this browser and clears with the nightly reset.",
        publicNote: PUBLIC_NOTE
      },
      checklist: {
        title: "Getting started",
        progress: "{n} of {total}",
        yourTrack: "Your track",
        change: "Change",
        hideDone: "Hide done",
        showDone: "Show done",
        showMe: "Show me",
        letAi: "Let AI do it",
        minutes: "{n} min",
        finishLine: "Finish line: run your own copy with",
        finishCode: "docker compose up",
        viaWalkthrough: "via walkthrough",
        viaAi: "via AI",
        viaHand: "by hand",
        upNext: "Up next",
        close: "Close"
      },
      coach: {
        stepOf: "{track} · {n} of {total}",
        announce: "Step {n} of {total}: {title}",
        yourTurn: "Your turn:",
        next: "Next",
        back: "Back",
        skip: "Skip step",
        close: "Close",
        doneWhen: "Done when:",
        inCode: "Same step, in code",
        portalTab: "Portal",
        takeMeThere: "Take me there",
        elsewhere: "This step is on another page.",
        working: "The Builder assistant is working on it.",
        aiMissed: "The assistant did not finish this step. Try Show me instead.",
        running: "Running, about a minute",
        openRun: "Open the run",
        stepDone: "Done",
        needs: "This step changes an agent of your own, so the shared agents stay the same " +
          "for everyone. Do “{title}” first.",
        needsGo: "Create an agent first"
      },
      codeCard: {
        events: "Stream, in order",
        copy: "Copy",
        copied: "Copied",
        keysOff: KEYS_OFF,
        tryTestChat: "Try it in Test chat instead",
        runOwn: "Run your own copy",
        next: "Next step"
      },
      ai: {
        strip: "Tour step:",
        back: "Back to tour"
      },
      toast: {
        done: "Step done via AI:",
        doneHand: "Step done:",
        detail: "{title} · {n} of {total} in {track}",
        next: "Next step",
        dismiss: "Dismiss"
      },
      finish: {
        title: "You've seen the whole engine",
        body: "Build, test, ship and operate, on agents that are only configuration. " +
          "Everything here resets tonight. Your own copy keeps it.",
        stats: {
          done: "Done", doneSub: "all four tracks", steps: "steps", viaAi: "done via AI",
          agents: "agents you built", runs: "test runs"
        },
        ways: [
          { title: "Run it on your machine",
            code: "git clone {REPO}\ncd ai-engine && docker compose up", codeLabel: "Shell",
            body: "Only Docker needed. Open localhost:8000 and enter the setup code from the logs.",
            href: REPO_DOCS + "quick-start.md", label: "Quick start" },
          { title: "Deploy it",
            links: [
              { label: "Deploy to Render", href: "https://render.com/deploy?repo={REPO}" },
              { label: "Railway", href: REPO_DOCS + "deploy/railway.md" },
              { label: "Coolify", href: REPO_DOCS + "deploy/coolify.md" }
            ],
            body: "One-click Blueprint, or the image at ghcr.io/waj-1/ai-engine (amd64, arm64).",
            href: "https://render.com/deploy?repo={REPO}", label: "Deploy to Render" },
          { title: "Build from Claude Code",
            code: "/plugin marketplace add waj-1/ai-engine\n" +
              "/plugin install waj-ai-engine@waj-ai-engine",
            body: "Skills that build, test and improve agents for you.",
            href: "{REPO}/blob/main/plugin/README.md", label: "Plugin guide" }
        ],
        deployLinks: [
          { label: "Railway", href: REPO_DOCS + "deploy/railway.md" },
          { label: "Coolify", href: REPO_DOCS + "deploy/coolify.md" },
          { label: "Render", href: REPO_DOCS + "deploy/render.md" },
          { label: "Docker Compose", href: REPO_DOCS + "deploy/compose.md" }
        ],
        star: "Star on GitHub",
        starHref: "{REPO}",
        docs: "Read the docs",
        docsHref: REPO_DOCS + "quick-start.md",
        compare: "Compare with Dify, Flowise, Langflow",
        compareHref: REPO_DOCS + "compare/README.md",
        close: "Keep exploring"
      },
      resume: {
        title: "Pick up the tour",
        progress: "{n} of {total}",
        next: "Next:",
        body: "{title} · {minutes} min. {short}",
        letAi: "Let AI do it",
        "continue": "Continue",
        open: "Open checklist",
        hide: "Hide"
      },
      entry: { label: "Getting started" },
      limit: LIMIT,
      limitRate: LIMIT_RATE,
      publicNote: PUBLIC_NOTE
    }
  };
})();
