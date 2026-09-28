"""
Composing a menu (UI redesign control-plane carry-overs; the ChatMenu board's
edit mode, the lhm.compose directive): VeraLHM.compose edits a menu on the SAME
edit-mode path the pencil uses and leaves edit mode on for the user; Save as
menu... hands a record to the owner and to the rail; addMenus puts saved menus
back; lhm.menu.save/list/delete keep them. The files are text, so this runs
anywhere; the behaviour is smoked on a stand-in page.
"""
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


LHM = _read("vera", "chat", "vera-lhm.js")
CAPS = _read("vera", "chat", "chat_panels_capabilities.py")


def test_compose_is_exported_and_uses_the_edit_mode_path():
    assert "compose: compose, composeUndo: composeUndo, saveAsMenu: saveAsMenu, addMenus: addMenus," in LHM
    body = LHM[LHM.index("function compose(spec){"):LHM.index("function composeUndo(")]
    assert "toggleEdit(true);" in body, "compose turns edit mode on the way the pencil does"
    assert "pick(m.id, { silent:true })" in body and "_cfg.onCompose" in body
    assert "_menuHistory.push(" in body, "a compose can be undone"
    for op in ("spec.add", "spec.remove", "spec.order"):
        assert op in body, op


def test_save_as_menu_hands_the_record_to_the_owner_and_the_rail():
    body = LHM[LHM.index("function saveAsMenu(name){"):LHM.index("function addMenus(list){")]
    assert "_cfg.saveMenu(rec)" in body and "addMenus([rec])" in body
    assert "id:'menu:' +" in body and "items:(m.tabs || []).map(" in body
    add = LHM[LHM.index("function addMenus(list){"):LHM.index("// ── every part's record")]
    assert "own:true" in add and "if(_menu(rec.id)) return;" in add, "a saved menu is added once, beneath the built-ins"


def test_the_saved_menu_capabilities_exist():
    for name in ('"lhm.menu.save"', '"lhm.menu.list"', '"lhm.menu.delete"'):
        assert name in CAPS, name
    assert '_LHM_MENU_KEY = "vera:ui:lhm:menu:{owner}:{id}"' in CAPS
    assert "async def cap_lhm_menu_save(menu: Optional[dict] = None, owner: str = \"\", session_id: str = \"\"" in CAPS
    assert 'await emit_event({"type": "lhm.menu.save"' in CAPS and 'await emit_event({"type": "lhm.menu.delete"' in CAPS
