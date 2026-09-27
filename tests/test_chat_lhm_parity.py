"""
Chat settings parity with the legacy rail, and nothing behind "Full ▸" (design edge, 2026-09-27). Text-level.

  * a session picked in the MENU instance loads in the CHAT instance (the menu has no transcript);
  * the fabric dataset list is bounded - thousands of <option>s in a visible select cost ~4.6 s per layout,
    which is what made the Context settings take ~9 s to open;
  * every LHM menu carries its panes (memory graph, loop graph, DAG, monitor, browser, code, capabilities, loops,
    goals) and its own settings at the top level - the same elements, docked, never copied;
  * one look for every settings surface: the Settings page's cards are the cards the menus show.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "chat", "chat_panel.html")
LIB = _read("vera", "chat", "vera-lhm.js")
LG = _read("vera", "loop_graph_element.js")


def _fn(name):
    body = HTML[HTML.index("function " + name + "("):]
    return body[:body.index("\n  function ", 10)]


def test_a_session_picked_in_the_menu_reaches_the_chat_instance():
    ls = HTML[HTML.index("  async function loadSession(sid){"):]
    ls = ls[:ls.index("await api('/memory/search'")]
    assert "localStorage.setItem('vera_session_id', sid);" in ls, "a reload comes back to the picked session"
    assert "if(_EMBED.only) _lhmPost('session',{ sid });" in ls, "the menu tells the chat instance"
    # and the chat instance already follows a 'session' act from the menu
    assert "else if(m.act==='session'&&a.sid&&a.sid!==SID) loadSession(a.sid);" in HTML


def test_the_fabric_dataset_list_is_bounded_and_filterable():
    assert 'id="ctxFabDSQ" type="search"' in HTML and 'id="ctxFabDSN"' in HTML
    assert "let _fabDSAll=[]; const _FABDS_MAX=120;" in HTML
    fill = _fn("_fabDSFill")
    assert "hits.slice(0,_FABDS_MAX)" in fill, "never more than the cap"
    assert "if(cur&&!shown.some(d=>d.dataset_id===cur))" in fill, "the chosen dataset is always kept"
    assert "sel.value=cur;" in fill
    assert "(b.record_count||0)-(a.record_count||0)" in HTML, "the largest datasets first"
    # the retrieval still reads the same select
    assert "const fabDS=document.getElementById('ctxFabDS')?.value||'';" in HTML


def test_nothing_is_behind_full():
    assert "silentInitial:true, noDeep:true," in HTML
    assert "VeraLHM.deep(true)" not in HTML, "no path in the chat opens the old full pane"
    for pane in ("paneMemGraph", "paneLoopGraph", "paneLoops", "paneGoals", "paneDagGraph", "paneDagHistory", "paneMonitor", "paneBrowser", "paneCode", "rpCaps"):
        assert "{ id:'%s'" % pane in HTML, pane
    assert "_QUICK[k]=function(el, api){ f(el, api); try{ _dockSync(el, k); }" in HTML, "every quick body docks after it draws"
    dock = _fn("_dockSync")
    assert "if(!el||!el.isConnected) return;" in dock, "a detached harvest never takes live panes"
    assert "Object.keys(_dkHome).forEach(id=>{ if(!mine[id]) _dkReturn(id); });" in dock, "what the open menu does not hold goes home"
    assert "document.createComment('lhm-dock '+p.id)" in dock
    # the quick bodies no longer point at Full
    assert "behind Full" not in HTML
    assert "(s.preview||'').toLowerCase().includes(q)).slice(0,300);" in HTML, "every session, not 24"
    assert 'data-sa="refresh"' in HTML and 'data-sa="title"' in HTML, "the history pane's actions"


def test_one_set_of_settings_cards_for_the_page_and_the_menus():
    assert "const _SP_CARDS=[];" in HTML and "function _spHomeAll(){ _SP_CARDS.forEach(c=>c.grid.appendChild(c.card)); }" in HTML
    assert "if(on){ _spBuild(); _spHomeAll(); page.hidden=false;" in HTML, "the page takes every card back when it opens"
    dock = _fn("_dockSync")
    assert "const cardsOk=_railUxDone&&!_spOpen;" in dock, "never before the rail's passes, never out of an open page"
    assert "_SP_CARDS.forEach(c=>{ if(!claimed.has(c.card)&&c.card.parentNode!==c.grid) c.grid.appendChild(c.card); });" in dock
    assert "_railUxDone=true;" in HTML
    # the Settings menu is the full list: the page's own index, a row opens its cards in place
    qs = _fn("_quickSettings")
    assert "_SP_MAP.map(g=>" in qs and 'data-cards="row:' in qs
    assert "{id:'canvas', n:'Session canvas', from:[['rpCfg','Session canvas']]" in HTML, "the canvas section has its row"
    # the page's card look is the menus' look
    assert "#rightRail.lhm-host .lhm-quick .sp-card .opt>input[type=\"checkbox\"]" in HTML
    assert "#settingsPage .sp-card .tb-btn.on{" in HTML and "#rightRail.lhm-host .lhm-quick .sp-card .tb-btn.on{" in HTML


def test_the_loop_graph_wears_the_design_tokens():
    assert "background:var(--lg-bg,var(--bg0,#15140f))" in LG
    assert "border:0;border-radius:var(--r-pill,99px)" in LG
    assert "--lg-bg:transparent" in HTML
