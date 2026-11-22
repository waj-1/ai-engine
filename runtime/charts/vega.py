"""Deterministic Vega-Lite v5 spec builders. Data values are the real
aggregated rows; the LLM never supplies numbers.

Neutral by default (`DEFAULT_PALETTE`). A caller that renders for a branded
deployment passes its own `palette`; the first colour is the accent of a
single-series bar or line."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

VEGA_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"
DEFAULT_PALETTE = ["#3366CC", "#4D4D4D", "#2CA58D", "#8C8C8C", "#D1495B"]


def _base(title: str, palette: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"$schema": VEGA_SCHEMA, "title": title, "background": "transparent",
            "config": {"range": {"category": palette or DEFAULT_PALETTE}, "view": {"stroke": None}}}


def pie_spec(rows: List[Dict[str, Any]], *, title: str, value_field: str = "amount",
             palette: Optional[List[str]] = None) -> Dict[str, Any]:
    s = _base(title, palette)
    s.update({
        "data": {"values": rows},
        "mark": {"type": "arc", "innerRadius": 50, "tooltip": True},
        "encoding": {
            "theta": {"field": value_field, "type": "quantitative"},
            "color": {"field": "key", "type": "nominal", "legend": {"title": None}},
        },
    })
    return s


def bar_spec(rows: List[Dict[str, Any]], *, title: str, x_field: str = "key",
             y_field: str = "amount", palette: Optional[List[str]] = None,
             horizontal: bool = False) -> Dict[str, Any]:
    s = _base(title, palette)
    accent = (palette or DEFAULT_PALETTE)[0]
    if horizontal:
        # Horizontal layout for categorical comparisons: long labels (product
        # names) render upright on the y axis instead of rotated/overlapping
        # under the plot. Bars are capped thin with a rounded data-end, the
        # value is direct-labeled at the bar tip (which doubles as the
        # low-contrast-accent relief), and the chart stretches to its card.
        # Rows are pre-sorted descending and the scale told to keep data order
        # ("sort": None) — an encoding-level "-x" sort conflicts across the
        # bar/text layers' unioned domain and silently falls back alphabetic.
        ordered = sorted(rows, key=lambda r: r.get(y_field) or 0, reverse=True)
        s.update({
            "data": {"values": ordered},
            "width": "container",
            "height": {"step": 34},
            "encoding": {
                "y": {"field": x_field, "type": "nominal", "sort": None,
                      "axis": {"title": None, "labelLimit": 200, "labelPadding": 8}},
                "x": {"field": y_field, "type": "quantitative",
                      "axis": {"title": None, "tickCount": 5, "format": ",.2~f"}},
            },
            "layer": [
                {"mark": {"type": "bar", "tooltip": True, "color": accent,
                          "cornerRadiusEnd": 4, "height": {"band": 0.62}}},
                {"mark": {"type": "text", "align": "left", "dx": 6},
                 "encoding": {"text": {"field": y_field, "type": "quantitative",
                                       "format": ",.2~f"}}},
            ],
        })
        return s
    s.update({
        "data": {"values": rows},
        "mark": {"type": "bar", "tooltip": True, "color": accent},
        "encoding": {
            "x": {"field": x_field, "type": "nominal", "sort": "-y", "axis": {"labelAngle": -30}},
            "y": {"field": y_field, "type": "quantitative"},
        },
    })
    return s


def line_spec(series: List[Dict[str, Any]], *, title: str, x_field: str = "date",
              y_field: str = "value", palette: Optional[List[str]] = None) -> Dict[str, Any]:
    s = _base(title, palette)
    accent = (palette or DEFAULT_PALETTE)[0]
    s.update({
        "data": {"values": series},
        "width": "container",
        "mark": {"type": "line", "point": True, "tooltip": True, "color": accent},
        "encoding": {
            "x": {"field": x_field, "type": "temporal", "axis": {"title": None}},
            "y": {"field": y_field, "type": "quantitative", "axis": {"title": None}},
        },
    })
    return s
