"""
langgraph_capabilities.py — LangGraph Agentic Loop Integration for Vera
============================================================================
Optional module. Phase 3 of the external-agentic-loop plan: a genuinely
independent agent-loop library (langchain-ai/langgraph), selectable in the
chat UI's Loop pane alongside Vera's own v1-v8, OpenClaw, smolagents and
PydanticAI — none of which this module is imported by or imports.

Deliberately the OPPOSITE paradigm from smolagents (which runs LLM-generated
Python as its action mechanism): LangGraph is an explicit graph of nodes and
edges, with real structured tool-calling (JSON tool-call protocol) rather
than code-as-action. This module wires up langgraph.prebuilt's ReAct agent
(the library's own proven, minimal reference agent) against a real Ollama
instance via its OpenAI-compatible surface — same trick smolagents uses.

Runs in a fresh, throwaway Docker container per invocation (image:
vera-langgraph, built from Dockerfile.langgraph in this directory) — never
in Vera's own process, never in vera:latest. See Dockerfile.langgraph.

The actual docker-launch/stream/stall/event-emit plumbing lives in
agentbridge_runtime.py's stream_bridge_container(), shared by every
container-based bridge (2026-08-16 — this module and smolagents_capabilities.py
each hand-rolled their own copy of it first; extracted once a third bridge,
PydanticAI, was about to become a fourth copy). This module supplies only
what's genuinely langgraph-specific: the image, argv, and event-type prefix.

langgraph.run is EVENT-DRIVEN, same contract as smolagents.run/openclaw.
prompt: it returns immediately once the container is launched, with {ok,
run_id, status:"running"}. The actual progress and result arrive as events
on the general bus (consume via GET /events):
  langgraph.run.start   - container launched
  langgraph.run.step    - one new message in the graph's growing state, as
                          the container's own stdout produces it — see
                          langgraph_entrypoint.py's stream_mode="values" loop
  langgraph.run.done    - final result: {ok, answer, steps, elapsed_s, model}
  langgraph.run.error   - container failed, stalled, or timed out
Every event carries {run_id, session_id} so a consumer (chat UI) can filter
to just its own run.

Capabilities registered
------------------------
  langgraph.status        - docker + image availability, config
  langgraph.image.ensure  - build the vera-langgraph image if missing
  langgraph.run           - launch one goal through a LangGraph ReAct agent

Configuration (env or runtime)
--------------------------------
  LANGGRAPH_ENABLED   "0" | "1"   (default "0" - opt-in)
  LANGGRAPH_IMAGE      default "vera-langgraph:latest"
  LANGGRAPH_TIMEOUT_S  default "300" - hard ceiling per run (docker itself
                        is killed past this; the agent's own max_steps is a
                        softer bound reached first in the normal case)
  LANGGRAPH_STALL_S    default "60" - no new BRIDGE_STEP/RESULT line for
                        this long -> treated as stalled and killed
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any, Dict

from Vera.vera.agentbridges.agentbridge_runtime import (
    build_image, image_present, pick_ollama_instance, sh, stream_bridge_container,
)
from Vera.vera.capability_orchestration import (
    capability, emit_event, now_iso, OLLAMA_INSTANCES, OLLAMA_MODEL,
)

_ENABLED = os.environ.get("LANGGRAPH_ENABLED", "0") == "1"
_IMAGE = os.environ.get("LANGGRAPH_IMAGE", "vera-langgraph:latest")
_TIMEOUT_S = int(os.environ.get("LANGGRAPH_TIMEOUT_S", "300") or 300)
_STALL_S = int(os.environ.get("LANGGRAPH_STALL_S", "60") or 60)
_DOCKERFILE_DIR = str(Path(__file__).parent)
_EVENT_PREFIX = "langgraph.run"


@capability(
    "langgraph.status",
    http_method="GET", http_path="/langgraph/status", http_tags=["langgraph"],
    memory="off",
    description="LangGraph bridge status: enabled flag, docker availability, "
                "whether the vera-langgraph image exists yet. "
                "Output: {enabled, docker_ok, image, image_present}.",
)
async def langgraph_status(trace_id=None) -> Dict[str, Any]:
    docker_ok = (await sh(["docker", "version", "--format", "{{.Server.Version}}"],
                          timeout=10)).get("ok", False)
    present = await image_present(_IMAGE) if docker_ok else False
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
    if not force and await image_present(_IMAGE):
        return {"ok": True, "present": True, "action": "none"}
    await emit_event({"type": "langgraph.image.build", "image": _IMAGE})
    r = await build_image(_IMAGE, str(Path(_DOCKERFILE_DIR) / "Dockerfile.langgraph"),
                          _DOCKERFILE_DIR)
    await emit_event({"type": "langgraph.image.ensured", "image": _IMAGE, "ok": r["ok"]})
    return {**r, "action": "build"}


@capability(
    "langgraph.run",
    http_method="POST", http_path="/langgraph/run", http_tags=["langgraph"],
    memory="on",
    description="Run ONE goal through a LangGraph ReAct agent (langgraph."
                "prebuilt.create_react_agent, structured tool-calling — the "
                "opposite paradigm from smolagents' code-as-action), in a "
                "fresh, throwaway Docker container (never in Vera's own "
                "process). EVENT-DRIVEN: returns IMMEDIATELY once the "
                "container is launched — {ok, run_id, status:'running'} — "
                "does not block until the run finishes. Live progress and "
                "the final result arrive as events on GET /events, filtered "
                "by run_id: langgraph.run.step (one per new graph-state "
                "message — tool_call/tool_result/ai — as it happens), "
                "langgraph.run.done ({ok, answer, steps, elapsed_s, model} "
                "under result), langgraph.run.error. A stalled or over-time "
                "container is killed and reported as langgraph.run.error "
                "rather than left to hang — same contract as smolagents/"
                "OpenClaw. Model/instance come from Vera's own live "
                "OLLAMA_INSTANCES registry, not env vars. "
                "Input: goal (str!), session_id (str). "
                "Output: {ok, run_id, status} | {ok:false, error}.",
)
async def langgraph_run(goal: str, session_id: str = "", trace_id=None) -> Dict[str, Any]:
    if not _ENABLED:
        return {"ok": False, "error": "LangGraph bridge disabled "
                                      "(set LANGGRAPH_ENABLED=1)"}
    goal = (goal or "").strip()
    if not goal:
        return {"ok": False, "error": "goal required"}

    instance_id, ollama_url = pick_ollama_instance(OLLAMA_INSTANCES)
    ollama_model = OLLAMA_MODEL
    if not ollama_url or not ollama_model:
        return {"ok": False, "error": "no Ollama instance available "
                                      "(OLLAMA_INSTANCES empty or OLLAMA_MODEL unset)"}

    if not await image_present(_IMAGE):
        ens = await langgraph_image_ensure()
        if not ens.get("ok"):
            return {"ok": False, "error": f"vera-langgraph image unavailable: "
                                          f"{ens.get('log', '')[-400:]}"}

    run_id = uuid.uuid4().hex[:12]
    await emit_event({"type": f"{_EVENT_PREFIX}.start", "run_id": run_id,
                      "session_id": session_id, "goal": goal[:200], "ts": now_iso()})

    argv = ["docker", "run", "--rm", "--name", f"vera-langgraph-{run_id}",
            "-e", f"GOAL={goal}",
            "-e", f"OLLAMA_BASE_URL={ollama_url}",
            "-e", f"OLLAMA_MODEL={ollama_model}",
            "--memory", "1g", "--cpus", "2",
            "--network", "host",
            _IMAGE]

    # Fire-and-emit: the caller gets run_id back now and follows progress via
    # /events, same as smolagents.run/openclaw.prompt. Not awaited — this
    # capability's own HTTP response must not block on the container.
    # gate_instance_id makes this container's Ollama call queue behind
    # Vera's own gated generation instead of firing blind and risking a
    # stall-kill under contention.
    asyncio.ensure_future(stream_bridge_container(
        run_id=run_id, session_id=session_id, argv=argv,
        event_type_prefix=_EVENT_PREFIX, emit=emit_event,
        timeout_s=_TIMEOUT_S, stall_s=_STALL_S,
        progress_kinds={"tool_call", "tool_result"},
        gate_instance_id=instance_id,
    ))

    return {"ok": True, "run_id": run_id, "status": "running"}
