# Use an agent from any OpenAI client

The engine answers the OpenAI chat API under `/v1`, so a program written for
OpenAI can talk to one of your agents by changing three settings:

| Setting | Value |
|---|---|
| Base URL | `http://localhost:8000/v1` (your engine's address, plus `/v1`) |
| API key | An engine API key (`wae_...`), from first-run setup or **Settings › API keys**. **Chat only** access is enough. |
| Model | The agent's slug, for example `demo` or `support-desk` |

Two routes are served:

| Route | What it does |
|---|---|
| `GET /v1/models` | Lists the published agents. Each one's `id` is its slug. |
| `POST /v1/chat/completions` | Runs one turn of the agent named by `model`, streaming or not. |

Both follow the same rules as `POST /v1/agents/{slug}/chat`: they need
`ENABLE_GENERIC_AGENTS=1` (the Docker Compose setup turns it on), they take the
same API keys, only published agents answer, and each key sees only its own
conversations.

What we tested: the OpenAI Python SDK against a Docker Compose install, both
streaming and non-streaming. The JavaScript snippet, Open WebUI and LibreChat
sections below are written from those projects' documented settings; we have
not run them ourselves yet.

## Try it

```bash
export AI_ENGINE_API_KEY=wae_...

curl http://localhost:8000/v1/models -H "Authorization: Bearer $AI_ENGINE_API_KEY"

curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $AI_ENGINE_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "demo", "messages": [{"role": "user", "content": "What can you help me with?"}]}'
```

## OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="wae_...")

# One answer
reply = client.chat.completions.create(
    model="demo",
    messages=[{"role": "user", "content": "What can you help me with?"}],
)
print(reply.choices[0].message.content)

# Streaming
stream = client.chat.completions.create(
    model="demo",
    messages=[{"role": "user", "content": "What can you help me with?"}],
    stream=True,
    stream_options={"include_usage": True},
)
for chunk in stream:
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

## OpenAI JavaScript SDK

```js
import OpenAI from "openai";

const client = new OpenAI({ baseURL: "http://localhost:8000/v1", apiKey: "wae_..." });

const stream = await client.chat.completions.create({
  model: "demo",
  messages: [{ role: "user", content: "What can you help me with?" }],
  stream: true,
});
for await (const chunk of stream) {
  process.stdout.write(chunk.choices[0]?.delta?.content ?? "");
}
```

## Open WebUI

Open WebUI can use any OpenAI-compatible server as a connection. Add one with
the base URL and key from the table above, either in its admin settings (the
connections section, under the OpenAI API) or with its environment variables:

```bash
OPENAI_API_BASE_URL=http://host.docker.internal:8000/v1
OPENAI_API_KEY=wae_...
```

Your published agents then appear in the model picker under their slugs.

- If Open WebUI runs in Docker, `localhost` inside its container is not your
  machine. Use an address the container can reach, such as
  `host.docker.internal`, and note that the Compose setup publishes the engine
  on `127.0.0.1` only (see [the Compose guide](deploy/compose.md)).
- Open WebUI also sends background requests to a model to write chat titles,
  tags and follow-up suggestions. If they go to your agent, each one is a real
  agent turn: it costs tokens and shows up in Conversations. Point those tasks
  at a plain model instead (Open WebUI has a task-model setting for this).
- We have not checked the exact labels in Open WebUI's screens; they change
  between versions. Its own documentation for OpenAI-compatible connections is
  the reference.

## LibreChat

LibreChat takes OpenAI-compatible servers as custom endpoints in
`librechat.yaml`:

```yaml
endpoints:
  custom:
    - name: "AI Engine"
      apiKey: "${AI_ENGINE_API_KEY}"
      baseURL: "http://host.docker.internal:8000/v1"
      models:
        default: ["demo"]
        fetch: true
      titleConvo: false
```

`fetch: true` reads the agent list from `/v1/models`. `titleConvo: false` stops
LibreChat from asking the agent to write a title for every chat; turn it on
only if you want that extra agent turn. The same note about `localhost` inside
a container applies. We have not run this configuration ourselves; LibreChat's
documentation for custom endpoints is the reference for the file's fields.

## How conversations work

OpenAI clients keep the conversation themselves and send all of it with every
request. The engine's own chat API works the other way round: you send one
message and it keeps the history. On the OpenAI routes, **the client's
`messages` are the conversation**:

- The last message must be a `user` message. It is the turn the agent answers.
- The `user` and `assistant` messages before it are the history the agent sees
  for that turn. The engine does not add its stored copy on top, so nothing is
  repeated and nothing is missing.
- If the user edits an earlier message, or regenerates an answer, the agent
  sees exactly what the client sends: the edited conversation, not the old one.

Every turn is also saved in the engine, so it appears under **Conversations**
in the portal and in `GET /ai/sessions` for the key, like any other chat. One
client conversation is saved as one engine conversation:

- If the request has an `X-Session-Id` header, that names the conversation.
  Any text up to 512 characters works (a chat id from your app, for example).
  It only has meaning for the key that sent it: two keys using the same value
  get two separate conversations.
- Without the header, a conversation is recognised by its first user message
  (and the `user` field, if the client sends one).

The response carries the engine's id for that conversation in the
`X-Engine-Session-Id` header.

### Limits to know about

- **Two chats that start with the same first message are saved as one
  conversation** when no `X-Session-Id` is sent (two chats that both open with
  "Hi", for instance). The agent's answers are not affected, because it reads
  the history the client sends; only the saved record mixes the two. Send
  `X-Session-Id` to keep them apart.
- **Editing the first message starts a new saved conversation** when no
  `X-Session-Id` is sent, for the same reason.
- **The saved record is a log of turns, not a copy of the client's screen.**
  A regenerated answer is saved as one more question and answer. An edited
  message is saved as a new turn; the old one stays in the record.
- **Agent memory follows the saved record.** If an agent has memory turned on,
  what it remembers about earlier chats, and any summary of a long
  conversation, is built from the saved turns. After an edit or a regenerate,
  that can include the replaced turn.
- **Long histories are trimmed** to the agent's conversation window (its most
  recent messages), the same as on the engine's own chat API.
- **The last user message can be at most 8,000 characters**, the same limit as
  the engine's own chat API. A longer one is refused with a 400.
- **Tools run inside the engine.** The agent's tool calls and their results are
  not sent to the client; the client receives the answer text.

## What is ignored

The request is accepted and these parts of it are not used:

- **`system` and `developer` messages.** An agent's instructions are set by
  whoever runs it, in the portal. A caller holding a chat key cannot replace
  or add to them through the API, so these messages are dropped, not merged.
  To change how an agent behaves, edit its instructions.
- **`tool` messages and `tool_calls`** in the history, and the `tools`,
  `tool_choice`, `functions` and `function_call` parameters. Attach tools to
  the agent in the portal instead.
- **Images, audio and files** in a message. Only the text parts are read.
- **Sampling and output parameters**: `temperature`, `top_p`, `max_tokens`,
  `max_completion_tokens`, `stop`, `seed`, `presence_penalty`,
  `frequency_penalty`, `logit_bias`, `logprobs`, `response_format`,
  `reasoning_effort`. The agent's model settings decide these.
- **`n`**: there is always one choice.
- Any other field the engine does not know.

`usage` reports the tokens of the whole agent turn, including the model calls
it made around its tools, so it can be larger than the visible text suggests.

## Errors

Errors use OpenAI's shape, `{"error": {"message", "type", "param", "code"}}`:

| Status | When |
|---|---|
| 400 | The body is not valid JSON, `model` or `messages` is missing, the last message is not a `user` message with text, or it is too long |
| 401 | No API key, or one that is wrong, revoked or expired (`invalid_api_key`) |
| 404 | No published agent has that slug (`model_not_found`), or the agent API is turned off |
| 429 | Too many requests from one address. This answer comes from the engine's rate limiter and its body is `{"error": "..."}`, a plain string |
| 500 | The turn failed part-way |
| 503 | The agent could not be loaded, or the install has no usable model yet (`model_unavailable`) |

In a stream the status is already 200 when a turn fails, so the failure arrives
as a `data: {"error": {...}}` frame followed by `data: [DONE]`.

The OpenAI SDKs retry 5xx answers by default. A retried request is a new turn
for the agent; set `max_retries=0` on the client if you do not want that.
