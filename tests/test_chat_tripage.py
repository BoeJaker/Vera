"""
The tri-page's canvas and graph columns in the chat (canvas-graph; the Canvas board): #canvasColumn and
#graphColumn beside #chatStack, + Canvas / + Graph in the top bar, ?pages=chat,canvas,graph, the session
canvas in <vera-canvas>, the chat's own context graph in <vera-context-graph>, the focused turn driving the graph,
_wPin through the resolver, the canvas.* / lhm.compose directives — and everything that was there before,
still there. Text-level: the chain is held together by the strings these tests look for.
"""
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "chat", "chat_panel.html")
EL = _read("vera", "canvas", "canvas_element.js")
PY = _read("vera", "canvas", "canvas_capabilities.py")


def _once(hay, needle):
    assert hay.count(needle) == 1, "%r expected once, found %d" % (needle, hay.count(needle))


# ── the columns ───────────────────────────────────────────────────────────────

def test_the_two_columns_sit_beside_the_chat_stack_inside_the_chat_column():
    col = HTML.index('<div id="chatColumn">')
    stack = HTML.index('<div id="chatStack">')
    cv = HTML.index('<div id="canvasColumn" class="tri-col" data-col="canvas">')
    gr = HTML.index('<div id="graphColumn" class="tri-col" data-col="graph">')
    host = HTML.index('<div id="chatPanelHost">')
    end = HTML.index('</div><!-- /#chatColumn -->')
    assert col < stack < cv < gr < host < end, "siblings of #chatStack, before the untouched panel host"
    _once(HTML, 'id="canvasColumnBody"')
    _once(HTML, 'id="graphColumnBody"')
    assert '<b>Session canvas</b>' in HTML and 'id="cvColNow"' in HTML, "the board's header: Session canvas · NOW"


def test_the_panel_host_is_untouched():
    for s in ('<div id="chatPanelHost">', '<iframe id="chatPanelFrame" class="panel-host-frame"',
              'body.has-panel #chatColumn{ flex-direction:row; gap:0; }', 'body.has-panel #chatPanelHost{'):
        _once(HTML, s)
    assert 'onclick="CH.panelPopOut()"' in HTML and 'onclick="CH.panelClose()"' in HTML


def test_the_header_buttons_drive_the_exported_toggle():
    _once(HTML, '''id="colCanvasBtn" onclick="CH.togglePage('canvas')"''')
    _once(HTML, '''id="colGraphBtn" onclick="CH.togglePage('graph')"''')
    assert '>+ Canvas</button>' in HTML and '>+ Graph</button>' in HTML
    # beside the existing layout toggles, which stay
    _once(HTML, 'id="viewToggleBtn" onclick="CH.toggleViewMode()"')
    assert 'onclick="CH.toggleRight()"' in HTML
    m = re.search(r"return\{\s*([^}]*?)init,send,", HTML)
    assert m and "togglePage,openPage,_pagesOpen,_canvasColumnMount,_graphAnchorPost" in m.group(1), "exported on CH"


def test_pages_come_from_the_url_then_localstorage():
    _once(HTML, "const _PAGES_KEY='vera.chat.pages';")
    assert "new URLSearchParams(location.search).get('pages')" in HTML
    assert "localStorage.setItem(_PAGES_KEY, JSON.stringify(_pagesOpen()))" in HTML
    assert "body.dataset.cols=_pagesOpen().filter(n=>n!=='graph').join(' ');" in HTML and "body.classList.toggle('has-cols', _pagesOpen().some(n=>n!=='graph'));" in HTML   # the graph page is the LHM grown, not a column
    _once(HTML, "try{ _pagesMount(); }catch(e){ console.warn('tri-page columns: mount failed', e); }")
    i = HTML.index("try{ _cmpMount(); }")
    assert HTML.index("try{ _pagesMount(); }") > i, "after the composer's modes, in init"


def test_the_layout_css():
    for s in ("body.has-cols #chatColumn{flex-direction:row;gap:0}",
              'body[data-cols~="canvas"] #canvasColumn,body[data-cols~="graph"] #graphColumn{display:flex}',
              '@media (max-width:1919px){ body[data-cols~="canvas"][data-cols~="graph"] #graphColumn{flex:0 0 min(340px,28%)} }',
              "#canvasColumn vera-canvas{--vc-max:none;margin:0;flex:1;min-height:0}",
              ".tri-col{display:none;flex-direction:column;flex:1 1 0;"):
        _once(HTML, s)


def test_the_canvas_column_hosts_the_session_canvas_element():
    assert "_capCall('canvas.session.resolve',{session_id:SID})" in HTML
    assert "el=document.createElement('vera-canvas')" in HTML and "el.setAttribute('canvas-id', cid)" in HTML
    assert "if(_pages.has('canvas')&&SID&&SID!==_cvColSid) _canvasColumnMount()" in HTML, "follows the session"
    _once(HTML, '<script src="/ui/elements/canvas_element.js"></script>')
    assert "el.addEventListener('vera:canvas:rendered'" in HTML and "dispatchEvent(new CustomEvent('vera:canvas:rendered'" in EL


def test_the_graph_column_is_the_chats_own_context_graph_and_hears_the_focused_turn():
    # the design's context graph, the chat's own element — no other panel embedded, no iframe
    assert '<div class="tri-bd" id="graphColumnBody"></div>' in HTML and 'graphColumnFrame' not in HTML
    assert "_ctxCol=document.createElement('vera-context-graph')" in HTML
    assert "_ctxCol.setContext(nodesAll, edgesAll.map(" in HTML and "color:srcCol" in HTML, "fed by the chat's own records and the assembled context"
    assert "function _ctxColLoopEv(ev){" in HTML and HTML.count("_ctxColLoopEv(ev)") >= 4, "the live loop's events reach the column's loop lane"
    assert "fetch(BASE+'/dream/goals/list')" in HTML and "_ctxCol.setPlan(" in HTML
    # the chat's other graphs, in the same column: the rail's Memory graph and the DAG graph, from the state the chat keeps
    assert "_ctxCol.setMemory(_memAllNodes, _memAllEdges, {color:_nodeColorByType, edgeColor:r=>_edgeStyle(r||'').c, hide:_memEdgeHideTypes})" in HTML
    assert "try{ memGraphLoad(); }catch(_){}" in HTML, "the rail's own loader"
    assert "_ctxCol.setDag(_lastDagNodes, _lastDagEdges);" in HTML   # the DAG's edges reach the element too
    assert "if(d.family==='memory'&&d.rec){ try{ _memNodeClick(d.id, d.rec); }catch(_){} }" in HTML, "Open on a memory record opens the rail's memory detail"
    # the user's notes: two menus on the left (rail | graph), no runs while typing, the assembled context in the column
    assert "#rightRail.lhm-host.lhm-grown > .lhm-det{width:min(46vw,680px);flex:0 0 min(46vw,680px)}" in HTML, "the graph grows the menu in place (no second column)"
    assert "if(ico&&!ico.classList.contains('top')&&ico.getAttribute('data-menu')!=='context') _ctxGrow(false); }catch(_){} }, true);" in HTML, "another rail icon folds the graph back"
    assert "_ctxCol.addEventListener('vera:ctx:collapse', ()=>{ _ctxGrow(false);" in HTML
    assert "if(Date.now()-_ctxTypingT<900){ svg.innerHTML='';" in HTML and "inp.addEventListener('input', ()=>{ _ctxTypingT=Date.now(); _ctxRunsDraw(); });" in HTML
    assert "function _ctxAssembledExtra(layers, present){" in HTML and "const extra=_ctxAssembledExtra(_CTX_ALL_LAYERS, CTX_NODES);" in HTML
    assert "const _ex=_ctxAssembledExtra(_ctxLayers, vis); const extraNodes=_ex.nodes, extraEdges=_ex.edges;" in HTML, "the rail's graph draws from the same function"
    for src in ("'__skill__'", "'__ont__'", "'__ent__'", "'__cap__'", "'__qa__'", "'__agent__'"):
        assert HTML.count(src) >= 1, src
    assert "const msg={type:'vera:graph:anchor', mid:w?(w.dataset.mid||''):'', rect, session_id:SID||''};" in HTML
    assert "try{ _ctxColumnSync(msg.mid); _ctxRunsDraw(); }catch(_){}" in HTML, "the focused turn drives the graph and the runs"
    assert "msgs.addEventListener('scroll', ()=>{ if(Date.now()-_grScrollProg>900) _grFocusMid=''; _ctxFollow(); }, {passive:true});" in HTML   # ctx-graph-5: the scroll follows the turn in view
    assert "Date.now()-_grAnchorT>150" in HTML, "throttled"
    assert '<svg id="ctxRunsOverlay" aria-hidden="true"></svg>' in HTML and "function _ctxRunsDraw(){" in HTML
    assert 'body[data-cols~="graph"] #graphColumn{order:-1;border-left:none;border-right:1px solid var(--border)}' in HTML, "the design's order: rail | graph | chat | canvas"
    assert "const leftSide=gc.left<M.left;" in HTML, "the runs enter the message from the graph's side"
    _once(HTML, '<script src="/ui/context_graph_element.js"></script>')
    assert "'/ui/panels/file/memory_graph_panel.html'," in HTML, "the alias table itself is unchanged"


def test_the_columns_report_into_the_one_panel_set():
    assert "return mine.concat(_pagesOpenNow()).concat(theirs);" in HTML
    assert "p.placement==='beside chat'||p.placement==='column'" in HTML
    assert "placement:'column', close:()=>togglePage('canvas')" in HTML and "id:'context-graph', label:'Context graph'" in HTML
    _once(HTML, "function _panelsOpenReport(){")
    assert "fetch(BASE+'/ui/panels/open/report'" in HTML


# ── _wPin through the resolver ────────────────────────────────────────────────

def test_wpin_goes_through_the_resolver_onto_the_session_canvas():
    body = HTML[HTML.index("async function _wPin(id, btn){"):HTML.index("/* ── Artifact rendering")]
    assert "const key='widget:'+_widgetKey(w.rec);" in body
    assert "_capCall('canvas.session.resolve',{session_id:SID||''})" in body
    assert "_capCall('canvas.add',{id:cid,session_id:SID||'',kind:'widget',content,key," in body
    assert "r.resolved==='shown'?'Already on the canvas · shown':'On the canvas'" in body
    assert "openPage('canvas');" in body
    assert "Pinned from chat" not in HTML and "_PIN_CANVAS_ID" not in HTML, "the special-case canvas is gone"
    assert "'Pin to canvas'" in body, "the button still reads as it did"


# ── the directives ────────────────────────────────────────────────────────────

def test_the_directive_cases():
    sw = HTML[HTML.index("async function _uiDirectiveApply(p){"):HTML.index("function _uiAskChip(row){")]
    assert "case 'lhm.compose': {" in sw and "if(!(window.VeraLHM&&typeof VeraLHM.compose==='function')) return later(" in sw
    assert "VeraLHM.compose({menu:String(a.menu||''), label:a.label, icon:a.icon, add, remove:" in sw   # the chat wiring pass: the vocabulary's {menu, widgets} as the library's spec
    assert "case 'canvas.ask': {" in sw and "_uiAskCard(q, a.choices, 'canvas item · '+key)" in sw and "_capCall('canvas.ask',{key, question:q, session_id:SID||''})" in sw
    assert "case 'canvas.add': case 'canvas.show': case 'canvas.pin': case 'canvas.park': case 'canvas.size': {" in sw
    assert "_capCall(name==='canvas.show'?'canvas.add':name, args)" in sw and "openPage('canvas');" in sw
    for s in ("case 'ui.ask':", "case 'panel.open':", "case 'panel.close':", "case 'lhm.focus':", "case 'graph.view':", "case 'graph.focus':", "case 'chat.mode':", "case 'chat.card':"):
        assert s in sw, s


# ── the element's projection ──────────────────────────────────────────────────

def test_the_element_renders_the_session_projection():
    # one projection for every canvas (Notes/42 defect 43): the session canvas and an agent's canvas through the same cards
    assert "this.renderSession(doc, blocks, body);\n      if (this.hasAttribute('rail')) this._railMark();" in EL
    assert "renderSession(doc, blocks, body) {" in EL
    for s in ('class="band pinned"', 'class="band now"', "<b>${plainDoc ? 'BLOCKS' : 'NOW'}</b>", 'class="band parked"', 'class="chips"', 'data-key="${esc(b.key)}" data-size="${size}"',
              'data-act="pin"', 'data-act="park"', 'data-act="size"', 'data-act="remove"'):
        assert s in EL, s
    assert "window.VeraWidget && typeof window.VeraWidget.draw === 'function'" in EL and "window.VeraWidget.draw(form, rec.data, size" in EL   # the form is the record's (rec.form or draw.form)
    assert '<div class="vc-rec"><b>' in EL, "the record card when there is no drawer"
    assert "fetch(base + '/mcp/call'" in EL and "Object.assign({ id: this.canvasId }, args || {})" in EL
    assert "const rev = doc.revision != null ? doc.revision : doc.rev != null ? doc.rev" in EL
    assert "static get observedAttributes() { return ['canvas-id', 'rows', 'compact', 'columns', 'rail', 'session-id']; }" in EL   # the rail and the session (agent V)
    assert "if (this.hasAttribute('compact')) this.style.setProperty('--vc-max', '240px');" in EL
    assert "this._timer = setInterval(() => this.refresh(), 3000);" in EL
    # the plain projection is what it was
    assert "'<div class=\"empty\">Nothing on this canvas — add a block above, or let an agent fill it.</div>'" in EL   # an agent's empty document, in the one projection (agent V)


def test_the_relevance_engine_drives_the_canvas_columns_focus_set():
    # P2: the chat asks after every reply (applies) and on a focus change in Hover/Zen (asks only); the element shows the focus per tier
    fn = HTML[HTML.index("async function _cvRelevance("):HTML.index("async function _canvasColumnMount(){")]
    assert "_capCall('canvas.session.relevance',{session_id:SID, turn:mid||'', text:String(text||'').slice(0,2000), entities:entities.slice(0,60), recent, apply:o.apply!==false})" in fn
    assert "cv.setFocus(r.focus||[], r.scores||{})" in fn and "if(den==='full') return;" in fn, "a focus change asks only in Hover and Zen"
    assert "try{ _cvRelevance(_cvRelMid, _cvRelText+'\\n'+String(fullText||'').slice(0,1500)); }catch(_){}" in HTML, "asked when the reply lands"
    assert "_cvRelMid=uMsg.mid; _cvRelText=msg;" in HTML
    assert "msgsEl.addEventListener('scroll', _cvRelFocus, {passive:true}); msgsEl.addEventListener('click', _cvRelFocus);" in HTML
    assert "cv&&cv.retier&&cv.retier();" in HTML, "the focus set follows the tier"
    assert "setFocus(keys, scores) {" in EL and "retier() {" in EL and "tier() {" in EL
    assert "const fcls = inF ? (F ? ' inf' : '') : (tier === 'zen' ? ' out' : ' dim');" in EL and ".it.out{display:none}" in EL
    assert 'async def cap_canvas_session_relevance(' in PY and '"canvas.session.relevance"' in PY and 'rev = await _write(doc, "add", key, turn=_anchor_turn(anchor))' in PY


def test_the_canvas_columns_stage_places_items_level_with_their_turns_and_routes_the_runs():
    # P3/P5: the placer takes measured turn tops, the column scrolls with the transcript, the runs go down the gutter, the checker
    assert "function place(items, turns, o) {" in EL and "function checkRoutes(routes, rects) {" in EL and "root.VeraCanvas = Object.assign(root.VeraCanvas || {}, api);" in EL
    assert "setTurns(turns, o) {" in EL and "syncScroll(msgsScrollTop, msgsTopClient) {" in EL and "itemRects() {" in EL and "_placeNow() {" in EL
    assert '<div class="stage" id="stage">' in EL and 'class="hidbtn" data-act="hid"' in EL and "vera:canvas:placed" in EL and "vera:canvas:hover" in EL
    assert "el.setAttribute('stage',''); el.setAttribute('columns', String(_cvCols()));" in HTML
    assert "function _cvTurnTops(){" in HTML and "top:Math.round(r.top-mr.top+msgs.scrollTop)" in HTML, "measured tops in the transcript's scroll frame"
    assert "new ResizeObserver(()=>_cvStageSync(true))" in HTML and "msgs.addEventListener('scroll', ()=>_cvStageSync(), {passive:true})" in HTML
    assert 'id="cvRunsOverlay"' in HTML and "let pts=[{x:M.right, y:my},{x:gx, y:my},{x:gx, y:iy},{x:it.rect.left, y:iy}];" in HTML, "down the gutter, never diagonally"
    assert "if(den==='zen'){ svg.innerHTML='';" in HTML and "const wanted=den==='hover'?items.filter(it=>it.key===_cvHoverKey||(_cvHoverMid&&it.mid===_cvHoverMid)):items;" in HTML
    assert "window.VeraCanvas.checkRoutes(drawn, rects)" in HTML and 'id="cvColCols"' in HTML


def test_the_python_side_names_match_the_vocabulary():
    for s in ('"canvas.session.resolve"', '"canvas.add"', '"canvas.pin"', '"canvas.park"', '"canvas.size"', '"canvas.recall"', '"canvas.timeline"',
              '"canvas.session.room"', '"canvas.ask"', 'ITEM_STATES = ("now", "parked", "pinned", "hidden")', 'ITEM_SIZES = ("s", "m", "l", "xl")',
              'async def cap_canvas_add(id: str = "", session_id: str = "", kind: str = "note", content: Any = None,',
              'key: str = "", at: str = "", size: str = "", anchor: Any = None, trace_id=None):',
              'doc["revision"] = int(doc.get("revision") or 0) + 1'):
        assert s in PY, s
