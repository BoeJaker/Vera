"""Planning styles as capabilities. Purely additive.

This module REGISTERS a new way to plan. It does not modify, wrap or re-enter
the loop's own planner, and removing this file returns the estate to exactly
what it was — which is the property that makes a style a style rather than a
fork of the planner.

Its routing profile is its own (`planning_style`), for the same reason: the
loop's profile belongs to the loop. The one role here is CPU-pinned on purpose,
mirroring the research profile's analyst — the GPU gate is capacity 1, so a
fan-out aimed at the GPU does not run in parallel, it QUEUES. On the CPU nodes,
which hold their own per-instance slots, the lenses genuinely run side by side.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (   # noqa: F401
    APP, capability, emit_event, now_iso, ollama_generate, register_routing_profile,
)

try:                                     # pragma: no cover - import shape only
    from Vera.vera.planning import planner_styles as PS
except ImportError:                      # pragma: no cover
    from vera.planning import planner_styles as PS   # type: ignore

log = logging.getLogger("vera.planning")

PROFILE = "planning_style"

try:
    register_routing_profile(
        PROFILE, label="Planning Styles", owner="planning",
        roles={
            # deny_gpu is the whole point - see the module docstring.
            "lens": {"job_type": "planning_lens", "deny_gpu": True,
                     "options": {"temperature": 0.3, "num_ctx": 8192}},
        })
except Exception as e:                       # pragma: no cover - never block load
    log.debug("register planning_style profile: %s", e)


async def _lens_generate(prompt: str, system: str = "") -> str:
    """One lens call, through the GATED generate path.

    Research reaches its nodes directly and holds no lease, which is how it puts
    several generations on one GPU node at once. This does not: every lens goes
    through ollama_generate, so the gate and the per-instance semaphore both
    still apply and a lens can never jump the queue in front of real work.
    """
    # think=False: a lens answers with a few short lines. On a CPU node a
    # reasoning model's thinking pass is minutes per call, and with five lenses
    # sharing two nodes it was the whole budget - the first live run produced
    # five timeouts and an empty brief.
    return await ollama_generate(prompt, system=system, json_mode=False,
                                 prefer_gpu=False, profile=PROFILE, role="lens",
                                 think=False)


@capability("plan.styles", memory="off", silent=True,
            http_method="GET", http_path="/plan/styles", http_tags=["planning"],
            description="List the available planning styles. 'single' is the "
                        "loop's own one-shot planner; the others are additive "
                        "alternatives that produce a plan without changing it. "
                        "Output: {styles:[{id,label,description,owner,callable}], "
                        "count}.",
            contract=_orch._inspection_contract("plan.styles", effects=["read"]))
async def plan_styles(trace_id=None):
    return {
        "styles": [{"id": s["id"], "label": s["label"],
                    "description": s["description"], "owner": s["owner"],
                    # Named, not implied: 'single' lives in the loop and is not
                    # invocable from here, and saying so beats an empty field.
                    "callable": s.get("plan") is not None}
                   for s in (PS.STYLES[k] for k in PS.style_ids())],
        "count": len(PS.STYLES),
    }


@capability("plan.detailed", memory="off",
            http_method="POST", http_path="/plan/detailed", http_tags=["planning"],
            description="Plan a goal with the DETAILED style: five short lenses "
                        "(decompose, artifacts, risks, criteria, caps) asked "
                        "concurrently on CPU nodes and merged host-locally with "
                        "no second model call. Returns a plan in the loop's own "
                        "plan shape plus the brief it was built from, so it can "
                        "be read, stored, or compared against the single-pass "
                        "plan for the same goal. Does NOT run the goal. "
                        "Inputs: goal (str!), max_steps (int=8), catalog (list "
                        "of cap names the plan may use), timeout_s (int=900, per "
                        "lens; CPU nodes are slow). Output: {ok, style, "
                        "plan:{steps,reason,done_when,brief}, brief_text}. "
                        "brief.missing names lenses that did not answer and "
                        "brief.errors says why.")
async def plan_detailed(goal: str = "", max_steps: int = 8,
                        catalog: Optional[List[str]] = None,
                        timeout_s: int = 900, trace_id=None):
    goal = str(goal or "").strip()
    if not goal:
        return {"ok": False, "error": "goal is required"}
    cat = list(catalog or [])
    if not cat:
        # Default to what this instance can actually do, so the caps lens is
        # filtered against reality rather than inventing plausible names.
        try:
            cat = sorted(_orch.CAPABILITY_REGISTRY)
        except Exception:                              # pragma: no cover
            cat = []
    plan = await PS.plan_detailed(goal, _lens_generate, catalog=cat,
                                  max_steps=int(max_steps or 8),
                                  timeout_s=float(timeout_s or 900))
    brief = plan.get("brief") or {}
    if brief.get("missing"):
        log.warning("plan.detailed: %d/%d lenses missing for %r: %s",
                    len(brief["missing"]), len(PS.LENSES), goal[:80],
                    brief.get("errors") or {})
    # emit_event is a coroutine: un-awaited it emits nothing and warns.
    await emit_event({"type": "plan.detailed", "goal": goal[:200],
                      "steps": len(plan.get("steps") or []),
                      "answered": brief.get("answered") or [],
                      "missing": brief.get("missing") or [],
                      "rejected_criteria": len(brief.get("rejected_criteria") or [])})
    return {"ok": True, "style": "detailed", "plan": plan,
            "brief_text": PS.render_brief(brief)}
