"""
The Explode scene redone as the Canvas board draws it (design landing, step 3): every band drawn on the iso plate, every
item the board's card on a stem (a widget an iso widget group with its caption, a context record an icon node), the galaxy on the
plate, a placed widget drawn as its form, Stack · Size,
Blocks off, the tip-in / flatten motion. Text-level; the layout itself is tested by test_exploded_iso_widgets.cjs.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "chat", "chat_panel.html")
EL = _read("vera", "chat", "exploded_element.js")


def test_the_element_draws_bands_and_the_boards_items_and_carries_stack_size_and_the_motion():
    # the iso's items are the Canvas board's: a card (.xit) on a stem, an iso widget group (.xig) with a frameless caption,
    # a typed icon node (.xnd) — no terminal-style frame; the layout is tested by tests/test_exploded_iso_labels.cjs
    for s in ("out.bands = []; out.widgets = []; out.stack = STK; out.wsz = WSZ;", "function widgetOf(c) {", "function groupOf(wg, ISO, o) {", "function isoBody(c, wd) {",
              "const xitHtml = (wg, face) =>", "const xigHtml = (wg) =>", '<span class="xstem"', '<div class="xit bb', '<div class="xit frameless', '<div class="xig', '<span class="xnd',
              "stack(on) {", "widgetSize(s) {", "tipIn() {", "flatten(done) {", "function ensureIso(doc, onload) {",
              "view: 'iso', full: false", 'data-a="stack"', 'data-a="wsz"', "widgetOf, groupOf, valueOf, isoBody, faceHtml, diagramHtml, ICON, version: 9"):
        assert s in EL, s
    for css in ("vera-exploded .xit{", "vera-exploded .xig{", "vera-exploded .xnd{", "vera-exploded .xstem{", "vera-exploded .xit.frameless{", "vera-exploded .xf-score{", "vera-exploded .xf-chart{", "vera-exploded .xf-diff{", "vera-exploded .xf-tab{",
                "vera-exploded .xp-band{", "vera-exploded .xp-band.empty{", ':root[data-blocks="off"] vera-exploded .xp-pl,', "vera-exploded.opening .xp-view{animation:xp-tip", "vera-exploded.closing .xp-view{animation:xp-flat",
                'vera-exploded[data-den="hover"] .xit:hover .xp-img', 'vera-exploded[data-den="zen"] .xit.open .xp-img'):
        assert css in EL, css
    assert "xp-if-hd" not in EL and "frameHtml" not in EL, "no terminal-style frame with a three-dot header"
    assert "a record the turn read" in EL and "return { form: 'bar'," in EL, "a read record stands as its relevance meter"
    # the items scale with the scene: the counter-scale follows the fit alone, set once per render, never by the pan zoom
    assert "out.inv = +(s * Math.min(1.45, 1 / Math.min(1, s))).toFixed(3);" in EL and "view.style.setProperty('--inv'" in EL
    pan = EL[EL.index("_applyPan() {"):EL.index("_click(e) {")]
    assert "--inv" not in pan


def test_the_host_tips_the_scene_in_flattens_it_and_lands_a_placed_widget_with_its_form():
    assert 'onclick="CH._xplToggle()"' in HTML and "function _xplToggle(){" in HTML and "function _xplClose(){" in HTML
    assert "_xplEl.flatten(()=>explode(false))" in HTML and "_xplEl&&_xplEl.tipIn&&_xplEl.tipIn();" in HTML
    assert "m.classList.add('xpl-back');" in HTML and "#msgs.xpl-back{animation:xpl-back" in HTML
    assert "_xplEl.addEventListener('vera:xpl:close', ()=>_xplClose());" in HTML
    assert "kind:'widget', tpl:tplName, key, form:r.form}" in HTML, "a placed widget carries its form"
    assert "made.push({n:el.getAttribute('title')||form||'widget', d:'widget · '+form, col:'#a78bfa', kind:'widget', form:form||'kv', data});" in HTML
    assert "_xplToggle,_xplClose," in HTML

def test_a_turn_selected_in_the_scene_drives_the_context_graphs_and_the_canvas():
    # Notes/42 defect 55: the chat's vera:xpl:turn hook syncs the grown graph at the turn's frame, re-draws the mini graph and
    # puts the turn's items in focus on the canvas; a turn's frame remembers its turn
    assert "try{ _ctxFollow(true); }catch(_){}" in HTML and "_ctxColSig=''; _ctxColumnSync(mid); }catch(_){}" in HTML   # the chat's own follow first (the mini's frame), then the grown sync
    assert "+':'+_ctxActiveFrame+':'+_grFocusMid;" in HTML   # the mini re-draws for a new focus (its turn label)
    assert "function _xplTurnReads(mid){" in HTML and "const rd=_xplTurnReads(mid); cur={mid," in HTML   # a turn without recorded reads takes its frame's
    assert "try{ if(window.VeraLHM) VeraLHM.render(); }catch(_){}\n      try{ const t=(_xplEl.state().scene.turns||[]).find(x=>x.mid===mid); _cvRelevance(mid, (t&&t.text)||'', {apply:false});" in HTML
    assert "_saveFrame('Turn '+HISTORY.filter(h=>h.role==='user').length,true,{ mid:" in HTML   # a turn's frame carries its mid (the ctx-graph slice), so the sync can activate it
    # the element emits the turn for every selection path through select(mid)
    assert "select(mid) { this._S.scene.sel = mid; this._schedule(); this.dispatchEvent(new CustomEvent('vera:xpl:turn'" in EL
    assert "solo(on) {" in EL and "tilt(deg) {" in EL and "swing(deg) {" in EL and 'data-a="tiltu"' in EL and 'data-a="solo"' in EL
