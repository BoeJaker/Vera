"""
gpu_trace_core.py — pure parsing/summary for a traced GPU load (no app imports)
==============================================================================

`bench.node_gpu` takes ONE nvidia-smi snapshot, which structurally cannot see
throttling: a card boosts to its rated clock and only decays after seconds of
sustained load, so an isolated sample always looks healthy. Measured 2026-09-09
on gpu-250 (Tesla V100-PCIE-12GB): the SM clock fell 1380 -> 742 MHz over ~10s
of generation with SW_THERMAL_SLOWDOWN set, while the chassis fans sat at 14-29%
because iLO has no thermal telemetry for a third-party PCIe card. No snapshot
taken before the decay would have shown any of it.

`bench.node_trace` samples DURING a generation instead. The parsing and the
verdict are pure, so they live here and are unit-tested without booting the app
(benchmark_capabilities.py imports the whole orchestrator).

Nothing here imports anything beyond the stdlib.
"""
from __future__ import annotations

from typing import Any, Dict, List

# nvidia-smi query whose CSV this module parses. `-l 1 -c N` self-terminates
# after N samples, so a sampler can never outlive the call that started it.
GPU_TRACE_QUERY = (
    "nvidia-smi --query-gpu=clocks.sm,clocks.mem,temperature.gpu,power.draw,"
    "utilization.gpu,clocks_event_reasons.active "
    "--format=csv,noheader,nounits -l 1 -c {count}")

# NVML clocksEventReasons bits worth naming. An unrecognised mask is reported as
# hex rather than dropped, so a new reason stays visible.
THROTTLE_BITS = (
    (0x0000000000000004, "sw_power_cap"),
    (0x0000000000000008, "hw_slowdown"),
    (0x0000000000000020, "sw_thermal_slowdown"),
    (0x0000000000000040, "hw_thermal_slowdown"),
    (0x0000000000000080, "hw_power_brake"),
)

# Samples below this GPU utilisation are idle and describe nothing about the
# load being attributed, so they are excluded from the summary.
BUSY_UTIL_PCT = 5.0


def decode_throttle(raw: str) -> List[str]:
    """Active throttle-reason names from nvidia-smi's hex bitmask field."""
    try:
        v = int(str(raw).strip(), 16)
    except Exception:
        return []
    if not v:
        return []
    return [n for b, n in THROTTLE_BITS if v & b] or [hex(v)]


def parse_gpu_trace(text: str) -> List[Dict[str, Any]]:
    """nvidia-smi CSV lines -> typed rows.

    A malformed line is skipped rather than failing the whole trace — a sampler
    that loses one line still produced a usable trace, and the caller sees the
    surviving sample count."""
    rows: List[Dict[str, Any]] = []
    for ln in (text or "").splitlines():
        parts = [p.strip() for p in ln.split(",")]
        if len(parts) < 6:
            continue

        def _num(v):
            try:
                return float(v)
            except Exception:
                return None

        sm = _num(parts[0])
        if sm is None:                     # header or noise line
            continue
        rows.append({"sm_mhz": sm, "mem_mhz": _num(parts[1]),
                     "temp_c": _num(parts[2]), "power_w": _num(parts[3]),
                     "util_pct": _num(parts[4]),
                     "throttle": decode_throttle(parts[5])})
    return rows


def trace_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Clock/thermal summary + verdict from the samples taken under load.

    Returns {} when there is nothing to summarise, so the caller can report the
    underlying sampler error instead of a misleading all-clear."""
    busy = [r for r in rows if (r.get("util_pct") or 0) > BUSY_UTIL_PCT] or rows
    sm = [r["sm_mhz"] for r in busy if r.get("sm_mhz") is not None]
    if not sm:
        return {}
    thr = [r for r in busy if r.get("throttle")]
    reasons = sorted({n for r in thr for n in r["throttle"]})
    peak_t = max((r["temp_c"] for r in busy if r.get("temp_c") is not None),
                 default=None)
    droop = round(100 * (max(sm) - min(sm)) / max(sm), 1) if max(sm) else 0.0
    out: Dict[str, Any] = {
        "samples": len(busy),
        "clock": {"start_mhz": sm[0], "end_mhz": sm[-1],
                  "min_mhz": min(sm), "max_mhz": max(sm), "droop_pct": droop},
        "mem_clock_mhz": busy[0].get("mem_mhz"),
        "peak_temp_c": peak_t,
        "peak_power_w": max((r["power_w"] for r in busy
                             if r.get("power_w") is not None), default=None),
        "throttled_samples": len(thr),
        "throttled_pct": round(100 * len(thr) / len(busy)),
        "throttle_reasons": reasons,
    }
    if reasons:
        out["verdict"] = (
            "THROTTLING (%s): SM clock %.0f->%.0f MHz (%.1f%% droop) at %s C. "
            "Sustained throughput here is capped by cooling/power, not by the "
            "model or by Ollama settings."
            % (", ".join(reasons), sm[0], min(sm), droop, peak_t))
    elif droop >= 15:
        out["verdict"] = ("Clock drooped %.1f%% with no throttle flag set — likely "
                          "utilisation/power dependent; try a longer num_predict "
                          "to confirm." % droop)
    else:
        out["verdict"] = ("Clock stable (%.1f%% droop), no throttling observed."
                          % droop)
    return out
