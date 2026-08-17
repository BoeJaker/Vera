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

import asyncio
import logging
import os
from typing import Any, Dict, List

from Vera.vera.capability_orchestration import (
    capability, CAPABILITY_REGISTRY, emit_event)
from Vera.vera.evolve.orchestrator_core import next_action

log = logging.getLogger("vera.orchestrator")

# --- Drive-loop configuration (M7 Phase B) -----------------------------------
# The drive loop is a scheduled tick that repeatedly runs one orchestrator step.
# It is SAFE-BY-DEFAULT in three layers:
#   1. DISABLED unless VERA_ORCHESTRATOR_ENABLED=1        (no tick at all)
#   2. OBSERVE (dry-run) unless VERA_ORCHESTRATOR_LIVE=1  (decides, never dispatches)
#   3. even when live, autonomous_orchestrate itself only acts while ENGAGED (Phase A),
#      which also hard-locks main — so the loop can never reach prod.
# So enabling the tick lets you WATCH its decisions on the Master page before ever
# letting it act; flipping LIVE=1 + engaging autonomous mode is the deliberate arming.
def _env_flag(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on")

def _orch_interval_s() -> int:
    try:
        return max(15, int(os.environ.get("VERA_ORCHESTRATOR_INTERVAL_S", "60")))
    except Exception:
        return 60

_DRIVE_TASK: "asyncio.Task | None" = None


async def _call(name: str, **kw) -> Any:
    """Invoke another capability by name (decoupled — the orchestrator drives the
    board + dispatch caps, it does not import their internals). CAPABILITY_REGISTRY
    stores a dict per cap; the callable is under "func"."""
    reg = CAPABILITY_REGISTRY.get(name) or {}
    fn = reg.get("func") if isinstance(reg, dict) else reg  # tolerate either shape
    if not callable(fn):
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


# --- The drive loop ----------------------------------------------------------
async def _drive_loop() -> None:
    """Scheduled drive loop: run one orchestrator step every interval while enabled.
    OBSERVE (dry-run) unless VERA_ORCHESTRATOR_LIVE=1; even live it only dispatches
    while autonomous mode is ENGAGED (which locks main). Each tick emits a
    `autonomous.orchestrate.tick` event so the Master page can show what the loop is
    deciding — visible before it is ever allowed to act."""
    log.info("orchestrator drive loop started (interval=%ss live=%s)",
             _orch_interval_s(), _env_flag("VERA_ORCHESTRATOR_LIVE"))
    while True:
        try:
            live = _env_flag("VERA_ORCHESTRATOR_LIVE")
            res = await autonomous_orchestrate(dry_run=not live)
            dec = (res.get("decision") or {}) if isinstance(res, dict) else {}
            await emit_event({"type": "autonomous.orchestrate.tick",
                              "engaged": res.get("engaged"),
                              "live": live,
                              "acted": res.get("acted"),
                              "action": dec.get("action"),
                              "item": dec.get("item"),
                              "reason": dec.get("reason")})
        except asyncio.CancelledError:
            log.info("orchestrator drive loop cancelled")
            raise
        except Exception as e:                          # a bad tick must never kill the loop
            log.warning("orchestrator drive tick failed: %s", e)
        try:
            await asyncio.sleep(_orch_interval_s())
        except asyncio.CancelledError:
            raise


def _drive_running() -> bool:
    return _DRIVE_TASK is not None and not _DRIVE_TASK.done()


@capability(
    "autonomous.drive", memory="on",
    http_method="POST", http_path="/autonomous/drive",
    http_tags=["autonomous", "evolve"],
    description="Control the closed-loop DRIVE LOOP (M7 Phase B, Loop Lab): the scheduled "
                "ticker that runs autonomous.orchestrate on an interval. action=start|stop|status "
                "(default status). Starting it is safe: it OBSERVES (dry-run, emits a tick event "
                "per step) unless VERA_ORCHESTRATOR_LIVE=1, and even live only dispatches while "
                "autonomous mode is ENGAGED (which locks main). Output: {ok, running, live, "
                "interval_s, enabled_env}.")
async def autonomous_drive(action: str = "status", trace_id=None) -> Dict[str, Any]:
    global _DRIVE_TASK
    action = (action or "status").strip().lower()
    if action == "start":
        if not _drive_running():
            _DRIVE_TASK = asyncio.ensure_future(_drive_loop())
        await emit_event({"type": "autonomous.drive.start",
                          "live": _env_flag("VERA_ORCHESTRATOR_LIVE")})
    elif action == "stop":
        if _drive_running():
            _DRIVE_TASK.cancel()
        _DRIVE_TASK = None
        await emit_event({"type": "autonomous.drive.stop"})
    return {"ok": True, "running": _drive_running(),
            "live": _env_flag("VERA_ORCHESTRATOR_LIVE"),
            "interval_s": _orch_interval_s(),
            "enabled_env": _env_flag("VERA_ORCHESTRATOR_ENABLED")}


def _maybe_autostart_drive() -> None:
    """Auto-start the drive loop at import ONLY if VERA_ORCHESTRATOR_ENABLED=1.
    Off by default — prod boots with the loop dormant. Never raises: a scheduling
    failure at import must not break module load (which would drop ~all evolve caps)."""
    if not _env_flag("VERA_ORCHESTRATOR_ENABLED"):
        return
    try:
        asyncio.get_running_loop()          # raises if no loop is running yet at import
    except RuntimeError:
        return                              # loaded before the loop; start via autonomous.drive
    try:
        global _DRIVE_TASK
        _DRIVE_TASK = asyncio.ensure_future(_drive_loop())
        log.info("orchestrator drive loop auto-started (VERA_ORCHESTRATOR_ENABLED=1)")
    except Exception as e:
        log.warning("orchestrator drive autostart skipped: %s", e)


_maybe_autostart_drive()
