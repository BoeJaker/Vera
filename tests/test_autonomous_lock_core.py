"""Critical-tier tests for the autonomous-mode hard main-lockout (Phase A).

This is the safety seal for the closed loop: while autonomous mode is engaged,
promoting/merging into the real mainline must be IMPOSSIBLE — unconditional, no
sentinel, no force. A regression here would let an autonomous loop reach prod, so
the decision is pure and guarded. No I/O.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.evolve import autonomous_lock_core as al  # noqa: E402


# ── is_engaged ───────────────────────────────────────────────────────────────

def test_engaged_truthy_values():
    for v in ("on", "engaged", "1", "true", "yes", "ON", " On "):
        assert al.is_engaged({"mode": v}), v


def test_not_engaged_values():
    for v in ("off", "", "no", "0", None):
        assert not al.is_engaged({"mode": v}), v
    assert not al.is_engaged({})
    assert not al.is_engaged(None)


# ── main_is_locked ───────────────────────────────────────────────────────────

ENGAGED = {"mode": "on", "since": "2026-08-16T00:00:00Z", "reason": "closed loop"}


def test_main_and_master_locked_when_engaged():
    assert al.main_is_locked(ENGAGED, "main")["locked"] is True
    assert al.main_is_locked(ENGAGED, "master")["locked"] is True


def test_resolved_mainline_locked_even_if_not_named_main():
    # a repo whose mainline is 'trunk' is still sealed
    assert al.main_is_locked(ENGAGED, "trunk", mainline="trunk")["locked"] is True


def test_bleeding_edge_and_features_never_locked():
    # the loop must keep landing on bleeding-edge / feature branches
    assert al.main_is_locked(ENGAGED, "bleeding-edge")["locked"] is False
    assert al.main_is_locked(ENGAGED, "feat/x")["locked"] is False
    assert al.main_is_locked(ENGAGED, "fix/y")["locked"] is False


def test_not_locked_when_disengaged():
    assert al.main_is_locked({"mode": "off"}, "main")["locked"] is False
    assert al.main_is_locked({}, "main")["locked"] is False


def test_lock_reason_is_loud_and_names_the_override_is_useless():
    r = al.main_is_locked(ENGAGED, "main")
    assert "AUTONOMOUS MODE ENGAGED" in r["reason"]
    assert "LOCKED" in r["reason"]
    assert "authorize_main or force" in r["reason"]   # makes clear no bypass exists
    assert "autonomous.release" in r["reason"]         # tells a human how to unlock


def test_target_is_case_and_whitespace_insensitive():
    assert al.main_is_locked(ENGAGED, "  MAIN  ")["locked"] is True
