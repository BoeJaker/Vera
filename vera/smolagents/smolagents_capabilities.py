"""
smolagents_capabilities.py — smolagents Agentic Loop Integration for Vera
============================================================================
Optional module. Phase 2 of the external-agentic-loop plan: a genuinely
independent, minimal (huggingface/smolagents, ~1,000 LOC core) agent loop,
selectable in the chat UI's Loop pane alongside Vera's own v1-v8 and
OpenClaw — none of which this module is imported by or imports.

Unlike the OpenClaw bridge (a long-lived WS connection to an already-running
external gateway), smolagents is a Python LIBRARY whose CodeAgent executes
LLM-GENERATED PYTHON as its action mechanism. That code runs in a fresh,
throwaway Docker container per invocation (image: vera-smolagents, built
from Dockerfile.smolagents in this directory) — never in Vera's own process,
never in vera:latest. See Dockerfile.smolagents's own header for why.

smolagents.run is EVENT-DRIVEN, same contract as openclaw.prompt (2026-08-16
— was a single blocking call until a live turn per step was requested):
it returns immediately once the container is launched, with {ok, run_id,
status:"running"}. The actual progress and result arrive as events on the
general bus (consume via GET /events, same as OpenClaw's chat-panel wiring):
  smolagents.run.start   - container launched
  smolagents.run.step    - one streamed agent step (task/planning/action/
                            final) as the container's own stdout produces it
                            — see smolagents_entrypoint.py's stream=True loop
  smolagents.run.done    - final result, same shape the old blocking call
                            used to return directly: {ok, answer, steps,
                            elapsed_s, model}
  smolagents.run.error   - container failed, stalled, or timed out
Every event carries {run_id, session_id} so a consumer (chat UI) can filter
to just its own run.

Capabilities registered
------------------------
  smolagents.status        - docker + image availability, config
  smolagents.image.ensure  - build the vera-smolagents image if missing
  smolagents.run           - launch one goal through a smolagents CodeAgent

Configuration (env or runtime)
--------------------------------
  SMOLAGENTS_ENABLED   "0" | "1"   (default "0" - opt-in)
  SMOLAGENTS_IMAGE      default "vera-smolagents:latest"
  SMOLAGENTS_TIMEOUT_S  default "300" - hard ceiling per run (docker itself
                        is killed past this; smolagents' own max_steps is a
                        softer bound reached first in the normal case)
  SMOLAGENTS_STALL_S    default "60" - no new SMOLAGENTS_STEP/RESULT line for
                        this long -> treated as stalled and killed (same
                        "measure genuine progress, not raw byte arrival"
                        lesson as the 2026-08-16 Ollama stream-stall fix)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from Vera.vera.capability_orchestration import (
    capability, emit_event, now_iso, OLLAMA_INSTANCES, OLLAMA_MODEL,
)

log = logging.getLogger("smolagents_bridge")

_ENABLED = os.environ.get("SMOLAGENTS_ENABLED", "0") == "1"
_IMAGE = os.environ.get("SMOLAGENTS_IMAGE", "vera-smolagents:latest")
_TIMEOUT_S = int(os.environ.get("SMOLAGENTS_TIMEOUT_S", "300") or 300)
_STALL_S = int(os.environ.get("SMOLAGENTS_STALL_S", "60") or 60)
_DOCKERFILE_DIR = str(Path(__file__).parent)


async def _sh(argv: list, timeout: float = 60) -> Dict[str, Any]:
    """Run a host command, capturing stdout/stderr. Never raises. Used only
    for the short, one-shot docker admin commands (status/build) below —
    smolagents.run itself streams its own container (see
    _stream_smolagents_container), it does not use this."""
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


def _pick_ollama_url() -> str:
    """Vera does NOT configure Ollama instances via env vars — OLLAMA_GPU_URL
    etc. are only ever the names copied INTO a spawned worker container's
    environment (see docker_capabilities.py's _DEFAULT_BACKEND_ENV); on prod's
    own process those names are never set (confirmed empty via /proc/<pid>/
    environ). The real source of truth is OLLAMA_INSTANCES, the same
    in-memory registry ollama_generate()/pick_instance() read from. Prefers a
    GPU instance that's currently reporting online; falls back to any
    online instance, then to whatever's registered at all."""
    online_gpu = [i for i in OLLAMA_INSTANCES.values()
                  if i.get("has_gpu") and i.get("status") == "online" and i.get("enabled", True)]
    if online_gpu:
        return online_gpu[0]["url"]
    online_any = [i for i in OLLAMA_INSTANCES.values()
                  if i.get("status") == "online" and i.get("enabled", True)]
    if online_any:
        return online_any[0]["url"]
    any_inst = list(OLLAMA_INSTANCES.values())
    return any_inst[0]["url"] if any_inst else ""


async def _image_present() -> bool:
    r = await _sh(["docker", "image", "inspect", _IMAGE], timeout=15)
    return bool(r.get("ok"))


@capability(
    "smolagents.status",
    http_method="GET", http_path="/smolagents/status", http_tags=["smolagents"],
    memory="off",
    description="smolagents bridge status: enabled flag, docker availability, "
                "whether the vera-smolagents image exists yet. "
                "Output: {enabled, docker_ok, image, image_present}.",
)
async def smolagents_status(trace_id=None) -> Dict[str, Any]:
    docker_ok = (await _sh(["docker", "version", "--format", "{{.Server.Version}}"],
                           timeout=10)).get("ok", False)
    present = await _image_present() if docker_ok else False
    return {"enabled": _ENABLED, "docker_ok": docker_ok,
            "image": _IMAGE, "image_present": present,
            "timeout_s": _TIMEOUT_S}


@capability(
    "smolagents.image.ensure",
    http_method="POST", http_path="/smolagents/image/ensure", http_tags=["smolagents"],
    memory="off",
    description="Build the vera-smolagents image if it doesn't already exist "
                "(or always, with force=true). Deliberately a SEPARATE image "
                "from vera:latest — see Dockerfile.smolagents. "
                "Inputs: force (bool). Output: {ok, present, action, log}.",
)
async def smolagents_image_ensure(force: bool = False, trace_id=None) -> Dict[str, Any]:
    if not force and await _image_present():
        return {"ok": True, "present": True, "action": "none"}
    await emit_event({"type": "smolagents.image.build", "image": _IMAGE})
    r = await _sh(
        ["docker", "build", "-t", _IMAGE, "-f",
         str(Path(_DOCKERFILE_DIR) / "Dockerfile.smolagents"), _DOCKERFILE_DIR],
        timeout=600)
    present = await _image_present()
    await emit_event({"type": "smolagents.image.ensured", "image": _IMAGE, "ok": present})
    return {"ok": present, "present": present, "action": "build",
            "log": (r.get("out", "") + "\n" + r.get("err", ""))[-2500:]}


async def _drain_stderr(stream: asyncio.StreamReader, buf: List[str]) -> None:
    """Consume the container's stderr concurrently with the stdout-reading
    loop below. Not optional: an asyncio subprocess with two pipes where only
    one is read can deadlock once the unread one's OS buffer fills — this
    keeps stderr moving and captures a tail for error reporting."""
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


async def _stream_smolagents_container(run_id: str, session_id: str, goal: str,
                                        ollama_url: str, ollama_model: str) -> None:
    """Runs the throwaway container, emitting one smolagents.run.step event
    per SMOLAGENTS_STEP: line as it arrives (real-time progress, not a
    post-hoc replay) and a final smolagents.run.done/error when it finishes.
    This is the background task smolagents_run() launches and returns from
    immediately — same shape as openclaw.prompt's own fire-and-emit contract."""
    name = f"vera-smolagents-{run_id}"
    argv = ["docker", "run", "--rm", "--name", name,
            "-e", f"GOAL={goal}",
            "-e", f"OLLAMA_BASE_URL={ollama_url}",
            "-e", f"OLLAMA_MODEL={ollama_model}",
            "--memory", "1g", "--cpus", "2",
            "--network", "host",
            _IMAGE]

    t0 = time.time()
    base = {"run_id": run_id, "session_id": session_id}

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except Exception as e:
        await emit_event({**base, "type": "smolagents.run.error",
                          "error": f"could not start container: {e}",
                          "elapsed_s": round(time.time() - t0, 2)})
        return

    stderr_buf: List[str] = []
    stderr_task = asyncio.ensure_future(_drain_stderr(proc.stderr, stderr_buf))

    result: Optional[Dict[str, Any]] = None
    action_steps_seen = 0
    stalled = False
    timed_out = False

    try:
        while True:
            remaining = _TIMEOUT_S - (time.time() - t0)
            if remaining <= 0:
                timed_out = True
                break
            wait_for = min(_STALL_S, remaining)
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=wait_for)
            except asyncio.TimeoutError:
                stalled = True
                break
            if not raw:
                break  # EOF — process finished producing output
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if line.startswith("SMOLAGENTS_STEP:"):
                try:
                    info = json.loads(line[len("SMOLAGENTS_STEP:"):])
                except Exception:
                    continue
                if info.get("kind") == "action":
                    action_steps_seen += 1
                await emit_event({**base, "type": "smolagents.run.step", **info})
            elif line.startswith("SMOLAGENTS_RESULT:"):
                try:
                    result = json.loads(line[len("SMOLAGENTS_RESULT:"):])
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
        result.setdefault("steps", action_steps_seen)
        await emit_event({**base, "type": "smolagents.run.done",
                          "ok": result.get("ok", False), "elapsed_s": elapsed,
                          "result": result})
        return

    if stalled:
        err = f"no progress for {_STALL_S}s (stalled) — action steps seen: {action_steps_seen}"
    elif timed_out:
        err = f"timed out after {_TIMEOUT_S}s — action steps seen: {action_steps_seen}"
    else:
        err = "container exited before printing SMOLAGENTS_RESULT"
    err_tail = ("".join(stderr_buf))[-800:]
    await emit_event({**base, "type": "smolagents.run.error",
                      "error": err, "stderr_tail": err_tail, "elapsed_s": elapsed})


@capability(
    "smolagents.run",
    http_method="POST", http_path="/smolagents/run", http_tags=["smolagents"],
    memory="on",
    description="Launch ONE goal through a smolagents CodeAgent, in a fresh, "
                "throwaway Docker container (never in Vera's own process). "
                "EVENT-DRIVEN (2026-08-16): returns IMMEDIATELY once the "
                "container is launched — {ok, run_id, status:'running'} — "
                "does not block until the run finishes. Live progress and "
                "the final result arrive as events on GET /events, filtered "
                "by run_id: smolagents.run.step (one per streamed agent "
                "step — task/planning/action/final, as it happens), "
                "smolagents.run.done ({ok, answer, steps, elapsed_s, model} "
                "under result), smolagents.run.error. A stalled or "
                "over-time container is killed and reported as "
                "smolagents.run.error rather than left to hang — same "
                "contract as the chat UI's OpenClaw bridge. Model/instance "
                "come from Vera's own live OLLAMA_INSTANCES registry "
                "(whatever ollama_generate() itself would route to), not "
                "env vars. Input: goal (str!), session_id (str). "
                "Output: {ok, run_id, status} | {ok:false, error}.",
)
async def smolagents_run(goal: str, session_id: str = "", trace_id=None) -> Dict[str, Any]:
    if not _ENABLED:
        return {"ok": False, "error": "smolagents bridge disabled "
                                      "(set SMOLAGENTS_ENABLED=1)"}
    goal = (goal or "").strip()
    if not goal:
        return {"ok": False, "error": "goal required"}

    ollama_url = _pick_ollama_url()
    ollama_model = OLLAMA_MODEL
    if not ollama_url or not ollama_model:
        return {"ok": False, "error": "no Ollama instance available "
                                      "(OLLAMA_INSTANCES empty or OLLAMA_MODEL unset)"}

    if not await _image_present():
        ens = await smolagents_image_ensure()
        if not ens.get("ok"):
            return {"ok": False, "error": f"vera-smolagents image unavailable: "
                                          f"{ens.get('log', '')[-400:]}"}

    run_id = uuid.uuid4().hex[:12]
    await emit_event({"type": "smolagents.run.start", "run_id": run_id,
                      "session_id": session_id, "goal": goal[:200], "ts": now_iso()})

    # Fire-and-emit: the caller gets run_id back now and follows progress via
    # /events, same as openclaw.prompt. Not awaited — this capability's own
    # HTTP response must not block on the container.
    asyncio.ensure_future(_stream_smolagents_container(
        run_id, session_id, goal, ollama_url, ollama_model))

    return {"ok": True, "run_id": run_id, "status": "running"}
