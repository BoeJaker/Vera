"""
smolagents_capabilities.py — smolagents Agentic Loop Integration for Vera
============================================================================
Optional module. Phase 2 of the external-agentic-loop plan: a genuinely
independent, minimal (huggingface/smolagents, ~1,000 LOC core) agent loop,
selectable in the chat UI's Loop pane alongside Vera's own v1-v8, OpenClaw,
LangGraph and PydanticAI — none of which this module is imported by or
imports.

Unlike the OpenClaw bridge (a long-lived WS connection to an already-running
external gateway), smolagents is a Python LIBRARY whose CodeAgent executes
LLM-GENERATED PYTHON as its action mechanism. That code runs in a fresh,
throwaway Docker container per invocation (image: vera-smolagents, built
from Dockerfile.smolagents in this directory) — never in Vera's own process,
never in vera:latest. See Dockerfile.smolagents's own header for why.

The actual docker-launch/stream/stall/event-emit plumbing lives in
agentbridge_runtime.py's stream_bridge_container(), shared by every
container-based bridge (2026-08-16 — this module and langgraph_capabilities.py
each hand-rolled their own copy of it first; extracted once a third bridge,
PydanticAI, was about to become a fourth copy). This module supplies only
what's genuinely smolagents-specific: the image, argv, and event-type prefix.

smolagents.run is EVENT-DRIVEN, same contract as openclaw.prompt: it returns
immediately once the container is launched, with {ok, run_id,
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
  SMOLAGENTS_STALL_S    default "60" - no new BRIDGE_STEP/RESULT line for
                        this long -> treated as stalled and killed
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any, Dict

from Vera.vera.agentbridges.agentbridge_runtime import (
    build_image, image_present, sh, stream_bridge_container,
)
from Vera.vera.capability_orchestration import (
    capability, emit_event, now_iso, OLLAMA_INSTANCES, OLLAMA_MODEL,
)

_ENABLED = os.environ.get("SMOLAGENTS_ENABLED", "0") == "1"
_IMAGE = os.environ.get("SMOLAGENTS_IMAGE", "vera-smolagents:latest")
_TIMEOUT_S = int(os.environ.get("SMOLAGENTS_TIMEOUT_S", "300") or 300)
_STALL_S = int(os.environ.get("SMOLAGENTS_STALL_S", "60") or 60)
_DOCKERFILE_DIR = str(Path(__file__).parent)
_EVENT_PREFIX = "smolagents.run"


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


@capability(
    "smolagents.status",
    http_method="GET", http_path="/smolagents/status", http_tags=["smolagents"],
    memory="off",
    description="smolagents bridge status: enabled flag, docker availability, "
                "whether the vera-smolagents image exists yet. "
                "Output: {enabled, docker_ok, image, image_present}.",
)
async def smolagents_status(trace_id=None) -> Dict[str, Any]:
    docker_ok = (await sh(["docker", "version", "--format", "{{.Server.Version}}"],
                          timeout=10)).get("ok", False)
    present = await image_present(_IMAGE) if docker_ok else False
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
    if not force and await image_present(_IMAGE):
        return {"ok": True, "present": True, "action": "none"}
    await emit_event({"type": "smolagents.image.build", "image": _IMAGE})
    r = await build_image(_IMAGE, str(Path(_DOCKERFILE_DIR) / "Dockerfile.smolagents"),
                          _DOCKERFILE_DIR)
    await emit_event({"type": "smolagents.image.ensured", "image": _IMAGE, "ok": r["ok"]})
    return {**r, "action": "build"}


@capability(
    "smolagents.run",
    http_method="POST", http_path="/smolagents/run", http_tags=["smolagents"],
    memory="on",
    description="Launch ONE goal through a smolagents CodeAgent, in a fresh, "
                "throwaway Docker container (never in Vera's own process). "
                "EVENT-DRIVEN: returns IMMEDIATELY once the container is "
                "launched — {ok, run_id, status:'running'} — does not block "
                "until the run finishes. Live progress and the final result "
                "arrive as events on GET /events, filtered by run_id: "
                "smolagents.run.step (one per streamed agent step — task/"
                "planning/action/final, as it happens), smolagents.run.done "
                "({ok, answer, steps, elapsed_s, model} under result), "
                "smolagents.run.error. A stalled or over-time container is "
                "killed and reported as smolagents.run.error rather than "
                "left to hang — same contract as the chat UI's OpenClaw "
                "bridge. Model/instance come from Vera's own live "
                "OLLAMA_INSTANCES registry, not env vars. "
                "Input: goal (str!), session_id (str). "
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

    if not await image_present(_IMAGE):
        ens = await smolagents_image_ensure()
        if not ens.get("ok"):
            return {"ok": False, "error": f"vera-smolagents image unavailable: "
                                          f"{ens.get('log', '')[-400:]}"}

    run_id = uuid.uuid4().hex[:12]
    await emit_event({"type": f"{_EVENT_PREFIX}.start", "run_id": run_id,
                      "session_id": session_id, "goal": goal[:200], "ts": now_iso()})

    argv = ["docker", "run", "--rm", "--name", f"vera-smolagents-{run_id}",
            "-e", f"GOAL={goal}",
            "-e", f"OLLAMA_BASE_URL={ollama_url}",
            "-e", f"OLLAMA_MODEL={ollama_model}",
            "--memory", "1g", "--cpus", "2",
            "--network", "host",
            _IMAGE]

    # Fire-and-emit: the caller gets run_id back now and follows progress via
    # /events, same as openclaw.prompt. Not awaited — this capability's own
    # HTTP response must not block on the container.
    asyncio.ensure_future(stream_bridge_container(
        run_id=run_id, session_id=session_id, argv=argv,
        event_type_prefix=_EVENT_PREFIX, emit=emit_event,
        timeout_s=_TIMEOUT_S, stall_s=_STALL_S,
        progress_kinds={"action"},
    ))

    return {"ok": True, "run_id": run_id, "status": "running"}
