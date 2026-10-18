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
from typing import Any, Dict, List, Optional, Sequence

MIN_RECORDS_PER_CONCEPT = 20      # doc 49: fail below roughly 20
MIN_PERPLEXITY_SHARE = 0.10       # codebook perplexity / K: below this, a few concepts do all the work
MAX_STEPS_PER_TRANSITION = 20.0   # dynamics steps per distinct observed transition: above this it is memorising
MIN_INDEX_SHARE = 0.5             # FAISS vectors / records assigned
STALE_DAYS = 14.0                 # no persist (training checkpoint) in this long
MAX_LARGEST_SHARE = 0.5           # one concept holding more than this share of the records swallows the space

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


def spread(populations: Sequence[int], k: int = 0) -> Dict[str, Any]:
    """How records spread over the concepts (FM-W.1, 18 Oct 2026): median population, share of concepts below
    MIN_RECORDS_PER_CONCEPT, the largest concept's share of the records. Unused concepts count as 0 up to k."""
    pops = sorted(int(p or 0) for p in populations or [])
    if k and len(pops) < k:
        pops = [0] * (k - len(pops)) + pops
    if not pops:
        return {}
    n, total = len(pops), sum(pops)
    mid = n // 2
    median = pops[mid] if n % 2 else (pops[mid - 1] + pops[mid]) / 2
    return {"median_population": median,
            "concepts_below_min": sum(1 for p in pops if p < MIN_RECORDS_PER_CONCEPT),
            "share_below_min": round(sum(1 for p in pops if p < MIN_RECORDS_PER_CONCEPT) / n, 3),
            "largest_population": pops[-1],
            "largest_share": round(pops[-1] / total, 3) if total else 0.0}


def verdict(model: Dict[str, Any], index: Optional[Dict[str, Any]] = None,
            held_out: Optional[Dict[str, Any]] = None,
            now: Optional[datetime] = None,
            populations: Optional[Sequence[int]] = None) -> Dict[str, Any]:
    """Return {status, reasons, metrics} for a worldview.stats()["model"] dict.

    ``index`` is worldview.stats()["index"]; ``held_out`` is a future evaluation
    {"beats_baseline": bool, ...} - absent today, which keeps the verdict below ok. ``populations`` (records per
    concept) lets the verdict judge the distribution, not only the mean (FM-W.1).
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
    dist = spread(populations, k) if populations is not None else {}
    metrics.update(dist)

    trained = bool(model.get("ready")) and records > 0 and isinstance(steps, dict) and any(int(v or 0) > 0 for v in steps.values())
    if not trained:
        return {"status": UNTRAINED, "reasons": ["model not trained or no records assigned"], "metrics": metrics}

    if age is None:
        reasons.append("no checkpoint time recorded")
    elif age > STALE_DAYS:
        return {"status": STALE, "reasons": [f"last checkpoint {age:.1f} days ago (> {STALE_DAYS:g})"], "metrics": metrics}

    if k and records / k < MIN_RECORDS_PER_CONCEPT:
        reasons.append(f"{records / k:.1f} records per concept (< {MIN_RECORDS_PER_CONCEPT})")
    if dist and dist["median_population"] < MIN_RECORDS_PER_CONCEPT:
        # the mean hid this on 18 Oct: 23.2 per concept, median 1, 78 of 94 concepts below 20
        reasons.append(f"median concept holds {dist['median_population']:g} records (< {MIN_RECORDS_PER_CONCEPT}); "
                       f"{dist['concepts_below_min']} of {k or len(populations)} concepts below {MIN_RECORDS_PER_CONCEPT}")
    if dist and dist["largest_share"] > MAX_LARGEST_SHARE:
        reasons.append(f"one concept holds {dist['largest_share']:.0%} of the records (> {MAX_LARGEST_SHARE:.0%})")
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
