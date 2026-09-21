"""The Work page: ONE table for eight pages (Loop Lab flattening slice 3).

Asked 2026-09-10: "look at loop lab flattening - just like you flattened those
4-5 tables - may also apply cross-page id like to drastically reduce the
number of pages in the UI". Run once, Improve · set up, the Improve page's
session list, Suite, Variants, Census, Tasks and Runs are one page now: a
table whose rows are tasks or, by toggle, driver runs, with what is live on
top, ONE poller, and the controls of the absorbed pages as buttons/modals.

Source-level, like the other panel tests: what these pin is the shape - the
rail, the single section, the toggle, expand/click, the one poller, and the
editor keeping what its form does not show - not pixels.
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


def _section(src, sec):
    i = src.index('<div class="sec" id="sec-%s"' % sec)
    return src[i:src.index("\n</div>\n", i)]


# ── the rail shrank: 22 → 17, Work first ─────────────────────────────────────
def test_the_absorbed_pages_are_gone_from_rail_and_dom(src):
    for sec in ("tasks", "runs", "census", "overview", "variants", "improve"):
        assert 'data-sec="%s"' % sec not in src, "rail still has %s" % sec
        assert 'id="sec-%s"' % sec not in src, "section %s still in the DOM" % sec
    secs = re.findall(r'<div class="sec" id="sec-([a-z]+)"', src)
    assert "work" in secs and len(secs) == 6, secs   # 17 after slice 3; 13 after Ship (5); 9 after Agents (6); 5 after Mission control (7); 6 with Schedule (2026-09-21)


def test_work_is_first_and_the_home(src):
    rail = re.findall(r'data-sec="([a-z]+)"', src)
    assert rail[0] == "work"
    assert re.search(r'<button class="btn on" data-sec="work"', src)
    assert '<div class="sec" id="sec-work">' in src, "shown at load"
    assert '<div class="sec" id="sec-ship" style="display:none">' in src
    assert "return b?b.dataset.sec:'work'" in _fn(src, "_curSec")
    assert "if(!routeHash()&&_curSec()==='work')nav('work')" in src, "a deep link lands on its page; otherwise Work"


def test_old_deep_links_still_land_on_the_table(src):
    nav = _fn(src, "nav")
    for sec in ("tasks", "runs", "census", "overview", "variants", "improve"):
        assert "%s:()=>nav('work')" % sec in nav, sec
    assert "work:loadWork" in nav


# ── one section, one table, two views ────────────────────────────────────────
def test_the_section_holds_the_toggle_the_live_strip_and_both_views(src):
    sec = _section(src, "work")
    assert 'id="work-mode"' in sec and "setWorkMode('runs')" in sec and "setWorkMode('tasks')" in sec
    for el in ("census-live", "work-live", "census-runs", "work-tasks", "census-charts", "work-charts", "ct-pick", "census-follow"):
        assert 'id="%s"' % el in sec, el
    for ctl in ("openRunOnce()", "openImproveForm()", "openTask({})", "openVariants()", "loadWork()"):
        assert ctl in sec, ctl


def test_the_runs_view_renders_every_driver_kind_in_the_same_table(src):
    body = _fn(src, "renderCensusTable")
    assert "_wkRow(x)" in body and "_cenRow(s)" in body, "census rows and the other kinds, one table"
    assert 'id="census-table"' in body
    row = _fn(src, "_wkRow")
    # 15 cells + the detail cell, exactly as a census row
    assert row.count("<td") == _fn(src, "_cenRow").count("<td") == 16, "the same 15 cells as a census row (plus its detail cell)"
    assert 'colspan="15"' in row
    assert "toggleWorkDriver(" in row
    det = _fn(src, "loadWorkDriverDetail")
    for kind in ("suite", "improve", "run"):
        assert "kind==='%s'" % kind in det
    assert "/evolve/improve/status" in det and "/evolve/run/get" in det
    assert "openRun(" in det and "openTaskHistory(" in det and "openSession(" in det


def test_the_tasks_view_is_a_filterable_table_with_expand_and_actions(src):
    body = _fn(src, "renderWorkTasks")
    assert "setWorkTaskText" in body and "setWorkTaskTemplate" in body and "toggleWorkTaskProblems" in body
    assert 'id="work-task-table"' in body and "toggleWorkTask(" in body
    for action in ("runTask(", "openTaskHistory(", "editWorkTask("):
        assert action in body, action
    det = _fn(src, "loadWorkTaskDetail")
    assert "/evolve/task/history" in det and "_thStats(" in det
    assert "openCensusGoal(" in det and "openRun(" in det
    assert "/evolve/tasks/overview" in _fn(src, "loadWorkTasks")


def test_the_editor_keeps_what_its_form_does_not_show(src):
    """A seeded census task carries census{}, overrides{model}, scenario,
    target that the form never showed; an edit used to strip them."""
    assert "_tkOrig=t.id?JSON.parse(JSON.stringify(t)):{}" in _fn(src, "openTask")
    assert "Object.assign({},_tkOrig," in _fn(src, "saveTask")
    assert "openTask(t)" in _fn(src, "editWorkTask") and "/evolve/tasks" in _fn(src, "editWorkTask")


# ── one poller ───────────────────────────────────────────────────────────────
def test_one_poller_asks_one_endpoint(src):
    poll = _fn(src, "censusPoll")
    assert poll.count("api('/evolve/work/live')") == 1
    assert "_wkLive(_workLive)" in poll
    assert "_cenBusy" in poll and "finally{ _cenBusy=false; _cenArm(); }" in poll
    assert "_curSec()==='work'" in _fn(src, "_cenFollowing")
    # the tables refresh when the live answer CHANGES, not on a tick
    assert "sig!==_workLiveSig" in poll
    # the absorbed pages' own timers no longer re-arm behind the page
    ses = _fn(src, "loadSessions")
    assert "if($('sessions-body')&&ss.some(" in ses
    assert "if(!$('sessions-body')){if(!quiet)workOnChange();return}" in ses


def test_events_refresh_work_not_the_vanished_pages(src):
    ev = _fn(src, "onEvent")
    assert "_curSec()==='runs'" not in ev and "_curSec()==='improve'" not in ev
    assert "workOnChange()" in ev
    assert "if(!$('ov-scoreboard')){workOnChange();return}" in _fn(src, "loadOverview")
    assert "if(!$('tasks-body')){if(_curSec()==='work'){setWorkMode('tasks');}return}" in _fn(src, "loadTasks")
    assert "workOnChange()" in _fn(src, "loadRuns")


# ── the absorbed pages' controls became modals ───────────────────────────────
def test_the_absorbed_controls_are_reachable_as_modals(src):
    imp = _fn(src, "openImproveForm")
    assert "_WORK_IMPROVE_FORM" in imp and "loadComponentMap()" in imp
    assert 'id="im-cat"' in src and 'id="im-profile"' in src and "startImprove()" in src
    var = _fn(src, "openVariants")
    assert "_WORK_VARIANTS" in var and "loadVariants()" in var and "bench-compare" in var
    ro = _fn(src, "openRunOnce")
    assert "_WORK_RUN_ONCE" in ro
    for fn in ("runSuite()", "runQuickSmoke()", "runGoal()", "openGenerate()"):
        assert fn in src, fn
    assert "openImproveForm()" in _fn(src, "improveTarget"), "the component map picks a target in the modal"
    assert "closeModal();workOnChange();nav('watch')" in _fn(src, "startImprove")


def test_the_quick_tests_card_moved_to_unit_tests(src):
    # ...and with Unit tests into the Ship page (slice 5): its test modal.
    ut = src[src.index("const _SHIP_TEST_MODAL="):src.index("`;", src.index("const _SHIP_TEST_MODAL="))]
    assert 'id="ut-path"' in ut and "capTest()" in ut and "genTests()" in ut


def test_the_trends_hold_what_the_suite_page_drew(src):
    ch = _fn(src, "renderWorkCharts")
    assert "/evolve/board" in ch and "svgBoard(" in ch and "svgTrend(" in ch
    assert "renderWorkCharts()" in _fn(src, "loadCensusRuns")


def test_the_panel_script_parses(src):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    bodies = re.findall(r"<script[^>]*>(.*?)</script>", src, re.S)
    p = subprocess.run([node, "--check", "-"], input="\n;\n".join(bodies),
                       text=True, capture_output=True)
    assert p.returncode == 0, "panel JS does not parse:\n" + (p.stderr or "")[-2000:]
