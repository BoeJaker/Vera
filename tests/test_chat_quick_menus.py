"""
The quick menus as the Canvas board draws them (design landing, step 2): every menu's body is a list of widgets —
Context: the context galaxy (the context_graph widget form) + the budget bar + "In this prompt" rows + the CTA to
the full graph; Sessions · Activity · Loop · Workspace · Sandbox · Ops glance · Settings compact bodies; the old pane
of every menu stays behind "Full ▸" in the same panel. Text-level.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "chat", "chat_panel.html")
LIB = _read("vera", "chat", "vera-lhm.js")


def test_the_library_carries_a_quick_body_and_the_full_pane_swap():
    assert "var _quick = null;" in LIB and "_quick = _el('div', 'lhm-quick');" in LIB
    assert "function _renderQuick(){" in LIB and "m.quick(_quick, { menu:m, render:render, pick:pick, deep:deep });" in LIB
    assert "_host.classList.toggle('lhm-quickmode', on);" in LIB
    assert "function deep(on){" in LIB and "deep: deep," in LIB
    assert "var dp = _el('button', 'lhm-deep', 'Full ▸');" in LIB and "dp.textContent = (m && m._deep) ? '◂ Quick' : 'Full ▸';" in LIB
    assert "'.lhm-quickmode .lhm-det > :not(.lhm-hd):not(.lhm-quick):not(.lhm-cta):not(.lhm-top):not(.lhm-wcfg){display:none!important}'," in LIB, "the old panes hide only while the quick body shows"


def test_every_menu_has_the_boards_body_and_the_context_menu_is_the_galaxy_budget_rows_cta():
    assert "const _QUICK={context:_quickContext, sessions:_quickSessions, activity:_quickActivity, loop:_quickLoop, workspace:_quickWorkspace, sandbox:_quickSandbox, ops:_quickOps, settings:_quickSettings};" in HTML
    assert "if(_QUICK[m.id]) m.quick=_QUICK[m.id]; });" in HTML
    ctx = HTML[HTML.index("function _quickContext(el){"):HTML.index("function _quickSessions(el){")]
    assert "VeraWidget.draw('context_graph',data,'m',{height:196,bare:true,view:_qGalView,allEdges:_qGalAll,off:_qGalOff,color:srcCol,hub:'aide'" in ctx, "the galaxy is the widget form"
    for s in ('data-w="context galaxy · graph" data-tpl="lhm:ctx-galaxy"', "class=\"vseg\"", "class=\"gal-all", "class=\"gal-mix\"", 'data-w="budget bar · meter" data-tpl="lhm:ctx-budget"', '<span class="comp">', 'In this prompt<button class="grp-act"', '<div class="lrow', '<span class="wbar">'):
        assert s in ctx, s
    assert "nodeClick(d.dataset.id)" in ctx, "a record in the galaxy opens"
    assert "cta:{label:'Expand to the full graph →', run:()=>{ if(!_pages.has('graph')) togglePage('graph'); }}}," in HTML
    assert "function _ctxShares(){" in HTML and "function _ctxMeterSegs(){" in HTML and "try{ _ctxMeterSegs(); }catch(_){}" in HTML, "the header meter carries the same shares"
    for fn, s in (("_quickSessions", "loadSession(r.dataset.sid)"), ("_quickActivity", "Running · '+running.length"), ("_quickLoop", '<div class="lps '), ("_quickLoop", "/workshop/agent_loop/cancel"),
                  ("_quickWorkspace", "panelOpen(r.dataset.panel)"), ("_quickWorkspace", "Open now · '+open.length+' · one set, one bridge"), ("_quickSandbox", "_sessionSbxTerminal()"), ("_quickOps", 'data-w="capabilities · counter"'), ("_quickSettings", "VeraLHM.pick('settings/Cfg')")):
        body = HTML[HTML.index("function " + fn + "(el){"):]
        body = body[:body.index("\n  function ", 10)]
        assert s in body, fn + ": " + s


def test_the_old_panes_and_their_controls_are_still_there():
    for ctl in ("ctxViewGraph", "ctxViewList", "ctxViewFabric", "ctxViewRaw", "ctxViewFrames", "ctxViewSettings", "ctxLayerBar", "histList", "histSearch", "rpCapList", "rpPanelList", "loopVariant", "sbxSessionTools", "monitorStream"):
        assert 'id="%s"' % ctl in HTML, ctl
    assert "onclick=\"CH.ctxTab('ctx',this)\"" in HTML and "onclick=\"CH.railTab('Caps',this)\"" in HTML
