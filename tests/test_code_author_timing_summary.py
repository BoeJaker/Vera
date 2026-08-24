import asyncio

import pytest

from vera.performance import timing_capabilities as timing


pytestmark = pytest.mark.critical


def _event(total, generation, *, syntax=0, smoke=0, runtime=0):
    return {
        "type": "code.author.timing",
        "path": "must-not-appear.py",
        "timing": {
            "schema": "vera.code-author-timing/v1",
            "total_ms": total,
            "generation_ms": generation,
            "post_generation_ms": total - generation,
            "last_stream_to_generation_return_ms": generation / 10,
            "last_stream_to_result_ready_ms": total / 10,
            "phases_ms": {"preparation": 10, "generation": generation,
                          "parse_and_syntax_repair": 20, "persistence": 30,
                          "smoke_and_runtime_repair": 40},
            "counters": {"syntax_repairs": syntax, "smoke_runs": smoke,
                         "runtime_repairs": runtime},
        },
    }


def test_summary_calculates_percentiles_and_repair_activity_without_paths():
    result = timing.summarize_code_author_timings([
        _event(100, 60), _event(200, 120, syntax=1, smoke=2),
        _event(300, 180, runtime=1), _event(400, 240, syntax=2),
    ])
    assert result["events"] == {"supplied": 4, "accepted": 4, "ignored": 0}
    assert result["metrics_ms"]["total_ms"] == {
        "samples": 4, "p50": 250, "p95": 385, "max": 400}
    assert result["phases_ms"]["generation"]["p95"] == 231
    assert result["counters"]["syntax_repairs"] == {
        "samples": 4, "total": 3, "runs_with_activity": 2, "activity_rate": 0.5}
    assert "must-not-appear.py" not in repr(result)


def test_summary_ignores_other_schemas_and_invalid_numeric_fields():
    valid = _event(100, 60)
    valid["timing"]["total_ms"] = float("nan")
    wrong_schema = _event(200, 120)
    wrong_schema["timing"]["schema"] = "vera.code-author-timing/v2"
    result = timing.summarize_code_author_timings([
        valid, wrong_schema, {"type": "other"}, "bad"])
    assert result["events"] == {"supplied": 4, "accepted": 1, "ignored": 3}
    assert result["metrics_ms"]["total_ms"]["samples"] == 0
    assert result["metrics_ms"]["total_ms"]["p95"] is None


def test_capability_bounds_window_and_prefers_dedicated_stream(monkeypatch):
    calls = []

    async def observe(name, limit, trace_id=None):
        calls.append((name, limit, trace_id))
        return [_event(100, 60)]

    monkeypatch.setitem(timing.CAPABILITY_REGISTRY, "obs.stream_history", {"raw": observe})
    result = asyncio.run(timing.code_author_timing_summary.__wrapped__(
        limit=9999, trace_id="summary-test"))
    assert calls == [("code.author.timing", 500, "summary-test")]
    assert result["window"] == {
        "requested": 500, "returned": 1, "source": "code.author.timing"}
    assert result["events"]["accepted"] == 1


def test_capability_falls_back_to_generic_events_during_migration(monkeypatch):
    calls = []

    async def stream(name, limit, trace_id=None):
        calls.append(("stream", name, limit, trace_id))
        return []

    async def events(limit, trace_id=None):
        calls.append(("events", limit, trace_id))
        return [_event(120, 70)]

    monkeypatch.setitem(timing.CAPABILITY_REGISTRY, "obs.stream_history", {"raw": stream})
    monkeypatch.setitem(timing.CAPABILITY_REGISTRY, "obs.events", {"raw": events})
    result = asyncio.run(timing.code_author_timing_summary.__wrapped__(
        limit=10, trace_id="migration-test"))
    assert calls == [
        ("stream", "code.author.timing", 10, "migration-test"),
        ("events", 10, "migration-test"),
    ]
    assert result["events"]["accepted"] == 1
    assert result["window"] == {"requested": 10, "returned": 1, "source": "events"}
