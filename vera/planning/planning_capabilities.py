"""Planning styles as capabilities. Purely additive.

This module REGISTERS a new way to plan. It does not modify, wrap or re-enter
the loop's own planner, and removing this file returns the estate to exactly
what it was — which is the property that makes a style a style rather than a
fork of the planner.

Its routing profile is its own (`planning_style`), for the same reason: the
loop's profile belongs to the loop. The one role, `lens`, prefers the GPU. The
design first pinned it to the CPU nodes so five lenses could run side by side
instead of queuing behind the capacity-1 GPU gate; measured live (2026-09-09)
the CPU nodes are a hard limit - the default 7.4 GB model produced 16 tokens in
329 s under the fan-out, and four of five lenses timed out at 900 s. On the GPU
the five lenses queue back-to-back through the gate; with think=False each is a
few short lines, so the queue is short. Override the role on the Model Routing
page if the estate changes.
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
            # GPU, not CPU - see the module docstring. No num_ctx: a lens must
            # never make the GPU reload its model with a different context
            # between two of the loop planner's own calls; the prompts are tiny.
            "lens": {"job_type": "planning_lens", "prefer_gpu": True,
                     "options": {"temperature": 0.3}},
            # The BROAD style, placed by the compute-roles rule
            # (.git/vera-work/shared-planning/compute-roles/PLAN.md):
            # `stream` - each work-stream's step plan: the plan the run waits on,
            #   so the GPU (seconds; a CPU node took 199-251 s, 2026-09-27). No
            #   num_ctx, for the lens's reason above.
            # `enrich` - a deeper per-stream brief planned IN PARALLEL on a CPU
            #   node, like the research analyst beside the writer; merged into
            #   the stream's steps when it lands. Held off the GPU; its job type
            #   plan_enrich prefers the long-horizon CPU node (cpu-247) and keeps
            #   off the embedding/worker node (cpu-246), and broad issues these
            #   one at a time. Model qwen3.6:35b-a3b (user's pick, 2026-09-27):
            #   measured on idle cpu-247 it decodes 9.8 tok/s and reads 37.5
            #   tok/s - faster than qwen2.5:7b (7.2 / 24.7) as a MoE with ~3B
            #   active - and its briefs were the more accurate; but it loads in
            #   62 s cold (~22 GB), so the route keeps its model WARM (keep_alive
            #   2h - whichever model is selected here, or per run in the loop's
            #   Brief model setting). Probe: shared-planning/compute-roles/
            #   cpu247-model-probe-2026-09-27.md.
            "stream": {"job_type": "planning_lens", "prefer_gpu": True,
                       "options": {"temperature": 0.3}},
            "enrich": {"job_type": "plan_enrich", "deny_gpu": True,
                       "model": "qwen3.6:35b-a3b",
                       "options": {"temperature": 0.3, "num_ctx": 8192,
                                   "keep_alive": "2h"}},
        })
except Exception as e:                       # pragma: no cover - never block load
    log.debug("register planning_style profile: %s", e)


async def _lens_generate(prompt: str, system: str = "") -> str:
    """One lens call, through the GATED generate path.

    Research reaches its nodes directly and holds no lease, which is how it puts
    several generations on one GPU node at once. This does not: every lens goes
    through ollama_generate, so the gate and the per-instance semaphore both
    still apply and a lens can never jump the queue in front of real work - the
    five lenses take the GPU slot one after another.
    """
    # think=False: a lens answers with a few short lines; a reasoning model's
    # thinking pass would multiply every lens's cost for nothing (on the CPU
    # nodes it was minutes per call and the whole budget).
    return await ollama_generate(prompt, system=system, json_mode=False,
                                 prefer_gpu=True, profile=PROFILE, role="lens",
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
                        "(decompose, artifacts, risks, criteria, caps), each a "
                        "short no-think GPU call through the gate, merged "
                        "host-locally with no second model call. A criterion or "
                        "artifact asserting a number the goal never gave is "
                        "rejected. Returns a plan in the loop's own plan shape "
                        "plus the brief it was built from, so it can be read, "
                        "stored, or compared against the single-pass plan for "
                        "the same goal. Does NOT run the goal. Inputs: goal "
                        "(str!), max_steps (int=8), catalog (list of cap names "
                        "the plan may use), timeout_s (int=900, per lens, "
                        "including time queued for the GPU). Output: {ok, style, "
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
