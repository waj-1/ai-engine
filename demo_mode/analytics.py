"""Anonymous usage analytics for the public demo (docs/deploy/demo.md, "Usage analytics").

Off unless ``DEMO_MODE`` is on and ``DEMO_POSTHOG_KEY`` is set. Then every demo
page (the entry page, the portal, and the gate's own "locked" and "too fast"
pages) loads `admin/static/demo-analytics.js`, which sends named events with
step, tool and page identifiers and counts to a PostHog project, straight from
the visitor's browser. It never sends what a visitor typed or any text from the
page; the script's header lists what it does send.

This module only builds the small JSON config the pages embed. The project key
is the public, write-only key a browser needs; it is not a secret.
"""
from __future__ import annotations

import html
import json
from typing import Any, Dict, Optional

from demo_mode import settings


def enabled() -> bool:
    """Demo mode is on and a project key is set."""
    return settings.enabled() and bool(settings.posthog_key())


def _seeded_slugs() -> list:
    """Slugs of the agents the demo seeds. A test chat with one of them is
    reported as `seeded`; any other agent (named by a visitor) as `visitor`,
    so a name a visitor typed is never sent."""
    from demo_mode import content

    slugs = [str(a.get("slug")) for a in content.AGENTS if a.get("slug")]
    try:
        from demo_mode import showcase

        slugs += sorted(showcase.SLUGS)
    except Exception:  # noqa: BLE001 (the showcase is optional)
        pass
    return slugs


def _messages() -> Dict[str, str]:
    """The sentences the server answers with when a cap is hit, so the script
    can name the cap without the server sending a new field."""
    from demo_mode import limits, voice

    return {
        "daily": limits.CAP_MESSAGE,
        "rate": limits.RATE_MESSAGE,
        "voice_daily": voice.CAP_MESSAGE,
        "voice_busy": voice.BUSY_MESSAGE,
        "voice_rate": voice.RATE_MESSAGE,
        "voice_off": voice.OFF_MESSAGE,
        "voice_length": voice.length_message(),
    }


def config(page: str, section: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """What `demo-analytics.js` needs, or None when analytics is off.

    `page` is the kind of page: ``entry`` (the Try the demo page), ``portal``,
    ``locked`` (a screen the demo switches off) or ``rate`` (a "too fast" page).
    `section` is the portal's sidebar section (admin/nav.py `section_for`)."""
    if not enabled():
        return None
    from urllib.parse import urlsplit

    return {
        "key": settings.posthog_key(),
        "host": settings.posthog_host(),
        # Events from any other host are tagged env=local, so a test install
        # never counts as real use.
        "public_host": (urlsplit(settings.public_url()).hostname or "").lower(),
        "repo_url": settings.repo_url(),
        "showcase_url": settings.showcase_page_url() if settings.showcase_enabled() else "",
        "page": page,
        "section": section or "",
        "seeded": _seeded_slugs(),
        "messages": _messages(),
    }


def head_tags(page: str, section: Optional[str] = None, version: str = "") -> str:
    """The two tags a page puts in its <head>, or "" when analytics is off.
    For pages that are not Jinja templates (demo_mode/gate.py)."""
    cfg = config(page, section)
    if cfg is None:
        return ""
    # `</` cannot close the script element early.
    data = json.dumps(cfg).replace("</", "<\\/")
    src = "/admin/static/demo-analytics.js" + (f"?v={html.escape(version)}" if version else "")
    return (f'<script type="application/json" id="demo-analytics-config">{data}</script>'
            f'<script src="{src}" defer></script>')
