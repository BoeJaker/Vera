"""A local command must not fork the server on the event loop.

Pins vera.execution.spawn_core, the pure spawn path every captured local
command (`docker exec/stop/cp` for the session sandboxes, `bash -lc`, git in
the Loop Lab worktrees) goes through. The regression this guards: under uvloop
asyncio.create_subprocess_exec is a real fork() of the whole server with the
GIL held — 1–2 s of frozen event loop per spawn on prod (2026-09-12,
perf.scan: 38 of 156 stalls in 15 min named it, mostly the session-sandbox
idle-sleep and auto-sync ticks). Two earlier fixes aimed at CPython's posix_spawn fast path
never engaged because uvloop never calls subprocess.Popen; this one does not
rely on the loop's subprocess machinery at all, and the tests below refuse a
return to it.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.execution import spawn_core  # noqa: E402

PY = sys.executable


def _run(coro):
    return asyncio.run(coro)


def test_captures_stdout_stderr_and_rc():
    res = _run(spawn_core.run_argv(
        [PY, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"]))
    assert res["ok"] is False
    assert res["rc"] == 3
    assert res["stdout"].strip() == "out"
    assert res["stderr"].strip() == "err"
    assert "error" not in res


def test_stdin_is_fed_to_the_child():
    res = _run(spawn_core.run_argv(
        [PY, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
        stdin_data="hello"))
    assert res["ok"] is True
    assert res["stdout"] == "HELLO"


def test_env_layers_over_environ():
    res = _run(spawn_core.run_argv(
        [PY, "-c", "import os; print(os.environ.get('VERA_SPAWN_T', '-'), os.environ.get('PATH') is not None)"],
        env={"VERA_SPAWN_T": "yes"}))
    assert res["stdout"].split() == ["yes", "True"]


def test_missing_executable_is_reported_not_raised():
    res = _run(spawn_core.run_argv(["/nonexistent/vera-no-such-binary"]))
    assert res["ok"] is False
    assert res["rc"] == -1
    assert res["error"].startswith("executable not found")


def test_timeout_kills_and_reaps_the_child():
    res = _run(spawn_core.run_argv(
        [PY, "-c", "import time; time.sleep(30)"], timeout=1))
    assert res["ok"] is False
    assert res["rc"] == -1
    assert res["error"] == "timeout after 1s"
    # The whole call must come back near the timeout, not after the child's
    # own 30 s: the kill happened and the reap did not hang.
    assert res["elapsed_ms"] < 15_000


def test_never_touches_the_loops_subprocess_machinery():
    """The exact regression: whatever loop implementation runs, spawning must
    not go through loop.subprocess_exec (a fork() of the server under uvloop).
    Make the loop's hook explode and prove the spawn still works."""
    async def body():
        loop = asyncio.get_running_loop()

        async def boom(*a, **k):
            raise AssertionError("spawn went through loop.subprocess_exec")

        loop.subprocess_exec = boom  # type: ignore[method-assign]
        return await spawn_core.run_argv([PY, "-c", "print('ok')"])

    res = _run(body())
    assert res["ok"] is True
    assert res["stdout"].strip() == "ok"


def test_event_loop_keeps_ticking_while_the_child_runs():
    """A slow child must not freeze the loop: an independent ticker keeps
    running while the spawn is awaited. Bound is deliberately loose (a loaded
    CI box) — the old path blocked the loop for the whole spawn, i.e. ticks ≈ 0."""
    async def body():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        t = asyncio.ensure_future(ticker())
        try:
            res = await spawn_core.run_argv([PY, "-c", "import time; time.sleep(1.0)"])
        finally:
            t.cancel()
        return res, ticks

    res, ticks = _run(body())
    assert res["ok"] is True
    assert ticks >= 3


@pytest.mark.parametrize("n", [1, 2, 3])
def test_spawn_pool_is_shared_and_bounded(n):
    # One module-level pool with a real bound — a runaway caller cannot spawn
    # an unbounded number of threads, and every call uses the same pool.
    assert spawn_core.SPAWN_POOL._max_workers >= 1

    async def body():
        return await asyncio.gather(*(
            spawn_core.run_argv([PY, "-c", f"print({i})"]) for i in range(n)))

    res = _run(body())
    assert [r["stdout"].strip() for r in res] == [str(i) for i in range(n)]
