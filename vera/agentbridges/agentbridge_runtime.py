"""
agentbridge_runtime.py — shared container-streaming runner for agent bridges
============================================================================
Every "external agent library, run in its own throwaway Docker container"
bridge (smolagents, LangGraph, PydanticAI, and any future one) launches a
container, reads its stdout line-by-line as the agent actually works,
emits one event per BRIDGE_STEP: line, and finishes with BRIDGE_RESULT:.
That plumbing — docker launch, concurrent stderr drain (the classic asyncio
two-pipe deadlock trap), stall detection measuring genuine progress rather
than raw byte arrival, hard-timeout kill, and the final done/error event —
was independently written twice (smolagents_capabilities.py,
langgraph_capabilities.py, 2026-08-16) before being extracted here. A new
bridge should call `stream_bridge_container()` directly rather than writing
a third copy.

Protocol a bridge's entrypoint.py must speak on stdout (unbuffered —
PYTHONUNBUFFERED=1 in its Dockerfile, flush=True on every print):
  BRIDGE_STEP:<json>     - zero or more, one per unit of real progress
  BRIDGE_RESULT:<json>   - exactly one, last line, {ok, answer, steps,
                            elapsed_s, model} | {ok:false, error}

What's genuinely bridge-specific (and stays in each bridge's own module):
  - the entrypoint's actual library calls (agent.run/stream/iter — every
    library's own streaming API is shaped differently)
  - the step vocabulary in `kind` (smolagents: task/planning/action/final;
    langgraph: human/tool_call/tool_result/ai; a new bridge picks its own)
  - the emitted event TYPE prefix (kept per-bridge — "smolagents.run.*" vs
    "langgraph.run.*" — for backward compat with anything already filtering
    on it, and so the event stream stays greppable per system)
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

STEP_PREFIX = "BRIDGE_STEP:"
RESULT_PREFIX = "BRIDGE_RESULT:"

EmitFn = Callable[[Dict[str, Any]], Awaitable[None]]


async def _drain_stderr(stream: asyncio.StreamReader, buf: List[str]) -> None:
    """Consume a container's stderr concurrently with the stdout-reading loop.
    Not optional: an asyncio subprocess with two pipes where only one is read
    can deadlock once the unread one's OS buffer fills."""
    try:
        while True:
            line = await stream.readline()
            if not line:
                break
            buf.append(line.decode("utf-8", errors="replace"))
            if len(buf) > 200:
                del buf[:100]
    except Exception:
        pass


async def stream_bridge_container(
    *,
    run_id: str,
    session_id: str,
    argv: List[str],
    event_type_prefix: str,
    emit: EmitFn,
    timeout_s: int,
    stall_s: int,
    step_line_prefix: str = STEP_PREFIX,
    result_line_prefix: str = RESULT_PREFIX,
    progress_kinds: Optional[set] = None,
) -> None:
    """Launch `argv` (a `docker run ...` command), stream its stdout, and emit
    `{event_type_prefix}.step` / `.done` / `.error` events as it progresses.
    `progress_kinds`, if given, is the set of step `kind` values that count
    toward the `steps` count reported in the final result (e.g. smolagents
    counts only "action" steps, langgraph only "tool_call"/"tool_result") —
    every step still gets its own event regardless, this only affects the
    summary count.

    Caller owns emitting the initial `{event_type_prefix}.start` event before
    calling this (this function only knows how to run+stream, not what a
    "start" means for a given bridge) and is expected to have already fired
    it off as a background task (`asyncio.ensure_future`), since this
    function runs for the container's whole lifetime.
    """
    base = {"run_id": run_id, "session_id": session_id}
    t0 = time.time()

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except Exception as e:
        await emit({**base, "type": f"{event_type_prefix}.error",
                    "error": f"could not start container: {e}",
                    "elapsed_s": round(time.time() - t0, 2)})
        return

    stderr_buf: List[str] = []
    stderr_task = asyncio.ensure_future(_drain_stderr(proc.stderr, stderr_buf))

    result: Optional[Dict[str, Any]] = None
    steps_seen = 0
    stalled = False
    timed_out = False

    try:
        while True:
            remaining = timeout_s - (time.time() - t0)
            if remaining <= 0:
                timed_out = True
                break
            wait_for = min(stall_s, remaining)
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=wait_for)
            except asyncio.TimeoutError:
                stalled = True
                break
            if not raw:
                break  # EOF — process finished producing output
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if line.startswith(step_line_prefix):
                try:
                    info = json.loads(line[len(step_line_prefix):])
                except Exception:
                    continue
                if progress_kinds is None or info.get("kind") in progress_kinds:
                    steps_seen += 1
                await emit({**base, "type": f"{event_type_prefix}.step", **info})
            elif line.startswith(result_line_prefix):
                try:
                    result = json.loads(line[len(result_line_prefix):])
                except Exception as e:
                    result = {"ok": False, "error": f"could not parse result: {e}"}
                break
    finally:
        if stalled or timed_out:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except Exception:
            pass
        stderr_task.cancel()

    elapsed = round(time.time() - t0, 2)

    if result is not None:
        result.setdefault("elapsed_s", elapsed)
        result.setdefault("steps", steps_seen)
        await emit({**base, "type": f"{event_type_prefix}.done",
                    "ok": result.get("ok", False), "elapsed_s": elapsed,
                    "result": result})
        return

    if stalled:
        err = f"no progress for {stall_s}s (stalled) — steps seen: {steps_seen}"
    elif timed_out:
        err = f"timed out after {timeout_s}s — steps seen: {steps_seen}"
    else:
        err = "container exited before printing its result line"
    err_tail = ("".join(stderr_buf))[-800:]
    await emit({**base, "type": f"{event_type_prefix}.error",
               "error": err, "stderr_tail": err_tail, "elapsed_s": elapsed})


async def sh(argv: List[str], timeout: float = 60) -> Dict[str, Any]:
    """Run a short, one-shot host command (status checks, image builds),
    capturing stdout/stderr. Never raises. NOT for the container being
    streamed — that's stream_bridge_container above."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return {"ok": False, "out": "", "err": f"timed out after {timeout}s"}
        return {"ok": proc.returncode == 0,
                "out": out.decode("utf-8", errors="replace"),
                "err": err.decode("utf-8", errors="replace")}
    except Exception as e:
        return {"ok": False, "out": "", "err": str(e)}


async def image_present(image: str) -> bool:
    r = await sh(["docker", "image", "inspect", image], timeout=15)
    return bool(r.get("ok"))


async def build_image(image: str, dockerfile: str, context_dir: str,
                      timeout: float = 600) -> Dict[str, Any]:
    r = await sh(["docker", "build", "-t", image, "-f", dockerfile, context_dir],
                timeout=timeout)
    present = await image_present(image)
    return {"ok": present, "present": present,
            "log": (r.get("out", "") + "\n" + r.get("err", ""))[-2500:]}
