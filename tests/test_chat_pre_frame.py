"""The chat PRE frame (four-minds FM-1.2): sense.frame per chat message behind sense.chat_pre, started with the turn,
never awaited before the model, recorded as a 'sense / frame' phase in the turn record."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

pytestmark = pytest.mark.critical


def _agents():
    return open(os.path.join(ROOT, "vera", "agents", "agents.py"), encoding="utf-8").read()


def test_the_frame_starts_only_behind_its_switch():
    src = _agents()
    i = src.index("_frame_task = None")
    block = src[i:i + 700]
    # one switchboard read per turn since D10-1 (cache=_sw)
    assert 'await _switch_mode("sense.chat_pre", body.get("minds"), cache=_sw) == "on"' in block
    assert 'asyncio.create_task(_sf_fn(text=_bare_q, kind="chat"))' in block
    assert '_sf.get("raw") or _sf.get("func")' in block


def test_the_model_never_waits_for_the_frame():
    src = _agents()
    # the only await on the frame task is inside its own phase reporter
    assert src.count("_frame_task)") == 1 and "asyncio.wait_for(asyncio.shield(_frame_task), 5.0)" in src
    assert "await _frame_task" not in src
    rep = src[src.index("async def _frame_phase_done("):src.index("async def _frame_phase_done(") + 1200]
    assert '_ph.sse("sense", "frame", "reading the message"' in rep


def test_the_frame_tasks_are_torn_down_with_the_stream():
    src = _agents()
    assert "_mem_task, _mem_phase,\n                       _frame_task, _frame_phase):" in src


def test_the_turn_record_keeps_the_sense_phase():
    from vera.agents import turn_phases_core as TP
    clock = TP.PhaseClock("t1", 0.0, clock=lambda: 0.05)
    f = clock.frame("sense", "frame", "reading the message", "done", ms=38, detail="markets")
    rec = TP.turn_record([f], agent="assistant", elapsed_ms=900)
    assert {"stage": "sense", "id": "frame", "state": "done", "at_ms": 50, "ms": 38} in rec["phases"]
    assert rec["kind"] == "plain"                    # a frame is not context acquisition


def test_the_switch_is_registered_off():
    from vera.minds import switchboard_core as SB
    f = SB.REGISTRY["sense.chat_pre"]
    assert f.modes == ("off", "on") and f.default == "off" and f.package == "FM-1.2"
