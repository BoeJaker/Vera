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


def test_the_scene_gets_the_window_meter_when_it_mounts():
    # the board's ctxbar ends in the budget; the element is made when Explode opens, after the turn's updateCtxMeter
    src = _read("vera", "chat", "chat_panel.html")
    assert "let _ctxMeterLast={used:0,max:0};" in src
    assert "_ctxMeterLast={used:+used||0,max:+max||0}; try{ if(_xplEl&&typeof _xplEl.setBudget==='function') _xplEl.setBudget(used,max); }catch(_){}" in src
    assert "if(_ctxMeterLast.max>0&&typeof _xplEl.setBudget==='function') _xplEl.setBudget(_ctxMeterLast.used,_ctxMeterLast.max); }catch(_){}" in src


def test_the_card_text_is_the_words_and_the_element_script_is_retried():
    src = _read("vera", "chat", "chat_panel.html")
    # the exchange card carried the widget block's chrome ("chosen by aide · form: gauge · Pin to canvas…")
    assert "c.querySelectorAll('.think-throb,.think-box,.cap-dot,.wblk,.cap-inline,.mm-slot,.mermaid,vera-mermaid,vera-widget,pre,table,figure,.msg-actions,.pa-atts').forEach(x=>x.remove());" in src
    # a failed static load of the element's script left the scene "loading" for good
    assert "_xpl.loadTries=(_xpl.loadTries||0)+1;" in src and "s.src='/ui/exploded_element.js?r='+Date.now();" in src
    assert "could not load its element (/ui/exploded_element.js)" in src


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
    # the parameter list is not the claim - it gained a `quiet` flag so an incoming toggle from the OTHER chat
    # instance does not echo straight back out again
    assert "function _runsToggle(which, on" in src
    # the two draws now stand down for a menu over them as well as for the switch (Notes/42 defect 67), and the
    # switch is on each column's own header as well as in the sheet (defect 70) — one state, read by every switch
    assert "if(_runsOff.ctx||_runsBlocked())" in src
    assert "if(_runsOff.cv||_runsBlocked()){ svg.innerHTML=''; _cvLastCheck=null; return; }" in src
    assert "kk.textContent='Runs';" in src and "b.dataset.runs=k;" in src
    assert 'data-runs="cv" onclick="CH._runsToggle(\'cv\')"' in src
    assert 'data-runs="ctx" onclick="CH._runsToggle(\'ctx\')"' in src
    assert "_spFilter,_runsToggle" in src

    # A RUN HAS TWO HALVES AND THEY LIVE IN DIFFERENT PAGES. On the board the chat runs as two instances -
    # ?only=chat draws the run from the message, ?only=menu draws it from the graph - and _runsOff was read from
    # localStorage once into a const at load, so the switch only ever silenced the half belonging to whichever
    # instance held the button. The other went on drawing its end of a run that now went nowhere.
    assert "_lhmPost('runs', { which, off:_runsOff[which] })" in src, "the toggle is broadcast"
    assert "function _runsApply(which, off)" in src and "_runsToggle(which, !off, true)" in src, "and applied without echoing"
    assert src.count("m.act==='runs'") >= 2, "both instances act on it"
    assert "ev.key!=='vera_runs_off'" in src, "and a plain second tab hears it too"
    # the expanded graph carries the same switch, over the end of the run it draws
    assert "_ctxCol.addEventListener('vera:ctx:runs'" in src
