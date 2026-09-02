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
from dataclasses import dataclass, field
import json
import math
import re
import threading
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

STEP_PREFIX = "BRIDGE_STEP:"
RESULT_PREFIX = "BRIDGE_RESULT:"
MAX_PROTOCOL_LINE_BYTES = 65_536
MAX_PAYLOAD_DEPTH = 8
MAX_PAYLOAD_NODES = 2_048
MAX_OBJECT_FIELDS = 128
MAX_ARRAY_ITEMS = 256
MAX_STRING_CHARS = 32_768
PROCESS_EXIT_GRACE_S = 10.0
PROCESS_KILL_GRACE_S = 5.0
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


@dataclass
class _ActiveBridgeRun:
    run_id: str
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    process: Any = None
    process_kill_sent: bool = False
    terminal: bool = False


_ACTIVE_RUNS: Dict[str, _ActiveBridgeRun] = {}
_ACTIVE_RUNS_LOCK = threading.RLock()


def _valid_run_id(run_id: Any) -> str:
    value = str(run_id or "").strip()
    if not _RUN_ID.fullmatch(value):
        raise ValueError("run_id must be a bounded identifier")
    return value


def _register_run(run_id: str) -> Optional[_ActiveBridgeRun]:
    control = _ActiveBridgeRun(run_id=run_id)
    with _ACTIVE_RUNS_LOCK:
        if run_id in _ACTIVE_RUNS:
            return None
        _ACTIVE_RUNS[run_id] = control
    return control


def _unregister_run(control: _ActiveBridgeRun) -> None:
    with _ACTIVE_RUNS_LOCK:
        if _ACTIVE_RUNS.get(control.run_id) is control:
            _ACTIVE_RUNS.pop(control.run_id, None)


def _mark_terminal(control: _ActiveBridgeRun) -> None:
    with _ACTIVE_RUNS_LOCK:
        control.terminal = True


def _kill_owned_process(control: _ActiveBridgeRun) -> None:
    """Send at most one kill to the exact process owned by this run."""
    with _ACTIVE_RUNS_LOCK:
        proc = control.process
        if proc is None or control.process_kill_sent:
            return
        control.process_kill_sent = True
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    except Exception:
        # Cleanup still waits/reaps below; cancellation remains requested.
        pass


async def cancel_bridge_run(run_id: str) -> Dict[str, Any]:
    """Request cancellation of one active bridge run without shell lookup.

    The registry owns the exact subprocess object, so cancellation never
    guesses a container name or kills an unrelated process. The runner emits
    the single terminal event and releases any GPU lease in its normal
    ``finally`` path.
    """
    run_id = _valid_run_id(run_id)
    with _ACTIVE_RUNS_LOCK:
        control = _ACTIVE_RUNS.get(run_id)
        if control is None or control.terminal:
            return {"ok": False, "accepted": False, "run_id": run_id,
                    "reason_code": "runtime_run_not_active"}
        control.cancel_event.set()
    _kill_owned_process(control)
    return {"ok": True, "accepted": True, "run_id": run_id,
            "state": "cancellation_requested"}


def active_bridge_run_ids() -> List[str]:
    """Return bounded identifiers only; never argv, prompts, or process data."""
    with _ACTIVE_RUNS_LOCK:
        return sorted(_ACTIVE_RUNS)[:256]


def _bounded_payload(value: Any) -> Any:
    """Validate untrusted bridge JSON before it reaches the event bus."""
    remaining = [MAX_PAYLOAD_NODES]

    def walk(item: Any, depth: int) -> Any:
        remaining[0] -= 1
        if remaining[0] < 0 or depth > MAX_PAYLOAD_DEPTH:
            raise ValueError("payload structure exceeds bounds")
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("payload number must be finite")
            return item
        if isinstance(item, str):
            if len(item) > MAX_STRING_CHARS:
                raise ValueError("payload string exceeds bounds")
            return item
        if isinstance(item, list):
            if len(item) > MAX_ARRAY_ITEMS:
                raise ValueError("payload array exceeds bounds")
            return [walk(child, depth + 1) for child in item]
        if isinstance(item, dict):
            if len(item) > MAX_OBJECT_FIELDS:
                raise ValueError("payload object exceeds bounds")
            result = {}
            for key, child in item.items():
                if not isinstance(key, str) or not key or len(key) > 128:
                    raise ValueError("payload key is invalid")
                result[key] = walk(child, depth + 1)
            return result
        raise ValueError("payload contains an unsupported value")

    return walk(value, 0)


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
            buf.append(line.decode("utf-8", errors="replace")[-2_000:])
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
    run_id = _valid_run_id(run_id)
    base = {"run_id": run_id, "session_id": str(session_id or "")[:256]}
    t0 = time.time()
    control = _register_run(run_id)
    if control is None:
        await emit({**base, "type": f"{event_type_prefix}.error",
                    "reason_code": "duplicate_run_id",
                    "error": "run_id is already active", "elapsed_s": 0.0})
        return

    gate_lease = None
    try:
        gate_lease = (await acquire_gpu_gate(gate_instance_id)
                      if gate_instance_id else None)
        if gate_lease is not None:
            await emit({**base, "type": f"{event_type_prefix}.gate_acquired",
                       "waited_s": gate_lease.get("waited_s", 0)})
        if control.cancel_event.is_set():
            _mark_terminal(control)
            await emit({**base, "type": f"{event_type_prefix}.error",
                        "reason_code": "cancelled", "cancelled": True,
                        "error": "run cancelled before container launch",
                        "elapsed_s": round(time.time() - t0, 2)})
            return
        await _stream_bridge_container_inner(
            base=base, t0=t0, argv=argv, event_type_prefix=event_type_prefix,
            emit=emit, timeout_s=timeout_s, stall_s=stall_s,
            step_line_prefix=step_line_prefix, result_line_prefix=result_line_prefix,
            progress_kinds=progress_kinds, control=control,
        )
    finally:
        # Released whatever happened above (done/error/stall/timeout/launch
        # failure/an exception this function didn't even anticipate) — a
        # bridge run must never strand a GPU slot for other callers.
        await release_gpu_gate(gate_lease)
        _unregister_run(control)


async def _stream_bridge_container_inner(
    *, base: Dict[str, str], t0: float, argv: List[str], event_type_prefix: str,
    emit: EmitFn, timeout_s: int, stall_s: int, step_line_prefix: str,
    result_line_prefix: str, progress_kinds: Optional[set],
    control: _ActiveBridgeRun,
) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except Exception as e:
        _mark_terminal(control)
        await emit({**base, "type": f"{event_type_prefix}.error",
                    "reason_code": "launch_failed",
                    "error": f"could not start container ({type(e).__name__})",
                    "elapsed_s": round(time.time() - t0, 2)})
        return

    with _ACTIVE_RUNS_LOCK:
        control.process = proc
        cancelled_before_bind = control.cancel_event.is_set()
    if cancelled_before_bind:
        _kill_owned_process(control)

    stderr_buf: List[str] = []
    stderr_task = asyncio.ensure_future(_drain_stderr(proc.stderr, stderr_buf))

    result: Optional[Dict[str, Any]] = None
    steps_seen = 0
    stalled = False
    timed_out = False
    protocol_error = ""
    cancelled = cancelled_before_bind
    teardown_error = ""

    try:
        while True:
            if control.cancel_event.is_set():
                cancelled = True
                break
            remaining = timeout_s - (time.time() - t0)
            if remaining <= 0:
                timed_out = True
                break
            wait_for = min(stall_s, remaining)
            deadline_limited = remaining <= stall_s
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=wait_for)
            except asyncio.TimeoutError:
                if deadline_limited:
                    timed_out = True
                else:
                    stalled = True
                break
            except (ValueError, asyncio.LimitOverrunError):
                protocol_error = "bridge output line exceeds protocol bounds"
                break
            except Exception as exc:
                protocol_error = f"could not read bridge output: {type(exc).__name__}"
                break
            if control.cancel_event.is_set():
                cancelled = True
                break
            if not raw:
                break  # EOF — process finished producing output
            if len(raw) > MAX_PROTOCOL_LINE_BYTES:
                protocol_error = "bridge output line exceeds protocol bounds"
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if line.startswith(step_line_prefix):
                try:
                    info = _bounded_payload(json.loads(line[len(step_line_prefix):]))
                    if not isinstance(info, dict):
                        raise ValueError("step payload must be an object")
                except Exception as exc:
                    protocol_error = f"invalid step payload: {exc}"
                    break
                if progress_kinds is None or info.get("kind") in progress_kinds:
                    steps_seen += 1
                await emit({**info, **base, "type": f"{event_type_prefix}.step"})
            elif line.startswith(result_line_prefix):
                try:
                    result = _bounded_payload(json.loads(line[len(result_line_prefix):]))
                    if not isinstance(result, dict):
                        raise ValueError("result payload must be an object")
                    if not isinstance(result.get("ok"), bool):
                        raise ValueError("result payload requires boolean ok")
                except Exception as e:
                    protocol_error = f"invalid result payload: {e}"
                break
    finally:
        termination_requested = bool(
            cancelled or stalled or timed_out or protocol_error)
        if termination_requested:
            _kill_owned_process(control)
        try:
            await asyncio.wait_for(proc.wait(), timeout=PROCESS_EXIT_GRACE_S)
        except asyncio.TimeoutError:
            if not termination_requested:
                teardown_error = "container did not exit after output ended"
            _kill_owned_process(control)
            try:
                await asyncio.wait_for(proc.wait(), timeout=PROCESS_KILL_GRACE_S)
            except Exception:
                teardown_error = "container could not be reaped after kill"
        except Exception:
            teardown_error = "container wait failed"
            _kill_owned_process(control)
            try:
                await asyncio.wait_for(proc.wait(), timeout=PROCESS_KILL_GRACE_S)
            except Exception:
                teardown_error = "container could not be reaped after kill"
        stderr_task.cancel()
        await asyncio.gather(stderr_task, return_exceptions=True)
        with _ACTIVE_RUNS_LOCK:
            control.process = None

    elapsed = round(time.time() - t0, 2)

    if (result is not None and not cancelled and not protocol_error
            and not teardown_error):
        _mark_terminal(control)
        result["elapsed_s"] = elapsed
        result["steps"] = steps_seen
        await emit({**base, "type": f"{event_type_prefix}.done",
                    "ok": result.get("ok", False), "elapsed_s": elapsed,
                    "result": result})
        return

    _mark_terminal(control)
    if teardown_error:
        reason_code = "teardown_failed"
        err = teardown_error
    elif cancelled or control.cancel_event.is_set():
        reason_code = "cancelled"
        err = "run cancelled"
    elif protocol_error:
        reason_code = "invalid_output"
        err = protocol_error
    elif stalled:
        reason_code = "stalled"
        err = f"no progress for {stall_s}s (stalled) — steps seen: {steps_seen}"
    elif timed_out:
        reason_code = "timeout"
        err = f"timed out after {timeout_s}s — steps seen: {steps_seen}"
    else:
        reason_code = "process_exit"
        err = "container exited before printing its result line"
    err_tail = ("".join(stderr_buf))[-800:]
    await emit({**base, "type": f"{event_type_prefix}.error",
               "reason_code": reason_code,
               "cancelled": bool(cancelled or control.cancel_event.is_set()),
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
