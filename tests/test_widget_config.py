"""
The widget config surface (UI redesign; the WidgetConfig, WidgetRegistry and Sizes boards; live defects 5 · 14 ·
15 · 18 · 19): window.VeraWidgetConfig lives in widget_element.js (the file every host loads) as the one sheet behind
every picker and every ⚙ - catalogue · record · live preview; every form has a SAMPLE face; the record carries the
board's keys (frame.dive, placement[], draw.proj) through widget_record.py; the registry panel draws the
WidgetRegistry board's cards and inspector over the same surface with nothing of the old panel removed.

Text-level over the js/html (the chain is held together by the strings these look for) and behaviour over the pure
record module (imported from the file). Runner: python _run.py test_widget_config (no pytest needed).
"""
import importlib.util
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _load_record():
    p = os.path.join(ROOT, "vera", "widgets", "widget_record.py")
    spec = importlib.util.spec_from_file_location("widget_record_cfg_test", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


EL = _read("vera", "widgets", "widget_element.js")
PANEL = _read("vera", "widgets", "widget_registry_panel.html")
R = _load_record()


# ── the surface, in the element file ─────────────────────────────────────────
def test_the_surface_is_exported_from_the_element_file_with_its_contract():
    assert "window.VeraWidgetConfig = { open: cfgOpen, close:" in EL
    assert "packs: PACKS.slice(), version: 2 }" in EL
    for key in ("mode", "record", "into", "title", "anchor", "sizes", "shape", "menuItems", "templates", "onChange", "ok"):
        assert re.search(r"opts\.%s\b" % re.escape(key), EL), key
    assert "Promise<record | null>" in EL


def test_the_three_columns_are_the_boards():
    # the catalogue, the record and the preview; the record's sections
    for cls in ("vwc-cat", "vwc-rec", "vwc-pvw", "vwc-cl", "vwc-rb", "vwc-stage", "vwc-pj", "vwc-ft"):
        assert "'" + cls + "'" in EL or '"' + cls + '"' in EL or "." + cls + "{" in EL, cls
    for sec in ("'Identity'", "'Source'", "'Frame'", "'Drawing'", "'Children'", "'Actions'", "'Placement'"):
        assert "sec(" + sec in EL, sec
    for row in ("'Title'", "'Form'", "'Shape'", "'Projection'", "'Skin'", "'Refresh'", "'Window'", "'Range'", "'Size'", "'Caption'", "'Legend'", "'Motion'", "'Deep dive'", "'Palette'", "'Bands'", "'Layout'"):
        assert "row(" in EL and row in EL, row


def test_the_catalogue_has_the_context_graph_entries_the_panels_and_the_templates():
    assert "Context galaxy · mini" in EL and "Context graph · full" in EL
    assert "{ id: 'context_graph', size: 'm'" in EL and "{ id: 'context_graph', size: 'xl'" in EL
    assert "group('Panels'" in EL and "group('Your templates'" in EL and "group('From the other menus'" in EL
    assert "group('Context graph'" in EL
    # the badges: projection · motion · live
    assert "'moves' : 'still'" in EL and "live ? 'live' : 'record'" in EL


def test_the_sheets_polish_names_counts_highlight_packs_range():
    # catalogue rows carry the BOARD's name with the id as the mono sub-label; the counts line; the highlight follows
    # every edit; the pack segment previews the skin on the sheet; Range is editable and rides in read.range
    assert "n: nm || f.id, sub: nm ? f.id : ''" in EL and "' forms · ' + live + ' with a live build'" in EL
    assert "if (rerender) { renderRec(); renderCat(); renderPacks(); }" in EL
    assert "const PACKS = [['inherit', 'Page'], ['standard', 'Standard'], ['newspaper', 'News'], ['terminal', 'Term'], ['pixel', 'Pixel']]" in EL
    assert "S.el.setAttribute('data-style', S.rec.skin)" in EL and "hd.appendChild(h('span', 'vwc-seg vwc-packs'))" in EL
    assert "rec.read.range = m ? [Number(m[1]), Number(m[2])] : null" in EL and "range: out.read.range || null" in EL
    # the element itself wears the record's skin and holds still on motion:false
    assert "if (skin) this.setAttribute('data-style', skin)" in EL and "data-motion=\"0\"" in EL
    # the composite reads its children (each a record; $subject bound to the composite's read) and draws per slot
    assert "async _readKids()" in EL and "$subject" in EL and "kids: this._kids || {}" in EL and "vw-slot-h" in EL


def test_the_sources_come_per_shape_and_fill_the_record_with_a_mapping():
    assert "'widget.sources'" in EL and "'widget.forms'" in EL and "'widget.validate'" in EL and "'widget.template.list'" in EL and "'widget.template.save'" in EL
    assert "s.shape === shape" in EL          # the chips are the sources of the record's shape
    assert "function pickSource(src)" in EL and "rec.read.args[a] = ''" in EL
    assert "SHAPE_FIELDS" in EL and "rec.read.map[k]" in EL


def test_the_preview_is_a_live_widget_at_the_chosen_size_with_the_json_under_it():
    assert "document.createElement('vera-widget')" in EL and "vw.setAttribute('size', size)" in EL and "vw.record = out" in EL
    assert "widget:rendered" in EL and "'sample · no source read'" in EL
    assert "pre.textContent = j" in EL


def test_keyboard_and_resolution():
    assert "ev.key === 'Escape'" in EL and "ev.key === 'Enter'" in EL
    assert "cfgClose(null)" in EL and "cfgClose(finalise(out, st.validated))" in EL and "validateDetached(st, out); cfgClose(out); return;" in EL   # OK never waits on widget.validate
    assert "' anyway'" in EL                  # a record with problems: shown, then added on a second press


def test_every_form_has_a_sample_face_and_the_element_draws_it():
    assert "function sample(x)" in EL and "sample, call, css" in EL
    for shape in ("level", "series", "values", "events", "graph", "items", "stages", "rate", "parts", "ohlcv", "matrix", "calendar", "string", "points", "panel", "composite"):
        assert re.search(r"^\s+%s: \(\) =>" % shape, EL, re.M), shape
    assert "return sampleFace(draw(f0, sample(f), size" in EL
    assert "data-sample=\"1\"" in EL
    # the element itself: the sample face when nothing was read (defect 19), the read button in the caption
    assert "const sampled = !wasRead && !have && form !== 'panel' && form !== 'composite';" in EL      # the sample only for a record never read
    assert "detail: { form, size, sample: sampled, empty: readEmpty, stale }" in EL
    # every catalogue form resolves to a renderer (defect 18)
    for f in ("threshold", "'small-multiples'", "box", "radar", "carousel", "terminal", "controls", "button", "header", "rail", "dial", "galaxy"):
        assert re.search(r"%s: '[a-z_]+'" % f, EL), f


def test_the_call_helper_opens_both_envelopes():
    assert "j.type === 'tool_result') ? j.content : (j && j.result !== undefined ? j.result" in EL
    assert "_call(name, args) { return call(this.base, name, args); }" in EL


# ── the record: the board's keys through widget_record.py ────────────────────
def test_the_record_carries_dive_placement_and_proj():
    r = R.normalise({"form": "radial", "source": "obs.pending", "title": "Gate", "frame": {"size": "m", "dive": False},
                     "draw": {"proj": "iso", "palette": "load", "bands": [60, 85]}, "placement": ["lhm", "canvas", "reply", "nowhere"]})
    assert r["frame"]["deep_dive"] is False and r["frame"]["dive"] is False
    assert r["projection"] == "iso" and "proj" not in r["draw"]
    assert r["placement"] == ["rail", "canvas", "chat"] and r["place"] == "rail"
    # the old keys still win when given
    r2 = R.normalise({"form": "radial", "frame": {"deep_dive": True}, "place": "ops", "projection": "flat"})
    assert r2["frame"]["dive"] is True and r2["place"] == "ops" and r2["placement"] == [] and r2["projection"] == "flat"


def test_palette_and_bands_are_every_forms_draw_keys():
    r, problems, warnings = R.validate({"form": "radial", "source": "obs.pending", "draw": {"palette": "kind", "bands": [60, 85], "glow": 1}})
    assert r["draw"]["palette"] == "kind" and r["draw"]["bands"] == [60, 85] and "glow" not in r["draw"]
    assert any("glow" in w for w in warnings) and not any("palette" in w or "bands" in w for w in warnings)


def test_to_template_keeps_the_placements():
    t = R.to_template({"form": "trace", "source": "sysmon.history", "title": "GPU", "placement": ["rail", "dashboard"]})
    assert t["placed"] == ["rail", "dashboard"]
    back = R.normalise(t)
    assert back["form"] == "trace" and back["source"] == "sysmon.history"


# ── the registry panel: the board's cards and inspector, nothing removed ──────
def test_the_registry_panel_draws_the_boards_cards_and_inspector():
    for s in ("Place ▾", "wrPlaceMenu(", "wrEdit(", "+ New widget…", 'class="c-h"', 'class="c-cfg"', 'class="c-pl"', 'class="c-ft"',
              'class="insp-h"', 'class="rec"', "The capability · widget.template.*", 'id="fil"', 'id="origins"', 'id="sorts"', 'id="q2"', 'id="pmenu"'):
        assert s in PANEL, s
    assert "S.open({ mode:'edit'" in PANEL and "S.open({ mode:'add'" in PANEL
    assert "function surface()" in PANEL and "window.parent" in PANEL


def test_nothing_of_the_old_panel_is_removed():
    for fn in ("wrLoad", "wrKind", "wrWhere", "shown", "wrList", "wrOpen", "wrCatalog", "wrPreview", "wrNewRecord", "wrNewPreview", "wrPlace", "wrRemove", "wrSaveCopy", "wrDelete", "wrNewToggle", "wrNewSave"):
        assert re.search(r"function %s\(" % fn, PANEL), fn
    for el in ('id="q"', 'id="kinds"', 'id="wheres"', 'id="list"', 'id="detail"', 'id="newForm"', 'id="rec"', 'id="nForm"', 'id="nPrev"', 'id="status"', 'id="count"', "+ New template"):
        assert el in PANEL, el
    assert '<script src="/ui/widgets/widget_element.js"></script>' in PANEL and '<script src="/ui/vera-ui.js"></script>' in PANEL
    assert "data-sz=" in PANEL              # the XS–XL preview bar stays
