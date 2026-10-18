"""Ship: the changes against the system (looplab-control §9.4 S4, 19 Oct 2026).

Owner, 18 Oct: "on the branches page can we have a better view of the changes (git graph) vs other parts of the system
like performance or errors so we can visually see the relation". THE timeline holds what landed above what the system
did, each lane from its own family's cap; a change aligns Compare on itself. Source-level, like the other panel tests.
"""
import os

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")


@pytest.fixture(scope="module")
def src():
    with open(os.path.join(ROOT, "vera", "evolve", "evolve_panel.html"), encoding="utf-8") as fh:
        return fh.read()


def _block(src):
    i = src.index("/* ═══ SHIP — changes against the system")
    return src[i:src.index("/* ═══ SCHEDULE —", i)]


def test_the_card_is_in_ship_on_the_shared_timeline_and_drill(src):
    i = src.index('<div class="sec" id="sec-ship"')
    sec = src[i:src.index('<div class="sec" id="sec-', i + 10)]
    assert 'id="ship-cs-card"' in sec and '<vera-drill class="olv-drill" id="ship-drill" url></vera-drill>' in sec
    assert "async function loadShip(){\n  shipChanges();" in src
    assert "<vera-timeline id=\"ship-tl\"" in _block(src)


def test_changes_sit_above_the_system_each_from_its_own_family(src):
    js = _block(src)
    for cap in ("evolve.schedule.events", "evolve.git.graph", "evolve.unittest.history", "memory.perf.rows", "caps.shortlist.replay"):
        assert "cap:'%s'" % cap in js, cap
    assert "where:{source:'releases',kind:'promotion'}" in js and "where:{source:'results'}" in js
    assert "X1" in src[src.index('id="ship-cs-note"'):src.index('id="ship-cs-note"') + 300], "the errors layer names who brings it"


def test_a_change_opens_its_pipeline_and_aligns_compare(src):
    js = _block(src)
    assert "dr.open('pipeline',it.pipeline_id,{root:true})" in js
    body = js[js.index("async function shipCompare("):]
    assert "VeraCompare.addPin({kind:'item'" in body and "kind:'lane'" in body and "c.setAnchor(n.pin,'1d')" in body
    assert "shipCompare(" in src[src.index("VeraDrill.registerKind('pipeline'"):src.index("VeraDrill.registerKind('release'")]


def test_compare_can_be_aligned_by_its_host():
    js = open(os.path.join(ROOT, "vera", "ui", "vera_compare.js"), encoding="utf-8").read()
    assert "setAnchor(id, win) {" in js
