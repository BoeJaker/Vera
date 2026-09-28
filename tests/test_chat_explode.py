"""
Explode in the chat (UI redesign, Notes/40 §6 P6; the Chat & canvas set): the
transcript pulled apart into one scene — a station per turn: what it read, the
exchange, what it produced, where it landed — in cards · front · iso, fed from
the transcript the chat has. Text-level; the layouts run under node in
tests/test_exploded_element.cjs.
"""
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "chat", "chat_panel.html")
PY = _read("vera", "chat", "chat_panels_capabilities.py")
EL = _read("vera", "chat", "exploded_element.js")


def _fn(name):
    i = HTML.index("function %s(" % name)
    j = HTML.find("\n  function ", i + 10)
    return HTML[i:j if j > 0 else i + 20000]


def test_the_element_is_served_and_loaded_and_is_the_chats_own():
    assert '@APP.get("/ui/exploded_element.js", include_in_schema=False)' in PY and 'Path(__file__).parent / "exploded_element.js"' in PY
    assert '<script src="/ui/exploded_element.js"></script>' in HTML
    assert "root.customElements.define('vera-exploded', VeraExploded);" in EL and "root.VeraExploded = api;" in EL
    for m in ("'cards'", "'front'", "'iso'"):
        assert m in EL
    assert "root.VeraISO.proj(S.tilt || 30, S.azim || 45, 1, true)" in EL, "the shared projection, at the view's own tilt and swing"
    assert "createGraph(" not in EL and "vera_graph.js" not in EL and "memory_graph" not in EL


def test_the_header_has_explode_and_its_modes_and_the_scene_takes_the_transcripts_place():
    assert '<button class="tb-btn" id="xplBtn" onclick="CH._xplToggle()"' in HTML
    assert "function _xplToggle(){ if(_xpl.on) _xplClose(); else explode(true); }" in HTML, "the same button closes the scene"
    assert ",_xplToggle,_xplClose," in HTML, "and both are on CH - an onclick naming what is not exported fails in silence"
    for m in ("cards", "front", "iso"):
        assert 'data-xm="%s" onclick="CH.explodeMode(\'%s\')"' % (m, m) in HTML, m
    assert '<div id="xplHost"></div>\n      <div id="msgs">' in HTML, "the scene in the transcript's place, the composer docked under it"
    # the scene is shown and the transcript hidden. The rule gained its geometry in 2394448 (the scene takes the
    # whole column, the composer floats over it), so pinning the declaration block whole made a layout change
    # read as the scene no longer being shown at all.
    # The claim: with the scene up, the transcript and the things that belong to it are hidden and the scene is
    # shown. Pinning either declaration WHOLE made two unrelated edits read as the feature breaking - the rule
    # gained its geometry in 2394448, and the turn rail joined the hidden list (it maps the transcript, and with
    # the scene in the transcript's place there is no transcript to map). So: the members, and the property.
    assert re.search(r"body\.exploded #msgs[^{\n]*\{display:none!important\}", HTML), "the transcript is hidden"
    assert re.search(r"body\.exploded #jumpToBottomBtn[^{\n]*\{display:none!important\}", HTML), "and so is its jump button"
    assert "body.exploded #xplHost{display:flex" in HTML, "and the scene is shown in its place"
    x = _fn("explode")
    assert "document.body.classList.toggle('exploded', on);" in x and "if(on){ _xplMount(); _xplRefresh();" in x
    assert "if(on) document.body.classList.add('has-msgs');" in x, "the composer docks under the scene"
    assert "_xplEl=document.createElement('vera-exploded')" in _fn("_xplMount")
    assert "_xplMo=new MutationObserver(" in _fn("_xplMount"), "the scene follows the transcript"
    assert "_xplEl.addEventListener('vera:xpl:turn', ev=>{ const mid=(ev.detail||{}).mid||''; if(!mid) return; _grFocusMid=mid;" in HTML, "a station picked is the turn in focus"


def test_pass_b_the_context_graph_layer_is_a_widget_form_no_thinking_node_images_per_tier():
    s = _fn("_xplScene")
    assert "if(b.querySelector('.think-throb')) return '';" in s, "a message that is still thinking contributes no words"
    _strip = re.search(r"c\.querySelectorAll\('([^']+)'\)\.forEach\(x=>x\.remove\(\)\);", s)
    assert _strip, "the clone is stripped before its words are taken"
    for _sel in (".think-throb", ".think-box", ".cap-dot"):
        assert _sel in _strip.group(1).split(","), "the thinking message is never a node: " + _sel
    assert "if(!body||body.querySelector('.think-throb')) return; const made=cur.made;" in s
    assert "kind:'image', src}" in s and "el.closest('.sd-img,.cap-imgs')?'image · diffusion':'image'" in s, "images travel with their source"
    assert "_TURN_RELS[mid]=_xplRels();" in HTML and "function _xplRels(){" in HTML and "const _xplCtxCard=(n)=>({id:n.id, n:n.label||n.id," in HTML
    assert "_xplEl.dataset.den=(document.documentElement.getAttribute('data-den')||'full').toLowerCase();" in HTML and "try{ if(_xpl.on) _xplRefresh(); }catch(_){}   // the scene's images follow the tier" in HTML
    assert "{ key: 'graph', name: 'context graph', sub: 'where it came from', col: 'var(--xp-dv1)', kind: 'graph' }," in EL
    assert "root.VeraWidget.draw('context_graph', { nodes: g.nodes, rels: g.rels }, 'm', { height: H, bare: true, labels: true, full: false })" in EL, "the mini graph is the registry's form"
    assert 'data-r="scrub"' in EL and "ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight'" in EL, "the timeline scrub"
    assert 'vera-exploded[data-den="hover"] .xp-it:hover .xp-img' in EL and 'vera-exploded[data-den="zen"] .xp-it.open .xp-img' in EL
    WE = _read("vera", "widgets", "widget_element.js"); WR = _read("vera", "widgets", "widget_record.py")
    assert "R.context_graph = (d, H, opts) => {" in WE and "context_graph: 'graph'" in WE and "'.vw-cgfull[data-cg]:not([data-live])'" in WE, "the form draws at every size; XL hands the data to the full element"
    assert '_F("context_graph", "graph", glyph="context_graph", options=("lanes", "labels")),' in WR, "the form is in the catalogue"


def test_place_puts_a_registry_widget_on_the_stations_plate_through_the_resolver():
    assert 'data-a="place"' in EL and "new CustomEvent('vera:xpl:place'" in EL
    assert "_xplEl.addEventListener('vera:xpl:place', ev=>_xplPlaceOpen((ev.detail||{}).mid||''));" in HTML
    o = _fn("_xplPlaceOpen"); p = _fn("_xplPlacePick")
    assert "_capCall('widget.template.list',{limit:200})" in o and "_capCall('widget.forms',{})" in o, "the picker lists the user's templates, then every form"
    assert "_capCall('canvas.add',{session_id:SID||'', kind:'widget', content, key, at:'now', size:'m', anchor:{mid, turn:mid}})" in p, "a pick goes through the resolver, anchored to the turn"
    assert "_capCall('widget.template.instantiate',{id:r.id, where:'iso plate', host:'chat', session_id:SID||'', config:{turn:mid}})" in p, "the template counts an iso-plate placement"
    assert "kind:'widget', tpl:tplName, key, form:r.form}" in p, "tagged with its template AND the form that draws it"


def test_the_scene_is_built_from_what_the_chat_has():
    s = _fn("_xplScene")
    assert "cur={mid, who:'you', t:tm(w), text:txt(w).slice(0,160), read:rd.read.slice(0,12), readAll:rd.read.slice(0,40), rel:rd.rel.slice(0,80), say:[], made:[], land:(_TURN_LAND[mid]||[]).slice(0,12)}" in s
    assert "const rd=_xplTurnReads(mid);" in s and "function _xplTurnReads(mid){ const r=_TURN_READS[mid]||[]; if(r.length) return { read:r, rel:_TURN_RELS[mid]||[] };" in HTML
    assert "const ns=f.nodes.filter(n=>n.included!==false); const ids=new Set(ns.map(n=>n.id));" in HTML, "a turn whose reads were never recorded falls back to its own context frame"
    assert "(t.readAll || t.read || []).forEach((c) => add(c));" in EL, "the graph layer sees everything the turn read"
    assert "if(!cur.reply){ cur.reply=txt(w).slice(0,160);" in s, "the reply folds into the question's station"
    for sel in (".cap-inline", ".art-card", "vera-widget", "vera-mermaid,.mermaid,pre.mermaid", "pre code", "vera-agent-loop-output", "img"):
        assert "body.querySelectorAll('%s')" % sel in s, sel
    assert "'.pa-atts .pa-att b'" in s, "attachments the question carried are what it read"
    assert "const live=CTX_NODES.filter(n=>n.included!==false).map(_xplCtxCard); last.read=live.slice(0,12); last.readAll=live.slice(0,40);" in s, "the newest question reads the live context"
    assert "_xplRecordReads(uMsg.mid);\n    _cvRelMid=uMsg.mid; _cvRelText=msg;\n    _paDecorate(uMsg.body, uCtx);" in HTML, "what a question read is recorded when it is sent"
    assert "(_TURN_LAND[mid]=_TURN_LAND[mid]||[]).push(" in _fn("_wPin"), "what a turn pinned to the canvas is recorded"
    for name in ("explode", "explodeMode", "_xplScene", "_xplRefresh"):
        assert re.search(r"\n    [^\n]*\b%s," % name, HTML), name + " is exported on CH"
