"""Regression guard for ``agents._wait_for_backing_store`` — the boot-seed gate.

Fresh instances (notably dev sandboxes) came up with 0 agents: ``_startup``
seeded before the shared Redis client was connected, so ``AgentRegistry.save()``
no-oped its Redis write and the seed was silently lost (agents lived only in the
in-memory cache; later ``_startup`` runs found them cached and skipped
re-saving, so ``list_all`` — which reads Redis — returned 0). The fix waits for
Redis before seeding. These pin the wait's contract: it returns True once Redis
is present, returns False (never hangs) on timeout, and re-checks over time.

Pure/deterministic — it only polls the module's ``_redis`` accessor, which the
tests monkeypatch, so no real backing store is touched.
"""
import asyncio
import time

import pytest

from vera.agents import agents

pytestmark = pytest.mark.critical


def test_returns_true_immediately_when_redis_present(monkeypatch):
    monkeypatch.setattr(agents, "_redis", lambda: object())
    assert asyncio.run(agents._wait_for_backing_store(timeout=5)) is True


def test_returns_true_once_redis_appears(monkeypatch):
    # None for the first couple of polls, then a client shows up
    calls = {"n": 0}

    def fake_redis():
        calls["n"] += 1
        return None if calls["n"] < 3 else object()

    monkeypatch.setattr(agents, "_redis", fake_redis)
    assert asyncio.run(agents._wait_for_backing_store(timeout=10)) is True
    assert calls["n"] >= 3


def test_returns_false_on_timeout_without_hanging(monkeypatch):
    monkeypatch.setattr(agents, "_redis", lambda: None)
    t0 = time.monotonic()
    result = asyncio.run(agents._wait_for_backing_store(timeout=1.0))
    assert result is False
    assert time.monotonic() - t0 < 5.0  # bounded — didn't hang
