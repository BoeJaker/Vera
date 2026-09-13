"""
Explode is the whole chat column (the Canvas board: the canvas and the context graph are built into the scene): opening
it folds the session canvas column and the grown graph away and closing it brings back what was open; and the relation
runs — context → chat, chat ↔ canvas — can be switched off from the ⋯ tools sheet (persisted). Text-level.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def test_explode_folds_the_columns_and_restores_them():
    src = _read("vera", "chat", "chat_panel.html")
    assert "let _xplHid=null;" in src
    assert "if(on){ _xplHid={ canvas:_pages.has('canvas'), graph:_ctxGrown, remote:_ctxRemote.on }; if(_xplHid.graph) _ctxGrow(false); if(_xplHid.remote) _lhmPost('grow-set',{ on:false }); if(_xplHid.canvas){ _pages.delete('canvas'); _pagesApply(); } }" in src
    assert "else if(_xplHid){ const hid=_xplHid; _xplHid=null; if(hid.graph) _ctxGrow(true); if(hid.remote) _lhmPost('grow-set',{ on:true }); if(hid.canvas){ _pages.add('canvas'); _pagesApply(); } }" in src
    # the + Graph button follows the grown state
    assert "const gb=document.getElementById('colGraphBtn'); if(gb) gb.classList.toggle('col-on', on);" in src


def test_relation_runs_can_be_switched_off():
    src = _read("vera", "chat", "chat_panel.html")
    assert "localStorage.getItem('vera_runs_off')" in src
    assert "function _runsToggle(which, on)" in src
    assert "if(_runsOff.ctx){ svg.innerHTML=''; return; }" in src
    assert "if(_runsOff.cv){ svg.innerHTML=''; _cvLastCheck=null; return; }" in src
    assert "kk.textContent='Runs';" in src and "b.dataset.runs=k;" in src
    assert "_spFilter,_runsToggle" in src
