"""
pydanticai_capabilities.py — PydanticAI Agentic Loop Integration for Vera
============================================================================
Optional module. A third external agent-loop bridge, alongside smolagents
(code-as-action) and LangGraph (message graph, JSON tool-calls) — none of
which this module is imported by or imports. PydanticAI's own paradigm:
typed/schema-first, with a first-class ThinkingPart in the model's response
graph (visible reasoning, not bolted on). See pydanticai_entrypoint.py's
header for the full paradigm note.

Runs in a fresh, throwaway Docker container per invocation (image:
vera-pydanticai, built from Dockerfile.pydanticai in this directory) —
never in Vera's own process, never in vera:latest.

Unlike smolagents/langgraph (built before the shared runtime existed, so
each hand-rolled its own container-streaming logic), this bridge is thin by
design: all the docker-launch/stream/stall/event-emit plumbing lives in
agentbridge_runtime.py's stream_bridge_container(), shared by every
container-based bridge. This module only supplies what's genuinely
bridge-specific: the image, the argv, and the event-type prefix.

pydanticai.run is EVENT-DRIVEN from day one, same contract as smolagents.run/
langgraph.run/openclaw.prompt: returns immediately once the container is
launched. Progress arrives as events on GET /events, filtered by run_id:
  pydanticai.run.start / .step / .done / .error

Capabilities registered
------------------------
  pydanticai.status        - docker + image availability, config
  pydanticai.image.ensure  - build the vera-pydanticai image if missing
  pydanticai.run           - launch one goal through a PydanticAI agent

Configuration (env or runtime)
--------------------------------
  PYDANTICAI_ENABLED   "0" | "1"   (default "0" - opt-in)
  PYDANTICAI_IMAGE      default "vera-pydanticai:latest"
  PYDANTICAI_TIMEOUT_S  default "300"
  PYDANTICAI_STALL_S    default "60"
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Dict

from Vera.vera.agentbridges.agentbridge_runtime import (
    build_image, image_present, stream_bridge_container,
)
from Vera.vera.capability_orchestration import (
    capability, emit_event, now_iso, OLLAMA_INSTANCES, OLLAMA_MODEL,
)

_ENABLED = os.environ.get("PYDANTICAI_ENABLED", "0") == "1"
_IMAGE = os.environ.get("PYDANTICAI_IMAGE", "vera-pydanticai:latest")
_TIMEOUT_S = int(os.environ.get("PYDANTICAI_TIMEOUT_S", "300") or 300)
_STALL_S = int(os.environ.get("PYDANTICAI_STALL_S", "60") or 60)
_DOCKERFILE_DIR = str(Path(__file__).parent)
_EVENT_PREFIX = "pydanticai.run"


def _pick_ollama_url() -> str:
    """Same lesson as smolagents_capabilities.py's _pick_ollama_url(): the
    real source of truth is OLLAMA_INSTANCES, not env vars."""
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
    "pydanticai.status",
    http_method="GET", http_path="/pydanticai/status", http_tags=["pydanticai"],
    memory="off",
    description="PydanticAI bridge status: enabled flag, docker availability, "
                "whether the vera-pydanticai image exists yet. "
                "Output: {enabled, docker_ok, image, image_present}.",
)
async def pydanticai_status(trace_id=None) -> Dict[str, Any]:
    from Vera.vera.agentbridges.agentbridge_runtime import sh
    docker_ok = (await sh(["docker", "version", "--format", "{{.Server.Version}}"],
                          timeout=10)).get("ok", False)
    present = await image_present(_IMAGE) if docker_ok else False
    return {"enabled": _ENABLED, "docker_ok": docker_ok,
            "image": _IMAGE, "image_present": present,
            "timeout_s": _TIMEOUT_S}


@capability(
    "pydanticai.image.ensure",
    http_method="POST", http_path="/pydanticai/image/ensure", http_tags=["pydanticai"],
    memory="off",
    description="Build the vera-pydanticai image if it doesn't already exist "
                "(or always, with force=true). Deliberately a SEPARATE image "
                "from vera:latest. Inputs: force (bool). "
                "Output: {ok, present, action, log}.",
)
async def pydanticai_image_ensure(force: bool = False, trace_id=None) -> Dict[str, Any]:
    if not force and await image_present(_IMAGE):
        return {"ok": True, "present": True, "action": "none"}
    await emit_event({"type": "pydanticai.image.build", "image": _IMAGE})
    r = await build_image(_IMAGE, str(Path(_DOCKERFILE_DIR) / "Dockerfile.pydanticai"),
                          _DOCKERFILE_DIR)
    await emit_event({"type": "pydanticai.image.ensured", "image": _IMAGE, "ok": r["ok"]})
    return {**r, "action": "build"}


@capability(
    "pydanticai.run",
    http_method="POST", http_path="/pydanticai/run", http_tags=["pydanticai"],
    memory="on",
    description="Launch ONE goal through a PydanticAI agent (typed/schema-"
                "first — a third distinct paradigm from smolagents' "
                "code-as-action and LangGraph's message graph), in a fresh, "
                "throwaway Docker container (never in Vera's own process). "
                "EVENT-DRIVEN: returns IMMEDIATELY once the container is "
                "launched — {ok, run_id, status:'running'}. Live progress "
                "(including the model's own reasoning — PydanticAI surfaces "
                "thinking as a first-class step) and the final result arrive "
                "as events on GET /events, filtered by run_id: "
                "pydanticai.run.step, pydanticai.run.done ({ok, answer, "
                "steps, elapsed_s, model} under result), pydanticai.run.error. "
                "A stalled or over-time container is killed and reported as "
                "an error rather than left to hang. Model/instance come from "
                "Vera's own live OLLAMA_INSTANCES registry, not env vars. "
                "Input: goal (str!), session_id (str). "
                "Output: {ok, run_id, status} | {ok:false, error}.",
)
async def pydanticai_run(goal: str, session_id: str = "", trace_id=None) -> Dict[str, Any]:
    if not _ENABLED:
        return {"ok": False, "error": "PydanticAI bridge disabled "
                                      "(set PYDANTICAI_ENABLED=1)"}
    goal = (goal or "").strip()
    if not goal:
        return {"ok": False, "error": "goal required"}

    ollama_url = _pick_ollama_url()
    ollama_model = OLLAMA_MODEL
    if not ollama_url or not ollama_model:
        return {"ok": False, "error": "no Ollama instance available "
                                      "(OLLAMA_INSTANCES empty or OLLAMA_MODEL unset)"}

    if not await image_present(_IMAGE):
        ens = await pydanticai_image_ensure()
        if not ens.get("ok"):
            return {"ok": False, "error": f"vera-pydanticai image unavailable: "
                                          f"{ens.get('log', '')[-400:]}"}

    run_id = uuid.uuid4().hex[:12]
    await emit_event({"type": f"{_EVENT_PREFIX}.start", "run_id": run_id,
                      "session_id": session_id, "goal": goal[:200], "ts": now_iso()})

    argv = ["docker", "run", "--rm", "--name", f"vera-pydanticai-{run_id}",
            "-e", f"GOAL={goal}",
            "-e", f"OLLAMA_BASE_URL={ollama_url}",
            "-e", f"OLLAMA_MODEL={ollama_model}",
            "--memory", "1g", "--cpus", "2",
            "--network", "host",
            _IMAGE]

    import asyncio
    asyncio.ensure_future(stream_bridge_container(
        run_id=run_id, session_id=session_id, argv=argv,
        event_type_prefix=_EVENT_PREFIX, emit=emit_event,
        timeout_s=_TIMEOUT_S, stall_s=_STALL_S,
        progress_kinds={"tool_call", "tool_result"},
    ))

    return {"ok": True, "run_id": run_id, "status": "running"}
