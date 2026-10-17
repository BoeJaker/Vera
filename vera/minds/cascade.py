"""decide.cascade.*: which student answers each loop question first (four-minds FM-2.5; see cascade_core).

decide.cascade.plan reads the shadow report (every implementation, every question, against outcomes), chooses per
question, stores the plan in Redis for the loop's students (student.load_plan) and returns it with the combined
census arm's switch modes. It refreshes itself every 6 h; the arm itself is a census schedule entry.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY, capability, schedule

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import cascade_core as CC
    from Vera.vera.minds import student as ST
    from Vera.vera.minds import contract_core as _MC
except Exception:                        # worktree / app-free import
    from vera.minds import cascade_core as CC          # type: ignore
    from vera.minds import student as ST               # type: ignore
    from vera.minds import contract_core as _MC        # type: ignore

log = logging.getLogger("vera.minds.cascade")
REFRESH_S = 6 * 3600
_LAST: Dict[str, Any] = {"at": 0.0}


async def build_plan() -> Dict[str, Any]:
    fn = (CAPABILITY_REGISTRY.get("decide.shadow.report") or {}).get("func")
    if not fn:
        return {"ok": False, "error": "decide.shadow.report not loaded"}
    report = await fn(question_id="all")
    if not isinstance(report, dict) or not report.get("questions"):
        return {"ok": False, "error": "the shadow report is empty", "report_keys": list((report or {}).keys())}
    plan = CC.choose(report, ST.model())
    r = getattr(_orch, "REDIS", None)
    if r is not None:
        await r.set(ST.PLAN_KEY, json.dumps(plan), ex=7 * 86400)
    ST.set_plan(plan)
    _LAST.update(at=time.time(), eligible=sorted(q for q, c in plan.items() if c.get("eligible")))
    return {"ok": True, "plan": plan, "arm_minds": CC.arm_minds(plan, ST.FEATURE),
            "eligible": _LAST["eligible"]}


@capability(
    "decide.cascade.plan", http_method="POST", http_path="/decide/cascade/plan", http_tags=["minds"],
    contract=_MC.contract("decide.cascade.plan", effects=["read", "write"], idempotency="idempotent"),
    memory="off", silent=True,
    description="Which student answers each loop decision question first, and from what confidence (four-minds "
                "FM-2.5, every question at once): from the shadow report, per question the runnable implementation "
                "(kNN, the in-slot student LLM) whose precision on outcomes reaches the live model's accuracy at the "
                "lowest threshold, with >= 30 outcomes and >= 20% coverage; the widest coverage wins. Stored for the "
                "loop's students; refreshed every 6 h. Output: {ok, plan: {question: {eligible, impl, tau, precision, "
                "coverage, live_accuracy, reason}}, arm_minds (the combined census arm's switches), eligible}.",
)
async def cap_decide_cascade_plan(trace_id=None) -> Dict[str, Any]:
    return await build_plan()


async def _refresh_job() -> None:
    if getattr(_orch, "_IS_WORKER", False) or time.time() - _LAST["at"] < REFRESH_S:
        return
    try:
        out = await build_plan()
        log.info("cascade plan: eligible %s", out.get("eligible"))
    except Exception as e:
        log.warning("cascade plan refresh: %s", e)
        _LAST["at"] = time.time()


schedule(_refresh_job, interval=900, name="decide_cascade_plan", singleton=True, skip_in_sandbox=True)
