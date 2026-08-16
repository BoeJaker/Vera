"""Critical-tier tests for perf-gate evaluation (vera/monitor/perf_gate_core.py, M3 perf-gating).

The gate reduces a perf.scan summary to pass/warn/fail and decides whether that
blocks a promote. Getting the thresholds or the advisory-vs-strict distinction
wrong would either block every merge on transient load or let a genuinely unhealthy
system be merged into — so the decision logic is pure and guarded here. No I/O.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.monitor import perf_gate_core as pg  # noqa: E402


# ── perf_verdict ─────────────────────────────────────────────────────────────

def test_nominal_is_pass():
    v = pg.perf_verdict({"crit": 0, "warn": 0, "info": 2, "ok": 5})
    assert v["verdict"] == "pass"


def test_any_critical_is_fail():
    v = pg.perf_verdict({"crit": 1, "warn": 0})
    assert v["verdict"] == "fail"
    assert "critical" in v["reason"]


def test_many_warnings_is_warn_not_fail():
    v = pg.perf_verdict({"crit": 0, "warn": 5})   # default max_warn=4
    assert v["verdict"] == "warn"


def test_warnings_at_threshold_still_pass():
    assert pg.perf_verdict({"crit": 0, "warn": 4})["verdict"] == "pass"


def test_thresholds_override():
    # tolerate one crit, and only warn above 1 warning
    v = pg.perf_verdict({"crit": 1, "warn": 0}, {"max_crit": 1})
    assert v["verdict"] == "pass"
    assert pg.perf_verdict({"crit": 0, "warn": 2}, {"max_warn": 1})["verdict"] == "warn"


def test_empty_and_garbage_summary_are_pass():
    assert pg.perf_verdict({})["verdict"] == "pass"
    assert pg.perf_verdict(None)["verdict"] == "pass"
    assert pg.perf_verdict({"crit": None, "warn": "x"})["verdict"] == "pass"


def test_crit_beats_warn_in_the_verdict():
    v = pg.perf_verdict({"crit": 2, "warn": 99})
    assert v["verdict"] == "fail"


# ── gate_blocks_promote ──────────────────────────────────────────────────────

def test_fail_blocks_only_in_strict_mode():
    assert pg.gate_blocks_promote("fail", strict=True) is True
    assert pg.gate_blocks_promote("fail", strict=False) is False


def test_warn_and_pass_never_block():
    for v in ("warn", "pass"):
        assert pg.gate_blocks_promote(v, strict=True) is False
        assert pg.gate_blocks_promote(v, strict=False) is False


# ── top_findings ─────────────────────────────────────────────────────────────

def test_top_findings_drops_ok_and_compacts():
    got = pg.top_findings([
        {"severity": "crit", "area": "Ollama", "title": "saturated", "detail": "x"},
        {"severity": "ok", "area": "Host", "title": "nominal"},
        {"severity": "warn", "area": "Host", "title": "CPU 92%"},
    ])
    assert got == [
        {"severity": "crit", "area": "Ollama", "title": "saturated"},
        {"severity": "warn", "area": "Host", "title": "CPU 92%"},
    ]


def test_top_findings_caps_at_n():
    findings = [{"severity": "warn", "area": "H", "title": str(i)} for i in range(10)]
    assert len(pg.top_findings(findings, n=3)) == 3


def test_top_findings_empty():
    assert pg.top_findings([]) == []
    assert pg.top_findings(None) == []
