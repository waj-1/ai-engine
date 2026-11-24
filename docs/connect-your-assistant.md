# Connect your AI assistant

The engine has its own admin MCP server at `/mcp`. Connect Claude, Cursor or
any MCP client and ask it to create an agent, write its instructions, attach
tools and run the tests. These are the same steps as in the portal.

## Create a key

The MCP server takes an API key with **Full access**. A **Chat only** key is
refused there. Create one under **Settings › API keys**. See
[Create and revoke API keys](partner/08-team-and-settings/04-api-keys.md).

## Add the server to your client

```json
{
  "mcpServers": {
    "waj-ai-engine": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer wae_..." }
    }
  }
}
```

## Claude Code plugin

In Claude Code, the [plugin](../plugin/README.md) in this repository sets the
server up and adds skills that build an agent, write and run its tests, improve
its instructions and compare models:

```
/plugin marketplace add waj-1/ai-engine
/plugin install waj-ai-engine@waj-ai-engine
```

## Build an agent over MCP

Building an agent this way is a handful of calls:

| Step | MCP tool |
|---|---|
| Register a model | `models(action="create", …)` |
| Connect your systems | `mcp_servers(action="create", source="external", endpoint=…)` |
| Create the agent | `templates(action="use", template_id=…, display_name=…)` |
| Give it a model, publish it, build a team | `update_profile(…)` |
| Write its instructions | `prompts(action="create_version", profile_id=…, content=…)` |
| Choose its tools | `profile_mcp_attachments(action="set", …)` |
| Group agents into categories | `agent_types(action="create", display_name=…)`, then `update_profile(profile_id=…, agent_type="support")` |
| Store a model key | `provider_keys(action="set", provider=…, value=…)` |

## Test over MCP

| Task | MCP tool |
|---|---|
| Write test suites | `eval_suites` |
| Run a suite against any agent, draft or published | `trigger_eval_run`, `eval_runs` |
| Test a draft of the instructions without serving it | `trigger_eval_run` with `prompt_overrides` |
| Run the same suite on another model | `trigger_eval_run` with `model_id`, `bakeoffs` |
| Compare two runs case by case | `eval_runs` |

## More

- What you can ask the assistant to do:
  [Manage agents from an AI assistant](partner/09-reference/03-ai-assistant.md)
- The full tool catalog: [admin-portal/06-mcp.md](admin-portal/06-mcp.md)
