"""
langgraph_capabilities.py — LangGraph Agentic Loop Integration for Vera
============================================================================
Optional module. Phase 3 of the external-agentic-loop plan: a genuinely
independent agent-loop library (langchain-ai/langgraph), selectable in the
chat UI's Loop pane alongside Vera's own v1-v8, OpenClaw, and smolagents —
none of which this module is imported by or imports.

Deliberately the OPPOSITE paradigm from smolagents (which runs LLM-generated
Python as its action mechanism): LangGraph is an explicit graph of nodes and
edges, with real structured tool-calling (JSON tool-call protocol) rather
than code-as-action. This module wires up langgraph.prebuilt's ReAct agent
(the library's own proven, minimal reference agent) against a real Ollama
instance via its OpenAI-compatible surface — same trick smolagents uses.

Runs in a fresh, throwaway Docker container per invocation (image:
vera-langgraph, built from Dockerfile.langgraph in this directory) — never
in Vera's own process, never in vera:latest. See Dockerfile.langgraph.

Capabilities registered
------------------------
  langgraph.status        - docker + image availability, config
  langgraph.image.ensure  - build the vera-langgraph image if missing
  langgraph.run           - run one goal through a LangGraph ReAct agent

Configuration (env or runtime)
--------------------------------
  LANGGRAPH_ENABLED   "0" | "1"   (default "0" - opt-in)
  LANGGRAPH_IMAGE      default "vera-langgraph:latest"
  LANGGRAPH_TIMEOUT_S  default "300" - hard ceiling per run (docker itself
                        is killed past this; the agent's own max_steps is a
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

from Vera.vera.capability_orchestration import (
    capability, emit_event, now_iso, OLLAMA_INSTANCES, OLLAMA_MODEL,
)

log = logging.getLogger("langgraph_bridge")

_ENABLED = os.environ.get("LANGGRAPH_ENABLED", "0") == "1"
_IMAGE = os.environ.get("LANGGRAPH_IMAGE", "vera-langgraph:latest")
_TIMEOUT_S = int(os.environ.get("LANGGRAPH_TIMEOUT_S", "300") or 300)
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


def _pick_ollama_url() -> str:
    """Same lesson as smolagents_capabilities.py's _pick_ollama_url(): Vera
    does NOT configure Ollama instances via env vars on prod's own process
    (confirmed empty via /proc/<pid>/environ) — the real source of truth is
    OLLAMA_INSTANCES, the same in-memory registry ollama_generate()/
    pick_instance() read from. Prefers a GPU instance that's currently
    reporting online; falls back to any online instance, then to whatever's
    registered at all."""
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
    "langgraph.status",
    http_method="GET", http_path="/langgraph/status", http_tags=["langgraph"],
    memory="off",
    description="LangGraph bridge status: enabled flag, docker availability, "
                "whether the vera-langgraph image exists yet. "
                "Output: {enabled, docker_ok, image, image_present}.",
)
async def langgraph_status(trace_id=None) -> Dict[str, Any]:
    docker_ok = (await _sh(["docker", "version", "--format", "{{.Server.Version}}"],
                           timeout=10)).get("ok", False)
    present = await _image_present() if docker_ok else False
    return {"enabled": _ENABLED, "docker_ok": docker_ok,
            "image": _IMAGE, "image_present": present,
            "timeout_s": _TIMEOUT_S}


@capability(
    "langgraph.image.ensure",
    http_method="POST", http_path="/langgraph/image/ensure", http_tags=["langgraph"],
    memory="off",
    description="Build the vera-langgraph image if it doesn't already exist "
                "(or always, with force=true). Deliberately a SEPARATE image "
                "from vera:latest — see Dockerfile.langgraph. "
                "Inputs: force (bool). Output: {ok, present, action, log}.",
)
async def langgraph_image_ensure(force: bool = False, trace_id=None) -> Dict[str, Any]:
    if not force and await _image_present():
        return {"ok": True, "present": True, "action": "none"}
    await emit_event({"type": "langgraph.image.build", "image": _IMAGE})
    r = await _sh(
        ["docker", "build", "-t", _IMAGE, "-f",
         str(Path(_DOCKERFILE_DIR) / "Dockerfile.langgraph"), _DOCKERFILE_DIR],
        timeout=600)
    present = await _image_present()
    await emit_event({"type": "langgraph.image.ensured", "image": _IMAGE, "ok": present})
    return {"ok": present, "present": present, "action": "build",
            "log": (r.get("out", "") + "\n" + r.get("err", ""))[-2500:]}


@capability(
    "langgraph.run",
    http_method="POST", http_path="/langgraph/run", http_tags=["langgraph"],
    memory="on",
    description="Run ONE goal through a LangGraph ReAct agent (langgraph."
                "prebuilt.create_react_agent, structured tool-calling — the "
                "opposite paradigm from smolagents' code-as-action), in a "
                "fresh, throwaway Docker container (never in Vera's own "
                "process). Not a streaming call — blocks until the run "
                "finishes, fails, or the timeout (LANGGRAPH_TIMEOUT_S) kills "
                "the container, same 'stall becomes a clean error, not a "
                "hang' contract as smolagents/OpenClaw. Model/instance come "
                "from Vera's own live OLLAMA_INSTANCES registry, not env "
                "vars. Input: goal (str!), session_id (str). "
                "Output: {ok, answer, steps, elapsed_s, error}.",
)
async def langgraph_run(goal: str, session_id: str = "", trace_id=None) -> Dict[str, Any]:
    if not _ENABLED:
        return {"ok": False, "error": "LangGraph bridge disabled "
                                      "(set LANGGRAPH_ENABLED=1)"}
    goal = (goal or "").strip()
    if not goal:
        return {"ok": False, "error": "goal required"}

    ollama_url = _pick_ollama_url()
    ollama_model = OLLAMA_MODEL
    if not ollama_url or not ollama_model:
        return {"ok": False, "error": "no Ollama instance available "
                                      "(OLLAMA_INSTANCES empty or OLLAMA_MODEL unset)"}

    if not await _image_present():
        ens = await langgraph_image_ensure()
        if not ens.get("ok"):
            return {"ok": False, "error": f"vera-langgraph image unavailable: "
                                          f"{ens.get('log', '')[-400:]}"}

    run_id = uuid.uuid4().hex[:12]
    name = f"vera-langgraph-{run_id}"
    await emit_event({"type": "langgraph.run.start", "run_id": run_id,
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
    line = next((ln for ln in out.splitlines() if ln.startswith("LANGGRAPH_RESULT:")), "")
    if not line:
        # Container was killed by the timeout, crashed before printing its
        # result line, or docker itself failed to run — surface plainly
        # rather than silently returning nothing.
        err = (r.get("err", "") or "no LANGGRAPH_RESULT line in output")[-800:]
        await emit_event({"type": "langgraph.run.error", "run_id": run_id,
                          "session_id": session_id, "error": err, "elapsed_s": elapsed})
        return {"ok": False, "error": err, "elapsed_s": elapsed,
                "log_tail": out[-800:]}

    try:
        result = json.loads(line[len("LANGGRAPH_RESULT:"):])
    except Exception as e:
        return {"ok": False, "error": f"could not parse result: {e}",
                "elapsed_s": elapsed, "raw": line[:800]}

    result.setdefault("elapsed_s", elapsed)
    await emit_event({"type": "langgraph.run.done", "run_id": run_id,
                      "session_id": session_id, "ok": result.get("ok", False),
                      "elapsed_s": elapsed})
    return result
