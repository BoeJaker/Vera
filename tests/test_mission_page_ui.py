"""Loop Lab flattening slice 7: Mission control - the fifth and last page.

Master, Activity and Errors were three pages over the same happenings; they
are one section now, sec-mission: a table of events (evolve.mission.events,
one call) with the live strip above it, and the Run-once / Improve-live
theatres folded under it, verbatim. Loop Lab is five pages: Work, Ship,
Agents, Mission control, Settings. Source-level assertions, the pattern of
test_ship_page_ui.
"""
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "vera")
PANEL = os.path.join(ROOT, "evolve", "evolve_panel.html")


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


# -- five pages ------------------------------------------------------------------
def test_loop_lab_is_five_pages(src):
    secs = re.findall(r'<div class="sec" id="sec-([a-z]+)"', src)
    assert sorted(secs) == ["agents", "mission", "settings", "ship", "work"], secs
    rail = re.findall(r'data-sec="([a-z]+)"', src)
    assert rail == ["work", "ship", "agents", "mission", "settings"], rail
    for sec in ("master", "test", "watch", "errors", "activity"):
        assert 'id="sec-%s"' % sec not in src, "section %s still in the DOM" % sec
    assert 'class="rail-grp"' not in src[src.index('id="nav"'):src.index("</div>", src.index('id="nav"'))], "five pages need no groups"
    nav = src[src.index("VeraPanelBridge.registerNav(["):]
    nav = nav[:nav.index("]);")]
    assert "{id:'mission', label:'Mission control'}" in nav
    for old in ("'test'", "'watch'", "'errors'", "'activity'", "'master'"):
        assert old not in nav, old
    for m in re.finditer(r'<button class="btn(?: on)?" data-sec="([a-z]+)" onclick="([^"]*)"', src):
        assert m.group(2) == "nav('%s')" % m.group(1), m.group(0)[:100]


def test_the_old_pages_still_route_and_the_theatres_open_as_folds(src):
    nav = _fn(src, "nav")
    assert "mission:missionPoll," in nav
    for old, target in (("test", "mcOpen('test')"), ("watch", "mcOpen('watch')"), ("errors", "mcOpen('errors')"),
                        ("master", "nav('mission')"), ("activity", "nav('mission')")):
        assert "%s:()=>%s" % (old, target) in nav, old
    mo = _fn(src, "mcOpen")
    assert "nav('mission')" in mo and "d.open=true" in mo and "{test:loadTest,watch:loadWatch,errors:loadErrors}" in mo
    assert "scrollIntoView" in mo


# -- nothing was lost ----------------------------------------------------------------
def test_every_element_of_the_five_pages_survived(src):
    ids = set(re.findall(r'id="([a-zA-Z0-9-]+)"', src))
    for want in (
        # Run once (Test)
        "active-run-card", "arh-task", "arh-goal", "sbx-strip", "ollama-map-card", "ollama-map", "test-pipeline-card", "test-pipeline",
        "test-author-card", "test-author-map", "tc-kind", "tc-run", "tc-cat", "tc-target", "tc-info", "tc-goal", "tc-cap", "tc-args",
        "tc-task", "tc-tag", "wf-status", "wf-fly", "wf-diagram", "evolve-alo", "test-eval", "test-result", "test-suite-card",
        "test-suite-log", "test-activity", "task-matrix", "test-runs",
        # Improve · live (Watch)
        "watch-sub", "watch-sel", "phase-steps", "watch-status", "evolve-alo-watch", "watch-eval", "watch-synth", "fleet-body",
        "editq-model-lbl", "editq-worker", "editq-grid", "editq-body",
        # Errors
        "err-autosync", "err-flow", "err-board",
    ):
        assert want in ids, "lost #%s in the merge" % want
    ms = _section(src, "mission")
    for want in ("mission-table", "mission-live", "mc-test", "mc-watch", "mc-errors", "evolve-alo", "evolve-alo-watch", "err-board",
                 "task-matrix", "test-activity"):
        assert 'id="%s"' % want in ms, "#%s is not on Mission control" % want
    # Master's cards and the audit table are the table + strip now.
    for gone in ("m-promo", "m-board", "m-gates", "m-activity", "master-auto", "audit-body", "aud-filter"):
        assert 'id="%s"' % gone not in src, gone
    for gone in ("function loadMaster(", "function _masterAutoToggle(", "_masterTimer", "function loadAudit("):
        assert gone not in src, gone
    for keep in ("function pollAutonomous(", "function autonomousRelease(", "function loadErrors(", "function errCard(", "function svgErrorFlow(",
                 "function loadTest(", "function loadWatch(", "function loadFleet(", "function loadEditq(", "function refreshActiveRun("):
        assert keep in src, keep


def test_the_theatres_own_lookups_follow_them(src):
    assert "function _testAlo(){return document.querySelector('#mc-test vera-agent-loop-output')}" in src
    assert "function _watchAlo(){return document.querySelector('#mc-watch vera-agent-loop-output')}" in src
    assert "alo.closest('#mc-test')" in src and "'#sec-test'" not in src and "'#sec-watch'" not in src
    ms = _section(src, "mission")
    assert 'id="mc-test" ontoggle="if(this.open)loadTest()"' in ms
    assert 'id="mc-watch" ontoggle="if(this.open)loadWatch()"' in ms
    assert 'id="mc-errors" ontoggle="if(this.open)loadErrors()"' in ms


# -- one table, one call --------------------------------------------------------------
def test_the_page_is_one_call_with_the_strip_in_it(src):
    lm = _fn(src, "loadMission")
    assert "api('/evolve/mission/events?limit=400')" in lm
    for fn in ("renderMissionLive(r)", "renderMissionTable()", "auto-banner"):
        assert fn in lm, fn
    for gone in ("/evolve/audit", "/evolve/errors", "/evolve/pipeline/list", "/board/items", "/autonomous/status", "/evolve/work/live"):
        assert gone not in lm, "the page reads %s itself" % gone
    live = _fn(src, "renderMissionLive")
    for pill in ("'needs promotion'", "'active items'", "'errors'", "'last gate'", "'live pipelines'", "'census '", "'improve · live'", "'run'", "'main LOCKED'"):
        assert pill in live, pill
    assert "mcOpen('watch')" in live and "mcOpen('test')" in live and "nav('ship')" in live and "nav('work')" in live
    assert "_mcWasLive[w]" in live and "d.open=true" in live, "a theatre opens by itself when its thing goes live"
    assert "live-dot" in live


def test_a_row_is_an_event_with_its_record_and_actions(src):
    row = _fn(src, "_mcRow")
    assert "eid(r.ref.kind,r.ref.id" in row, "the record an event names is a link"
    for a in ("errSuggest(", "errApprove(", "errDismiss("):
        assert a in row, a + " from the Errors page, in the row"
    assert "r.remediation?'apply safe fix':'approve → commit'" in row
    assert "toggleMission(" in row and 'class="mc-detail"' in row
    det = _mcDetail = _fn(src, "_mcDetail")
    assert "r.suggestion" in det and "r.remediation_result" in det and "r.passed" in det and "JSON.stringify(raw" in det
    f = _fn(src, "_mcFiltered")
    for k in ("mc-q", "mc-kind", "mc-family", "mc-problems", "mc-hide-exec"):
        assert k in f, k


# -- pollers and bus hooks follow the page and its folds ----------------------------
def test_the_pollers_follow_the_page_and_its_folds(src):
    for gone in ("_curSec()==='test'", "_curSec()==='watch'", "_curSec()==='errors'", "_curSec()==='master'", "_curSec()==='activity'"):
        assert gone not in src, gone
    assert "_curSec()==='mission'&&$('mc-watch')&&$('mc-watch').open){clearTimeout(window._fleetT)" in src
    assert "if(t.startsWith('evolve.errors.')){if(_curSec()==='mission'){missionRefresh();if($('mc-errors')&&$('mc-errors').open)loadErrors()}}" in src
    assert "if(t.startsWith('evolve.')&&_curSec()==='mission')missionRefresh();" in src
    poll = _fn(src, "missionPoll")
    assert "finally" in poll and "_mcArm()" in poll and "_mcBusy" in poll
    arm = _fn(src, "_mcArm")
    assert "_curSec()==='mission'" in arm and "_mcMeta.any_live" in arm and "setTimeout(missionPoll,6000)" in arm
    rf = _fn(src, "missionRefresh")
    assert "if(_curSec()!=='mission')return" in rf and "setTimeout(missionPoll,1500)" in rf, "debounced"
    assert "setInterval(" not in src[src.index("function renderMissionLive"):src.index("async function pollAutonomous")]


def test_the_panel_script_parses(src):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    bodies = re.findall(r"<script[^>]*>(.*?)</script>", src, re.S)
    p = subprocess.run([node, "--check", "-"], input="\n;\n".join(bodies), text=True, capture_output=True)
    assert p.returncode == 0, "panel JS does not parse:\n" + (p.stderr or "")[-2000:]
