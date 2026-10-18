"""FM-11 automatic revert (four-minds §4 / §12, backbone D10-11, 18 Oct 2026)."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import calibration_core as CC  # noqa: E402
from vera.minds import revert_core as RV  # noqa: E402

pytestmark = pytest.mark.critical


def _gold(n, prec, ece=0.0):
    return {"vs_gold": {"n": n, "precision_at_tau": prec, "ece": ece}}


def test_alert_records_carry_the_kind_and_the_old_strings_stay():
    recs = CC.alert_records("loop.tier", "knn:minilm", _gold(40, 0.90, ece=0.2), _gold(40, 0.95))
    assert [r["kind"] for r in recs] == ["precision_drop", "ece"]
    assert CC.alerts("loop.tier", "knn:minilm", _gold(40, 0.90, ece=0.2), _gold(40, 0.95)) == [r["text"] for r in recs]
    esc = CC.alert_records("loop.tier", "", {}, {}, {"rate": 0.7, "took": 10, "handed": 30}, 0.4)
    assert [r["kind"] for r in esc] == ["escalation", "drift"]


def test_only_a_taking_student_on_a_revert_trigger_goes_off():
    items = [{"question": "loop.tier", "impl": "knn:minilm", "kind": "precision_drop", "text": "tier fell"},
             {"question": "loop.step_done", "impl": "", "kind": "escalation", "text": "esc"},
             {"question": "loop.intent", "impl": "", "kind": "drift", "text": "drift"},
             {"question": "loop.prestep_needed", "impl": "x", "kind": "ece", "text": "ece only"}]
    modes = {"decide.student.tier": "first", "decide.student.step_done": "shadow",
             "decide.student.intent": "sampled", "decide.student.prestep_needed": "first"}
    out = RV.plan(items, modes, {"loop.tier": "knn:minilm"})
    assert [(r["switch"], r["from"], r["to"]) for r in out] == [("decide.student.tier", "first", "off"),
                                                             ("decide.student.intent", "sampled", "off")]
    assert RV.reason(out[0]).startswith("auto-revert (FM-11): tier fell")


def test_a_precision_drop_of_another_implementation_does_not_revert():
    items = [{"question": "loop.tier", "impl": "llm:qwen3:0.6b", "kind": "precision_drop", "text": "x"}]
    assert RV.plan(items, {"decide.student.tier": "first"}, {"loop.tier": "knn:minilm"}) == []
    assert RV.plan(items, {"decide.student.tier": "first"}, {}) != []          # no plan known: any student counts


def test_first_modes_of_step_verify_count_as_taking():
    assert RV.taking("first_met") and RV.taking("first_not_met") and not RV.taking("shadow") and not RV.taking("off")


def test_the_guard_capability():
    src = open(os.path.join(ROOT, "vera", "minds", "calibration.py"), encoding="utf-8").read()
    body = src[src.index('"decide.calibration.guard"'):]
    assert "RV.plan(rep.get(\"alert_items\") or [], modes, students)" in body
    assert 'by="four-minds:auto-revert"' in body and '"type": "minds.killed"' in body
    assert "dry_run" in body
