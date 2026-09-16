"""
The mini context graph is the element's (Notes/42 defects 47 · 48 · 30): the quick menu's galaxy is drawn by
VeraContextGraph.miniHtml from the state the grown graph holds (All edges and the families passed through), a record
picked opens its compact card, the card's include/exclude toggles the record, a List toggle opens the records drawer
with search; the frames history reaches the grown graph (setFrames). Text-level.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def test_the_mini_has_both_faces_and_the_quick_menu_switches_them():
    """Notes/42 defect 60: the user likes what the mini grew into on this edge and wants the board's face as well."""
    el = _read("vera", "chat", "context_graph_element.js")
    # the element draws whichever face it is handed; the switch is the host's
    assert "const simple = String(opts.style || S.miniStyle || 'detailed').toLowerCase() === 'simple';" in el
    # simple is the CONTEXT plot: the other families are folded away, not drawn small
    assert "mix: Object.assign({}, S.mix || {}, { memory: 'off', loop: 'off', plan: 'off', estate: 'off' })" in el
    # and nothing else is laid over it - no lanes, no list, no record card
    assert "if (simple) return '<div class=\"cg-mini simple\"" in el
    assert "const detail = simple ? null :" in el and "list = simple ? false :" in el

    src = _read("vera", "chat", "chat_panel.html")
    assert "let _qGalStyle=" in src and "localStorage.getItem('vera_ctx_mini_style')" in src
    assert "VeraContextGraph.miniHtml(st, 262, miniH, { style:_qGalStyle })" in src
    assert 'data-style="' in src and "localStorage.setItem('vera_ctx_mini_style', _qGalStyle)" in src
    # the view buttons keep their own handler now that a second segment shares the row
    assert "el.querySelectorAll('.vseg button[data-view]')" in src
    # and the face is part of what decides a repaint
    assert "'#'+_qGalStyle+_qGalSel" in src


def test_the_mini_is_the_elements_face():
    src = _read("vera", "chat", "chat_panel.html")
    assert "if(window.VeraContextGraph&&typeof VeraContextGraph.miniHtml==='function'){" in src
    assert "allEdges:_qGalAll, off:Object.keys(_qGalOff).filter(k=>_qGalOff[k])" in src
    assert "gal=VeraContextGraph.miniHtml(st, 262, miniH, { style:_qGalStyle });" in src   # the face it is drawn in (defect 60)
    # the widget renderer stays the fallback
    assert "if(!gal) gal=window.VeraWidget?VeraWidget.draw('context_graph',data,'m'" in src


def test_the_minis_detail_list_search_and_frames():
    src = _read("vera", "chat", "chat_panel.html")
    assert "let _qGalSel='', _qGalList=false, _qGalQ='';" in src
    assert 'class="gal-all gal-list' in src and 'class="gal-srch"' in src
    assert "'.cg-mini .cg-node[data-id], .cg-mini .cg-row[data-id]'" in src and "'.cg-mini [data-a=\"toggle\"][data-id]'" in src
    assert "_ctxCol.setFrames(CTX_FRAMES, {active});" in src and "let _ctxColFrameSig='';" in src   # ctx-graph-5: the frame in view is the active one