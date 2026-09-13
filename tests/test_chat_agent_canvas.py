"""
The chat agent puts widgets, tables and diagrams on the SESSION canvas (Notes/42 defect 42): the STUDIO hint names the
session canvas and every registry form, and gives the ⟦canvas.add⟧ shapes; the directive normalises a widget's content
into a record and shapes a diagram/table/note given as a string; the fence's record builder accepts any registry form;
the harvest lifts the reply's widget blocks with their record. Text-level.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def test_hint_names_the_session_canvas_and_the_directive_shapes():
    src = _read("vera", "chat", "chat_panel.html")
    assert "THE SESSION CANVAS (the column beside this chat" in src
    assert "WRITE IT IN THE REPLY as that fence/table" in src
    assert '⟦canvas.add {"kind":"widget","key":"widget:gpu","content":{"form":"gauge","title":"GPU","data":{"value":62,"min":0,"max":100,"unit":"%"}}}⟧' in src
    assert '⟦canvas.add {"kind":"table","key":"table:hosts","content":{"columns":["host","cpu %"],"rows":[["a",12],["b",40]],"caption":"…"}}⟧' in src
    assert '⟦canvas.add {"kind":"diagram","key":"diagram:boot","content":{"title":"Boot","mermaid":' in src
    # the document canvas stays, second, for a board that outlives the session
    assert "A pinned canvas DOCUMENT (a board that outlives this session" in src
    assert src.index("THE SESSION CANVAS") < src.index("A pinned canvas DOCUMENT")


def test_hint_lists_every_registry_form():
    src = _read("vera", "chat", "chat_panel.html")
    assert "\"form\": \"<one of: '+_widgetForms().join('|')+'>\"" in src
    assert "function _widgetForms(){ try{ if(window.VeraWidget&&typeof VeraWidget.forms==='function')" in src
    assert "const known=_W_DRAWS.includes(draw)||_widgetFormKnown(draw);" in src
    assert "draw:{form:known?draw:'table'" in src


def test_directive_shapes_its_content():
    src = _read("vera", "chat", "chat_panel.html")
    assert "if(name==='canvas.add') _cvDirectiveShape(args);" in src
    assert "function _cvDirectiveShape(args){" in src
    assert "const rec=_widgetRecord(src,'aide'); const form=rec.draw.form;" in src
    assert "c=Object.assign({}, rec, {widget:form, form, title, record:rec});" in src
    assert "else if(kind==='diagram'){ if(typeof c==='string') c={mermaid:c};" in src
    assert "function _cvTableFromMd(md){" in src
    assert "if(!args.key){" in src


def test_harvest_lifts_widget_blocks_with_their_record():
    src = _read("vera", "chat", "chat_panel.html")
    assert "body.querySelectorAll('.wblk[id^=\"w\"]').forEach((el,i)=>{ const w=_W_STORE[el.id];" in src
    assert "content:Object.assign({}, rec, {widget:form, form, title, record:rec}), col:'#a78bfa', k:'widget'}); });" in src


def test_grown_graph_events_reach_the_chat():
    # agent G's slice 4: the element emits All edges / Incl-Excl all / Preview page; the chat acts, and the mini's hooks too
    src = _read("vera", "chat", "chat_panel.html")
    assert "_ctxCol.addEventListener('vera:ctx:alledges', ev=>{ _qGalAll=!!(ev.detail||{}).on;" in src
    assert "_ctxCol.addEventListener('vera:ctx:toggle-all', ev=>{ const on=!!(ev.detail||{}).included; CTX_NODES.forEach(n=>{ n.included=on; });" in src
    assert "_ctxCol.addEventListener('vera:ctx:preview', ev=>{ const u=String((ev.detail||{}).url||''); if(u) try{ _browserNavigate(u); }catch(_){} });" in src
    assert ".cg-mini [data-a=\"incl-all\"], .cg-mini [data-a=\"excl-all\"]" in src
    assert ".cg-mini [data-a=\"preview\"][data-url]" in src
    assert "_qGalAll=!_qGalAll; try{ if(_ctxCol&&typeof _ctxCol.allEdges==='function') _ctxCol.allEdges(_qGalAll); }catch(_){} VeraLHM.render(); });" in src


def test_canvas_draws_a_record_carried_in_the_content():
    src = _read("vera", "canvas", "canvas_element.js")
    assert "const rec = c && c.draw ? c : (c && c.record && c.record.draw ? Object.assign({}, c.record, c.title ? { title: c.title } : {}) : null);" in src
