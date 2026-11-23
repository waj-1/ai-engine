#!/usr/bin/env python3
"""Place a text-only voice call to an agent, with no realtime provider key.

    python deploy/compose/voice_text_call.py <agent-slug> "What can you help me with?"

It opens the agent's voice socket (`WS /v1/agents/<slug>/voice`) on the
simulated realtime provider, which stands in for the model that listens and
speaks: each message is delivered as if the caller had said it, and what the
agent would say comes back as text. Everything else is the real call path:
the gates, the agent's voice settings, the hand-off to the agent, its model
and tools, and the checks on what may be spoken.

The engine only honours the simulated provider from this machine and only
where `VOICE_LIVE_SIM_ENABLED=1`, so run this next to the engine (inside its
container on the compose stack). Without the flag the call is refused with
the reason, which for an install with no realtime key reads "Realtime model
key missing: …". The agent needs voice turned on and a realtime model picked.

`--token` sends an access token as the first frame (needed when the flag is
off, to see the refusal a real caller would get). Exit code 0 when every
message was answered.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys


async def call(url: str, messages: list[str], token: str | None, timeout: float) -> int:
    import aiohttp

    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(url, max_msg_size=0) as ws:
            if token:
                await ws.send_str(json.dumps({"type": "auth", "token": token}))
            pending = list(messages)
            answered = 0
            greeted = False
            while True:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
                except asyncio.TimeoutError:
                    print(f"[no reply within {timeout:.0f}s]", file=sys.stderr)
                    return 1
                if msg.type == aiohttp.WSMsgType.BINARY:
                    continue  # audio; the simulated provider sends a token frame
                if msg.type != aiohttp.WSMsgType.TEXT:
                    reason = getattr(msg, "extra", "") or ""
                    code = ws.close_code
                    if answered == len(messages) and messages:
                        return 0
                    print(f"[call closed: {code} {reason}]".rstrip(), file=sys.stderr)
                    return 1
                frame = json.loads(msg.data)
                kind = frame.get("type")
                if kind == "ready":
                    provider = frame.get("provider") or "the agent's realtime model"
                    print(f"[ready: {provider}]")
                    if not frame.get("greeting"):
                        greeted = True
                        if pending:
                            await ws.send_str(json.dumps({"type": "text", "text": pending.pop(0)}))
                elif kind == "transcript" and frame.get("final"):
                    who = "caller" if frame.get("role") == "user" else "agent"
                    note = " (holding line)" if frame.get("out_of_band") else ""
                    print(f"{who}{note}: {frame.get('text')}")
                elif kind == "error":
                    print(f"[error: {frame.get('message')}]", file=sys.stderr)
                elif kind == "turn_end":
                    if greeted:
                        answered += 1
                    greeted = True
                    if pending:
                        await ws.send_str(json.dumps({"type": "text", "text": pending.pop(0)}))
                    elif answered >= len(messages):
                        await ws.close()
                        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("slug", help="the agent's address name, as in /v1/agents/<slug>/chat")
    parser.add_argument("messages", nargs="+", help="what the caller says, one turn each")
    parser.add_argument("--port", default=os.getenv("PORT", "8000"))
    parser.add_argument("--token", default=None, help="access token, sent as the first frame")
    parser.add_argument("--profile-id", default=None, help="call this draft copy of the agent")
    parser.add_argument("--locale", default=None, help="language hint, e.g. ar or en-GB")
    parser.add_argument("--real", action="store_true",
                        help="do not ask for the simulated provider (shows what a real call gets)")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    query = [] if args.real else ["provider=simulated"]
    if args.profile_id:
        query.append(f"profileId={args.profile_id}")
    if args.locale:
        query.append(f"locale={args.locale}")
    url = f"ws://127.0.0.1:{args.port}/v1/agents/{args.slug}/voice"
    if query:
        url += "?" + "&".join(query)
    try:
        return asyncio.run(call(url, args.messages, args.token, args.timeout))
    except Exception as exc:  # noqa: BLE001 — say what the engine answered
        print(f"could not place the call: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
