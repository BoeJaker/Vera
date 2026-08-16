# ============================================================================
# orchestrator_capabilities.py — the closed-loop orchestrator (M7 Phase B)
# ============================================================================
#
# Part of Loop Lab (the evolve/ subsystem) — a DEDICATED orchestrator that drives
# the autonomous closed loop: pick a ready board item and dispatch it into a
# container, or idle when there's nothing to do. It runs ONLY when autonomous mode
# is ENGAGED (Phase A) — which also locks main, so the loop can never reach prod —
# and it NEVER runs v7 (or any v1-v8) agentic loop (operator directive). Dry-run by
# default: a step returns its decision and does nothing unless explicitly told to
# act. The decision logic is the pure orchestrator_core (unit-tested); this layer
# only wires it to board.status / board.items / board.dispatch.
# ============================================================================

from __future__ import annotations

import logging
from typing import Any, Dict, List

from Vera.vera.capability_orchestration import (
    capability, CAPABILITY_REGISTRY, emit_event)
from Vera.vera.evolve.orchestrator_core import next_action

log = logging.getLogger("vera.orchestrator")


async def _call(name: str, **kw) -> Any:
    """Invoke another capability by name (decoupled — the orchestrator drives the
    board + dispatch caps, it does not import their internals)."""
    fn = CAPABILITY_REGISTRY.get(name)
    if not fn:
        return {"error": f"capability not found: {name}"}
    try:
        return await fn(**kw)
    except Exception as e:                              # never let one cap error crash a step
        log.debug("orchestrator _call %s failed: %s", name, e)
        return {"error": str(e)}


@capability(
    "autonomous.orchestrate", memory="on",
    http_method="POST", http_path="/autonomous/orchestrate",
    http_tags=["autonomous", "evolve"],
    description="Closed-loop orchestrator STEP (M7 Phase B, Loop Lab): decide + optionally "
                "take the SINGLE next action — dispatch one ready board item into a container. "
                "DRY-RUN by default (returns the decision, does nothing). Acts only when "
                "dry_run=false AND autonomous mode is ENGAGED (Phase A) — so it can never run "
                "unattended without the main-lock on, and never touches main. When the board is "
                "dry it IDLES (it NEVER runs v7 or any v1-v8 loop). Input: dry_run (bool=true), "
                "agent (str=claude_code), executor (str=deterministic). Output: "
                "{ok, engaged, dry_run, decision, acted, result}.")
async def autonomous_orchestrate(dry_run: bool = True, agent: str = "claude_code",
                                 executor: str = "deterministic", trace_id=None) -> Dict[str, Any]:
    st = await _call("autonomous.status")
    engaged = bool(isinstance(st, dict) and st.get("engaged"))
    items_res = await _call("board.items")
    items: List[Dict] = (items_res.get("items") if isinstance(items_res, dict) else []) or []
    decision = next_action(items, engaged, agent)
    acted = False
    result = None
    # Only ever ACT on a dispatch, only when live (not dry-run) and the lock is on.
    if (not dry_run) and engaged and decision.get("action") == "dispatch":
        result = await _call("board.dispatch", id=decision.get("item"),
                             executor=executor, agent=agent)
        acted = not (isinstance(result, dict) and result.get("error"))
        await emit_event({"type": "autonomous.orchestrate.dispatch",
                          "item": decision.get("item"), "ok": acted})
    return {"ok": True, "engaged": engaged, "dry_run": dry_run,
            "decision": decision, "acted": acted, "result": result}
