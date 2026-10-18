"""The Loop Lab Releases section on the kit (looplab-control §9.4 S3, 19 Oct 2026).

A release goes through the release lane (evolve.release.submit) - one queue every session submits to - so the page no
longer releases directly; THE timeline shows promotions, releases, lane requests and test runs; THE matrix shows every
pipeline's stages; THE drill opens a pipeline (stages, its gate runs, the change) and a release (what it carried).
Source-level, like the other panel tests.
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
    i = src.index("/* ── Releases on the kit (looplab-control §9.4 S3)")
    return src[i:src.index("/* ═══ MODELS + PERFORMANCE", i)]


def test_a_release_goes_through_the_lane_not_straight_to_prod(src):
    sec = _section(src, "releases")
    assert 'onclick="relRequest()"' in sec and "relReleaseNow" not in src
    js = _block(src)
    assert "post('/evolve/release/submit',{confirm:true," in js and "post('/evolve/release/withdraw',{id})" in js
    assert "'/evolve/release/prod'" not in js


def test_the_lane_card_shows_the_queue_and_the_full_tier_hold(src):
    js = _block(src)
    body = js[js.index("async function relLane("):js.index("async function relRequest(")]
    assert "VeraDrill.call('evolve.release.lane',{})" in body and "ft.held" in body and "relWithdraw(" in body


def test_timeline_matrix_and_drill_are_the_kit(src):
    sec = _section(src, "releases")
    assert '<vera-drill class="olv-drill" id="rel-drill" url></vera-drill>' in sec
    js = _block(src)
    for k in ("'pipeline'", "'release'"):
        assert "VeraDrill.registerKind(" + k in js
    assert "<vera-timeline id=\"rel-tl\">" in js and "<vera-matrix id=\"rel-mxel\">" in js
    for kind in ("promotion", "release", "release-request"):
        assert "kind:'%s'" % kind in js
    assert "<vera-diff pipeline=" in js


def test_cards_and_history_open_the_drill(src):
    assert "onclick=\"relOpen(\\''+esc(c.id)+'\\')\"" in src
    assert "relOpenRelease(\\''+esc(r.id)+'\\')" in src
