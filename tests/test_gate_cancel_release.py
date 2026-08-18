"""Gate heartbeat frees the GPU slot the moment its run is cancelled.

The short-TTL heartbeat (test_gate_renew) self-heals a DEAD holder, but it did
NOT cover a run that is cancelled while STILL generating: a cancelled/runaway
loop keeps doing generations, each re-acquiring the slot and the heartbeat
renewing it, so it holds the GPU indefinitely — observed live 2026-08-18 (a
cancelled test run kept the GPU busy). The heartbeat now polls the run's cancel
state and RELEASES the slot at once when the run is cancelled. This pins the
cancel-detection primitive `_run_is_cancelled`.

Imports the monolith orchestration module, so it runs in-container.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera import capability_orchestration as O  # noqa: E402


class _FakeRedis:
    def __init__(self, status):
        self._status = status

    async def hget(self, key, field):
        assert key.startswith("vera:loop:run:")
        assert field == "status"
        return self._status


def _cancelled(status, monkeypatch):
    monkeypatch.setattr(O, "REDIS", _FakeRedis(status))
    return asyncio.run(O._run_is_cancelled("sid-1"))


def test_cancelled_and_stopped_statuses_free_the_slot(monkeypatch):
    for st in ("cancelled", "canceled", "stopped", "  STOPPED  ", b"cancelled"):
        assert _cancelled(st, monkeypatch) is True, st


def test_live_statuses_keep_the_slot(monkeypatch):
    for st in ("running", "done", "error", "", None):
        assert _cancelled(st, monkeypatch) is False, st


def test_no_session_is_not_cancelled(monkeypatch):
    monkeypatch.setattr(O, "REDIS", _FakeRedis("cancelled"))
    assert asyncio.run(O._run_is_cancelled("")) is False


def test_redis_none_is_fail_open_not_cancelled(monkeypatch):
    # No Redis → can't confirm a cancel → DON'T free a live slot (fail-open).
    monkeypatch.setattr(O, "REDIS", None)
    assert asyncio.run(O._run_is_cancelled("sid-1")) is False


def test_redis_error_is_safe(monkeypatch):
    class _Boom:
        async def hget(self, *a):
            raise RuntimeError("redis down")
    monkeypatch.setattr(O, "REDIS", _Boom())
    assert asyncio.run(O._run_is_cancelled("sid-1")) is False
