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
    assert "body.dataset.cols=_pagesOpen().join(' ')" in HTML and "body.classList.toggle('has-cols', _pages.size>0)" in HTML
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
    assert "_ctxCol.setContext(CTX_NODES, CTX_EDGES.map(" in HTML and "color:srcCol" in HTML, "fed by the chat's own records"
    assert "function _ctxColLoopEv(ev){" in HTML and HTML.count("_ctxColLoopEv(ev)") >= 4, "the live loop's events reach the column's loop lane"
    assert "fetch(BASE+'/dream/goals/list')" in HTML and "_ctxCol.setPlan(" in HTML
    assert "const msg={type:'vera:graph:anchor', mid:w?(w.dataset.mid||''):'', rect, session_id:SID||''};" in HTML
    assert "try{ _ctxColumnSync(msg.mid); _ctxRunsDraw(); }catch(_){}" in HTML, "the focused turn drives the graph and the runs"
    assert "msgs.addEventListener('scroll', ()=>_graphAnchorPost(), {passive:true})" in HTML
    assert "Date.now()-_grAnchorT>150" in HTML, "throttled"
    assert '<svg id="ctxRunsOverlay" aria-hidden="true"></svg>' in HTML and "function _ctxRunsDraw(){" in HTML
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
    assert "if (doc.mode === 'session' || blocks.some(b => b && b.key)) { this.renderSession(doc, blocks, body); return; }" in EL
    assert "renderSession(doc, blocks, body) {" in EL
    for s in ('class="band pinned"', 'class="band now"', '<b>NOW</b>', 'class="band parked"', 'class="chips"', 'data-key="${esc(b.key)}" data-size="${size}"',
              'data-act="pin"', 'data-act="park"', 'data-act="size"', 'data-act="remove"'):
        assert s in EL, s
    assert "window.VeraWidget && typeof window.VeraWidget.draw === 'function'" in EL and "window.VeraWidget.draw(rec.draw.form, rec.data, size" in EL
    assert '<div class="vc-rec"><b>' in EL, "the record card when there is no drawer"
    assert "fetch(base + '/mcp/call'" in EL and "Object.assign({ id: this.canvasId }, args || {})" in EL
    assert "const rev = doc.revision != null ? doc.revision : doc.rev != null ? doc.rev" in EL
    assert "static get observedAttributes() { return ['canvas-id', 'rows', 'compact']; }" in EL
    assert "if (this.hasAttribute('compact')) this.style.setProperty('--vc-max', '240px');" in EL
    assert "this._timer = setInterval(() => this.refresh(), 3000);" in EL
    # the plain projection is what it was
    assert "body.innerHTML = '<div class=\"empty\">This canvas is empty — blocks appear here as they are added.</div>';" in EL


def test_the_python_side_names_match_the_vocabulary():
    for s in ('"canvas.session.resolve"', '"canvas.add"', '"canvas.pin"', '"canvas.park"', '"canvas.size"', '"canvas.recall"', '"canvas.timeline"',
              '"canvas.session.room"', '"canvas.ask"', 'ITEM_STATES = ("now", "parked", "pinned", "hidden")', 'ITEM_SIZES = ("s", "m", "l", "xl")',
              'async def cap_canvas_add(id: str = "", session_id: str = "", kind: str = "note", content: Any = None,',
              'key: str = "", at: str = "", size: str = "", anchor: Any = None, trace_id=None):',
              'doc["revision"] = int(doc.get("revision") or 0) + 1'):
        assert s in PY, s
