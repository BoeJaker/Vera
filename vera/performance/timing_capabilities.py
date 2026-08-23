"""Aggregate compact performance events without introducing another data store."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY, capability


SUMMARY_SCHEMA_VERSION = "vera.code-author-timing-summary/v1"
TIMING_SCHEMA_VERSION = "vera.code-author-timing/v1"
_SCALAR_METRICS = (
    "total_ms",
    "generation_ms",
    "post_generation_ms",
    "last_stream_to_generation_return_ms",
    "last_stream_to_result_ready_ms",
    "telemetry_emit_ms",
)
_PHASES = (
    "preparation",
    "generation",
    "parse_and_syntax_repair",
    "persistence",
    "smoke_and_runtime_repair",
)
_COUNTERS = ("syntax_repairs", "smoke_runs", "runtime_repairs")


def _non_negative_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _percentile(values: Iterable[float], quantile: float) -> int | float | None:
    """Return an inclusive, linearly interpolated percentile."""
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    value = ordered[lower]
    if upper != lower:
        value += (ordered[upper] - value) * (position - lower)
    rounded = round(value, 2)
    return int(rounded) if rounded.is_integer() else rounded


def _distribution(values: list[float]) -> dict[str, int | float | None]:
    return {
        "samples": len(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "max": (int(max(values)) if values and max(values).is_integer()
                else (round(max(values), 2) if values else None)),
    }


def summarize_code_author_timings(events: Iterable[Any]) -> dict[str, Any]:
    """Summarize valid v1 timing events and explicitly account for rejected input."""
    supplied = 0
    accepted: list[Mapping[str, Any]] = []
    for event in events:
        supplied += 1
        if not isinstance(event, Mapping) or event.get("type") != "code.author.timing":
            continue
        timing = event.get("timing")
        if isinstance(timing, Mapping) and timing.get("schema") == TIMING_SCHEMA_VERSION:
            accepted.append(timing)

    metrics: dict[str, dict[str, int | float | None]] = {}
    for name in _SCALAR_METRICS:
        values = [number for timing in accepted
                  if (number := _non_negative_number(timing.get(name))) is not None]
        metrics[name] = _distribution(values)

    phases: dict[str, dict[str, int | float | None]] = {}
    for name in _PHASES:
        values = []
        for timing in accepted:
            raw_phases = timing.get("phases_ms")
            number = _non_negative_number(raw_phases.get(name)) if isinstance(raw_phases, Mapping) else None
            if number is not None:
                values.append(number)
        phases[name] = _distribution(values)

    counters: dict[str, dict[str, int | float]] = {}
    for name in _COUNTERS:
        values = []
        for timing in accepted:
            raw_counters = timing.get("counters")
            number = _non_negative_number(raw_counters.get(name)) if isinstance(raw_counters, Mapping) else None
            if number is not None:
                values.append(number)
        nonzero = sum(value > 0 for value in values)
        counters[name] = {
            "samples": len(values),
            "total": int(sum(values)),
            "runs_with_activity": nonzero,
            "activity_rate": round(nonzero / len(values), 4) if values else 0.0,
        }

    return {
        "schema": SUMMARY_SCHEMA_VERSION,
        "source_schema": TIMING_SCHEMA_VERSION,
        "method": "inclusive-linear-interpolation",
        "events": {"supplied": supplied, "accepted": len(accepted),
                   "ignored": supplied - len(accepted)},
        "metrics_ms": metrics,
        "phases_ms": phases,
        "counters": counters,
    }


@capability(
    "code.author.timing.summary", memory="off", silent=True,
    http_method="GET", http_path="/code/author/timing/summary", http_tags=["code", "obs"],
    description="Aggregate recent code.author.timing v1 events into deterministic p50/p95/max "
                "phase statistics and repair activity. Inputs: limit (1..500 recent events).",
)
async def code_author_timing_summary(limit: int = 200, trace_id=None) -> dict[str, Any]:
    bounded_limit = max(1, min(int(limit or 200), 500))
    observer = CAPABILITY_REGISTRY.get("obs.events", {}).get("raw")
    if observer is None:
        result = summarize_code_author_timings([])
        result["error"] = "obs.events is unavailable"
        return result
    events = await observer(limit=bounded_limit, trace_id=trace_id)
    result = summarize_code_author_timings(events if isinstance(events, list) else [])
    result["window"] = {"requested": bounded_limit, "returned": len(events) if isinstance(events, list) else 0}
    return result
