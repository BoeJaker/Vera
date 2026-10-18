"""Runs on the kit (looplab-control §9.4 S5, 19 Oct 2026).

"every run, goal drill with waterfall and step diff, goals x steps, regression matrix, causes, trend": one series at a
time from evolve.suites, drawn with THE matrix, THE timeline and THE drill; a goal's waterfall and step diff read its
loop's trace (workshop.agent_loop.trace). Source-level, like the other panel tests.
"""
import os

import pytest

pytestmark = pytest.mark.critical


@pytest.fixture(scope="module")
def src():
    with open(os.path.join(os.path.dirname(__file__), "..", "vera", "evolve", "evolve_panel.html"), encoding="utf-8") as fh:
        return fh.read()


def _block(src):
    i = src.index("/* ═══ RUNS — every run of a series on the kit")
    return src[i:src.index("/* ═══ SCHEDULE —", i)]


def test_runs_sit_atop_work_and_load_with_it(src):
    i = src.index('<div class="sec" id="sec-work"')
    sec = src[i:src.index("<!-- Work, live:", i)]
    assert 'id="runs-card"' in sec and '<vera-drill class="olv-drill" id="runs-drill" url></vera-drill>' in sec
    assert "function loadWork(){ runsLoad();" in src


def test_one_series_a_regression_matrix_and_causes(src):
    js = _block(src)
    assert "VeraDrill.call('evolve.suites',{limit:300})" in js
    assert "cols_order:'n'" in js and "ok:['done']" in js and "sort:'failures'" in js
    assert "regressed in the last run" in js
    assert "function runsCauses()" in js and "hit the wall-clock cap" in js


def test_a_goal_drills_to_its_waterfall_step_diff_and_history(src):
    js = _block(src)
    assert "VeraDrill.registerKind('goal-run'" in js and "VeraDrill.registerKind('suite-run'" in js
    assert "ctx.call('workshop.agent_loop.trace',{session_id:sid})" in js
    for tab in ("'waterfall'", "'steps'", "'history'"):
        assert "id:" + tab in js, tab


def test_the_trend_is_lanes_over_the_series(src):
    js = _block(src)
    assert "value:'pass_rate',where:{tag}" in js and "value:'avg_combined',where:{tag}" in js
