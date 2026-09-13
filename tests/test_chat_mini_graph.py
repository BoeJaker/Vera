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


def test_the_mini_is_the_elements_face():
    src = _read("vera", "chat", "chat_panel.html")
    assert "if(window.VeraContextGraph&&typeof VeraContextGraph.miniHtml==='function'){" in src
    assert "allEdges:_qGalAll, off:Object.keys(_qGalOff).filter(k=>_qGalOff[k])" in src
    assert "gal=VeraContextGraph.miniHtml(st, 262, miniH);" in src
    # the widget renderer stays the fallback
    assert "if(!gal) gal=window.VeraWidget?VeraWidget.draw('context_graph',data,'m'" in src


def test_the_minis_detail_list_search_and_frames():
    src = _read("vera", "chat", "chat_panel.html")
    assert "let _qGalSel='', _qGalList=false, _qGalQ='';" in src
    assert 'class="gal-all gal-list' in src and 'class="gal-srch"' in src
    assert "'.cg-mini .cg-node[data-id], .cg-mini .cg-row[data-id]'" in src and "'.cg-mini [data-a=\"toggle\"][data-id]'" in src
    assert "_ctxCol.setFrames(CTX_FRAMES, {active:null});" in src and "let _ctxColFrameSig='';" in src