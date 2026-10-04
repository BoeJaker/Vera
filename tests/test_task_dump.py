"""sys.debug.tasks: every asyncio task's await chain (4 Oct 2026: a loop sat 16 min with no
events and nothing named the wait; py-spy cannot see an awaiting coroutine)."""

import asyncio
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("tdc", ROOT / "vera" / "task_dump_core.py")
T = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T)
SRC = (ROOT / "vera" / "capability_orchestration.py").read_text(encoding="utf-8")


async def _inner(ev):
    await ev.wait()


async def _middle(ev):
    await _inner(ev)


async def stuck_writer_step(ev):
    await _middle(ev)


def test_the_await_chain_reaches_the_future():
    async def go():
        ev = asyncio.Event()
        t = asyncio.ensure_future(stuck_writer_step(ev), )
        await asyncio.sleep(0.01)
        d = T.dump([t])
        ev.set()
        await t
        return d
    d = asyncio.run(go())
    (row,) = d["tasks"]
    names = " ".join(row["chain"])
    assert "stuck_writer_step" in names and "_middle" in names and "_inner" in names
    assert row["chain"][-1].startswith("awaiting ")


def test_match_filters_and_limit_bounds():
    async def go():
        ev = asyncio.Event()
        ts = [asyncio.ensure_future(stuck_writer_step(ev)) for _ in range(3)]
        ts.append(asyncio.ensure_future(asyncio.sleep(5)))
        await asyncio.sleep(0.01)
        a = T.dump(ts, match="stuck_writer")
        b = T.dump(ts, limit=1)
        ev.set()
        for t in ts:
            t.cancel()
        return a, b
    a, b = asyncio.run(go())
    assert a["total"] == 4 and a["matched"] == 3
    assert len(b["tasks"]) == 1


def test_the_cap_is_registered_read_only():
    i = SRC.index('@capability("sys.debug.tasks"')
    block = SRC[i:i + 2500]
    assert 'memory="off"' in block and "asyncio.all_tasks()" in block
    assert "_task_dump_core.dump(" in block
