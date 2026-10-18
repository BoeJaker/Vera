"""The Overview: every Loop Lab operation on one timeline (looplab-control §9 S1, owner O1, 18 Oct 2026).

"the overview should give you a full view of all loop lab operations in a single pane ... all in one combined
timeline". It is THE timeline (/ui/vera-timeline.js, K1) with lanes registered as records - no timeline of its own -
and the tests lane is optional ("be able to see tests in the overview/schedule too (optionally)").

Source-level, like the other panel tests: what these pin is the shape, not pixels.
"""
import os
import re

import pytest

pytestmark = pytest.mark.critical

PANEL = os.path.join(os.path.dirname(__file__), "..", "vera", "evolve", "evolve_panel.html")


@pytest.fixture(scope="module")
def src():
    with open(PANEL, encoding="utf-8") as fh:
        return fh.read()


def _section(src, sec):
    i = src.index('<div class="sec" id="sec-%s"' % sec)
    return src[i:src.index("\n</div>\n", i)]


def _block(src):
    i = src.index("/* ═══ OVERVIEW — every Loop Lab operation on one timeline")
    return src[i:src.index("/* ═══ SCHEDULE —", i)]


def test_the_overview_uses_the_shared_timeline_not_its_own(src):
    js = _block(src)
    assert "/ui/vera-timeline.js" in js and "VeraTimeline.registerLane(_olvSpec(L))" in js
    assert "<vera-timeline id=\"olv-tl\">" in js
    assert "<svg" not in js, "the timeline draws; the page does not"


def test_one_read_of_the_schedule_feeds_the_lanes_by_source(src):
    js = _block(src)
    lanes = re.findall(r"\{lane:'(ll\.[a-z]+)'.*?where:'([a-z-]+)'", js)
    assert dict(lanes) == {"ll.windows": "loop-lab", "ll.runs": "loop-lab-run", "ll.results": "results",
                           "ll.releases": "releases", "ll.agents": "agents-work"}
    assert "source:{cap:'evolve.schedule.events',args:_olvArgs()}" in js and "where:{source:L.where}" in js


def test_the_tests_lane_is_there_and_off_until_asked(src):
    js = _block(src)
    assert re.search(r"\{lane:'ll\.tests'[^}]*on:0", js)
    assert "cap:'evolve.unittest.history'" in js


def test_controls_sit_on_the_page_and_the_drill_links_to_the_owner(src):
    sec = _section(src, "overview")
    assert 'id="olv-range"' in sec and 'id="olv-lanes"' in sec and 'id="olv-drill"' in sec
    js = _block(src)
    assert "addEventListener('timeline:select',e=>olvDrill(e.detail))" in js
    assert "addEventListener('timeline:brush'" in js
    assert "open in '+esc(name)" in js


def test_the_overview_has_its_own_loader_beside_the_old_suite_one(src):
    assert src.count("async function loadOverviewPage(") == 1
    assert src.count("async function loadOverview(") == 1, "the suite loader keeps its name"
    assert "overview:loadOverviewPage," in src
