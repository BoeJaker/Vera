"""Loop Lab flattening slice 6: the Agents page.

Sessions, Board, Notes, Capacity and Swarm were five pages showing the
agents' work from five stores. They are one section now, sec-agents: ONE
table of agent sessions (evolve.agents.rows, one call) - or, by toggle, of
board items - with the Swarm's live counts as pills above it and the edit
queue's work grid under it. The Board's item form, the Notes editor and the
Capacity seats are modals from the header bar (and the seats pill), NOT
former pages folded under the table. Source-level assertions, the pattern
of test_ship_page_ui.
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


# -- the rail shrank: 13 -> 9, five pages are one ----------------------------
def test_the_five_pages_are_gone_and_agents_is_there(src):
    for sec in ("csessions", "board", "notes", "capacity", "swarm"):
        assert 'data-sec="%s"' % sec not in src, "rail still has %s" % sec
        assert 'id="sec-%s"' % sec not in src, "section %s still in the DOM" % sec
    assert '<div class="sec" id="sec-agents" style="display:none">' in src
    secs = re.findall(r'<div class="sec" id="sec-([a-z]+)"', src)
    assert "agents" in secs and len(secs) == 5, secs   # 9 after slice 6; 5 after Mission control (7)
    rail = re.findall(r'data-sec="([a-z]+)"', src)
    assert rail.index("agents") == rail.index("ship") + 1, "Agents follows Ship"
    nav = src[src.index("VeraPanelBridge.registerNav(["):]
    nav = nav[:nav.index("]);")]
    assert "{id:'agents', label:'Agents'}" in nav
    for old in ("'csessions'", "'board'", "'notes'", "'capacity'", "'swarm'"):
        assert old not in nav, old


def test_every_rail_button_calls_nav_plainly(src):
    """Found 2026-09-10: the Agents button landed as onclick="nav(\\'agents\\')" (a
    raw-string patch kept the backslashes) - a JS syntax error on every click,
    while the deep link still worked, so the probe never saw it."""
    for m in re.finditer(r'<button class="btn(?: on)?" data-sec="([a-z]+)" onclick="([^"]*)"', src):
        assert m.group(2) == "nav('%s')" % m.group(1), m.group(0)[:120]
    assert "\\'" not in "".join(re.findall(r'onclick="([^"]*)"', src[:src.index("</div>", src.index('id="nav"'))]))


def test_the_old_pages_still_route_to_the_table_that_holds_their_data(src):
    nav = _fn(src, "nav")
    assert "agents:agentsPoll," in nav
    for old in ("csessions", "swarm"):
        assert "%s:()=>nav('agents')" % old in nav, old
    assert "board:()=>{nav('agents');setAgentsMode('items')}" in nav, "the Board is the items mode of the table"
    assert "notes:()=>{nav('agents');openNotes()}" in nav and "capacity:()=>{nav('agents');openSeats()}" in nav, \
        "Notes and Capacity are modals over the page"
    for e in ("item", "chat"):
        m = re.search(r"\n  %s:\s*\{[^\n]*page:'([a-z]+)'" % e, src)
        assert m and m.group(1) == "agents", "%s records live on Agents" % e


# -- the page is ONE table: nothing folded, nothing stacked under it ---------
def test_nothing_on_the_page_is_folded(src):
    """The contract: one table, its live strip above, the infographic (the edit
    queue's grid) under it, the controls as modals. A former page in a
    <details> hides it - the thing the redo of slices 5-7 removes."""
    ag = _section(src, "agents")
    assert "<details" not in ag and 'class="fold"' not in ag
    assert ".fold{" not in src and ".fold>" not in src, "the fold CSS went with the last fold"
    cards = re.findall(r'<div class="card"(?: id="([a-z-]+)")?>', ag)
    assert cards == ["agents-card", "board-lanes-card", ""], \
        "the Agents page is its table's card, the board by lane, and the edit queue's grid: %r" % cards
    for gone in ("board-body", "board-new", "board-status", "board-fltrepo", "board-fltproj", "board-fltbranch", "cap-new"):
        assert 'id="%s"' % gone not in src, "the fold-era board view and forms are gone: #%s" % gone


def test_the_board_lanes_are_the_pages_infographic(src):
    """The Board page's lanes view IS an infographic - the work plane by lane,
    a card per item - and it is on the page, visible, from the page's one
    call. (Lost in the 2026-09-12 redo, which deleted it with its fold.)"""
    ag = _section(src, "agents")
    i = ag.index('id="board-lanes-card"')
    assert "display:none" not in ag[i - 60:i + 60] and 'id="board-lanes"' in ag and 'id="board-lanes-all"' in ag
    assert ag.index('id="agents-table"') < i < ag.index('id="editq-grid"'), "under the table, above the edit queue"
    bl = _fn(src, "renderBoardLanes")
    assert "_agItems.filter(" in bl and "/board/items" not in bl, "from the page's one call"
    assert "_LANES.filter(" in bl and "_LANE_COLOR[lane]" in bl
    assert "agFilter('items','lane','" in bl.replace("\\'", "'"), "a lane's head filters the table"
    for on_card in ("openBoardItem(", "boardMove(", "boardDispatch(", "boardEdit(", "eid('branch',it.branch", "eid('pipeline',it.pipeline)",
                    "it.comment_count"):
        assert on_card in bl, on_card
    assert "renderBoardLanes();" in _fn(src, "loadAgents")
    assert "renderBoardLanes()" in _fn(src, "agentsOnChange") and "renderBoardLanes()" in _fn(src, "agFilter"), \
        "the lanes follow the filter row"
    for gone in ("function loadBoardTab(", "function _fillFilter(", "_boardCtxSeeded", "function boardToggleNew(",
                 "function capToggleNew(", "function loadNotes(", "agents-board", "agents-capacity", "agents-notes"):
        assert gone not in src, gone
    for want in ("agents-swarm", "agents-table", "csess-stalled", "editq-grid", "editq-body"):
        assert 'id="%s"' % want in ag, "#%s is on the Agents page" % want
    # The sessions table and the swarm's cross-section are the table now.
    for gone in ("csess-body", "csess-summary", "csess-status", "swarm-body", "swarm-summary", "swarm-status"):
        assert 'id="%s"' % gone not in src, gone


def test_the_controls_are_the_header_bar_and_modals(src):
    """The Board's item form, the Notes editor and the Capacity seats are
    modals: their elements live in the modal templates, not on the page, and
    a header button (or the seats pill, or an old deep-link) opens them."""
    ag = _section(src, "agents")
    for btn in ('onclick="openItemForm()"', 'onclick="openSeats()"', 'onclick="openNotes()"', 'onclick="agentsPoll()"'):
        assert btn in ag, btn
    item = src[src.index("const _AG_ITEM_MODAL=`"):src.index("`;", src.index("const _AG_ITEM_MODAL=`"))]
    for want in ("bn-id", "bn-title", "bn-lane", "bn-labels", "bn-repo", "bn-project", "bn-body", "bn-msg"):
        assert 'id="%s"' % want in item, "the item form's #%s" % want
        assert 'id="%s"' % want not in ag, "#%s is in the modal, not on the page" % want
    assert 'onclick="boardSave()"' in item and 'onclick="closeModal()">Cancel' in item
    seats = src[src.index("const _AG_SEATS_MODAL=`"):src.index("`;", src.index("const _AG_SEATS_MODAL=`"))]
    for want in ("cap-status", "cs-id", "cs-target", "cs-label", "cs-auth", "cap-seats", "cap-ollama"):
        assert 'id="%s"' % want in seats, "the seats modal's #%s" % want
        assert 'id="%s"' % want not in ag, "#%s is in the modal, not on the page" % want
    assert 'onclick="capRegister()"' in seats and 'onclick="loadCapacity()"' in seats
    notes = src[src.index("const _AG_NOTES_MODAL=`"):src.index("`;", src.index("const _AG_NOTES_MODAL=`"))]
    for want in ("notes-scope", "notes-host", "ll-notes-el"):
        assert 'id="%s"' % want in notes, "the notes modal's #%s" % want
        assert 'id="%s"' % want not in ag, "#%s is in the modal, not on the page" % want
    assert '<vera-session-notes id="ll-notes-el" scope="general"' in notes
    assert '<script src="/ui/vera-notes.js"></script>' in ag, "the notes element's script stays on the page"
    # opening
    assert "openModal(_AG_ITEM_MODAL);_boardFillLanes();" in _fn(src, "openItemForm")
    be = _fn(src, "boardEdit")
    assert "openModal(_AG_ITEM_MODAL);_boardFillLanes();" in be and "$('bn-id').value=it.id" in be, "edit is the same form, filled"
    assert "openModal(_AG_SEATS_MODAL);loadCapacity()" in _fn(src, "openSeats")
    on = _fn(src, "openNotes")
    assert "openModal(_AG_NOTES_MODAL)" in on and "notesScope()" in on, "the notes modal reopens on the scope last picked"
    assert "_notesScope=s.value" in _fn(src, "notesScope")
    # saving closes the modal and refreshes THE table (there is no lanes view to reload)
    bs = _fn(src, "boardSave")
    assert "closeModal();agentsRefresh()" in bs and "loadBoardTab" not in bs
    cr = _fn(src, "capRegister")
    assert "loadCapacity();agentsRefresh()" in cr, "a new seat shows in the seats pill too"
    # the seats pill and the row detail's edit button open the modals
    assert "\"openSeats()\"" in _fn(src, "renderAgentsHead")
    assert "event.stopPropagation();boardEdit(" in _fn(src, "_agItemDetail")
    assert "'agents-board'" not in src and "'agents-capacity'" not in src


# -- one table, one call --------------------------------------------------------
def test_the_page_is_one_call_and_two_modes(src):
    la = _fn(src, "loadAgents")
    assert "api('/evolve/agents/rows?mode=both&limit=500'" in la, "one read carries both modes"
    assert "stalled_after_s=" in la, "the Sessions page's stalled-after pick still reaches the watch"
    for fn in ("renderAgentsHead(r)", "_agFillFilters(r)", "renderAgentsTable()"):
        assert fn in la, fn
    assert "x.items=byS[x.id]||[]" in la, "a session's items are joined from the one items list"
    for gone in ("/ide/claude_sessions/watch", "/board/items", "/capacity/status", "/evolve/improve/list", "/evolve/editq"):
        assert gone not in la, "the page reads %s itself" % gone
    assert "claude_sessions/watch?max_sessions=40" not in src
    sm = _fn(src, "setAgentsMode")
    assert "b.dataset.mode===m" in sm and "'ag-lane','ag-plan'" in sm
    rt = _fn(src, "renderAgentsTable")
    assert "rows.map(_agItemRow)" in rt and "rows.map(_agRow)" in rt


def test_a_row_is_a_session_with_the_columns_of_the_five_pages(src):
    row = _fn(src, "_agRow")
    assert "_agAgent(r.agent)" in row and "codex" in _fn(src, "_agAgent") + src[src.index("const _AG_AGENT="):src.index("const _AG_AGENT=") + 200], \
        "whose session is this - the question the Swarm could not answer"
    assert "eid('chat',r.id" in row and "eid('pipeline',p.id)" in row and "eid('branch',br[0]" in row
    assert "_csessActions({claude_session_id:r.id,state:r.watch_state,claims:r.claims})" in row, \
        "resume / release, from the Sessions page"
    assert "r.by_lane" in row and "_LANE_COLOR" in row, "its items by lane"
    assert "toggleAgent(" in row and 'class="ag-detail"' in row
    det = _fn(src, "_agDetail")
    for keep in ("eid('item',i.id)", "eid('pipeline',p.id)", "eid('branch',i.branch", "shipReveal(", "openSessionChat(", "r.reason", "r.claims"):
        assert keep in det, keep
    item = _fn(src, "_agItemRow")
    for keep in ("eid('item',i.id)", "eid('branch',i.branch", "eid('pipeline',i.pipeline)", "boardMove(", "boardDispatch(", "openBoardItem(",
                 "i.session_state"):
        assert keep in item, keep
    idet = _fn(src, "_agItemDetail")
    assert "/board/item?id=" in idet and "it.comments" in idet and "boardComment(" in idet and "boardClaim(" in idet and "boardEdit(" in idet


def test_the_swarm_became_the_strip_above_the_table(src):
    head = _fn(src, "renderAgentsHead")
    for pill in ("'loops'", "'editors'", "'live pipelines'", "'dispatches'", "'active agent sessions'", "'containers'", "'open items'", "'seats'"):
        assert pill in head, pill
    assert "Active Claude sessions" not in src
    assert "agFilter('items','lane','in_progress')" in head and "agFilter('sessions','hide','1')" in head, "a pill filters the table"
    assert "o.nodes" in head and "gate" in head, "the Ollama gate, from the Capacity page"
    for gone in ("function loadSwarm(", "function _swArm(", "function swarmPoll(", "function loadCSessions(", "_CSESS_COLOR"):
        assert gone not in src, gone


# -- pollers and actions follow the page ----------------------------------------
def test_the_poller_is_armed_in_a_finally_and_only_while_live(src):
    poll = _fn(src, "agentsPoll")
    assert "finally" in poll and "_agArm()" in poll and "_agBusy" in poll
    arm = _fn(src, "_agArm")
    assert "_curSec()==='agents'" in arm and "_agMeta.any_live" in arm and "setTimeout(agentsPoll,document.hidden?60000:15000)" in arm
    assert "setTimeout(loadAgents" not in src
    assert "if(s==='agents')agentsPoll();" in src, "the context bar refreshes the page that is open"
    assert "board-fltrepo" not in _fn(src, "_ctxBroadcast"), "no second filter set to mirror the context into"


def test_actions_refresh_the_table(src):
    for fn in ("boardMove", "boardClaim", "boardComment", "boardDispatch", "boardSave", "csessResume", "csessRelease"):
        assert "agentsRefresh()" in _fn(src, fn), fn
    assert "loadCSessions()" not in src
    rf = _fn(src, "agentsRefresh")
    assert "if(_curSec()==='agents')loadAgents()" in rf


def test_the_panel_script_parses(src):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    bodies = re.findall(r"<script[^>]*>(.*?)</script>", src, re.S)
    p = subprocess.run([node, "--check", "-"], input="\n;\n".join(bodies), text=True, capture_output=True)
    assert p.returncode == 0, "panel JS does not parse:\n" + (p.stderr or "")[-2000:]
