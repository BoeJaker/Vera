"""
The harness side menu as the Harness board draws it (design landing, step 6 / B1b): ONE header row (☰ · Vera · N panels
· ⌘K · search · ✎), Open now, the panels with sections, registered, the widgets (meters as bars · running loops · live
events), the note, the foot — vera-lhm.js side() draws the header and meter widgets; the harness passes them and the
design stylesheet puts the menu on the board's surfaces. Text-level.

The header used to be two rows — the ☰/title bar, and under it a search field with the ✎ beside it — which read as two
headers on a menu that has one. The second row is gone and what it carried moved up.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


LIB = _read("vera", "chat", "vera-lhm.js")
HTML = _read("vera", "capability_orchestration.html")


def test_the_library_draws_the_top_row_and_meter_widgets():
    assert "var top = _el('div', 'lhm-s-top'); top.setAttribute('data-w', 'header · header');" in LIB
    assert "var tb = _el('button', 'lhm-s-tb'" in LIB
    assert "if(Array.isArray(w.bars) && w.bars.length){" in LIB and "var bar = _el('div', 'lhm-s-wbar');" in LIB
    for css in ("'.lhm-side .lhm-s-top{", "'.lhm-side .lhm-s-tb{", "'.lhm-side .lhm-s-wbar{"):
        assert css in LIB, css


def test_the_side_menu_has_one_header_row_not_two():
    """The ☰/title row IS the header: the search and the ✎ sit on it, and the
    second row that used to hold them — .lhm-s-hd — is gone, markup and CSS."""
    assert "lhm-s-hd" not in LIB, "the second header row is still built"
    assert "lhm-s-hd" not in HTML, "the harness still styles a second header row"
    # both controls are appended to the one header row
    for line in ("s.addEventListener('click', function(){ if(cfg.search.open) cfg.search.open(); });\n      if(!cfg.top) top.appendChild(_el('span', 'nm', ''));   // keep the row's shape when there is no title\n      top.appendChild(s);",
                 "ed.addEventListener('click', function(){ sideEdit(host); }); top.appendChild(ed); }",
                 "if(top.childNodes.length) wrap.appendChild(top);"):
        assert line in LIB, line
    # the search is an icon button on the row, so its label rides the tooltip
    assert "s.title = (cfg.search.label || 'Find a panel') + ' · ' + (cfg.search.hint || '⌘K');" in LIB


def test_the_harness_passes_the_top_row_and_its_meters_and_wears_the_boards_surfaces():
    assert "top: { title: 'Vera', sub: ((_uiPanelCache || []).length || rows.length) + ' panels · ⌘K', toggle: () => tabToggleLhm()," in HTML
    assert "function _lhmMetersWidget(){" in HTML and "label: 'GPU · Ollama'" in HTML and "label: 'Queue'" in HTML
    assert "    _lhmMetersWidget(),\n    _lhmLoopsWidget(),\n    feed ? { title: 'Live events'" in HTML, "meters · running loops · live events, in the board's order"
    for css in (".lhm-nav{width:186px;background:var(--s1", "#lhmNav .lhm-side .lhm-s-row{height:32px", "#lhmNav .lhm-side .lhm-s-row.on{", "#lhmNav .lhm-side .lhm-s-w{background:var(--surf2", "#lhmNav .lhm-nav-foot{"):
        assert css in HTML, css
    for keep in ('id="lhmNavTabs"', 'id="lhmEventsFeed"', 'onclick="_lhmToggleEvents()"', 'onclick="tabToggleLhm()" title="Switch to horizontal tabs"'):
        assert keep in HTML, keep
