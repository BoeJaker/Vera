"""S7 / D01-2: a startup hook registered both ways starts once.

About ten modules start their startup coroutine with start_at_import AND register the same function with
schedule(..., interval=STARTUP_INTERVAL), usually under another name; the scheduler then started a second copy (two
cluster pollers, two job listeners, agents re-saved twice per boot). Runs in the gate container."""
import asyncio
import os
import sys

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import Vera.vera.capability_orchestration as orch  # noqa: E402  (fails loudly, never skips)
from Vera.vera import scheduler_leadership as lead  # noqa: E402

ONCE = 999999


def test_a_hook_started_at_import_is_not_started_again_by_the_scheduler(monkeypatch):
    starts = {"both": 0, "scheduled_only": 0, "periodic": 0}

    async def both():
        starts["both"] += 1

    async def scheduled_only():
        starts["scheduled_only"] += 1

    async def periodic():
        starts["periodic"] += 1

    monkeypatch.setattr(orch, "_STARTED_AT_IMPORT", set())
    monkeypatch.setattr(orch, "SCHEDULED_TASKS", [
        {"name": "s7_both_scheduled_name", "fn": both, "int": ONCE, "last": None, "runs": 0},
        {"name": "s7_scheduled_only", "fn": scheduled_only, "int": ONCE, "last": None, "runs": 0},
        {"name": "s7_periodic_same_fn", "fn": both, "int": 1, "last": None, "runs": 0},  # periodic: never suppressed
        {"name": "s7_periodic", "fn": periodic, "int": 1, "last": None, "runs": 0},
    ])
    monkeypatch.setattr(orch, "is_dev_sandbox", lambda: False)
    monkeypatch.setattr(orch, "_IS_WORKER", False)
    monkeypatch.setattr(lead, "may_run", lambda task, is_leader=False: True)

    async def ok_lead():
        return None
    monkeypatch.setattr(orch, "_refresh_scheduler_leadership", ok_lead)

    async def scenario():
        t0 = orch.start_at_import(both, "s7_both_import_name")     # a different name, the same function
        assert t0 is not None
        loop = asyncio.create_task(orch.scheduler_loop())
        await asyncio.sleep(2.5)
        loop.cancel()
        try:
            await loop
        except asyncio.CancelledError:
            pass
    asyncio.run(scenario())
    rows = {t["name"]: t for t in orch.SCHEDULED_TASKS}
    # 'both': once at import + the periodic entry's ticks, never a second once-only start
    assert rows["s7_both_scheduled_name"]["runs"] == 1                 # marked run, not started
    assert starts["scheduled_only"] == 1                              # a once-only job with no import start still runs
    assert starts["periodic"] >= 2 and rows["s7_periodic_same_fn"]["runs"] >= 2
    assert starts["both"] == 1 + rows["s7_periodic_same_fn"]["runs"]  # exactly: import + periodic, no extra copy


def test_a_skipped_start_is_not_counted(monkeypatch):
    async def hook():
        return None
    monkeypatch.setattr(orch, "_STARTED_AT_IMPORT", set())
    monkeypatch.setattr(orch, "_IS_WORKER", True)                      # workers skip non-allow-listed hooks
    assert orch.start_at_import(hook, "s7_not_on_workers") is None
    assert hook not in orch._STARTED_AT_IMPORT
