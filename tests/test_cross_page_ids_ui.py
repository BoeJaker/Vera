"""Every id in Loop Lab is a link to ONE record per entity, and a deep link.

Loop Lab flattening slice 4 (PLAN.md section 3): "Every id anywhere (task,
run, census run, session, pipeline, branch, sandbox, board item) is a deep
link (#<page>?<entity>=<id>) that opens the SAME record modal wherever it
appears." Pinned here: the registry of kinds and their openers, the link
helper and its one delegated click handler, the hash router (on load and on
change, with the address bar kept honest), the two records that did not
exist (suite, branch), and that the tables actually use the link.
"""
import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.critical

PANEL = os.path.join(os.path.dirname(__file__), "..", "vera", "evolve", "evolve_panel.html")


@pytest.fixture(scope="module")
def src():
    with open(PANEL, encoding="utf-8") as fh:
        return fh.read()


def _fn(src, name):
    start = src.index("function " + name + "(")
    return src[start:src.index("\n}\n", start)]


def test_one_registry_names_every_kind_and_its_record(src):
    i = src.index("const ENTITY={")
    reg = src[i:src.index("\n};", i)]
    for kind, opener in (("task", "openTaskHistory"), ("census", "openCensusGoal"), ("run", "openRun"), ("suite", "openSuite"),
                         ("session", "openSession"), ("loop", "openCensusLiveLoop"), ("pipeline", "openPipe"),
                         ("branch", "openBranch"), ("item", "openBoardItem"), ("chat", "openSessionChat"), ("commit", "crossLinkToDispatch")):
        assert re.search(r"\n  %s:\s*\{label:'[^']+',\s+open:.*%s\(" % (kind, opener), reg), (kind, opener)
    assert "openCensusRun(id)" in reg, "a census run without a goal opens the run"


def test_an_id_is_a_link_with_a_deep_link_href_and_one_click_handler(src):
    body = _fn(src, "eid")
    assert "class=\"eid\"" in body and "href=\"'+esc(entityHref(kind,id,extra))" in body
    assert "data-kind" in body and "data-id" in body and "data-extra" in body
    assert "document.addEventListener('click',e=>{" in src and "e.target.closest('a.eid')" in src
    assert "openEntity(a.dataset.kind,a.dataset.id,extra);" in src
    href = _fn(src, "entityHref")
    assert "'#'+((ENTITY[kind]||{}).page||'work')+'?'+q.toString()" in href
    assert "history.replaceState(null,'',location.pathname+location.search+entityHref(kind,id,extra))" in _fn(src, "openEntity")


def test_the_hash_routes_on_load_and_on_change(src):
    r = _fn(src, "routeHash")
    assert "location.hash" in r and "if(page)nav(page);" in r, "the page's loader must run even when it is the page on screen"
    assert "ENTITY[kind].open(id,extra)" in r
    assert "window.addEventListener('hashchange',routeHash);" in src
    assert "if(!routeHash()&&_curSec()==='work')nav('work');" in src, "on load: the deep link wins over the default page"
    nav = _fn(src, "nav")
    assert "history.replaceState(null,'',location.pathname+location.search+'#'+sec)" in nav, "the address bar names the page"


def test_the_records_that_did_not_exist(src):
    s = _fn(src, "openSuite")
    assert "/evolve/suites?limit=" in s and "eid('task',x.task)" in s and "eid('run',x.run_id" in s
    assert "eid('census',s.census_run||s.suite_id" in s, "a census-sourced scoreboard links to its census record"
    b = _fn(src, "openBranch")
    assert "/evolve/pipeline/list" in b and "/evolve/sandbox/list" in b and "eid('pipeline',p.id)" in b
    assert "openBranchSandbox(" in b


@pytest.mark.parametrize("fn,needle", [
    ("_cenRow", "eid('census',s.run_id)"),
    ("_wkRow", "eid(d.kind==='improve'?'session':(d.kind==='census'?'census':d.kind),d.id)"),
    ("_wkRow", "eid('task',d.task)"),
    ("loadCensusGoals", "eid('census',run,esc(g.id),{goal:g.id})"),
    ("loadCensusGoals", "eid('loop',g.session_id"),
    ("renderWorkTasks", "eid('task',t.task_id,'<b>'+esc(t.task_id)+'</b>')"),
    ("loadWorkTaskDetail", "eid(dk,drv.id,null,dk==='census'?{goal:r.goal}:null)"),
    ("loadWorkTaskDetail", "eid('loop',r.session"),
    ("openTaskHistory", "eid(d.kind==='improve'?'session':(d.kind==='census'?'census':(d.kind==='suite'?'suite':'run')),d.id"),
    ("openRunDetail", "eid('task',d.task,esc(d.task))"),
    ("openRunDetail", "eid('loop',d.loop_session"),
    ("loadPipelines", "eid('pipeline',p.id)"),
    ("loadPipelines", "eid('branch',p.branch"),
    ("refreshTestRuns", "eid('task',x.task)"),
    ("loadCSessions", "eid('item',x.id"),
    ("loadCSessions", "eid('chat',s.claude_session_id"),
    ("loadBoardTab", "eid('branch',it.branch"),
    ("loadBoardTab", "eid('pipeline',it.pipeline)"),
    ("openSession", "eid('run',x.run_id,eid('task',x.task))"),
])
def test_the_tables_use_the_link(src, fn, needle):
    assert needle in _fn(src, fn), "%s does not link its ids: %s" % (fn, needle)


def test_the_panel_script_parses(src):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    bodies = re.findall(r"<script[^>]*>(.*?)</script>", src, re.S)
    p = subprocess.run([node, "--check", "-"], input="\n;\n".join(bodies), text=True, capture_output=True)
    assert p.returncode == 0, "panel JS does not parse:\n" + (p.stderr or "")[-2000:]
