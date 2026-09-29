"""Soft node preference, and taking a free GPU without queueing behind a loop.

Two routing controls that the rule set could not express, both wanted for the
same reason: `cpu-247` was doing every CPU generation job while `cpu-246` sat
almost idle, and the GPU sat idle between loop calls.

WHY cpu-247 HAD IT ALL. Nothing was pinned. `avoid_embed: true` excludes the
node currently hosting the embed model, and that is `cpu-246`, so the candidate
list collapsed to one before load was even considered:

    deny_gpu -> ['cpu-246', 'cpu-247']
    avoid_embed: excluded 'cpu-246' -> ['cpu-247']
    least busy: picked 'cpu-247' (in_use=0) from ['cpu-247']

That flag is a HARD exclusion and was the right tool when the intent was
"never put generation on the embed box". The intent now is softer - each node
FAVOURS a class of work but either can take either - and a hard exclusion
cannot say that. Hence `prefer`.

WHY A BONUS AND NOT A PIN. The picker scores

    in_use + colocated * 0.5 + proxy_queue * 0.2 + priority * 0.01

and takes the lowest. A preference has to be strong enough to break a tie when
both nodes are idle, and weak enough that a BUSY preferred node loses to an
idle one - otherwise it is a pin with extra steps. PREFER_BONUS = 0.5 does
exactly that, because `in_use` moves in whole numbers:

    both idle          preferred 0 - 0.5 = -0.5  beats  0.0        -> preferred
    preferred busy(1)  preferred 1 - 0.5 =  0.5  loses to 0.0      -> the other
    both busy(1)       preferred 1 - 0.5 =  0.5  beats  1.0        -> preferred

So the preference decides which node gets the work while there is a choice, and
gets out of the way the moment the preferred node is the busier one.

NOT A GPU CONTROL. An earlier version of this module also carried a
"use the GPU only while it is idle" flag, for summarize. It was dropped: the
gate lease is per-GENERATION, not per-step, so an executor's lease is already
released before its condense begins - which means a summarise cannot queue
behind the call awaiting it, and the honest answer for an INLINE job is simply
GPU-only (`prefer_gpu` + `allow: ["gpu-*"]`), not a conditional. Shipping an
unconfigured mechanism would have been worse than not having one.

Pure: instance dicts in, decision out. No app imports, no clock, no I/O.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

#: How much a preferred node's score is reduced. Half of one in-flight call:
#: enough to win a tie, not enough to win when the preferred node is busier.
#: See the module docstring for the arithmetic this number has to satisfy.
PREFER_BONUS = 0.5


def preference_bonus(instance_id: str, prefer: Any,
                     bonus: float = PREFER_BONUS) -> float:
    """The score adjustment for `instance_id` under a `prefer` rule.

    Returns a NEGATIVE number for the preferred node (lower score wins) and
    0.0 for everything else, including when no preference is set.
    """
    want = str(prefer or "").strip()
    if not want or str(instance_id) != want:
        return 0.0
    try:
        return -abs(float(bonus))
    except (TypeError, ValueError):        # pragma: no cover - defensive
        return -PREFER_BONUS



def preferred_of(rule: Optional[Dict[str, Any]]) -> str:
    """The soft-preferred instance id for a rule, or ""."""
    return str((rule or {}).get("prefer") or "").strip()


# ── Warm residency and saturation (2026-09-28) ───────────────────────────────
# Two facts the score could not see. A node that already has the model LOADED
# answers without a cold load (the 9b: 10.5 s cold vs ~1 s warm on gpu-250-cpu;
# the 35b MoE 62 s cold on cpu-247). And a CPU node now serves TWO requests at
# once (OLLAMA_NUM_PARALLEL=2), so `in_use == 1` there still has a free slot,
# while a node with every slot taken queues whatever is sent next.
#
# WARM_BONUS is deliberately SMALLER than PREFER_BONUS: an explicit preference
# still wins while its node is idle, and warmth decides among the rest -
#
#   preferred idle, cold     0 - 0.5        = -0.5  beats  other idle, warm -0.3
#   preferred busy(1), cold  1 - 0.5        =  0.5  loses to other idle, warm -0.3
#
# FULL_PENALTY is a whole call's worth, added on top of in_use, so a saturated
# node loses to any node with a free slot however the fractions fall.
WARM_BONUS = 0.3
FULL_PENALTY = 1.0


def _base(name: Any) -> str:
    n = str(name or "").strip()
    return n[:-len(":latest")] if n.endswith(":latest") else n


def is_resident(model: Any, running: Any) -> bool:
    """Whether `model` is among a node's loaded models (/api/ps names)."""
    want = _base(model)
    return bool(want) and any(_base(r) == want for r in (running or ()))


def warm_bonus(model: Any, running: Any, bonus: float = WARM_BONUS) -> float:
    return -abs(float(bonus)) if is_resident(model, running) else 0.0


def saturation_penalty(in_use: Any, slots: Any, penalty: float = FULL_PENALTY) -> float:
    try:
        n, s = int(in_use or 0), int(slots or 0)
    except (TypeError, ValueError):
        return 0.0
    return float(penalty) if s > 0 and n >= s else 0.0


def spill_candidates(cands: Dict[str, Dict[str, Any]], model: Any,
                     slots_of, tps_of, min_tps: float, scenario_active: bool,
                     ctx_need: int = 0, max_ctx: int = 0) -> Dict[str, Dict[str, Any]]:
    """CPU nodes a GPU-preferring call may spill onto while the GPU is full:
    the model already LOADED there, a slot free, the prompt small enough
    (a CPU prompt-eval of a big context is slower than waiting), and the node
    PROVEN fast enough for this model - or a scenario the user switched on
    covers the job type."""
    out: Dict[str, Dict[str, Any]] = {}
    if max_ctx and ctx_need and ctx_need > max_ctx:
        return out
    for iid, inst in (cands or {}).items():
        if inst.get("has_gpu") or not is_resident(model, inst.get("running")):
            continue
        if saturation_penalty(inst.get("in_use"), slots_of(iid, inst)):
            continue
        tps = float(tps_of(iid) or 0)
        if scenario_active or (tps and tps >= float(min_tps or 0)):
            out[iid] = inst
    return out
