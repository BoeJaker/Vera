"""A health verdict for the JEPA worldview: ok | degraded | stale | untrained, with reasons.

worldview.stats reports raw counters; consumers (research's worldview source, the
recall chip) trusted any trained model. Documentation 49 §4.1 asks for a verdict
every consumer checks. This module computes it from the stats the model already
reports. It is deliberately strict: without a held-out next-concept evaluation the
model cannot be "ok" - the best it can be is "degraded" - because nothing yet shows
its predictions beat a baseline.

Pure: no torch, no Redis, no registry.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

MIN_RECORDS_PER_CONCEPT = 20      # doc 49: fail below roughly 20
MIN_PERPLEXITY_SHARE = 0.10       # codebook perplexity / K: below this, a few concepts do all the work
MAX_STEPS_PER_TRANSITION = 20.0   # dynamics steps per distinct observed transition: above this it is memorising
MIN_INDEX_SHARE = 0.5             # FAISS vectors / records assigned
STALE_DAYS = 14.0                 # no persist (training checkpoint) in this long

OK, DEGRADED, STALE, UNTRAINED = "ok", "degraded", "stale", "untrained"


def _age_days(iso: Any, now: datetime) -> Optional[float]:
    if not iso or not isinstance(iso, str):
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0.0, (now - t).total_seconds() / 86400.0)


def verdict(model: Dict[str, Any], index: Optional[Dict[str, Any]] = None,
            held_out: Optional[Dict[str, Any]] = None,
            now: Optional[datetime] = None) -> Dict[str, Any]:
    """Return {status, reasons, metrics} for a worldview.stats()["model"] dict.

    ``index`` is worldview.stats()["index"]; ``held_out`` is a future evaluation
    {"beats_baseline": bool, ...} - absent today, which keeps the verdict below ok.
    """
    now = now or datetime.now(timezone.utc)
    model = model or {}
    reasons: List[str] = []
    k = int(model.get("num_concepts") or 0)
    records = int(model.get("records_assigned") or 0)
    transitions = int(model.get("transitions_observed") or 0)
    steps = model.get("train_steps") or {}
    dyn_steps = int(steps.get("dynamics") or 0) if isinstance(steps, dict) else 0
    perplexity = float(((model.get("codebook") or {}).get("perplexity")) or 0.0)
    vectors = int((index or {}).get("vectors") or 0)
    age = _age_days(model.get("last_fabric_persist"), now)

    metrics = {
        "records_assigned": records,
        "num_concepts": k,
        "records_per_concept": round(records / k, 2) if k else 0.0,
        "perplexity_share": round(perplexity / k, 4) if k else 0.0,
        "dynamics_steps_per_transition": round(dyn_steps / transitions, 2) if transitions else None,
        "index_share": round(vectors / records, 3) if records else 0.0,
        "checkpoint_age_days": round(age, 2) if age is not None else None,
    }

    trained = bool(model.get("ready")) and records > 0 and isinstance(steps, dict) and any(int(v or 0) > 0 for v in steps.values())
    if not trained:
        return {"status": UNTRAINED, "reasons": ["model not trained or no records assigned"], "metrics": metrics}

    if age is None:
        reasons.append("no checkpoint time recorded")
    elif age > STALE_DAYS:
        return {"status": STALE, "reasons": [f"last checkpoint {age:.1f} days ago (> {STALE_DAYS:g})"], "metrics": metrics}

    if k and records / k < MIN_RECORDS_PER_CONCEPT:
        reasons.append(f"{records / k:.1f} records per concept (< {MIN_RECORDS_PER_CONCEPT})")
    if k and perplexity / k < MIN_PERPLEXITY_SHARE:
        reasons.append(f"codebook perplexity {perplexity:.1f} is {perplexity / k:.1%} of K={k} (< {MIN_PERPLEXITY_SHARE:.0%})")
    if transitions and dyn_steps / transitions > MAX_STEPS_PER_TRANSITION:
        reasons.append(f"{dyn_steps / transitions:.0f} dynamics steps per observed transition (> {MAX_STEPS_PER_TRANSITION:g}): memorising")
    if index is not None and records and vectors / records < MIN_INDEX_SHARE:
        reasons.append(f"index holds {vectors} vectors for {records} assigned records")
    if not held_out:
        reasons.append("no held-out next-concept evaluation against a baseline")
    elif not held_out.get("beats_baseline"):
        reasons.append("held-out next-concept prediction does not beat the baseline")

    return {"status": DEGRADED if reasons else OK, "reasons": reasons, "metrics": metrics}


def usable(v: Dict[str, Any]) -> bool:
    """Whether a consumer may use the worldview's output as evidence."""
    return (v or {}).get("status") == OK
