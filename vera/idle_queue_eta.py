"""How long the background queue will take, from what has actually been measured.

The queue can already say WHAT is waiting and WHY. It cannot say how long any
of it will take, so "2 jobs waiting" is equally consistent with ninety seconds
of work and with six hours - and a scheduler cannot choose between "run it in
the gap before the next census" and "this needs the night" without that number.

THE RULE THIS MODULE EXISTS TO ENFORCE: an estimate is only ever produced from
a rate that was OBSERVED. There is no default seconds-per-record, no assumed
tokens-per-second, and no fallback constant. When nothing has been measured for
a kind, `estimate` returns None and the panel says "no estimate yet" - which is
information. A made-up number is not: it would be indistinguishable from a
measured one in the UI and would then be used to schedule real work.

Two cost shapes, because the queue's jobs genuinely have two:

  per-item   embedding. Cost is items x seconds-per-item. Measured on prod
             2026-09-08: ~4.0s per record on an idle box, ~11.1s while a census
             ran (2.8x) - which is also why the queue waits for a quiet box
             rather than merely a free one.
  per-token  a dream cycle or a narration. Cost is tokens / tokens-per-second,
             and _ROUTE_STATS already carries a rolling ema_tps per
             (model, node) fed by every real ollama_generate call.

Pure: rates and counts in, seconds out. No Redis, no clock, no registry - the
caller owns all three, which is what makes a timeline testable without a live
node.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

#: Cost shape per job kind. A kind absent from here has no shape and therefore
#: no estimate - deliberately, so a new producer cannot silently inherit an
#: estimator that does not describe it.
PER_ITEM = "per_item"
PER_TOKEN = "per_token"

KIND_SHAPE: Dict[str, str] = {
    "embed.sessions": PER_ITEM,
    "embed.sources": PER_ITEM,
    "dream": PER_TOKEN,
    "narrator": PER_TOKEN,
}

#: An EMA is only worth trusting once it has seen a few samples. Below this the
#: rate is reported but flagged `provisional`, so a single unlucky first run
#: cannot masquerade as a measurement.
MIN_SAMPLES = 3


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def shape_of(kind: Any) -> str:
    return KIND_SHAPE.get(str(kind or ""), "")


def blend(previous: Any, sample: Any, weight: float = 0.3) -> Optional[float]:
    """Fold one observation into a rolling average.

    The same EMA weight the router uses for tokens/sec, so a rate learned here
    and a rate read from _ROUTE_STATS respond to change at the same speed and
    can be compared without a footnote.
    """
    s = _num(sample)
    if s is None or s <= 0:
        return _num(previous)
    p = _num(previous)
    if p is None or p <= 0:
        return s
    w = _num(weight)
    if w is None or not (0 < w <= 1):
        w = 0.3
    return (1 - w) * p + w * s


def observe(rates: Optional[Dict[str, Any]], kind: str, seconds: Any,
            items: Any = 1) -> Dict[str, Any]:
    """Record what one completed job actually cost, per item.

    Returns a NEW rates dict - the caller owns storage, and mutating theirs
    would make the update invisible to whatever else holds a reference.
    """
    out = {k: dict(v) for k, v in (rates or {}).items() if isinstance(v, dict)}
    sec, n = _num(seconds), _num(items)
    if sec is None or sec <= 0 or n is None or n <= 0:
        return out                          # nothing usable to learn from
    row = out.setdefault(str(kind), {"per_item_s": None, "samples": 0})
    row["per_item_s"] = blend(row.get("per_item_s"), sec / n)
    row["samples"] = int(row.get("samples", 0)) + 1
    return out


def estimate(job: Optional[Dict[str, Any]],
             rates: Optional[Dict[str, Any]] = None,
             tps: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Seconds this job will take, and WHERE that number came from.

    None when nothing has been measured for it. The caller must render that as
    "no estimate yet" rather than as zero: a job of unknown length is not a
    job of no length, and treating it as one is how a scheduler decides an
    eight-hour backfill fits in a ten-minute gap.
    """
    j = job if isinstance(job, dict) else {}
    kind = str(j.get("kind") or "")
    shape = shape_of(kind)
    if not shape:
        return None
    items = _num(j.get("items_remaining"))
    if shape == PER_ITEM:
        row = (rates or {}).get(kind) or {}
        rate = _num(row.get("per_item_s"))
        if rate is None or rate <= 0 or items is None or items <= 0:
            return None
        samples = int(row.get("samples", 0) or 0)
        return {"seconds": round(items * rate, 1),
                "basis": "%s items x %.2fs measured (%d sample%s)"
                         % (int(items), rate, samples, "" if samples == 1 else "s"),
                "provisional": samples < MIN_SAMPLES}
    # per-token
    toks = _num(j.get("expected_tokens"))
    rate = _num(tps)
    if toks is None or toks <= 0 or rate is None or rate <= 0:
        return None
    return {"seconds": round(toks / rate, 1),
            "basis": "%d tokens at %.1f tok/s observed" % (int(toks), rate),
            "provisional": False}


def timeline(jobs: Optional[Iterable[Dict[str, Any]]],
             rates: Optional[Dict[str, Any]] = None,
             tps_for: Any = None, start_at: Any = 0) -> List[Dict[str, Any]]:
    """When each queued job would start and finish, in order.

    The queue runs ONE job at a time on purpose, so the timeline is a running
    sum rather than a packing problem. A job with no estimate does not get a
    guessed length - it ends the timeline, because everything after it would be
    an offset from a number nobody measured. That is the honest shape: the bar
    stops where the knowledge stops.
    """
    t = _num(start_at) or 0.0
    out: List[Dict[str, Any]] = []
    for j in (jobs or []):
        if not isinstance(j, dict):
            continue
        tps = tps_for(j) if callable(tps_for) else tps_for
        est = estimate(j, rates, tps)
        row = {"id": j.get("id", ""), "kind": j.get("kind", ""),
               "title": j.get("title", ""), "starts_in_s": int(t)}
        if est is None:
            row.update({"seconds": None, "ends_in_s": None,
                        "basis": "no measurement yet for this kind",
                        "provisional": True})
            out.append(row)
            break
        t += float(est["seconds"])
        row.update({"seconds": est["seconds"], "ends_in_s": int(t),
                    "basis": est["basis"], "provisional": est["provisional"]})
        out.append(row)
    return out


def total_seconds(rows: Optional[Iterable[Dict[str, Any]]]) -> Optional[float]:
    """The whole queue's length, or None if any of it is unknown.

    None rather than a partial sum: "at least 40 minutes" and "40 minutes" lead
    to different scheduling decisions, and a partial sum presented as a total
    is the more dangerous of the two.
    """
    total, complete = 0.0, True
    for r in (rows or []):
        s = _num((r or {}).get("seconds"))
        if s is None:
            complete = False
            continue
        total += s
    return round(total, 1) if complete else None
