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
