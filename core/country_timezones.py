"""The timezone a country dialing code implies: the zone a tenant's
"today/now" is read in. It mirrors the main database's own country-code
lookup, so keep the two in sync."""
from __future__ import annotations

from typing import Optional

TZ_BY_COUNTRY_CODE = {
    "971": "Asia/Dubai", "962": "Asia/Amman", "974": "Asia/Qatar", "968": "Asia/Muscat",
    "966": "Asia/Riyadh", "965": "Asia/Kuwait", "973": "Asia/Bahrain", "961": "Asia/Beirut",
    "20": "Africa/Cairo", "1": "America/New_York", "91": "Asia/Kolkata", "46": "Europe/Stockholm",
}


def zone_name_for_country_code(country_code: Optional[str]) -> Optional[str]:
    return TZ_BY_COUNTRY_CODE.get(str(country_code or "").strip().lstrip("+"))
