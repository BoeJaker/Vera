"""
The one left-hand menu (UI redesign lhm-library, Notes/40 §2 + §9): vera-lhm.js
draws a 46px rail of quick menus beside a detail column — header, the menu's
tabs, its panes, one CTA — and a ☰ top-level list that swaps in place. The
chat owns such a menu; the harness can host it (absorb) through the panel
bridge's messages. Every part is a widget (data-w).

These tests hold what a redesign must never lose: every one of the chat's 13
tabs is still in the strip and belongs to exactly one rail menu; the panes
keep their ids; the library is served and loaded before the appearance
runtime; the owner publishes to a host only when it is embedded; the hosted
state hides only the rail and the strip; ☰, header, CTA and the edit mode
exist. Text-level, so it runs anywhere.
"""
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "chat", "chat_panel.html")
LIB = _read("vera", "chat", "vera-lhm.js")
CAPS = _read("vera", "chat", "chat_panels_capabilities.py")

TABS = ["Hist", "Cfg", "ctx", "loop", "memgraph", "dag", "code", "browser", "monitor", "goals", "loops", "Caps", "Panels"]
PANES = ["rpHist", "rpCfg", "rpCtx", "rpCaps", "rpPanels", "rpLoop", "paneLoopGraph", "paneGoals", "paneLoops", "paneMemGraph",
         "paneDagGraph", "paneList", "paneFabric", "paneDagHistory", "paneBrowser", "paneFrames", "paneCode", "paneMonitor"]


def _strip():
    i = HTML.index('<div class="ctx-tab-bar">')
    return HTML[i:HTML.index("</div>\n  <div id=\"ctxCtrl\">", i)]


def _menus_block():
    i = HTML.index("const LHM_MENUS=[")
    return HTML[i:HTML.index("];", i)]


# ── nothing lost ─────────────────────────────────────────────────────────────

def test_every_tab_is_still_in_the_strip():
    strip = _strip()
    for t in TABS:
        assert re.search(rf"CH\.(railTab|ctxTab)\('{t}'", strip), f"tab {t} left the strip"


def test_every_tab_belongs_to_exactly_one_rail_menu():
    block = _menus_block()
    ids = re.findall(r"tabs:\[([^\]]*)\]", block)
    assigned = []
    for group in ids:
        assigned += re.findall(r"id:'([^']+)'", group)
    for t in TABS:
        assert assigned.count(t) == 1, f"tab {t} assigned {assigned.count(t)} times"
    assert not set(assigned) - set(TABS), "a menu names a tab the strip does not have"


def test_the_rail_has_the_designs_eight_menus():
    block = _menus_block()
    menus = re.findall(r"\{id:'([a-z]+)',\s*icon:'([^']+)',\s*label:'([^']+)'", block)
    assert [m[0] for m in menus] == ["sessions", "context", "activity", "loop", "workspace", "sandbox", "ops", "settings"]
    assert all(m[1] for m in menus), "every rail menu carries an icon"
    assert [m[2] for m in menus] == ["Sessions", "Context", "Activity", "Loop", "Workspace", "Sandbox", "Ops glance", "Settings"]


def test_every_menu_has_a_meta_line_and_one_cta():
    block = _menus_block()
    assert block.count("meta:()=>") == 8
    # a menu's label may be a getter rather than a literal: the Context menu's reads "Expand to the full graph" or
    # "Fold back to the quick menu" depending on where the graph is, so both spellings count
    assert block.count("cta:{label:") + block.count("cta:{get label(") == 8


def test_the_panes_keep_their_ids():
    for p in PANES:
        assert f'id="{p}"' in HTML, f"pane {p} is gone"
    assert "['rpHist','rpCfg','rpCtx','rpCaps','rpPanels','rpLoop'].forEach" in HTML, "_unifyRails still folds the legacy panes"


# ── wiring ───────────────────────────────────────────────────────────────────

def test_the_library_is_loaded_before_the_appearance_runtime_and_mounted_after_the_rails_unify():
    assert HTML.index('<script src="/ui/vera-lhm.js"></script>') < HTML.index('<script src="/ui/vera-ui.js"></script>')
    init = HTML[HTML.index("setViewMode(savedMode);"):]
    assert init.index("_unifyRails();") < init.index("try{ _lhmMount(); }catch(e){")
    assert "VeraLHM.mount({host, title:'Vera', tabBar:'.ctx-tab-bar', menus:LHM_MENUS" in HTML


def test_the_library_is_served_beside_vera_ui_js():
    assert '@APP.get("/ui/vera-lhm.js", include_in_schema=False)' in CAPS
    assert 'Path(__file__).parent / "vera-lhm.js"' in CAPS
    assert os.path.exists(os.path.join(ROOT, "vera", "chat", "vera-lhm.js"))


def test_folding_keeps_the_rail_and_hosting_hides_only_rail_and_strip():
    assert "#rightRail.lhm-host.slim{width:46px !important;min-width:46px !important}" in HTML
    assert "#rightRail.lhm-host.slim > .lhm-det{display:none}" in HTML
    assert "html.vpb-nav-hosted #rightRail.lhm-host.slim{width:0 !important" in HTML
    assert "html.vpb-nav-hosted .lhm-rail,html.vpb-nav-hosted .lhm-det .ctx-tab-bar{display:none!important}" in LIB
    # the detail column (header, panes, CTA) is never hidden by hosting
    assert "vpb-nav-hosted .lhm-det{" not in LIB


def test_the_open_now_rows_know_who_opened_the_panel():
    assert "_panelOpenedBy=(arguments.length>1&&arguments[1])||'you';" in HTML
    assert HTML.count("panelOpen(direct.id,'aide')") == 1
    assert HTML.count("panelOpen(owner.id,'aide')") == 1
    assert "panelOpen(best.panel.id,'aide')" in HTML
    assert "origin:_panelOpenedBy||'you', placement:'beside chat'" in HTML


# ── the library ──────────────────────────────────────────────────────────────

def test_the_library_has_the_parts_and_names_them_as_widgets():
    assert "window.VeraLHM = { mount: mount, pick: pick, setActiveTab: setActiveTab, toggleTop: toggleTop, toggleEdit: toggleEdit, render: render, spec: spec, absorb: absorb" in LIB
    for tag in ("'rail · '", "'menu header · header'", "'tabs · strip'", "'top list · list'", "'cta · button'"):
        assert tag in LIB, f"{tag} is not a named widget"
    # the parts are still named as widgets - that is what the tags above check - but edit mode no longer STAMPS
    # the name across each of them: the user rejected that look (Notes/42 defect 22), so the name is on the part's
    # own bar and in its title instead
    assert "content:attr(data-w)" not in LIB
    assert "function _nameParts(root)" in LIB and "a widget of this menu" in LIB


def test_the_top_list_swaps_in_place_and_lists_what_is_open():
    assert ".lhm-topmode .lhm-det > :not(.lhm-hd):not(.lhm-top){display:none!important}" in LIB
    assert "'Open now · '" in LIB
    assert "'Menus · '" in LIB


def test_the_owner_publishes_only_when_embedded():
    """A standalone chat must never hear its own state on the listener it keeps for the panels it hosts."""
    assert "window.parent && window.parent !== window" in LIB
    assert "if(!_embedded || !_cfg) return;" in LIB
    assert "type: 'vera:panel:state'" in LIB and "lhm: s" in LIB


def test_the_owner_answers_the_hosts_messages():
    for m in ("'vera:panel:init'", "'vera:panel:nav_hosted'", "'vera:panel:nav_unhosted'", "d.action === 'nav_select'"):
        assert m in LIB, f"{m} unhandled"
    assert "classList.add('vpb-nav-hosted')" in LIB and "classList.remove('vpb-nav-hosted')" in LIB
    assert "type: 'vera:panel:action_result'" in LIB


def test_absorb_draws_the_rail_and_the_current_menus_tabs_from_a_spec():
    body = LIB[LIB.index("function absorb(host, spec, pickFn, opts){"):]
    assert "pickFn(m.id)" in body and "pickFn(cur.id + '/' + t.id)" in body
    assert "'rail · absorbed · '" in body and "'tabs · absorbed'" in body
