"""The census is ONE table: a row per run, expand for goals, click for the record.

Asked 2026-09-10: "could you make the entire census UI one table that gives me
all the data on each census by expanding the row to see individual tasks run
and their stats, click on them to see what is now in the modal - the full
detail - in one filterable table instead of over 5 separate tables." And: the
in-progress census was "very slow to update", reachable only by loading one UI,
then Compare, then a modal.

Source-level, like the other panel tests. What these pin is the SHAPE - the
single table, the expand path, the one-click record, the live controls, the
routing column - not pixel detail.
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


def _section(src):
    i = src.index('id="sec-work"')
    return src[i:src.index("\n</div>\n", i)]   # to the section's own close (the page after it has changed twice)


# ── one table, not five cards ───────────────────────────────────────────────
def test_the_data_cards_are_gone(src):
    sec = _section(src)
    for old in ("census-compare", "census-operator", "census-op-status", "census-board",
                "census-base", "census-head", "census-cmp-counts"):
        assert old not in sec, "the old '%s' card is still in the section" % old
    assert 'id="census-runs"' in sec


def test_the_templates_card_stays(src):
    """The launcher is a control, not a data table, and its own tests pin it."""
    assert 'id="ct-pick"' in _section(src)


def test_one_table_renders_every_run(src):
    body = _fn(src, "renderCensusTable")
    assert 'id="census-table"' in body
    assert "_cenRow" in body and "_cenOpRow" in body, "operator batches are rows of the same table"


def test_a_run_row_expands_to_its_goals(src):
    assert "function toggleCensusRun(" in src
    row = _fn(src, "_cenRow")
    assert "toggleCensusRun(" in row
    assert "cen-detail" in row
    body = _fn(src, "loadCensusGoals")
    assert "/census/run?run=" in body


def test_a_goal_row_opens_the_full_record(src):
    body = _fn(src, "loadCensusGoals")
    assert "openCensusGoal(" in body


def test_the_table_is_filterable(src):
    body = _fn(src, "renderCensusTable")
    assert 'id="cen-text"' in body and "setCensusText" in body
    assert "setCensusTemplate" in body and "setCensusStatus" in body
    assert "toggleCensusPartial" in body


def test_the_live_run_is_the_first_row(src):
    body = _fn(src, "renderCensusTable")
    assert "'current'" in body
    assert "sort(" in body


# ── the comparison lives in the expanded row ────────────────────────────────
def test_compare_is_per_run_with_a_default_base(src):
    assert "function _cenDefaultBase(" in src
    base = _fn(src, "_cenDefaultBase")
    assert "template" in base and "excluded" in base, "the default base is the previous usable run of the SAME template"
    goals = _fn(src, "loadCensusGoals")
    assert "loadCensusCompare(" in goals
    assert "cen-cmp" in goals


# ── routing is on the row ───────────────────────────────────────────────────
def test_routing_is_a_column_on_runs_and_goals(src):
    assert "function _cenRoute(" in src
    assert "_cenRoute(" in _fn(src, "_cenRow")
    assert "_cenRoute(" in _fn(src, "loadCensusGoals")
    rt = _fn(src, "_cenRoute")
    for signal in ("coder_changed", "spill_calls", "reroutes", "coder_overridden"):
        assert signal in rt, "routing cell must show %s" % signal


def test_the_goal_modal_shows_the_calls(src):
    body = _fn(src, "openCensusGoal")
    assert "record.routing" in body
    assert "re-route" in body.lower()
    for col in ("tok/s", "GPU", "node"):
        assert col in body


# ── which code a run ran on, and where it changed ───────────────────────────
def test_code_is_a_column_on_runs_and_goals(src):
    assert "function _cenCode(" in src
    assert "_cenCode(" in _fn(src, "_cenRow")
    assert "_cenCode(g.code_summary)" in _fn(src, "loadCensusGoals")
    cell = _fn(src, "_cenCode")
    for signal in ("changed_during_goal", "boundary", "segments", "changes"):
        assert signal in cell, "code cell must show %s" % signal
    assert "restart" in cell.lower()


def test_the_goal_modal_names_the_code(src):
    body = _fn(src, "openCensusGoal")
    assert "code_summary" in body and "_cenCodeOf(" in body


# ── one task through time ───────────────────────────────────────────────────
def test_a_goal_opens_its_task_history(src):
    assert "async function openTaskHistory(" in src
    assert "openTaskHistory(_thTaskIdFor(" in _fn(src, "loadCensusGoals")
    body = _fn(src, "openTaskHistory")
    assert "/evolve/task/history?id=" in body
    assert "openModalLoading(" in body and body.index("openModalLoading(") < body.index("await api(")
    assert "census_stats" in body and "code_changes" in body
    # A census result opens the census record; a suite result opens the run.
    assert "openCensusGoal(" in body and "openRun(" in body


def test_the_tasks_page_shows_history_beside_the_definition(src):
    body = _fn(src, "loadTasks")
    assert "/evolve/tasks/overview" in body
    assert "openTaskHistory(" in body
    for col in ("runs", "ok", "wall median", "last"):
        assert col in body


# ── the live run can be paused, resumed and dropped from here ───────────────
def test_the_live_strip_carries_the_controls(src):
    body = _fn(src, "loadCensusLive")
    assert "censusControl('pause')" in body.replace("\\'", "'")
    assert "censusControl('resume')" in body.replace("\\'", "'")
    assert "censusControl('drop')" in body.replace("\\'", "'")
    ctl = _fn(src, "censusControl")
    assert "/census/control/set" in ctl
    assert "confirm(" in ctl, "a drop ends the whole set; it must ask"


def test_the_live_strip_says_paused(src):
    body = _fn(src, "loadCensusLive")
    assert "paused" in body and "harness" in body


# ── the live strip refreshes without the click chain ────────────────────────
def test_the_live_strip_still_returns_the_done_count(src):
    """The poller refreshes the table only when a goal lands."""
    assert "return doneN" in _fn(src, "loadCensusLive")


def test_the_live_row_reloads_its_goals_when_one_lands(src):
    body = _fn(src, "loadCensusRuns")
    assert "delete _cenRecCache['current']" in body
    assert "loadCensusGoals('current')" in body


def test_the_panel_script_parses(src):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    bodies = re.findall(r"<script[^>]*>(.*?)</script>", src, re.S)
    p = subprocess.run([node, "--check", "-"], input="\n;\n".join(bodies),
                       text=True, capture_output=True)
    assert p.returncode == 0, "panel JS does not parse:\n" + (p.stderr or "")[-2000:]
