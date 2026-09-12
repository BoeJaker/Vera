"""
The Explode scene redone as the Canvas board draws it (design landing, step 3): every band drawn on the iso plate, every
item an iso widget on a stem with its caption, the galaxy on the plate, a placed widget drawn as its form, Stack · Size,
Blocks off, the tip-in / flatten motion. Text-level; the layout itself is tested by test_exploded_iso_widgets.cjs.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "chat", "chat_panel.html")
EL = _read("vera", "chat", "exploded_element.js")


def test_the_element_draws_bands_and_iso_widgets_and_carries_stack_size_and_the_motion():
    for s in ("out.bands = []; out.widgets = []; out.stack = STK; out.wsz = WSZ;", "function widgetOf(c) {", "function frameHtml(wg, H) {",
              "const iwHtml = (wg) =>", '<span class="xp-stem"', '<div class="xp-if', '<span class="xp-cap">', "stack(on) {", "widgetSize(s) {", "tipIn() {", "flatten(done) {",
              "view: 'iso', full: false", 'data-a="stack"', 'data-a="wsz"', "widgetOf, version: 3"):
        assert s in EL, s
    for css in ("vera-exploded .xp-iw{", "vera-exploded .xp-band{", "vera-exploded .xp-band.empty{", ':root[data-blocks="off"] vera-exploded .xp-pl,', "vera-exploded.opening .xp-view{animation:xp-tip", "vera-exploded.closing .xp-view{animation:xp-flat",
                'vera-exploded[data-den="hover"] .xp-iw:hover .xp-img,vera-exploded[data-den="zen"] .xp-iw.open .xp-img{display:block}'):
        assert css in EL, css
    assert "a record the turn read" in EL and "return { form: 'bar'," in EL, "a read record stands as its relevance meter"


def test_the_host_tips_the_scene_in_flattens_it_and_lands_a_placed_widget_with_its_form():
    assert 'onclick="CH._xplToggle()"' in HTML and "function _xplToggle(){" in HTML and "function _xplClose(){" in HTML
    assert "_xplEl.flatten(()=>explode(false))" in HTML and "_xplEl&&_xplEl.tipIn&&_xplEl.tipIn();" in HTML
    assert "m.classList.add('xpl-back');" in HTML and "#msgs.xpl-back{animation:xpl-back" in HTML
    assert "_xplEl.addEventListener('vera:xpl:close', ()=>_xplClose());" in HTML
    assert "kind:'widget', tpl:tplName, key, form:r.form}" in HTML, "a placed widget carries its form"
    assert "made.push({n:el.getAttribute('title')||form||'widget', d:'widget · '+form, col:'#a78bfa', kind:'widget', form:form||'kv', data});" in HTML
    assert "_xplToggle,_xplClose," in HTML
