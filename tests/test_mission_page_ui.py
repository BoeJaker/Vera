"""Loop Lab flattening slice 7: Mission control - the fifth and last page.

Master, Activity and Errors were three pages over the same happenings; they
are one section now, sec-mission: ONE table of events (evolve.mission.events,
one call), the live strip above it, what is in flight shown in full above the
table only while it runs, the compose form as a modal, the error flow as the
page's infographic. NOTHING FOLDED: a former page hidden in a <details> is a
page inside a page, the opposite of flattening (learned 2026-09-12). Loop Lab
is five pages: Work, Ship, Agents, Mission control, Settings.
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


def test_nothing_on_the_page_is_folded(src):
    """The rule for every flattened page: one table, its infographics visible,
    its controls as modals. A former page in a <details> hides it."""
    ms = _section(src, "mission")
    assert 'class="fold"' not in ms and "<details" not in ms
    assert 'id="mission-table"' in ms and 'id="err-flow"' in ms
    assert 'id="mc-live"' in ms and 'style="display:none' in ms[ms.index('id="mc-live"') - 40:ms.index('id="mc-live"') + 60] or \
        'id="mc-live" style="display:none' in ms, "the live panel is hidden until something runs"


def test_the_old_pages_still_route_to_the_modal_the_panel_or_the_filter(src):
    nav = _fn(src, "nav")
    assert "mission:missionPoll," in nav
    for old, target in (("test", "mcOpen('test')"), ("watch", "mcOpen('watch')"), ("errors", "mcOpen('errors')"),
                        ("master", "nav('mission')"), ("activity", "nav('mission')")):
        assert "%s:()=>%s" % (old, target) in nav, old
    mo = _fn(src, "mcOpen")
    assert "nav('mission')" in mo
    assert "if(_testRunId||_mcLiveNow.run)mcRevealLive('run');else openRunCompose()" in mo, "test: the run in flight, else the compose modal"
    assert "mcRevealLive('improve')" in mo and "mcFilter('error')" in mo
    rv = _fn(src, "mcRevealLive")
    assert "r.style.display=which==='run'?'':'none'" in rv and "if(which==='run')loadTest();else loadWatch();" in rv


# -- nothing was lost ----------------------------------------------------------------
def test_every_element_of_the_five_pages_survived(src):
    ids = set(re.findall(r'id="([a-zA-Z0-9-]+)"', src))
    for want in (
        # Run once (Test)
        "active-run-card", "arh-task", "arh-goal", "sbx-strip", "ollama-map-card", "ollama-map", "test-pipeline-card", "test-pipeline",
        "test-author-card", "test-author-map", "tc-kind", "tc-run", "tc-cat", "tc-target", "tc-info", "tc-goal", "tc-cap", "tc-args",
        "tc-task", "tc-tag", "wf-status", "wf-fly", "wf-diagram", "evolve-alo", "test-eval", "test-result", "test-suite-card",
        "test-suite-log", "test-activity", "task-matrix",
        # Improve · live (Watch)
        "watch-sub", "watch-sel", "phase-steps", "watch-status", "evolve-alo-watch", "watch-eval", "watch-synth",
        "editq-model-lbl", "editq-worker", "editq-grid", "editq-body",
        # Errors
        "err-autosync", "err-flow",
    ):
        assert want in ids, "lost #%s in the merge" % want
    ms = _section(src, "mission")
    for want in ("mission-table", "mission-live", "mc-live", "mc-live-run", "mc-live-improve", "evolve-alo", "evolve-alo-watch",
                 "err-flow", "active-run-card", "wf-diagram", "test-eval", "test-result", "watch-sel", "phase-steps"):
        assert 'id="%s"' % want in ms, "#%s is not on Mission control" % want
    # The tasks' infographics live on Work; the editors' work grid on Agents.
    wk, ag = _section(src, "work"), _section(src, "agents")
    assert 'id="task-matrix"' in wk and 'id="test-activity"' in wk and "taskmatrix:opencell" in wk
    assert 'id="editq-grid"' in ag and 'id="editq-body"' in ag and 'id="editq-worker"' in ag
    # Master's cards, the audit table and the recent-runs list are the table +
    # strip now (their rows are its rows); the infographics are NOT - see
    # test_the_infographics_are_visible_on_the_page.
    for gone in ("m-promo", "m-board", "m-gates", "m-activity", "master-auto", "audit-body", "aud-filter", "test-runs"):
        assert 'id="%s"' % gone not in src, gone
    for gone in ("function loadMaster(", "function _masterAutoToggle(", "_masterTimer", "function loadAudit(", "function loadErrors("):
        assert gone not in src, gone
    for keep in ("function pollAutonomous(", "function autonomousRelease(", "function renderErrorFlow(", "function svgErrorFlow(",
                 "function loadTest(", "function loadTestForm(", "function openRunCompose(", "function loadWatch(", "function loadEditq(",
                 "function refreshActiveRun("):
        assert keep in src, keep


def test_the_infographics_are_visible_on_the_page(src):
    """The infographics of the pages Mission control absorbed are ON the page,
    always, from the same call - not folded, not only-while-live, not gone:
    the Errors page's flow AND its kanban lanes, the Watch page's fleet, the
    Overview's activity chart, the Test page's Ollama routing map. (Lost in
    the 2026-09-12 redo, which cut the folds out wholesale.)"""
    ms = _section(src, "mission")
    live = ms[ms.index('id="mc-live"'):ms.index('id="mc-live-improve"')]
    for host in ("err-flow", "err-board", "fleet-body", "mc-activity", "ollama-map-card", "ollama-map"):
        assert 'id="%s"' % host in ms, "#%s is not on Mission control" % host
        assert 'id="%s"' % host not in live, "#%s must not depend on a run being live" % host
        i = ms.index('id="%s"' % host)
        assert "display:none" not in ms[i - 80:i + 40], "#%s is hidden" % host
    assert "$('ollama-map-card').style.display" not in src, "the routing map is not toggled by the run"
    # the error lanes: the same rows, a card each with its actions, lane head -> the table
    el = _fn(src, "renderErrorLanes")
    assert "(_mcRows||[]).filter(x=>x.kind==='error').map(x=>x.raw||{})" in el and "ERR_LANES.map(" in el
    assert "errCard(it,_errJustEntered.has(it.id),staggerN++)" in el and "mcFilter('error',''+st+'')" in el.replace("\\'", "'")
    ec = _fn(src, "errCard")
    for a in ("errSuggest(", "errApprove(", "errDismiss(", "class=\"err-card s-'+esc(it.state)+(isEnter?' enter':'')"):
        assert a in ec, a
    assert "if(errState&&(r.kind!=='error'||r.state!==errState))return false;" in _fn(src, "_mcFiltered")
    assert 'id="mc-errstate"' in ms
    # the fleet: loops with a bar, editors; from the call
    fl = _fn(src, "renderFleet")
    assert "f.loops||[]" in fl and "f.editors||[]" in fl and 'class="bar"' in fl and "width:'+(s.pct||0)+'%" in fl
    assert "watchSession(" in fl and "openEditq(" in fl
    lm = _fn(src, "loadMission")
    assert "renderErrorLanes();" in lm and "renderFleet(r.fleet||{});" in lm and "svgActivity(r.activity||[])" in lm
    for gone in ("/evolve/improve/list", "/evolve/editq?", "/evolve/activity"):
        assert gone not in lm, "the page reads %s itself" % gone
    assert "function svgActivity(" in src


def test_the_theatres_own_lookups_follow_them(src):
    assert "function _testAlo(){return document.querySelector('#mc-live-run vera-agent-loop-output')}" in src
    assert "function _watchAlo(){return document.querySelector('#mc-live-improve vera-agent-loop-output')}" in src
    assert "alo.closest('#mc-live-run')" in src and "'#sec-test'" not in src and "'#sec-watch'" not in src and "'#mc-test'" not in src


def test_the_compose_form_is_a_modal_and_the_live_panel_shows_what_runs(src):
    ms = _section(src, "mission")
    assert 'onclick="openRunCompose()"' in ms and 'onclick="openImproveForm()"' in ms
    assert 'id="tc-kind"' not in ms, "the compose form is not on the page; it is the modal"
    form = src[src.index("const _MC_RUN_FORM="):src.index("`;", src.index("const _MC_RUN_FORM="))]
    for want in ('id="tc-kind"', 'id="tc-goal"', 'id="tc-cat"', 'id="tc-task"', 'id="tc-tag"', 'onclick="tcRun()"'):
        assert want in form, want
    oc = _fn(src, "openRunCompose")
    assert "openModal(_MC_RUN_FORM)" in oc and "loadTestForm()" in oc
    live = _fn(src, "renderMissionLive")
    assert "_mcLiveNow={run:!!(run.live||run.running),improve:!!lv.improve}" in live
    assert "if(_mcLiveNow.run&&!_mcWasLive.run)mcRevealLive('run')" in live, "revealed once per transition to live"
    assert "else if(!_mcLiveNow.run&&!_mcLiveNow.improve&&!_testRunId)mcHideLive()" in live, "hidden when nothing runs"
    for e in ("sync errors", "clear errors", 'id="err-autosync"'):
        assert e in ms, e


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
    assert "nav('ship')" in live and "nav('work')" in live
    assert "mcRevealLive('run')" in live and "mcRevealLive('improve')" in live


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
    assert "_fleetT" not in src, "the fleet rides the page's one poll, it does not poll itself"
    assert "if(t.startsWith('evolve.errors.')){missionRefresh()}" in src
    assert "if(t.startsWith('evolve.editq.')){watchOnEditq(ev);if(_curSec()==='agents')loadEditq()}" in src
    assert "if(t.startsWith('evolve.')&&_curSec()==='mission')missionRefresh();" in src
    poll = _fn(src, "missionPoll")
    assert "finally" in poll and "_mcArm()" in poll and "_mcBusy" in poll
    arm = _fn(src, "_mcArm")
    assert "_curSec()==='mission'" in arm and "_mcMeta.any_live" in arm and "setTimeout(missionPoll,document.hidden?60000:15000)" in arm
    rf = _fn(src, "missionRefresh")
    assert "if(_curSec()!=='mission')return" in rf and "setTimeout(missionPoll,4000)" in rf, "debounced"
    assert "setInterval(" not in src[src.index("function renderMissionLive"):src.index("async function pollAutonomous")]


def test_the_panel_script_parses(src):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    bodies = re.findall(r"<script[^>]*>(.*?)</script>", src, re.S)
    p = subprocess.run([node, "--check", "-"], input="\n;\n".join(bodies), text=True, capture_output=True)
    assert p.returncode == 0, "panel JS does not parse:\n" + (p.stderr or "")[-2000:]
