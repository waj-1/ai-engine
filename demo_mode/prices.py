"""List prices for the demo's model rows, so cost figures are real numbers.

The demo's model rows are priced by hand (``pricing_mode: manual``) because
the price table the engine otherwise reads does not list these models.
Without rates a test run's cost and the model's page show no price at all.

Source: Azure's public Retail Prices API (https://prices.azure.com/api/retail/prices,
service "Foundry Models", product "Azure OpenAI GPT5"), read on 2026-11-24.
Global Standard deployments, USD per 1 million tokens:

    gpt-5.4-mini   input 0.75   output 4.50    (meters "5.4 mini Inp Gl" / "5.4 mini Opt Gl")
    gpt-5.5        input 5.00   output 30.00   (meters "5.5 ShortCo inp Gl" / "5.5 ShortCo opt Gl")

A deployment is matched by its NAME, so only a deployment named after its
model gets a price; any other name (and any other provider) gets none rather
than a guess. Data Zone and Provisioned deployments cost more; if the demo
moves to one, change the numbers here. The trace store prices each model call
by itself from the model name the provider reports (Laminar's own price list,
which had the same two prices on that date); these rates are for the screens
that price from the model row: Settings > Models and test runs.
"""
from __future__ import annotations

from typing import Dict, Tuple

PRICES_AS_OF = "2026-11-24"

#: deployment name -> (USD per 1M input tokens, USD per 1M output tokens).
AZURE_LIST_PRICES: Dict[str, Tuple[float, float]] = {
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.5": (5.00, 30.00),
}


def manual_rates(provider: str, deployment: str) -> Dict[str, float]:
    """``extra_config`` keys (USD per 1k tokens) for a model row, or {} when
    this deployment's price is not known."""
    if provider != "azure_openai":
        return {}
    found = AZURE_LIST_PRICES.get((deployment or "").strip().lower())
    if not found:
        return {}
    return {"cost_per_1k_in": round(found[0] / 1000, 8), "cost_per_1k_out": round(found[1] / 1000, 8)}
