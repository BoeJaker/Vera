"""
The harness absorbs the open panel's menu (UI redesign lhm-library, the
Harness board): while the active tab's panel publishes a full LHM spec (the
chat's eight quick menus), the harness's own left-hand menu draws that panel's
rail and its current menu's tabs IN PLACE of its tab list, and a ☰ bar swaps
the tab list back in the same place — one menu, never a second one beside it.

These tests hold the wiring that makes it one menu: the library is loaded; a
state from a frame no init ever addressed is resolved by its source window
(the chat's iframe is created by its own mount script, so the load poll never
addresses it); a full LHM spec is hosted only in the vertical menu, so in the
horizontal tab bar the panel keeps its own rail; the absorbed rendering
replaces the tab list and the ☰ swap is a state flip of the same host, not a
new element; picks go back to the panel as nav_select. Text-level.
"""
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "capability_orchestration.html")


def test_the_library_is_loaded_after_the_appearance_runtime():
    assert HTML.index('<script src="/ui/vera-ui.js?v=scale2"></script>') < HTML.index('<script src="/ui/vera-lhm.js"></script>')


def test_an_unaddressed_state_is_resolved_by_its_source_and_the_frame_learns_its_id():
    assert "function _panelIdOfSource(src)" in HTML
    assert "if(f.contentWindow === src){ const p = f.closest('.panel'); return p ? _panelIdFromEl(p) : ''; }" in HTML
    body = HTML[HTML.index("if(!d || typeof d!=='object' || d.type!=='vera:panel:state') return;"):]
    assert "const pid = _panelIdOfSource(e.source); if(!pid) return;" in body[:600]
    assert "e.source.postMessage({type:'vera:panel:init', panel_id: pid, session_id:''}, '*');" in body[:900]


def test_a_full_lhm_is_hosted_only_in_the_vertical_menu():
    assert "function _navHostedFor(nav){ return _navHostingAllowed() && (!(nav && nav.lhm) || _lhmMode); }" in HTML
    assert "type: _navHostedFor(nav) ? 'vera:panel:nav_hosted' : 'vera:panel:nav_unhosted'" in HTML
    # re-evaluated per panel when the mode or the autohide toggles change
    assert "const msg = { type: _navHostedFor(_panelNavCache[pid]) ? 'vera:panel:nav_hosted' : 'vera:panel:nav_unhosted' };" in HTML
    apply = HTML[HTML.index("function _tabApplyLhm(){"):]
    assert "_reapplyNavHosting();" in apply[:apply.index("\n}\n") + 3]


def test_the_absorbed_menu_replaces_the_tab_list_in_the_same_host():
    sync = HTML[HTML.index("function _lhmNavSync(){"):]
    sync = sync[:sync.index("\n}\n")]
    assert "const absorbed = _lhmAbsorbedFor();" in sync
    assert "if(absorbed && _lhmRenderAbsorbed(host, absorbed.pid, absorbed.nav)) return;" in sync
    # rendered into #lhmNavTabs itself — no second nav element is created
    assert "const host = document.getElementById('lhmNavTabs'); if(!host) return;" in sync
    render = HTML[HTML.index("function _lhmRenderAbsorbed(host, pid, nav){"):]
    render = render[:render.index("\n}\n")]
    assert "VeraLHM.absorb(abs, nav.lhm, id => {" in render
    assert "host.appendChild(bar)" in render and "host.appendChild(abs)" in render


def test_the_menu_button_swaps_in_place_and_picks_go_back_to_the_panel():
    render = HTML[HTML.index("function _lhmRenderAbsorbed(host, pid, nav){"):]
    render = render[:render.index("\n}\n")]
    assert "tb.addEventListener('click', ()=>{ _lhmAbsorbTop = !_lhmAbsorbTop; _lhmNavSync(); });" in render
    assert "if(_lhmAbsorbTop) return false;" in render, "with the swap on, the caller draws the ordinary tab list under the bar"
    assert "_lhmNavSelect(pid, id);" in render
    assert "onTop: () => _lhmNavSelect(pid, '☰')" in render
    # the swap resets when the active tab changes
    assert "if(absorbed && absorbed.pid !== _lhmAbsorbPid){ _lhmAbsorbPid = absorbed.pid; _lhmAbsorbTop = false; }" in HTML


def test_the_absorbed_menu_has_room_for_the_rail_and_the_tabs():
    assert ".body-wrap.lhm .lhm-nav.absorbed{width:212px}" in HTML
    assert ".body-wrap.lhm .main.autohide-reserve-v.absorbed{padding-left:212px}" in HTML
    assert re.search(r"\.lhm-nav \.lhm-topbar\{display:flex", HTML)


def test_the_generic_section_list_and_the_hover_dropdown_survive():
    """Panels that register a plain section list keep the behaviour they had."""
    assert "sub.className = 'subitem' + (it.id===nav.active ? ' on' : '');" in HTML
    assert "function _tabAttachHoverMenus(){" in HTML
    assert "function _showTabDropdown(tabEl, pid, nav){" in HTML
