"""Was a census run measuring the loop, or measuring its environment?

Run 41 cost 3.8 hours and produced 7/12 against 11/12 and 10/12 either side.
Read as a score it is a large regression. It was not: the operator confirmed the
network was contended. The harness could not tell — it checks the Ollama GPU
gate and the running-loop count, and both read free for the entire run.

The timings locate it exactly. Across the six goals that finished cleanly in all
three runs and do no network work, run 41 was FASTER:

    run40 2986s     run41 2859s     run42 2962s      (-4.3%, -3.5%)

while every goal that collapsed — research-web, research-report,
build-browser-verified — is network-dependent, and each passes on both sides of
that run. A slow box slows everything; this slowed one class of goal.

So the signature is a CONTRAST, not a threshold: network-bound goals degrade
together while compute-bound goals hold their times. That is checkable after the
fact, from data every past run already has, which is why this is a reader rather
than a new probe — it works on runs 1-42 as they stand.

WHY THIS EXISTS AT ALL
The census's whole value is the timeline, and a run that measured the network
instead of the loop is worse than no run: it looks like evidence. Anything
drawing a trend across run 41 is reading contention as a code change.

Pure: run records in, a verdict out.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

#: A goal touches the network when the TEMPLATE says so — `intent: research`
#: reaches out for sources, `output: browser` drives a real browser session.
#: Derived from the template's own declared axes rather than a hardcoded goal
#: list, so it holds for a template nobody has written yet.
NETWORK_INTENTS = ("research",)
NETWORK_OUTPUTS = ("browser",)

#: How far compute-bound goals may drift and still count as "held steady",
#: measured as the MEDIAN per-goal ratio (see compute_drift for why median).
#: Run 41 sits at +5.2%, run 42 at +15.6%; a uniformly slower box lands far
#: outside this.
COMPUTE_DRIFT = 0.20

#: A single network goal degrading is ordinary variance — half the default set
#: sits within ~10% of the wall cap. It takes a MAJORITY moving together to be
#: a signature. Run 42 degraded one of three (and improved the other two); run
#: 41 degraded three of three.
NETWORK_MAJORITY = 0.5

#: How much harder the network class must be hit than the compute class. Run 41
#: was 100% of network goals against 14% of compute goals — a factor of 7. A
#: run where both classes fail at similar rates is a slow box or a real
#: regression, and must not be excused as contention.
DISPROPORTION = 2.0


def network_bound(goal_meta: Optional[Dict[str, Any]]) -> bool:
    """Does this goal depend on the network to succeed?"""
    g = goal_meta or {}
    return (str(g.get("intent", "")).lower() in NETWORK_INTENTS
            or str(g.get("output", "")).lower() in NETWORK_OUTPUTS)


def split_goals(template: Optional[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Goal ids split into network-bound and compute-bound."""
    net, comp = [], []
    for g in ((template or {}).get("goals") or []):
        gid = str(g.get("id") or "")
        if not gid:
            continue
        (net if network_bound(g) else comp).append(gid)
    return {"network": net, "compute": comp}


def _wall(row: Optional[Dict[str, Any]]) -> Optional[float]:
    try:
        w = (row or {}).get("wall_s")
        return float(w) if w is not None else None
    except (TypeError, ValueError):
        return None


def _done(row: Optional[Dict[str, Any]]) -> bool:
    return str((row or {}).get("status") or "") == "done"


def compute_drift(run: Dict[str, Any], baseline: Dict[str, Any],
                  compute_ids: Sequence[str]) -> Optional[float]:
    """Typical change in wall time across compute goals that finished cleanly
    in BOTH runs. None when there is nothing comparable.

    The MEDIAN of per-goal ratios, not the ratio of totals. Summing lets one
    goal speak for the group: run 41's `analyse-data` went 759s -> 1652s, and
    on totals that single outlier drags the compute set to +20.4% and buries
    the fact that the other six were FASTER. On medians the same run reads
    +5.2%, which is what "goals generally held their times" actually means.

    That outlier is real and unexplained (see the run 40-42 analysis) — this
    resists it rather than excluding it, because dropping an inconvenient point
    to make a verdict come out is how an instrument starts lying.

    Only goals done in both count: a wall-capped goal contributes the cap, not
    its work, so including one would report the cap as a slowdown.
    """
    ratios: List[float] = []
    for gid in compute_ids or []:
        ra, rb = run.get(gid), baseline.get(gid)
        wa, wb = _wall(ra), _wall(rb)
        if _done(ra) and _done(rb) and wa and wb and wb > 0:
            ratios.append(wa / wb)
    if not ratios:
        return None
    ratios.sort()
    mid = len(ratios) // 2
    median = (ratios[mid] if len(ratios) % 2
              else (ratios[mid - 1] + ratios[mid]) / 2.0)
    return median - 1.0


def degraded(run: Dict[str, Any], baseline: Dict[str, Any],
             ids: Sequence[str]) -> List[str]:
    """Goals that finished in the baseline and did NOT finish in this run."""
    return [gid for gid in (ids or [])
            if _done(baseline.get(gid)) and not _done(run.get(gid))]


def assess(run: Optional[Dict[str, Any]], baseline: Optional[Dict[str, Any]],
           template: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Did this run measure the loop, or its environment?

    Returns a verdict plus the numbers behind it, because the point is to be
    argued with: `environmental` means the run should be excluded from a trend,
    and that claim has to show its working.
    """
    run, baseline = run or {}, baseline or {}
    split = split_goals(template)
    net_ids, comp_ids = split["network"], split["compute"]

    drift = compute_drift(run, baseline, comp_ids)
    net_bad = degraded(run, baseline, net_ids)
    comp_bad = degraded(run, baseline, comp_ids)
    net_eligible = [g for g in net_ids if _done(baseline.get(g))]

    comp_eligible = [g for g in comp_ids if _done(baseline.get(g))]
    share = (len(net_bad) / len(net_eligible)) if net_eligible else 0.0
    comp_share = (len(comp_bad) / len(comp_eligible)) if comp_eligible else 0.0
    held = drift is not None and abs(drift) <= COMPUTE_DRIFT

    # DISPROPORTION, not purity. Requiring zero compute failures was the first
    # rule written here and the real data rejected it: run 41 — the confirmed
    # contended run — also lost `underspecified`, a compute goal. Demanding a
    # clean split would have made the detector unable to detect the one run it
    # exists for. What actually distinguishes the case is that one CLASS was
    # hit far harder: 3 of 3 network goals against 1 of 7 compute goals.
    environmental = bool(held and net_eligible
                         and share > NETWORK_MAJORITY
                         and share >= DISPROPORTION * max(comp_share, 1e-9))

    if environmental:
        note = ("network-bound goals degraded %d of %d against %d of %d "
                "compute goals, while compute times held to %+.1f%% — this run "
                "measured the network, not the loop; exclude it from any trend"
                % (len(net_bad), len(net_eligible), len(comp_bad),
                   len(comp_eligible), 100 * drift))
    elif drift is None:
        note = "not enough goals finished in both runs to compare"
    elif not held:
        note = ("compute-bound goals moved %+.1f%%, so any slowdown is not "
                "network-specific" % (100 * drift))
    elif net_bad and comp_share and share < DISPROPORTION * comp_share:
        note = ("both classes failed at similar rates (network %d/%d, compute "
                "%d/%d) — a slow box or a real regression, not contention"
                % (len(net_bad), len(net_eligible), len(comp_bad),
                   len(comp_eligible)))
    elif not net_bad:
        note = "nothing degraded against the baseline"
    else:
        note = ("only %d of %d network goals degraded — ordinary variance "
                "against the wall cap" % (len(net_bad), len(net_eligible)))

    return {
        "environmental": environmental,
        "note": note,
        "compute_drift": None if drift is None else round(drift, 4),
        "network_degraded": net_bad,
        "network_eligible": net_eligible,
        "compute_degraded": comp_bad,
    }
