"""DEEP-21 D21-6 / K4 finished (compute-roles, 11 Oct 2026): census comparisons flag goals a pipeline
gate overlapped - wall time not comparable, outcome still is."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.census import census_core as cc  # noqa: E402

pytestmark = pytest.mark.critical


def _row(gid, wall, ov=0, status="done"):
    r = {"id": gid, "status": status, "wall_s": wall}
    if ov:
        r["gate_overlap"] = {"gate_overlap_s": ov, "gates": ["p1"]}
    return r


def test_compare_marks_overlapped_goals():
    base = [_row("a", 100), _row("b", 200)]
    head = [_row("a", 150, ov=90), _row("b", 210)]
    g = {x["id"]: x for x in cc.compare_runs(base, head)}
    assert g["a"]["wall_comparable"] is False and g["a"]["head_gate_overlap_s"] == 90
    assert "not comparable" in g["a"]["note"] and g["a"]["outcome_change"] == "held"
    assert g["b"]["wall_comparable"] is True and "note" not in g["b"]


def test_summary_counts_overlapped_goals():
    s = cc.summarise_run("r1", [_row("a", 100, ov=30), _row("b", 200), {"id": "c", "status": "done",
                                                                         "gate_overlap": None}])
    assert s["gate_overlap_goals"] == 1
