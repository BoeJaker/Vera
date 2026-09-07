"""One prompt at a time per session, in the order they were asked.

The gateway accepts a second `chat.send` into a busy session and then gives it
nothing — no deltas, an empty final message (observed on 2026.4.29, which also
refuses the `queueMode` property that would have ordered them gateway-side).
So the ordering lives here, and these tests pin the bookkeeping that decides
what may be sent when.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.openclaw import openclaw_device_core as dev  # noqa: E402

SESSION = "vera-bridge"
OTHER = "another-session"


def prompt(queue_id, message="hi", session=SESSION):
    return dev.QueuedPrompt(queue_id=queue_id, session_key=session,
                            message=message, queued_at="2026-09-07T00:00:00Z")


# ── order and position ───────────────────────────────────────────────────────

def test_position_counts_everything_ahead_of_it():
    queue = dev.PromptQueue()
    assert queue.add(prompt("a")) == 1
    assert queue.add(prompt("b")) == 2
    assert queue.add(prompt("c")) == 3


def test_position_includes_the_run_already_in_flight():
    queue = dev.PromptQueue()
    queue.add(prompt("a"))
    queue.mark_sent("a", "run-a")
    assert queue.add(prompt("b")) == 2       # behind the active run


def test_prompts_leave_in_the_order_they_arrived():
    queue = dev.PromptQueue()
    for qid in ("a", "b", "c"):
        queue.add(prompt(qid))
    seen = []
    for run in ("run-a", "run-b", "run-c"):
        nxt = queue.next_ready(SESSION)
        seen.append(nxt.queue_id)
        queue.mark_sent(nxt.queue_id, run)
        queue.complete(run)
    assert seen == ["a", "b", "c"]


# ── one at a time ────────────────────────────────────────────────────────────

def test_nothing_is_ready_while_a_run_is_in_flight():
    queue = dev.PromptQueue()
    queue.add(prompt("a"))
    queue.add(prompt("b"))
    queue.mark_sent("a", "run-a")
    assert queue.is_busy(SESSION)
    assert queue.next_ready(SESSION) is None      # b waits its turn


def test_finishing_a_run_releases_the_next():
    queue = dev.PromptQueue()
    queue.add(prompt("a"))
    queue.add(prompt("b"))
    queue.mark_sent("a", "run-a")
    queue.complete("run-a")
    assert not queue.is_busy(SESSION)
    assert queue.next_ready(SESSION).queue_id == "b"


def test_sessions_do_not_block_each_other():
    queue = dev.PromptQueue()
    queue.add(prompt("a"))
    queue.add(prompt("x", session=OTHER))
    queue.mark_sent("a", "run-a")
    assert queue.next_ready(SESSION) is None
    assert queue.next_ready(OTHER).queue_id == "x"


def test_an_empty_session_has_nothing_ready():
    assert dev.PromptQueue().next_ready(SESSION) is None


# ── completion, failure, and never getting an answer ─────────────────────────

def test_a_completed_prompt_reports_done():
    queue = dev.PromptQueue()
    queue.add(prompt("a"))
    queue.mark_sent("a", "run-a")
    finished = queue.complete("run-a", "final")
    assert finished.status == dev.PROMPT_DONE and finished.run_id == "run-a"


def test_an_aborted_run_is_a_failure_that_still_frees_the_session():
    queue = dev.PromptQueue()
    queue.add(prompt("a"))
    queue.add(prompt("b"))
    queue.mark_sent("a", "run-a")
    finished = queue.complete("run-a", "aborted")
    assert finished.status == dev.PROMPT_FAILED and finished.error == "aborted"
    assert queue.next_ready(SESSION).queue_id == "b"


def test_a_run_nobody_queued_still_frees_its_session():
    """Another client's run, or one sent with queue=false."""
    queue = dev.PromptQueue()
    assert queue.complete("run-unknown") is None


def test_a_stalled_run_is_abandoned_so_the_queue_moves_on():
    queue = dev.PromptQueue()
    queue.add(prompt("a"))
    queue.add(prompt("b"))
    queue.mark_sent("a", "run-a")
    stalled = queue.fail_active(SESSION, "run timed out")
    assert stalled.status == dev.PROMPT_INTERRUPTED
    assert stalled.error == "run timed out"
    assert queue.next_ready(SESSION).queue_id == "b"


def test_a_dropped_connection_loses_only_what_was_in_flight():
    queue = dev.PromptQueue()
    queue.add(prompt("a"))
    queue.add(prompt("b"))
    queue.add(prompt("x", session=OTHER))
    queue.mark_sent("a", "run-a")
    queue.mark_sent("x", "run-x")

    lost = queue.abandon_all("connection dropped")
    assert {p.queue_id for p in lost} == {"a", "x"}
    # The ones that never went out keep their place.
    assert queue.next_ready(SESSION).queue_id == "b"
    assert queue.waiting(SESSION) == 1


# ── cancelling ───────────────────────────────────────────────────────────────

def test_a_waiting_prompt_can_be_cancelled():
    queue = dev.PromptQueue()
    queue.add(prompt("a"))
    queue.add(prompt("b"))
    cancelled = queue.cancel("b")
    assert cancelled.status == dev.PROMPT_CANCELLED
    assert queue.waiting(SESSION) == 1


def test_a_prompt_already_with_the_gateway_cannot_be_recalled():
    queue = dev.PromptQueue()
    queue.add(prompt("a"))
    queue.mark_sent("a", "run-a")
    assert queue.cancel("a") is None
    assert queue.find("a").status == dev.PROMPT_SENT


def test_cancelling_something_unknown_says_so():
    assert dev.PromptQueue().cancel("nope") is None


# ── what the outside sees ────────────────────────────────────────────────────

def test_a_run_can_be_traced_back_to_its_queue_id():
    queue = dev.PromptQueue()
    queue.add(prompt("a"))
    queue.mark_sent("a", "run-a")
    assert queue.queue_id_for_run("run-a") == "a"
    assert queue.queue_id_for_run("run-other") == ""


def test_the_snapshot_shows_active_and_waiting_without_message_bodies():
    queue = dev.PromptQueue()
    queue.add(prompt("a", message="a secret question"))
    queue.add(prompt("b"))
    queue.mark_sent("a", "run-a")

    snap = queue.snapshot()
    assert set(snap) == {SESSION}
    assert snap[SESSION]["active"]["queue_id"] == "a"
    assert snap[SESSION]["active"]["run_id"] == "run-a"
    assert [w["queue_id"] for w in snap[SESSION]["waiting"]] == ["b"]
    assert "a secret question" not in str(snap)
    assert snap[SESSION]["active"]["chars"] == len("a secret question")


def test_an_idle_bridge_reports_an_empty_queue():
    queue = dev.PromptQueue()
    queue.add(prompt("a"))
    queue.mark_sent("a", "run-a")
    queue.complete("run-a")
    assert queue.snapshot() == {}
    assert queue.sessions() == []


# ── the gateway-side mode, where it exists ───────────────────────────────────

def test_queue_mode_is_only_sent_when_asked_for():
    plain = dev.build_chat_send_params(session_key=SESSION, message="m",
                                       idempotency_key="k")
    assert "queueMode" not in plain
    queued = dev.build_chat_send_params(session_key=SESSION, message="m",
                                        idempotency_key="k",
                                        queue_mode="followup")
    assert queued["queueMode"] == "followup"


def test_the_modes_are_the_ones_the_protocol_defines():
    assert dev.QUEUE_MODES == ("steer", "followup", "collect", "interrupt")
