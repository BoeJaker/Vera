"""
The quick menu's edit mode and the widget picker as the ChatMenu board draws them (design landing, step 7 / A8+A12):
✎ outlines every widget with its name and gives it ⋮⋮ ⚙ ⧉ ✕; the foot adds (the picker: the other menus' widgets,
every widget form, your templates), resets, saves as a menu of your own that keeps the composed body. Text-level.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


LIB = _read("vera", "chat", "vera-lhm.js")
HTML = _read("vera", "chat", "chat_panel.html")


def test_the_library_composes_the_quick_body_and_draws_the_picker():
    for fn in ("function _quickCompose(m){", "function _quickBars(m){", "function _quickFoot(m){", "function openPicker(){", "function _pickRender(m){", "function _addedWidget(m, a, i){", "function _qparts(m){"):
        assert fn in LIB, fn
    assert "var _QK = 'vera:lhm:quick:';" in LIB, "what you compose is remembered per menu"
    assert "'+ Add a widget — from any menu, any widget form, or your templates'" in LIB
    assert "_el('div', 'grp', 'From the other menus')" in LIB or "n:'From the other menus'" in LIB
    assert "grip.title = 'Drag to reorder';" in LIB and "rmB.title = qs.removed[key] ? 'Put it back' : 'Remove from this menu';" in LIB
    assert "from:m.id, widgets:_qspec(m) };" in LIB, "the saved menu carries the composition"
    assert "if(src && typeof src.quick === 'function'){ mm.quick = src.quick;" in LIB, "a menu of your own keeps the quick body"
    assert "if(el.closest && el.closest('.lhm-quick')) return;" in LIB, "the generic bars leave the quick body's widgets to their own"
    assert "try{ delete _quick.dataset.sig; }catch(e){}" in LIB
    assert "openPicker: openPicker, closePicker: closePicker," in LIB
    for css in ("'.lhm-editing .lhm-quick .wid{margin-top:10px;position:relative;", "'.lhm-wbar.lhm-qbar{", "'.lhm-wadd{", "'.lhm-pick{position:fixed;", "'.lhm-prow{"):
        assert css in LIB, css
    # Edit mode used to outline every part and print its name over the UI; the user read that as dev mode and it went
    # (Notes/42 defect 22). What is left is the working part of it - the edit bar, the per-part bars, the add row -
    # and the part under the pointer is marked by a quiet ring instead.
    # A dashed border still belongs to ONE thing - the "+ Add a widget" row, which is an empty slot asking to be
    # filled and reads as one. What went is the dashed outline drawn around every PART while editing.
    _dashed = [l for l in LIB.splitlines() if "outline:1px dashed" in l]
    assert all(".lhm-editing" not in l and "[data-w]" not in l and ".wid{" not in l for l in _dashed), "edit mode is not a blueprint: " + "; ".join(_dashed)[:200]
    assert "content:attr(data-w)" not in LIB, "and it does not print its own part names over the UI"
    assert "'.lhm-editing .lhm-quick .wid:hover{box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--acc) 34%,transparent)}'," in LIB, "the part you point at is marked quietly"
    assert "'.lhm-editing [data-w]:hover{box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--acc) 34%,transparent)}'," in LIB


def test_the_chat_feeds_the_picker_from_the_registry():
    assert "pickerSources:async()=>{" in HTML and "_capCall('widget.forms',{})" in HTML and "_capCall('widget.template.list',{session_id:SID||''})" in HTML
    assert "n:'Widget forms'" in HTML and "n:'Your templates'" in HTML
    assert "body .lhm-pick{background:" in HTML and "#rightRail.lhm-host .lhm-wbar.lhm-qbar{" in HTML
