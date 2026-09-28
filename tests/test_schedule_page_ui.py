"""The Loop Lab Schedule page: a <vera-calendar> fed by the scheduler's
events, the editor for every kind, the scheduler switch and the tick.

Pins the wiring by regex over the HTML source (the way the other page tests
do), so a refactor that drops the calendar feed, the slot-select hook or
the seed button fails here before a person notices an empty page.
"""
import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL = os.path.join(HERE, "..", "vera", "evolve", "evolve_panel.html")
ELEMENT = os.path.join(HERE, "..", "vera", "calendar_element.js")


@pytest.fixture(scope="module")
def src():
    with open(PANEL, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def element():
    with open(ELEMENT, encoding="utf-8") as fh:
        return fh.read()


def test_schedule_page_exists_and_is_wired(src):
    assert '<div class="sec" id="sec-schedule"' in src
    assert 'data-sec="schedule" onclick="nav(\'schedule\')"' in src
    assert "schedule:loadSchedule," in src
    assert "{id:'schedule', label:'Schedule'}" in src
    assert '<script src="/ui/elements/calendar.js"></script>' in src


def test_calendar_is_fed_by_the_scheduler(src):
    m = re.search(r'<vera-calendar id="sch-cal"([^>]*)>', src)
    assert m, "no <vera-calendar> on the Schedule page"
    attrs = m.group(1)
    assert 'src="/evolve/schedule/events' in attrs
    assert 'poll="' in attrs and 'view="' in attrs
    # it takes the page's height (owner, 2026-09-28: "the calendar in the scheduler doesnt take up the entire UI")
    assert " fill" in attrs
    assert "addEventListener('slot-select'" in src and "addEventListener('event-open'" in src


def test_editor_covers_every_kind_and_the_controls(src):
    for k in ("census", "suite", "task", "pipeline", "board", "loop", "tests", "cap"):
        assert "%s:" % k in src[src.index("const SCH_KIND_LABEL"):src.index("};", src.index("const SCH_KIND_LABEL"))], k
    for fn in ("schOpen(", "schSave(", "schDelete(", "schToggle(", "schRunNow(", "schTick(", "schSeed(", "schCfgSave("):
        assert "async function " + fn in src or "function " + fn in src, fn
    for path in ("/evolve/schedule/list", "/evolve/schedule/upsert", "/evolve/schedule/delete",
                 "/evolve/schedule/enable", "/evolve/schedule/run_now", "/evolve/schedule/tick",
                 "/evolve/schedule/seed_weekday_census", "/evolve/schedule/config/set", "/evolve/schedule/history"):
        assert path in src, path
    assert 'id="sch-enabled"' in src and 'id="sch-list"' in src and 'id="sch-runs"' in src


def test_element_is_reusable(element):
    assert "customElements.define('vera-calendar'" in element
    for name in ("slot-select", "event-open", "range-change"):
        assert "'%s'" % name in element, name
    assert "static get observedAttributes()" in element and "'src'" in element
    assert "set events(" in element and "refresh()" in element
    for v in ("'month'", "'week'", "'day'"):
        assert v in element


def test_results_mode_is_wired(src):
    assert 'id="sch-gran"' in src
    assert "function schMode(" in src and "mode='+mode+'&granularity='+gran" in src
    assert "function schResultModal(" in src and "ev.source==='results'" in src


def test_layers_replace_the_mode_select(src):
    """The calendar's layers - windows, runs, results, board items, Vera's calendar - each switched on its own,
    counted, and drawn through the element's filter (owner, 2026-09-28: "easy to see the results layer")."""
    assert 'id="sch-mode"' not in src and 'id="sch-layers"' in src
    blk = src[src.index("const SCH_LAYERS"):src.index("];", src.index("const SCH_LAYERS"))]
    for k in ("windows", "runs", "results", "board", "calendar"):
        assert "k:'%s'" % k in blk, k
    assert "cal.filter=" in src and "addEventListener('events-loaded'" in src
    assert "include_calendar=1" in src
    # results are drawn solid, windows as bands behind them
    assert "ev.display='background'" in src and "ev.display='solid'" in src


def test_board_items_tie_both_ways(src):
    assert 'id="sch-board"' in src and "board_id:" in src          # the editor's link on every kind
    assert "function schForBoard(" in src and "function schBoardLine(" in src
    assert "/evolve/schedule/list?board_id=" in src
    drawer = src[src.index("async function openBoardItem("):src.index("async function boardPostComment(")]
    assert "schForBoard(" in drawer and "schBoardLine(it.id)" in drawer and 'id="bd-scheds"' in drawer


def test_element_fills_filters_and_lays_out_overlaps(element):
    assert ":host([fill])" in element and "ResizeObserver" in element
    assert "set filter(" in element and "_shown()" in element
    assert "'events-loaded'" in element
    assert "display === 'background'" in element and "display === 'solid'" in element
