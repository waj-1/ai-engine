"""Azure OpenAI Realtime — declared, not yet implemented, and NOT the Azure
duplex-voice path.

Registered so an operator who sets an `azure_openai` realtime model row gets a
clear, actionable error instead of a mystery, exactly how
`runtime/llm_factory.py` treats `bedrock`/`ollama`.

**The Azure migration's actual duplex-voice provider is `azure_voice_live`**
(`voice/live/providers/azure_voice_live.py`, registered under slug
`azure_voice_live`) — Microsoft Foundry's Voice Live API in cascaded mode
(Azure STT, locale-pinned and phrase-list-boosted -> a chat model -> Azure
neural TTS), running in `uaenorth`. Point an `agent_models` row's `provider`
at `azure_voice_live`, not `azure_openai`. See the design spec at
`docs/superpowers/specs/2026-07-28-azure-voice-live-provider-design.md`
("Approved decisions") for why cascaded Voice Live was chosen over raw Azure
OpenAI Realtime: no Gulf region for the native `gpt-realtime` model, that
model retires 2027-02, and going raw would mean rebuilding VAD/noise-
suppression/echo-cancellation ourselves instead of getting them from Voice
Live's session config.

This module — the raw Azure OpenAI Realtime API, as distinct from Voice
Live — remains unimplemented and is intentionally out of scope for that
spec. If it's ever revisited, resolve the open question from the *original*
design spec first: the Azure migration measured quota in `uaenorth` and
found no realtime-capable model there (`gpt-4o` is at zero). A realtime
deployment would most likely need its own Azure OpenAI account in a
different region, addressed via the model row's `extra_config.endpoint`
rather than the shared `AZURE_OPENAI_ENDPOINT`. See
`docs/superpowers/specs/2026-07-26-duplex-voice-edge-design.md` ('Azure
dependency') before starting.

The conformance suite in `voice/live/testing.py` defines what "done" means:
`assert_realtime_contract` must pass against a real session. Map
`ToolResultMode` as follows —
  INTERRUPT -> conversation.item.create + response.create
  SILENT    -> conversation.item.create only (no response.create)
  WHEN_IDLE -> queue the pair until response.done
— and drive the filler with announce-then-call in the persona prompt, since
raw Azure OpenAI Realtime has no non-blocking function calls (Voice Live
does support asynchronous function calling — see the 2026-07-28 spec).
"""
from __future__ import annotations

from voice.live.base import RealtimeSession
from voice.live.config import LiveVoiceConfig
from voice.live.factory import (
    UnsupportedRealtimeProviderError,
    register_realtime_provider,
)


def build_azure_realtime(cfg: LiveVoiceConfig, persona: str) -> RealtimeSession:
    raise UnsupportedRealtimeProviderError(
        "azure_openai realtime voice is not implemented -- and it is NOT the "
        "Azure duplex-voice path. Use provider slug azure_voice_live "
        "(voice/live/providers/azure_voice_live.py -- Microsoft Foundry Voice "
        "Live, cascaded mode, uaenorth) on the agent_models row instead; see "
        "docs/superpowers/specs/2026-07-28-azure-voice-live-provider-design.md. "
        "The raw Azure OpenAI Realtime API is deliberately out of scope for "
        "that spec (no Gulf region for native gpt-realtime, and the model "
        "retires 2027-02) -- if ever revisited, start from the module "
        "docstring in voice/live/providers/azure_realtime.py."
    )


register_realtime_provider("azure_openai", build_azure_realtime)
