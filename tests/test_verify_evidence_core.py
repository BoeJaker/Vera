"""The verifier quotes the tool, not the story (plan item 18).

run70-73 (24 Sep 2026): 11 verifier reasons cited the executor's summary;
build-multifile's "run the tests" step was verified met on a `cat` after
pytest had reported three failures. Pure tests of the evidence rules plus a
source pin that the verifier applies them.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.dag import verify_evidence_core as V  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")

PYTEST_FAIL = ('{"ok": true, "rc": 0, "stdout": "============================= test session starts '
               '==============================\\ncollected 3 items\\ntest_stats.py::test_mean FAILED\\n'
               '=========================== 3 failed in 0.05s ==========================="}')
PYTEST_OK = '{"ok": true, "rc": 0, "stdout": "collected 3 items\\n\\n=== 3 passed in 0.02s ==="}'
CAT = '{"ok": true, "rc": 0, "stdout": "import pytest\\nfrom statkit.stats import mean"}'


def test_the_census_criterion_is_about_a_test_outcome():
    assert V.wants_test_outcome("Test run completes with exit code 0 and no failed/test_error outputs, "
                                "confirming all statistical functions pass.")
    assert V.wants_test_outcome("The test suite passes successfully.")
    # a criterion about the test FILE is not about an outcome
    assert not V.wants_test_outcome("File test_stats.py created by code.author with syntax_ok=true, "
                                    "containing valid pytest/unittest assertions.")
    assert not V.wants_test_outcome("clock.html exists and shows the time")


def test_a_cat_after_a_failing_pytest_does_not_pass_the_step():
    calls = [{"tool": "exec.bash.run", "ok": True, "preview": PYTEST_FAIL},
             {"tool": "exec.bash.run", "ok": True, "preview": CAT}]
    v = V.settle_test_criterion("Test run completes with exit code 0 and all tests pass", calls)
    assert v == {"met": False, "reason": 'the last test run in this step reported failures: '
                                         '"test_stats.py::test_mean FAILED"'}


def test_no_test_run_at_all_is_not_met():
    calls = [{"tool": "exec.bash.run", "ok": True, "preview": CAT}]
    v = V.settle_test_criterion("all tests pass", calls)
    assert v and v["met"] is False and "no test run happened" in v["reason"]


def test_a_clean_run_goes_to_the_judge():
    calls = [{"tool": "exec.bash.run", "ok": True, "preview": PYTEST_FAIL},
             {"tool": "exec.python.run", "ok": True, "preview": PYTEST_OK}]
    assert V.settle_test_criterion("all tests pass", calls) is None
    assert V.last_test_run(calls)["passed"] is True


def test_unittest_output_is_read_too():
    ran_fail = '{"ok": false, "rc": 1, "stderr": "..F.\\nFAIL: test_normal_cases\\nRan 4 tests in 0.001s\\nFAILED (failures=1)"}'
    ran_ok = '{"ok": true, "rc": 0, "stderr": "....\\nRan 4 tests in 0.001s\\n\\nOK"}'
    assert V.last_test_run([{"tool": "exec.python.run", "preview": ran_fail}])["passed"] is False
    assert V.last_test_run([{"tool": "exec.python.run", "preview": ran_ok}])["passed"] is True


def test_meta_entries_and_non_exec_calls_are_not_test_runs():
    calls = [{"tool": "(denied done)", "preview": PYTEST_OK}, {"tool": "sandbox.session.fs.read", "preview": PYTEST_OK}]
    assert V.last_test_run(calls)["found"] is False


def test_the_verifier_applies_the_rules():
    src = open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()
    i = src.index("async def _v6_verify_step(")
    body = src[i:i + 40000]
    assert "_settled = _verify_evidence.settle_test_criterion(crit, _calls)" in body
    assert "_verify_evidence.EVIDENCE_RULE" in body
    assert "the executor's OWN ACCOUNT, not evidence" in body
    assert body.index("_settled = ") < body.index("_authored = _v6_authored_path(_last)")   # before the fast path
    ast.parse(src)
