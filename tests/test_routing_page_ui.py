"""Routing on the Models page (looplab-control §9.4 S6, 19 Oct 2026).

Owner, 18 Oct: "better vis over the routing history and why things were routed where they were". The ollama family's
own reads drawn with THE matrix and THE drill; history past the request log's ring waits on B1+ and says so.
"""
import os

import pytest

pytestmark = pytest.mark.critical


@pytest.fixture(scope="module")
def src():
    with open(os.path.join(os.path.dirname(__file__), "..", "vera", "evolve", "evolve_panel.html"), encoding="utf-8") as fh:
        return fh.read()


def _block(src):
    i = src.index("/* ═══ ROUTING — where calls go and why")
    return src[i:src.index("/* ═══ SCHEDULE —", i)]


def test_the_card_is_on_models_and_loads_with_it(src):
    i = src.index('<div class="sec" id="sec-models"')
    sec = src[i:src.index('<div class="sec" id="sec-', i + 10)]
    assert 'id="rt-card"' in sec and '<vera-drill class="olv-drill" id="rt-drill" url></vera-drill>' in sec
    assert "async function loadModels(){\n  routingLoad();" in src


def test_it_reads_the_ollama_familys_own_caps(src):
    js = _block(src)
    for cap in ("ollama.route_stats", "ollama.request_log", "ollama.routing.get"):
        assert "VeraDrill.call('%s'" % cap in js, cap


def test_every_decision_says_why_and_how_good_the_estimate_was(src):
    js = _block(src)
    assert "x.route_reason" in js and "estimate off by" in js and "cpu_spill" in js
    assert "history waits on B1+" in js


def test_a_cell_drills_to_its_models_and_its_rule_and_a_call_to_why_it_went_there(src):
    js = _block(src)
    assert "VeraDrill.registerKind('route-cell'" in js and "VeraDrill.registerKind('route-call'" in js
    assert "id:'rule',label:'The rule'" in js and "id:'why',label:'Why here'" in js
