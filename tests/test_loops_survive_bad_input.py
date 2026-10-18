"""S27 / D01-3: one bad job or task message no longer stops the scheduler or a worker until the next restart.

The scheduler loop had no try: a job entry that raised (or a leadership refresh that did) ended the loop, and with it
every scheduled job. The worker loop's per-message block had none either: a message without an id, or a payload
that is not JSON, killed that worker. Runs in the gate container, where Vera.vera resolves to the branch."""
import ast
import asyncio
import json
import os
import sys

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import Vera.vera.capability_orchestration as orch  # noqa: E402  (fails loudly, never skips)
from Vera.vera import scheduler_leadership as lead  # noqa: E402


def test_one_bad_job_does_not_stop_the_scheduler(monkeypatch):
    ran = []

    async def good():
        ran.append(1)

    tasks = [{"name": "s27.bad"},                                     # no "int", no "last": raises in the body
             {"name": "s27.good", "fn": good, "int": 3600, "last": None, "runs": 0}]
    monkeypatch.setattr(orch, "SCHEDULED_TASKS", tasks)
    monkeypatch.setattr(orch, "SCHEDULER_ERRORS", {})
    monkeypatch.setattr(orch, "is_dev_sandbox", lambda: False)
    monkeypatch.setattr(orch, "_IS_WORKER", False)
    monkeypatch.setattr(lead, "may_run", lambda task, is_leader=False: True)

    async def broken_lead():
        raise ConnectionError("redis blip")
    monkeypatch.setattr(orch, "_refresh_scheduler_leadership", broken_lead)

    async def run_a_little():
        t = asyncio.create_task(orch.scheduler_loop())
        await asyncio.sleep(2.5)                                      # two or three ticks
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        return t
    t = asyncio.run(run_a_little())
    assert t.cancelled()                                              # it was still running: nothing killed it
    assert ran == [1]                                                 # the good job ran (once: interval 3600)
    assert orch.SCHEDULER_ERRORS["s27.bad"]["count"] >= 2             # caught on every tick
    assert orch.SCHEDULER_ERRORS["(leadership)"]["count"] >= 2
    rows = {r["name"]: r for r in asyncio.run(orch.CAPABILITY_REGISTRY["obs.scheduler"]["raw"]())
            if isinstance(r, dict) and r.get("name")}
    assert rows["s27.bad"]["errors"] >= 2 and "KeyError" in rows["s27.bad"]["last_error"]


class FakeRedis:
    def __init__(self):
        self.xadds, self.acks, self.dels = [], [], []

    async def xadd(self, name, fields, maxlen=None, approximate=False):
        self.xadds.append((name, fields))

    async def xack(self, stream, group, msg_id):
        self.acks.append((stream, msg_id))

    async def xdel(self, stream, msg_id):
        self.dels.append((stream, msg_id))

    async def expire(self, *a, **kw):
        return True


def test_a_poison_message_is_moved_aside_and_its_caller_told(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(orch, "REDIS", r)
    monkeypatch.setitem(orch.WORKER_REGISTRY, "w-s27", {"status": "running:x", "tasks_failed": 0})
    data = {b"id": b"task-1", b"capability": b"x.y", b"payload": b"{not json", b"trace_id": b"tr",
            b"reply_to": b"vera:reply:abc"}
    asyncio.run(orch._dead_letter_task("vera:tasks", b"1-0", data, ValueError("bad json"), "w-s27"))
    dead = [f for n, f in r.xadds if n == orch.TASK_DEAD_STREAM]
    assert len(dead) == 1 and "ValueError" in dead[0]["error"] and json.loads(dead[0]["fields"])["id"] == "task-1"
    assert r.acks == [("vera:tasks", b"1-0")] and r.dels == [("vera:tasks", b"1-0")]
    reply = [f for n, f in r.xadds if n == "vera:reply:abc"]
    assert reply and reply[0]["id"] == "task-1" and "could not be run" in reply[0]["error"]
    w = orch.WORKER_REGISTRY["w-s27"]
    assert w["status"] == "idle" and w["tasks_failed"] == 1


def test_a_message_without_an_id_is_still_moved_aside(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(orch, "REDIS", r)
    monkeypatch.setitem(orch.WORKER_REGISTRY, "w-s27b", {})
    asyncio.run(orch._dead_letter_task("vera:tasks", b"2-0", {b"junk": b"\xff"}, KeyError(b"id"), "w-s27b"))
    assert [n for n, _ in r.xadds] == [orch.TASK_DEAD_STREAM] and r.acks == [("vera:tasks", b"2-0")]


def test_the_worker_message_block_is_inside_the_dead_letter_try():
    src = open(os.path.join(ROOT, "vera", "capability_orchestration.py"), encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "worker_loop")
    loops = [n for n in ast.walk(fn) if isinstance(n, ast.For) and getattr(n.target, "elts", None)
             and [getattr(e, "id", "") for e in n.target.elts] == ["msg_id", "data"]]
    assert loops and isinstance(loops[0].body[0], ast.Try) and len(loops[0].body) == 1
    handlers = loops[0].body[0].handlers
    assert any("_dead_letter_task" in ast.unparse(h) for h in handlers)
