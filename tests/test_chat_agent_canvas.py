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
    assert "const form=known?draw:'table';" in src and "draw:{form:form, size:" in src


def test_a_follow_ups_calls_run_under_the_same_mode():
    # mirror web1.png: the second web.research (issued after the first's result) was staged with Run/Skip under Auto-act
    src = _read("vera", "chat", "chat_panel.html")
    fn = src[src.index("async function continueConversation("):src.index("async function _webSearch(")]
    assert "const chainAuto=!!document.getElementById('cfgCapAutoExec')?.checked;" in fn
    assert "await processCapPlaceholders(txt||'',targetBody,chainAuto);" in fn and "processCapPlaceholders(txt||'',targetBody,false)" not in fn


def test_a_lifted_canvas_directive_is_shaped_and_anchored_to_the_turn():
    # mirror a42i.png: the items the model placed landed PARKED (no anchor → the relevance pass parked them); the chips were harvested
    src = _read("vera", "chat", "chat_panel.html")
    assert "const _shapeCanvasDirective=(name,a)=>{ if(/^canvas\\.(add|show)$/.test(name)&&a&&typeof a==='object'){ try{ if(name==='canvas.add') _cvDirectiveShape(a); if(!a.anchor&&_cvRelMid) a.anchor={mid:_cvRelMid, turn:_cvRelMid}; }catch(_){} } };" in src
    assert src.count("_shapeCanvasDirective(") == 2, "the lift and the shorthand both shape"
    assert "if((el.classList.contains('drv')||/^canvas\\./i.test(n)||" in src


def test_the_canvas_column_carries_the_driven_ribbon():
    # the Driven board: a chip in the message, a RIBBON on the surface it drove, a row in the log — the canvas column
    # (the surface the model drives most) had none
    src = _read("vera", "chat", "chat_panel.html")
    assert 'id="cvDrvRibbon" class="drv-ribbon"' in src and 'data-w="driven ribbon · canvas"' in src
    assert "function _cvDrvRibbon(res){" in src and "async function _cvDrvUndo(){" in src
    assert "try{ _cvDrvRibbon(content); }catch(_){} }" in src, "the chip and the ribbon come from one answer"
    assert "_drvUndoRow,_cvDrvRibbon,_cvDrvUndo" in src
    # and it is BOUNDED where it sits, so a long "driven by" cannot push the column's own controls off the row.
    # Pinned by the claim rather than by one selector: the ribbon moved from the column's own header row into the
    # canvas element's banner when the two rows became one (the canvas's final form §4.2).
    assert ".cv-bn .drv-ribbon{max-width:46%;overflow:hidden}" in src
    assert 'slot="banner-start"' in src and 'id="cvDrvRibbon"' in src.split('slot="banner-end"')[0], \
        "the ribbon rides with the canvas's name, not with its controls"


def test_a_directive_lands_as_the_boards_chip_with_undo():
    # mirror a42h.png: each ui.directive landed as a JSON dump; the items landed PARKED; the receipts were harvested
    src = _read("vera", "chat", "chat_panel.html")
    assert "if(capName==='ui.directive'&&content&&typeof content==='object'&&content.row){ try{ el.innerHTML=_drvChipHtml(content, resultId, previewClean); el.classList.add('drv'); }catch(_){} try{ _cvDrvRibbon(content); }catch(_){} }" in src
    assert "function _drvChipHtml(res, resultId, raw){" in src and "async function _drvUndoRow(rowId, btn){" in src
    assert "args.content=c; if(!args.at) args.at='now';" in src
    assert "/canvas\\./.test(n)))&&!el.classList.contains('error')) return;" in src
    assert ".cap-inline.drv{padding:4px 8px;border-left:2px solid var(--acc3,#d4a96a)}" in src


def test_a_vocabulary_call_is_lifted_into_ui_directive():
    # the Control board: one vocabulary, one dispatcher — a model's [[canvas.add {…}]] ran as a bare cap and left no log row
    src = _read("vera", "chat", "chat_panel.html")
    assert "let _DIR_VOCAB=new Set(['panel.open','panel.dispatch','panel.query','panel.close','canvas.show','canvas.add'" in src
    assert "async function _dirVocabLoad(){ try{ const r=await _capCall('ui.directive.vocab',{});" in src
    scan = src[src.index("function _findAllPlaceholders(text){"):src.index("function hasCapPlaceholders(text){")]
    assert "const liftDirectives=()=>{ matches.forEach(mt=>{ if(mt.type!=='cap'||!mt.groups||mt.groups[0]==='ui.directive'||!_isDirective(mt.groups[0])) return;" in scan
    assert "matches.length=0; deduped.forEach(x=>matches.push(x)); liftDirectives();" in scan


def test_a_canvas_calls_receipt_is_not_harvested_as_an_item():
    # mirror a42g.png: the model's canvas.append put its items on the canvas AND the harvest lifted each receipt card too
    src = _read("vera", "chat", "chat_panel.html")
    assert "/canvas\\./.test(n)))&&!el.classList.contains('error')) return;" in src


def test_a_fenced_block_is_content_not_a_call():
    # defect 68: a ```mermaid fence was unwrapped before the scan and its text — A[[node.parse(x)]], mermaid's subroutine
    # shape — matched a cap spelling, so the call ran and the drawn diagram was replaced by prose and a capability card
    src = _read("vera", "chat", "chat_panel.html")
    assert ".replace(/```[a-z]*\\n?([\\s\\S]*?)```/g,(m,inner)=>/^\\s*(?:\\[\\[|\\u27e6)[\\s\\S]*(?:\\]\\]|\\u27e7)\\s*$/.test(inner)?inner:m)" in src
    assert "const fences=[]; { const fre=/```[\\s\\S]*?```/g; let fm; while((fm=fre.exec(text))!==null) fences.push([fm.index, fm.index+fm[0].length]); }" in src
    assert "if(inFence(match.idx)||isNodeShape(match.idx)) continue;" in src


def test_mermaid_node_shape_is_not_a_call_even_unfenced():
    # the user's correction: the fence is not what triggers it, the diagram CONTENT is. mermaid writes a subroutine node
    # as id[[text]] — the id abuts the bracket — so a [[ glued to an identifier is read as that shape wherever it sits,
    # including a fence the stream has not closed yet (which is what a diagram mid-render is)
    src = _read("vera", "chat", "chat_panel.html")
    assert "const isNodeShape=(i)=>text.slice(i,i+2)==='[[' && i>0 && /[A-Za-z0-9_]/.test(text[i-1]);" in src
    assert "if(tail>=0 && !fences.some(f=>tail>=f[0]&&tail<f[1])) fences.push([tail, text.length]);" in src


def test_a_bare_cap_call_with_json_runs():
    # mirror a42f.png: [[canvas.add {"kind":…}]] — no cap: prefix, no parentheses — rendered as raw text
    src = _read("vera", "chat", "chat_panel.html")
    assert "const CAP_BARE_SPACE_RE=/\\[\\[([a-z][a-z0-9_]*(?:\\.[a-z0-9_.]+)+)\\s+(\\{[\\s\\S]*?\\})\\s*\\]\\]/gi;" in src
    assert "const reBs=new RegExp(CAP_BARE_SPACE_RE.source,'gi');" in src
    gate = src[src.index("function hasCapPlaceholders(text){"):src.index("function _stripPanelMarkup(text){")]
    assert "|[a-z][a-z0-9_]*\\.[a-z0-9_.]+\\s+\\{)/.test(t)" in gate


def test_a_reply_of_directives_alone_runs():
    # mirror a42d.png: the model wrote two ⟦canvas.add …⟧ and they sat in the reply as raw text — the gate only knew [[…]]
    src = _read("vera", "chat", "chat_panel.html")
    gate = src[src.index("function hasCapPlaceholders(text){"):src.index("function _stripPanelMarkup(text){")]
    assert "|| /\\u27e6\\s*[a-z][a-z0-9_.]+/.test(t);" in gate
    assert ".replace(/`(\\u27e6[^\\u27e7]*\\u27e7)`/g,'$1');" in src
    assert "else if(!/^[a-z]+:/.test(String(args.key))) args.key=kind+':'+slug(args.key);" in src


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
    # the harvest is a table of hooks now, so the rule is asserted by what it looks for and what it reads, not by
    # the shape of the loop that used to hold it
    assert "cvHook('widget', '.wblk[id^=\"w\"]'" in src, "a widget block is a hook"
    assert "const w=_W_STORE[el.id];" in src, "and it reads the record the block was drawn from"
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


def test_the_reply_record_says_its_renderer_form():
    # the iso plate drew a lifted gauge as "a panel widget…": the chat's record kept form = the KIND; form is the renderer everywhere else
    src = _read("vera", "chat", "chat_panel.html")
    assert "const form=known?draw:'table';" in src
    assert "form, kind:_W_KIND[form]||'panel', draw:{form:form, size:_W_SIZE[size]?size:'m'" in src
    assert "w.rec.form=w.rec.draw.form; w.rec.kind=_W_KIND[w.rec.draw.form]||'panel';" in src
    assert "if(w.rec.reads&&w.rec.reads.cap) delete content.data;" in src
    assert "kind:'widget', key, form:w.rec.draw.form, record:w.rec}); if(_xpl.on) _xplRefresh();" in src
    # Pin anchors to the turn (the question's mid): a reply row's pin used to key the landed layer by the reply's own mid
    assert "function _turnMidOf(wrap){" in src and "const mid=_turnMidOf(btn&&btn.closest('.mwrap'));" in src


def test_canvas_draws_a_record_carried_in_the_content():
    src = _read("vera", "canvas", "canvas_element.js")
    assert "const rec = isRec(c) ? c : (c && isRec(c.record) ? Object.assign({}, c.record, c.title ? { title: c.title } : {}) : null);" in src   # w85: a content with a form is a record too
