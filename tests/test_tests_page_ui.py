"""The Loop Lab Tests section (looplab-control §9.4 S2, 18 Oct 2026): the scheduled tests on the kit.

Owner to four-minds, 18 Oct: "your addition to the loop lab UI could be much better - its hard to read and doesnt use
the loop lab style ask vera 44 to add it to their plan ...". Handover: four-minds/LOOPLAB-TESTS-HANDOVER.md. The page
draws decide.tests.status with THE timeline, THE matrix and THE drill; four-minds owns the data.

Source-level, like the other panel tests.
"""
import os
import re

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")


@pytest.fixture(scope="module")
def src():
    with open(os.path.join(ROOT, "vera", "evolve", "evolve_panel.html"), encoding="utf-8") as fh:
        return fh.read()


def _section(src, sec):
    i = src.index('<div class="sec" id="sec-%s"' % sec)
    return src[i:src.index("\n</div>\n", i)]


def _block(src):
    i = src.index("/* ═══ TESTS — every scheduled test on the kit")
    return src[i:src.index("/* ═══ SCHEDULE —", i)]


def test_the_stop_gap_frame_is_gone_and_the_kit_draws_it(src):
    sec = _section(src, "tests")
    assert "<iframe" not in sec and "/decide/tests" not in sec
    assert '<vera-drill class="olv-drill" id="tst-drill" url></vera-drill>' in sec
    assert "tests:loadTestsPage," in src
    js = _block(src)
    assert "VeraDrill.call('decide.tests.status',{})" in js
    assert "<vera-timeline id=\"tst-tl\"" in js and "<vera-matrix id=\"tst-mxel\">" in js


def test_a_broken_measure_reads_broken_never_a_score(src):
    js = _block(src)
    assert "c.broken===true||(c.cases&&(st.error||0)>=c.cases)" in js
    assert "BROKEN</span> every case errored" in js
    assert "if(h.error)return '<span class=\"tst-broken\">BROKEN</span>" in js


def test_every_headline_names_its_source(src):
    js = _block(src)
    body = js[js.index("function _tstHeadline("):js.index("function _tstHealthChip(")]
    cases = re.findall(r"case '([a-z_]+)':", body)
    assert set(cases) >= {"matrix", "claims", "shortlist", "cascade", "calibration", "memory", "chat_corpus"}
    assert body.count("+src(") >= len(cases), "each case appends the cap it read"


def test_runs_and_windows_are_lanes_filtered_to_the_tests_entries(src):
    js = _block(src)
    assert "where:{source:'loop-lab',schedule_id:ids}" in js and "where:{schedule_id:ids}" in js
    for cap in ("caps.shortlist.replay", "memory.perf.rows", "decide.matrix.history", "decide.claims.history",
                "worldview.spread.history"):
        assert "cap:'%s'" % cap in js, cap


def test_the_matrix_greys_thin_cells_and_puts_live_first(src):
    js = _block(src)
    assert "+x.outcomes_n>=30" in js and "'thin'" in js
    assert "cells.sort((a,b)=>(b.live?1:0)-(a.live?1:0))" in js
    assert "Array.isArray(r.cells)" in js, "the flat cells four-minds added are read when present"


def test_controls_live_in_the_drill_only(src):
    js = _block(src)
    rows = js[js.index("$('tst-rows').innerHTML="):js.index("_tstTimeline();\n  _tstMatrix();")]
    assert "<button" not in rows, "the rows are a display: run now / enable sit in the drill"
    kit = js[js.index("function _tstKit("):]
    assert "tstRunNow(" in kit and "tstEnable(" in kit
    assert "confirm('Start this scheduled test now?')" in js


def test_live_strip_protected_and_declared_lanes(src):
    sec = _section(src, "tests")
    assert '<vera-live-strip id="tst-live"' in sec
    js = _block(src)
    assert "items:'census.gates',where:{state:'running'}" in js and "items:'census.protected'" in js
    assert "cap:'sys.activity.status'" in js and "where:{kind:'test'}" in js


def test_race_to_green_is_a_matrix_of_gate_attempts_that_drills_into_failures(src):
    js = _block(src)
    body = js[js.index("async function _tstRace("):]
    assert "r.pipeline_id&&r.branch" in body and "cols_order:'n'" in body and "ok:['green']" in body
    assert "open('test-run',c.ts,{root:true,tab:c.v==='red'?'failures':''})" in body


def test_activity_has_a_reader_in_the_sys_family():
    o = open(os.path.join(ROOT, "vera", "capability_orchestration.py"), encoding="utf-8").read()
    i = o.index('@capability("sys.activity.status"')
    assert "return await _health_activity()" in o[i:i + 900]
