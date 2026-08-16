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

GPU gate (2026-08-16, real bug fix — was the actual cause of "streamed
outputs tend to time out and produce no output"): a bridge container makes
a RAW HTTP call straight to Ollama — it has no idea Vera's own
ollama_generate() calls all queue behind a cross-process gate
(vera/ollama_gate.py, gpu_cap=1 on a GPU node, shared by prod AND every dev
sandbox). Under real contention a bridge's single model call can queue
invisibly at the Ollama server for a long time with ZERO stdout output
during the wait — indistinguishable, from this module's own stall detector,
from a genuinely hung container, so it gets killed exactly like one. Fixed
by acquiring the SAME gate lease Vera's own calls use (same node key, same
capacity/TTL/wait policy) BEFORE launching the container, so a bridge run
queues fairly instead of firing blind — see acquire_gpu_gate/release_
gpu_gate below, wired into stream_bridge_container via `gate_iid`.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

STEP_PREFIX = "BRIDGE_STEP:"
RESULT_PREFIX = "BRIDGE_RESULT:"


def pick_ollama_instance(ollama_instances: Dict[str, Dict[str, Any]]
                         ) -> Tuple[str, str]:
    """(instance_id, url) for the best available Ollama instance — prefers an
    online GPU instance, falls back to any online instance, then to whatever's
    registered at all. Vera does NOT configure Ollama instances via env vars
    on prod's own process (confirmed empty via /proc/<pid>/environ) — the
    real source of truth is OLLAMA_INSTANCES, the same in-memory registry
    ollama_generate()/pick_instance() read from. The instance_id is what the
    GPU gate below keys its slots on — the same identifier Vera's own
    generation calls use, so a bridge run queues in the SAME line, not a
    separate invisible one."""
    online_gpu = [(k, v) for k, v in ollama_instances.items()
                  if v.get("has_gpu") and v.get("status") == "online" and v.get("enabled", True)]
    if online_gpu:
        return online_gpu[0][0], online_gpu[0][1]["url"]
    online_any = [(k, v) for k, v in ollama_instances.items()
                  if v.get("status") == "online" and v.get("enabled", True)]
    if online_any:
        return online_any[0][0], online_any[0][1]["url"]
    any_inst = list(ollama_instances.items())
    if any_inst:
        return any_inst[0][0], any_inst[0][1].get("url", "")
    return "", ""


async def acquire_gpu_gate(instance_id: str) -> Optional[Dict[str, Any]]:
    """Claim the SAME cross-process GPU slot lease Vera's own ollama_generate()
    acquires for this instance (vera/ollama_gate.py) before launching a bridge
    container — a bridge's raw HTTP call to Ollama has no other way to know
    that gate exists. Fail-open by construction, same guarantee as the gate
    itself: gate disabled, node ungated, coordination Redis down, or any
    import/lookup error all just mean "proceed unslotted" (returns None) —
    this can only ever ADD fair waiting before the container starts, never
    block or break a run."""
    if not instance_id:
        return None
    try:
        import Vera.vera.capability_orchestration as orch
        from Vera.vera import ollama_gate as gate
        if not gate.gate_enabled():
            return None
        if orch.COORD_REDIS is None:
            await orch._ensure_coord_redis()
        inst = orch.OLLAMA_INSTANCES.get(instance_id, {})
        cap = gate.capacity_for(bool(inst.get("has_gpu")))
        if cap <= 0 or orch.COORD_REDIS is None:
            return None
        return await gate.acquire(orch.COORD_REDIS, instance_id, cap,
                                  gate.ttl_ms(), gate.wait_s())
    except Exception:
        return None


async def release_gpu_gate(lease: Optional[Dict[str, Any]]) -> None:
    if lease is None:
        return
    try:
        import Vera.vera.capability_orchestration as orch
        from Vera.vera import ollama_gate as gate
        await gate.release(orch.COORD_REDIS, lease)
    except Exception:
        pass

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
    gate_instance_id: str = "",
) -> None:
    """Launch `argv` (a `docker run ...` command), stream its stdout, and emit
    `{event_type_prefix}.step` / `.done` / `.error` events as it progresses.
    `progress_kinds`, if given, is the set of step `kind` values that count
    toward the `steps` count reported in the final result (e.g. smolagents
    counts only "action" steps, langgraph only "tool_call"/"tool_result") —
    every step still gets its own event regardless, this only affects the
    summary count.

    `gate_instance_id`, if given, acquires Vera's own cross-process GPU gate
    lease for that Ollama instance BEFORE launching the container (released
    after, whatever the outcome) — see acquire_gpu_gate's docstring for why
    this matters: without it, a bridge's raw Ollama call can queue invisibly
    behind Vera's own gated calls and get killed by the stall detector below
    as if it had hung, when it was really just waiting its turn.

    Caller owns emitting the initial `{event_type_prefix}.start` event before
    calling this (this function only knows how to run+stream, not what a
    "start" means for a given bridge) and is expected to have already fired
    it off as a background task (`asyncio.ensure_future`), since this
    function runs for the container's whole lifetime.
    """
    base = {"run_id": run_id, "session_id": session_id}
    t0 = time.time()

    gate_lease = await acquire_gpu_gate(gate_instance_id) if gate_instance_id else None
    if gate_lease is not None:
        await emit({**base, "type": f"{event_type_prefix}.gate_acquired",
                   "waited_s": gate_lease.get("waited_s", 0)})

    try:
        await _stream_bridge_container_inner(
            base=base, t0=t0, argv=argv, event_type_prefix=event_type_prefix,
            emit=emit, timeout_s=timeout_s, stall_s=stall_s,
            step_line_prefix=step_line_prefix, result_line_prefix=result_line_prefix,
            progress_kinds=progress_kinds,
        )
    finally:
        # Released whatever happened above (done/error/stall/timeout/launch
        # failure/an exception this function didn't even anticipate) — a
        # bridge run must never strand a GPU slot for other callers.
        await release_gpu_gate(gate_lease)


async def _stream_bridge_container_inner(
    *, base: Dict[str, str], t0: float, argv: List[str], event_type_prefix: str,
    emit: EmitFn, timeout_s: int, stall_s: int, step_line_prefix: str,
    result_line_prefix: str, progress_kinds: Optional[set],
) -> None:
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
