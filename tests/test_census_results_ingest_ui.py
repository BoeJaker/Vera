"""The Runs view shows an ingested census goal as what it is.

Slice 2 of the Loop Lab flattening: a census goal is a run record with
source=census. Source-level pins, like the other panel tests: the source
filter offers every source the store writes, a census row names its census
run instead of pretending to be a sandbox or in-process run, every row opens
the task through time, and the run-detail modal hands a census record to the
census record rather than to a critic that would score an empty output.
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


def _runs_section(src):
    i = src.index('id="sec-runs"')
    return src[i:src.index("</div>\n</div>", i)]


def test_the_source_filter_offers_every_source_the_store_writes(src):
    sec = _runs_section(src)
    opts = re.findall(r'<option value="([a-z]*)"', sec[sec.index('id="runs-src"'):])
    for s in ("suite", "run", "manual", "improve", "census", "goal", "captest", "ide"):
        assert s in opts, "source '%s' cannot be picked" % s


def test_the_hint_no_longer_promises_a_suite_run_that_never_came(src):
    sec = _runs_section(src)
    assert "appears here as a suite run" not in sec
    assert "source <span class=\"mono\">census</span>" in sec


def test_a_census_row_names_its_census_run_and_opens_the_task_history(src):
    body = _fn(src, "_runsRender")
    assert "r2.source==='census'" in body
    assert "r2.census_run" in body, "the 'where' cell is the census run, not sandbox/in-proc"
    assert "openTaskHistory(" in body, "every run row opens its task through time"
    assert "r2.hit_cap" in body, "a capped goal is marked as capped, not as a generic error"
    assert "compareRun(" in body


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
