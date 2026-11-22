"""Registry of duplex-voice agent builders — the extension seam for /voice/live.

`endpoints/voice_live.py` used to branch on `agent_kind` inline, so giving a
new agent kind a voice meant editing the endpoint. Now a kind registers a
builder here and the endpoint only consults the registry. Everything else on
the voice edge — realtime model row, persona prompt, greeting, relay custody,
budget, ceilings — is already admin-portal config (`voice_config.live`,
`agent_models`, `agent_prompts`, `agent_channels`), so ONE registration per
agent kind is the entire code footprint of duplex voice for it; a new
*tenant/profile* of an already-registered kind needs no code at all.

The params dict contract (assembled by the WS handshake in
`endpoints/voice_live.py`):

  user_token    caller bearer token or portal-session token (may be None)
  business_id   resolved tenant/merchant id (may be None)
  user_id       portal-session user id (may be None)
  store_ids     portal-session store scope (list, may be empty)
  stores        portal-session store objects (list, may be empty)
  locale        portal locale ("en"/"ar"/None)
  profile_slug  voice_config.live.agent_profile_slug — the voice specialist
                profile serving this call (None = the kind's active profile)

A builder takes what it needs from that dict, constructs the agent, and
returns it; the agent MUST expose `.profile` (every RuntimeAgent does) —
the endpoint feeds it to the persona resolver so voice personas respect the
same promote/rollback governance as every other prompt.

Imports inside each builder are deliberate: they keep this module import-light
(it's imported by the endpoint at boot) and they are the seams the test suite
monkeypatches (each builder's `create_<kind>_agent` factory).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class VoiceCredentialRefused(Exception):
    """Raised by a `VoiceCredential.verify` to refuse the call. `detail` is
    the WebSocket close reason the client sees; `reason` is for the log."""

    def __init__(self, detail: str, reason: str = ""):
        super().__init__(f"{detail} ({reason})")
        self.detail = detail
        self.reason = reason


@dataclass(frozen=True)
class VoiceCredential:
    """A second credential an auth-required kind needs besides the bearer
    token: typically a token for the backend its tools read AS the caller.

    `name` is both the auth-frame key the client sends it under and the
    builder params key it reaches the builder as. A header-authenticated
    caller (server-to-server, no auth frame) sends it in `header` instead.
    `verify(value, verified_user_id)` runs off the event loop before any
    agent, budget or provider work, and raises `VoiceCredentialRefused` to
    refuse. A verified caller WITHOUT the credential is refused with close
    reason `"<name>_required"`; only the trusted simulated-eval path (no
    caller identity at all) runs without one.
    """

    name: str
    header: str
    verify: Optional[Callable[[str, Optional[str]], Any]] = None

@dataclass(frozen=True)
class VoiceTenantAccess:
    """A kind whose voice brain reads one tenant's data AS the caller.

    The handshake then demands a verified caller (bearer header or auth
    frame) who `admit(user_token, tenant_id)` says belongs to the tenant the
    call names (`businessId`), runs before any config, budget or provider
    work, and closes a non-member with CLOSE_FORBIDDEN. `frame_params(frame)`
    turns the client's auth frame (``{}`` for a header-authenticated caller)
    into `(builder_params, pinned_context)`: params reach the builder, the
    pinned context rides every delegated turn, with the verified `user_id`.
    Both callables run off the event loop and must whitelist and coerce what
    they read: the frame is client-supplied. Such a kind is never served on
    the trusted simulated-eval path without an auth frame, because without
    one the call would have no data at all.
    """

    admit: Callable[[str, Optional[str]], bool]
    frame_params: Callable[[Any], Tuple[Dict[str, Any], Dict[str, Any]]]
    refused_reason: str = "Not a member of this tenant."


_BUILDERS: Dict[str, Callable[[Dict[str, Any]], Any]] = {}
_OPTIONS: Dict[str, Dict[str, Any]] = {}


def register_voice_agent_builder(
    agent_kind: str,
    builder: Callable[[Dict[str, Any]], Any],
    *,
    requires_auth: bool = False,
    identity_source: Optional[str] = None,
    session_reader: Optional[Callable[[Any], Optional[Dict[str, Any]]]] = None,
    default_phrase_list: Optional[Callable[[], List[str]]] = None,
    credential: Optional[VoiceCredential] = None,
    tenant_access: Optional[VoiceTenantAccess] = None,
    client_context_keys: Tuple[str, ...] = ("screen_context",),
    session_metadata_keys: Tuple[str, ...] = (),
) -> None:
    """Register `builder` as the duplex-voice constructor for `agent_kind`.

    `requires_auth=True` makes the WS handshake demand a verified first-frame
    auth (see endpoints/voice_live.py) before building the agent — for kinds
    whose data access needs a real user identity. `identity_source`
    overrides `TurnIdentity.source` (so a kind's voice sessions carry the
    same source tag as its HTTP chat sessions). `credential` names a second
    credential the handshake demands and verifies (see `VoiceCredential`);
    `tenant_access` a membership gate and auth-frame reader for a kind that
    reads one tenant's data as the caller (see `VoiceTenantAccess`).

    `client_context_keys`: the turn-context keys a client may set through the
    auth frame's `context` and later `context` frames; everything else is
    dropped, because this dict reaches the agent's prompt. Of those,
    `session_metadata_keys` are also copied onto the session row's metadata
    (audit linkage, e.g. which record a call was about).

    `session_reader(conn)` reads the kind's signed portal-session cookie off
    the WebSocket handshake (a browser cannot set headers on a WebSocket, so a
    portal's identity only reaches the socket that way); kinds with no portal
    session leave it unset. `default_phrase_list()` supplies the recognizer
    phrase list used when the profile sets none. Both are callables so a
    registration stays import-light and tests can patch what they import.
    """
    key = agent_kind.strip().lower()
    _BUILDERS[key] = builder
    _OPTIONS[key] = {
        "requires_auth": requires_auth,
        "identity_source": identity_source,
        "session_reader": session_reader,
        "default_phrase_list": default_phrase_list,
        "credential": credential,
        "tenant_access": tenant_access,
        "client_context_keys": tuple(client_context_keys),
        "session_metadata_keys": tuple(session_metadata_keys),
    }


def voice_agent_requires_auth(agent_kind: str) -> bool:
    """True iff `agent_kind`'s registration demands first-frame WS auth."""
    return bool(_OPTIONS.get((agent_kind or "").strip().lower(), {}).get("requires_auth"))


def voice_identity_source(agent_kind: str) -> Optional[str]:
    """The TurnIdentity.source override for `agent_kind`, if registered."""
    return _OPTIONS.get((agent_kind or "").strip().lower(), {}).get("identity_source")


def voice_session_reader(agent_kind: str) -> Optional[Callable[[Any], Optional[Dict[str, Any]]]]:
    """The portal-session reader registered for `agent_kind`, or None."""
    return _OPTIONS.get((agent_kind or "").strip().lower(), {}).get("session_reader")


def voice_default_phrase_list(agent_kind: str) -> List[str]:
    """The recognizer phrase list `agent_kind` falls back to, or []."""
    fn = _OPTIONS.get((agent_kind or "").strip().lower(), {}).get("default_phrase_list")
    return list(fn()) if fn else []


def voice_credential(agent_kind: str) -> Optional[VoiceCredential]:
    """The second credential `agent_kind`'s handshake demands, or None."""
    return _OPTIONS.get((agent_kind or "").strip().lower(), {}).get("credential")


def voice_tenant_access(agent_kind: str) -> Optional[VoiceTenantAccess]:
    """`agent_kind`'s tenant membership gate and auth-frame reader, or None."""
    return _OPTIONS.get((agent_kind or "").strip().lower(), {}).get("tenant_access")


_DEFAULT_CONTEXT_KEYS: Tuple[str, ...] = ("screen_context",)


def voice_client_context_keys(agent_kind: str) -> Tuple[str, ...]:
    """Turn-context keys a client may set for `agent_kind`'s calls."""
    opts = _OPTIONS.get((agent_kind or "").strip().lower())
    return opts["client_context_keys"] if opts else _DEFAULT_CONTEXT_KEYS


def voice_session_metadata_keys(agent_kind: str) -> Tuple[str, ...]:
    """Client-context keys copied onto `agent_kind`'s session row metadata."""
    return _OPTIONS.get((agent_kind or "").strip().lower(), {}).get("session_metadata_keys", ())


def voice_agent_kinds() -> frozenset:
    """The agent kinds that currently have a duplex-voice builder."""
    return frozenset(_BUILDERS)


def build_voice_agent(agent_kind: str, params: Dict[str, Any]) -> Any:
    """Construct the agent serving a duplex voice call for `agent_kind`.

    Raises LookupError for a kind with no registered builder — the endpoint
    translates that into its forbidden-close path, same bucket as any other
    misconfigured-tenant error.
    """
    builder = _BUILDERS.get((agent_kind or "").strip().lower())
    if builder is None:
        raise LookupError(
            f"duplex voice does not support agent_kind '{agent_kind}' — no "
            f"registered voice builder (see runtime/agents/voice_registry.py). "
            f"Registered: {sorted(_BUILDERS)}"
        )
    return builder(params)


# ---------------------------------------------------------------------------
# Built-in builders
# ---------------------------------------------------------------------------


def _demo_builder(agent_key: str):
    """Builder for one of the four AI-agents demo portal agents
    (runtime/agents/agents_demo.py). Anonymous by design — the portal is
    public, so the endpoint's IP-scoped ceilings are the abuse posture."""

    def _build(params: Dict[str, Any]) -> Any:
        from runtime.agents.agents_demo import create_demo_agent

        return create_demo_agent(
            agent_key,
            locale=params.get("locale") or "en",
            call_case=params.get("call_case"),
            profile_slug=params.get("profile_slug"),
        )

    return _build


for _key, _kind in (("support", "agents_support"), ("inquiries", "agents_inquiries"),
                    ("collection", "agents_collection"), ("sales", "agents_sales")):
    register_voice_agent_builder(_kind, _demo_builder(_key))

# Product agents register their own builders from a plugin package (see
# core/plugins.py), so this module names no agent it does not ship with.
# Last on purpose: a plugin imports `register_voice_agent_builder` from here.
from core.plugins import load_hook  # noqa: E402

load_hook("voice_builders")
