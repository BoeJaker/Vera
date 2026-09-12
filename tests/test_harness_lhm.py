"""
The harness is the home of the one LHM (UI redesign, Notes/40 §9 and the
composable-menus section; the Harness board). vera-lhm.js carries the
top-level side menu and the tab-mode strips; the harness's vertical menu is
built from them; absorb (the chat's menu in the same place) stays. The files
are text, so this runs anywhere.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


LIB = _read("vera", "chat", "vera-lhm.js")
HARNESS = _read("vera", "capability_orchestration.html")


def _once(text, needle):
    assert text.count(needle) == 1, "%r expected once, found %d" % (needle, text.count(needle))


def test_the_library_renders_the_side_menu_and_the_strips():
    assert "function side(host, cfg){" in LIB and "function strips(host, cfg){" in LIB
    assert "side: side, sideEdit: sideEdit, strips: strips," in LIB, "exported"
    # the Harness board's parts: search · Open now · the panels with their sections · N registered · widgets · note
    for part in ("'lhm-s-srch'", "'Open now · ' + open.length + ' · one set, one bridge'", "'lhm-s-grp', 'Panels'", "' registered · ⌘K'", "'lhm-s-grp', 'Widgets'", "'lhm-s-note'"):
        assert part in LIB, part
    assert "cfg.onPanel(p.id, ev)" in LIB and "cfg.onSection(p.id, sec.id, i)" in LIB and "cfg.onTab(p.id, sec, t)" in LIB
    for cls in (".lhm-side{", ".lhm-side .lhm-s-row{", ".lhm-side .lhm-s-sec{", ".lhm-side .lhm-s-opt{", ".lhm-side .lhm-s-w{", ".lhm-strips{", ".lhm-strips .lhm-st{", ".lhm-strips .lhm-st-o{"):
        assert cls in LIB, cls
    # every part is a widget: named for edit mode
    for w in ("'search · search'", "'open now · list'", "'panels · tree'", "'widgets · host'", "'sub-tabs · strip'"):
        assert w in LIB, w


def test_the_harness_builds_its_main_menu_from_the_component():
    assert "if(window.VeraLHM && typeof VeraLHM.side === 'function'){ _lhmNativeRender(host); _tabAttachHoverMenus(); return; }" in HARNESS
    assert "function _lhmNativeRender(host){" in HARNESS and "VeraLHM.side(box, {" in HARNESS
    assert "box.className = 'lhm-side-host'" in HARNESS, "the ☰ bar the absorb path drew stays above the list"
    assert "function _lhmNativeLegacy(host){" in HARNESS, "the hand-drawn list stays as the fallback"
    # the one panel set as Open now; the panels with the registry's sections and a panel's registered nav as one tree
    assert "open, panels, registered: { n: (_uiPanelCache || []).length, open: () => openTabPicker() }," in HARNESS
    assert "function _panelSectionsFor(pid){" in HARNESS and "Array.isArray(reg.sections)" in HARNESS and "secs.push({ id: '__nav'" in HARNESS
    assert "_lhmNavSelect(pid, sec.nav ? tab.id : (sec.id + '/' + tab.id))" in HARNESS
    # widgets in the menu: live events (the existing feed, lifted into a widget) and running loops
    assert "title: 'Live events'" in HARNESS and "el: feed, cls: 'events'" in HARNESS and "function _lhmLoopsWidget(){" in HARNESS
    assert "if(fe && host.contains(fe)) host.parentNode.insertBefore(fe, host.nextSibling);" in HARNESS, "the feed survives a rebuild"
    assert "if(_lhmEventsOpen) return;" not in HARNESS, "events no longer replace the list"
    assert "_lhmNavSync();   // the widget's header and its count follow" in HARNESS
    # the design's width; absorb untouched
    assert ".lhm-nav{display:none;flex-direction:column;width:186px;" in HARNESS
    assert "function _lhmRenderAbsorbed(host, pid, nav){" in HARNESS and "VeraLHM.absorb(abs, nav.lhm, id => {" in HARNESS


def test_a_side_menu_edits_every_part_is_a_widget():
    assert "function sideEdit(host, on){" in LIB and "sideEdit: sideEdit," in LIB
    assert "function _wireBars(root){" in LIB and "root = root || _host; if(!root) return;" in LIB, "the edit bars wire any root"
    assert "if(cfg.edit !== false){ var ed = _el('button', 'lhm-s-edit'" in LIB and "wrap.appendChild(_el('div', 'lhm-wcfg'));" in LIB, "the ✎ and the record sheet inside the side menu"
    assert "if(host._lhmEditing) sideEdit(host, true);" in LIB, "a re-render keeps the menu in edit mode"
    assert "open: () => openTabPicker() }, edit: true, id: 'harness-main'," in HARNESS and "var _sideEditOn = {};" in LIB


def test_a_tab_the_aide_opened_is_marked_and_wears_the_driven_ribbon():
    assert 'id="tabRibbon" data-w="driven ribbon · controls"' in HARNESS and "#tabBar .tab.driven .who{" in HARNESS
    assert "function _tabDrivenMark(){" in HARNESS and "t.classList.toggle('driven', driven);" in HARNESS
    assert "async function _panelsSetSync(){" in HARNESS and "name: 'ui.panels.open', arguments: { session_id: _veraSessionId }" in HARNESS, "the one panel set, read back"
    assert "origin: _tabOrigin[pid] || 'you', placement: 'harness tab'" in HARNESS, "the harness reports who opened each tab"
    assert "async function _drivenUndo(){" in HARNESS and "x.name === 'panel.open' && x.outcome === 'applied'" in HARNESS and "name: 'ui.directive.undo'" in HARNESS
    assert "function _drivenLog(){" in HARNESS


def test_tabs_mode_gets_the_sections_strip():
    _once(HARNESS, '<div id="tabSubtabs"></div>')
    assert "function _tabStripsRender(){" in HARNESS and "VeraLHM.strips(host, { title: row ? row.label : pid," in HARNESS
    assert "#tabSubtabs:empty,.body-wrap.lhm #tabSubtabs{display:none}" in HARNESS
    assert "  _tabStripsRender();\n  // the main LHM" in HARNESS, "rendered on every nav sync"
