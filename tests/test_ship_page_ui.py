"""Loop Lab flattening slice 5: the Ship page.

CI/CD, Review, Sources, Sandbox and Unit tests were five pages listing the
same branches from five stores. They are one section now, sec-ship: a table
of branches (evolve.ship.branches, one call) with the infographics those
pages drew reused below it and their forms and long lists folded up.
Source-level assertions, the pattern of test_work_page_ui: the panel is
static HTML + inline JS, so its shape is pinned by reading the file.
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


# -- the rail shrank: 17 -> 13, five pages are one ---------------------------
def test_the_five_pages_are_gone_and_ship_is_there(src):
    for sec in ("pipelines", "review", "sources", "sandbox", "unittests"):
        assert 'data-sec="%s"' % sec not in src, "rail still has %s" % sec
        assert 'id="sec-%s"' % sec not in src, "section %s still in the DOM" % sec
    assert '<div class="sec" id="sec-ship" style="display:none">' in src
    secs = re.findall(r'<div class="sec" id="sec-([a-z]+)"', src)
    assert "ship" in secs and len(secs) == 6, secs   # 13 after slice 5; 9 after Agents (6); 5 after Mission control (7); 6 with Schedule (2026-09-21)
    rail = re.findall(r'data-sec="([a-z]+)"', src)
    assert rail.index("ship") == rail.index("work") + 1, "Ship follows Work"
    nav = src[src.index("VeraPanelBridge.registerNav(["):]
    nav = nav[:nav.index("]);")]
    assert "{id:'ship', label:'Ship'}" in nav
    for old in ("'pipelines'", "'review'", "'sources'", "'sandbox'", "'unittests'"):
        assert old not in nav, old


def test_the_old_pages_still_route_to_the_table_that_holds_their_data(src):
    nav = _fn(src, "nav")
    assert "ship:loadShip," in nav
    for old in ("pipelines", "review", "sources", "sandbox", "unittests", "logs"):
        assert "%s:()=>nav('ship')" % old in nav, old
    assert "nav('sandbox')" not in src and "nav('pipelines')" not in src
    assert "nav(\\'sandbox\\')" not in src and "nav(\\'pipelines\\')" not in src
    for e in ("pipeline", "branch", "commit"):
        m = re.search(r"\n  %s:\s*\{[^\n]*page:'([a-z]+)'" % e, src)
        assert m and m.group(1) == "ship", "%s records live on Ship" % e


# -- nothing was lost: every element of the five pages is declared -----------
def test_every_element_of_the_five_pages_survived(src):
    ids = set(re.findall(r'id="([a-zA-Z0-9-]+)"', src))
    for want in (
        # CI/CD
        "git-repo", "add-repo-body", "ar-id", "ar-path", "git-graph", "pipe-lanes", "author-map",
        # Sources
        "src-area", "src-profile", "src-review-out", "src-obs-out",
        # Sandbox
        "sb-mode", "sbx-spawn-branch", "sb-port", "sb-status-line", "sbx-capacity",
        "sbx-tools", "sbx-tools-tgt", "sbxtab-term", "sbxtab-files", "sbxtab-diff", "sbxtab-code", "sbxp-term",
        "sbt-out", "sbt-cmd", "sbxp-files", "sbf-tree", "sbf-edit", "sbxp-diff", "sbd-files", "sbd-view", "sbxp-code",
        "sbc-frame",
        # Unit tests
        "ct-cap", "ct-args", "ut-path", "ut-repo-hint", "qt-out", "ut-matrix-status", "ut-filter", "ut-only-gated",
        "ut-matrix-summary", "ut-matrix-body", "ut-trend", "ut-lanes", "ut-regressions", "tg-branch", "tg-body",
    ):
        assert want in ids, "lost #%s in the merge" % want
    ship = _section(src, "ship")
    for want in ("git-graph", "pipe-lanes", "author-map", "ut-lanes", "sbx-tools", "ut-matrix-body", "git-repo", "sb-mode", "ship-mode"):
        assert 'id="%s"' % want in ship, "#%s is not on the Ship page" % want
    # The review queue, the sandbox list, the pipeline list, the git log, the
    # edges list and the logs tail are the table's rows (and their detail) now.
    for gone in ("review-body", "sbx-list", "sbx-count", "pipes-body", "gitlog-body", "edges-list", "logs-body", "git-body"):
        assert 'id="%s"' % gone not in src, gone
    # The controls are modals: sandbox, test, pipeline-from, add-repo.
    for m in ("_SHIP_SANDBOX_MODAL", "_SHIP_TEST_MODAL", "_SHIP_FROM_MODAL", "_SHIP_REPO_MODAL"):
        assert "const %s=`" % m in src, m
    for want, modal in (('id="sbx-spawn-branch"', "_SHIP_SANDBOX_MODAL"), ('id="sb-port"', "_SHIP_SANDBOX_MODAL"), ('id="ct-cap"', "_SHIP_TEST_MODAL"),
                        ('id="tg-body"', "_SHIP_TEST_MODAL"), ('id="src-area"', "_SHIP_FROM_MODAL"), ('id="src-obs-out"', "_SHIP_FROM_MODAL"),
                        ('id="ar-path"', "_SHIP_REPO_MODAL")):
        t = src[src.index("const %s=`" % modal):src.index("`;", src.index("const %s=`" % modal))]
        assert want in t, (want, modal)
        assert want not in ship, want + " is on the page, not in its modal"


def test_the_infographics_are_the_same_elements_visible_under_the_table(src):
    """Nothing on the page is folded: the table, then every infographic the
    five pages drew, as the same elements, visible."""
    ship = _section(src, "ship")
    assert "<details" not in ship and 'class="fold"' not in ship
    assert '<vera-git-graph id="git-graph" repo="vera" limit="150"></vera-git-graph>' in ship
    assert '<vera-branch-pipeline id="pipe-lanes" mode="lanes"></vera-branch-pipeline>' in ship
    assert '<vera-author-map id="author-map" hours="72"></vera-author-map>' in ship
    assert 'id="ut-lanes"' in ship and 'id="ut-trend"' in ship and 'id="ut-regressions"' in ship
    assert 'id="ut-matrix-body"' in ship and 'onclick="loadUnitTests()"' in ship, "the coverage matrix: a card, collected on demand"
    assert "Press collect" in ship and "/evolve/tests/matrix" not in _fn(src, "loadShip"), \
        "evolve.tests.matrix collects the test tree (13 s on prod): never on the page's poll"
    assert 'id="sbx-tools"' in ship, "the connection panel opens next to the table"
    assert ship.index('id="ship-table"') < ship.index("git-graph"), "the table first"


def test_the_controls_are_the_header_bar_and_modals(src):
    ship = _section(src, "ship")
    head = ship[ship.index('<span class="card-t">Ship</span>'):ship.index('id="sb-status-line"')]   # the header and the control bar under it
    for c in ('id="git-repo"', 'id="sb-mode"', 'onclick="openSandboxModal()"', 'onclick="openTestTools()"', 'onclick="openPipelineFrom()"',
              'onclick="openAddRepo()"', 'onclick="sbxReap()"', 'id="ship-mode"'):
        assert c in head, c
    assert "openModal(_SHIP_SANDBOX_MODAL)" in _fn(src, "openSandboxModal")
    assert "openModal(_SHIP_TEST_MODAL)" in _fn(src, "openTestTools") and "genTests()" in _fn(src, "openTestTools")
    assert "openModal(_SHIP_FROM_MODAL)" in _fn(src, "openPipelineFrom") and "loadSources()" in _fn(src, "openPipelineFrom")
    assert "openModal(_SHIP_REPO_MODAL)" in _fn(src, "openAddRepo")
    sm = _fn(src, "setShipMode")
    assert "b.dataset.mode===m" in sm and "renderShipTable()" in sm
    rt = _fn(src, "renderShipTable")
    assert "if(_shipMode==='pipelines'){" in rt and "ps.map(_shipPipeRow)" in rt and "rows.map(_shipRow)" in rt


# -- one table, one call --------------------------------------------------------
def test_the_page_is_one_call_and_reuses_the_pages_renderers(src):
    ls = _fn(src, "loadShip")
    assert "api('/evolve/ship/branches?limit=300')" in ls
    for fn in ("renderSbxStatusLine(r.runner||{})", "renderSbxCapacity(r.capacity)", "renderEdges({edges:r.edges||[]",
               "_utLanes(r.tests||{}", "renderShipHead(r)", "renderShipTable()", "loadGit()"):
        assert fn in ls, fn
    assert "if(r.any_live&&$('ship-follow')&&$('ship-follow').checked&&_curSec()==='ship')window._shipT=setTimeout(loadShip,document.hidden?60000:15000)" in ls
    assert "/evolve/pipeline/list" not in ls and "/evolve/sandbox/list" not in ls and "/evolve/unittest/history" not in ls


def test_a_row_is_a_branch_with_the_columns_of_the_five_pages(src):
    row = _fn(src, "_shipRow")
    assert "eid('branch',r.branch" in row and "eid('pipeline',p.id)" in row and "eid('chat',r.session_id" in row
    assert "pipeActions(p)" in row and "pipeDecision(p.decision)" in row and "ctrlBadge(r.owner)" in row
    assert "sbxConnect(" in row and "openBranchSandbox(" in row
    assert "toggleShipBranch(" in row and 'class="ship-detail"' in row
    assert "t.fewer" in row, "green on fewer tests is said in the row"
    st = _fn(src, "_shipStage")
    assert "r.edge.main_state" in st, "an edge says where it stands against main"
    det = _fn(src, "_shipDetail")
    assert "pending.map(reviewCard)" in det, "a pipeline awaiting a decision is its review card"
    assert "_sbxItemHtml(r.sandbox)" in det, "the sandbox with every control the Sandbox page had"
    assert "_edgeRowHtml(r.edge" in det and "p.superseded" in det and "openBranch(" in det
    assert "openTestTools(" in det, "generate tests for this branch: the test modal, on this branch"
    assert "shipLoadLogs(r.sandbox.name" in det, "the sandbox's own log, in its row"
    filt = _fn(src, "_shipFiltered")
    for f in ("ship-q", "ship-role", "ship-stage", "ship-merged"):
        assert f in filt, f


def test_the_sandbox_and_edge_renderers_became_one_item_functions(src):
    assert "function renderSbxList(" not in src and "function loadReview(" not in src
    assert "function _sbxItemHtml(s){" in src and "function renderSbxStatusLine(r){" in src
    assert "function renderEdges(r){" in src and "function _edgeRowHtml(e,main){" in src
    assert "async function loadSandbox(){return loadShip()}" in src, "every control that refreshed the list refreshes the table"
    item = _fn(src, "_sbxItemHtml")
    for keep in ("sbxConnect(", "copySandboxSsh(", "sandboxUpNamed(", "sbxPin(", "sandboxDownNamed(", "sbxSafety(", "sbxPlan(",
                 "s.owner||'unknown'", "s.head_commit", "s.merged_to_bleeding_edge", "s.git_worktree.state"):
        assert keep in item, keep
    edge = _fn(src, "_edgeRowHtml")
    for keep in ("edgeEnsure(", "edgeMirror(", "edgeRelease(", "e.main_state"):
        assert keep in edge, keep
    for keep in ("function reviewCard(", "function decidedCard(", "function wsReviewCard(", "function toggleReviewDiff(",
                 "function reviewVerdict("):
        assert keep in src, keep


# -- pollers and actions follow the page ----------------------------------------
def test_one_poller_follows_the_ship_page(src):
    assert "_curSec()==='pipelines'" not in src and "_curSec()==='review'" not in src and "_curSec()==='sandbox'" not in src
    assert "_pipeT" not in src and "_logsTimer" not in src and "function loadPipelines(" not in src and "function loadGitLog(" not in src
    assert "if(t.startsWith('evolve.pipeline.')){shipRefresh();pollPipeLive(ev.id)}" in src
    assert "if(t.startsWith('ide.workspace.changes.')||t==='evolve.sandbox.review'){shipRefresh()}" in src
    assert "else if(s==='ship')loadShip();" in src
    assert "function shipRefresh(){if(_curSec()==='ship')loadShip()}" in src


def test_actions_refresh_the_table_not_the_pages_that_are_gone(src):
    assert "loadReview()" not in src
    for fn in ("pipePromote", "pipeRollback", "reviewVerdict", "wsAccept", "wsReject"):
        assert "shipRefresh()" in _fn(src, fn), fn


def test_a_branch_record_leads_back_to_its_row(src):
    b = _fn(src, "openBranch")
    assert "shipReveal(" in b and "nav(\\'sandbox\\')" not in b and "CI / CD" not in b
    rv = _fn(src, "shipReveal")
    assert "_shipOpen[branch]=true" in rv and "nav('ship')" in rv


def test_the_panel_script_parses(src):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    bodies = re.findall(r"<script[^>]*>(.*?)</script>", src, re.S)
    p = subprocess.run([node, "--check", "-"], input="\n;\n".join(bodies), text=True, capture_output=True)
    assert p.returncode == 0, "panel JS does not parse:\n" + (p.stderr or "")[-2000:]
