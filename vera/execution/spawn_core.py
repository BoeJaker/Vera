"""
spawn_core.py — run a local command WITHOUT forking the server on the event loop
================================================================================

Every captured local command in Vera (`docker exec/stop/cp` for the session
sandboxes, `bash -lc` for exec.bash.run, git in the Loop Lab worktrees) funnels
through exec_capabilities._run_local, which used asyncio.create_subprocess_exec.

uvicorn's loop="auto" picks uvloop on this host, and uvloop's
loop.subprocess_exec is libuv's uv_spawn wrapped in PyOS_BeforeFork() /
PyOS_AfterFork_Parent() (uvloop/handles/process.pyx). On Linux uv_spawn is a
real fork(): the kernel copies the parent's page tables — 11 GB of VSZ on prod
— while the GIL is held, so every spawn froze the whole loop for 1–2 s under
load. perf.stalls showed it in the act: create_subprocess_exec sitting directly
on logging's _releaseLock at-fork hook with no Python frame in between.
perf.scan for one 15-minute window (2026-09-12, 156 stalls, worst 7.2 s)
named that hook 24 times and create_subprocess_exec itself 14 more — most of
them the session-sandbox idle-sleep and auto-sync ticks doing a workspace
dirty-check or a `docker stop`.

Two earlier attempts (close_fds=False, an absolute docker path) targeted
CPython's subprocess.Popen posix_spawn fast path — which uvloop never enters —
so they were inert. This module spawns through subprocess.Popen on a dedicated
worker thread: CPython's own vfork/posix_spawn path applies whatever the loop
implementation, there is no page-table copy, spawn cost stops scaling with the
server's size, and the event loop only ever awaits a future.

Pure: no app import, no event-loop assumptions beyond asyncio itself.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

# A dedicated pool, not the default to_thread executor: a `docker cp` of a big
# workspace can legitimately run for minutes (600 s timeout) and must not
# starve every other to_thread user in the process while it does.
SPAWN_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=int(os.getenv("VERA_SPAWN_WORKERS", "32") or 32),
    thread_name_prefix="vera-spawn")


def spawn_and_wait(argv: List[str], stdin_data: str, timeout: float,
                   cwd: Optional[str], env: Optional[Dict[str, str]]
                   ) -> Tuple[Optional[int], bytes, bytes, bool]:
    """Worker-thread half: spawn, feed stdin, collect both streams, enforce the
    timeout. Returns (rc, stdout_bytes, stderr_bytes, timed_out). A bad argv[0]
    raises FileNotFoundError to the caller, like Popen does."""
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if stdin_data else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
    )
    try:
        out, err = proc.communicate(
            stdin_data.encode("utf-8") if stdin_data else None, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        # Reap so the killed child does not linger as a zombie; bounded, in
        # case a grandchild still holds the pipes open.
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        return proc.returncode, b"", b"", True
    return proc.returncode, out, err, False


async def run_argv(argv: List[str], stdin_data: str = "", timeout: float = 600,
                   cwd: Optional[str] = None,
                   env: Optional[Dict[str, str]] = None,
                   max_output: int = 1_000_000) -> Dict[str, Any]:
    """Run `argv` off the event loop and return the captured result:
    {ok, rc, stdout, stderr, elapsed_ms} — plus `error` on a spawn failure or
    timeout. `env`, when given, is layered over os.environ."""
    t0 = time.monotonic()
    loop = asyncio.get_running_loop()
    try:
        rc, stdout_b, stderr_b, timed_out = await loop.run_in_executor(
            SPAWN_POOL, spawn_and_wait, list(argv), stdin_data, timeout, cwd,
            {**os.environ, **(env or {})} if env else None)
    except FileNotFoundError as e:
        return {"ok": False, "error": f"executable not found: {e}",
                "rc": -1, "stdout": "", "stderr": str(e),
                "elapsed_ms": 0}
    if timed_out:
        return {"ok": False, "error": f"timeout after {timeout}s",
                "rc": -1, "stdout": "", "stderr": "",
                "elapsed_ms": round((time.monotonic() - t0) * 1000)}
    return {
        "ok":         rc == 0,
        "rc":         rc,
        "stdout":     stdout_b.decode("utf-8", errors="replace")[:max_output],
        "stderr":     stderr_b.decode("utf-8", errors="replace")[:max_output],
        "elapsed_ms": round((time.monotonic() - t0) * 1000),
    }
