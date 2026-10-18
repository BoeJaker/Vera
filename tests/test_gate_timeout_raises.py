"""S1 / D03-1: a GPU-gate queue timeout must refuse the generation, not run it ungated.

GateAcquisitionError's message is "required inference lease unavailable: queue_timeout"; the slot code compared
the whole message with "queue_timeout", never matched, logged "proceeding UNGATED" and generated anyway. These
tests drive the real _ollama_slot against a fake coordinator Redis.

Runs in the gate container, where Vera.vera resolves to the branch."""
import asyncio
import os
import sys

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import Vera.vera.capability_orchestration as orch  # noqa: E402  (fails loudly, never skips)


class BusyRedis:
    """Every slot is held by someone else: SET NX never succeeds."""
    async def set(self, *a, **kw):
        return False


class BrokenRedis:
    async def set(self, *a, **kw):
        raise ConnectionError("redis down")


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setattr(orch, "_GATE_ON", True)
    monkeypatch.setattr(orch, "_GATE_BROKER_CONFIGURED", False)
    monkeypatch.setattr(orch._gate, "capacity_for", lambda has_gpu, env=None: 1)
    monkeypatch.setattr(orch, "_node_has_gpu", lambda iid: True)

    async def no_ensure():
        return None
    monkeypatch.setattr(orch, "_ensure_coord_redis", no_ensure)
    return monkeypatch


async def _enter(iid):
    async with orch._ollama_slot(iid, timeout=5, gate_wait=0.2):
        return "generated"


def test_a_queue_timeout_refuses_instead_of_generating_ungated(gated):
    gated.setattr(orch, "COORD_REDIS", BusyRedis())
    with pytest.raises(Exception) as e:
        asyncio.run(_enter("s1-busy-node"))
    assert "gpu gate timeout on s1-busy-node" in str(e.value)


def test_a_broken_coordinator_still_proceeds_ungated_by_design(gated):
    gated.setattr(orch, "COORD_REDIS", BrokenRedis())
    assert asyncio.run(_enter("s1-broken-node")) == "generated"


def test_the_local_permit_is_released_after_the_refusal(gated):
    gated.setattr(orch, "COORD_REDIS", BusyRedis())

    async def three():
        out = []
        for _ in range(3):                   # one loop: a leaked permit would make attempt 2 wait out its timeout
            t0 = asyncio.get_running_loop().time()
            try:
                await _enter("s1-permit-node")
                out.append("generated")
            except Exception as e:  # noqa: BLE001
                out.append((str(e)[:24], round(asyncio.get_running_loop().time() - t0, 1)))
        return out
    res = asyncio.run(three())
    assert all(isinstance(r, tuple) and r[0] == "gpu gate timeout on s1-p" and r[1] < 2 for r in res), res
