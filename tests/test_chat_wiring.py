"""
The chat wiring pass (UI redesign control-plane carry-overs, chat side): the
lhm.compose directive composes through VeraLHM.compose with the vocabulary's
{menu, widgets} as the library's spec; Save as menu... stores through
lhm.menu.save and the saved menus come back on mount; the Driven ribbon shows
on a panel the aide opened beside the chat, with undo through the directive log
and a way to the Driven panel; the inline shorthand ⟦name {args}⟧ is lifted
into a ui.directive call, badged while streaming, and told to the model. The
file is text, so this runs anywhere.
"""
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "chat", "chat_panel.html")


def _fn(name):
    m = re.search(r"\n  (?:async )?function %s\(" % re.escape(name), HTML)
    assert m, "no function " + name
    nxt = re.search(r"\n  (?:async )?function \w+\(|\n  /\* ═", HTML[m.end():])
    return HTML[m.start():m.end() + (nxt.start() if nxt else len(HTML))]


def test_lhm_compose_composes_through_the_library():
    d = _fn("_uiDirectiveApply")
    c = d[d.index("case 'lhm.compose': {"):d.index("case 'lhm.compose.undo':")]
    assert "VeraLHM.compose({menu:String(a.menu||''), label:a.label, icon:a.icon, add, remove:" in c
    assert "typeof w==='string'?{id:w,label:w}:w" in c, "a widget may be a tab id"
    assert "edit mode is on for you to confirm" in c
    assert "VeraLHM.composeUndo()" in d


def test_saved_menus_store_and_come_back():
    m = _fn("_lhmMount")
    assert "saveMenu:(rec)=>{ _capCall('lhm.menu.save',{menu:rec, session_id:SID||''})" in m
    assert "VeraLHM.saveAsMenu(n)" in m and "'⧉ Save as menu…'" in m
    assert "_capCall('lhm.menu.list',{session_id:SID||''}).then(r=>{ if(r&&r.ok&&Array.isArray(r.menus)&&r.menus.length) VeraLHM.addMenus(r.menus); })" in m
    assert "VeraLHM.mount({host, title:'Vera', tabBar:'.ctx-tab-bar', menus:LHM_MENUS," in m, "the mount call is the one it was"


def test_the_driven_ribbon_on_a_panel_the_aide_opened():
    assert 'id="chatPanelRibbon" class="drv-ribbon"' in HTML and ".drv-ribbon{" in HTML
    assert "try{ _drvRibbon(p.id, _panelOpenedBy||'you'); }catch(_){}" in _fn("panelOpen")
    rb = _fn("_drvRibbon")
    assert "const driven=by&&by!=='you'&&by!=='user';" in rb and "_capCall('ui.policy.get'" in rb
    u = _fn("_drvUndo")
    assert "x.name==='panel.open'&&x.outcome==='applied'&&((x.args||{}).id===pid)" in u and "_capCall('ui.directive.undo',{session_id:SID||'', row_id:row.id})" in u
    assert "function _drvLog(){ panelOpen('driven','you'); }" in HTML
    assert "if(rb) rb.style.display='none'" in _fn("panelClose")


def test_the_inline_directive_shorthand():
    assert "const DIRECTIVE_INLINE_RE=/\\u27e6\\s*([a-z][a-z0-9_.]+)\\s*(\\{[\\s\\S]*?\\})?\\s*\\u27e7/g;" in HTML
    f = _fn("_findAllPlaceholders")
    assert "matches.push({idx:m.index,end:m.index+m[0].length,type:'cap',groups:['ui.directive',JSON.stringify({name:m[1],args})]});" in f
    s = _fn("_renderStreamingMarkdown")
    assert "_streamBadge('cap','ui.directive '+n)" in s and "t.lastIndexOf('\\u27e6')" in s
    assert "⟦panel.open {\"id\":\"exec-panel\"}⟧" in _fn("_studioHint")
    for name in ("_drvRibbon", "_drvUndo", "_drvLog", "_findAllPlaceholders", "_renderStreamingMarkdown"):
        assert re.search(r"\n    [^\n]*\b%s," % name, HTML), name + " exported"
