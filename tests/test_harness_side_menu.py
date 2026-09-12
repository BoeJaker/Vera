"""
The harness side menu as the Harness board draws it (design landing, step 6 / B1b): the top row (☰ · Vera · N panels ·
⌘K), the search, Open now, the panels with sections, registered, the widgets (meters as bars · running loops · live
events), the note, the foot — vera-lhm.js side() gains the top row and meter widgets; the harness passes them and the
design stylesheet puts the menu on the board's surfaces. Text-level.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


LIB = _read("vera", "chat", "vera-lhm.js")
HTML = _read("vera", "capability_orchestration.html")


def test_the_library_draws_the_top_row_and_meter_widgets():
    assert "if(cfg.top){ var top = _el('div', 'lhm-s-top');" in LIB and "var tb = _el('button', 'lhm-s-tb'" in LIB
    assert "if(Array.isArray(w.bars) && w.bars.length){" in LIB and "var bar = _el('div', 'lhm-s-wbar');" in LIB
    for css in ("'.lhm-side .lhm-s-top{", "'.lhm-side .lhm-s-tb{", "'.lhm-side .lhm-s-wbar{"):
        assert css in LIB, css


def test_the_harness_passes_the_top_row_and_its_meters_and_wears_the_boards_surfaces():
    assert "top: { title: 'Vera', sub: ((_uiPanelCache || []).length || rows.length) + ' panels · ⌘K', toggle: () => tabToggleLhm()," in HTML
    assert "function _lhmMetersWidget(){" in HTML and "label: 'GPU · Ollama'" in HTML and "label: 'Queue'" in HTML
    assert "    _lhmMetersWidget(),\n    _lhmLoopsWidget(),\n    feed ? { title: 'Live events'" in HTML, "meters · running loops · live events, in the board's order"
    for css in (".lhm-nav{width:186px;background:var(--s1", "#lhmNav .lhm-side .lhm-s-row{height:32px", "#lhmNav .lhm-side .lhm-s-row.on{", "#lhmNav .lhm-side .lhm-s-w{background:var(--surf2", "#lhmNav .lhm-nav-foot{"):
        assert css in HTML, css
    for keep in ('id="lhmNavTabs"', 'id="lhmEventsFeed"', 'onclick="_lhmToggleEvents()"', 'onclick="tabToggleLhm()" title="Switch to horizontal tabs"'):
        assert keep in HTML, keep
