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
    # The chips (.gal-mix / .gm) are GONE and the budget bar took their job (user 2026-09-18: "instead of chips
    # ... can the little chart above 'in this prompt' and below the graph be the interface to enable disable
    # sections"). They were a second legend for the same families - colour, name, tokens - all of which the rows
    # below already carry, so the control moved onto the chart that was already drawing the same split. The bar's
    # widths stay the true token shares: _qGalOff filters the GRAPH, never what the prompt carries.
    for s in ('data-w="context galaxy · graph" data-tpl="lhm:ctx-galaxy"', "class=\"vseg\"", "class=\"gal-all", 'data-w="budget bar · meter" data-tpl="lhm:ctx-budget"', '<span class="comp">', 'In this prompt<button class="grp-act"', 'title="Refresh the assembled context">↻</button>', '<div class="lrow', '<span class="caret">', '<span class="wbar">'):
        assert s in ctx, s
    assert "class=\"gal-mix\"" not in ctx, "the chips were replaced by the budget bar"
    assert "querySelectorAll('.gm')" not in ctx, "and their handler with them"
    # each band is its family's share AND the switch for whether the graph draws it; an off band keeps its place
    # and a trace of its own colour, because a band that vanished could never be pressed again
    assert "'<i class=\"'+(_qGalOff[r.f]?'off':'on')+'\" data-fam=\"'+esc(r.f)+'\" style=\"width:'+r.pct.toFixed(1)+'%;--c:'+r.col+'\"" in ctx, "the bar's bands carry family, state and colour"
    # and _qGalOff is now the ONE source filter: the legacy .lbtn row reads it through _ctxLayers, so a band
    # repaints every surface it governs rather than only the rail's own menu
    assert "el.querySelectorAll('.comp i[data-fam]').forEach(c=>c.addEventListener('click',()=>{ const f=c.dataset.fam; _qGalOff[f]=!_qGalOff[f]; _ctxViewRepaint(); }));" in ctx, "pressing a band toggles the same _qGalOff the chips drove, and repaints every surface"
    # THE RAW PROMPT. The legacy Context pane held six sub-views: Graph, List, Fabric, Raw, Frames and Settings.
    # Every one of them is covered by a surface that outlives it - the plot, "In this prompt", the fabric section,
    # the frames scrubber - and its Settings sections have already been MOVED into the Settings page (_spTake
    # appendChild's them out of #rpCtx, which is why that view now reads "6 sections moved to Settings").
    # Raw was the exception: the assembled system prompt could be read in that pane and nowhere else. It lives
    # here now, folded shut, because it is long and it is not what the menu is for.
    assert 'data-w="raw prompt \u00b7 text"' in ctx, "the raw prompt has a home outside the legacy pane"
    assert 'data-a="raw-toggle"' in ctx and 'data-a="raw-refresh"' in ctx
    assert "const _ctxRawText=()=>" in HTML and "_assembledCtx.fullRaw" in HTML, "it reads the real assembled prompt"
    # the fold and the prompt's length are part of the repaint key, or pressing the fold repaints nothing - the
    # same early-return the budget bar's off-state fell into
    assert "+':'+(_qRawOpen?'R':'r')+':'+_ctxRawLen();" in HTML, "the fold is in the menu's repaint signature"
    # and the refresh must not also toggle the fold
    assert "ev.stopPropagation();" in HTML

    assert "nodeClick(d.dataset.id)" in ctx, "a record in the galaxy opens"
    assert "cta:{get label(){ return _ctxGrown?'Fold back to the quick menu ←':'Expand to the full graph →'; }, run:()=>_ctxGrow()}}," in HTML, "the CTA grows the menu into the full graph and folds it back"
    assert "function _ctxShares(){" in HTML and "function _ctxMeterSegs(){" in HTML and "try{ _ctxMeterSegs(); }catch(_){}" in HTML, "the header meter carries the same shares"
    for fn, s in (("_quickSessions", "loadSession(r.dataset.sid)"), ("_quickActivity", "Running · '+running.length"), ("_quickLoop", '<div class="lps '), ("_quickLoop", "/workshop/agent_loop/cancel"),
                  ("_quickWorkspace", "panelOpen(r.dataset.panel)"), ("_quickWorkspace", "Open now · '+open.length+' · one set, one bridge"), ("_quickSandbox", "_sessionSbxTerminal()"), ("_quickOps", 'data-w="capabilities · counter"'), ("_quickSettings", "_settingsPage(true);")):
        body = HTML[HTML.index("function " + fn + "(el){"):]
        body = body[:body.index("\n  function ", 10)]
        assert s in body, fn + ": " + s


def test_the_old_panes_and_their_controls_are_still_there():
    for ctl in ("ctxViewGraph", "ctxViewList", "ctxViewFabric", "ctxViewRaw", "ctxViewFrames", "ctxViewSettings", "ctxLayerBar", "histList", "histSearch", "rpCapList", "rpPanelList", "loopVariant", "sbxSessionTools", "monitorStream"):
        assert 'id="%s"' % ctl in HTML, ctl
    assert "onclick=\"CH.ctxTab('ctx',this)\"" in HTML and "onclick=\"CH.railTab('Caps',this)\"" in HTML
