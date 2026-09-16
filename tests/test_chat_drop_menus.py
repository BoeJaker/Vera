"""The chat's drop menus (Notes/42 defect 63).

A native <select> paints its closed control from the page but its OPEN list from the operating system — a system-white
list in the system's font over a dark themed UI, whatever the page says. The list is the chat's own now: the select
keeps its place, its styling, its value and its handlers, and only the popup is taken over.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


SRC = _read("vera", "chat", "chat_panel.html")


def test_every_select_wears_the_chats_caret_not_the_systems():
    # the rail's selects already did; the ones outside it — the agent selector at the top among them — did not
    assert "select{cursor:pointer;appearance:none;-webkit-appearance:none;padding-right:20px;background-image:url(" in SRC
    assert 'select[multiple],select[size]:not([size="1"]){background-image:none;padding-right:7px}' in SRC
    # the rail keeps its own rule (its own caret colour), which is more specific
    assert ".rpane select{cursor:pointer;appearance:none;" in SRC


def test_the_list_that_opens_is_the_chats_own():
    assert ".vsel-m{position:fixed;" in SRC and ".vsel-o{" in SRC and ".vsel-g{" in SRC
    assert "function _vselOpen(sel, quiet)" in SRC and "function _vselPick(o)" in SRC
    # one delegated listener takes the press before the OS popup — every select there is or ever will be
    assert "const s=t&&t.closest&&t.closest('select'); if(!s) return;" in SRC
    assert "ev.preventDefault(); ev.stopPropagation(); _vselOpen(s);" in SRC


def test_the_select_is_still_the_value_everything_reads():
    # nothing is wrapped, moved or hidden: a pick writes the value back and dispatches change as a native pick would,
    # and it picks the OPTION — a list that refilled while the menu was open may hold two of a value, or none of it
    assert "if(sel.contains(o)) o.selected=true; else sel.value=o.value;" in SRC
    assert "sel.dispatchEvent(new Event('change',{bubbles:true}))" in SRC
    # and opening focuses the select, so a list that fills on focus still fills
    assert "sel.dispatchEvent(new Event('focus'))" in SRC
    assert re.search(r'id="agentBarSel"[^>]*onfocus="CH\._refreshAgentList\(\)"', SRC)


def test_a_multiple_select_is_left_to_its_chips():
    # _enhanceMultiSelect already replaces those with toggle chips
    assert "if(s.multiple||s.disabled||(s.size>1)||s.classList.contains('ms-hidden')) return;" in SRC


def test_a_list_that_refills_while_it_is_open_is_drawn_again():
    assert "const mo=new MutationObserver(()=>{ if(_vsel&&_vsel.sel===sel) _vselOpen(sel, true); }); mo.observe(sel, {childList:true});" in SRC
    assert "function _vselOpen(sel, quiet)" in SRC and "v.mo&&v.mo.disconnect()" in SRC


def test_the_list_can_be_driven_from_the_keyboard():
    assert "if(ev.key==='Escape')" in SRC and "if(ev.key==='ArrowDown')" in SRC and "if(ev.key==='ArrowUp')" in SRC
    assert "if(ev.key==='Enter'||ev.key==='Tab')" in SRC
