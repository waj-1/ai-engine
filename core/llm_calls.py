"""Count each model call once, on the READ side.

With the LangChain instrumentation and a provider SDK's instrumentation both
on (OpenAI and Azure OpenAI models; the same would hold for any provider
whose SDK is instrumented), one model call is exported as TWO LLM spans:

* the LangChain wrapper's (``AzureChatOpenAI.chat`` / ``ChatOpenAI.chat``):
  token counts, the messages, but ``model = 'unknown'`` and no cost on a
  streamed call (on a plain ``invoke`` it carries the model and a cost too);
* the provider SDK's (``openai.chat``): the same token counts, the real
  response model, the cost.

They are NOT parent and child. The wrapper hangs off LangChain's run tree and
the SDK span off the OpenTelemetry context, so a delegated call's wrapper sits
under the hand-off tool span while its SDK span sits under the request root
(measured on the demo's store, 2026-11-24). Laminar's per-trace totals
(``traces_replacing.input_tokens`` / ``output_tokens`` / ``total_cost``) add
every LLM span, so tokens read double, and cost reads double wherever both
spans are priced.

Why this is fixed where the numbers are read and not where spans are made:
neither instrumentation can be switched off without losing something a screen
needs. The LangChain one also makes every ``<tool>.tool`` span (actions,
failed actions, hand-offs, the tool filter); the provider one is the only
span of a streamed call that names the model and carries a cost, and the only
source of the embedding spans. And the stored rows of every existing install
are already doubled: a read-side rule corrects those too.

The rule (the same in SQL and in Python, below): **a LangChain wrapper span
is left out when the same trace holds a span of another instrumentation with
the same input and output token counts whose time range overlaps it.** Its
twin is counted, with the larger of the two costs and cache counts. A span
with no twin (a provider whose SDK is not instrumented, an embedding call) is
always counted, so nothing is ever dropped on a guess.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

#: An attribute only the LangChain instrumentation writes (LangChain's own
#: ``ls_model_type`` run parameter, exported by OpenLLMetry's callback handler).
WRAPPER_MARKER = "traceloop.association.properties.ls_model_type"

# Tuple positions inside the per-trace array (ClickHouse tuples are 1-based).
_START, _END, _IN, _OUT, _COST, _CACHE_READ, _CACHE_NEW, _WRAPPER = range(1, 9)

_SAME = ("y.{w} != x.{w} AND y.{i} = x.{i} AND y.{o} = x.{o} "
         "AND y.{s} <= x.{e} AND x.{s} <= y.{e}").format(
    w=_WRAPPER, i=_IN, o=_OUT, s=_START, e=_END)


def once_columns(table: str = "") -> str:
    """SELECT columns for a ``FROM spans ... GROUP BY trace_id`` aggregate:
    ``llm_calls_once``, ``input_tokens_once``, ``output_tokens_once``,
    ``cost_once``, ``cache_read_once`` and ``cache_creation_once`` for the
    trace, each model call counted once. ``table`` is the spans table's alias
    (with its dot), when the query has one.

    Two helper columns come with them (``llm_all``, ``llm_calls``); a caller
    selects only the ``*_once`` ones from the subquery."""
    t = table
    attr = "simpleJSONExtractUInt(%sattributes, 'gen_ai.usage.%s')"
    return (
        "arraySort(x -> x.1, groupArrayIf(("
        f"{t}start_time, {t}end_time, toInt64({t}input_tokens), toInt64({t}output_tokens), "
        f"toFloat64({t}total_cost), toInt64({attr % (t, 'cache_read_input_tokens')}), "
        f"toInt64({attr % (t, 'cache_creation_input_tokens')}), "
        f"toUInt8(position({t}attributes, '{WRAPPER_MARKER}') > 0)), {t}span_type = 1)) AS llm_all,\n"
        # Every span but a wrapper that has a twin.
        f"arrayFilter(x -> NOT (x.{_WRAPPER} = 1 AND arrayExists(y -> {_SAME}, llm_all)), llm_all) "
        "AS llm_calls,\n"
        "length(llm_calls) AS llm_calls_once,\n"
        f"arraySum(x -> x.{_IN}, llm_calls) AS input_tokens_once,\n"
        f"arraySum(x -> x.{_OUT}, llm_calls) AS output_tokens_once,\n"
        + ",\n".join(
            # The counted span's own value, or its dropped twin's when larger.
            f"arraySum(x -> greatest(x.{pos}, if(x.{_WRAPPER} = 0, "
            f"arrayMax(arrayMap(y -> y.{pos}, arrayFilter(y -> {_SAME}, llm_all))), {zero})), "
            f"llm_calls) AS {name}"
            for pos, name, zero in ((_COST, "cost_once", "0."),
                                    (_CACHE_READ, "cache_read_once", "0"),
                                    (_CACHE_NEW, "cache_creation_once", "0"))
        )
    )


def _ts(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    text = str(value or "").strip().replace("Z", "+00:00").replace(" ", "T")
    if not text:
        return None
    # ClickHouse prints nanoseconds; Python parses microseconds.
    head, dot, frac = text.partition(".")
    zone = ""
    for sign in ("+", "-"):
        if sign in frac:
            frac, _, rest = frac.partition(sign)
            zone = sign + rest
    if dot:
        text = "%s.%s%s" % (head, frac[:6], zone)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        from datetime import timezone

        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def is_wrapper(span: Dict[str, Any]) -> bool:
    """True for a LangChain wrapper span. Reads the ``wrapper`` flag when the
    row has one, else looks for the marker in its attributes (a dict or the
    stored JSON text)."""
    if "wrapper" in span:
        return bool(span.get("wrapper"))
    attrs = span.get("attributes")
    if isinstance(attrs, dict):
        return WRAPPER_MARKER in attrs
    return WRAPPER_MARKER in str(attrs or "")


def _twins(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    if is_wrapper(a) == is_wrapper(b):
        return False
    if int(_num(a.get("input_tokens"))) != int(_num(b.get("input_tokens"))):
        return False
    if int(_num(a.get("output_tokens"))) != int(_num(b.get("output_tokens"))):
        return False
    a0, a1, b0, b1 = _ts(a.get("start_time")), _ts(a.get("end_time")), \
        _ts(b.get("start_time")), _ts(b.get("end_time"))
    if None in (a0, a1, b0, b1):
        return False
    return b0 <= a1 and a0 <= b1


def count_once(llm_spans: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """The same rule in Python, for one trace's LLM spans (rows with
    ``start_time``, ``end_time``, ``input_tokens``, ``output_tokens``,
    ``total_cost``, optional ``cache_read_tokens`` / ``cache_creation_tokens``
    and either ``wrapper`` or ``attributes``). Returns the totals and
    ``dropped``: the positions (in the order given) of the spans left out."""
    spans: List[Dict[str, Any]] = list(llm_spans)
    dropped = [i for i, s in enumerate(spans)
               if is_wrapper(s) and any(_twins(s, o) for o in spans if o is not s)]
    out = {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "total_cost": 0.0,
           "cache_read_tokens": 0, "cache_creation_tokens": 0, "dropped": dropped}
    for i, s in enumerate(spans):
        if i in dropped:
            continue
        twins = [] if is_wrapper(s) else [o for o in spans if o is not s and _twins(s, o)]
        out["llm_calls"] += 1
        out["input_tokens"] += int(_num(s.get("input_tokens")))
        out["output_tokens"] += int(_num(s.get("output_tokens")))
        out["total_cost"] += max([_num(s.get("total_cost"))] + [_num(t.get("total_cost")) for t in twins])
        for key in ("cache_read_tokens", "cache_creation_tokens"):
            out[key] += int(max([_num(s.get(key))] + [_num(t.get(key)) for t in twins]))
    return out
