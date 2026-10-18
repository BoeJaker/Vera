"""Performance on the kit (looplab-control §9.4 S7, 19 Oct 2026): over time, why it moved, quality beside speed."""
import os

import pytest

pytestmark = pytest.mark.critical


@pytest.fixture(scope="module")
def src():
    with open(os.path.join(os.path.dirname(__file__), "..", "vera", "evolve", "evolve_panel.html"), encoding="utf-8") as fh:
        return fh.read()


def _block(src):
    i = src.index("/* ═══ PERFORMANCE — over time, why it moved")
    return src[i:src.index("/* ═══ SCHEDULE —", i)]


def test_the_cards_load_with_performance(src):
    i = src.index('<div class="sec" id="sec-performance"')
    sec = src[i:src.index('<div class="sec" id="sec-', i + 10)]
    for x in ('id="pf-tl-host"', 'id="pf-why"', 'id="pf-qs"', 'id="pf-drill"'):
        assert x in sec, x
    assert "async function loadPerformance(){\n  perfKit();" in src


def test_lanes_read_each_familys_cap_including_the_durable_call_record(src):
    js = _block(src)
    for cap in ("evolve.performance.view", "evolve.unittest.history", "ollama.calls.rollup", "evolve.schedule.events"):
        assert "cap:'%s'" % cap in js, cap
    assert "items:'loop_lab.census.series'" in js


def test_missing_measures_say_so(src):
    i = src.index('<div class="sec" id="sec-performance"')
    sec = src[i:src.index('<div class="sec" id="sec-', i + 10)]
    assert "U1" in sec
    js = _block(src)
    assert "no per-reply quality measure yet (Q1)" in js and "Root-cause per change is R1 / I1" in js
    assert "corpus BROKEN" in js
