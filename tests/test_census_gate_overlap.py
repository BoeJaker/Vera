"""Census rows say when a pipeline gate overlapped the goal (compute-roles K4, owner 11 Oct 2026):
a gate shares the nodes' CPU with the census's inference, so a row's wall time is flagged, not
silently compared."""
import ast
import calendar
import pathlib
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "tools" / "loop-census" / "run_census.py").read_text(encoding="utf-8")

pytestmark = pytest.mark.critical


def _funcs(*names):
    """Functions from run_census.py without importing the harness (it runs on import)."""
    tree = ast.parse(SRC)
    body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    ns = {"calendar": calendar, "time": time}
    exec(compile(ast.Module(body=body, type_ignores=[]), "run_census_part", "exec"), ns)
    return ns


F = _funcs("_epoch", "gate_windows", "overlap_seconds")


def test_overlap_counts_each_second_once():
    ov = F["overlap_seconds"]
    assert ov([], 0, 100) == 0
    assert ov([(10, 30)], 0, 100) == 20
    assert ov([(10, 30), (20, 50)], 0, 100) == 40          # overlapping gates once
    assert ov([(-50, 10), (90, 200)], 0, 100) == 20        # clipped to the goal
    assert ov([(200, 300)], 0, 100) == 0


def test_gate_windows_from_pipeline_steps():
    gw, ep = F["gate_windows"], F["_epoch"]
    pipe = {"steps": [{"stage": "adopt", "ts": "2026-10-11T09:58:20.311287Z"},
                      {"stage": "test-pool", "ts": "2026-10-11T09:58:20.398159Z"},
                      {"stage": "critical-tests", "ts": "2026-10-11T09:59:37.880143Z"}]}
    assert gw(pipe) == [(ep("2026-10-11T09:58:20"), ep("2026-10-11T09:59:37"))]
    running = {"steps": pipe["steps"][:2], "ended_at": ""}
    assert gw(running, now=ep("2026-10-11T10:00:00"))[0][1] == ep("2026-10-11T10:00:00")
    assert gw({"steps": pipe["steps"][:2], "ended_at": "2026-10-11T10:00:00Z"}) == []
    assert gw({"steps": [{"stage": "adopt", "ts": "2026-10-11T09:58:20Z"}]}) == []


def test_rows_carry_gate_overlap():
    assert '"gate_overlap": _gate_ov,' in SRC
    assert "_gate_ov = gate_overlap(t0, time.time())" in SRC
    assert 'mcp("evolve.pipeline.list", {"limit": 40}' in SRC
