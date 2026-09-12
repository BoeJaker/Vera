"""
Loops write to the canvas (UI redesign, Notes/40 §6 P7): a run is an item with its
steps. The chat puts every loop it runs or watches on the session canvas as one
keyed item and keeps it current from the loop's own events; <vera-canvas> draws
the loop item. Text-level (the resolver's keyed update runs for real in
tests/test_canvas_resolver.py).
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


def _fn(name):
    i = HTML.index("function %s(" % name)
    j = HTML.find("\n  function ", i + 10)
    return HTML[i:j if j > 0 else i + 20000]


def test_the_chat_puts_every_loop_on_the_session_canvas_and_keeps_it_current():
    tee = _fn("_ctxColLoopEv")
    assert "_ctxCol.appendLoopEvent(ev);" in tee and "try{ _loopCanvasEv(ev); }catch(_){}" in tee, "the same stream the loop card and the graph column read"
    ev = _fn("_loopCanvasEv")
    assert "const rid=String(ev.run_id||ev.stream_id||ev.session_id||'run').slice(0,80);" in ev and "key:'loop:'+rid" in ev
    assert "R.status=(ev.ok===false||/error|failed|cancelled/.test(t))?'fail':'ok';" in ev
    assert "_loopCanvasT[rid]=setTimeout(()=>_loopCanvasFlush(rid), R.added?1500:50);" in ev, "added on the first event, then debounced updates"
    fl = _fn("_loopCanvasFlush")
    assert "_capCall('canvas.add',{session_id:SID, kind:'loop', key:R.key, content, at:'now', size:'m', anchor:{mid:R.mid, turn:R.mid}})" in fl
    assert "await _capCall('canvas.update',{session_id:SID, key:R.key, content});" in fl
    assert "(_TURN_LAND[R.mid]=_TURN_LAND[R.mid]||[]).push(" in fl, "the run lands in the turn's exploded station too"
    assert "VeraContextGraph.loopFromEvents(R.evs).steps.map(s=>({n:s.label, cap:s.cap, status:s.status, ms:s.ms}))" in _fn("_loopRunContent"), "the steps from the one loop adapter"
    assert "try{ _loopCanvasMid=(statusCard&&statusCard.mid)||''; }catch(_){}" in HTML, "anchored to the turn that launched it"
    assert re.search(r"\n    [^\n]*\b_loopCanvasEv,", HTML)


def test_the_element_draws_a_loop_item_and_the_resolver_knows_the_type():
    assert "loop: c => {" in EL and 'class="vc-steps"' in EL and "c.goal || c.title || 'agentic loop'" in EL
    assert "const title = c.title || c.name || c.goal ||" in EL
    assert '"loop":     {"desc": "An agentic run as an item' in PY and '"loop": "goal"' in PY
    assert 'key: str = "", session_id: str = "", trace_id=None):' in PY and "rev = await _write(doc, \"update\", key)" in PY, "a keyed update is a write like any other"
