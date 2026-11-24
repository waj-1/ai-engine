# WAJ AI Engine plugin

Lets your AI assistant build, test and ship agents on your own WAJ AI Engine.
It connects the engine's MCP server and adds four skills.

| Skill | Ask for it like this |
|---|---|
| `build-agent` | "Build a support agent that answers from our returns policy" |
| `test-agent` | "Write a test suite for the support agent and run it" |
| `improve-instructions` | "The agent is too wordy. Fix its instructions and prove it got better" |
| `compare-models` | "Would a cheaper model do as well on the support suite?" |

## Before you install

1. Start the engine (`docker compose up` in the repository root) and finish the
   first-run page.
2. Create an API key with **Full access** under **Settings › API keys**.
3. Export it where your assistant runs:

   ```bash
   export WAJ_AI_ENGINE_API_KEY=wae_...
   # only if the engine is not on this machine:
   export WAJ_AI_ENGINE_URL=https://engine.example.com
   ```

## Install in Claude Code

```
/plugin marketplace add waj-1/ai-engine
/plugin install waj-ai-engine@waj-ai-engine
```

## Other MCP clients

Cursor, and any client that speaks MCP over HTTP, can use the server without
the plugin. Add this to the client's MCP settings:

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

The skills are plain Markdown under [`skills/`](skills); copy them into your
client's rules or skills folder if it has one.

## Privacy

The plugin talks only to the engine address you set, which is your own
install. It sends nothing to WAJ or to anyone else, and it has no telemetry.
