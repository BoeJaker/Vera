"""Compare: the Loop Lab's compare workspace (looplab-control §9 K4, 18 Oct 2026).

Owner: "if i want to compare one thing next to another it should be possible" and "it should be easy to composite
elements along a timeline as part of the compare system". The section hosts THE compare element (/ui/vera-compare.js),
which draws with THE timeline; pins come from the Overview's drill panel and carry their lane RECORD.

Source-level, like the other panel tests.
"""
import os

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def src():
    return _read("vera", "evolve", "evolve_panel.html")


def _section(src, sec):
    i = src.index('<div class="sec" id="sec-%s"' % sec)
    return src[i:src.index("\n</div>\n", i)]


def test_compare_is_a_section_with_the_shared_element(src):
    sec = _section(src, "compare")
    assert '<vera-compare id="cmp" mode="stack"></vera-compare>' in sec
    assert "compare:loadComparePage," in src
    assert "{id:'compare', label:'Compare'," in src


def test_pins_come_from_the_drill_and_carry_their_lane_record(src):
    i = src.index("async function olvPin(")
    body = src[i:src.index("\nfunction _olvPinBar(", i)]
    assert "const spec=_olvSpec(L);" in body and "lane:spec" in body, "a pin re-reads its own source after a reload"
    assert "VeraCompare.addPin(p)" in body
    assert src.count("_olvPinBar(") >= 3, "events and test runs both offer pinning"


def test_the_element_draws_with_the_shared_timeline_and_drill():
    js = _read("vera", "ui", "vera_compare.js")
    assert "V.render({ width: w, range: range, lanes: model, overlay: ov })" in js
    assert "<vera-drill data-vc-col=" in js
    assert "<svg" not in js, "no chart code of its own"


def test_the_route_serves_it():
    libs = _read("vera", "ui", "libs.py")
    assert '@APP.get("/ui/vera-compare.js", include_in_schema=False)' in libs and '_script("vera_compare.js")' in libs
