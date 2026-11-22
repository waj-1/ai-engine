"""The ONE place the demo portal's fictional company is named.

Every customer, account, amount and story in the AI-agents demo portal is
fictional test data about a made-up home-internet and mobile provider that
operates in the UAE and Saudi Arabia. Swap the company by editing this dict:
prompts, use cases, the knowledge base and the portal all render from it.

Text elsewhere (fixtures JSON, KB markdown) never spells the name out — it
carries the placeholders below, which `render()` fills in at load time:

    {{brand}}          English name        {{brand_ar}}        Arabic name
    {{brand_short}}    English short name  {{brand_short_ar}}  Arabic short name
    {{brand_domain}}   fictional web domain
"""
from __future__ import annotations

import re
from typing import Any, Dict

BRAND: Dict[str, str] = {
    "name_en": "Falak Connect",
    "name_ar": "فلك كونكت",
    "short_en": "Falak",
    "short_ar": "فلك",
    # Fictional — never a real site. Used only inside KB text ("the app or
    # the website") so no answer ever reads a URL aloud.
    "domain": "falakconnect.example",
    "tagline_en": "Home internet and mobile for the UAE and Saudi Arabia",
    "tagline_ar": "إنترنت منزلي وخدمات جوال في الإمارات والسعودية",
}

_PLACEHOLDERS = {
    "brand": "name_en",
    "brand_ar": "name_ar",
    "brand_short": "short_en",
    "brand_short_ar": "short_ar",
    "brand_domain": "domain",
}

_TOKEN = re.compile(r"\{\{\s*(brand(?:_short)?(?:_ar)?|brand_domain)\s*\}\}")


def render(text: str) -> str:
    """Fill every ``{{brand…}}`` placeholder in ``text`` from BRAND."""
    if not text or "{{" not in text:
        return text
    return _TOKEN.sub(lambda m: BRAND[_PLACEHOLDERS[m.group(1)]], text)


def render_obj(value: Any) -> Any:
    """``render`` applied to every string inside a JSON-shaped value."""
    if isinstance(value, str):
        return render(value)
    if isinstance(value, list):
        return [render_obj(v) for v in value]
    if isinstance(value, dict):
        return {k: render_obj(v) for k, v in value.items()}
    return value
