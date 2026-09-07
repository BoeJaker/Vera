"""Two prompts in one session must not share a buffer.

Observed on the live bridge (2026-09-07): "alpha" and "beta" were sent while
the model was busy, so both runs finalized four seconds apart. With deltas
accumulated per SESSION, the first run's response carried "alphabeta" and the
second carried "" — each run's fallback text was whatever the shared buffer
happened to hold.

The run is the unit of an answer. The session is only a fallback for a frame
that carries no runId.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.openclaw import openclaw_device_core as dev  # noqa: E402

SESSION = "vera-bridge"
RUN_A = "b3b30c91dfad441f950ac383cee649e9"
RUN_B = "d1df5ef0581d42959dad8eb485a6ad4f"


def test_interleaved_runs_keep_their_own_text():
    buffers = dev.RunBuffers()
    # Both runs stream into the same session, tokens interleaved.
    buffers.append(RUN_A, SESSION, "al")
    buffers.append(RUN_B, SESSION, "be")
    buffers.append(RUN_A, SESSION, "pha")
    buffers.append(RUN_B, SESSION, "ta")

    assert buffers.take(RUN_A, SESSION) == "alpha"
    assert buffers.take(RUN_B, SESSION) == "beta"


def test_the_regression_shape_exactly():
    """The old behaviour, asserted as the thing that must not happen."""
    buffers = dev.RunBuffers()
    buffers.append(RUN_A, SESSION, "alpha")
    buffers.append(RUN_B, SESSION, "beta")
    first = buffers.take(RUN_A, SESSION)
    second = buffers.take(RUN_B, SESSION)
    assert first != "alphabeta"
    assert second != ""
    assert (first, second) == ("alpha", "beta")


def test_taking_a_run_twice_yields_nothing_the_second_time():
    buffers = dev.RunBuffers()
    buffers.append(RUN_A, SESSION, "alpha")
    assert buffers.take(RUN_A, SESSION) == "alpha"
    assert buffers.take(RUN_A, SESSION) == ""


def test_a_frame_without_a_run_id_falls_back_to_the_session():
    buffers = dev.RunBuffers()
    buffers.append("", SESSION, "orphan")
    assert buffers.take("", SESSION) == "orphan"


def test_a_run_id_beats_the_session_it_arrived_on():
    assert dev.RunBuffers.key(RUN_A, SESSION) == RUN_A
    assert dev.RunBuffers.key("", SESSION) == SESSION
    assert dev.RunBuffers.key("", "") == ""


def test_empty_deltas_open_no_buffer():
    buffers = dev.RunBuffers()
    buffers.append(RUN_A, SESSION, "")
    assert buffers.pending() == 0


def test_open_buffers_are_countable():
    buffers = dev.RunBuffers()
    buffers.append(RUN_A, SESSION, "a")
    buffers.append(RUN_B, SESSION, "b")
    assert buffers.pending() == 2
    buffers.take(RUN_A, SESSION)
    assert buffers.pending() == 1


def test_a_dropped_connection_discards_partial_answers():
    """Partials must never be spliced onto whatever run comes next."""
    buffers = dev.RunBuffers()
    buffers.append(RUN_A, SESSION, "half an ans")
    buffers.clear()
    assert buffers.pending() == 0
    assert buffers.take(RUN_A, SESSION) == ""


def test_a_full_exchange_of_two_overlapping_runs():
    """Replay both runs through the reader's own dispatch helpers."""
    buffers = dev.RunBuffers()
    frames = [
        ("agent", {"runId": RUN_A, "sessionKey": SESSION, "stream": "assistant",
                   "data": {"delta": "alpha", "text": "alpha"}}),
        ("agent", {"runId": RUN_B, "sessionKey": SESSION, "stream": "assistant",
                   "data": {"delta": "beta", "text": "beta"}}),
        # The gateway finalized the second run first, with an empty message.
        ("chat", {"runId": RUN_B, "sessionKey": SESSION, "state": "final",
                  "message": {"role": "assistant", "content": []}}),
        ("chat", {"runId": RUN_A, "sessionKey": SESSION, "state": "final",
                  "message": {"role": "assistant", "content": []}}),
    ]
    answers = {}
    for event, payload in frames:
        run_id = payload.get("runId", "")
        delta = dev.stream_delta(event, payload)
        if delta:
            buffers.append(run_id, SESSION, delta)
        terminal = dev.final_answer(event, payload)
        if terminal:
            _, text = terminal
            answers[run_id] = text or buffers.take(run_id, SESSION)

    assert answers == {RUN_A: "alpha", RUN_B: "beta"}
    assert buffers.pending() == 0
