"""context.recall within a budget: what answered in time now, the rest on the next fetch.

Pure (asyncio only, no app imports) so it is testable from a worktree as `vera.fabric.recall_budget_core`.

Measured on prod (context-and-ui, 2026-10-05): one chat context fetch spent 7.1-7.3 s of its ~9 s in context.recall,
whose three sources are gathered with no limit. Cold, related_qa took 5.6 s (it waits on a query embedding) and the
entity crawl 3.7 s; warm, every source answered in 0.03-0.25 s. So a caller with a budget gets the sources that
answered in it, and a source still running is left to finish in the background, its answer kept for the next fetch
of the same question (the chat fetches again when the person sends, and on every pause in typing).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, Tuple


async def _timed(coro, clock: Callable[[], float]):
    t0 = clock()
    try:
        res = await coro
    except asyncio.CancelledError:
        raise
    except Exception as e:          # a failed source is its exception, timed like any other
        res = e
    return res, (clock() - t0) * 1000.0


async def gather_until(coros: Dict[str, Any], timeout: float = 0.0,
                       clock: Callable[[], float] = time.monotonic
                       ) -> Tuple[Dict[str, Any], Dict[str, float], Dict[str, "asyncio.Task"]]:
    """Run named coroutines together.

    timeout <= 0: wait for all (the behaviour every caller had before the budget).
    timeout > 0: return when it runs out. Returns (done, ms, pending):
      done    - {name: result, or the exception it raised} for every source that finished in time
      ms      - {name: how long it took} for the same sources
      pending - {name: task} for the sources still running; they are NOT cancelled - the caller decides what to do
                with their answers (context.recall keeps them for the next fetch)."""
    tasks = {n: asyncio.ensure_future(_timed(c, clock)) for n, c in coros.items()}
    if not tasks:
        return {}, {}, {}
    if timeout and timeout > 0:
        await asyncio.wait(list(tasks.values()), timeout=float(timeout))
    else:
        await asyncio.wait(list(tasks.values()))
    done: Dict[str, Any] = {}
    ms: Dict[str, float] = {}
    pending: Dict[str, "asyncio.Task"] = {}
    for n, t in tasks.items():
        if not t.done():
            pending[n] = t
            continue
        if t.cancelled():
            done[n] = asyncio.CancelledError()
            continue
        res, took = t.result()
        done[n], ms[n] = res, round(took, 1)
    return done, ms, pending


def keep_when_done(task: "asyncio.Task", put: Callable[[Any], None]) -> None:
    """When a pending source finishes, hand its result to `put` (a cache write) - unless it failed or was cancelled.
    Never raises into the event loop."""
    def _cb(t):
        try:
            if t.cancelled():
                return
            res, _ms = t.result()
            if isinstance(res, BaseException):
                return
            put(res)
        except Exception:
            pass
    task.add_done_callback(_cb)
