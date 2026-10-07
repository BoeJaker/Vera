"""Phase frames for the chat stream (vera/agents/turn_phases_core.py): the shape agreed with the chat turn record."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.agents import turn_phases_core as tp  # noqa: E402


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def test_frame_has_the_agreed_fields():
    c = Clock(10.0)
    pc = tp.PhaseClock("sid:m3", 10.0, clock=c)
    c.t = 10.25
    f = pc.frame("acquire", "web", "searching the web", "start")
    assert f == {"type": "phase", "turn_id": "sid:m3", "id": "web", "stage": "acquire",
                 "label": "searching the web", "state": "start", "at_ms": 250}


def test_duration_only_on_finished_states():
    pc = tp.PhaseClock("t", 0.0, clock=Clock(1.0))
    assert "ms" not in pc.frame("model", "answer", "x", "start", ms=5)
    assert pc.frame("model", "answer", "x", "done", ms=1234.4)["ms"] == 1234
    assert pc.frame("acquire", "web", "x", "fail", ms=3)["ms"] == 3


def test_first_is_the_model_stage_only():
    pc = tp.PhaseClock("t", 0.0, clock=Clock(0.4))
    assert pc.frame("model", "answer", "x", "first")["at_ms"] == 400
    with pytest.raises(ValueError):
        pc.frame("acquire", "web", "x", "first")


def test_unknown_stage_or_state_is_refused():
    pc = tp.PhaseClock("t", 0.0)
    with pytest.raises(ValueError):
        pc.frame("thinking", "x", "x", "start")
    with pytest.raises(ValueError):
        pc.frame("model", "x", "x", "running")


def test_optional_fields_are_cleaned_and_bounded():
    pc = tp.PhaseClock("t", 0.0, clock=Clock(0.0))
    f = pc.frame("acquire", "web", "  searching\n the   web ", "done", ms=10, count="3", detail="",
                 sources=[{"title": "A " * 200, "url": "https://a"}, "junk", {"url": "https://b"}] + [{"url": "u"}] * 20)
    assert f["label"] == "searching the web"
    assert f["count"] == 3 and "detail" not in f
    assert len(f["sources"]) == 9            # ten offered, one not a source
    assert len(f["sources"][0]["title"]) <= 140 and f["sources"][1]["title"] == ""


def test_sse_is_one_event():
    raw = tp.PhaseClock("t", 0.0, clock=Clock(0.0)).sse("model", "answer", "writing", "start")
    assert raw.startswith(b"data: ") and raw.endswith(b"\n\n")
    assert json.loads(raw[6:].decode())["type"] == "phase"


def test_marks_become_done_frames_with_their_real_times():
    pc = tp.PhaseClock("t", 100.0, clock=Clock(100.0))
    marks = [("agent_lookup", 0.020), ("begin_activity", 0.300), ("context_inject", 0.150),
             ("canvas_prepare", 1.2), ("unknown", 5.0)]
    fr = tp.from_marks(pc, marks)
    assert [f["id"] for f in fr] == ["agent", "notes", "canvas"]      # internal and unknown marks are not shown
    assert [f["ms"] for f in fr] == [20, 150, 1200]
    assert [f["at_ms"] for f in fr] == [20, 470, 1670]                 # where each ended, from the turn's start
    assert all(f["state"] == "done" for f in fr)


def test_tiny_steps_are_left_out():
    pc = tp.PhaseClock("t", 0.0, clock=Clock(0.0))
    assert tp.from_marks(pc, [("agent_lookup", 0.0004)], min_ms=1) == []


def test_web_label():
    assert tp.web_label("bitcoin price today") == "searching the web for “bitcoin price today”"
    assert tp.web_label("") == "searching the web"


def _gate(*nodes):
    return {"nodes": [dict(n, gated=True) for n in nodes]}


def test_a_full_gpu_says_the_answer_waits_for_it():
    g = _gate({"node": "gpu-250", "has_gpu": True, "capacity": 1, "held": 1},
              {"node": "cpu-246", "has_gpu": False, "capacity": 2, "held": 0})
    q = tp.gpu_queue(g, "gpu-250")
    assert q["label"] == "waiting for the GPU" and q["detail"] == "gpu-250 busy with 1 other job"
    assert tp.gpu_queue(g, "") is not None                       # unknown node: the GPU nodes decide


def test_a_free_slot_or_nothing_known_says_nothing():
    assert tp.gpu_queue(_gate({"node": "gpu-250", "has_gpu": True, "capacity": 1, "held": 0}), "gpu-250") is None
    assert tp.gpu_queue({}, "gpu-250") is None and tp.gpu_queue(None) is None
    # the answer runs on a CPU node with room: a busy GPU is not its queue
    g = _gate({"node": "gpu-250", "has_gpu": True, "capacity": 1, "held": 1},
              {"node": "cpu-246", "has_gpu": False, "capacity": 2, "held": 1})
    assert tp.gpu_queue(g, "cpu-246") is None


def test_the_wait_is_a_model_step_in_the_frame_shape():
    clock = tp.PhaseClock("t1", 0.0, clock=lambda: 1.5)
    f = clock.frame("model", "queue", "waiting for the GPU", "done", ms=1200, detail="until the first word")
    assert f["stage"] == "model" and f["id"] == "queue" and f["ms"] == 1200