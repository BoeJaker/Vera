"""D01-6: wrapper-private arguments never reach the worker, and a worker runs only what a cap accepts.

`_timeout` was popped only after dispatch_task, so it travelled in the task payload and a node worker called the cap
with it: every offloaded ingress.fetch.batch failed "unexpected keyword argument '_timeout'" (web-ingress, 18 Oct).
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

NAME = "test.d016.remote"


@orch.capability(NAME, mode="distributed", memory="off", silent=True, mcp_expose=False)
async def _remote(x: int = 0, trace_id=None):
    return {"x": x}


def test_dispatch_carries_no_private_arguments(monkeypatch):
    sent, waited = {}, {}

    async def fake_dispatch(name, kw, tid, bg=""):
        sent.update(name=name, kw=dict(kw))
        return "task-1"

    async def fake_wait(task_id, timeout=None):
        waited.update(task_id=task_id, timeout=timeout)
        return {"x": 7}

    monkeypatch.setattr(orch, "REDIS", object())
    monkeypatch.setattr(orch, "dispatch_task", fake_dispatch)
    monkeypatch.setattr(orch, "wait_for_result", fake_wait)
    res = asyncio.run(orch.CAPABILITY_REGISTRY[NAME]["func"](x=7, _timeout=42, _local=True))
    assert res == {"x": 7}
    assert sent["name"] == NAME and sent["kw"] == {"x": 7}            # no _timeout, no _local
    assert waited == {"task_id": "task-1", "timeout": 42.0}           # the caller's timeout still governs the wait


def test_without_a_timeout_the_cap_default_applies(monkeypatch):
    waited = {}

    async def fake_dispatch(name, kw, tid, bg=""):
        return "task-2"

    async def fake_wait(task_id, timeout=None):
        waited["timeout"] = timeout
        return {}
    monkeypatch.setattr(orch, "REDIS", object())
    monkeypatch.setattr(orch, "dispatch_task", fake_dispatch)
    monkeypatch.setattr(orch, "wait_for_result", fake_wait)
    asyncio.run(orch.CAPABILITY_REGISTRY[NAME]["func"](x=1))
    assert waited["timeout"] == orch._cap_timeout(NAME)


def test_a_local_run_drops_a_key_the_cap_does_not_take(monkeypatch):
    got = {}

    async def strict(a: int = 0, trace_id=None):                       # no **kwargs
        got["a"] = a
        return {"ok": True}
    monkeypatch.setattr(orch, "PENDING_RESULTS", {})
    asyncio.run(orch._run_local({"raw": strict, "name": "t.strict"}, "task-3",
                                {"a": 5, "_timeout": 9, "stray": 1}, "tr"))
    assert got == {"a": 5}
