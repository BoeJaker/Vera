"""Cooperative-cancel helper (_loop_run_cancelled) — the 2026-08-17 orphan-loop fix.

A cancelled loop must stop even when the runner task-handle can't reach an orphaned
coroutine: the loop cycle checks vera:loop:run:<sid> status each turn. These pin the
helper's status logic and its FAIL-OPEN behaviour (a Redis error must NEVER abort a
live run). Imports the monolith module, so it runs in-container, not the pure gate.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import dag_workshop_capabilities as W  # noqa: E402


class _FakeR:
    def __init__(self, status=None, raise_=False):
        self._status = status
        self._raise = raise_

    async def hget(self, key, field):
        if self._raise:
            raise RuntimeError("redis down")
        return self._status


def _check(status=None, raise_=False, none_client=False, sid="s1"):
    orig = W._redis
    try:
        W._redis = (lambda: None) if none_client else (lambda: _FakeR(status, raise_))
        return asyncio.run(W._loop_run_cancelled(sid))
    finally:
        W._redis = orig


def test_cancelled_statuses_detected():
    assert _check("cancelled") is True
    assert _check("canceled") is True
    assert _check("stopped") is True
    assert _check("  CANCELLED  ") is True      # whitespace + case tolerant
    assert _check(b"cancelled") is True          # bytes straight from redis


def test_live_and_terminal_non_cancel_statuses_are_not_cancel():
    assert _check("running") is False
    assert _check("done") is False               # normal completion is NOT a cancel
    assert _check("error") is False
    assert _check("") is False
    assert _check(None) is False


def test_fail_open_never_aborts_a_live_run():
    assert _check(raise_=True) is False          # read error -> False (don't kill a live run)
    assert _check(none_client=True) is False     # no redis client -> False
    assert _check("cancelled", sid="") is False  # empty sid -> False
