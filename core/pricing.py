"""Per-model USD pricing for the dashboard + comparison views.

Each `agent_models` row carries `extra_config.pricing_mode` ("auto"|"manual",
default "auto"). In auto mode we price from real token counts via the
`tokencost` library's built-in per-model price table; when tokencost doesn't
know the model we fall back to the manually-entered `cost_per_1k_in/out`.
In manual mode we always use the entered rates. Returns None ("no price")
when nothing can price the model.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

try:  # tokencost is optional at import time so tests/CI without it still load.
    from tokencost import TOKEN_COSTS
except Exception:  # pragma: no cover - env-dependent
    TOKEN_COSTS = {}

# NOTE: we read tokencost's price DATA (`TOKEN_COSTS`, per-token rates) directly
# rather than calling tokencost's `calculate_cost_by_tokens` — so a test can
# monkeypatch `pricing.TOKEN_COSTS` and fully control the math, and so we don't
# depend on tokencost being importable at runtime for the manual path.


def pricing_mode(model: Dict[str, Any]) -> str:
    extra = (model or {}).get("extra_config") or {}
    mode = (extra.get("pricing_mode") or "auto").strip().lower()
    return "manual" if mode == "manual" else "auto"


def resolve_tokencost_key(model: Dict[str, Any]) -> Optional[str]:
    """tokencost key: explicit override, else the model_string normalized
    (strip anything before the last `/`, and an `@version` suffix).

    KEPT after the 2026-08-16 Vertex removal even though the
    `publishers/<vendor>/models/<id>@<ver>` ids that motivated it are no longer
    produced: `_build_openai` supports pointing a row at an OpenAI-COMPATIBLE
    endpoint via `extra_config.base_url`, and OpenRouter-style ids are
    `<vendor>/<model>` — so the slash split still does real normalization work.
    Removing it would silently change the tokencost lookup (and therefore the
    displayed cost) for any such row."""
    extra = (model or {}).get("extra_config") or {}
    override = (extra.get("tokencost_model") or "").strip()
    if override:
        return override
    raw = (model or {}).get("model_string") or ""
    if not raw:
        return None
    key = raw.split("/")[-1]      # drop a `<vendor>/` (or publisher-path) prefix
    key = key.split("@")[0]       # drop @version
    return key or None


def _manual_rates(model: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    extra = (model or {}).get("extra_config") or {}
    if "cost_per_1k_in" in extra or "cost_per_1k_out" in extra:
        return (float(extra.get("cost_per_1k_in") or 0.0),
                float(extra.get("cost_per_1k_out") or 0.0))
    return None


def effective_rates(model: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """($/1k in, $/1k out) for display. auto→tokencost; manual→entered rates."""
    if pricing_mode(model) == "auto":
        key = resolve_tokencost_key(model)
        if key and key in TOKEN_COSTS:
            row = TOKEN_COSTS[key]
            return (float(row.get("input_cost_per_token") or 0.0) * 1000,
                    float(row.get("output_cost_per_token") or 0.0) * 1000)
        return _manual_rates(model)
    return _manual_rates(model)


def cost_usd(
    model: Dict[str, Any],
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> Optional[float]:
    """USD for one turn. None when the model can't be priced.

    ``input_tokens`` is the LangChain TOTAL input (cache_read/creation are
    subsets of it). Uncached input is billed at the input rate; cache_read at
    tokencost's ``cache_read_input_token_cost`` and cache_creation at
    ``cache_creation_input_token_cost`` (each falling back to the full input
    rate when tokencost lacks them). With the cache args at their 0 defaults
    the result is identical to the pre-cache behaviour.
    """
    in_tok = int(input_tokens or 0)
    out_tok = int(output_tokens or 0)
    cr = int(cache_read_tokens or 0)
    cc = int(cache_creation_tokens or 0)
    uncached = max(0, in_tok - cr - cc)
    if pricing_mode(model) == "auto":
        key = resolve_tokencost_key(model)
        if key and key in TOKEN_COSTS:
            row = TOKEN_COSTS[key]
            rin = float(row.get("input_cost_per_token") or 0.0)
            rout = float(row.get("output_cost_per_token") or 0.0)
            raw_cr = row.get("cache_read_input_token_cost")
            rcr = float(raw_cr) if raw_cr is not None else rin
            raw_cc = row.get("cache_creation_input_token_cost")
            rcc = float(raw_cc) if raw_cc is not None else rin
            return uncached * rin + cr * rcr + cc * rcc + out_tok * rout
    rates = _manual_rates(model)  # auto+unknown falls through here, or manual mode
    if rates is None:
        return None
    rin, rout = rates  # per 1k tokens; manual mode has no cache rates
    return in_tok / 1000 * rin + out_tok / 1000 * rout


def cache_savings_usd(model: Dict[str, Any], cache_read_tokens: int) -> float:
    """USD saved by cache hits = cache_read * (input_rate - cache_read_rate).

    0.0 when the model can't be priced (unknown to tokencost, or manual mode
    which carries no cache rate).
    """
    cr = int(cache_read_tokens or 0)
    if cr <= 0 or pricing_mode(model) != "auto":
        return 0.0
    key = resolve_tokencost_key(model)
    if not key or key not in TOKEN_COSTS:
        return 0.0
    row = TOKEN_COSTS[key]
    rin = float(row.get("input_cost_per_token") or 0.0)
    raw_cr = row.get("cache_read_input_token_cost")
    rcr = float(raw_cr) if raw_cr is not None else rin
    return cr * (rin - rcr)
