"""A failure that two fix steps did not change ends the run (plan item 28).

run77 build-multifile (25 Sep 2026): 43 cycles over three steps chasing one
failing test, every verify honest, no ceiling.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.dag import fix_loop_core as F  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
R1 = 'the last test run in this step reported failures: "test_stats.py::test_calculate_mode FAILED                                [ 75%]"'
R2 = 'the last test run in this step reported failures: "statkit/test_stats.py::test_calculate_mode FAILED                        [100%]"'
R3 = 'the criterion asks for a test outcome but no test run happened in this step'
R4 = 'the last test run in this step reported failures: "E         comparison failed"'


def test_the_census_reasons_share_one_signature():
    assert F.failure_signature(R1) == "test_stats.py::test_calculate_mode"
    assert F.failure_signature(R2) == "test_stats.py::test_calculate_mode"
    assert F.failure_signature(R3) == ""
    assert F.failure_signature(R4) == "e comparison failed"
    assert F.failure_signature("code.author produced x.py and a real parser verified its syntax") == ""


def test_three_unmet_results_with_one_failure_stop_the_run():
    results = [{"id": 4, "met": False, "met_reason": R1}, {"id": 5, "met": False, "met_reason": R3},
               {"id": 5, "met": False, "met_reason": R2}, {"id": 6, "met": False, "met_reason": R2}]
    streak, sig = F.same_failure_streak(results)
    assert (streak, sig) == (3, "test_stats.py::test_calculate_mode") and F.should_stop(streak)
    assert not F.should_stop(2) and "survived 3 attempts" in F.report(sig, 3)


def test_a_different_failure_or_a_pass_resets_the_count():
    results = [{"id": 4, "met": False, "met_reason": R1}, {"id": 5, "met": False, "met_reason": R4},
               {"id": 6, "met": False, "met_reason": R1}]
    assert F.same_failure_streak(results) == (1, "test_stats.py::test_calculate_mode")
    assert F.same_failure_streak([{"id": 1, "met": True, "met_reason": "3 passed"}]) == (0, "")


def test_the_failure_is_read_from_every_attempt_not_only_the_last():
    """run78 build-multifile: each step's final reason differed; the first attempt of every step
    named test_median."""
    med = 'the last test run in this step reported failures: "statkit/test_stats.py::test_median FAILED [ 66%]"'
    results = [
        {"id": 1, "met": True, "met_reason": "code.author produced stats.py"},
        {"id": 3, "met": False, "met_reason": R3, "verify_reasons": [med, 'the last test run in this step reported failures: "Tests failed."', R3]},
        {"id": 4, "met": False, "met_reason": med, "verify_reasons": ['code.edit changed statkit/stats.py AFTER the last test run ("ERROR: ===== test session starts")', med, med]},
        {"id": 5, "met": False, "met_reason": 'the last test run in this step reported failures: "ERROR: Traceback (most recent call last):"',
         "verify_reasons": [med, R3, 'the last test run in this step reported failures: "ERROR: Traceback (most recent call last):"']},
    ]
    streak, sig = F.same_failure_streak(results)
    assert sig == "test_stats.py::test_median" and streak == 3 and F.should_stop(streak)
    assert F.result_signatures(results[3]) == ["test_stats.py::test_median", "error: traceback (most recent call last):"]


def test_the_runner_stops_before_the_controller_and_the_gate_reports_it():
    src = open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()
    assert src.index("_fix_loop.same_failure_streak(results)") < src.index("ctrl = await _v6_control(")
    assert "agent_loop_v6.fix_loop_bound" in src
    assert "bounded_failure=fix_loop_bound" in src
    assert 'res["verify_reasons"] = list(_vr)' in src
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_v6_final_gate")
    body = ast.get_source_segment(src, fn)
    assert body.index("if bounded_failure:") < body.index("ledger = _v6_build_ledger(")
    assert '"follow_up": []' in body
