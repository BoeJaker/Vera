"""The durable per-call record (looplab-control B1+, 19 Oct 2026): the pure core, and where the write happens."""
import json
import os

import pytest

from vera.workers import ollama_calls_core as core

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
ROWS = [
    {"ts": "2026-10-19T04:10:00Z", "model": "m", "instance": "gpu-250", "job_type": "chat", "status": "done", "elapsed_s": 4.0,
     "est_seconds": 5.0, "tok_per_s": 40, "queue_ms": 5, "options": {"temperature": 0.2}, "think": False, "writer_load1": 1.5},
    {"ts": "2026-10-19T04:40:00Z", "model": "m", "instance": "gpu-250", "job_type": "chat", "status": "done", "elapsed_s": 6.0,
     "est_seconds": 5.0, "tok_per_s": 30, "queue_ms": 15, "options": {"temperature": 0.2}, "cpu_spill": True, "writer_load1": 2.5},
    {"ts": "2026-10-19T04:50:00Z", "model": "m", "instance": "cpu-246", "job_type": "chat", "status": "error", "elapsed_s": 1.0},
    {"ts": "2026-10-19T05:05:00Z", "model": "m", "instance": "gpu-250", "job_type": "chat", "status": "done", "elapsed_s": 2.0,
     "options": {"temperature": 0.7}, "think": True},
]


def test_stream_entries_parse_and_bad_ones_are_skipped():
    rows = core.parse([(b"1-0", {b"data": json.dumps(ROWS[0]).encode()}), ("2-0", {"data": "not json"})])
    assert len(rows) == 1 and rows[0]["_id"] == "1-0" and rows[0]["model"] == "m"


def test_filters():
    assert [core.matches(r, instance="gpu-250") for r in ROWS] == [True, True, False, True]
    since = core._ts(ROWS[2])
    assert [core.matches(r, since=since) for r in ROWS] == [False, False, True, True]


def test_hourly_rollup_says_speed_estimate_error_spills_and_load():
    out = core.rollup(ROWS)
    four = [r for r in out if r["bucket"].startswith("2026-10-19T04") and r["instance"] == "gpu-250"][0]
    assert four["calls"] == 2 and four["p50_s"] in (4.0, 6.0) and four["tps"] == 35.0
    assert four["est_err"] == 0.2 and four["spills"] == 1 and four["load1"] == 2.0 and four["errors"] == 0
    err = [r for r in out if r["instance"] == "cpu-246"][0]
    assert err["errors"] == 1
    assert out[0]["bucket"].startswith("2026-10-19T05"), "newest first"


def test_parameters_counts_what_the_calls_ran_with():
    p = core.parameters(ROWS)
    assert p["temperature"] == {"0.2": 2, "0.7": 1} and p["think"] == {"False": 1, "True": 1}


def test_every_generate_call_is_recorded_with_its_parameters_and_never_waits():
    src = open(os.path.join(ROOT, "vera", "capability_orchestration.py"), encoding="utf-8").read()
    assert '"options": dict(routing_info.get("options") or {}), "think": think, "json_mode": bool(json_mode),' in src
    fin = src[src.index("_inflight_release(inst, _req_slot_id)\n        _ollama_call_record(req_entry)"):][:200]
    assert "_ollama_call_record(req_entry)" in fin, "written in the finally - however the call ended"
    rec = src[src.index("def _ollama_call_record("):src.index("def _ollama_log_append(")]
    assert "create_task(_ollama_call_xadd(data))" in rec and "except Exception:" in rec
    assert "maxlen=OLLAMA_CALLS_MAXLEN, approximate=True" in rec
    assert 'OLLAMA_CALLS_KEY = "vera:ollama:calls"' in src and core.STREAM_KEY == "vera:ollama:calls"
    assert 'os.path.join(_here, "workers/ollama_calls_capabilities.py")' in src
