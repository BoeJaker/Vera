"""Required inference leases cannot silently degrade to unslotted generation."""
import asyncio

import pytest

from vera.ollama_gate import GateAcquisitionError, acquire, release

pytestmark = pytest.mark.critical


class Redis:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, nx, px):
        assert nx and px > 0
        if key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, script, count, key, owner):
        if self.values.get(key) == owner:
            del self.values[key]
            return 1
        return 0


@pytest.mark.parametrize('redis,capacity,reason', [
    (None, 1, 'coordination_unavailable'),
    (Redis(), 0, 'ungated_node'),
])
def test_required_missing_coordination_or_capacity_refuses(redis, capacity, reason):
    async def run():
        with pytest.raises(GateAcquisitionError) as error:
            await acquire(redis, 'gpu', capacity, 1000, 0, required=True)
        assert error.value.reason == reason
        assert await acquire(redis, 'gpu', capacity, 1000, 0) is None
    asyncio.run(run())


def test_required_transport_failure_is_safe_and_legacy_stays_open():
    class Broken:
        async def set(self, *args, **kwargs):
            raise RuntimeError('redis://secret-password@private-host')

    async def run():
        with pytest.raises(GateAcquisitionError) as error:
            await acquire(Broken(), 'gpu', 1, 1000, 0, required=True)
        assert error.value.reason == 'coordination_error'
        assert 'secret' not in str(error.value)
        assert await acquire(Broken(), 'gpu', 1, 1000, 0) is None
    asyncio.run(run())


def test_required_and_legacy_callers_share_slots_and_owner_fencing():
    async def run():
        redis = Redis()
        prod = await acquire(redis, 'gpu', 1, 1000, 0, owner='prod')
        with pytest.raises(GateAcquisitionError, match='queue_timeout'):
            await acquire(redis, 'gpu', 1, 1000, 0, required=True)
        assert await release(redis, prod)
        sandbox = await acquire(redis, 'gpu', 1, 1000, 0, required=True)
        assert sandbox['key'] == prod['key']
        assert not await release(redis, prod)
        assert await release(redis, sandbox)
    asyncio.run(run())


def test_cancelled_required_waiter_does_not_release_another_owner():
    async def run():
        redis = Redis()
        lease = await acquire(redis, 'gpu', 1, 1000, 0)
        waiter = asyncio.create_task(acquire(redis, 'gpu', 1, 1000, 60, required=True))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert redis.values[lease['key']] == lease['owner']
    asyncio.run(run())


@pytest.mark.parametrize('changes', [
    {'wait': float('inf')}, {'wait': -1}, {'wait': float('nan')},
    {'poll': 0}, {'poll': float('nan')}, {'ttl': 0}, {'node': ''},
])
def test_required_invalid_policy_refuses_before_io(changes):
    async def run():
        args = dict(node='gpu', capacity=1, ttl=1000, wait=0, poll=0.25)
        args.update(changes)
        redis = Redis()
        with pytest.raises(GateAcquisitionError, match='invalid_policy'):
            await acquire(redis, **args, required=True)
        assert not redis.values
    asyncio.run(run())
