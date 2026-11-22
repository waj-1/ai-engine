import json
from runtime.charts.vega import DEFAULT_PALETTE, pie_spec, bar_spec, line_spec, VEGA_SCHEMA

ROWS = [{"key": "card", "amount": 100.0, "count": 2, "pct": 66.67},
        {"key": "cash", "amount": 50.0, "count": 1, "pct": 33.33}]


def _valid(spec):
    assert spec["$schema"] == VEGA_SCHEMA
    assert "data" in spec and "values" in spec["data"]
    json.dumps(spec)
    return spec


def test_pie_spec_embeds_rows():
    s = _valid(pie_spec(ROWS, title="By scheme", value_field="amount"))
    assert s["mark"]["type"] == "arc"
    assert s["data"]["values"] == ROWS
    assert s["encoding"]["theta"]["field"] == "amount"
    assert s["encoding"]["color"]["field"] == "key"


def test_bar_spec_embeds_rows():
    s = _valid(bar_spec(ROWS, title="Top stores", x_field="key", y_field="amount"))
    assert s["mark"]["type"] == "bar"
    assert s["encoding"]["x"]["field"] == "key"
    assert s["encoding"]["y"]["field"] == "amount"


def test_bar_spec_horizontal_flips_axes_and_labels_tips():
    """Opt-in horizontal layout: categories move to the y axis (long labels
    render upright instead of rotated/overlapping), values to x, and a text
    layer direct-labels each bar tip. The default (vertical) stays untouched —
    see test_bar_spec_embeds_rows."""
    s = _valid(bar_spec(ROWS, title="Top stores", horizontal=True))
    assert s["encoding"]["y"]["field"] == "key"
    assert s["encoding"]["x"]["field"] == "amount"
    assert s["width"] == "container"
    assert [layer["mark"]["type"] for layer in s["layer"]] == ["bar", "text"]
    assert s["layer"][0]["mark"]["color"] == DEFAULT_PALETTE[0]  # default palette intact


def test_line_spec_embeds_series():
    series = [{"date": "2026-06-01", "value": 150.0}, {"date": "2026-06-02", "value": 0.0}]
    s = _valid(line_spec(series, title="Daily sales"))
    assert s["mark"]["type"] == "line"
    assert s["data"]["values"] == series
    assert s["encoding"]["x"]["field"] == "date"


def test_default_palette_is_neutral_and_a_palette_overrides_it():
    """With no `palette` kwarg every builder emits the neutral default; a
    caller's own palette replaces it everywhere, accent included."""
    series = [{"date": "2026-06-01", "value": 150.0}]
    for spec in (pie_spec(ROWS, title="t"), bar_spec(ROWS, title="t"), line_spec(series, title="t")):
        assert spec["config"]["range"]["category"] == DEFAULT_PALETTE
    assert bar_spec(ROWS, title="t")["mark"]["color"] == DEFAULT_PALETTE[0]

    brand = ["#112233", "#445566"]
    bar = bar_spec(ROWS, title="t", palette=brand)
    line = line_spec(series, title="t", palette=brand)
    assert bar["config"]["range"]["category"] == brand
    assert bar["mark"]["color"] == "#112233"
    assert line["mark"]["color"] == "#112233"
