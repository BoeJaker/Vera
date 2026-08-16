"""Critical-tier tests for board.sync decision logic (vera/board/board_core.py, M4).

board.sync reflects a linked evolve pipeline's state onto its board item — lane +
an idempotent progress comment. The lane mapping, the idempotency fingerprint, and
the human-parked-lane protection are the parts a regression would silently corrupt
board state, so they live as pure functions and are guarded here. No app/IO.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.board import board_core as bc  # noqa: E402


# ── pipeline_lane: decision/gate/review -> lane ──────────────────────────────

def test_promoted_pipeline_maps_to_done():
    assert bc.pipeline_lane({"decision": "promoted"}) == "done"


def test_rolled_back_pipeline_maps_to_dropped():
    # regression guard: a discarded pipeline must NOT land back in in_progress
    assert bc.pipeline_lane({"decision": "rolled_back"}) == "dropped"


def test_held_pipeline_maps_to_blocked():
    assert bc.pipeline_lane({"decision": "held"}) == "blocked"


def test_review_requested_maps_to_needs_review():
    assert bc.pipeline_lane({"decision": "pending", "review_requested": True}) == "needs_review"


def test_promoted_beats_review_requested():
    # a terminal decision wins even if review was once requested
    assert bc.pipeline_lane({"decision": "promoted", "review_requested": True}) == "done"


def test_pending_pipeline_stays_in_progress():
    assert bc.pipeline_lane({"decision": "pending"}) == "in_progress"
    assert bc.pipeline_lane({}) == "in_progress"        # missing decision defaults pending


# ── pipeline_sync_sig: idempotency fingerprint ───────────────────────────────

def test_sig_is_stable_for_same_state():
    rec = {"decision": "pending", "gate_passed": True, "review_requested": False}
    assert bc.pipeline_sync_sig(rec, "needs_review") == bc.pipeline_sync_sig(rec, "needs_review")


def test_sig_changes_when_gate_flips():
    a = bc.pipeline_sync_sig({"decision": "pending", "gate_passed": False}, "in_progress")
    b = bc.pipeline_sync_sig({"decision": "pending", "gate_passed": True}, "in_progress")
    assert a != b


def test_sig_changes_when_lane_changes():
    rec = {"decision": "promoted", "gate_passed": True}
    assert bc.pipeline_sync_sig(rec, "done") != bc.pipeline_sync_sig(rec, "in_progress")


def test_sig_normalizes_review_requested_truthiness():
    # bool(...) so a missing flag and an explicit False fingerprint identically
    assert bc.pipeline_sync_sig({"decision": "pending"}, "in_progress") == \
        bc.pipeline_sync_sig({"decision": "pending", "review_requested": False}, "in_progress")


# ── should_apply_lane: never yank an item out of a human-owned terminal lane ──

def test_moves_when_lane_differs_and_not_terminal():
    assert bc.should_apply_lane("in_progress", "needs_review") is True


def test_no_move_when_lane_same():
    assert bc.should_apply_lane("done", "done") is False


def test_never_yanks_out_of_done_or_dropped():
    # a human parked it in done/dropped — reflect INTO these, never back OUT
    assert bc.should_apply_lane("done", "in_progress") is False
    assert bc.should_apply_lane("dropped", "blocked") is False
