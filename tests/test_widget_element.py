"""
The widget element's wiring (UI redesign M2): it is served; the chat's reply
block draws through it (and keeps its own renderers as the fallback); the
registry panel loads it and binds the record sheet and the new-template form to
a live preview; VeraDash takes a record tile beside its panel tiles and keeps
every mechanic it had. The files are text, so this runs anywhere.
"""
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "chat", "chat_panel.html")
EL = _read("vera", "widgets", "widget_element.js")
DASH = _read("vera", "chat", "vera-dashboard.js")
PANEL = _read("vera", "widgets", "widget_registry_panel.html")
UI = _read("vera", "ui builder", "ui_capabilities.py")


def test_the_element_is_served_and_exposes_one_renderer():
    assert '@APP.get("/ui/widgets/widget_element.js", include_in_schema=False)' in UI
    assert 'Path(__file__).parent.parent / "widgets" / "widget_element.js"' in UI
    assert "customElements.define('vera-widget', VeraWidgetEl)" in EL
    assert "window.VeraWidget = { draw, forms, normalise, formByShape, dataFor, applyMap, pick, mapped, formFor, readable, key, hydrate, sample, call, css: () => CSS, ensureCss," in EL
    # the compositions the Sizes board names
    for s in ("'xs'", "'s'", "'m'", "'l'", "'xl'"):
        assert s in EL
    assert "widget:refresh" in EL and "widget:resize" in EL and "widget:rendered" in EL
    assert "const REFRESH_FLOOR = 10;" in EL, "never tighter than ten seconds"


def test_the_chat_draws_its_reply_blocks_through_the_element():
    assert '<script src="/ui/widgets/widget_element.js"></script>' in HTML
    d = HTML[HTML.index("function _wDraw(form, data, hpx){"):HTML.index("function _wFormFor(")]
    assert "VeraWidget.draw(form, data, sz, {height:H, bare:true})" in d and "form!=='pipes'" in d
    assert "if(form==='trace'){" in d, "the chat's own renderers stay as the fallback"
    s = HTML[HTML.index("function _widgetFormByShape(x, depth){"):HTML.index("function _widgetData(")]
    assert "return VeraWidget.formByShape(x);" in s
    assert "VeraWidget.ensureCss(document)" in HTML


def test_the_registry_panel_previews_from_the_real_forms():
    assert '<script src="/ui/widgets/widget_element.js"></script>' in PANEL
    assert 'id="prev"' in PANEL and "wrPreview(t, document.getElementById('prev')" in PANEL
    assert "el.record = t;" in PANEL and "document.createElement('vera-widget')" in PANEL
    assert "wrCatalog(nf);" in PANEL and "api('/ui/widgets/forms')" in PANEL
    assert 'id="nPrev"' in PANEL and "addEventListener('input', wrNewPreview)" in PANEL
    # nothing removed: list · record · Place into · instances · save a copy · delete · new template
    for piece in ("function wrList()", "async function wrOpen(", "Place into", "async function wrPlace(", "async function wrRemove(", "async function wrSaveCopy(", "async function wrDelete(", "async function wrNewSave("):
        assert piece in PANEL, piece


def test_veradash_takes_a_record_tile_and_keeps_every_mechanic():
    assert "function addRecord(record, o2) {" in DASH
    assert "document.createElement('vera-widget')" in DASH and "el.setAttribute('size', 'auto');" in DASH
    assert "state.dynamic[wid] = { record: record, wid: wid };" in DASH
    assert "if (info && info.record) { addRecord(info.record, { silent: true, wid: wid }); return; }" in DASH
    assert "addWidget: addWidget, addRecord: addRecord, refresh: applyLayout" in DASH
    assert DASH.count("data-vera-widget-el") == 2, "the element script is injected once"
    for fn in ("function init(grid, opts)", "function save()", "function load()", "function onDragStart(e)", "function onDragOver(e)", "function onDrop(e)",
               "function onResizeDown(e)", "function snap(n, lo, hi, list)", "function applySize(clientX, clientY)", "function autoScrollTick()",
               "function hide(wid)", "function show(wid)", "function renderHidden()", "function applyLayout()", "function toggleEdit()", "function reset()",
               "function addPopButtons(w)", "function popToggle(w)", "function soloRequest(w)", "function floatW(w)", "function dock(w)", "function popWindow(w)",
               "function wireWidget(w)", "function openLoader()", "function addWidget(panelId, o2)", "function removeDynamic(wid)", "function restoreDynamic()",
               "function fetchPanels()", "function renderLoader(q)", "function buildDoc(full)"):
        assert fn in DASH, fn + " still defined"
    assert "window.VeraDash = { init: init };" in DASH
