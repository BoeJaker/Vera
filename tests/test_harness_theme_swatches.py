"""
The harness header's theme swatches are the theme menu: a swatch calls _setTheme (the menu's own pick — the server's
choice, the vars, the broadcast to every panel, the button, the saved theme) and the swatches follow the current
theme whichever way it changes (every path ends in _updateThemeBtn). Text-level.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "capability_orchestration.html")


def test_a_swatch_is_the_menus_pick_and_follows_the_current_theme():
    sw = HTML[HTML.index("async function _hdrSwatches(){"):HTML.index("function _dashRecLabel(){")]
    assert "await _setTheme(s.dataset.theme);" in sw, "the same pick as the theme menu"
    assert "_applyThemeById(s.dataset.theme)" not in sw, "no second theme path"
    assert "const bg=v['--bg']||v['--bg0']||(t.type==='light'?'#f4f4f2':'#111318'), ac=v['--ac']||v['--acc']||t.accent||'#5a9e8f';" in sw, "the catalogue's own colours"
    assert "function _hdrSwatchesSync(){" in HTML and "function _hdrCurTheme(){" in HTML
    assert "function _updateThemeBtn(themeId){\n  try{ _hdrSwatchesSync(); }catch(e){}" in HTML, "every theme path syncs the swatches"
    assert "row.onclick = ()=>{ _setTheme(tid); menu.remove(); };" in HTML, "the menu's own pick is untouched"
