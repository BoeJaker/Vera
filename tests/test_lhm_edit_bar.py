"""
The LHM's edit mode you can see your way through (vera/chat/vera-lhm.js): an edit bar at the top of the menu while
editing — "Editing this menu · + Add widget · ✓ Done" — in the chat's quick menu and the harness's side menu; the side
menu's + Add widget opens the WidgetConfig sheet into 'side', keeps the record per menu and draws it live; ⚙ on a
record you added opens the sheet in edit mode; ✕ takes it out; Done leaves edit mode. Text-level.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def test_edit_bar_in_both_menus():
    src = _read("vera", "chat", "vera-lhm.js")
    assert "function _ebar(title, onAdd, onDone)" in src
    assert "'.lhm-editing .lhm-ebar,.lhm-editing > .lhm-side > .lhm-ebar{display:flex}'" in src
    assert "_el('button', 'pri', '+ Add widget')" in src and "_el('button', '', '✓ Done')" in src
    # the quick menu: first in the body, cleaned with the editor's parts
    assert "if(_editing){ _quickEbar(m); _quickBars(m); _quickFoot(m); }" in src
    assert "_quick.insertBefore(bar, _quick.firstChild)" in src
    assert "'.lhm-ebar, .lhm-wedit, .lhm-added-grp, .wid[data-added], .lhm-wbar.lhm-qbar'" in src
    # the side menu: under the header; Done leaves edit mode
    assert "wrap.appendChild(_ebar((cfg.top && cfg.top.title) || 'this menu', function(){ sideAdd(host, cfg); }, function(){ sideEdit(host, false); }))" in src


def test_side_menu_add_path_and_records():
    src = _read("vera", "chat", "vera-lhm.js")
    assert "function sideAdd(host, cfg)" in src and "into:'side'" in src
    assert "function _sideAddedOf(host)" in src and "'vera.lhm.side.added.'" in src
    assert "var box = _el('div', 'lhm-s-w lhm-s-added')" in src and "document.createElement('vera-widget')" in src
    assert "host._lhmSideCfg = cfg" in src and "function _sideRedraw(host)" in src
    assert "sideAdd: sideAdd" in src


def test_gear_on_an_added_record_opens_the_sheet():
    src = _read("vera", "chat", "vera-lhm.js")
    assert "S.open({ mode:'edit', into:'lhm', record:ai.record" in src
    assert "S.open({ mode:'edit', into:'side', record:rec" in src
    assert "rmB.title = 'Take it out of this menu'" in src
