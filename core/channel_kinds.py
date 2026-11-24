"""Which kinds of channel (``agent_channels.kind``) this build offers.

A channel's kind says what sort of request source it is. The engine knows the
generic ones; a plugin adds its own product surfaces through the ``channels``
hook (see ``core.plugins.HOOKS``):

* ``channel_kinds()`` returns ``{kind: plain label}``, listed before the
  generic kinds (the first one is the form's default).
* ``tenant_channel_kinds()`` names the kinds whose channel belongs to one
  business: the portal and the admin MCP refuse to save one without a
  ``business_id``, and drop a stray ``business_id`` on any other kind.
* ``full_build_channel_kinds()`` names the kinds a scoped build
  (``core/deployment.py``) never offers or lists.
* ``default_channels()`` returns the rows ``admin/seed_channels.py`` inserts
  at boot (insert-only, on slug).
* ``name_example()`` returns ``(name, slug)``: the example the New channel
  form shows in its empty Name and Short code name fields.

The stored kind is also checked by the ``agent_channels`` CHECK constraint, so
a kind a plugin adds must be allowed there too.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from core.plugins import call_hook, load_hook

GENERIC_CHANNEL_KINDS: Dict[str, str] = {
    "public_api": "Public API",
    "embed": "Website widget",
    "other": "Partner portal / other",
}


def _providers():
    return load_hook("channels")


def channel_kind_labels() -> Dict[str, str]:
    """Every kind this build offers, plugin kinds first, with its label."""
    labels: Dict[str, str] = {}
    for kinds in call_hook(_providers(), "channel_kinds"):
        labels.update(kinds or {})
    for kind, label in GENERIC_CHANNEL_KINDS.items():
        labels.setdefault(kind, label)
    return labels


def tenant_channel_kinds() -> Tuple[str, ...]:
    out: List[str] = []
    for kinds in call_hook(_providers(), "tenant_channel_kinds"):
        out.extend(k for k in (kinds or ()) if k not in out)
    return tuple(out)


def full_build_channel_kinds() -> Tuple[str, ...]:
    out: List[str] = []
    for kinds in call_hook(_providers(), "full_build_channel_kinds"):
        out.extend(k for k in (kinds or ()) if k not in out)
    return tuple(out)


# The New channel form's example name and code name in a build whose plugins
# give none: a neutral surface, not any product's app.
DEFAULT_NAME_EXAMPLE: Tuple[str, str] = ("Website chat", "website_chat")


def channel_name_example() -> Tuple[str, str]:
    """``(name, slug)`` the New channel form shows as placeholders: the last
    plugin's ``name_example()``, else :data:`DEFAULT_NAME_EXAMPLE`."""
    found = [tuple(x) for x in call_hook(_providers(), "name_example") if x]
    return found[-1] if found else DEFAULT_NAME_EXAMPLE


def default_channels() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for found in call_hook(_providers(), "default_channels"):
        rows.extend(found or [])
    return rows
