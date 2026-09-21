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
    assert 'src="/evolve/schedule/events"' in attrs
    assert 'poll="' in attrs and 'view="' in attrs
    assert "addEventListener('slot-select'" in src and "addEventListener('event-open'" in src


def test_editor_covers_every_kind_and_the_controls(src):
    for k in ("census", "suite", "task", "pipeline", "board", "cap"):
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
