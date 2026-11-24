# Voice

An agent can take voice calls at `WS /v1/agents/<slug>/voice`. Voice works
with Gemini Live or Azure Voice Live. The realtime model listens and speaks,
and the agent's own chat model answers, so both need their keys.

## Keys

The realtime model needs a key of its own. Put it in the `.env` file next to
`docker-compose.yml`, then run `docker compose up -d` again.

| Realtime model | Key |
|---|---|
| Gemini Live (seeded: **Agents › Models** lists the realtime rows) | `GOOGLE_API_KEY` |
| Azure Voice Live (add a model: provider `azure_voice_live`, extra settings `{"realtime": true, "endpoint": "https://<resource>.cognitiveservices.azure.com", "auth_env_var": "AZURE_VOICE_LIVE_API_KEY"}`) | the variable named in `auth_env_var` |

Without a realtime key the Voice page says which variable is missing instead
of offering the call, and the socket closes with the same sentence (code 4502).

## Turn voice on for an agent

1. Open the agent and go to **Settings › set up voice for this agent**.
2. Tick **Duplex voice enabled**.
3. Pick the **Realtime model**, a **Voice ID** and a **Language**.
4. Save.

**Call** on that page places a call from your browser. The microphone needs
`localhost` or HTTPS. The portal guide has every setting:
[Set up how your agent sounds on calls](partner/02-agents/05-voice.md).

## Callers that are not a browser

A caller that is not a browser sends an API key, as an `Authorization` header
or as the first frame:

```
{"type": "auth", "token": "wae_..."}
```

Audio frames are raw 16-bit PCM at the rates in the server's `ready` frame.
`voice/live/protocol.py` lists every frame.

## Limits and recording

- Calls are capped per agent per day: `VOICE_LIVE_DAILY_SECONDS`, default 3600
  seconds.
- Calls are recorded for the Conversations screen unless
  `VOICE_RECORDING_ENABLED=0`.
- Voice checks (**Testing › Voice checks**) speak the caller's lines with
  Azure Speech and need `AZURE_SPEECH_KEY`.

## Test without a realtime key

The call path can be tried in text with a simulated realtime provider. The
question arrives as if spoken, the agent's own model answers, and the spoken
answer comes back as text. Turn voice on for the agent and pick any realtime
model first.

```bash
VOICE_LIVE_SIM_ENABLED=1 docker compose up -d
docker compose exec -T engine sh deploy/compose/with_env.sh \
  python deploy/compose/voice_text_call.py support-desk "What are your office hours?"
```

This check does not exercise speech recognition or synthesis. Voice test
suites in *sim* mode use the same flag.

Leave `VOICE_LIVE_SIM_ENABLED` unset on a shared server. With it, a call from
the engine's own machine needs no key.
