"""The runs view shows an ingested census goal as what it is.

Slice 2 of the Loop Lab flattening: a census goal is a run record with
source=census. Since slice 3 the Runs page is the runs view of the Work page
(renderCensusTable): every driver kind in one table, a single run's source
filter among the filters, and the run-detail modal hands a census record to
the census record rather than to a critic that would score an empty output.
"""
import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.critical

PANEL = os.path.join(os.path.dirname(__file__), "..", "vera", "evolve", "evolve_panel.html")


@pytest.fixture(scope="module")
def src():
    with open(PANEL, encoding="utf-8") as fh:
        return fh.read()


def _fn(src, name):
    start = src.index("function " + name + "(")
    return src[start:src.index("\n}\n", start)]


def test_the_runs_view_offers_every_kind_and_every_single_run_source(src):
    body = _fn(src, "renderCensusTable")
    for k in ("census", "suite", "improve", "run"):
        assert "'%s'" % k in body, "kind '%s' cannot be picked" % k
    for s in ("run", "manual", "goal", "captest", "ide"):
        assert "'%s'" % s in body, "source '%s' cannot be picked" % s


def test_the_old_runs_page_and_its_promise_are_gone(src):
    assert 'id="sec-runs"' not in src
    assert "appears here as a suite run" not in src
    assert "function _runsRender(" not in src, "dead renderer"


def test_a_census_record_reads_as_a_census_run_in_the_task_history(src):
    body = _fn(src, "loadWorkTaskDetail")
    assert "openCensusGoal(" in body and "openRun(" in body, "a census result opens the census record, a run the run"
    assert "r.hit_cap" in body and "r.reruns" in body


def test_the_run_detail_hands_a_census_record_to_the_census_record(src):
    body = _fn(src, "openRunDetail")
    assert "d.source==='census'" in body
    assert "openCensusGoal(" in body and "d.census_run" in body
    assert "openTaskHistory(" in body
    # the critics score a final output; a census record has none here
    assert "isCensus?'':'<button class=\"btn\" onclick=\"assessRun(" in body
    assert "if(!isCensus)h+='<label class=\"fld\" style=\"margin-top:8px\">final output" in body


def test_the_scoreboard_says_when_the_latest_suite_is_a_census_run(src):
    body = _fn(src, "renderScoreboard")
    assert "cur.source==='census'" in body and "cur.census_run" in body


def test_the_panel_still_parses(src):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    bodies = re.findall(r"<script[^>]*>(.*?)</script>", src, re.S)
    assert bodies
    p = subprocess.run([node, "--check", "-"], input="\n;\n".join(bodies),
                       text=True, capture_output=True)
    assert p.returncode == 0, "panel JS does not parse:\n" + (p.stderr or "")[-2000:]
