"""
The mini context graph is the element's (Notes/42 defects 47 · 48 · 30): the quick menu's galaxy is drawn by
VeraContextGraph.miniHtml from the state the grown graph holds (All edges and the families passed through), a record
picked opens its compact card, the card's include/exclude toggles the record, a List toggle opens the records drawer
with search; the frames history reaches the grown graph (setFrames). Text-level.
"""
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def test_the_mini_has_both_faces_and_the_quick_menu_switches_them():
    """Notes/42 defect 60: the user likes what the mini grew into on this edge and wants the board's face as well."""
    el = _read("vera", "chat", "context_graph_element.js")
    # the element draws whichever face it is handed; the switch is the host's
    assert "const simple = String(opts.style || S.miniStyle || 'detailed').toLowerCase() === 'simple';" in el
    # SIMPLE is the board's face and the fold is the whole of it: the context plot by itself - its rings, its
    # spokes, its records and the hub - with the other families FOLDED AWAY rather than drawn small. That one line
    # is what makes the two faces look like different graphs; removing it once turned simple into detailed with a
    # strip missing, which the user reported as having "2 complex" graphs.
    assert "const base = simple ? Object.assign({}, S, { mix: Object.assign({}, S.mix || {}, { loop: 'off', plan: 'off', estate: 'off' }), list: false }) : S;" in el
    # SIMPLE is the design's mini GALAXY - a different drawing, not drawPlot with layers off. Its sparseness is the
    # point and it is explicit in the board (Canvas.dc.html 6170): three or four marks per SOURCE, each source its
    # own sector, relevance falling outward along 28/50/72/94, the outermost hollow and carrying the source's name.
    # Drawing every record instead is what made simple read as a second complex graph.
    # the simple face has a draw of its own; its parameter list is not the claim (it gained the arrival store)
    assert "function drawMiniGalaxy(o, w, h" in el
    assert "if (simple) return '<div class=\"cg-mini simple\"" in el and "drawMiniGalaxy(o, w, h" in el, "simple draws the galaxy"
    # three or four marks per source is the whole of the sparseness. The rule now has ONE definition - the arrival
    # pass has to know which marks are about to be drawn, and a second copy of the formula beside the first is how
    # the two quietly stop agreeing - so the test follows it to where it lives rather than to the binding it had.
    assert "Math.min(3 + (si % 2), s.rows.length)" in el, "three or four marks per source - the whole of the sparseness"
    assert "const nOf = (s, si) =>" in el and "const N = nOf(s, si);" in el, "the drawing and the arrival read the same rule"
    assert "const r = 28 + k * 22, a = (a0 + (k - (N - 1) / 2) * 11) * RAD;" in el, "the board's radii and sector spread"
    assert "const a0 = -90 + si * (360 / SRC.length);" in el, "the sources sit evenly around the circle"
    assert "'Memory recalls'" in el and "'Graph (Neo4j)'" in el, "the rim labels are the legend's names, not the raw keys"
    # and the COMPLEX pair is untouched: the mini's detailed face and the grown graph are the same drawing at two sizes
    assert "drawPlot(o, opts.arr) + '</div><div class=\"cg-lanes\">'" in el, "detailed still draws the full plot and its lanes"
    # The memories are on EVERY graph — simple, the complex mini and the grown one. The board names "Memory recalls"
    # in the legend and the list beside the other seven sources; the memory level governs the recalls as well as the
    # arm (defect 65), so folding it off took them out of the simple plot entirely. It is not folded.
    assert "{ loop: 'off', plan: 'off', estate: 'off' }" in el and "memory: 'off', loop:" not in el, "the recalls are never folded off the simple face"
    # and nothing may reach around the fold to put a family back on that face
    assert "simplePlot" not in el, "the fold is the only thing that decides the simple face"
    # the recalls keep their own arm on the detailed face: memory is excluded from ctx so it is drawn on the
    # memory ring, not fanned into a context sector. The filter now also reads a source's LEVEL (off / focus /
    # all) rather than a plain on-off set, so the line is asserted by what it does, not by its old wording.
    assert "if (n.source === 'memory') return false;" in el, "the recalls keep their own arm on the detailed face"
    assert "const srcLvl = (s) => mixOf(S, s || '?');" in el, "a source has three levels, like a family"
    # and nothing else is laid over it - no lanes, no list, no record card
    assert "if (simple) return '<div class=\"cg-mini simple\"" in el
    assert "const detail = simple ? null :" in el and "list = simple ? false :" in el

    src = _read("vera", "chat", "chat_panel.html")
    assert "let _qGalStyle=" in src and "localStorage.getItem('vera_ctx_mini_style')" in src
    # what this is about: the element draws the mini, at the rail's width, in the face the menu chose, with no
    # card laid over the plot. Pinning the options object whole made ADDING an option (the arrival store) read as
    # the face having been dropped.
    call = re.search(r"gal=VeraContextGraph\.miniHtml\(st, 262, miniH, \{([^}]*)\}\)", src)
    assert call, "the element draws the mini at the rail's width"
    assert "style:_qGalStyle" in call.group(1) and "card:false" in call.group(1)
    assert 'data-style="' in src and "localStorage.setItem('vera_ctx_mini_style', _qGalStyle)" in src
    # the view buttons keep their own handler now that a second segment shares the row
    assert "el.querySelectorAll('.vseg button[data-view]')" in src
    # and the face is part of what decides a repaint
    assert "'#'+_qGalStyle+_qGalSel" in src


def test_the_mini_is_the_elements_face():
    src = _read("vera", "chat", "chat_panel.html")
    # The quick menu returns early on an unchanged signature. A body composed before the element script has loaded
    # draws the widget FALLBACK, and unless the renderer is part of that key the early return then fires for ever and
    # the element never draws (Notes/42 defect 84) - measured on the mirror, where .gal-host held a .vw-gal while the
    # element sat loaded and idle beside it.
    assert "const sig=(window.VeraContextGraph&&typeof VeraContextGraph.miniHtml==='function'?'E':'-')+S.rows.map(r=>r.f+r.tok).join('|')" in src, "the renderer is part of the repaint key"
    # and a key is only read when the menu is composed AGAIN, which nothing did once the element landed - so the
    # stand-in stayed for the life of the page. Having fallen back, the menu watches for the element and composes once.
    assert "if(!_ctxElWait) _ctxElWait=setInterval(" in src and "clearInterval(_ctxElWait); _ctxElWait=null; try{ VeraLHM.render(); }catch(_){}" in src, "the fallback is a stand-in, not a decision"
    assert "} else if(_ctxElWait){ clearInterval(_ctxElWait); _ctxElWait=null; }" in src, "and it stops watching once the element is there"
    assert "if(window.VeraContextGraph&&typeof VeraContextGraph.miniHtml==='function'){" in src
    assert "allEdges:_qGalAll, off:Object.keys(_qGalOff).filter(k=>_qGalOff[k])" in src
    call = re.search(r"gal=VeraContextGraph\.miniHtml\(st, 262, miniH, \{([^}]*)\}\)", src)
    assert call and "style:_qGalStyle" in call.group(1) and "card:false" in call.group(1)   # the face it is drawn in (defect 60)
    # the widget renderer stays the fallback
    assert "if(!gal) gal=window.VeraWidget?VeraWidget.draw('context_graph',data,'m'" in src


def test_the_minis_detail_list_search_and_frames():
    src = _read("vera", "chat", "chat_panel.html")
    assert "let _qGalSel='', _qSrcOpen={}, _qGalQ='', _ctxElWait=null, _ctxMiniWarned=false;" in src
    # The drawer became sections, and one use of the old name survived inside the stateFrom call - so every render
    # threw there, the catch emptied the galaxy and the widget fallback drew, which is why the face switch looked
    # dead (Notes/42 defect 84). Nothing may name it again, and the plot carries no drawer of its own.
    assert "_qGalList" not in src, "the drawer's old name is gone from every site, not just its declaration"
    assert "sel:_qGalSel||null, list:false, q:_qGalQ," in src, "the plot has no drawer: the records are sections of the menu"
    # and a render that throws has to say so, or the fallback hides it for the life of the page
    assert "console.warn('context mini: the element threw, falling back to the widget renderer', e)" in src
    # ONE list: each source is a section that opens IN PLACE to its own records. A second list of everything under
    # the first was not integrating them - it was the same drawer with its button moved.
    assert 'class="gal-srch"' in src and 'class="grp-act gal-list' not in src
    assert "let gal='', miniH=196, _recsOf=()=>'';" in src, "the plot keeps its box whatever a section is doing"
    assert "_recsOf=(f)=>{" in src and "VeraContextGraph.miniList(st, { q:_qGalQ, fam:f })" in src
    # the add box opens with the section, above its records — capabilities, skills and ontologies are the parts
    # of the prompt a reader most wants to change, and they are changed where they are listed
    assert "+(open?_ctxAddBox(r.f)+_recsOf(r.f):'');" in src, "a section's records sit under the row that opened it"
    assert "if(_qSrcOpen[f]){ delete _qSrcOpen[f];" in src and "else { _qSrcOpen[f]=true; _qSrcOn=f; }" in src, "opening a section lights its source on the plot"
    _el2 = _read("vera", "chat", "context_graph_element.js")
    assert "const o2 = opts.fam ? Object.assign({}, o, { list: o.list.filter((r) => String(r.source || '') === String(opts.fam)) }) : o;" in _el2
    # the board's face is what the menu opens on
    assert "localStorage.getItem('vera_ctx_mini_style')==='detailed'?'detailed':'simple'" in src, "simple is the default face"
    # the rows carry the element's own scope wherever the host puts them: .cg-mini declares both the grid that
    # lays a row out and every --cg-* colour, so bare rows draw as run-together text
    assert "return '<div class=\"cg-mini cg-recs\">'+VeraContextGraph.miniList(st, { q:_qGalQ, fam:f })+'</div>';" in src
    _el = _read("vera", "chat", "context_graph_element.js")
    assert ".cg-mini.cg-recs{height:auto;overflow:visible}" in _el and ".cg-mini.cg-recs .cg-list{position:relative;inset:auto;width:100%;" in _el
    # and it has to be readable: the rim labels name the sources, and the rows are read one by one
    assert ".cg-mini .cg-slbl{font-size:8.5px;" in _el and ".cg-mini .cg-hubt{font-size:8.5px}" in _el
    assert ".cg-mini .cg-list{width:100%;left:0;border-left:none;font-size:10px}" in _el and ".cg-mini .cg-row{padding:3px 8px;font-size:10px;" in _el
    # .cg-gd too: the mini's DEFAULT face is the simple galaxy, whose marks are .cg-gd, so a selector naming only
    # .cg-node picks records on the face the reader rarely has open
    assert "'.cg-mini .cg-node[data-id], .cg-mini .cg-gd[data-id], .cg-mini .cg-row[data-id]'" in src and "'.cg-mini [data-a=\"toggle\"][data-id]'" in src
    assert "_ctxCol.setFrames(CTX_FRAMES, {active});" in src and "let _ctxColFrameSig='';" in src   # ctx-graph-5: the frame in view is the active one