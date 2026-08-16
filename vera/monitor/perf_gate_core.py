"""Pure perf-gate evaluation (M3 perf-gating) — unit-testable, no I/O.

`perf.scan` already measures runtime health — event-loop stalls (≈ socket flap /
hangs), Ollama node health + saturation (contention), host CPU/RAM, stale Redis
consumers, zombie jobs — and returns a severity summary {crit,warn,info,ok}. This
module turns that into a promote verdict: is the system healthy enough to land a
change into it right now? Pure so the thresholds/decision are tested without the
app or live metrics.
"""
from __future__ import annotations

from typing import Dict, List

# Default gate thresholds (the cap layer may override from env).
DEFAULT_THRESHOLDS = {
    "max_crit": 0,     # ANY critical perf finding → fail verdict
    "max_warn": 4,     # more warnings than this → warn verdict
}


def perf_verdict(summary: Dict, thresholds: Dict = None) -> Dict:
    """Reduce a perf.scan `summary` ({crit,warn,info,ok} counts) to
    {verdict: 'pass'|'warn'|'fail', crit, warn, reason}. Pure.

    - crit over `max_crit`  → 'fail'  (the system is actively unhealthy)
    - warn over `max_warn`  → 'warn'  (degraded, surfaced, not fatal)
    - otherwise             → 'pass'
    """
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    crit = max(0, int((summary or {}).get("crit", 0) or 0))
    warn = max(0, int((summary or {}).get("warn", 0) or 0))
    if crit > int(t["max_crit"]):
        return {"verdict": "fail", "crit": crit, "warn": warn,
                "reason": f"{crit} critical perf finding(s) (limit {t['max_crit']})"}
    if warn > int(t["max_warn"]):
        return {"verdict": "warn", "crit": crit, "warn": warn,
                "reason": f"{warn} perf warning(s) (over {t['max_warn']})"}
    return {"verdict": "pass", "crit": crit, "warn": warn, "reason": "perf nominal"}


def gate_blocks_promote(verdict: str, strict: bool) -> bool:
    """Whether a perf verdict should BLOCK a promote. A 'fail' blocks ONLY in strict
    mode; 'warn'/'pass' never block. Perf is a RUNTIME property (not a property of
    the branch), so the default is advisory — surface it, don't stop a merge on
    transient system load — and a hard gate is opt-in (VERA_PERF_GATE_STRICT)."""
    return bool(strict) and verdict == "fail"


def top_findings(findings: List[Dict], n: int = 5) -> List[Dict]:
    """The most severe findings (perf.scan sorts crit→ok already), compacted to
    {severity, area, title} for surfacing on a pipeline record / the UI. Drops the
    'ok' rows — a gate only cares about what's wrong."""
    out: List[Dict] = []
    for f in (findings or []):
        if len(out) >= max(0, int(n)):
            break
        if (f or {}).get("severity") in ("ok", None):
            continue
        out.append({"severity": f.get("severity"), "area": f.get("area"),
                    "title": f.get("title")})
    return out
