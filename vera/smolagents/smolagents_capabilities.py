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

Capabilities registered
------------------------
  smolagents.status        - docker + image availability, config
  smolagents.image.ensure  - build the vera-smolagents image if missing
  smolagents.run           - run one goal through a smolagents CodeAgent

Configuration (env or runtime)
--------------------------------
  SMOLAGENTS_ENABLED   "0" | "1"   (default "0" - opt-in)
  SMOLAGENTS_IMAGE      default "vera-smolagents:latest"
  SMOLAGENTS_TIMEOUT_S  default "300" - hard ceiling per run (docker itself
                        is killed past this; smolagents' own max_steps is a
                        softer bound reached first in the normal case)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from Vera.vera.capability_orchestration import capability, emit_event, now_iso

log = logging.getLogger("smolagents_bridge")

_ENABLED = os.environ.get("SMOLAGENTS_ENABLED", "0") == "1"
_IMAGE = os.environ.get("SMOLAGENTS_IMAGE", "vera-smolagents:latest")
_TIMEOUT_S = int(os.environ.get("SMOLAGENTS_TIMEOUT_S", "300") or 300)
_DOCKERFILE_DIR = str(Path(__file__).parent)


async def _sh(argv: list, timeout: float = 60) -> Dict[str, Any]:
    """Run a host command, capturing stdout/stderr. Never raises."""
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


@capability(
    "smolagents.run",
    http_method="POST", http_path="/smolagents/run", http_tags=["smolagents"],
    memory="on",
    description="Run ONE goal through a smolagents CodeAgent, in a fresh, "
                "throwaway Docker container (never in Vera's own process). "
                "Not a streaming call — blocks until the run finishes, fails, "
                "or the timeout (SMOLAGENTS_TIMEOUT_S) kills the container, "
                "same 'stall becomes a clean error, not a hang' contract as "
                "the chat UI's OpenClaw bridge. Model comes from Vera's own "
                "GPU Ollama instance (OLLAMA_GPU_URL/OLLAMA_MODEL env). "
                "Input: goal (str!), session_id (str). "
                "Output: {ok, answer, steps, elapsed_s, error}.",
)
async def smolagents_run(goal: str, session_id: str = "", trace_id=None) -> Dict[str, Any]:
    if not _ENABLED:
        return {"ok": False, "error": "smolagents bridge disabled "
                                      "(set SMOLAGENTS_ENABLED=1)"}
    goal = (goal or "").strip()
    if not goal:
        return {"ok": False, "error": "goal required"}

    ollama_url = os.environ.get("OLLAMA_GPU_URL", "") or os.environ.get("OLLAMA_CPU_A_URL", "")
    ollama_model = os.environ.get("OLLAMA_MODEL", "")
    if not ollama_url or not ollama_model:
        return {"ok": False, "error": "OLLAMA_GPU_URL/OLLAMA_MODEL not configured on this instance"}

    if not await _image_present():
        ens = await smolagents_image_ensure()
        if not ens.get("ok"):
            return {"ok": False, "error": f"vera-smolagents image unavailable: "
                                          f"{ens.get('log', '')[-400:]}"}

    run_id = uuid.uuid4().hex[:12]
    name = f"vera-smolagents-{run_id}"
    await emit_event({"type": "smolagents.run.start", "run_id": run_id,
                      "session_id": session_id, "goal": goal[:200], "ts": now_iso()})

    argv = ["docker", "run", "--rm", "--name", name,
            "-e", f"GOAL={goal}",
            "-e", f"OLLAMA_BASE_URL={ollama_url}",
            "-e", f"OLLAMA_MODEL={ollama_model}",
            "--memory", "1g", "--cpus", "2",
            "--network", "host",
            _IMAGE]

    t0 = time.time()
    r = await _sh(argv, timeout=_TIMEOUT_S)
    elapsed = round(time.time() - t0, 2)

    out = r.get("out", "")
    line = next((ln for ln in out.splitlines() if ln.startswith("SMOLAGENTS_RESULT:")), "")
    if not line:
        # Container was killed by the timeout, crashed before printing its
        # result line, or docker itself failed to run — surface plainly
        # rather than silently returning nothing (today's Ollama-stall
        # lesson: a hung/killed external run must become a clear error).
        err = (r.get("err", "") or "no SMOLAGENTS_RESULT line in output")[-800:]
        await emit_event({"type": "smolagents.run.error", "run_id": run_id,
                          "session_id": session_id, "error": err, "elapsed_s": elapsed})
        return {"ok": False, "error": err, "elapsed_s": elapsed,
                "log_tail": out[-800:]}

    try:
        result = json.loads(line[len("SMOLAGENTS_RESULT:"):])
    except Exception as e:
        return {"ok": False, "error": f"could not parse result: {e}",
                "elapsed_s": elapsed, "raw": line[:800]}

    result.setdefault("elapsed_s", elapsed)
    await emit_event({"type": "smolagents.run.done", "run_id": run_id,
                      "session_id": session_id, "ok": result.get("ok", False),
                      "elapsed_s": elapsed})
    return result
