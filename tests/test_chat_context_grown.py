"""
The expanded context is the chat's LHM grown in place (the user: it is part of chat's LHM, not a separate menu):
"Expand to the full graph →" widens the Context quick menu into the full context graph in the same panel; the CTA
folds it back; + Graph and every old caller of the graph page grow the menu; the runs reach the message from the
grown panel. Text-level.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "chat", "chat_panel.html")


def test_the_graph_page_grows_the_context_menu_in_place():
    assert "function _ctxGrow(on){" in HTML and "host.classList.toggle('lhm-grown', on); document.body.classList.toggle('ctx-grown', on);" in HTML
    assert "if(name==='graph') return _ctxGrow();" in HTML, "+ Graph and every caller of the graph page grow the menu"
    assert "body.dataset.cols=_pagesOpen().filter(n=>n!=='graph').join(' ');" in HTML, "the graph is never a column"
    assert "function _ctxGraphHost(){ return document.getElementById('ctxGrown') || document.getElementById('graphColumnBody'); }" in HTML
    assert "cta:{get label(){ return _ctxGrown?'Fold back to the quick menu ←':'Expand to the full graph →'; }, run:()=>_ctxGrow()}}," in HTML
    assert "_ctxCol.addEventListener('vera:ctx:collapse', ()=>{ _ctxGrow(false);" in HTML
    assert "#rightRail.lhm-host.lhm-grown > .lhm-det{width:min(46vw,680px)" in HTML and "body.ctx-grown #ctxRunsOverlay,body.ctx-remote #ctxRunsOverlay{display:block;position:fixed" in HTML
    assert "gcol=_ctxGrown?document.querySelector('#rightRail.lhm-host > .lhm-det'):document.getElementById('graphColumn')" in HTML, "the runs start at the grown panel"
    assert 'id="graphColumn"' in HTML and 'id="graphColumnBody"' in HTML, "the old column stays as a mount host"
