import asyncio

import pytest

from vera.evolve.singleflight import SingleFlight


@pytest.mark.asyncio
@pytest.mark.critical
async def test_concurrent_callers_share_one_expensive_operation():
    gate = SingleFlight()
    started = 0
    release = asyncio.Event()

    async def operation():
        nonlocal started
        started += 1
        await release.wait()
        return "green"

    first = asyncio.create_task(gate.run("same", operation))
    await asyncio.sleep(0)
    second = asyncio.create_task(gate.run("same", operation))
    await asyncio.sleep(0)
    assert started == 1
    release.set()
    assert await first == ("green", False)
    assert await second == ("green", True)


@pytest.mark.asyncio
@pytest.mark.critical
async def test_response_cancellation_does_not_orphan_or_duplicate_operation():
    gate = SingleFlight()
    started = 0
    release = asyncio.Event()

    async def operation():
        nonlocal started
        started += 1
        await release.wait()
        return 4495

    caller = asyncio.create_task(gate.run("same", operation))
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    retry = asyncio.create_task(gate.run("same", operation))
    await asyncio.sleep(0)
    assert started == 1
    release.set()
    assert await retry == (4495, True)


@pytest.mark.asyncio
@pytest.mark.critical
async def test_failed_operation_does_not_poison_later_attempts():
    gate = SingleFlight()

    async def failure():
        raise RuntimeError("failed")

    with pytest.raises(RuntimeError, match="failed"):
        await gate.run("same", failure)

    async def success():
        return "recovered"

    assert await gate.run("same", success) == ("recovered", False)
