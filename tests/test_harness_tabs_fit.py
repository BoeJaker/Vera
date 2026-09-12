"""
The Harness board's tab bar fits its width and never scrolls: the open tabs first, then the rest in order as far as the
width beside the "+N ⌘K" chip and the tab controls allows; the remainder folds into the chip (its count; the picker
lists every registered panel, and opening one shows its tab). Re-fitted after every render, on resize, when a tab is
registered. Open tabs are never folded. Text-level.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "capability_orchestration.html")


def test_the_tab_bar_fits_and_never_scrolls():
    assert "function _tabsFit(){" in HTML
    assert ".tabs{overflow-x:hidden}" in HTML and ".tabs .tab.folded{display:none}" in HTML
    assert ".tabs .tab-controls{margin-left:auto;flex:0 0 auto;order:10}" in HTML, "tabs · chip · controls, in that order"
    assert "tabs.filter(open).forEach(t=>{ keep.add(t); avail-=w.get(t); });" in HTML, "open tabs are never folded"
    assert "tabs.filter(t=>!keep.has(t)).forEach(t=>{ if(avail-w.get(t)>=0){ keep.add(t); avail-=w.get(t); } });" in HTML, "then the rest, in order, while they fit"
    assert "chip.textContent=(folded?'+'+folded+' ':'+ ')+'⌘K';" in HTML, "the chip counts what folded"


def test_it_is_refitted_when_it_matters():
    assert "window.addEventListener('resize', _tabsFitSoon);" in HTML
    assert "try{ new MutationObserver(_tabsFitSoon).observe(bar,{childList:true}); }catch(_){} }" in HTML, "a tab registered later"
    assert "  _lhmNavSync();\n  // 6) the tab bar fits its width (the Harness board): the open tabs show, what does not fit folds into the chip\n  try{ _tabsFitSoon(); }catch(_){}\n}" in HTML, "after every render"
