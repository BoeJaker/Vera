"""Every chat turn is recorded from its phase frames (four-minds FM-0.3)."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.agents import turn_phases_core as tp  # noqa: E402

pytestmark = pytest.mark.critical


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _turn(silent, triggered=False):
    c = Clock()
    ph = tp.PhaseClock("t1", 100.0, c, silent=silent)
    out = []
    if triggered:
        out.append(ph.sse("acquire", "web", "searching the web for x", "start"))
        c.t = 100.8
        out.append(ph.sse("acquire", "web", "searching the web for x", "done", ms=800, count=3))
    c.t = 101.2
    out.append(ph.sse("model", "answer", "writing the answer", "first"))
    c.t = 103.0
    out.append(ph.sse("model", "answer", "writing the answer", "done", ms=1800, count=420))
    return ph, out


def test_a_silent_clock_sends_nothing_but_logs_everything():
    ph, out = _turn(silent=True, triggered=True)
    assert out == [b"", b"", b"", b""]
    assert [f["state"] for f in ph.log] == ["start", "done", "first", "done"]
    assert ph.raw(ph.log[0]) == b""


def test_a_client_that_asked_still_gets_its_frames():
    ph, out = _turn(silent=False)
    assert all(o.startswith(b"data: ") for o in out) and len(ph.log) == 2


def test_turn_record_ttft_kind_and_phases():
    ph, _ = _turn(silent=True, triggered=True)
    rec = tp.turn_record(ph.log, agent="vera", elapsed_ms=3000, chars=420)
    assert rec["ttft_ms"] == 1200 and rec["kind"] == "triggered" and rec["turn_id"] == "t1"
    assert [p["state"] for p in rec["phases"]] == ["done", "first", "done"]
    plain = tp.turn_record(_turn(silent=True)[0].log, agent="vera", elapsed_ms=3000)
    assert plain["kind"] == "plain" and plain["ttft_ms"] == 1200


def test_no_token_turns_are_marked():
    ph = tp.PhaseClock("t2", 0.0, lambda: 0.0, silent=True)
    rec = tp.turn_record(ph.log, agent="vera", elapsed_ms=900)
    env = tp.as_envelope(rec)
    assert rec["ttft_ms"] is None and env["stages"][0] == {"stage": "ttft", "ms": 900.0, "outcome": "no-token"}


def test_envelope_feeds_the_timing_window():
    from vera.fabric import retrieval_timing_core as rtc
    w = rtc.Window(10)
    for silent in (True, True):
        ph, _ = _turn(silent=silent, triggered=True)
        w.add(tp.as_envelope(tp.turn_record(ph.log, agent="vera", elapsed_ms=3000, chars=1)))
    s = w.summary()["vera|triggered"]
    assert s["calls"] == 2 and s["stages"]["ttft"]["p50"] == 1200.0


def test_the_log_is_bounded():
    ph = tp.PhaseClock("t3", 0.0, lambda: 0.0, silent=True)
    for _ in range(tp.MAX_LOG + 20):
        ph.frame("acquire", "web", "x", "start")
    assert len(ph.log) == tp.MAX_LOG


def test_stream_wiring():
    src = open(os.path.join(ROOT, "vera", "agents", "agents.py"), encoding="utf-8").read()
    assert "silent=not body.get(\"phase_frames\"))" in src
    assert "_record_chat_turn(_ph, agent.name, elapsed_ms, _resp_chars, session_id)" in src
    assert "if _ph.silent:                      # no frames to order for this client" in src


def test_the_post_reply_step_is_said_and_usage_is_counted_once():
    src = open(os.path.join(ROOT, "vera", "agents", "agents.py"), encoding="utf-8").read()
    assert '_ph.sse("post", "memory-use", "checking which memories the answer used", "start")' in src
    assert "await asyncio.wait({_usage_t}, timeout=0.8)" in src         # bounded: the stream's end waits <= 0.8 s
    assert "if _usage_t is None:\n                _usage_start()" in src  # an early end still counts, once
    assert src.count("record_turn_usage(session_id") == 1


def test_post_steps_enter_the_turn_record():
    ph = tp.PhaseClock("t2", 0.0, clock=lambda: 2.0)
    ph.frame("model", "answer", "writing the answer", "done", ms=1500)
    ph.frame("post", "memory-use", "checking which memories the answer used", "done", ms=120,
             detail="used 2 of the 6 it was given")
    rec = tp.turn_record(ph.log, agent="assistant", elapsed_ms=2000)
    assert {"stage": "post", "id": "memory-use", "state": "done", "at_ms": 2000, "ms": 120} in rec["phases"]