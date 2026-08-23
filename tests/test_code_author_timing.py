import asyncio

import pytest

from vera.dag import dag_workshop_capabilities as workshop
from vera.dag.dag_workshop_capabilities import _code_author_timing


pytestmark = pytest.mark.critical


def test_code_author_timing_separates_visible_and_post_generation_delay():
    timing = _code_author_timing(
        10.0, 12.0, 20.0, 27.0, last_stream_at=19.5,
        last_result_stream_at=26.0,
        phases={"preparation": 2.0, "generation": 8.0,
                "parse_and_syntax_repair": 1.0, "persistence": 0.5,
                "smoke_and_runtime_repair": 5.5},
        counters={"syntax_repairs": 1, "smoke_runs": 2, "runtime_repairs": 1},
    )
    assert timing["schema"] == "vera.code-author-timing/v1"
    assert timing["total_ms"] == 17_000
    assert timing["generation_ms"] == 8_000
    assert timing["post_generation_ms"] == 7_000
    assert timing["last_stream_to_generation_return_ms"] == 500
    assert timing["last_stream_to_result_ready_ms"] == 1_000
    assert timing["phases_ms"]["smoke_and_runtime_repair"] == 5_500
    assert timing["counters"]["smoke_runs"] == 2


def test_code_author_timing_handles_no_stream_and_clamps_bad_clock_values():
    timing = _code_author_timing(5.0, 4.0, 3.0, 2.0,
                                 phases={"generation": -1.0},
                                 counters={"syntax_repairs": -3})
    assert timing["total_ms"] == 0
    assert timing["generation_ms"] == 0
    assert timing["last_stream_to_result_ready_ms"] is None
    assert timing["phases_ms"]["generation"] == 0
    assert timing["counters"]["syntax_repairs"] == 0


def test_code_author_returns_and_emits_timing_without_a_live_model(monkeypatch):
    events, streamed = [], []

    async def generate(**kwargs):
        await kwargs["stream_cb"]("print('ok')")
        return {"text": "```python file=timed.py\nprint('ok')\n```", "truncated": False}

    async def save(path, content, **kwargs):
        return {"ok": True, "path": path, "fs_path": "", "version": 1,
                "bytes": len(content), "lang": "python"}

    async def no_lines(*args, **kwargs): return []
    async def no_hint(*args, **kwargs): return ""
    async def no_files(*args, **kwargs): return []
    async def emit(event): events.append(event)
    async def stream(chunk): streamed.append(chunk)

    monkeypatch.setitem(workshop.CAPABILITY_REGISTRY, "llm.generate", {"raw": generate})
    monkeypatch.setattr(workshop, "code_store_save", save)
    monkeypatch.setattr(workshop, "_v5_context_schema_lines", no_lines)
    monkeypatch.setattr(workshop, "_package_hint", no_hint)
    monkeypatch.setattr(workshop, "_v5_workdir_files", no_files)
    monkeypatch.setattr(workshop, "emit_event", emit)
    monkeypatch.setenv("VERA_CODE_AUTHOR_SMOKE_RUN", "0")

    result = asyncio.run(workshop.cap_code_author.__wrapped__(
        task="write a hello-world script", path="timed.py",
        session_id="timing-test", trace_id="trace-test", stream_cb=stream))
    assert result["ok"] is True
    assert streamed == ["print('ok')"]
    assert result["timing"]["schema"] == "vera.code-author-timing/v1"
    assert result["timing"]["last_stream_to_result_ready_ms"] is not None
    assert result["timing"]["counters"] == {
        "runtime_repairs": 0, "smoke_runs": 0, "syntax_repairs": 0}
    timing_events = [event for event in events if event.get("type") == "code.author.timing"]
    assert len(timing_events) == 1
    assert timing_events[0]["session_id"] == "timing-test"
    assert timing_events[0]["trace_id"] == "trace-test"
